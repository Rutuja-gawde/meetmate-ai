"""
Proof-of-concept: for a 3-tile video call layout, detect which tile
has the green "active speaker" highlight border, and OCR that tile's
name label. Saves debug crops so we can visually verify regions.
"""

import cv2
import numpy as np
import pytesseract

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def split_into_tiles(frame, num_tiles=3):
    height, width = frame.shape[:2]
    tile_width = width // num_tiles
    tiles = []
    for i in range(num_tiles):
        x_start = i * tile_width
        x_end = (i + 1) * tile_width if i < num_tiles - 1 else width
        tiles.append(frame[:, x_start:x_end])
    return tiles


def is_green_highlighted(tile, border_thickness=15, inset_margin=8, green_pixel_threshold=0.15):
    h, w = tile.shape[:2]
    inset_tile = tile[inset_margin:h - inset_margin, inset_margin:w - inset_margin]
    ih, iw = inset_tile.shape[:2]

    top = inset_tile[0:border_thickness, :]
    bottom = inset_tile[ih - border_thickness:ih, :]
    left = inset_tile[:, 0:border_thickness]
    right = inset_tile[:, iw - border_thickness:iw]

    border = np.concatenate([
        top.reshape(-1, 3), bottom.reshape(-1, 3),
        left.reshape(-1, 3), right.reshape(-1, 3)
    ]).reshape(1, -1, 3).astype(np.uint8)

    hsv = cv2.cvtColor(border, cv2.COLOR_BGR2HSV)[0]

    green_mask = (
        (hsv[:, 0] >= 35) & (hsv[:, 0] <= 85) &
        (hsv[:, 1] >= 60) &
        (hsv[:, 2] >= 60)
    )
    green_fraction = green_mask.sum() / len(green_mask)

    is_active = green_fraction >= green_pixel_threshold
    return is_active, green_fraction


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
    best_img = candidates[0]
    for img in candidates:
        for psm in ["7", "6"]:
            text = pytesseract.image_to_string(img, config=f"--psm {psm}").strip()
            letters_only = "".join(c for c in text if c.isalpha() or c.isspace())
            if len(letters_only.strip()) > len(best_text):
                best_text = letters_only.strip()
                best_img = img

    return best_text, best_img


def analyze_frame(frame_path, num_tiles=3):
    frame = cv2.imread(frame_path)
    if frame is None:
        print(f"Could not read {frame_path}")
        return

    tiles = split_into_tiles(frame, num_tiles)
    print(f"\n=== Analyzing {frame_path} (frame size: {frame.shape[1]}x{frame.shape[0]}) ===")

    for i, tile in enumerate(tiles):
        is_active, avg_color = is_green_highlighted(tile)
        name, name_strip = ocr_tile_name(tile)
        marker = "  <-- ACTIVE SPEAKER" if is_active else ""

        cv2.imwrite(f"debug_tile_{i}_full.jpg", tile)
        cv2.imwrite(f"debug_tile_{i}_namestrip.jpg", name_strip)

        print(f"Tile {i}: name='{name}' | avg_border_color(R,G,B)={avg_color}{marker}")


if __name__ == "__main__":
    import sys
    frame_path = sys.argv[1] if len(sys.argv) > 1 else "frame_0_0.0s.jpg"
    analyze_frame(frame_path)