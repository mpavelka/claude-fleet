"""Thin SQLite layer. State that isn't derivable from tmux/filesystem lives here:
the mapping of instance id -> repo, working tree, tmux session, relay URL.
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    with _connect() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS instances (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                repo_url      TEXT NOT NULL,
                workdir       TEXT NOT NULL,
                tmux_session  TEXT NOT NULL,
                relay_url     TEXT,
                created_at    TEXT NOT NULL,
                stopped_at    TEXT
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS credentials (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                host        TEXT NOT NULL,
                username    TEXT NOT NULL,
                secret_enc  TEXT NOT NULL,
                git_name    TEXT,
                git_email   TEXT,
                created_at  TEXT NOT NULL
            )
            """
        )
        # Migration: record which credential an instance was spawned with.
        cols = [r[1] for r in c.execute("PRAGMA table_info(instances)")]
        if "credential_id" not in cols:
            c.execute("ALTER TABLE instances ADD COLUMN credential_id TEXT")
        # Migration: which git provider a credential targets (github|gitlab).
        cred_cols = [r[1] for r in c.execute("PRAGMA table_info(credentials)")]
        if "provider" not in cred_cols:
            c.execute("ALTER TABLE credentials ADD COLUMN provider TEXT")
        # Migration: per-instance remote-control spawn mode. NULL (every row
        # from before this column) means "follow CLAUDE_RC_SPAWN_MODE".
        if "spawn_mode" not in cols:
            c.execute("ALTER TABLE instances ADD COLUMN spawn_mode TEXT")
        # Scheduled runs (see scheduler.py). Times are ISO UTC. last_exit is
        # NULL while a run is going (or when it never started -- last_note
        # then says why).
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS schedules (
                id           TEXT PRIMARY KEY,
                instance_id  TEXT NOT NULL,
                label        TEXT NOT NULL,
                cron         TEXT NOT NULL,
                command      TEXT NOT NULL,
                enabled      INTEGER NOT NULL DEFAULT 1,
                next_run_at  TEXT,
                last_run_at  TEXT,
                last_log     TEXT,
                last_exit    INTEGER,
                last_note    TEXT,
                created_at   TEXT NOT NULL
            )
            """
        )


def add_instance(iid, name, repo_url, workdir, tmux_session, credential_id=None,
                 spawn_mode=None) -> None:
    with _connect() as c:
        c.execute(
            "INSERT INTO instances "
            "(id, name, repo_url, workdir, tmux_session, credential_id, spawn_mode, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (iid, name, repo_url, workdir, tmux_session, credential_id, spawn_mode, _now()),
        )


def all_instances() -> list[sqlite3.Row]:
    with _connect() as c:
        return c.execute("SELECT * FROM instances ORDER BY created_at DESC").fetchall()


def get(iid) -> sqlite3.Row | None:
    with _connect() as c:
        return c.execute("SELECT * FROM instances WHERE id = ?", (iid,)).fetchone()


def get_by_workdir(workdir) -> sqlite3.Row | None:
    with _connect() as c:
        return c.execute(
            "SELECT * FROM instances WHERE workdir = ?", (workdir,)
        ).fetchone()


def set_relay(iid, url) -> None:
    with _connect() as c:
        c.execute("UPDATE instances SET relay_url = ? WHERE id = ?", (url, iid))


def mark_stopped(iid) -> None:
    with _connect() as c:
        c.execute(
            "UPDATE instances SET stopped_at = ? WHERE id = ? AND stopped_at IS NULL",
            (_now(), iid),
        )


def reactivate(iid) -> None:
    """Clear stopped/relay state so a re-run instance reads as running and its
    relay URL is re-scraped fresh."""
    with _connect() as c:
        c.execute(
            "UPDATE instances SET stopped_at = NULL, relay_url = NULL WHERE id = ?",
            (iid,),
        )


def delete(iid) -> None:
    with _connect() as c:
        c.execute("DELETE FROM instances WHERE id = ?", (iid,))


# --------------------------------------------------------------------------- #
# Credentials (secrets stored encrypted; see crypto.py)
# --------------------------------------------------------------------------- #
def add_credential(cid, name, provider, host, username, secret_enc, git_name, git_email) -> None:
    with _connect() as c:
        c.execute(
            "INSERT INTO credentials "
            "(id, name, provider, host, username, secret_enc, git_name, git_email, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, name, provider, host, username, secret_enc, git_name, git_email, _now()),
        )


def all_credentials() -> list[sqlite3.Row]:
    with _connect() as c:
        return c.execute("SELECT * FROM credentials ORDER BY name").fetchall()


def get_credential(cid) -> sqlite3.Row | None:
    with _connect() as c:
        return c.execute("SELECT * FROM credentials WHERE id = ?", (cid,)).fetchone()


def update_credential_identity(cid, git_name, git_email) -> None:
    with _connect() as c:
        c.execute(
            "UPDATE credentials SET git_name = ?, git_email = ? WHERE id = ?",
            (git_name, git_email, cid),
        )


def update_credential_token(cid, secret_enc) -> None:
    with _connect() as c:
        c.execute(
            "UPDATE credentials SET secret_enc = ? WHERE id = ?",
            (secret_enc, cid),
        )


def instances_by_credential(cid) -> list[sqlite3.Row]:
    with _connect() as c:
        return c.execute(
            "SELECT * FROM instances WHERE credential_id = ?", (cid,)
        ).fetchall()


def delete_credential(cid) -> None:
    with _connect() as c:
        c.execute("DELETE FROM credentials WHERE id = ?", (cid,))


def set_instance_credential(iid, credential_id) -> None:
    with _connect() as c:
        c.execute(
            "UPDATE instances SET credential_id = ? WHERE id = ?",
            (credential_id, iid),
        )


# --------------------------------------------------------------------------- #
# Schedules (see scheduler.py)
# --------------------------------------------------------------------------- #
_SCHEDULE_FIELDS = {"enabled", "next_run_at", "last_run_at", "last_log", "last_exit", "last_note"}


def add_schedule(sid, instance_id, label, cron, command, next_run_at) -> None:
    with _connect() as c:
        c.execute(
            "INSERT INTO schedules "
            "(id, instance_id, label, cron, command, enabled, next_run_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (sid, instance_id, label, cron, command, next_run_at, _now()),
        )


def all_schedules() -> list[sqlite3.Row]:
    with _connect() as c:
        return c.execute("SELECT * FROM schedules ORDER BY created_at").fetchall()


def schedules_for(instance_id) -> list[sqlite3.Row]:
    with _connect() as c:
        return c.execute(
            "SELECT * FROM schedules WHERE instance_id = ? ORDER BY created_at",
            (instance_id,),
        ).fetchall()


def get_schedule(sid) -> sqlite3.Row | None:
    with _connect() as c:
        return c.execute("SELECT * FROM schedules WHERE id = ?", (sid,)).fetchone()


def update_schedule(sid, **fields) -> None:
    unknown = set(fields) - _SCHEDULE_FIELDS
    if unknown:
        raise ValueError(f"unknown schedule fields: {unknown}")
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _connect() as c:
        c.execute(f"UPDATE schedules SET {cols} WHERE id = ?", (*fields.values(), sid))


def delete_schedule(sid) -> None:
    with _connect() as c:
        c.execute("DELETE FROM schedules WHERE id = ?", (sid,))
