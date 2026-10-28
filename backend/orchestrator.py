import os
import shutil
import subprocess

# --- Paths to each part of the project --------------------------------------
# Defaults to the folder that contains backend/, transcription/ and summarization/,
# so it works wherever the repo is cloned. Set MEETMATE_ROOT in backend/.env to override.
PROJECT_ROOT = os.getenv("MEETMATE_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
TRANSCRIPTION_DIR = os.path.join(PROJECT_ROOT, "transcription")
SUMMARIZATION_DIR = os.path.join(PROJECT_ROOT, "summarization")


def _venv_python(project_dir: str) -> str:
    """Finds the Python inside a folder's meetmate-env (Windows or Linux layout)."""
    windows_path = os.path.join(project_dir, "meetmate-env", "Scripts", "python.exe")
    if os.path.exists(windows_path):
        return windows_path
    return os.path.join(project_dir, "meetmate-env", "bin", "python")


TRANSCRIPTION_PYTHON = _venv_python(TRANSCRIPTION_DIR)
SUMMARIZATION_PYTHON = _venv_python(SUMMARIZATION_DIR)


def _cleanup(paths) -> None:
    """Deletes temporary work files so recordings and transcripts are not kept.
    Set KEEP_WORK_FILES=1 in backend/.env to keep them while testing."""
    if os.getenv("KEEP_WORK_FILES", "0").strip() == "1":
        return
    for path in paths:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def run_transcription(audio_source_path: str, participants: list[str] | None = None,
                      timeout_seconds: int = 3600) -> str:
    """Copies the uploaded audio into the transcription folder, runs transcribe_diarize.py
    and returns the transcript text.

    The Whisper model is NOT passed on the command line: transcribe_diarize.py picks it
    from WHISPER_MODEL in transcription/.env (default large-v3-turbo). Passing a model
    here would silently override that choice.

    participants: names typed on the upload form (may be empty). They reach the script as
    MEETMATE_PARTICIPANTS; an empty value means "unknown", and the script then auto-detects.

    timeout_seconds: hard ceiling so a stuck subprocess (e.g. a failed model download)
    fails loudly instead of hanging the request forever.
    """
    filename = os.path.basename(audio_source_path)
    base_name = os.path.splitext(filename)[0]
    dest_path = os.path.join(TRANSCRIPTION_DIR, filename)
    transcript_path = os.path.join(TRANSCRIPTION_DIR, f"{base_name}_transcript.txt")
    transcript_json_path = os.path.join(TRANSCRIPTION_DIR, f"{base_name}_transcript.json")

    shutil.copy(audio_source_path, dest_path)

    print(f"[orchestrator] Starting transcription subprocess "
          f"(timeout={timeout_seconds}s)...", flush=True)
    try:
        try:
            result = subprocess.run(
                [TRANSCRIPTION_PYTHON, "transcribe_diarize.py", filename],
                cwd=TRANSCRIPTION_DIR,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                env={**os.environ, "MEETMATE_PARTICIPANTS": "|".join(participants or [])},
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"Transcription timed out after {timeout_seconds}s. This usually means "
                f"a model is stuck downloading, or the file is very long. Partial output:\n{e.stdout or ''}"
            )

        # Show the script's own progress and [time] lines in this terminal.
        print(result.stdout, flush=True)

        if result.returncode != 0:
            raise RuntimeError(f"Transcription failed:\n{result.stderr}")

        with open(transcript_path, "r", encoding="utf-8") as f:
            return f.read()
    finally:
        _cleanup([dest_path, transcript_path, transcript_json_path])


def run_summarization(transcript_text: str, timeout_seconds: int = 900) -> str:
    """Writes the transcript into the summarization folder, runs summarizer.py and
    returns the summary JSON string.

    timeout_seconds: hard ceiling so a stuck Ollama call fails loudly instead of
    hanging the request forever.
    """
    transcript_path = os.path.join(SUMMARIZATION_DIR, "transcript.txt")
    summary_path = os.path.join(SUMMARIZATION_DIR, "summary.json")

    with open(transcript_path, "w", encoding="utf-8") as f:
        f.write(transcript_text)

    print(f"[orchestrator] Starting summarization subprocess "
          f"(timeout={timeout_seconds}s)...", flush=True)
    try:
        try:
            result = subprocess.run(
                [SUMMARIZATION_PYTHON, "summarizer.py", "transcript.txt"],
                cwd=SUMMARIZATION_DIR,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"Summarization timed out after {timeout_seconds}s - check that Ollama "
                f"is running and the model is pulled locally. Partial output:\n{e.stdout or ''}"
            )

        print(result.stdout, flush=True)

        if result.returncode != 0:
            raise RuntimeError(f"Summarization failed:\n{result.stderr}")

        with open(summary_path, "r", encoding="utf-8") as f:
            return f.read()
    finally:
        _cleanup([transcript_path, summary_path])