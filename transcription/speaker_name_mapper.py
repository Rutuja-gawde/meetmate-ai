"""
Builds a mapping from generic diarization speaker labels (SPEAKER_00,
SPEAKER_01, ...) to real names, by:
1. Sampling video frames at regular intervals
2. For each sample, finding the tile with the STRONGEST active-speaker
   highlight (border-vs-interior brightness contrast, not a fixed hue,
   since each participant's tile has a different background color) and
   OCR'ing that tile's name
3. Validating the OCR'd name against a known participant list via fuzzy
   matching, discarding anything that doesn't confidently match
4. Matching each diarization speaker turn against the name(s) seen active
   during that time range, picking the most frequent valid match

Speed: a tile's name does not change during a meeting, so once the same
name has been read twice from a tile, that name is reused instead of
running OCR again. It is re-checked every REVERIFY_EVERY samples in case
the layout changed. OCR also stops at the first confident match instead of
trying every image variant.
"""

import os

import cv2
import numpy as np
import pytesseract
from collections import Counter
from difflib import SequenceMatcher

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

def load_participant_names():
    """Names typed on the upload form arrive as MEETMATE_PARTICIPANTS ("A|B|C"). An empty value
    means the user gave none, so nobody is named and speakers keep generic labels. Only when the
    variable does not exist at all (running this script by hand) are the test names used."""
    raw = os.environ.get("MEETMATE_PARTICIPANTS")
    if raw is None:
        return ["Jarvis", "Friday", "Edith"]
    return [name.strip() for name in raw.split("|") if name.strip()]


KNOWN_NAMES = load_participant_names()

EARLY_EXIT_THRESHOLD = 0.75   # stop OCR variants as soon as one reads this well
REVERIFY_EVERY = 60           # re-read a cached tile name every N samples (tiles keep their names)
GIVE_UP_AFTER = 12            # failed reads of one tile before we only retry it occasionally
RETRY_EVERY = 5               # ...and then retry every N samples


def split_into_tiles(frame, num_tiles=3):
    height, width = frame.shape[:2]
    tile_width = width // num_tiles
    tiles = []
    for i in range(num_tiles):
        x_start = i * tile_width
        x_end = (i + 1) * tile_width if i < num_tiles - 1 else width
        tiles.append(frame[:, x_start:x_end])
    return tiles


def _border_brightness_contrast(tile, border_thickness=15, inset_margin=8):
    """
    Returns how much brighter the tile's border is compared to its own
    interior. The active-speaker indicator is a bright white/light glow
    layered on top of whatever color that person's tile normally is, so
    we compare brightness contrast rather than searching for one hue.
    """
    h, w = tile.shape[:2]
    inset_tile = tile[inset_margin:h - inset_margin, inset_margin:w - inset_margin]
    ih, iw = inset_tile.shape[:2]

    if ih <= 0 or iw <= 0:
        return 0.0

    top = inset_tile[0:border_thickness, :]
    bottom = inset_tile[ih - border_thickness:ih, :]
    left = inset_tile[:, 0:border_thickness]
    right = inset_tile[:, iw - border_thickness:iw]
    border_pixels = np.concatenate([
        top.reshape(-1, 3), bottom.reshape(-1, 3),
        left.reshape(-1, 3), right.reshape(-1, 3)
    ])

    interior_margin = border_thickness + inset_margin
    interior = tile[interior_margin:h - interior_margin, interior_margin:w - interior_margin]
    if interior.shape[0] <= 0 or interior.shape[1] <= 0:
        return 0.0
    interior_pixels = interior.reshape(-1, 3)

    border_brightness = cv2.cvtColor(
        border_pixels.reshape(1, -1, 3).astype(np.uint8), cv2.COLOR_BGR2HSV
    )[0][:, 2].mean()
    interior_brightness = cv2.cvtColor(
        interior_pixels.reshape(1, -1, 3).astype(np.uint8), cv2.COLOR_BGR2HSV
    )[0][:, 2].mean()

    return border_brightness - interior_brightness


