"""The core: clone repos, spawn/kill Claude sessions in tmux, and reconcile
what's on disk + in tmux against what's tracked in the database.

No shell=True anywhere; every external command is passed argv-style so repo
URLs and paths can't inject shell syntax.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit

import config
import crypto
import db


class SpawnError(Exception):
    """Raised when cloning or launching an instance fails."""


# --------------------------------------------------------------------------- #
# tmux / filesystem helpers
# --------------------------------------------------------------------------- #
def _tmux_alive(session: str) -> bool:
    return (
        subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True,
        ).returncode
        == 0
    )


def _kill_tmux(session: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)


def _clean_url(m: str) -> str:
    """Trim a scraped URL: cut at the first control/escape byte (Claude prints
    the URL inside an OSC-8 hyperlink escape) and drop trailing punctuation."""
    m = re.split(r"[\x00-\x1f\x7f]", m)[0]
    return m.rstrip(".,)\"'")


def _scan_relay(workdir: str) -> str | None:
    """Look for the relay URL in the captured session log. Prefer a Claude
    URL if several links were printed."""
    logfile = os.path.join(workdir, ".relay.log")
    if not os.path.isfile(logfile):
        return None
    try:
        with open(logfile, "r", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    matches = [_clean_url(m) for m in re.findall(config.RELAY_REGEX, text)]
    matches = [m for m in matches if m]
    if not matches:
        return None
    for m in matches:
        if "claude" in m:
            return m
    return matches[0]


# --------------------------------------------------------------------------- #
# Git credential injection
# --------------------------------------------------------------------------- #
def _git(workdir: str, *args: str) -> None:
    subprocess.run(["git", "-C", workdir, *args], capture_output=True, check=True)


def _to_https(repo_url: str, fallback_host: str) -> str:
    """Normalize a repo URL to a token-less HTTPS URL so the credential store
    helper applies. Handles https(with creds), git@host:path, and ssh://."""
    u = repo_url.strip()
    if u.startswith(("http://", "https://")):
        p = urlsplit(u)
        netloc = p.hostname + (f":{p.port}" if p.port else "")
        return urlunsplit(("https", netloc, p.path, "", ""))
    if u.startswith("ssh://"):
        p = urlsplit(u)
        host = p.hostname or fallback_host
        return f"https://{host}{p.path}"
    if u.startswith("git@") or ("@" in u.split(":", 1)[0] and ":" in u):
        # scp-like: user@host:group/repo.git
        userhost, path = u.split(":", 1)
        host = userhost.split("@", 1)[-1] or fallback_host
        return f"https://{host}/{path.lstrip('/')}"
    return u


