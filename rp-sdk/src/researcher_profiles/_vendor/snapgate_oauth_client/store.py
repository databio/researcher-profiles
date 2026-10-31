# SNAPGATE module - do not edit. Overwritten on updates. Configure via ClientConfig.
"""Where logins are kept: one store contract, a JSON file store and a memory store.

The contract (a database store follows it too):

- ``locked(key)`` keeps OTHER PROCESSES out of that key for the whole ``with``
  block. For a database: its own session, ``SELECT ... FOR UPDATE`` on the
  row, closed (rolled back) when the block exits. Threads in one process are
  kept out by the caller's lock (``TokenSource(lock=...)``; ``ConnectClient``
  always passes one). ``FileStore`` and ``MemoryStore`` also lock threads
  themselves.
- The yielded entry has ``get()`` and ``put(cred | None)``. ``put`` is durable
  when it returns (a database store commits inside ``put``), so a rotated
  token is saved before anyone uses it, and a tombstone survives the
  ``LoginEnded`` raised right after it. ``put`` is the last write in a locked
  block (a commit releases a Postgres row lock). ``put(None)`` forgets the key.
- ``load`` may run unlocked only if writes are atomic (a reader never sees a
  half-written login).
- A tombstone is a ``Credential`` with ``ended_at``/``ended_reason`` set and no
  tokens (:func:`tombstone`), so a status page can say WHY a login ended.
- A store that cannot read a stored login raises ``CredentialStoreError``.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from .errors import CredentialStoreError
from .models import Credential, CredentialKey

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows: the threading lock only
    fcntl = None  # type: ignore[assignment]

FILE_VERSION = 1


class LockedEntry(Protocol):
    def get(self) -> Credential | None: ...

    def put(self, cred: Credential | None) -> None: ...  # None forgets it; durable at once


class CredentialStore(Protocol):
    def load(self, key: CredentialKey) -> Credential | None: ...  # unlocked read

    def locked(self, key: CredentialKey) -> AbstractContextManager[LockedEntry]: ...


def tombstone(cred: Credential, reason: str, at: int) -> Credential:
    """``cred`` with its tokens removed and why/when it ended."""
    return replace(
        cred,
        access_token="",
        access_expires_at=None,
        refresh_token=None,
        ended_at=at,
        ended_reason=reason,
    )


class _Entry:
    """A LockedEntry over a store's own load/put; the store's lock is already held."""

    def __init__(self, store: FileStore | MemoryStore, key: CredentialKey):
        self._store, self._key = store, key

    def get(self) -> Credential | None:
        return self._store.load(self._key)

    def put(self, cred: Credential | None) -> None:
        self._store.put(self._key, cred)


# -- the file store ------------------------------------------------------------------


def _ensure_dir(path: Path) -> None:
    """Create ``path`` (mode 0700) if it is missing; an existing folder is left alone."""
    if not path.exists():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)


class _PathLock:
    """One lock per credentials file: a re-entrant thread lock plus ``flock``.

    The ``flock`` is taken once per outermost acquire, so a thread that already
    holds it (``put`` inside ``locked``) does not deadlock on its own lock.
    """

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._rlock = threading.RLock()
        self._depth = 0
        self._fd: int | None = None

    def acquire(self) -> None:
        self._rlock.acquire()
        try:
            if self._depth == 0 and fcntl is not None:
                _ensure_dir(self.lock_path.parent)
                fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                self._fd = fd
            self._depth += 1
        except BaseException:
            self._rlock.release()
            raise

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        self._rlock.release()


_path_locks: dict[str, _PathLock] = {}
_path_locks_guard = threading.Lock()


def _lock_for(path: Path) -> _PathLock:
    key = str(path.absolute())
    with _path_locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = _path_locks[key] = _PathLock(path.with_name(path.name + ".lock"))
        return lock


class FileStore:
    """Many logins in one JSON file (CLIs and local services).

    Mode 0600 (its folder 0700 when created), written atomically: a 0600 temp
    file in the same folder, ``fsync``, then ``os.replace``. The lock is
    ``fcntl.flock`` on ``<path>.lock`` plus a per-path thread lock (on Windows,
    the thread lock only). A missing file is empty. A file that is not JSON or
    has no ``"version": 1`` raises :class:`CredentialStoreError` on read; a
    write (a new login) replaces it.
    """

    def __init__(self, path: str | os.PathLike, *, login_command: str | None = None):
        self.path = Path(path)
        self.login_command = login_command
        self._lock = _lock_for(self.path)

    # -- the contract

    def load(self, key: CredentialKey) -> Credential | None:
        return self._get(self._read(), key)

    @contextlib.contextmanager
    def locked(self, key: CredentialKey) -> Iterator[LockedEntry]:
        self._lock.acquire()
        try:
            yield _Entry(self, key)
        finally:
            self._lock.release()

    def put(self, key: CredentialKey, cred: Credential | None) -> None:
        """Store ``cred`` under ``key`` (``None`` forgets it). Takes the lock itself.

        An unreadable or old-format file is replaced, not an error.
        """
        self._lock.acquire()
        try:
            logins = self._read(tolerant=True)
            if cred is not None:
                entry = cred.to_dict()
                if key.owner:
                    entry["owner"] = key.owner
                logins[key.storage_key] = entry
            elif logins.pop(key.storage_key, None) is None and not self._damaged():
                return
            self._write(logins)
        finally:
            self._lock.release()

    def keys(self) -> list[CredentialKey]:
        """Every stored key (not part of the store contract)."""
        out = []
        for raw in self._read().values():
            if isinstance(raw, dict) and isinstance(raw.get("server"), str):
                owner = raw.get("owner")
                out.append(CredentialKey(raw["server"], owner if isinstance(owner, str) else None))
        return out

    # -- the file

    def _damaged_error(self) -> CredentialStoreError:
        fix = (
            f"Run `{self.login_command}` to replace it."
            if self.login_command
            else "Sign in again to replace it."
        )
        return CredentialStoreError(f"{self.path} is from an older version or damaged. {fix}")

    def _damaged(self) -> bool:
        try:
            self._read()
        except CredentialStoreError:
            return True
        return False

    def _read(self, *, tolerant: bool = False) -> dict:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as e:
            if tolerant:
                return {}
            raise CredentialStoreError(f"Could not read {self.path} ({e}).") from None
        try:
            raw = json.loads(text)
        except ValueError:
            raw = None
        if (
            not isinstance(raw, dict)
            or raw.get("version") != FILE_VERSION
            or not isinstance(raw.get("logins"), dict)
        ):
            if tolerant:
                return {}
            raise self._damaged_error()
        return raw["logins"]

    def _get(self, logins: dict, key: CredentialKey) -> Credential | None:
        raw = logins.get(key.storage_key)
        if raw is None:
            return None
        if isinstance(raw, dict):
            raw = {k: v for k, v in raw.items() if k != "owner"}
        try:
            return Credential.from_dict(raw)
        except CredentialStoreError:
            raise self._damaged_error() from None

    def _write(self, logins: dict) -> None:
        parent = self.path.parent
        _ensure_dir(parent)
        data = json.dumps({"version": FILE_VERSION, "logins": logins}, indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        os.chmod(self.path, 0o600)
        if hasattr(os, "O_DIRECTORY"):
            with contextlib.suppress(OSError):
                dfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)


# -- the memory store ----------------------------------------------------------------


class MemoryStore:
    """A dict plus a lock (tests, short-lived processes). One process only."""

    def __init__(self):
        self._data: dict[CredentialKey, Credential] = {}
        self._lock = threading.RLock()

    def load(self, key: CredentialKey) -> Credential | None:
        with self._lock:
            return self._data.get(key)

    @contextlib.contextmanager
    def locked(self, key: CredentialKey) -> Iterator[LockedEntry]:
        with self._lock:
            yield _Entry(self, key)

    def put(self, key: CredentialKey, cred: Credential | None) -> None:
        """Store ``cred`` under ``key`` (``None`` forgets it)."""
        with self._lock:
            if cred is None:
                self._data.pop(key, None)
            else:
                self._data[key] = cred
