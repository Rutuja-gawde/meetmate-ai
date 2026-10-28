"""Database access (Supabase Postgres). Only the backend talks to the database, using the
service-role key, so every function here takes the user id and filters by it.

Settings (backend/.env):
    SUPABASE_URL=https://<project>.supabase.co
    SUPABASE_SERVICE_ROLE_KEY=...   # Project Settings > API. SECRET: never put it in the website.
"""

import os
from datetime import datetime, timezone

from supabase import Client, create_client

from crypto import decrypt_json, encrypt_json

PROVIDERS = ("google", "notion", "slack")

_client: Client | None = None


def client() -> Client:
    global _client
    if _client is None:
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in backend/.env")
        _client = create_client(url, key)
    return _client


# ---------------------------------------------------------------- connections

def save_connection(user_id: str, provider: str, data: dict) -> None:
    client().table("connections").upsert({
        "user_id": user_id,
        "provider": provider,
        "data_encrypted": encrypt_json(data),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


def get_connection(user_id: str, provider: str) -> dict | None:
    rows = (client().table("connections").select("data_encrypted")
            .eq("user_id", user_id).eq("provider", provider).limit(1).execute().data)
    return decrypt_json(rows[0]["data_encrypted"]) if rows else None


def get_connections(user_id: str) -> dict[str, dict]:
    """provider -> decrypted settings, for every provider the user has connected."""
    rows = (client().table("connections").select("provider,data_encrypted")
            .eq("user_id", user_id).execute().data)
    result = {}
    for row in rows:
        data = decrypt_json(row["data_encrypted"])
        if data is not None:
            result[row["provider"]] = data
    return result


def delete_connection(user_id: str, provider: str) -> None:
    client().table("connections").delete().eq("user_id", user_id).eq("provider", provider).execute()


# ------------------------------------------------------------------- meetings

def save_meeting(user_id: str, filename: str, result: dict) -> str | None:
    row = client().table("meetings").insert({
        "user_id": user_id,
        "filename": filename,
        "result": result,
    }).execute().data
    return row[0]["id"] if row else None


def list_meetings(user_id: str, limit: int = 50) -> list[dict]:
    return (client().table("meetings").select("id,filename,created_at,result")
            .eq("user_id", user_id).order("created_at", desc=True).limit(limit).execute().data)


def get_meeting(user_id: str, meeting_id: str) -> dict | None:
    rows = (client().table("meetings").select("id,filename,created_at,result")
            .eq("user_id", user_id).eq("id", meeting_id).limit(1).execute().data)
    return rows[0] if rows else None


def delete_meeting(user_id: str, meeting_id: str) -> None:
    client().table("meetings").delete().eq("user_id", user_id).eq("id", meeting_id).execute()
