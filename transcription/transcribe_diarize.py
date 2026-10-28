"""
MeetMate AI - Transcription Agent
==================================
Combines faster-whisper (speech-to-text) with pyannote.audio (speaker
diarization) to produce a single speaker-labeled transcript.

Speed: the whole recording is transcribed in one pass instead of once per
speaker turn, and the video OCR runs alongside the GPU work. Diarization and
Whisper also run at the same time when the GPU has enough memory (10 GB or more);
on a smaller GPU they run one after the other, because sharing a small GPU
makes both slower.

Accuracy: uses a large multilingual Whisper model by default and detects
the spoken language per 30-second window, so meetings that switch between
languages (for example English and Hindi) are handled.

Settings (optional, put them in the transcription/.env file):
    WHISPER_MODEL=large-v3-turbo   # or: small, medium, large-v3
    WHISPER_LANGUAGE=              # leave EMPTY for meetings that mix languages
                                   # (detected every 30 s). Only ONE code is allowed
                                   # here, e.g. en. Several codes fall back to auto-detect.
    HF_HUB_OFFLINE=1               # optional: once all models are downloaded, skip the
                                   # internet check so runs do not wait on slow Wi-Fi
    RUN_MODELS_TOGETHER=           # optional: 1 = diarization and Whisper at the same
                                   # time, 0 = one after the other (default: automatic)
    WHISPER_BATCHED=               # optional: 1 = batched Whisper on the GPU, usually 2-4x faster.
                                   # Off by default: compare the transcript with and without it
                                   # before trusting it for your languages.
    WHISPER_BATCH_SIZE=8           # only with WHISPER_BATCHED=1; lower it if the GPU runs out of memory
    WHISPER_COMPUTE_TYPE=          # optional: leave empty for the most accurate setting
                                   # (float16 on a GPU). Use int8_float16 only if the GPU
                                   # runs out of memory.
"""

import sys
import io
import os
import gc
import re
import json
import time
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# Load the .env file first: libraries such as huggingface_hub read some
# settings (for example HF_HUB_OFFLINE) at the moment they are imported.
load_dotenv()

import soundfile as sf
import torch
from faster_whisper import WhisperModel
from pyannote.audio import Pipeline

from speaker_name_mapper import extract_name_timeline, build_speaker_name_mapping
from text_fixes import NameCorrector, clean_word

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

DEFAULT_MODEL = "large-v3-turbo"


def participants_from_env() -> list[str]:
    """The names typed on the website's upload form (MEETMATE_PARTICIPANTS="A|B|C").
    Run by hand with no variable set, the three test names are used, as before."""
    raw = os.environ.get("MEETMATE_PARTICIPANTS")
    if raw is None:
        return ["Jarvis", "Friday", "Edith"]
    return [name.strip() for name in raw.split("|") if name.strip()]


def gpu_memory_gb() -> float:
    """Total memory of the first GPU in GB, or 0 when there is no GPU."""
    if torch.cuda.is_available():
        return torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
    return 0.0


def pick_compute_type(device: str) -> str:
    """float16 is the most accurate setting on a GPU, so it is the default.
    Only if the GPU runs out of memory, set WHISPER_COMPUTE_TYPE=int8_float16 in
    the .env file: it needs about half the memory, with a very small accuracy cost."""
    setting = (os.getenv("WHISPER_COMPUTE_TYPE") or "").strip()
    if setting:
        return setting
    return "float16" if device == "cuda" else "int8"


def should_run_models_together() -> bool:
    """Diarization and Whisper only run at the same time when the GPU is big
    enough to hold both. On a small GPU they compete for memory and both slow
    down. Override with RUN_MODELS_TOGETHER=1 or 0 in the .env file."""
    setting = (os.getenv("RUN_MODELS_TOGETHER") or "").strip()
    if setting in ("0", "1"):
        return setting == "1"
    return gpu_memory_gb() >= 10


def parse_language_setting(raw):
    """Turns the WHISPER_LANGUAGE setting into ONE language code, or None for
    automatic detection.

    Whisper can only be told a single fixed language. For a meeting that mixes
    several languages (for example "en, hi, mr"), the right choice is automatic
    detection, which looks at every 30-second window on its own."""
    codes = re.findall(r"\b[A-Za-z]{2,3}\b", raw or "")
    if len(codes) == 1:
        return codes[0].lower()
    if len(codes) > 1:
        print(f"[2/3] WHISPER_LANGUAGE lists several languages ({raw!r}); "
              "using automatic per-window language detection instead.", flush=True)
    return None


