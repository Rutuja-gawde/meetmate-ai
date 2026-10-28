"""
MeetMate AI - backend API
=========================
1. Users sign in on the website with Supabase (Google). Every request carries their
   login token, and this server checks it (auth.py).
2. The website uploads a recording to POST /jobs and immediately gets a job id.
3. A single background worker processes jobs one at a time (the GPU can only do one
   meeting at once), so extra uploads wait in a short queue.
4. The website asks GET /jobs/<id> every few seconds and shows the real stage:
   waiting, transcribing, summarizing, syncing, done.
5. Finished meetings are saved to the user's own history (database), and the results are
   sent to whichever apps THAT user connected (Google Calendar + Gmail, Notion, Slack),
   using their own credentials.

Settings (backend/.env):
    SUPABASE_URL=                  # https://<project>.supabase.co
    SUPABASE_SERVICE_ROLE_KEY=     # SECRET, backend only
    SUPABASE_JWT_SECRET=           # only for projects on the legacy shared JWT secret
    TOKEN_ENCRYPTION_KEY=          # see crypto.py
    GOOGLE_CLIENT_ID=              # the same Google OAuth client configured in Supabase
    GOOGLE_CLIENT_SECRET=
    ALLOWED_ORIGINS=               # your website address(es), e.g. https://meetmate.vercel.app
    MAX_UPLOAD_MB=90               # Cloudflare's free plan rejects uploads over 100 MB
    MAX_QUEUED_JOBS=3              # how many recordings may wait; more get "server busy"
    RESULT_TTL_MINUTES=60          # how long a finished job stays in memory
    UPLOAD_DIR=uploads
    FRONTEND_DIST=                 # optional: also serve a built website from this server
"""

import json
import os
import threading
import time
import traceback
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # must run before the modules below read their settings

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from auth import current_user, user_from_authorization
from task_agent import structure_tasks
from automation_agent import push_to_notion, push_to_slack, check_notion, send_slack_test
from google_agent import push_to_calendar, send_summary_email, build_summary_email
from orchestrator import run_transcription, run_summarization
from participants import clean_participants


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


MAX_UPLOAD_MB = _env_int("MAX_UPLOAD_MB", 90)
MAX_QUEUED_JOBS = _env_int("MAX_QUEUED_JOBS", 3)
MAX_JOBS_PER_USER = _env_int("MAX_JOBS_PER_USER", 1)  # waiting + running at once
RESULT_TTL_MINUTES = _env_int("RESULT_TTL_MINUTES", 60)
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads"))
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if origin.strip()
]
FRONTEND_DIST = Path(
    os.getenv("FRONTEND_DIST") or (Path(__file__).resolve().parent.parent / "frontend" / "dist")
)
ALLOWED_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac",
}

# ---------------------------------------------------------------------------
# Integrations (each user's own connected apps)
# ---------------------------------------------------------------------------

# A call that hangs keeps its thread busy until it returns, so the pool is larger
# than the number of integrations to leave room for a few stuck calls.
_automation_pool = ThreadPoolExecutor(max_workers=8)


def _run_automations_parallel(jobs, timeout_seconds: int = 30):
    """Runs every (label, fn, args) job at the same time and waits at most
    timeout_seconds in total. Never raises - returns one status dict per job so
    one broken integration can't take down the whole response."""
    started = time.monotonic()
    futures = [(label, _automation_pool.submit(fn, *args)) for label, fn, args in jobs]

    statuses = []
    for label, future in futures:
        remaining = max(0.0, timeout_seconds - (time.monotonic() - started))
        try:
            future.result(timeout=remaining)
            print(f"[main] {label}: OK", flush=True)
            statuses.append({"step": label, "status": "ok"})
        except FutureTimeoutError:
            future.cancel()
            print(f"[main] {label}: TIMED OUT after {timeout_seconds}s - skipping", flush=True)
            statuses.append({"step": label, "status": "timed_out"})
        except Exception as e:
            print(f"[main] {label}: FAILED - {e}", flush=True)
            statuses.append({"step": label, "status": "failed", "error": str(e)})
    return statuses


# ---------------------------------------------------------------------------
# Job queue
# ---------------------------------------------------------------------------

