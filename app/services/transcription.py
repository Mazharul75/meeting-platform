"""Free meeting transcripts: open-source Whisper running on the server, no third-party API,
no per-minute billing. Transcribes the finished recording's parts (in order) instead of a
live, low-gain microphone stream, so normal speaking volume is picked up correctly.

The model is downloaded once (from Hugging Face, on first use) and cached on disk afterwards.
CPU inference is fine for a short meeting; a very long recording will simply take a while.
"""
from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

WHISPER_MODEL_SIZE = "base"


@lru_cache
def _model() -> "WhisperModel":
    from faster_whisper import WhisperModel

    return WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")


def transcribe_audio_bytes(data: bytes, suffix: str) -> str:
    """Transcribes one recording part (a self-contained webm/mp4 file) and returns its text."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        path = Path(f.name)
    try:
        segments, _info = _model().transcribe(str(path), beam_size=1, vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()
    finally:
        path.unlink(missing_ok=True)
