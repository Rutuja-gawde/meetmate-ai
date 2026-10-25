"""
Proof-of-concept: extract frames from a meeting video and OCR any
visible text, so we can see what a speaker's name tile looks like
to the OCR engine before building the full pipeline integration.
"""

import cv2
import pytesseract
import sys

# Update this path to match your Tesseract install location
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def extract_and_ocr_frames(video_path: str, interval_seconds: float = 2.0, max_frames: int = 15):
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    frame_interval = int(fps * interval_seconds)

    frame_count = 0
    saved_count = 0

    while cap.isOpened() and saved_count < max_frames:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_count % frame_interval == 0:
            timestamp = frame_count / fps
            text = pytesseract.image_to_string(frame)
            text_clean = text.strip().replace("\n", " | ")

            print(f"--- Frame at {timestamp:.1f}s ---")
            print(f"OCR text: {text_clean if text_clean else '(nothing detected)'}")
            print()

            # Save this frame as an image so we can visually inspect it
            cv2.imwrite(f"frame_{saved_count}_{timestamp:.1f}s.jpg", frame)
            saved_count += 1

        frame_count += 1

    cap.release()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python ocr_speaker_test.py <video_file>")
        sys.exit(1)

    extract_and_ocr_frames(sys.argv[1])
    