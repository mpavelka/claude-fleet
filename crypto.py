"""Symmetric encryption for credential secrets at rest.

Secrets are encrypted with a master key read from the FLEET_SECRET_KEY
environment variable -- the key never lives in the database. If the key is
absent we fail closed: credential features are disabled but the rest of the app
still runs.

Generate a key with:  python crypto.py
"""
try:
    from cryptography.fernet import Fernet
except ImportError:  # pragma: no cover - dependency missing
    Fernet = None

import config


def _key() -> str | None:
    """The configured key, trimmed of stray whitespace/newlines from pasting."""
    key = config.SECRET_KEY
    return key.strip() if key else None


def key_status() -> str:
    """One of 'ok' | 'missing' | 'invalid' | 'no-lib'. Anything other than 'ok'
    means credential storage is disabled."""
    if Fernet is None:
        return "no-lib"
    key = _key()
    if not key:
        return "missing"
    try:
        Fernet(key.encode())
    except Exception:
        return "invalid"
    return "ok"


def key_message() -> str | None:
    """Human-readable reason credentials are disabled, or None when all good."""
    return {
        "ok": None,
        "missing": "FLEET_SECRET_KEY is not set.",
        "invalid": "FLEET_SECRET_KEY is not a valid key (must be a value from "
                   "`python crypto.py`).",
        "no-lib": "The 'cryptography' package is not installed.",
    }[key_status()]


def available() -> bool:
    """True only when we can actually encrypt/decrypt with the current key."""
    return key_status() == "ok"


def _cipher() -> "Fernet":
    msg = key_message()
    if msg is not None:
        raise RuntimeError(f"{msg} Credential storage is disabled.")
    return Fernet(_key().encode())


def encrypt(plaintext: str) -> str:
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _cipher().decrypt(token.encode()).decode()


def generate_key() -> str:
    if Fernet is None:
        raise RuntimeError("The 'cryptography' package is not installed.")
    return Fernet.generate_key().decode()


if __name__ == "__main__":
    print(generate_key())
