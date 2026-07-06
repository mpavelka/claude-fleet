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


def available() -> bool:
    """True when we can actually encrypt/decrypt (library present + key set)."""
    return Fernet is not None and bool(config.SECRET_KEY)


def _cipher() -> "Fernet":
    if Fernet is None:
        raise RuntimeError("The 'cryptography' package is not installed.")
    key = config.SECRET_KEY
    if not key:
        raise RuntimeError(
            "FLEET_SECRET_KEY is not set; credential storage is disabled. "
            "Generate one with `python crypto.py`."
        )
    return Fernet(key.encode())


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
