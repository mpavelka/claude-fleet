"""Scheduled runs: cron-style commands executed in an instance's working tree.

One scheduler per application, schedules per instance. A single daemon thread
(started from app.py, like the worktree sweeper) wakes every TICK seconds,
fires each enabled schedule whose `next_run_at` has passed, and reaps runs
that finished. Schedules live in SQLite next to the instances they belong to,
so they share the instance's lifecycle (cleanup deletes them) and need no
system crontab, cron daemon or root inside the container.

Each run is its own detached tmux session (`fleet-run-<schedule id>`) started
in the instance's clone with the same per-instance environment its
remote-control session gets (gh config, Docker access), so a run can be
attached to while it's going and survives a restart of the dashboard process
on a non-container host. Output goes to `<clone>/.git/fleet-runs/<id>/`: inside
the clone (so it goes away with it and the Files browser can reach it) but
under .git, so it never shows up in `git status`.

Policy:
- Overlap: a schedule whose previous run is still going is skipped, not queued.
- Missed runs (dashboard down at the due time) fire once when it comes back;
  several missed runs collapse into one -- like systemd's Persistent=true.
- Times are in FLEET_TIMEZONE (default: the server's local zone).
"""
import os
import shlex
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

from croniter import croniter

import config
import db
import manager

TICK = 30  # seconds between scheduler passes
KEEP_LOGS = 20  # run logs kept per schedule
_SESSION_PREFIX = "fleet-run-"
_lock = threading.Lock()


class ScheduleError(Exception):
    """User-facing problem with a schedule (bad cron, missing instance...)."""


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(iso: str | None) -> datetime | None:
    return datetime.fromisoformat(iso) if iso else None


def validate_cron(expr: str) -> str:
    """Normalize and check a 5-field cron expression (or @hourly/@daily/...).
    Seconds/year fields that croniter would also accept are refused so the
    expression means what it would in a crontab."""
    expr = " ".join((expr or "").split())
    if not expr:
        raise ScheduleError("A cron expression is required, e.g. 40 7 * * *")
    if not expr.startswith("@") and len(expr.split()) != 5:
        raise ScheduleError(
            "Use 5 fields: minute hour day-of-month month day-of-week (e.g. 40 7 * * 1-5)."
        )
    if expr == "@reboot":
        raise ScheduleError("@reboot is not supported.")
    if not croniter.is_valid(expr):
        raise ScheduleError(f"Not a valid cron expression: {expr}")
    return expr


def next_run(expr: str, after: datetime | None = None) -> datetime:
    """Next time `expr` fires strictly after `after` (default now), evaluated
    in the configured time zone (DST-aware), returned in UTC."""
    base = (after or _now()).astimezone(config.TIMEZONE)
    return croniter(expr, base).get_next(datetime).astimezone(timezone.utc)


def localtime(iso: str | None) -> str:
    """Jinja filter: an ISO UTC timestamp shown in the configured zone."""
    dt = _parse(iso)
    if dt is None:
        return ""
    return dt.astimezone(config.TIMEZONE).strftime("%Y-%m-%d %H:%M %Z")


def timezone_name() -> str:
    return str(config.TIMEZONE)


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def create(iid: str, cron: str, command: str, label: str = "") -> str:
    row = db.get(iid)
    if row is None:
        raise ScheduleError("No such instance.")
    cron = validate_cron(cron)
    command = (command or "").strip()
    if not command:
        raise ScheduleError("A command is required, e.g. bin/scheduled-run job-scan")
    sid = uuid.uuid4().hex[:12]
    db.add_schedule(sid, iid, label.strip() or command, cron, command,
                    _iso(next_run(cron)))
    return sid


def set_enabled(sid: str, enabled: bool) -> None:
    s = _get_or_raise(sid)
    # Re-enabling starts from now: no catch-up run for the time it was off.
    db.update_schedule(sid, enabled=1 if enabled else 0,
                       next_run_at=_iso(next_run(s["cron"])))


def delete(sid: str) -> None:
    manager._kill_tmux(session_name(sid))
    db.delete_schedule(sid)


def forget_instance(iid: str) -> None:
    """Stop and delete every schedule of an instance being cleaned up."""
    for s in db.schedules_for(iid):
        delete(s["id"])


def run_now(sid: str) -> None:
    s = _get_or_raise(sid)
    with _lock:
        _reap(s)
        if _running(sid):
            raise ScheduleError("A run of this schedule is already in progress.")
        _launch(db.get_schedule(sid))


def _get_or_raise(sid: str):
    s = db.get_schedule(sid)
    if s is None:
        raise ScheduleError("No such schedule.")
    return s


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
def session_name(sid: str) -> str:
    return f"{_SESSION_PREFIX}{sid}"


def _running(sid: str) -> bool:
    return manager._tmux_alive(session_name(sid))


def _log_dir(workdir: str, sid: str) -> str:
    return os.path.join(workdir, ".git", "fleet-runs", sid)


