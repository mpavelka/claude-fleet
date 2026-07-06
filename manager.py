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

import config
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
# Lifecycle
# --------------------------------------------------------------------------- #
def spawn(repo_url: str, name: str | None) -> str:
    """Clone `repo_url` into a fresh working tree and launch Claude in a
    detached tmux session. Returns the new instance id."""
    repo_url = repo_url.strip()
    if not repo_url:
        raise SpawnError("A repository URL is required.")

    iid = uuid.uuid4().hex[:12]
    workdir = os.path.join(config.FLEET_ROOT, iid)
    os.makedirs(config.FLEET_ROOT, exist_ok=True)

    clone = subprocess.run(
        ["git", "clone", repo_url, workdir],
        capture_output=True,
        text=True,
    )
    if clone.returncode != 0:
        # Nothing to clean up beyond a possibly-partial dir.
        shutil.rmtree(workdir, ignore_errors=True)
        raise SpawnError(f"git clone failed: {clone.stderr.strip() or clone.stdout.strip()}")

    session = f"claude-{iid}"
    launch = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-c", workdir, config.CLAUDE_RC_CMD],
        capture_output=True,
        text=True,
    )
    if launch.returncode != 0:
        shutil.rmtree(workdir, ignore_errors=True)
        raise SpawnError(f"tmux launch failed: {launch.stderr.strip()}")

    # Mirror the pane to a log file we can later scan for the relay URL.
    logfile = os.path.join(workdir, ".relay.log")
    subprocess.run(
        ["tmux", "pipe-pane", "-t", session, "-o", f"cat >> {shlex.quote(logfile)}"],
        capture_output=True,
    )

    label = (name or "").strip() or _repo_basename(repo_url)
    db.add_instance(iid, label, repo_url, workdir, session)
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


# --------------------------------------------------------------------------- #
# Read model
# --------------------------------------------------------------------------- #
def list_instances() -> list[dict]:
    """Unified view combining the DB, live tmux sessions, and directories on
    disk. Directories with no DB row surface as untracked orphans."""
    result: list[dict] = []
    seen_dirs: set[str] = set()

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
