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
- **Details & logs** — every card links to a detail page showing the instance's
  metadata and its session log. For a **running** instance this is a live
  `tmux capture-pane` snapshot — the actual rendered screen, refreshed every
  2s — rather than the raw piped byte stream, which would otherwise show the
  same redrawing status block duplicated over and over (remote-control repaints
  it every few seconds). For a **dead/orphaned** instance (no pane left to
  snapshot) it falls back to the historical captured log, which is exactly
  what says why it exited (e.g. not signed in, workspace trust not accepted).
- **Re-run** — the detail page of an orphaned instance has a *Re-run session*
  button that relaunches remote-control in the existing working tree (no
  re-clone; git auth and trust are already in place). Handy after fixing
  whatever made it exit.

## Prerequisites (host-level)

- `python3` (3.10+)
- `git`
- `tmux`
- `claude` CLI, already authenticated on the server (spawned instances inherit
  its credentials)

## Install & run

```sh
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

## Per-instance git credentials

Each instance can authenticate to its git host as its own identity. You curate a
pool of credentials in the UI and pick one when spawning an instance. Both
**GitHub** and **GitLab** are supported — and because the host comes from the
repo URL, GitLab.com and self-hosted GitLab work the same way.

**Adding one only needs a provider, label and token** — everything else is
derived or defaulted (tucked behind *Advanced*):

- **Provider** (GitHub/GitLab) is just a label to help you tell credentials
  apart; it's shown as a badge in the list and the spawn dropdown.
- **Host** is taken from each repo's URL at spawn time (it isn't encoded in the
  token), so you never type it — that's what makes self-hosted GitLab "just
  work". SSH URLs are auto-converted to HTTPS.
- **Username** defaults to `oauth2`, which works for GitHub PATs and GitLab
  personal/project/group tokens (both hosts authenticate by the token, not the
  username). Only a GitLab *deploy token* needs a specific username — set it
  under Advanced.
- **Commit identity** (`user.name`/`user.email`) is optional and also under
  Advanced; when set, the agent *commits* as that identity, not just pushes.

**Updating a credential's commit identity later** — each credential in the list
has an *Edit commit identity* disclosure. Saving it updates the stored identity
and also runs `git config user.name`/`user.email` in **every existing working
tree** that was cloned with that credential (not just future ones); leaving a
field blank clears it there too. Working trees that were already cleaned up are
skipped.

Under the hood:

- **Storage** — the token is encrypted at rest with `FLEET_SECRET_KEY` (Fernet);
  the key never touches the database. Without the key set, credential features
  are disabled but the rest of the app runs.
- **Injection** — on spawn, the token is written to a `git credential-store` file
  **outside the working tree** (mode 0600), and that clone's *local* git config
  is pointed at it (inherited global helpers are reset first, so instances stay
  isolated). The token never appears on a command line, in `.git/config`, or in
  the relay log. Any `git` command Claude runs authenticates automatically.
- **Cleanup** — removing an instance's working tree also deletes its secret file.

The UI lives in `templates/_credential_list.html` (the pool) and
`templates/_credential_form.html` (the add form).

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
| `RELAY_REGEX`      | `https://claude\.ai/code/session_\S+` | Pattern used to extract the relay URL from logs |

## How remote-control spawning works

Verified against `claude` v2.1.92. On spawn, the app handles everything
`claude remote-control` needs so it doesn't stall on an interactive prompt:

1. **Workspace trust** — a freshly cloned dir isn't trusted, and remote-control
   exits with *"Workspace not trusted"*. The app pre-accepts it by setting
   `hasTrustDialogAccepted` for the clone's **resolved** path in Claude's
   project config (`~/.claude.json`, or `$CLAUDE_CONFIG_DIR/.claude.json`),
   preserving the rest of the file and its `0600` perms.
2. **Spawn mode** — the command is launched as
   `claude remote-control --name <label> --spawn same-dir`, which labels the
   session in claude.ai/code and skips the interactive spawn-mode `[1/2]`
   prompt (each instance is already its own clone).
3. **Enable prompt** — if the *"Enable Remote Control? (y/n)"* prompt appears, a
   background watcher answers `y` once (it only fires when it sees that exact
   prompt, so it's a no-op otherwise).
4. **Relay URL** — remote-control prints `https://claude.ai/code/session_<id>`
   inside a terminal hyperlink escape; the scraper reads `<workdir>/.relay.log`,
   applies `RELAY_REGEX`, and trims trailing control bytes.

Requires a signed-in subscription account (see **Claude account** above). To
debug a session by hand:

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