jobs: dict[str, dict] = {}
pending: deque[str] = deque()
jobs_lock = threading.Lock()
work_available = threading.Condition(jobs_lock)


def _set_job(job_id: str, **fields) -> None:
    with jobs_lock:
        jobs[job_id].update(fields)


def _purge_old_jobs() -> None:
    cutoff = time.time() - RESULT_TTL_MINUTES * 60
    with jobs_lock:
        expired = [jid for jid, job in jobs.items() if job["finished"] and job["finished"] < cutoff]
        for jid in expired:
            del jobs[jid]


def _load_connections(user_id: str) -> dict:
    """The apps this user connected. A database hiccup must not lose the meeting,
    so on error the meeting is processed and simply not synced."""
    try:
        return db.get_connections(user_id)
    except Exception as e:
        print(f"[main] Could not load connections for {user_id[:8]}: {e}", flush=True)
        return {}


def _build_sync_jobs(connections: dict, tasks, meeting_title, email_to, email_text, email_html):
    sync_jobs = []
    if "notion" in connections:
        sync_jobs.append(("notion", push_to_notion, (tasks, connections["notion"])))
    if "slack" in connections:
        sync_jobs.append(("slack", push_to_slack, (tasks, connections["slack"], meeting_title)))
    if "google" in connections:
        google = connections["google"]
        sync_jobs.append(("calendar", push_to_calendar, (tasks, google)))
        sync_jobs.append(("email", send_summary_email,
                          (email_to, f"Meeting minutes: {meeting_title}", email_text, email_html, google)))
    return sync_jobs


def _process(job_id: str, path: str, filename: str, user: dict, participants: list[str]) -> None:
    started = time.perf_counter()
    try:
        _unload_summarizer()
        transcript = run_transcription(path, participants)
        transcription_time = time.perf_counter() - started

        _set_job(job_id, stage="summarizing")
        stage_start = time.perf_counter()
        summary_json = run_summarization(transcript)
        summary_data = json.loads(summary_json)
        tasks = structure_tasks(summary_json)
        summarization_time = time.perf_counter() - stage_start

        automation_status = []
        automation_time = 0.0
        connections = _load_connections(user["id"])
        if connections:
            _set_job(job_id, stage="syncing")
            stage_start = time.perf_counter()
            meeting_title = os.path.splitext(filename)[0]
            email_text, email_html = build_summary_email(meeting_title, summary_data, tasks)
            automation_status = _run_automations_parallel(
                _build_sync_jobs(connections, tasks, meeting_title, user["email"], email_text, email_html)
            )
            automation_time = time.perf_counter() - stage_start

        result = {
            "status": "processed",
            "transcript": transcript,
            "summary": summary_data.get("summary"),
            "agenda": summary_data.get("agenda", []),
            "decisions": summary_data.get("decisions"),
            "tasks": tasks,
            "automation_status": automation_status,
        }
        try:
            result["meeting_id"] = db.save_meeting(user["id"], filename, result)
        except Exception as e:
            print(f"[main] Could not save meeting history: {e}", flush=True)

        _set_job(job_id, status="done", stage="done", result=result, finished=time.time())
        print(f"[main] Job {job_id[:8]} done in {time.perf_counter() - started:.0f}s "
              f"(transcription {transcription_time:.0f}s, summarization {summarization_time:.0f}s, "
              f"integrations {automation_time:.0f}s)", flush=True)
    except Exception:
        # The full error goes to this terminal; the website only gets a safe message.
        print(f"[main] Job {job_id[:8]} FAILED:\n{traceback.format_exc()}", flush=True)
        _set_job(
            job_id,
            status="failed",
            error="Something went wrong while processing this recording. "
                  "Please try again, or try a different file.",
            finished=time.time(),
        )
    finally:
        try:
            os.remove(path)  # never keep uploaded recordings
        except OSError:
            pass


def _worker() -> None:
    while True:
        with work_available:
            while not pending:
                work_available.wait()
            job_id = pending.popleft()
            job = jobs[job_id]
            job["status"] = "running"
            job["stage"] = "transcribing"
            path, filename, user = job["path"], job["filename"], job["user"]
            participants = job["participants"]
        _process(job_id, path, filename, user, participants)


threading.Thread(target=_worker, name="meetmate-worker", daemon=True).start()


