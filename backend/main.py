"""
MeetMate AI - backend API

How it works
------------
1. The website uploads a recording to POST /jobs and immediately gets a job id.
2. A single background worker processes jobs one at a time (the GPU can only
   do one meeting at once), so extra uploads wait in a short queue.
3. The website asks GET /jobs/<id> every few seconds and shows the real stage:
   waiting, transcribing, summarizing, syncing, done.
4. If the built website exists in ../frontend/dist, this server also serves it,
   so one address (and one tunnel) is enough to share the whole app.

Settings (backend/.env) - all optional:
    ACCESS_CODE=             # anyone using the app must type this. REQUIRED when public.
    ENABLE_INTEGRATIONS=0    # 1 = also push to Notion/Slack/Calendar/Gmail (your accounts!)
    EMAIL_RECIPIENTS=        # only used when integrations are on
    MAX_UPLOAD_MB=90         # Cloudflare's free plan rejects uploads over 100 MB
    MAX_QUEUED_JOBS=3        # how many recordings may wait; more get "server busy"
    RESULT_TTL_MINUTES=60    # how long finished results stay available
    ALLOWED_ORIGINS=         # only if the website is hosted on a different address
    UPLOAD_DIR=uploads
    FRONTEND_DIST=           # defaults to ../frontend/dist
"""

import json
import os
import secrets
import threading
import time
import traceback
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from task_agent import structure_tasks
from automation_agent import push_to_notion, push_to_slack
from google_agent import push_to_calendar, send_summary_email, build_summary_email
from orchestrator import run_transcription, run_summarization

load_dotenv()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


ACCESS_CODE = os.getenv("ACCESS_CODE", "").strip()
ENABLE_INTEGRATIONS = os.getenv("ENABLE_INTEGRATIONS", "0").strip() == "1"
MAX_UPLOAD_MB = _env_int("MAX_UPLOAD_MB", 90)
MAX_QUEUED_JOBS = _env_int("MAX_QUEUED_JOBS", 3)
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

if not ACCESS_CODE:
    print("[main] NOTE: no ACCESS_CODE is set, so anyone who can reach this server can use it. "
          "Set ACCESS_CODE in backend/.env before sharing a public link.", flush=True)
if ENABLE_INTEGRATIONS:
    print("[main] Integrations are ON: results are sent to the Notion, Slack, Calendar and Gmail "
          "accounts configured on this machine.", flush=True)


def _access_ok(code) -> bool:
    if not ACCESS_CODE:
        return True
    if not code:
        return False
    return secrets.compare_digest(code.encode("utf-8"), ACCESS_CODE.encode("utf-8"))


def _check_access(code) -> None:
    if not _access_ok(code):
        raise HTTPException(status_code=401, detail="Wrong or missing access code.")


# ---------------------------------------------------------------------------
# Integrations (only used when ENABLE_INTEGRATIONS=1)
# ---------------------------------------------------------------------------

# A call that hangs keeps its thread busy until it returns, so the pool is larger
# than the number of integrations to leave room for a few stuck calls.
_automation_pool = ThreadPoolExecutor(max_workers=8)


def _email_recipients() -> list[str]:
    raw = os.getenv("EMAIL_RECIPIENTS", "")
    return [address.strip() for address in raw.split(",") if address.strip()]


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


def _process(job_id: str, path: str, filename: str) -> None:
    started = time.perf_counter()
    try:
        transcript = run_transcription(path)
        transcription_time = time.perf_counter() - started

        _set_job(job_id, stage="summarizing")
        stage_start = time.perf_counter()
        summary_json = run_summarization(transcript)
        summary_data = json.loads(summary_json)
        tasks = structure_tasks(summary_json)
        summarization_time = time.perf_counter() - stage_start

        automation_status = []
        automation_time = 0.0
        if ENABLE_INTEGRATIONS:
            _set_job(job_id, stage="syncing")
            stage_start = time.perf_counter()
            meeting_title = os.path.splitext(filename)[0]
            email_text, email_html = build_summary_email(meeting_title, summary_data, tasks)
            automation_status = _run_automations_parallel([
                ("notion", push_to_notion, (tasks,)),
                ("slack", push_to_slack, (tasks,)),
                ("calendar", push_to_calendar, (tasks,)),
                ("email", send_summary_email,
                 (_email_recipients(), f"Meeting minutes: {meeting_title}", email_text, email_html)),
            ])
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
            path, filename = job["path"], job["filename"]
        _process(job_id, path, filename)


threading.Thread(target=_worker, name="meetmate-worker", daemon=True).start()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def reject_bad_uploads_early(request: Request, call_next):
    """Checks the access code and the declared size BEFORE the upload body is read,
    so strangers and oversized files are turned away cheaply."""
    if request.method == "POST" and request.url.path == "/jobs":
        if not _access_ok(request.headers.get("x-access-code")):
            return JSONResponse({"detail": "Wrong or missing access code."}, status_code=401)
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
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
def health_check():
    return {"status": "MeetMate AI backend is running"}


@app.get("/config")
def public_config():
    """What the website needs to know before showing the upload form."""
    return {
        "requires_code": bool(ACCESS_CODE),
        "max_upload_mb": MAX_UPLOAD_MB,
        "integrations_enabled": ENABLE_INTEGRATIONS,
    }


@app.post("/jobs", status_code=202)
async def create_job(file: UploadFile, x_access_code: str | None = Header(default=None)):
    _check_access(x_access_code)
    _purge_old_jobs()

    filename = os.path.basename(file.filename or "recording")
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Unsupported file type. Upload an audio or video recording.")

    with jobs_lock:
        if len(pending) >= MAX_QUEUED_JOBS:
            raise HTTPException(status_code=503, detail="The server is busy right now. Please try again in a few minutes.")

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
        if len(pending) >= MAX_QUEUED_JOBS:
            destination.unlink(missing_ok=True)
            raise HTTPException(status_code=503, detail="The server is busy right now. Please try again in a few minutes.")
        jobs[job_id] = {
            "status": "queued",
            "stage": "waiting",
            "filename": filename,
            "path": str(destination),
            "created": time.time(),
            "finished": None,
            "result": None,
            "error": None,
        }
        pending.append(job_id)
        work_available.notify()

    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, x_access_code: str | None = Header(default=None)):
    _check_access(x_access_code)
    _purge_old_jobs()

    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
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


# Serve the built website from the same address (must be last, after the API routes).
if (FRONTEND_DIST / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
    print(f"[main] Serving the website from {FRONTEND_DIST}", flush=True)
else:
    print(f"[main] No built website found at {FRONTEND_DIST}. "
          "Run 'npm run build' in the frontend folder to create it.", flush=True)