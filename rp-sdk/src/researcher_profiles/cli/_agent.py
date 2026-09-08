"""``rp agent`` and ``rp profile``: the agent credential and profile-editing verbs.

Both verbs resolve a credential from ``credentials.toml``, build a
``ManagementClient`` from it, and dispatch on a subcommand. Everything here
talks to a remote server, so every import of the client machinery is deferred
into the handler that needs it.
"""

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ._shared import EXIT_ERROR, EXIT_OK, EXIT_USAGE, _add_host, add_json, add_subcommand


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Register this group's verbs on the root subparser action."""
    p_agent = add_subcommand(
        sub,
        "agent",
        "Agent credential and identity commands",
        "rp agent whoami",
        "rp agent scopes",
        "rp agent config",
    )
    agent_sub = p_agent.add_subparsers(dest="agent_cmd", metavar="<subcommand>")

    p_agent_whoami = add_subcommand(
        agent_sub,
        "whoami",
        "Show agent identity and scopes",
        "rp agent whoami",
        "rp agent whoami --json",
    )
    add_json(p_agent_whoami, "JSON output")
    _add_host(p_agent_whoami)

    p_agent_scopes = add_subcommand(
        agent_sub,
        "scopes",
        "Show the scope catalog",
        "rp agent scopes",
        "rp agent scopes --json",
    )
    add_json(p_agent_scopes, "JSON output")
    _add_host(p_agent_scopes)

    p_agent_config = add_subcommand(
        agent_sub,
        "config",
        "Show resolved credential source",
        "rp agent config",
    )
    _add_host(p_agent_config)

    p_profile = add_subcommand(
        sub,
        "profile",
        "Agent profile editing commands (pull/diff/push/visibility)",
        "rp profile pull -o profile.md",
        "rp profile diff profile.md",
        "rp profile push profile.md",
    )
    profile_sub = p_profile.add_subparsers(dest="profile_cmd", metavar="<subcommand>")

    p_pull = add_subcommand(
        profile_sub,
        "pull",
        "Fetch profile as a round-trip document",
        "rp profile pull",
        "rp profile pull -o my-profile.md",
    )
    p_pull.add_argument(
        "-o", "--output", default="profile.md", help="Output file (default: profile.md)"
    )
    _add_host(p_pull)

    p_diff = add_subcommand(
        profile_sub,
        "diff",
        "Show what a push would change",
        "rp profile diff profile.md",
    )
    p_diff.add_argument("file", help="Path to the round-trip document")
    _add_host(p_diff)

    p_push = add_subcommand(
        profile_sub,
        "push",
        "Push changed fields from a round-trip document",
        "rp profile push profile.md",
        "rp profile push profile.md --dry-run",
    )
    p_push.add_argument("file", help="Path to the round-trip document")
    p_push.add_argument("--if-match", help="Base hash for conflict detection")
    p_push.add_argument("--force", action="store_true", help="Skip base_hash check")
    p_push.add_argument(
        "--dry-run", action="store_true", help="Show what would change without writing"
    )
    _add_host(p_push)

    p_vis = add_subcommand(
        profile_sub,
        "visibility",
        "Get or set profile visibility",
        "rp profile visibility get",
        "rp profile visibility set --tier internal",
    )
    p_vis.add_argument("visibility_cmd", choices=["get", "set"], help="get or set")
    p_vis.add_argument("--tier", help="Visibility tier (for set)")
    _add_host(p_vis)


def _parse_roundtrip(path: Path) -> dict:
    """Parse a pull document: YAML frontmatter + body."""
    import yaml

    text = path.read_text()
    if not text.startswith("---"):
        return {"frontmatter": {}, "body": text}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {"frontmatter": {}, "body": text}
    front = yaml.safe_load(parts[1]) or {}
    body = parts[2].lstrip("\n")
    return {"frontmatter": front, "body": body if body.strip() else None}


def _editable_fields(source: Mapping[str, Any]) -> dict[str, Any]:
    """Non-None values for every field in ``EDITABLE_METADATA_FIELDS``, sorted."""
    from ..profile.edit import EDITABLE_METADATA_FIELDS

    out: dict[str, Any] = {}
    for field_name in sorted(EDITABLE_METADATA_FIELDS):
        val = source.get(field_name)
        if val is not None:
            out[field_name] = val
    return out


def _agent_config(cred) -> int:
    """Print which credential was resolved and where it came from."""
    print(f"source:  {cred.source}")
    print(f"url:     {cred.url}")
    print(f"key:     {cred.key[:20]}...")
    if cred.profile:
        print(f"profile: {cred.profile}")
    if cred.owner:
        print(f"owner:   {cred.owner}")
    if cred.scopes:
        print(f"scopes:  {', '.join(cred.scopes)}")
    return EXIT_OK