def _unload_summarizer() -> None:
    """Frees the GPU memory held by the Ollama model before transcription starts.

    The speech models (pyannote + Whisper) need the GPU to themselves. On a card with less
    than ~16 GB, a language model still sitting in memory makes Windows spill the speech
    models into slow shared system memory, and a job that takes minutes takes half an hour.
    The model is loaded again for the summary step (a few seconds). Set KEEP_LLM_LOADED=1
    in backend/.env only if your GPU has plenty of memory. Best effort: errors are ignored."""
    if os.getenv("KEEP_LLM_LOADED", "").strip() == "1":
        return
    import httpx

    host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").strip()
    if not host.startswith("http"):
        host = f"http://{host}"
    model = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
    try:
        httpx.post(f"{host.rstrip('/')}/api/generate",
                   json={"model": model, "keep_alive": 0}, timeout=15)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def reject_bad_uploads_early(request: Request, call_next):
    """Checks the login and the declared size BEFORE the upload body is read,
    so strangers and oversized files are turned away cheaply."""
    if request.method == "POST" and request.url.path == "/jobs":
        if user_from_authorization(request.headers.get("authorization")) is None:
            return JSONResponse({"detail": "Please sign in to continue."}, status_code=401)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > (MAX_UPLOAD_MB + 5) * 1024 * 1024:
            return JSONResponse(
                {"detail": f"This file is too large. The limit is {MAX_UPLOAD_MB} MB."},
                status_code=413,
            )
    return await call_next(request)


# Added after the middleware above so it wraps it: error responses also carry CORS headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/health")
def health_check():
    return {"status": "MeetMate AI backend is running"}


@app.get("/config")
def public_config():
    """What the website needs to know before showing the upload form."""
    return {"max_upload_mb": MAX_UPLOAD_MB}


@app.post("/jobs", status_code=202)
async def create_job(file: UploadFile, participants: str = Form(""), user: dict = Depends(current_user)):
    _purge_old_jobs()
    names = clean_participants(participants)

    filename = os.path.basename(file.filename or "recording")
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Unsupported file type. Upload an audio or video recording.")

    with jobs_lock:
        if len(pending) >= MAX_QUEUED_JOBS:
            raise HTTPException(status_code=503, detail="The server is busy right now. Please try again in a few minutes.")
        active = sum(1 for j in jobs.values()
                     if j["user"]["id"] == user["id"] and j["status"] in ("queued", "running"))
        if active >= MAX_JOBS_PER_USER:
            raise HTTPException(status_code=429, detail="You already have a recording being processed. "
                                                        "Please wait for it to finish (refresh the page to see its progress).")

    job_id = uuid.uuid4().hex
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    destination = UPLOAD_DIR / f"{job_id}{extension}"
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    written = 0

    try:
        with open(destination, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"This file is too large. The limit is {MAX_UPLOAD_MB} MB.",
                    )
                out.write(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="The file is empty.")
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except Exception:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Could not save the upload. Please try again.")

    with work_available:
        # Checked again: other uploads may have been accepted while this one was uploading.
        active = sum(1 for j in jobs.values()
                     if j["user"]["id"] == user["id"] and j["status"] in ("queued", "running"))
        if len(pending) >= MAX_QUEUED_JOBS or active >= MAX_JOBS_PER_USER:
            destination.unlink(missing_ok=True)
            raise HTTPException(status_code=503, detail="The server is busy right now. Please try again in a few minutes.")
        jobs[job_id] = {
            "status": "queued",
            "stage": "waiting",
            "user": {"id": user["id"], "email": user["email"]},
            "filename": filename,
            "participants": names,
            "path": str(destination),
            "created": time.time(),
            "finished": None,
            "result": None,
            "error": None,
        }
        pending.append(job_id)
        work_available.notify()

    return {"job_id": job_id}


