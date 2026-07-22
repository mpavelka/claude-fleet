"""Environment health: probe the external tools and services the app relies on.

Each probe is cheap (a `--version` call, or `docker info` for the daemon) and
time-bounded so a wedged binary can't hang a request. Results feed the
expandable status indicator in the UI.
"""
import os
import shutil
import subprocess

import config
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


def _docker_info_server(env: dict[str, str] | None = None) -> str:
    """Run `docker info` and return the server version from stdout only, or ''
    on any failure. Checking stdout specifically matters: the docker CLI can
    exit 0 even when it can't reach a daemon, printing its "Cannot connect..."
    message to stderr -- a naive stdout-or-stderr read would mistake that
    error text for a real server version."""
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=_TIMEOUT, env=env,
        )
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _probe_docker() -> dict:
    """Local `docker` CLI + ambient daemon (e.g. a plain host deployment with
    Docker installed directly). In the sandboxed K3s architecture there is
    deliberately no local daemon -- see _probe_docker_sandbox for the actual
    relevant check there."""
    item = _probe_tool("docker", ["--version"], required=False)
    if item["state"] != "ok":
        return item
    server = _docker_info_server()
    if server:
        item["detail"] = f"daemon running (server {server})"
    else:
        item["state"] = "warn"
        item["detail"] = "installed; daemon not responding"
    return item


def _probe_docker_sandbox() -> dict:
    """Whether spawned sessions have been wired to a sandboxed Docker daemon
    (config.DOCKER_HOST) -- see docs/deployment-k3s.md. Optional: most
    deployments won't set this, and that's a normal, unremarkable state."""
    base = {"name": "sandboxed docker", "required": False, "path": None}
    if not config.DOCKER_HOST:
        return {
            **base,
            "version": None,
            "detail": "not configured — spawned sessions have no Docker access",
            "state": "absent",
        }
    env = dict(os.environ, DOCKER_HOST=config.DOCKER_HOST)
    if config.DOCKER_TLS_VERIFY:
        env["DOCKER_TLS_VERIFY"] = config.DOCKER_TLS_VERIFY
    if config.DOCKER_CERT_PATH:
        env["DOCKER_CERT_PATH"] = config.DOCKER_CERT_PATH
    server = _docker_info_server(env)
    if server:
        return {
            **base,
            "version": f"reachable (server {server})",
            "detail": config.DOCKER_HOST,
            "state": "ok",
        }
    return {
        **base,
        "version": None,
        "detail": f"configured ({config.DOCKER_HOST}) but not reachable",
        "state": "warn",
    }


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
        _probe_docker_sandbox(),
        _probe_secret_key(),
    ]
    if any(c["state"] == "error" for c in checks):
        overall = "error"
    elif any(c["state"] == "warn" for c in checks):
        overall = "warn"
    else:
        overall = "ok"
    return {"overall": overall, "checks": checks}
