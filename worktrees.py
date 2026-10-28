"""Worktrees of remote-control sessions, per instance.

In worktree spawn mode every session remote-control serves runs in its own git
worktree under <clone>/.claude/worktrees/, named `bridge-cse_<id>` after the
claude.ai session (`session_<id>`). Claude Code leaves enough on disk to tell
which of those are still in use, without asking remote-control:

- `.git/worktrees/<name>/CLAUDE_BASE`: Claude's "I created this" marker,
  holding the commit the worktree started from.
- `.git/worktrees/<name>/locked`: `claude agent <name> (pid P start S)`, held
  by the remote-control server while the session is attached and released
  when it is archived or the server stops. `start` is the kernel start time
  (/proc/P/stat field 22), which tells a live owner from a reused pid -- e.g.
  a lock left behind by a pod restart.
- `$CLAUDE_CONFIG_DIR/sessions/<pid>.json`: one record per live Claude process
  (cwd, busy/idle, claude.ai session id), stale files left by killed ones.
- `$CLAUDE_CONFIG_DIR/projects/<cwd, non-alnum -> "-">/*.jsonl`: transcripts,
  including `pr-link` records for every PR a session opened. They outlive the
  worktree, so PRs of removed worktrees stay listed.

remote-control itself removes the worktree of a session archived with a clean
git status. What it leaves behind (sessions archived with uncommitted files,
sessions still open when the server died) is what `sweep` handles: a worktree
nothing is live in and whose `git status` is clean is removed. Its branch is
deleted too unless it holds commits of its own -- those stay as a local branch
so committed work is never lost, merged or not (squash merges make "merged"
unknowable from git alone).
"""
import glob
import json
import os
import re
import subprocess
import threading
import time

import config
import db

WORKTREES_SUBDIR = os.path.join(".claude", "worktrees")
# Worktrees younger than this are never swept: closes the window between
# remote-control creating one and taking its lock.
MIN_AGE_SECONDS = 120
BRIDGE_PREFIX = "bridge-cse_"


# --------------------------------------------------------------------------- #
# Claude state on disk
# --------------------------------------------------------------------------- #
def _claude_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def _proc_start(pid: int) -> str | None:
    """Kernel start time of `pid` (field 22 of /proc/<pid>/stat), or None if
    there is no such process. The command name (field 2) may contain spaces
    and parentheses, so fields are counted from its closing paren."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            stat = fh.read()
    except OSError:
        return None
    rest = stat[stat.rfind(")") + 2:].split()
    return rest[19] if len(rest) > 19 else None


def _alive(pid, start=None) -> bool:
    try:
        actual = _proc_start(int(pid))
    except (TypeError, ValueError):
        return False
    return actual is not None and (start is None or str(start) == actual)


def live_sessions() -> dict[str, dict]:
    """Live Claude session records keyed by realpath of their cwd."""
    out: dict[str, dict] = {}
    for path in glob.glob(os.path.join(_claude_dir(), "sessions", "*.json")):
        try:
            with open(path) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if rec.get("cwd") and _alive(rec.get("pid"), rec.get("procStart")):
            out[os.path.realpath(rec["cwd"])] = rec
    return out


_LOCK_RE = re.compile(r"\(pid (\d+) start (\d+)\)")


def _lock_owner_alive(reason: str | None) -> bool:
    """Whether a Claude-set worktree lock is held by a live process. A lock
    without Claude's pid/start stamp was set by hand: treat it as held."""
    if reason is None:
        return False
    m = _LOCK_RE.search(reason)
    return _alive(m.group(1), m.group(2)) if m else True