def _write_credfile(iid: str, host: str, username: str, token: str) -> str:
    """Write a git credential-store file for this instance, outside any working
    tree, readable only by us. Returns its path."""
    secrets_dir = os.path.join(config.SECRETS_ROOT, iid)
    os.makedirs(secrets_dir, mode=0o700, exist_ok=True)
    credfile = os.path.join(secrets_dir, ".git-credentials")
    line = f"https://{quote(username, safe='')}:{quote(token, safe='')}@{host}\n"
    # Open with restrictive perms before writing the secret.
    fd = os.open(credfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(line)
    return credfile


def _remove_secrets(iid: str) -> None:
    shutil.rmtree(os.path.join(config.SECRETS_ROOT, iid), ignore_errors=True)


# --------------------------------------------------------------------------- #
# Claude workspace trust + remote-control confirmation
# --------------------------------------------------------------------------- #
def _claude_json_path() -> str:
    """Where Claude stores per-project state (incl. trust). CLAUDE_CONFIG_DIR
    relocates it; otherwise it's ~/.claude.json."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~")
    return os.path.join(base, ".claude.json")


def _trust_workdir(workdir: str) -> None:
    """Mark a directory as trusted so `claude remote-control` doesn't block on
    the interactive workspace-trust dialog. Edits Claude's project config in
    place, preserving everything else and the file's 0600 perms."""
    path = _claude_json_path()
    # Claude keys trust by the resolved path (getcwd resolves symlinks, e.g.
    # /tmp -> /private/tmp on macOS), so realpath must match, not abspath.
    abspath = os.path.realpath(workdir)
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        # Never overwrite a config we couldn't parse (could be a transient race).
        raise SpawnError(f"Could not read Claude config at {path} to grant trust: {exc}")

    projects = data.setdefault("projects", {})
    entry = projects.get(abspath) or {}
    entry["hasTrustDialogAccepted"] = True
    entry.setdefault("hasCompletedProjectOnboarding", True)
    projects[abspath] = entry

    tmp = f"{path}.fleet-{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)  # atomic


_RC_PROMPT = "Enable Remote Control?"


def _confirm_remote_control(session: str, logfile: str, timeout: float = 30) -> None:
    """`claude remote-control` asks 'Enable Remote Control? (y/n)' on start.
    Wait for that prompt in the captured log and answer 'y' exactly once. Only
    fires when the prompt is actually seen, so it's a no-op for other commands."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _tmux_alive(session):
            return
        try:
            with open(logfile, "r", errors="replace") as fh:
                if _RC_PROMPT in fh.read():
                    subprocess.run(
                        ["tmux", "send-keys", "-t", session, "y", "Enter"],
                        capture_output=True,
                    )
                    return
        except OSError:
            pass
        time.sleep(0.5)


def _remote_control_cmd(label: str) -> str:
    """The shell command tmux runs. For a bare remote-control invocation, add
    --name (to identify the session in claude.ai/code) and --spawn same-dir (to
    skip the interactive spawn-mode prompt; each instance is its own clone)."""
    cmd = config.CLAUDE_RC_CMD
    if "remote-control" in cmd:
        if "--name" not in cmd:
            cmd = f"{cmd} --name {shlex.quote(label)}"
        if "--spawn" not in cmd:
            cmd = f"{cmd} --spawn same-dir"
    return cmd


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def spawn(repo_url: str, name: str | None, credential_id: str | None = None) -> str:
    """Clone `repo_url` into a fresh working tree and launch Claude in a
    detached tmux session. If a credential is given, git in that tree is wired
    to authenticate with it. Returns the new instance id."""
    repo_url = repo_url.strip()
    if not repo_url:
        raise SpawnError("A repository URL is required.")

    cred = db.get_credential(credential_id) if credential_id else None
    if credential_id and cred is None:
        raise SpawnError("Selected credential no longer exists.")

    iid = uuid.uuid4().hex[:12]
    workdir = os.path.join(config.FLEET_ROOT, iid)
    os.makedirs(config.FLEET_ROOT, exist_ok=True)

    # Prepare credential material before cloning so the token is never on a
    # command line or in the pane log -- only the credfile path is.
    credfile = None
    clone_url = repo_url
    if cred is not None:
        token = crypto.decrypt(cred["secret_enc"])
        # The git host comes from the repo URL (it isn't encoded in the token);
        # an optional stored host is only a fallback for odd URLs.
        fallback_host = cred["host"] if "host" in cred.keys() else ""
        clone_url = _to_https(repo_url, fallback_host)
        host = urlsplit(clone_url).hostname or fallback_host
        if not host:
            raise SpawnError(f"Could not determine the git host from '{repo_url}'.")
        username = (cred["username"] or "oauth2").strip()
        credfile = _write_credfile(iid, host, username, token)

    if credfile:
        clone_cmd = [
            "git", "-c", f"credential.helper=store --file={credfile}",
            "clone", clone_url, workdir,
        ]
    else:
        clone_cmd = ["git", "clone", clone_url, workdir]

    clone = subprocess.run(clone_cmd, capture_output=True, text=True)
    if clone.returncode != 0:
        shutil.rmtree(workdir, ignore_errors=True)
        _remove_secrets(iid)
        raise SpawnError(f"git clone failed: {clone.stderr.strip() or clone.stdout.strip()}")

    # Persist auth + commit identity on this clone only (local config).
    if cred is not None:
        # Reset any inherited global helpers, then add ours -> full isolation.
        _git(workdir, "config", "--replace-all", "credential.helper", "")
        _git(workdir, "config", "--add", "credential.helper", f"store --file={credfile}")
        if cred["git_name"]:
            _git(workdir, "config", "user.name", cred["git_name"])
        if cred["git_email"]:
            _git(workdir, "config", "user.email", cred["git_email"])

    # Pre-accept the workspace-trust dialog so remote-control doesn't exit.
    _trust_workdir(workdir)

    label = (name or "").strip() or _repo_basename(repo_url)
    session = f"claude-{iid}"
    launch = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-x", "220", "-y", "50",
         "-c", workdir, _remote_control_cmd(label)],
        capture_output=True,
        text=True,
    )
    if launch.returncode != 0:
        shutil.rmtree(workdir, ignore_errors=True)
        _remove_secrets(iid)
        raise SpawnError(f"tmux launch failed: {launch.stderr.strip()}")

    # Mirror the pane to a log file we can later scan for the relay URL.
    logfile = os.path.join(workdir, ".relay.log")
    subprocess.run(
        ["tmux", "pipe-pane", "-t", session, "-o", f"cat >> {shlex.quote(logfile)}"],
        capture_output=True,
    )

    # Answer the "Enable Remote Control? (y/n)" prompt in the background.
    threading.Thread(
        target=_confirm_remote_control, args=(session, logfile), daemon=True
    ).start()

    db.add_instance(iid, label, repo_url, workdir, session, credential_id=credential_id)
    return iid


def kill(iid: str) -> None:
    """Stop the tmux session but keep the working tree (it becomes an orphan
    the UI can inspect and later clean up)."""
    row = db.get(iid)
    if row is None:
        return
    _kill_tmux(row["tmux_session"])
    db.mark_stopped(iid)


def cleanup(workdir: str) -> None:
    """Delete a working tree on disk. Works for both tracked orphans and
    untracked leftover directories. Refuses paths outside FLEET_ROOT."""
    absw = os.path.abspath(workdir)
    root = os.path.abspath(config.FLEET_ROOT)
    if absw != root and not absw.startswith(root + os.sep):
        raise ValueError("Refusing to remove a path outside FLEET_ROOT.")
    if absw == root:
        raise ValueError("Refusing to remove the root directory.")

    row = db.get_by_workdir(absw)
    if row is not None:
        _kill_tmux(row["tmux_session"])
        db.delete(row["id"])
    shutil.rmtree(absw, ignore_errors=True)
    # Drop any credential material for this instance (id == workdir basename).
    _remove_secrets(os.path.basename(absw))


# --------------------------------------------------------------------------- #
# Read model
# --------------------------------------------------------------------------- #
def list_instances() -> list[dict]:
    """Unified view combining the DB, live tmux sessions, and directories on
    disk. Directories with no DB row surface as untracked orphans."""
    result: list[dict] = []
    seen_dirs: set[str] = set()
    cred_names = {c["id"]: c["name"] for c in db.all_credentials()}

    for row in db.all_instances():
        item = dict(row)
        running = _tmux_alive(item["tmux_session"])
        workdir_exists = os.path.isdir(item["workdir"])

        if running and not item["relay_url"]:
            url = _scan_relay(item["workdir"])
            if url:
                db.set_relay(item["id"], url)
                item["relay_url"] = url

        item["running"] = running
        item["workdir_exists"] = workdir_exists
        item["tracked"] = True
        item["credential_name"] = cred_names.get(item.get("credential_id"))
        item["status"] = (
            "running" if running else "orphan" if workdir_exists else "missing"
        )
        result.append(item)
        seen_dirs.add(os.path.abspath(item["workdir"]))

    # Untracked directories (e.g. leftovers from a crash or a deleted DB row).
    if os.path.isdir(config.FLEET_ROOT):
        for name in sorted(os.listdir(config.FLEET_ROOT)):
            path = os.path.abspath(os.path.join(config.FLEET_ROOT, name))
            if not os.path.isdir(path) or path in seen_dirs:
                continue
            result.append(
                {
                    "id": f"untracked:{name}",
                    "name": "(untracked)",
                    "repo_url": None,
                    "workdir": path,
                    "tmux_session": None,
                    "relay_url": None,
                    "created_at": _dir_ctime(path),
                    "stopped_at": None,
                    "running": False,
                    "workdir_exists": True,
                    "tracked": False,
                    "status": "orphan",
                }
            )

    return result


def get_instance(iid: str) -> dict | None:
    """The unified view for a single instance (tracked or untracked)."""
    return next((i for i in list_instances() if i["id"] == iid), None)


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def instance_log(workdir: str, max_bytes: int = 200_000) -> str:
    """Return the captured tmux pane output for an instance, cleaned of ANSI
    escapes. Reads the tail so a long-running session's log stays bounded."""
    if not workdir:
        return ""
    path = os.path.join(workdir, ".relay.log")
    if not os.path.isfile(path):
        return ""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            data = fh.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return _ANSI_RE.sub("", text)


def _repo_basename(repo_url: str) -> str:
    tail = repo_url.rstrip("/").rsplit("/", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def _dir_ctime(path: str) -> str:
    try:
        ts = os.stat(path).st_ctime
        return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return ""