def has_video_track(file_path: str) -> bool:
    """Checks if the file has a video stream (vs audio-only), using ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", file_path],
        capture_output=True, text=True,
    )
    return "video" in result.stdout


def convert_to_wav(input_path: str) -> str:
    output_path = os.path.splitext(input_path)[0] + "_converted.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-ar", "16000", "-ac", "1", output_path],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return output_path


def diarize_audio(audio_path: str, hf_token: str, num_speakers: int | None = None):
    started = time.perf_counter()
    device = "cuda" if torch.cuda.is_available() else "cpu"

    print("[1/3] Loading diarization pipeline (first run may download model weights"
          " - this can take a while depending on your connection)...", flush=True)

    # try token= first, fall back to use_auth_token= for older/newer
    # pyannote.audio versions that expect the other argument name
    try:
        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=hf_token,
        )
    except TypeError:
        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            use_auth_token=hf_token,
        )

    pipeline.to(torch.device(device))
    load_seconds = time.perf_counter() - started

    print(f"[1/3] Running diarization on {audio_path} ({device.upper()}) ...", flush=True)
    processing_started = time.perf_counter()
    audio_np, sample_rate = sf.read(audio_path, dtype="float32")
    if audio_np.ndim == 1:
        waveform = torch.from_numpy(audio_np).unsqueeze(0)
    else:
        waveform = torch.from_numpy(audio_np.T)
    # With the participants typed on the form the speaker count is known and the result is
    # more accurate; without them the model works it out itself.
    speaker_hint = {"num_speakers": num_speakers} if num_speakers else {}
    diarization = pipeline({"waveform": waveform, "sample_rate": sample_rate}, **speaker_hint)

    speaker_turns = []
    if hasattr(diarization, "itertracks"):
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            speaker_turns.append((turn.start, turn.end, speaker))
    elif hasattr(diarization, "speaker_diarization"):
        for turn, speaker in diarization.speaker_diarization:
            speaker_turns.append((turn.start, turn.end, speaker))
    else:
        raise RuntimeError(
            f"Unrecognized diarization output type: {type(diarization)}"
        )
    # Hand the GPU memory back so Whisper has the whole card to itself.
    del pipeline, diarization
    if device == "cuda":
        gc.collect()
        torch.cuda.empty_cache()

    print(f"[1/3] Diarization done: {len(speaker_turns)} speaker turns.", flush=True)
    print(f"[time] Diarization: loading {load_seconds:.0f}s, processing "
          f"{time.perf_counter() - processing_started:.0f}s", flush=True)
    return speaker_turns


def load_full_audio_mono(audio_path: str):
    audio_np, sr = sf.read(audio_path, dtype="float32")
    if audio_np.ndim > 1:
        audio_np = audio_np.mean(axis=1)
    return audio_np, sr


def transcribe_full_audio(audio_path: str, model_size: str, language: str | None,
                          participants: list[str] | None = None):
    """
    Transcribes the whole recording in one pass and returns word-level
    timestamps. One pass is much faster than transcribing every speaker turn
    separately, and gives Whisper more context, which improves accuracy.
    """
    started = time.perf_counter()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = pick_compute_type(device)

    print(f"[2/3] Loading Whisper model '{model_size}' ({device.upper()}, {compute_type};"
          f" the first run downloads the model file)...", flush=True)
    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    load_seconds = time.perf_counter() - started

    audio, _sr = load_full_audio_mono(audio_path)  # already 16 kHz mono from convert_to_wav
    audio_seconds = len(audio) / _sr

    print(f"[2/3] Transcribing {audio_seconds:.0f}s of audio...", flush=True)
    processing_started = time.perf_counter()
    options = dict(
        language=language,
        # With no fixed language, detect the language of every 30-second
        # window so mixed-language meetings are not forced into one language.
        multilingual=language is None,
        beam_size=5 if device == "cuda" else 2,
        word_timestamps=True,
        vad_filter=True,                       # skip silence, avoids made-up text
        vad_parameters={"min_silence_duration_ms": 500},
        condition_on_previous_text=False,      # avoids repeating loops on long audio
    )
    segments = info = None
    if device == "cuda" and (os.getenv("WHISPER_BATCHED") or "").strip() == "1":
        try:
            from faster_whisper import BatchedInferencePipeline
            batch_size = int(os.getenv("WHISPER_BATCH_SIZE") or 8)
            batched_options = {k: v for k, v in options.items() if k != "condition_on_previous_text"}
            segments, info = BatchedInferencePipeline(model=model).transcribe(
                audio, batch_size=batch_size, **batched_options)
            print(f"[2/3] Using batched Whisper (batch size {batch_size}).", flush=True)
        except Exception as e:
            segments = info = None
            print(f"[2/3] Batched Whisper is not available ({e}); using the normal mode.", flush=True)
    if segments is None:
        try:
            segments, info = model.transcribe(audio, **options)
        except TypeError:
            # An older faster-whisper without the "multilingual" option: carry on without it.
            options.pop("multilingual", None)
            print("[2/3] This faster-whisper version has no 'multilingual' option; "
                  "run: pip install -U faster-whisper", flush=True)
            segments, info = model.transcribe(audio, **options)

    # Devanagari -> Roman letters (set ROMANIZE_DEVANAGARI=0 to keep Whisper's own script),
    # and near-miss spellings of the participants' names fixed to the form's spelling.
    romanize = (os.getenv("ROMANIZE_DEVANAGARI") or "1").strip() != "0"
    names = NameCorrector(participants)
    words = []
    count = 0
    for segment in segments:
        for w in segment.words or []:
            text = clean_word(w.word, names, romanize)
            words.append({"start": w.start, "end": w.end, "text": text})
        count += 1
        if count % 10 == 0:
            print(f"    ...transcribed {segment.end:.0f}s of {info.duration:.0f}s", flush=True)

    print(f"[2/3] Transcription done. Main language: {info.language} "
          f"(confidence {info.language_probability:.2f}).", flush=True)
    print(f"[time] Whisper: loading {load_seconds:.0f}s, processing "
          f"{time.perf_counter() - processing_started:.0f}s for {audio_seconds:.0f}s of audio", flush=True)
    return words


def identify_speaker_names(video_path: str, num_tiles: int = 3):
    """Runs the OCR step. Returns the active-speaker timeline, or [] on failure."""
    print("[OCR] Video track detected - looking for real speaker names...", flush=True)
    started = time.perf_counter()
    try:
        timeline = extract_name_timeline(video_path, num_tiles=num_tiles, sample_interval=float(os.getenv("OCR_SAMPLE_INTERVAL", "1.0")))
        print(f"[time] Video name reading: {time.perf_counter() - started:.0f}s", flush=True)
        return timeline
    except Exception as e:
        print(f"[OCR] Name detection failed, falling back to generic labels: {e}", flush=True)
        return []


_TIMELINE = {}
_RUN_STARTED = time.perf_counter()


def timed(name, fn, *args):
    """Runs fn(*args) and records when it started and ended (seconds since the run began)."""
    _TIMELINE[name] = [time.perf_counter() - _RUN_STARTED, None]
    try:
        return fn(*args)
    finally:
        _TIMELINE[name][1] = time.perf_counter() - _RUN_STARTED


def get_overlap(a_start, a_end, b_start, b_end):
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def assign_speaker(segment_start, segment_end, speaker_turns):
    if not speaker_turns:
        return "UNKNOWN"

    best_speaker = None
    best_overlap = 0.0
    for turn_start, turn_end, speaker in speaker_turns:
        overlap = get_overlap(segment_start, segment_end, turn_start, turn_end)
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = speaker
    if best_speaker is not None:
        return best_speaker

    # The word falls in a gap between diarized turns: use the closest turn.
    midpoint = (segment_start + segment_end) / 2
    nearest = min(
        speaker_turns,
        key=lambda t: max(t[0] - midpoint, midpoint - t[1], 0.0),
    )
    return nearest[2]


def merge_transcript_with_speakers(words, speaker_turns):
    merged = []
    for word in words:
        speaker = assign_speaker(word["start"], word["end"], speaker_turns)
        merged.append({
            "start": round(word["start"], 2),
            "end": round(word["end"], 2),
            "speaker": speaker,
            "text": word["text"].strip(),
        })
    return merged


def format_transcript(merged_segments, speaker_name_map=None):
    """
    Collapses consecutive same-speaker segments into readable lines.
    If speaker_name_map is provided, uses real names (e.g. 'Sushant')
    instead of generic labels (e.g. 'Speaker 00') wherever a confident
    name was found for that speaker.
    """
    speaker_name_map = speaker_name_map or {}
    lines = []
    current_speaker = None
    current_text = []

    for seg in merged_segments:
        raw_label = seg["speaker"]
        speaker_label = speaker_name_map.get(raw_label, raw_label.replace("SPEAKER_", "Speaker "))

        if speaker_label != current_speaker:
            if current_speaker is not None:
                lines.append(f"[{current_speaker}] {' '.join(current_text)}")
            current_speaker = speaker_label
            current_text = [seg["text"]]
        else:
            current_text.append(seg["text"])

    if current_speaker is not None:
        lines.append(f"[{current_speaker}] {' '.join(current_text)}")

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: python transcribe_diarize.py <audio_file> [whisper_model_size]")
        sys.exit(1)

    original_path = sys.argv[1]
    # Priority: WHISPER_MODEL in .env, then the command-line argument, then the default.
    model_size = os.getenv("WHISPER_MODEL") or (sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODEL)
    language = parse_language_setting(os.getenv("WHISPER_LANGUAGE"))

    hf_token = os.getenv("HF_TOKEN")
    if not hf_token:
        print("ERROR: Set your Hugging Face token as HF_TOKEN in a .env file "
              "or environment variable.")
        sys.exit(1)

    if not os.path.exists(original_path):
        print(f"ERROR: File not found: {original_path}")
        sys.exit(1)

    participants = participants_from_env()
    # The name reader assumes the people are shown in one row of equal-width tiles.
    num_speakers = len(participants) if 2 <= len(participants) <= 8 else None
    num_tiles = num_speakers if num_speakers and num_speakers <= 4 else 3
    print(f"[info] Participants: {participants or 'not given (speakers are auto-detected)'}", flush=True)

    overall_started = time.perf_counter()
    if torch.cuda.is_available():
        gpu = torch.cuda.get_device_properties(0)
        print(f"[info] GPU: {gpu.name}, {gpu.total_memory / 1024**3:.1f} GB memory", flush=True)
    else:
        print("[info] No GPU detected, running on the CPU.", flush=True)

    print("[0/3] Converting input to standard WAV format...", flush=True)
    conversion_started = time.perf_counter()
    audio_path = convert_to_wav(original_path)
    print(f"[time] Audio conversion: {time.perf_counter() - conversion_started:.0f}s", flush=True)

    try:
        has_video = has_video_track(original_path)
        if not has_video:
            print("[OCR] No video track detected - using generic speaker labels.", flush=True)

        # Diarization, transcription and OCR do not depend on each other. The OCR
        # (processor work) always runs alongside. The two GPU models only run at
        # the same time when the GPU is big enough for both.
        together = should_run_models_together()
        print(f"[info] Diarization and Whisper will run "
              f"{'at the same time' if together else 'one after the other'}.", flush=True)

        with ThreadPoolExecutor(max_workers=3) as pool:
            ocr_job = pool.submit(timed, "video names", identify_speaker_names, original_path, num_tiles) if has_video else None

            if together:
                diarization_job = pool.submit(timed, "speakers", diarize_audio, audio_path, hf_token, num_speakers)
                transcription_job = pool.submit(timed, "whisper", transcribe_full_audio, audio_path, model_size, language, participants)
                speaker_turns = diarization_job.result()
                all_words = transcription_job.result()
            else:
                speaker_turns = timed("speakers", diarize_audio, audio_path, hf_token, num_speakers)
                all_words = timed("whisper", transcribe_full_audio, audio_path, model_size, language, participants)

            name_timeline = ocr_job.result() if ocr_job else []

        print("[time] Timeline (seconds since start): " + " | ".join(
            f"{name} {start:.0f}-{end:.0f}" for name, (start, end) in _TIMELINE.items() if end is not None),
            flush=True)

        # DEBUG: show each diarized speaker's total turns and durations
        speaker_durations = defaultdict(float)
        speaker_turn_counts = defaultdict(int)
        for start, end, speaker in speaker_turns:
            speaker_durations[speaker] += (end - start)
            speaker_turn_counts[speaker] += 1
        print(f"[DEBUG] Speaker turn stats -> total seconds: {dict(speaker_durations)}, "
              f"turn counts: {dict(speaker_turn_counts)}", flush=True)

        speaker_name_map = {}
        if name_timeline:
            speaker_name_map = build_speaker_name_mapping(speaker_turns, name_timeline, debug=True)
            print(f"[OCR] Speaker name mapping: {speaker_name_map}", flush=True)

        all_words.sort(key=lambda w: w["start"])

        print("[3/3] Merging transcript with speaker labels...", flush=True)
        merged = merge_transcript_with_speakers(all_words, speaker_turns)
        final_text = format_transcript(merged, speaker_name_map)

        print("\n===== FINAL SPEAKER-LABELED TRANSCRIPT =====\n")
        print(final_text)

        base_name = os.path.splitext(os.path.basename(original_path))[0]
        txt_out = f"{base_name}_transcript.txt"
        json_out = f"{base_name}_transcript.json"

        with open(txt_out, "w", encoding="utf-8") as f:
            f.write(final_text)

        with open(json_out, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)

        print(f"\nSaved: {txt_out}")
        print(f"Saved: {json_out}")
        print(f"[time] Whole transcription step: {time.perf_counter() - overall_started:.0f}s", flush=True)
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)


if __name__ == "__main__":
    main()