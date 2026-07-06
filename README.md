# Claude Fleet

A small dashboard to clone git repos and run many `claude remote-control`
instances on a server. One repo → many isolated instances, each in its own
cloned working tree and its own detached tmux session, with the remote-control
link and QR code surfaced in the UI.

## What it does

- **Clone & spawn** — each instance gets a fresh `git clone` under `FLEET_ROOT`
  and a detached tmux session running `claude remote-control`.
- **Manage** — live green/amber/red status, kill a running instance, and see
  its relay link + QR code.
- **Orphans** — killing an instance keeps its working tree so you can inspect
  Claude's changes. The dashboard lists these orphans (including untracked
  directories left over from crashes) and offers a one-click cleanup.

## Prerequisites (host-level)

- `python3` (3.10+)
- `git`
- `tmux`
- `claude` CLI, already authenticated on the server (spawned instances inherit
  its credentials)

## Install & run

```sh
cd claude-fleet
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Needed only if you want to store GitLab credentials (see below):
export FLEET_SECRET_KEY="$(python crypto.py)"   # persist this somewhere safe

python app.py            # serves on 127.0.0.1:8700 by default
```

Open http://127.0.0.1:8700 (locally) or put it behind the reverse proxy below.

## Exposing it safely

**This dashboard spawns processes with your Claude credentials — never put the
raw port on a public interface.** The app binds to `127.0.0.1` and expects a
reverse proxy to terminate TLS and authenticate. A reference `Caddyfile` is
included (automatic HTTPS + basic-auth):

```sh
caddy hash-password --plaintext 'yourpassword'   # paste hash into Caddyfile
caddy run --config Caddyfile
```

As defense-in-depth, set `FLEET_AUTH_TOKEN` and have the proxy inject the same
value in the `X-Auth-Token` header (the Caddyfile shows this). Then even a
misconfigured firewall exposing port 8700 won't hand out shells.

## Per-instance GitLab credentials

Each instance can authenticate to GitLab as its own identity. You curate a pool
of credentials in the UI and pick one when spawning an instance.

- **Storage** — each credential is a GitLab access token (Personal, Project, or
  Group). Tokens are encrypted at rest with `FLEET_SECRET_KEY` (Fernet); the key
  never touches the database. Without the key set, credential features are
  disabled but the rest of the app runs.
- **Injection** — on spawn, the token is written to a `git credential-store` file
  **outside the working tree** (mode 0600), and that clone's *local* git config
  is pointed at it (inherited global helpers are reset first, so instances stay
  isolated). The token never appears on a command line, in `.git/config`, or in
  the relay log. Any `git` command Claude runs authenticates automatically.
- **Commit identity** — a credential's optional name/email are set as the clone's
  local `user.name`/`user.email`, so the agent *commits* as its own identity too.
- **Cleanup** — removing an instance's working tree also deletes its secret file.

The token username depends on the token type (your username for a PAT, the token
name for Project/Group tokens). Give the repo URL over HTTPS (SSH URLs are
auto-converted to HTTPS when a credential is attached).

## Configuration (environment variables)

| Variable           | Default                          | Purpose                                        |
|--------------------|----------------------------------|------------------------------------------------|
| `FLEET_ROOT`       | `~/.claude-fleet/instances`      | Where per-instance clones live                 |
| `FLEET_DB`         | `~/.claude-fleet/fleet.db`       | SQLite state file                              |
| `FLEET_SECRETS`    | `~/.claude-fleet/secrets`        | Per-instance git credential files (0600)       |
| `FLEET_SECRET_KEY` | _(unset)_                        | Fernet key encrypting credentials; `python crypto.py` mints one |
| `FLEET_HOST`       | `127.0.0.1`                      | Bind address (keep loopback in production)     |
| `FLEET_PORT`       | `8700`                           | Bind port                                      |
| `FLEET_AUTH_TOKEN` | _(unset)_                        | If set, required in `X-Auth-Token` header      |
| `CLAUDE_RC_CMD`    | `claude remote-control`          | Command launched inside each working tree      |
| `RELAY_REGEX`      | `https?://\S+`                   | Pattern used to extract the relay URL from logs|

## The one assumption to verify

The relay URL is captured by mirroring the tmux pane to `<workdir>/.relay.log`
(via `tmux pipe-pane`) and scanning it with `RELAY_REGEX`. If your `claude`
version prints the remote-control URL differently, adjust `CLAUDE_RC_CMD` and/or
`RELAY_REGEX` — no code change needed. To debug a session by hand:

```sh
tmux attach -t claude-<instance-id>
```

## How state is modeled

The UI reconciles three sources on every refresh:

- **SQLite** — the mapping of instance id → repo, working tree, tmux session, relay URL.
- **tmux** — `tmux has-session` decides running vs stopped.
- **Filesystem** — directories under `FLEET_ROOT` with no DB row show up as
  untracked orphans.

So `running` = live tmux session, `orphan` = working tree on disk without a live
session, `missing` = tracked but the working tree is gone.