@app.get("/jobs/active")
def get_active_job(user: dict = Depends(current_user)):
    """The user's recording that is waiting or being processed, if any. The website calls this
    when it opens, so closing the tab (or refreshing) does not lose track of a running job.
    Declared before /jobs/{job_id} so "active" is not read as a job id."""
    with jobs_lock:
        for job_id, job in jobs.items():
            if job["user"]["id"] == user["id"] and job["status"] in ("queued", "running"):
                return {"job_id": job_id, "filename": job["filename"],
                        "status": job["status"], "stage": job["stage"]}
    return {"job_id": None}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, user: dict = Depends(current_user)):
    _purge_old_jobs()

    with jobs_lock:
        job = jobs.get(job_id)
        # Someone else's job looks exactly like a job that does not exist.
        if job is None or job["user"]["id"] != user["id"]:
            raise HTTPException(status_code=404, detail="This job was not found. It may have expired.")
        payload = {
            "status": job["status"],
            "stage": job["stage"],
            "queue_position": (list(pending).index(job_id) + 1) if job_id in pending else 0,
        }
        if job["status"] == "done":
            payload["result"] = job["result"]
        elif job["status"] == "failed":
            payload["error"] = job["error"]
    return payload


# ---------------------------------------------------------------------------
# Meeting history
# ---------------------------------------------------------------------------

@app.get("/meetings")
def list_meetings(user: dict = Depends(current_user)):
    rows = db.list_meetings(user["id"])
    return [
        {
            "id": r["id"],
            "filename": r["filename"],
            "created_at": r["created_at"],
            "summary": (r.get("result") or {}).get("summary"),
            "task_count": len((r.get("result") or {}).get("tasks") or []),
        }
        for r in rows
    ]


@app.get("/meetings/{meeting_id}")
def get_meeting(meeting_id: str, user: dict = Depends(current_user)):
    row = db.get_meeting(user["id"], meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Meeting not found.")
    return row


@app.delete("/meetings/{meeting_id}", status_code=204)
def delete_meeting(meeting_id: str, user: dict = Depends(current_user)):
    db.delete_meeting(user["id"], meeting_id)


# ---------------------------------------------------------------------------
# Connections (each user's own Google / Notion / Slack)
# ---------------------------------------------------------------------------

class GoogleConnection(BaseModel):
    refresh_token: str


class NotionConnection(BaseModel):
    token: str
    database_id: str


class SlackConnection(BaseModel):
    webhook_url: str


@app.get("/connections")
def list_connections(user: dict = Depends(current_user)):
    connected = db.get_connections(user["id"])
    return {provider: provider in connected for provider in db.PROVIDERS}


@app.put("/connections/google", status_code=204)
def connect_google(body: GoogleConnection, user: dict = Depends(current_user)):
    token = body.refresh_token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing Google refresh token.")
    db.save_connection(user["id"], "google", {"refresh_token": token})


@app.put("/connections/notion", status_code=204)
def connect_notion(body: NotionConnection, user: dict = Depends(current_user)):
    token, database_id = body.token.strip(), body.database_id.strip()
    if not token or not database_id:
        raise HTTPException(status_code=400, detail="Enter both the Notion token and the database ID.")
    # Accept a pasted database URL too: .../My-Tasks-<32 hex chars>?v=...
    if "/" in database_id or "?" in database_id:
        import re
        found = re.findall(r"[0-9a-fA-F]{32}", database_id.replace("-", ""))
        if not found:
            raise HTTPException(status_code=400, detail="That does not look like a Notion database ID or link.")
        database_id = found[-1]
    connection = {"token": token, "database_id": database_id}
    try:
        check_notion(connection)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.save_connection(user["id"], "notion", connection)


@app.put("/connections/slack", status_code=204)
def connect_slack(body: SlackConnection, user: dict = Depends(current_user)):
    url = body.webhook_url.strip()
    # Only Slack's own webhook addresses: stops this server being used to call arbitrary URLs.
    if not url.startswith("https://hooks.slack.com/"):
        raise HTTPException(status_code=400, detail="Enter a Slack incoming webhook URL "
                                                    "(it starts with https://hooks.slack.com/).")
    connection = {"webhook_url": url}
    try:
        send_slack_test(connection)  # posts a test message, so the user sees at once that it works
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.save_connection(user["id"], "slack", connection)


@app.delete("/connections/{provider}", status_code=204)
def disconnect(provider: str, user: dict = Depends(current_user)):
    if provider not in db.PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown connection.")
    db.delete_connection(user["id"], provider)


# Serve the built website from the same address (must be last, after the API routes).
# Optional: with the website on Vercel this is normally not used.
if (FRONTEND_DIST / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
    print(f"[main] Serving the website from {FRONTEND_DIST}", flush=True)
