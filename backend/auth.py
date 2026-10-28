"""Checks the Supabase login token that the website sends with every request.

The website signs the user in with Supabase and sends  Authorization: Bearer <token>.
This file verifies that token and tells the API who the user is. Nothing is trusted
from the website except a token that Supabase itself signed.

Settings (backend/.env):
    SUPABASE_URL=https://<project>.supabase.co
    SUPABASE_JWT_SECRET=      # only needed if your project still uses the legacy shared
                              # secret (Project Settings > API > JWT Secret). Projects that
                              # use the newer signing keys need nothing here: the public
                              # keys are fetched from SUPABASE_URL automatically.
"""

import os

import jwt
from fastapi import Header, HTTPException
from jwt import PyJWKClient

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "").strip()

_jwks_client = (
    PyJWKClient(f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json", cache_keys=True, lifespan=3600)
    if SUPABASE_URL
    else None
)


def _decode(token: str) -> dict:
    header = jwt.get_unverified_header(token)
    alg = header.get("alg", "")
    if alg.startswith("HS"):
        if not JWT_SECRET:
            raise jwt.InvalidTokenError("HS256 token but SUPABASE_JWT_SECRET is not set")
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience="authenticated", leeway=300)
    if _jwks_client is None:
        raise jwt.InvalidTokenError("SUPABASE_URL is not set")
    signing_key = _jwks_client.get_signing_key_from_jwt(token)
    return jwt.decode(token, signing_key.key, algorithms=["ES256", "RS256"], audience="authenticated", leeway=300)


def user_from_authorization(authorization: str | None) -> dict | None:
    """Returns {"id", "email"} for a valid 'Bearer <token>' header, otherwise None."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    try:
        claims = _decode(token)
    except Exception as e:  # expired, wrong signature, malformed, key fetch failed...
        print(f"[auth] rejected token: {type(e).__name__}: {e}", flush=True)
        return None
    user_id = claims.get("sub")
    if not user_id:
        return None
    return {"id": user_id, "email": claims.get("email") or ""}


def current_user(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI dependency: use  user = Depends(current_user)  on any protected route."""
    user = user_from_authorization(authorization)
    if user is None:
        raise HTTPException(status_code=401, detail="Please sign in to continue.")
    return user
