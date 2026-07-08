"""Tests for fable.music — beat/tempo detection and energy sections.

All waveforms are synthesized directly with numpy; only the optional
end-to-end test touches ffmpeg (skipped unless imageio_ffmpeg is available).
"""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest

from fable.music import MusicSection, analyze_music, detect_beats, detect_sections

RATE = 22050


def _click_track(
    bpm: float, duration: float, rate: int = RATE, seed: int = 0
) -> tuple[np.ndarray, np.ndarray]:
    """Clicks (short decaying noise bursts) every 60/bpm seconds.

    Returns (samples, click_times).
    """
    rng = np.random.default_rng(seed)
    samples = np.zeros(int(duration * rate), dtype=np.float32)
    interval = 60.0 / bpm
    click_len = int(0.03 * rate)  # 30 ms burst
    envelope = np.exp(-np.linspace(0.0, 8.0, click_len)).astype(np.float32)
    click_times = np.arange(0.0, duration - 0.05, interval)
    for t in click_times:
        start = int(t * rate)
        burst = rng.standard_normal(click_len).astype(np.float32) * envelope
        samples[start : start + click_len] += burst
    peak = np.abs(samples).max()
    if peak > 0:
        samples /= peak
    return samples, click_times


def _assert_tiles(sections: list[MusicSection], duration: float) -> None:
    assert sections, "expected at least one section"
    assert sections[0].start == 0.0
    assert sections[-1].end == pytest.approx(duration, abs=1e-6)
    for prev, nxt in zip(sections, sections[1:]):
        assert nxt.start == pytest.approx(prev.end, abs=1e-9)
        assert nxt.end > nxt.start


class TestDetectBeats:
    def test_120_bpm_click_track_tempo(self):
        samples, _ = _click_track(120.0, 30.0)
        tempo, beats = detect_beats(samples, RATE)
        # Octave preference must land on 120, not 60 or 240.
        assert abs(tempo - 120.0) <= 3.0
        assert len(beats) >= 55
        assert np.median(np.diff(beats)) == pytest.approx(0.5, abs=0.02)

    def test_120_bpm_beats_align_to_clicks(self):
        samples, clicks = _click_track(120.0, 30.0)
        _, beats = detect_beats(samples, RATE)
        assert beats
        deviations = [
            beat - clicks[np.argmin(np.abs(clicks - beat))] for beat in beats
        ]
        assert np.median(np.abs(deviations)) < 0.03

    def test_100_bpm_click_track_tempo(self):
        samples, _ = _click_track(100.0, 30.0, seed=7)
        tempo, beats = detect_beats(samples, RATE)
        assert abs(tempo - 100.0) <= 3.0
        assert beats

    def test_silence_returns_zero(self):
        silence = np.zeros(10 * RATE, dtype=np.float32)
        tempo, beats = detect_beats(silence, RATE)
        assert tempo == 0.0
        assert beats == []

    def test_too_short_input_returns_zero(self):
        short = np.random.default_rng(1).standard_normal(RATE // 2).astype(np.float32)
        tempo, beats = detect_beats(short, RATE)
        assert tempo == 0.0
        assert beats == []


class TestDetectSections:
    def test_quiet_then_loud(self):
        rng = np.random.default_rng(2)
        duration = 40.0
        n = int(duration * RATE)
        samples = rng.standard_normal(n).astype(np.float32)
        samples[: n // 2] *= 0.05  # quiet first half
        samples[n // 2 :] *= 0.9  # loud second half

        sections = detect_sections(samples, RATE)
        _assert_tiles(sections, duration)
        assert sections[0].label == "low"
        assert sections[-1].label == "high"
        assert sections[0].energy < sections[-1].energy

    def test_silence_is_graceful(self):
        silence = np.zeros(10 * RATE, dtype=np.float32)
        sections = detect_sections(silence, RATE)
        _assert_tiles(sections, 10.0)
        assert all(s.label == "low" for s in sections)

    def test_half_second_input_is_graceful(self):
        short = np.random.default_rng(3).standard_normal(RATE // 2).astype(np.float32)
        sections = detect_sections(short, RATE)
        _assert_tiles(sections, 0.5)

    def test_empty_input(self):
        assert detect_sections(np.zeros(0, dtype=np.float32), RATE) == []


@pytest.mark.skipif(
    importlib.util.find_spec("imageio_ffmpeg") is None,
    reason="imageio_ffmpeg not installed",
)
def test_analyze_music_end_to_end(tmp_path):
    import wave

    samples, _ = _click_track(120.0, 12.0)
    wav_path = tmp_path / "clicks.wav"
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(wav_path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(RATE)
        fh.writeframes(pcm.tobytes())

    analysis = analyze_music(str(wav_path))
    assert analysis.path == str(wav_path)
    assert analysis.duration == pytest.approx(12.0, abs=0.1)
    assert abs(analysis.tempo - 120.0) <= 3.0
    assert analysis.beats
    _assert_tiles(analysis.sections, analysis.duration)
