"""Server-wide Claude authentication, driven from the web UI.

The browser OAuth flow works headlessly because `claude auth login --claudeai`
redirects to a hosted callback (platform.claude.com) that shows an
authorization code — no localhost callback to reach. We run the login in a
detached tmux session, scrape the authorize URL from its piped output, let the
user approve in their own browser, then feed the pasted code back into the
session with `tmux send-keys`.

This targets the default config dir (one Claude account for the whole server),
per the chosen scope.
"""
import json
import os
import re
import shlex
import subprocess
import time

import config

LOGIN_SESSION = "claude-fleet-login"


def _login_log() -> str:
    """Path of the login session's piped log. Computed lazily so it reflects the
    config loaded at startup (see config.load), not import-time defaults."""
    return os.path.join(os.path.dirname(config.DB_PATH), "claude-login.log")

# The authorize URL claude prints (and any hosted-callback variant).
# Restricted to RFC 3986 characters so a match stops at terminal escape bytes
# (BEL, ESC), which `\S` would happily swallow.
_URL_CHARS = r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]"
_AUTHORIZE_RE = re.compile(rf"https://{_URL_CHARS}*oauth/authorize{_URL_CHARS}*")

# Newer CLIs print the URL as an OSC 8 hyperlink: ESC ] 8 ; params ; URL ST,
# where ST is BEL or ESC \. Capture its target...
_OSC8_RE = re.compile(r"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]*)(?:\x07|\x1b\\)")
# ...and strip every other escape sequence (OSC, CSI, two-byte ESC) from the
# visible text.
_ESCAPE_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL/ST
    r"|\x1b\[[0-?]*[ -/]*[@-~]"           # CSI (colors, cursor moves)
    r"|\x1b[@-Z\\-_]"                     # other two-byte escapes
)


def _find_authorize_url(text: str) -> str | None:
    """Find the authorize URL in raw terminal output. Prefers an OSC 8
    hyperlink target and falls back to the visible text with escapes removed."""
    for target in _OSC8_RE.findall(text):
        m = _AUTHORIZE_RE.fullmatch(target.strip())
        if m:
            return m.group(0)
    m = _AUTHORIZE_RE.search(_ESCAPE_RE.sub("", text))
    return m.group(0) if m else None


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def status() -> dict:
    """Parsed `claude auth status --json`, or {'loggedIn': False} on any error
    (claude missing, not a subprocess-able env, etc.)."""
    try:
        r = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        return json.loads(r.stdout.strip() or "{}")
    except Exception:
        return {"loggedIn": False}


def _login_active() -> bool:
    return (
        subprocess.run(
            ["tmux", "has-session", "-t", LOGIN_SESSION], capture_output=True
        ).returncode
        == 0
    )


def login_url() -> str | None:
    """Scrape the authorize URL from the login session's piped log."""
    log = _login_log()
    if not os.path.isfile(log):
        return None
    try:
        text = open(log, "r", errors="replace").read()
    except OSError:
        return None
    return _find_authorize_url(text)


def login_state(error: str | None = None) -> dict:
    """UI-facing view: phase is 'in' | 'pending' | 'out'."""
    st = status()
    if st.get("loggedIn"):
        return {"phase": "in", "status": st, "error": error}
    if _login_active():
        return {"phase": "pending", "status": st, "url": login_url(), "error": error}
    return {"phase": "out", "status": st, "error": error}


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
def start_login() -> str | None:
    """Launch `claude auth login` in a detached session and wait briefly for the
    authorize URL to appear. Returns the URL (or None if it didn't show yet)."""
    log = _login_log()
    subprocess.run(["tmux", "kill-session", "-t", LOGIN_SESSION], capture_output=True)
    try:
        os.remove(log)
    except OSError:
        pass
    os.makedirs(os.path.dirname(log), exist_ok=True)

    # Wide window so the long URL isn't display-wrapped; BROWSER=true prevents
    # any attempt to open a browser on the server itself.
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", LOGIN_SESSION, "-x", "220", "-y", "50",
         "-e", "BROWSER=/usr/bin/true", "claude auth login --claudeai"],
        capture_output=True,
    )
    subprocess.run(
        ["tmux", "pipe-pane", "-t", LOGIN_SESSION, "-o", f"cat >> {shlex.quote(log)}"],
        capture_output=True,
    )
    return _wait(login_url, timeout=8)


def submit_code(code: str) -> bool:
    """Send the pasted authorization code to the login session and wait for the
    exchange to complete. Returns True if we end up logged in."""
    code = code.strip()
    if not code or not _login_active():
        return False
    subprocess.run(
        ["tmux", "send-keys", "-t", LOGIN_SESSION, code, "Enter"], capture_output=True
    )
    return bool(_wait(lambda: status().get("loggedIn") or None, timeout=12))


def logout() -> None:
    """Cancel any in-progress login and sign out."""
    subprocess.run(["tmux", "kill-session", "-t", LOGIN_SESSION], capture_output=True)
    subprocess.run(["claude", "auth", "logout"], capture_output=True, timeout=15)


def _wait(pred, timeout: float, interval: float = 0.5):
    """Poll `pred` until it returns something truthy or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = pred()
        if val:
            return val
        time.sleep(interval)
    return pred()
