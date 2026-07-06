"""Central configuration, all overridable via environment variables.

Nothing here requires root. Defaults live under the user's home so the app
runs anywhere `python3`, `git`, `tmux`, and `claude` are available.
"""
import os

# Root directory that holds one cloned working tree per instance.
FLEET_ROOT = os.path.abspath(
    os.environ.get("FLEET_ROOT", os.path.expanduser("~/.claude-fleet/instances"))
)

# SQLite state file.
DB_PATH = os.path.abspath(
    os.environ.get("FLEET_DB", os.path.expanduser("~/.claude-fleet/fleet.db"))
)

# Per-instance secret material (git credential files) lives here, OUTSIDE any
# working tree, one subdir per instance. Never committed, never in the pane log.
SECRETS_ROOT = os.path.abspath(
    os.environ.get("FLEET_SECRETS", os.path.expanduser("~/.claude-fleet/secrets"))
)

# The command tmux runs inside each instance's working tree. This is the one
# thing to verify against your installed `claude` version -- see README.
CLAUDE_RC_CMD = os.environ.get("CLAUDE_RC_CMD", "claude remote-control")

# Regex used to pull the relay URL out of the captured session log.
RELAY_REGEX = os.environ.get("RELAY_REGEX", r"https?://\S+")

# Defense-in-depth: if set, every request must carry this value in the
# X-Auth-Token header. The reverse proxy is expected to inject it. Leave unset
# when you trust the proxy alone.
AUTH_TOKEN = os.environ.get("FLEET_AUTH_TOKEN")

# Bind address. Default to loopback so the app is only reachable via the proxy.
HOST = os.environ.get("FLEET_HOST", "127.0.0.1")
PORT = int(os.environ.get("FLEET_PORT", "8700"))
