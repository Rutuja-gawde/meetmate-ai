"""
Debug tool: extracts frames at specific known timestamps (where we
manually confirmed who's speaking) and prints the raw border-vs-interior
brightness contrast for every tile, so we can pick the right threshold.
"""

import cv2
import numpy as np
from speaker_name_mapper import split_into_tiles, _border_brightness_contrast


def check_timestamps(video_path, timestamps_and_expected):
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)

    for ts, expected in timestamps_and_expected:
        frame_number = int(ts * fps)
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
        ret, frame = cap.read()
        if not ret:
            print(f"Could not read frame at {ts}s")
            continue

        tiles = split_into_tiles(frame, num_tiles=3)
        print(f"\n--- {ts}s (expected active: {expected}) ---")
        for i, tile in enumerate(tiles):
            contrast = _border_brightness_contrast(tile)
            print(f"  Tile {i}: contrast = {contrast:.2f}")

    cap.release()


if __name__ == "__main__":
    import sys
    video_path = sys.argv[1]
    # Corrected: these are SECONDS into the video (from the player's 00:00:24 / 00:00:33 display)
    check_timestamps(video_path, [
        (0, "Sushant (likely, start of video)"),
        (24, "Shruti"),
        (33, "Rutuja"),
    ])