def _agent_whoami(client, args: argparse.Namespace) -> int:
    """Print the agent's identity, tier, scopes and editable profiles."""
    from .auth.agent import AgentAPIError

    try:
        result = client.identity.whoami()
    except AgentAPIError as e:
        print(f"Error: {e.detail}", file=sys.stderr)
        return EXIT_ERROR
    if args.as_json:
        print(json.dumps(result, indent=2))
        return EXIT_OK
    p = result.get("principal", {})
    print(f"Agent:   {p.get('label', '?')} ({p.get('handle', '?')})")
    owner = result.get("owner")
    if owner:
        print(f"Owner:   {owner.get('name', '?')} ({owner.get('orcid', '?')})")
    print(f"Tier:    {result.get('tier', '?')}")
    print(f"Scopes:  {', '.join(result.get('scopes', []))}")
    not_granted = result.get("scopes_not_granted", [])
    if not_granted:
        print(f"Missing: {', '.join(not_granted)}")
    for prof in result.get("profiles", []):
        print(f"Profile: {prof.get('slug', '?')} ({prof.get('role', '?')})")
    return EXIT_OK


def _agent_scopes(client, args: argparse.Namespace) -> int:
    """Print the server's scope catalog."""
    from .auth.agent import AgentAPIError

    try:
        result = client.identity.scopes()
    except AgentAPIError as e:
        print(f"Error: {e.detail}", file=sys.stderr)
        return EXIT_ERROR
    if args.as_json:
        print(json.dumps(result, indent=2))
        return EXIT_OK
    for name, info in result.items():
        flag = " [DANGEROUS]" if info.get("dangerous") else ""
        default = " (default)" if info.get("default_on") else ""
        print(f"  {name}{default}{flag}")
        print(f"    {info.get('description', '')}")
        if info.get("fields"):
            print(f"    Fields: {', '.join(info['fields'])}")
    return EXIT_OK


#: ``rp agent`` subcommands that need a ``ManagementClient``. ``config`` does
#: not, because it reports the credential itself and never calls the server.
_AGENT_HANDLERS: dict[str, Callable[..., int]] = {
    "whoami": _agent_whoami,
    "scopes": _agent_scopes,
}


def _cmd_agent(args: argparse.Namespace) -> int:
    """Handle the ``agent`` verb group (credential and identity)."""
    from .auth.agent import CredentialError, ManagementClient, resolve_credential

    if not args.agent_cmd:
        print("rp agent: subcommand required (whoami, scopes, config)", file=sys.stderr)
        return EXIT_USAGE

    try:
        cred = resolve_credential(host=args.host)
    except CredentialError as e:
        print(str(e), file=sys.stderr)
        return EXIT_ERROR

    if args.agent_cmd == "config":
        return _agent_config(cred)

    handler = _AGENT_HANDLERS.get(args.agent_cmd)
    if handler is None:
        print(f"rp agent: unknown subcommand {args.agent_cmd!r}", file=sys.stderr)
        return EXIT_USAGE
    return handler(ManagementClient(cred), args)


def _fetch_soul(cred, slug: str, manifest: list) -> str:
    """Fetch the profile's SOUL body, or warn and return an empty one.

    The body is written into the round-trip document and a later ``rp profile
    push`` writes it back, so a fetch that failed must say so: pushing an empty
    SOUL that was never actually read would erase the real one.
    """
    soul_url = ""
    for entry in manifest or []:
        if entry.get("role") == "soul" or "SOUL" in entry.get("content_url", ""):
            soul_url = entry.get("content_url", "")
            break
    if not soul_url or soul_url.startswith("http"):
        return ""
    try:
        import httpx
    except ImportError as e:
        print(
            f"warning: cannot fetch SOUL ({e}); the round-trip document's body will be empty",
            file=sys.stderr,
        )
        return ""
    url = f"{cred.url.rstrip('/')}/api/v1/profiles/{slug}/content/{soul_url}"
    try:
        resp = httpx.get(url, headers={"Authorization": f"Bearer {cred.key}"}, timeout=30.0)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(
            f"warning: cannot fetch SOUL from {url}: {e}; "
            "the round-trip document's body will be empty",
            file=sys.stderr,
        )
        return ""
    return resp.text


