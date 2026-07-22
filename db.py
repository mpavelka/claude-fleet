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


def add_instance(iid, name, repo_url, workdir, tmux_session, credential_id=None) -> None:
    with _connect() as c:
        c.execute(
            "INSERT INTO instances "
            "(id, name, repo_url, workdir, tmux_session, credential_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (iid, name, repo_url, workdir, tmux_session, credential_id, _now()),
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


def instances_by_credential(cid) -> list[sqlite3.Row]:
    with _connect() as c:
        return c.execute(
            "SELECT * FROM instances WHERE credential_id = ?", (cid,)
        ).fetchall()


def delete_credential(cid) -> None:
    with _connect() as c:
        c.execute("DELETE FROM credentials WHERE id = ?", (cid,))
