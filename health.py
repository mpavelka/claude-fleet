"""Environment health: probe the external tools and services the app relies on.

Each probe is cheap (a `--version` call, or `docker info` for the daemon) and
time-bounded so a wedged binary can't hang a request. Results feed the
expandable status indicator in the UI.
"""
import shutil
import subprocess

import crypto

_TIMEOUT = 5  # seconds per probe


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT)
        out = (r.stdout.strip() or r.stderr.strip())
        first = out.splitlines()[0].strip() if out else ""
        return r.returncode, first
    except (subprocess.TimeoutExpired, OSError):
        return 1, ""


def _probe_tool(name: str, version_args: list[str], required: bool) -> dict:
    path = shutil.which(name)
    if not path:
        return {
            "name": name,
            "required": required,
            "path": None,
            "version": None,
            "detail": "not found on PATH",
            "state": "error" if required else "absent",
        }
    _, version = _run([name, *version_args])
    return {
        "name": name,
        "required": required,
        "path": path,
        "version": version or "installed",
        "detail": None,
        "state": "ok",
    }


def _probe_docker() -> dict:
    """Docker is optional; distinguish 'not installed' from 'installed but the
    daemon isn't responding'."""
    item = _probe_tool("docker", ["--version"], required=False)
    if item["state"] != "ok":
        return item
    rc, server = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if rc == 0 and server:
        item["detail"] = f"daemon running (server {server})"
    else:
        item["state"] = "warn"
        item["detail"] = "installed; daemon not responding"
    return item


def _probe_claude_account() -> dict:
    """Whether the server is signed in to a Claude account (required to spawn
    remote-control sessions)."""
    import auth_claude

    st = auth_claude.status()
    if st.get("loggedIn"):
        return {
            "name": "claude account",
            "required": False,
            "path": None,
            "version": st.get("email") or "signed in",
            "detail": st.get("subscriptionType"),
            "state": "ok",
        }
    return {
        "name": "claude account",
        "required": False,
        "path": None,
        "version": None,
        "detail": "not signed in — use the Claude account section",
        "state": "warn",
    }


def _probe_secret_key() -> dict:
    """Not a binary, but a service dependency: without it credential storage is
    disabled."""
    ok = crypto.available()
    return {
        "name": "credential encryption",
        "required": False,
        "path": None,
        "version": "FLEET_SECRET_KEY set" if ok else None,
        "detail": None if ok else f"{crypto.key_message()} credentials disabled",
        "state": "ok" if ok else "warn",
    }


def check() -> dict:
    """Return {'overall': <state>, 'checks': [...]} where state is one of
    ok | warn | error. (Key is 'checks', not 'items', to avoid colliding with
    dict.items in Jinja attribute lookup.)"""
    checks = [
        _probe_tool("tmux", ["-V"], required=True),
        _probe_tool("git", ["--version"], required=True),
        _probe_tool("claude", ["--version"], required=True),
        _probe_claude_account(),
        _probe_docker(),
        _probe_secret_key(),
    ]
    if any(c["state"] == "error" for c in checks):
        overall = "error"
    elif any(c["state"] == "warn" for c in checks):
        overall = "warn"
    else:
        overall = "ok"
    return {"overall": overall, "checks": checks}
