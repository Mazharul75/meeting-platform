"""The free, open-source transcription service. The real model is never loaded in tests -
only the glue (temp file handling, joining segments) is under test here."""
from __future__ import annotations

from types import SimpleNamespace

from app.services import transcription


def test_transcribe_audio_bytes_joins_segment_text(monkeypatch):
    seen_paths = []

    class FakeModel:
        def transcribe(self, path, beam_size, vad_filter):
            seen_paths.append(path)
            segments = [SimpleNamespace(text=" Hello there. "), SimpleNamespace(text="How are you? ")]
            return segments, SimpleNamespace(language="en")

    monkeypatch.setattr(transcription, "_model", lambda: FakeModel())
    text = transcription.transcribe_audio_bytes(b"fake webm bytes", ".webm")
    assert text == "Hello there. How are you?"
    assert seen_paths and seen_paths[0].endswith(".webm")
    import os

    assert not os.path.exists(seen_paths[0])  # the temp file is always cleaned up


def test_transcribe_audio_bytes_handles_no_speech(monkeypatch):
    class FakeModel:
        def transcribe(self, path, beam_size, vad_filter):
            return [], SimpleNamespace(language="en")

    monkeypatch.setattr(transcription, "_model", lambda: FakeModel())
    assert transcription.transcribe_audio_bytes(b"silence", ".mp4") == ""
