# SNAPGATE module - do not edit. Overwritten on updates. Configure via ClientConfig.
"""``TokenSource``: the one place a login is renewed, saved and ended.

A refresh token is presented only while holding the store's lock, and only
after a fresh re-read, so two processes never spend the same token (the second
would present a retired token and, after the grace window, end the login).
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import TypeVar

from .errors import CredentialStoreError, LoginEnded, login_ended, not_signed_in
from .grants import refresh_credential, revoke_credential
from .http import Http, UrllibHttp
from .models import ClientConfig, Credential, CredentialKey
from .store import CredentialStore, LockedEntry, tombstone

T = TypeVar("T")


class TokenSource:
    """A fresh access token for one stored login, and saving or ending that login.

    ``lock`` (optional) is held around every locked block: it keeps threads of
    this process out, while ``store.locked`` keeps other processes out.
    ``on_ended(tombstone)`` is called once, after the lock is released, when a
    refresh finds the login has ended (never for a login that already was a
    tombstone); then the ``LoginEnded`` is raised.

    A 403 (for example ``key_confined``) is never a reason to refresh: it is a
    valid key in the wrong place, and the host reports it.
    """

    def __init__(
        self,
        config: ClientConfig,
        store: CredentialStore,
        key: CredentialKey,
        *,
        http: Http | None = None,
        now: Callable[[], float] = time.time,
        lock: AbstractContextManager | None = None,
        on_ended: Callable[[Credential], None] | None = None,
    ):
        self.config = config
        self.store = store
        self.key = key
        self.http = http or UrllibHttp(config.app_name)
        self.now = now
        self.lock = lock
        self.on_ended = on_ended

    @contextlib.contextmanager
    def _locked(self) -> Iterator[LockedEntry]:
        with self.lock or contextlib.nullcontext(), self.store.locked(self.key) as entry:
            yield entry

    def _live(self, cred: Credential | None) -> Credential:
        """``cred`` if it is a live login; else ``NotSignedIn`` or ``LoginEnded``."""
        if cred is None:
            raise not_signed_in(self.key.server, self.config)
        if cred.ended:
            raise login_ended(
                self.key.server,
                self.config,
                reason=cred.ended_reason or "ended",
                ended_at=cred.ended_at,
            )
        return cred

    def access_token(self) -> str:
        """The stored access token, refreshed first if it expires within the margin."""
        cred = self._live(self.store.load(self.key))
        if not cred.expiring(self.now(), self.config.refresh_margin):
            return cred.access_token
        return self._renew(lambda cred: cred.expiring(self.now(), self.config.refresh_margin))

    def after_unauthorized(self, rejected: str) -> str:
        """The server answered 401 to ``rejected``. Refresh once, unless someone already did."""
        return self._renew(lambda cred: cred.access_token == rejected)

    def call(
        self,
        fn: Callable[[str], T],
        *,
        unauthorized: tuple[type[BaseException], ...] = (PermissionError,),
    ) -> T:
        """``fn(token)``; on ``unauthorized``, :meth:`after_unauthorized` and retry exactly once."""
        token = self.access_token()
        try:
            return fn(token)
        except unauthorized:
            fresh = self.after_unauthorized(token)
        return fn(fresh)

    def save(self, cred: Credential) -> None:
        """Store a new login under this key, then revoke the login it replaced
        (best effort, after saving; a tombstone or unreadable entry is not revoked)."""
        with self._locked() as entry:
            old = self._read(entry)
            entry.put(cred)
        if old is not None and not old.ended:
            revoke_credential(old, self.config, http=self.http)

    def sign_out(self) -> bool:
        """Revoke the stored login (best effort) and forget it. True if the server confirmed."""
        confirmed = False
        with self._locked() as entry:
            cred = self._read(entry)
            if cred is not None and not cred.ended:
                confirmed = revoke_credential(cred, self.config, http=self.http)
            entry.put(None)
        return confirmed

    @staticmethod
    def _read(entry: LockedEntry) -> Credential | None:
        try:
            return entry.get()
        except CredentialStoreError:
            return None  # an unreadable entry is replaced

    def _renew(self, needed: Callable[[Credential], bool]) -> str:
        """Under the lock: re-read, and refresh when ``needed(cred)`` still holds."""
        ended: Credential | None = None
        with self._locked() as entry:
            cred = self._live(entry.get())  # another process may have refreshed
            if not needed(cred):
                return cred.access_token
            try:
                fresh = refresh_credential(cred, self.config, http=self.http, now=self.now)
            except LoginEnded as e:
                ended = tombstone(cred, e.reason, int(self.now()))
                entry.put(ended)
                error = e
            else:
                entry.put(fresh)  # saved before anyone uses it
                return fresh.access_token
        if self.on_ended is not None:
            self.on_ended(ended)
        raise error
