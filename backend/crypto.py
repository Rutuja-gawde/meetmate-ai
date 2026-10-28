"""Encrypts per-user integration secrets (Google refresh token, Notion token, Slack webhook)
before they are stored in the database.

Generate a key once and put it in backend/.env as TOKEN_ENCRYPTION_KEY:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Losing or changing this key makes every stored connection unreadable (users reconnect).
"""

import json
import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    key = (os.getenv("TOKEN_ENCRYPTION_KEY") or "").strip()
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set in backend/.env")
    return Fernet(key.encode())


def encrypt_json(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode("utf-8")).decode("utf-8")


def decrypt_json(token: str) -> dict | None:
    try:
        return json.loads(_fernet().decrypt(token.encode("utf-8")).decode("utf-8"))
    except (InvalidToken, ValueError):
        return None