def find_active_tile_index(tiles, min_margin=1.0):
    """
    Returns the index of whichever tile has the highest border-vs-interior
    brightness contrast, as long as it clearly stands out from the next
    highest by at least min_margin. No absolute floor is used, since each
    tile's own background color gives it a different baseline contrast
    range (verified: Shruti's blue tile peaks around -27 when active,
    while Rutuja's purple tile peaks near 0 - a fixed high threshold would
    incorrectly reject the weaker-signal tiles).
    """
    contrasts = [_border_brightness_contrast(tile) for tile in tiles]
    sorted_contrasts = sorted(contrasts, reverse=True)

    best_index = int(np.argmax(contrasts))
    top = sorted_contrasts[0]
    second = sorted_contrasts[1] if len(sorted_contrasts) > 1 else -999

    if (top - second) >= min_margin:
        return best_index
    return None


def clean_name(raw_name: str) -> str:
    """Strips leading numbers/noise (e.g. '23 RUTUJA GAWDE' -> 'Rutuja Gawde')."""
    words = raw_name.split()
    words = [w for w in words if not w.isdigit()]
    return " ".join(words).strip().title()


def match_known_name(raw_name: str, threshold: float = 0.6):
    """Fuzzy-matches cleaned OCR text against KNOWN_NAMES. Returns None if no confident match."""
    if not raw_name:
        return None
    best_match = None
    best_score = 0.0
    for known in KNOWN_NAMES:
        score = SequenceMatcher(None, raw_name.lower(), known.lower()).ratio()
        if score > best_score:
            best_score = score
            best_match = known
    return best_match if best_score >= threshold else None


def ocr_tile_name(tile, region_top_pct=0.75, region_bottom_pct=0.98):
    h, w = tile.shape[:2]
    strip = tile[int(h * region_top_pct):int(h * region_bottom_pct), :]
    gray = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)

    candidates = []
    _, binary1 = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if np.mean(binary1) < 127:
        binary1 = cv2.bitwise_not(binary1)
    candidates.append(binary1)

    _, binary2 = cv2.threshold(gray, 100, 255, cv2.THRESH_BINARY_INV)
    candidates.append(binary2)

    _, binary3 = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY_INV)
    candidates.append(binary3)

    best_text = ""
    for img in candidates:
        for psm in ["7", "6"]:
            text = pytesseract.image_to_string(img, config=f"--psm {psm}").strip()
            letters_only = "".join(c for c in text if c.isalpha() or c.isspace()).strip()

            # Stop early once a variant clearly reads as one of the known names.
            if match_known_name(clean_name(letters_only), EARLY_EXIT_THRESHOLD):
                return letters_only

            if len(letters_only) > len(best_text):
                best_text = letters_only

    return best_text


def extract_name_timeline(video_path: str, num_tiles: int = 3, sample_interval: float = 1.0):
    """
    Samples the video at sample_interval seconds. For each sample, finds
    the single strongest-highlighted tile, works out its name (OCR, or the
    cached name once confirmed), and validates against KNOWN_NAMES.
    Returns a list of (timestamp, name) for only the samples that passed.
    """
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_interval = max(1, int(fps * sample_interval))

    timeline = []
    frame_count = 0
    sampled_count = 0
    confirmed = {}   # tile index -> name confirmed by two matching OCR reads
    failures = {}    # tile index -> consecutive OCR reads that matched no known name
    last_read = {}   # tile index -> most recent OCR match
    ocr_calls = 0

    print(f"[OCR] Sampling video every {sample_interval}s for active speaker...", flush=True)

    while cap.isOpened():
        # grab() decodes a frame without converting it, which is much cheaper
        # for the frames we skip.
        if not cap.grab():
            break

        if frame_count % frame_interval == 0:
            ret, frame = cap.retrieve()
            if ret:
                sampled_count += 1
                timestamp = frame_count / fps
                tiles = split_into_tiles(frame, num_tiles)

                active_index = find_active_tile_index(tiles)
                if active_index is not None:
                    name = None
                    use_cache = (
                        active_index in confirmed
                        and sampled_count % REVERIFY_EVERY != 0
                    )
                    # A name belongs to exactly one tile: when every tile but one is known
                    # and the participant list has one name per tile, the last tile gets
                    # the remaining name without any OCR.
                    owners = {n: t for t, n in confirmed.items()}
                    if (active_index not in confirmed and len(confirmed) == num_tiles - 1
                            and len(KNOWN_NAMES) == num_tiles):
                        left = [n for n in KNOWN_NAMES if n not in owners]
                        if len(left) == 1:
                            confirmed[active_index] = left[0]
                            owners[left[0]] = active_index

                    skip_ocr = (
                        active_index not in confirmed
                        and failures.get(active_index, 0) >= GIVE_UP_AFTER
                        and sampled_count % RETRY_EVERY != 0
                    )
                    if use_cache and active_index in confirmed:
                        name = confirmed[active_index]
                    elif skip_ocr:
                        name = None
                    else:
                        ocr_calls += 1
                        raw_name = ocr_tile_name(tiles[active_index])
                        read = match_known_name(clean_name(raw_name))
                        # Another tile already owns this name, so this read must be wrong.
                        if read and owners.get(read, active_index) != active_index:
                            read = None
                        failures[active_index] = 0 if read else failures.get(active_index, 0) + 1
                        if read:
                            if last_read.get(active_index) == read:
                                confirmed[active_index] = read
                            else:
                                confirmed.pop(active_index, None)
                            last_read[active_index] = read
                            name = read
                        else:
                            # OCR failed this time: fall back to a confirmed name if we have one
                            name = confirmed.get(active_index)

                    if name:
                        timeline.append((timestamp, name))

        frame_count += 1

    cap.release()
    print(f"[OCR] Found {len(timeline)} valid active-speaker samples out of "
          f"{sampled_count} sampled frames ({ocr_calls} OCR reads).", flush=True)
    return timeline