def _profile_pull(client, cred, slug: str, args: argparse.Namespace) -> int:
    """Write the profile out as a round-trip document (frontmatter plus SOUL)."""
    import yaml

    from .auth.agent import AgentAPIError

    try:
        data = client.profile.get(slug)
    except AgentAPIError as e:
        print(f"Error: {e.detail}", file=sys.stderr)
        return EXIT_ERROR
    content_hash = data.get("content_hash")
    soul = _fetch_soul(cred, slug, data.get("manifest") or [])

    front = {
        "slug": slug,
        "rid": data.get("rid"),
        "base_hash": content_hash,
        **_editable_fields(data.get("metadata", {})),
    }

    out = Path(args.output)
    with open(out, "w") as f:
        f.write("---\n")
        yaml.dump(front, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        f.write("---\n\n")
        f.write(soul)

    print(f"Wrote {out}", file=sys.stderr)
    if content_hash:
        print(f"base_hash: {content_hash}", file=sys.stderr)
    return EXIT_OK


def _profile_diff(client, slug: str, args: argparse.Namespace) -> int:
    """Report which fields a push from this document would change."""
    from .auth.agent import AgentAPIError

    path = Path(args.file)
    if not path.is_file():
        print(f"File not found: {path}", file=sys.stderr)
        return EXIT_ERROR
    local = _parse_roundtrip(path)
    try:
        remote = client.profile.get(slug)
    except AgentAPIError as e:
        print(f"Error: {e.detail}", file=sys.stderr)
        return EXIT_ERROR
    remote_meta = remote.get("metadata", {})
    changed = [
        name
        for name, value in _editable_fields(local["frontmatter"]).items()
        if value != remote_meta.get(name)
    ]
    if local.get("body") is not None:
        changed.append("soul")
    if changed:
        print(f"Would change: {', '.join(changed)}")
    else:
        print("No changes detected.")
    return EXIT_OK


def _profile_push(client, slug: str, args: argparse.Namespace) -> int:
    """Push the document's changed metadata fields, and its SOUL body if present."""
    from .auth.agent import AgentAPIError

    path = Path(args.file)
    if not path.is_file():
        print(f"File not found: {path}", file=sys.stderr)
        return EXIT_ERROR
    local = _parse_roundtrip(path)
    base_hash = args.if_match or local["frontmatter"].get("base_hash")
    if args.force:
        base_hash = None

    patch = _editable_fields(local["frontmatter"])

    if args.dry_run:
        print(f"Would patch: {', '.join(sorted(patch.keys()))}")
        if local.get("body"):
            print("Would update soul.")
        return EXIT_OK

    try:
        if patch:
            result = client.profile.patch_metadata(slug, patch, base_hash=base_hash)
            print(f"Metadata: updated {', '.join(result.get('updated', []))}")
            base_hash = result.get("content_hash")
        if local.get("body") is not None:
            client.profile.put_soul(slug, local["body"], base_hash=base_hash)
            print("Soul: updated")
    except AgentAPIError as e:
        print(f"Error: {e.detail}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def _profile_visibility(client, slug: str, args: argparse.Namespace) -> int:
    """Read or set the profile's visibility tier."""
    from .auth.agent import AgentAPIError

    if args.visibility_cmd == "get":
        try:
            data = client.profile.get(slug)
        except AgentAPIError as e:
            print(f"Error: {e.detail}", file=sys.stderr)
            return EXIT_ERROR
        print(f"visibility: {data.get('metadata', {}).get('visibility', '?')}")
        return EXIT_OK
    if not args.tier:
        print("--tier required for set", file=sys.stderr)
        return EXIT_USAGE
    try:
        client.profile.patch_visibility(slug, profile_visibility=args.tier)
        print(f"Visibility set to {args.tier}")
    except AgentAPIError as e:
        print(f"Error: {e.detail}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def _cmd_profile(args: argparse.Namespace) -> int:
    """Handle the ``profile`` verb group (agent-side profile editing)."""
    from .auth.agent import CredentialError, ManagementClient, resolve_credential

    if not args.profile_cmd:
        print("rp profile: subcommand required (pull, diff, push, visibility)", file=sys.stderr)
        return EXIT_USAGE

    try:
        cred = resolve_credential(host=args.host)
    except CredentialError as e:
        print(str(e), file=sys.stderr)
        return EXIT_ERROR

    client = ManagementClient(cred)
    slug = cred.profile
    if not slug:
        print("No profile configured. Set 'profile' in credentials.toml.", file=sys.stderr)
        return EXIT_ERROR

    if args.profile_cmd == "pull":
        return _profile_pull(client, cred, slug, args)
    if args.profile_cmd == "diff":
        return _profile_diff(client, slug, args)
    if args.profile_cmd == "push":
        return _profile_push(client, slug, args)
    if args.profile_cmd == "visibility":
        return _profile_visibility(client, slug, args)
    print(f"rp profile: unknown subcommand {args.profile_cmd!r}", file=sys.stderr)
    return EXIT_USAGE


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "agent": _cmd_agent,
    "profile": _cmd_profile,
}
