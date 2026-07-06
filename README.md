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

python app.py                      # reads ~/.claude-fleet/.env if present
python app.py -c /etc/claude-fleet.env   # or point at a specific config file
```

By default the server listens on `127.0.0.1:8700`. Open http://127.0.0.1:8700
locally, or put it behind the reverse proxy below.

All settings can come from a **config file**, real **environment variables**, or
built-in defaults — see [Configuration](#configuration). The quickest start is
to copy the sample and edit it:

```sh
mkdir -p ~/.claude-fleet
cp .env.example ~/.claude-fleet/.env
# generate the credential-encryption key and add it to the file:
echo "FLEET_SECRET_KEY=$(python crypto.py)" >> ~/.claude-fleet/.env
```

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

## Claude account (sign-in)

`claude remote-control` requires the server to be signed in to a Claude account
with a subscription. The **Claude account** section lets you do this from the
browser — no SSH or port forwarding needed, even on a headless server:

1. Click **Sign in to Claude**. The backend runs `claude auth login` in a tmux
   session and scrapes the authorization URL.
2. Open the shown link (or scan the QR) in your browser and approve access.
   The OAuth flow redirects to a hosted Claude page that displays an
   authorization **code** (it does *not* rely on a localhost callback).
3. Paste that code back into the dashboard. The backend delivers it to the
   login session and polls until you're signed in, then shows the account.

This is a single server-wide login (the default config dir); every instance
shares it. Credentials land in `~/.claude/.credentials.json` (Linux) or the
Keychain (macOS) — the dashboard never stores them itself.

## Environment status

The header has an **Environment** indicator (a colored dot you can expand) that
probes the tools and services the app depends on: `tmux`, `git`, `claude`
(required), and `docker` plus credential encryption (optional). It shows each
one's version and flags anything missing or degraded (e.g. docker installed but
the daemon isn't running). It refreshes every 15s while expanded.

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

## Configuration

Configuration is handled by [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/).
Every value can come from three places, in **descending precedence**:

1. **Real environment variables** — always win.
2. **A config file** in `.env` format (`KEY=value` lines). Path resolution:
   the `--config`/`-c` flag → the `FLEET_CONFIG` env var → the default
   `~/.claude-fleet/.env`. A missing file is fine (defaults are used).
3. **Built-in defaults** (the table below).

```sh
python app.py -c /etc/claude-fleet.env
```

See [`.env.example`](.env.example) for a template. The variable names are the
same whether set in the environment or the file:

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

## Remote-control spawn — known gaps

The relay URL is captured by mirroring the tmux pane to `<workdir>/.relay.log`
(via `tmux pipe-pane`) and scanning it with `RELAY_REGEX`. Verified against
`claude` v2.1.92, the current `claude remote-control` behaviour is:

- It shows an interactive **`Enable Remote Control? (y/n)`** prompt on start —
  the spawn code does **not yet** answer it, so a real remote-control instance
  will wait at that prompt. Answering needs `tmux send-keys -t <session> y Enter`.
- After confirming, it prints `https://claude.ai/code/session_<id>` (so a
  tighter `RELAY_REGEX` is `https://claude\.ai/code/session_\S+`).
- It requires a signed-in subscription account (see **Claude account** above)
  and the workspace-trust dialog accepted for the directory — fresh clones
  aren't trusted yet.

Wiring these three up is the next task. To debug a session by hand:

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