def build_speaker_name_mapping(speaker_turns, name_timeline, debug=True, min_win_ratio=0.5):
    """
    Decides which real name belongs to which diarization speaker (SPEAKER_00, ...).

    Each speaker collects "votes": the names seen highlighted on screen while that
    speaker's turns were playing. Rules:
    - A name is given to AT MOST ONE speaker. (Before, two voices could both be called
      "Sushant", and the transcript then merged them into one person.)
    - The strongest speaker/name pair is settled first; the names it uses are removed from
      the other speakers' votes, so a weaker speaker is judged on what is left.
    - min_win_ratio: a name must hold at least this share of the speaker's remaining votes,
      otherwise that speaker keeps the generic "Speaker XX" label rather than risk a
      confidently wrong name.
    - Elimination: if exactly one speaker and exactly one known participant are left over,
      they are matched to each other.
    """
    tallies = {}
    for start, end, speaker in speaker_turns:
        for timestamp, name in name_timeline:
            if start <= timestamp <= end:
                tallies.setdefault(speaker, Counter())[name] += 1

    all_speakers = sorted({speaker for _, _, speaker in speaker_turns})

    if debug:
        print("\n[DEBUG] Speaker vote tallies (which names were seen active during each speaker's turns):")
        for speaker, votes in tallies.items():
            total = sum(votes.values())
            top_name, top_count = votes.most_common(1)[0]
            print(f"  {speaker}: {dict(votes)} -> top='{top_name}' ratio={top_count / total:.2f}")
        silent = [s for s in all_speakers if s not in tallies]
        if silent:
            print(f"  (no OCR votes at all for: {set(silent)})")

    mapping = {}
    used_names = set()
    while True:
        best = None
        for speaker, votes in tallies.items():
            if speaker in mapping:
                continue
            available = {n: c for n, c in votes.items() if n not in used_names}
            if not available:
                continue
            name, count = max(available.items(), key=lambda item: item[1])
            ratio = count / sum(available.values())
            if ratio < min_win_ratio:
                continue
            if best is None or (count, ratio) > best[0]:
                best = ((count, ratio), speaker, name)
        if best is None:
            break
        _, speaker, name = best
        mapping[speaker] = name
        used_names.add(name)

    leftover_speakers = [s for s in all_speakers if s not in mapping]
    leftover_names = [n for n in KNOWN_NAMES if n not in used_names]
    if len(leftover_speakers) == 1 and len(leftover_names) == 1:
        mapping[leftover_speakers[0]] = leftover_names[0]
        if debug:
            print(f"  (by elimination: {leftover_speakers[0]} -> {leftover_names[0]})")

    if debug:
        unmapped = [s for s in all_speakers if s not in mapping]
        if unmapped:
            print(f"  (left as generic labels: {unmapped})")

    return mapping


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python speaker_name_mapper.py <video_file>")
        sys.exit(1)

    timeline = extract_name_timeline(sys.argv[1])
    for ts, name in timeline:
        print(f"{ts:.1f}s -> {name}")