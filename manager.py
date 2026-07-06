"""The core: clone repos, spawn/kill Claude sessions in tmux, and reconcile
what's on disk + in tmux against what's tracked in the database.

No shell=True anywhere; every external command is passed argv-style so repo
URLs and paths can't inject shell syntax.
"""
import os
import re
import shlex
import shutil
import subprocess
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
    matches = re.findall(config.RELAY_REGEX, text)
    if not matches:
        return None
    for m in matches:
        if "claude" in m:
            return m.rstrip(".,)")
    return matches[0].rstrip(".,)")


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
        # The GitLab host comes from the repo URL (it isn't encoded in the
        # token); an optional stored host is only a fallback for odd URLs.
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

    session = f"claude-{iid}"
    launch = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-c", workdir, config.CLAUDE_RC_CMD],
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

    label = (name or "").strip() or _repo_basename(repo_url)
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


def _repo_basename(repo_url: str) -> str:
    tail = repo_url.rstrip("/").rsplit("/", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def _dir_ctime(path: str) -> str:
    try:
        ts = os.stat(path).st_ctime
        return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return ""