def _prune_logs(log_dir: str) -> None:
    try:
        logs = sorted(f for f in os.listdir(log_dir) if f.endswith(".log"))
    except OSError:
        return
    for name in logs[:-KEEP_LOGS]:
        stem = os.path.join(log_dir, name[:-4])
        for ext in (".log", ".exit"):
            try:
                os.remove(stem + ext)
            except OSError:
                pass


def _launch(s) -> None:
    """Start one run of schedule `s` in its instance's clone. Records the
    outcome on the schedule row; never raises for a run that can't start."""
    sid = s["id"]
    inst = db.get(s["instance_id"])
    started = _now()
    if inst is None or not os.path.isdir(os.path.join(inst["workdir"], ".git")):
        db.update_schedule(sid, last_run_at=_iso(started), last_exit=None,
                           last_log=None, last_note="skipped: working tree is missing")
        return
    workdir = inst["workdir"]
    log_dir = _log_dir(workdir, sid)
    os.makedirs(log_dir, exist_ok=True)
    stem = os.path.join(log_dir, started.strftime("%Y%m%d-%H%M%S"))
    log, exit_file = stem + ".log", stem + ".exit"
    with open(log, "w") as fh:
        fh.write(f"# {s['label']} -- started {localtime(_iso(started))}\n$ {s['command']}\n\n")

    # tmux hands this string to the default shell; the user's command is a
    # crontab-style shell line, quoted as one argument to its own `sh -c`.
    shell = (f"sh -c {shlex.quote(s['command'])} >> {shlex.quote(log)} 2>&1; "
             f"echo $? > {shlex.quote(exit_file)}")
    launch = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name(sid), "-x", "220", "-y", "50",
         *manager._docker_env_flags(),
         *manager._gh_env_flags(manager._gh_config_dir_if_exists(inst["id"])),
         "-e", f"FLEET_SCHEDULE_ID={sid}",
         "-c", workdir, shell],
        capture_output=True, text=True,
    )
    if launch.returncode != 0:
        db.update_schedule(sid, last_run_at=_iso(started), last_exit=None, last_log=log,
                           last_note=f"failed to start: {launch.stderr.strip()}")
        return
    db.update_schedule(sid, last_run_at=_iso(started), last_exit=None, last_log=log,
                       last_note=None)
    _prune_logs(log_dir)


def _reap(s) -> None:
    """Record the exit code of a finished run (once)."""
    if not s["last_log"] or s["last_exit"] is not None or s["last_note"]:
        return
    if _running(s["id"]):
        return
    try:
        with open(s["last_log"][:-4] + ".exit") as fh:
            code = int(fh.read().strip())
        note = None
    except (OSError, ValueError):
        code, note = None, "ended without an exit code (killed?)"
    db.update_schedule(s["id"], last_exit=code, last_note=note)


def tick(now: datetime | None = None) -> None:
    """One scheduler pass: reap finished runs, fire due schedules."""
    now = now or _now()
    with _lock:
        for s in db.all_schedules():
            _reap(s)
            due = _parse(s["next_run_at"])
            if not s["enabled"] or due is None or due > now:
                continue
            # Advance first so a failing launch can't refire every tick.
            db.update_schedule(s["id"], next_run_at=_iso(next_run(s["cron"], now)))
            if _running(s["id"]):
                print(f"schedule {s['id']}: previous run still in progress, skipped", flush=True)
                continue
            _launch(db.get_schedule(s["id"]))


def start() -> None:
    """Run tick() every TICK seconds in a daemon thread."""
    def loop():
        while True:
            try:
                tick()
            except Exception as exc:  # never let the thread die
                print(f"scheduler tick failed: {exc}", flush=True)
            time.sleep(TICK)

    threading.Thread(target=loop, name="scheduler", daemon=True).start()


# --------------------------------------------------------------------------- #
# Read model
# --------------------------------------------------------------------------- #
def list_for(iid: str) -> list[dict]:
    out = []
    with _lock:
        for s in db.schedules_for(iid):
            _reap(s)
    for s in db.schedules_for(iid):
        item = dict(s)
        item["running"] = _running(s["id"])
        item["session"] = session_name(s["id"])
        out.append(item)
    return out


def summary(iid: str) -> dict | None:
    """Card summary: how many schedules, the soonest next run, last failure."""
    rows = [s for s in db.schedules_for(iid)]
    if not rows:
        return None
    enabled = [s for s in rows if s["enabled"]]
    nxt = min((s["next_run_at"] for s in enabled if s["next_run_at"]), default=None)
    failed = any(s["last_exit"] not in (None, 0) for s in rows)
    return {"total": len(rows), "enabled": len(enabled), "next_run_at": nxt, "failed": failed}


def last_log(sid: str, max_bytes: int = 200_000) -> str:
    s = _get_or_raise(sid)
    if not s["last_log"]:
        return ""
    try:
        with open(s["last_log"], "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return "(log file is gone)"
    return manager._ANSI_RE.sub("", data.decode("utf-8", errors="replace"))
