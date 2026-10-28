"""Cleans the optional "who is in this meeting" list typed on the upload form.

The names are passed to the transcription step, where they (a) hint Whisper how the names
are spelled, (b) tell the speaker detector how many people to expect and (c) are the only
names the video name-reader may assign. They come from a web form, so they are limited to
plain name characters before they go anywhere near a process or a prompt.
"""

import re

MAX_PARTICIPANTS = 8
MAX_NAME_LENGTH = 40
_NAME_CHARS = re.compile(r"[^\w .'\-]", re.UNICODE)


def clean_participants(raw: str | None) -> list[str]:
    names, seen = [], set()
    for part in re.split(r"[,\n;]", raw or ""):
        name = _NAME_CHARS.sub("", part)
        name = re.sub(r"\s+", " ", name).strip(" .'-")[:MAX_NAME_LENGTH].strip()
        if len(name) < 2 or name.lower() in seen:
            continue
        seen.add(name.lower())
        names.append(name)
        if len(names) >= MAX_PARTICIPANTS:
            break
    return names
