# Claude Fleet

A small dashboard to clone git repos and run many `claude remote-control`
instances on a server. One repo → many isolated instances, each in its own
cloned working tree and its own detached tmux session. Each session is named
after its instance, so you pick it up from the session list at claude.ai/code
or in the Claude app.

## What it does

- **Clone & spawn** — each instance gets a fresh `git clone` under `FLEET_ROOT`
  and a detached tmux session running `claude remote-control`.
- **Manage** — live green/amber/red status, kill a running instance, and see
  whether its remote-control session has connected (the card shows
  *Connected*, linking to the session, once the relay URL appears in its log).
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
- **Files** — a read-only file browser (sidebar → *Files*) over `/tmp` and
  the instance clones (`FLEET_BROWSE_ROOTS`), for grabbing whatever an instance
  left behind: open a file in the browser, download it, or download a whole
  folder as a `.zip`. Sort by name or newest first. Paths that resolve outside
  those folders (`..`, symlinks elsewhere) and the secrets dir/DB are refused.
  Viewed files are served under a `Content-Security-Policy: sandbox`, so an
  HTML artifact renders with its relative CSS/JS/images and its scripts run,
  but in an isolated origin that can't act on the dashboard.
- **Sessions & worktrees** — in worktree mode the detail page lists every
  session worktree of the instance: whether a session is attached (busy/idle,
  with a link to it in claude.ai/code), its branch, uncommitted files and
  commits. Cards summarize active sessions and ones that ended with
  uncommitted work. See [Session worktrees](#session-worktrees).
- **Pull requests** — the detail page lists every PR the instance's sessions
  opened or worked on (read from their transcripts, so it covers removed
  worktrees and same-dir instances too), with open/merged/closed state for
  instances with a GitHub credential.
- **Spawn mode per instance** — the spawn form can pin an instance to
  `worktree`, `same-dir` or `session` mode; left at default it follows
  `CLAUDE_RC_SPAWN_MODE`.
- **Sandboxed Docker access (optional)** — set `DOCKER_HOST` (see
  [Configuration](#configuration)) and every spawned session's `docker` CLI is
  transparently pointed at it; unset (the default), spawned sessions have no
  Docker access at all. See [docs/deployment-k3s.md](docs/deployment-k3s.md)
  for the recommended way to back this with an isolated, gVisor-sandboxed
  daemon rather than a host/cluster Docker socket.

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

## Running as a container

A multi-arch image (amd64 + arm64, built from the [`Dockerfile`](Dockerfile))
is published to `ghcr.io/mpavelka/claude-fleet` by
[.github/workflows/docker-publish.yml](.github/workflows/docker-publish.yml)
on every push to `main` and on version tags — see
[docs/publishing.md](docs/publishing.md) for exactly which tags each trigger
produces and how to cut a release.

```sh
docker run -d --name claude-fleet -p 8700:8700 \
  -v claude-fleet-data:/data \
  -e FLEET_SECRET_KEY=... \
  ghcr.io/mpavelka/claude-fleet:latest
```

All state (SQLite DB, per-instance clones, per-instance git credential files)
lives under `/data` in the container — mount a volume there. For a production
Kubernetes (K3s) deployment, including how to safely let spawned agent
sessions run Docker commands of their own without any path to host root, see
[docs/deployment-k3s.md](docs/deployment-k3s.md).

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
(required), and `docker`, `gh` plus credential encryption (optional). It shows
each one's version and flags anything missing or degraded (e.g. docker
installed but the daemon isn't running). It refreshes every 15s while
expanded.

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
- **`gh` CLI** — if the credential's provider is **GitHub**, the same token is
  also written to a per-instance `gh` config (`hosts.yml`, mode 0600, next to
  the git credential file) and that session's `GH_CONFIG_DIR` points at it. So
  `gh pr create`, `gh issue view`, etc. inside that instance authenticate the
  same way `git` does — no separate `gh auth login` needed. GitLab credentials
  don't get this (`gh` is GitHub-only); the `gh` CLI itself ships in the Docker
  image.
- **Cleanup** — removing an instance's working tree also deletes its secret file
  (and `gh` config, if any).

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
| `FLEET_SECRETS`    | `~/.claude-fleet/secrets`        | Per-instance git credential (and `gh` config) files (0600) |
| `FLEET_SECRET_KEY` | _(unset)_                        | Fernet key encrypting credentials; `python crypto.py` mints one |
| `FLEET_HOST`       | `127.0.0.1`                      | Bind address (keep loopback in production)     |
| `FLEET_PORT`       | `8700`                           | Bind port                                      |
| `FLEET_AUTH_TOKEN` | _(unset)_                        | If set, required in `X-Auth-Token` header      |
| `CLAUDE_RC_CMD`    | `claude remote-control`          | Command launched inside each working tree      |
| `CLAUDE_RC_SPAWN_MODE` | `worktree`                   | Default spawn mode for instances spawned without one: `worktree` (each session gets its own git worktree), `same-dir` (all sessions share the clone) or `session` (single session); an explicit `--spawn` in `CLAUDE_RC_CMD` wins |
| `CLAUDE_RC_CAPACITY` | _(unset → 32)_                 | Max concurrent sessions per instance (`--capacity`) |
| `FLEET_WORKTREE_SWEEP_MINUTES` | `15`               | How often worktrees of ended sessions with a clean `git status` are removed; `0` disables the periodic sweep (re-run still sweeps) |
| `RELAY_REGEX`      | `https://claude\.ai/code(?:/session_\|\?environment=)\S+` | Pattern used to extract the relay URL from logs |
| `DOCKER_HOST`      | _(unset)_                        | If set, spawned sessions' `docker` CLI points here instead of having no Docker access — see [docs/deployment-k3s.md](docs/deployment-k3s.md) |
| `DOCKER_TLS_VERIFY`| _(unset)_                        | Passed through to spawned sessions alongside `DOCKER_HOST`     |
| `DOCKER_CERT_PATH` | _(unset)_                        | Passed through to spawned sessions alongside `DOCKER_HOST`     |
| `FLEET_BROWSE_ROOTS` | `/tmp`, `FLEET_ROOT`          | Comma-separated folders the *Files* browser can read; set to empty to disable |
| `FLEET_ENVIRONMENT_BRIEFING` | _(unset)_               | Markdown briefed into every spawned instance — see **Environment briefing** below |

## Environment briefing

Every spawned instance starts with zero awareness of the environment it's
actually running in — e.g. on a K3s deployment whose `docker-sandbox` runs
with `--iptables=false`, an agent has to discover through trial and error
that it needs `docker run --network host` for outbound connectivity. Set
`FLEET_ENVIRONMENT_BRIEFING` to a block of Markdown and it's written into
Claude's global memory file (`$CLAUDE_CONFIG_DIR/CLAUDE.md`, default
`~/.claude/CLAUDE.md`) every time an instance is spawned or re-run — no
manual filesystem edits, and every instance reads it automatically at
startup since it's Claude's own memory mechanism, not something Fleet
invented.

The write is additive: it's wrapped in `<!-- claude-fleet:environment-briefing:...
-->` marker comments, so any other content already in `CLAUDE.md` is left
alone, and changing (or unsetting) the setting updates (or removes) just that
section on the next spawn/rerun rather than duplicating it.

```sh
# In ~/.claude-fleet/.env (or wherever --config points):
FLEET_ENVIRONMENT_BRIEFING="This cluster's docker-sandbox runs with --iptables=false.
Use \`docker run --network host ...\` for outbound connectivity; a bridge
network will silently have no internet access.
Container registry mirror: registry.internal:5000"
```

Keep deployment-wide facts like this here, separate from a repo's own
`CLAUDE.md` (which stays about that codebase, not the infrastructure it
happens to run on this week).

## How remote-control spawning works

Verified against `claude` v2.1.92. On spawn, the app handles everything
`claude remote-control` needs so it doesn't stall on an interactive prompt:

1. **Workspace trust** — a freshly cloned dir isn't trusted, and remote-control
   exits with *"Workspace not trusted"*. The app pre-accepts it by setting
   `hasTrustDialogAccepted` for the clone's **resolved** path in Claude's
   project config (`~/.claude.json`, or `$CLAUDE_CONFIG_DIR/.claude.json`),
   preserving the rest of the file and its `0600` perms.
2. **Spawn mode** — the command is launched as
   `claude remote-control --name <label> --remote-control-session-name-prefix <label> --spawn worktree --no-create-session-in-dir`
   (mode from the instance, else `CLAUDE_RC_SPAWN_MODE`), which labels the
   instance and the sessions it spawns in
   claude.ai/code and skips the interactive spawn-mode `[1/2]` prompt. In
   worktree mode every session you start on the instance (from the mobile app
   or claude.ai/code) gets its own git worktree under
   `<clone>/.claude/worktrees/<name>` on a `worktree-<name>` branch, so
   concurrent sessions never share a working tree; no session is pre-created
   in the clone itself. Worktrees share the clone's git config, so the
   instance's credential and commit identity apply in them too, and
   `.claude/worktrees/` is added to the clone's `.git/info/exclude` so it never
   shows up as untracked. `same-dir` restores the old behaviour (every session
   in the clone).
3. **Enable prompt** — if the *"Enable Remote Control? (y/n)"* prompt appears, a
   background watcher answers `y` once (it only fires when it sees that exact
   prompt, so it's a no-op otherwise).
4. **Relay URL** — remote-control prints `https://claude.ai/code/session_<id>`
   for a pre-created session, or only the environment link
   `https://claude.ai/code?environment=env_<id>` in worktree mode (the session
   link is preferred when both appear), inside a terminal hyperlink escape;
   the scraper reads `<workdir>/.relay.log`, applies `RELAY_REGEX`, and trims
   trailing control bytes. Finding it is what
   flips the card from *Connecting…* to *Connected*; the full URL is shown on
   the instance's detail page.

Requires a signed-in subscription account (see **Claude account** above). To
debug a session by hand:

```sh
tmux attach -t claude-<instance-id>
```

## Session worktrees

In worktree mode remote-control names each session's worktree
`bridge-cse_<id>` after the claude.ai session `session_<id>`, and locks it
(`claude agent … (pid P start S)`) while the session is attached. Archiving a
session releases the lock, and remote-control deletes the worktree itself if
its `git status` is clean. A worktree is left behind when the session ended
with uncommitted files, or was still open when the server stopped (Kill, pod
restart).

The fleet tells used from unused worktrees from what Claude Code leaves on
disk: the `CLAUDE_BASE` marker Claude writes into every worktree it creates,
the lock's pid *and* kernel start time (so a reused pid after a restart isn't
mistaken for the owner), and the live session records in
`$CLAUDE_CONFIG_DIR/sessions/`. Every `FLEET_WORKTREE_SWEEP_MINUTES`, and when
an instance is re-run, it removes each worktree that

- Claude created (has `CLAUDE_BASE`),
- nothing live is attached to (no live lock owner, no live session in it),
- has a clean `git status` (no modified or untracked files), and
- is at least two minutes old.

Its `worktree-*` branch is deleted only if it has no commits of its own;
otherwise the branch is kept, so committed work survives whether or not it was
merged (squash merges make "merged" unknowable from git alone). Worktrees with
uncommitted work are kept and flagged; the detail page can remove them after a
confirmation. Worktrees you created yourself and hand-set locks are never
touched. Transcripts are kept, so ended sessions stay in the app and their PRs
stay listed.

## How state is modeled

The UI reconciles three sources on every refresh:

- **SQLite** — the mapping of instance id → repo, working tree, tmux session,
  relay URL, credential and spawn mode.
- **tmux** — `tmux has-session` decides running vs stopped.
- **Filesystem** — directories under `FLEET_ROOT` with no DB row show up as
  untracked orphans.

So `running` = live tmux session, `orphan` = working tree on disk without a live
session, `missing` = tracked but the working tree is gone.