def _project_dir_name(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


_pr_cache: dict[str, tuple[float, int, list[dict]]] = {}


def _transcript_prs(path: str) -> list[dict]:
    """`pr-link` records in one transcript, cached on (mtime, size): active
    transcripts run to megabytes and the detail page polls."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    hit = _pr_cache.get(path)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    prs = []
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                if '"pr-link"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") == "pr-link" and rec.get("prUrl"):
                    prs.append({
                        "url": rec["prUrl"],
                        "number": rec.get("prNumber"),
                        "repo": rec.get("prRepository"),
                        "at": rec.get("timestamp") or "",
                        "cwd": None,
                    })
    except OSError:
        return []
    _pr_cache[path] = (st.st_mtime, st.st_size, prs)
    return prs


def _prs_in(project_dirs: list[str], cwd_for: dict[str, str]) -> list[dict]:
    """PRs from every transcript in `project_dirs`, newest first, one entry per
    PR. `cwd_for` maps a project dir to the working directory it belongs to."""
    seen: dict[str, dict] = {}
    for d in project_dirs:
        for t in glob.glob(os.path.join(d, "*.jsonl")):
            for pr in _transcript_prs(t):
                prev = seen.get(pr["url"])
                if prev is None or pr["at"] > prev["at"]:
                    seen[pr["url"]] = dict(pr, cwd=cwd_for.get(d))
    return sorted(seen.values(), key=lambda p: p["at"], reverse=True)


def worktree_prs(path: str) -> list[dict]:
    d = os.path.join(_claude_dir(), "projects", _project_dir_name(path))
    return _prs_in([d], {d: path})


def instance_prs(workdir: str) -> list[dict]:
    """PRs opened by any session of an instance: in the clone itself (same-dir
    mode) or in any of its worktrees, including ones since removed."""
    projects = os.path.join(_claude_dir(), "projects")
    root = _project_dir_name(os.path.realpath(workdir))
    wt_prefix = root + "--claude-worktrees-"
    dirs, cwd_for = [], {}
    try:
        names = os.listdir(projects)
    except OSError:
        return []
    for name in names:
        if name == root or name.startswith(wt_prefix):
            d = os.path.join(projects, name)
            dirs.append(d)
            # Encoding is lossy; label by the worktree name it ends in.
            cwd_for[d] = workdir if name == root else name[len(wt_prefix):]
    return _prs_in(dirs, cwd_for)


# --------------------------------------------------------------------------- #
# Git
# --------------------------------------------------------------------------- #
def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    """Run git; a hang or a missing binary reads as a failed command, so one
    stuck repo can't take down the dashboard poll or a re-run."""
    try:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def _admin_dir(path: str) -> str | None:
    """A linked worktree's metadata dir (.git/worktrees/<name>), read from its
    `.git` file rather than assumed from the directory name."""
    try:
        with open(os.path.join(path, ".git")) as fh:
            line = fh.read().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    d = line[len("gitdir:"):].strip()
    return d if os.path.isabs(d) else os.path.normpath(os.path.join(path, d))


def _read(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _porcelain(workdir: str) -> list[dict]:
    r = _git(workdir, "worktree", "list", "--porcelain")
    if r.returncode != 0:
        return []
    entries, cur = [], {}
    for line in r.stdout.splitlines() + [""]:
        if not line:
            if cur:
                entries.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(" ")
        cur[key] = val if val else True
    return entries


def list_worktrees(workdir: str, sessions: dict | None = None) -> list[dict]:
    """Worktrees under <workdir>/.claude/worktrees with who (if anyone) is in
    them and what work they hold. `sessions` lets callers share one
    live_sessions() scan across instances."""
    if not os.path.isdir(os.path.join(workdir, ".git")):
        return []
    sessions = live_sessions() if sessions is None else sessions
    base_dir = os.path.realpath(os.path.join(workdir, WORKTREES_SUBDIR)) + os.sep
    out = []
    for e in _porcelain(workdir):
        path = os.path.realpath(e.get("worktree", ""))
        if not path.startswith(base_dir) or "bare" in e:
            continue
        name = os.path.basename(path)
        admin = _admin_dir(path)
        base = _read(os.path.join(admin, "CLAUDE_BASE")) if admin else None
        lock = e.get("locked")
        lock = (lock if isinstance(lock, str) else "") if lock else None
        missing = "prunable" in e or not os.path.isdir(path)

        changed = None
        ahead = None
        if not missing:
            st = _git(path, "status", "--porcelain")
            changed = len(st.stdout.splitlines()) if st.returncode == 0 else None
            if base:
                rc = _git(path, "rev-list", "--count", f"{base}..HEAD")
                ahead = int(rc.stdout.strip()) if rc.returncode == 0 else None

        session = sessions.get(path)
        active = session is not None or _lock_owner_alive(lock)
        session_id = None
        if name.startswith(BRIDGE_PREFIX):
            session_id = "session_" + name[len(BRIDGE_PREFIX):]
        elif session and session.get("bridgeSessionId"):
            session_id = session["bridgeSessionId"]
        # CLAUDE_BASE is written once at creation; the admin dir's own mtime
        # moves whenever git drops an index.lock in it.
        try:
            age = time.time() - os.stat(os.path.join(admin, "CLAUDE_BASE")).st_mtime
        except (OSError, TypeError):
            age = 0

        out.append({
            "name": name,
            "path": path,
            "branch": (e.get("branch") or "").removeprefix("refs/heads/") or None,
            "claude_made": base is not None,
            "active": active,
            "status": (session or {}).get("status"),
            "session_url": f"https://claude.ai/code/{session_id}" if session_id else None,
            "changed": changed,
            "ahead": ahead,
            "missing": missing,
            # Removable by the sweep: nothing live, Claude made it, clean
            # status (unknown status counts as not clean), not brand new.
            "removable": (not active and base is not None and not missing
                          and changed == 0 and age >= MIN_AGE_SECONDS),
        })
    out.sort(key=lambda w: (not w["active"], w["name"]))
    return out


def summary(workdir: str, sessions: dict) -> dict:
    wts = list_worktrees(workdir, sessions)
    return {
        "active": sum(w["active"] for w in wts),
        "with_work": sum((not w["active"]) and bool(w["changed"]) for w in wts),
        "total": len(wts),
    }


class WorktreeError(Exception):
    pass


def remove(workdir: str, path: str, force: bool = False) -> str:
    """Remove one of `workdir`'s worktrees. Without `force` only a removable
    one (see list_worktrees); with it, any that nothing is live in -- its
    uncommitted files are lost. The branch is deleted only if it holds no
    commits of its own. Returns a short description of what was done."""
    wt = next((w for w in list_worktrees(workdir)
               if w["path"] == os.path.realpath(path)), None)
    if wt is None:
        raise WorktreeError("No such worktree for this instance.")
    if wt["active"]:
        raise WorktreeError("A session is still using this worktree.")
    if not force and not wt["removable"]:
        raise WorktreeError("This worktree has uncommitted work; confirm removal.")

    _git(workdir, "worktree", "unlock", wt["path"])  # no-op if not locked
    args = ["worktree", "remove", wt["path"]]
    if force:
        args.insert(2, "--force")
    r = _git(workdir, *args)
    if r.returncode != 0:
        raise WorktreeError(f"git worktree remove failed: {r.stderr.strip()}")

    kept = ""
    branch = wt["branch"]
    if branch and branch.startswith("worktree-"):
        if wt["ahead"] == 0:
            _git(workdir, "branch", "-D", branch)
        else:
            kept = f"; kept branch {branch} ({wt['ahead']} commit(s))"
    _git(workdir, "worktree", "prune")
    return f"removed {wt['name']}{kept}"


def sweep(workdir: str, sessions: dict | None = None) -> list[str]:
    """Remove every removable worktree of one clone. Best-effort."""
    done = []
    wts = list_worktrees(workdir, sessions)
    for wt in wts:
        if wt["removable"]:
            try:
                done.append(remove(workdir, wt["path"]))
            except (WorktreeError, OSError):
                pass
    if any(w["missing"] for w in wts):
        # Directory deleted by hand: drop git's record of it (branch stays).
        _git(workdir, "worktree", "prune")
    return done


def sweep_all() -> dict[str, list[str]]:
    sessions = live_sessions()
    out = {}
    for row in db.all_instances():
        if os.path.isdir(row["workdir"]):
            done = sweep(row["workdir"], sessions)
            if done:
                out[row["id"]] = done
    return out


def start_sweeper() -> None:
    """Run sweep_all every WORKTREE_SWEEP_MINUTES in a daemon thread (no-op
    when that is 0)."""
    minutes = config.WORKTREE_SWEEP_MINUTES
    if not minutes:
        return

    def loop():
        while True:
            time.sleep(minutes * 60)
            try:
                for iid, done in sweep_all().items():
                    print(f"worktree sweep {iid}: {', '.join(done)}", flush=True)
            except Exception as exc:  # never let the thread die
                print(f"worktree sweep failed: {exc}", flush=True)

    threading.Thread(target=loop, name="worktree-sweeper", daemon=True).start()


# --------------------------------------------------------------------------- #
# PR state (GitHub only, via the instance's gh config)
# --------------------------------------------------------------------------- #
_state_cache: dict[str, tuple[float, dict]] = {}
STATE_TTL = 300


def pr_states(iid: str, prs: list[dict]) -> None:
    """Annotate `prs` in place with state/title from `gh pr view`, using the
    instance's own gh config (so private repos work). Cached; silently skipped
    without gh or a GitHub credential."""
    gh_dir = os.path.join(config.SECRETS_ROOT, iid, "gh")
    if not os.path.isdir(gh_dir):
        return
    env = dict(os.environ, GH_CONFIG_DIR=gh_dir, GH_PROMPT_DISABLED="1")
    now = time.time()
    for pr in prs:
        hit = _state_cache.get(pr["url"])
        if hit and now - hit[0] < STATE_TTL:
            pr.update(hit[1])
            continue
        try:
            r = subprocess.run(
                ["gh", "pr", "view", pr["url"], "--json", "state,title,isDraft"],
                capture_output=True, text=True, timeout=15, env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return  # gh missing or hanging: don't try the rest
        if r.returncode != 0:
            continue
        try:
            info = json.loads(r.stdout)
        except ValueError:
            continue
        state = "DRAFT" if info.get("isDraft") and info.get("state") == "OPEN" else info.get("state")
        extra = {"state": state, "title": info.get("title")}
        _state_cache[pr["url"]] = (now, extra)
        pr.update(extra)
