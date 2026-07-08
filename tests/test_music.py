"""Tests for monteur.music — beat/tempo detection and energy sections.

All waveforms are synthesized directly with numpy; only the optional
end-to-end test touches ffmpeg (skipped unless imageio_ffmpeg is available).
"""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest

from monteur.music import (
    MusicAnalysis,
    MusicSection,
    analyze_music,
    best_energy_window,
    detect_beats,
    detect_downbeats,
    detect_drops,
    detect_phrases,
    detect_sections,
)

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


def _click_track_with_thumps(
    bpm: float, duration: float, rate: int = RATE, seed: int = 0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Click track where every 4th click carries a 60 Hz decaying-sine thump.

    Returns (samples, click_times, bar_start_times).
    """
    samples, click_times = _click_track(bpm, duration, rate=rate, seed=seed)
    bar_starts = click_times[::4]
    thump_len = int(0.25 * rate)
    t = np.arange(thump_len) / rate
    thump = (np.sin(2 * np.pi * 60.0 * t) * np.exp(-t / 0.08)).astype(np.float32)
    for start_t in bar_starts:
        start = int(start_t * rate)
        end = min(start + thump_len, samples.size)
        samples[start:end] += 0.8 * thump[: end - start]
    peak = np.abs(samples).max()
    if peak > 0:
        samples = samples / peak
    return samples, click_times, bar_starts


def _quiet_then_loud(
    quiet_s: float = 20.0,
    loud_s: float = 20.0,
    quiet_amp: float = 0.15,
    loud_amp: float = 0.9,
    rate: int = RATE,
    seed: int = 5,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n_quiet = int(quiet_s * rate)
    n_loud = int(loud_s * rate)
    samples = rng.standard_normal(n_quiet + n_loud).astype(np.float32)
    samples[:n_quiet] *= quiet_amp
    samples[n_quiet:] *= loud_amp
    return samples


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

    def test_compressed_master_is_not_all_high(self):
        # A loud master with only ~15% verse/chorus level difference must
        # still yield quieter sections — labelling the whole song "high"
        # makes the montage cut on every beat from start to finish.
        rng = np.random.default_rng(5)
        duration = 120.0
        n = int(duration * RATE)
        samples = rng.standard_normal(n).astype(np.float32)
        t = np.arange(n) / RATE
        level = 0.85 + 0.15 * (np.sin(2 * np.pi * t / 30.0) > 0)
        samples *= level.astype(np.float32)

        sections = detect_sections(samples, RATE)
        _assert_tiles(sections, duration)
        high_time = sum(s.end - s.start for s in sections if s.label == "high")
        assert high_time < 0.6 * duration
        assert any(s.label != "high" for s in sections)

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


class TestDetectDownbeats:
    def test_thump_beats_become_downbeats(self):
        samples, _, bar_starts = _click_track_with_thumps(120.0, 60.0)
        _, beats = detect_beats(samples, RATE)
        downbeats = detect_downbeats(samples, RATE, beats)

        assert len(downbeats) >= 25  # ~30 bars in 60 s at 120 BPM
        # Spacing: 4 beats at 120 BPM = 2.0 s.
        assert np.median(np.diff(downbeats)) == pytest.approx(2.0, abs=0.05)
        # >= 80% of detected downbeats land within 40 ms of true bar starts.
        hits = sum(
            1
            for db in downbeats
            if abs(db - bar_starts[np.argmin(np.abs(bar_starts - db))]) < 0.04
        )
        assert hits / len(downbeats) >= 0.8

    def test_fewer_than_8_beats_returns_empty(self):
        samples, _, _ = _click_track_with_thumps(120.0, 60.0)
        beats = [0.5 * i for i in range(7)]
        assert detect_downbeats(samples, RATE, beats) == []

    def test_silence_returns_empty(self):
        silence = np.zeros(10 * RATE, dtype=np.float32)
        _, beats = detect_beats(silence, RATE)
        assert detect_downbeats(silence, RATE, beats) == []

    def test_short_input_returns_empty(self):
        short = np.random.default_rng(4).standard_normal(RATE // 2).astype(np.float32)
        _, beats = detect_beats(short, RATE)
        assert detect_downbeats(short, RATE, beats) == []


class TestDetectPhrases:
    def test_30_bars_gives_8_bar_phrases(self):
        downbeats = [2.0 * i for i in range(30)]
        phrases = detect_phrases(downbeats)
        assert phrases == [0.0, 16.0, 32.0, 48.0]
        assert np.diff(phrases) == pytest.approx(16.0)  # 8 bars * 2 s

    def test_8_bars_gives_4_bar_phrases(self):
        downbeats = [2.0 * i for i in range(8)]
        phrases = detect_phrases(downbeats)
        assert phrases == [0.0, 8.0]  # 4 bars * 2 s apart

    def test_first_phrase_starts_at_first_downbeat(self):
        downbeats = [1.5 + 2.0 * i for i in range(20)]
        phrases = detect_phrases(downbeats)
        assert phrases[0] == 1.5
        assert set(phrases) <= set(downbeats)

    def test_fewer_than_4_downbeats_returns_empty(self):
        assert detect_phrases([]) == []
        assert detect_phrases([0.0, 2.0, 4.0]) == []


class TestDetectDrops:
    def test_quiet_then_loud_has_one_drop_near_20s(self):
        samples = _quiet_then_loud(quiet_s=20.0, loud_s=20.0)
        drops = detect_drops(samples, RATE)
        assert len(drops) == 1
        assert abs(drops[0] - 20.0) <= 1.0

    def test_constant_loudness_has_no_drops(self):
        rng = np.random.default_rng(6)
        samples = 0.9 * rng.standard_normal(40 * RATE).astype(np.float32)
        assert detect_drops(samples, RATE) == []

    def test_drop_snaps_to_downbeat(self):
        samples = _quiet_then_loud(quiet_s=20.0, loud_s=20.0)
        downbeats = [2.0 * i for i in range(20)]  # one lands exactly at 20.0
        beats = [0.5 * i for i in range(80)]
        drops = detect_drops(samples, RATE, downbeats=downbeats, beats=beats)
        assert drops == [20.0]

    def test_snap_only_within_one_beat(self):
        samples = _quiet_then_loud(quiet_s=20.0, loud_s=20.0)
        downbeats = [3.0, 40.0]  # nearest downbeat is far from the drop
        beats = [0.5 * i for i in range(80)]
        unsnapped = detect_drops(samples, RATE)
        drops = detect_drops(samples, RATE, downbeats=downbeats, beats=beats)
        assert drops == unsnapped  # too far to snap: time unchanged

    def test_silence_and_short_input(self):
        silence = np.zeros(10 * RATE, dtype=np.float32)
        assert detect_drops(silence, RATE) == []
        short = np.random.default_rng(7).standard_normal(RATE // 2).astype(np.float32)
        assert detect_drops(short, RATE) == []
        assert detect_drops(np.zeros(0, dtype=np.float32), RATE) == []


class TestStructureGracefulDegradation:
    def test_all_three_empty_on_silence(self):
        silence = np.zeros(10 * RATE, dtype=np.float32)
        _, beats = detect_beats(silence, RATE)
        assert detect_downbeats(silence, RATE, beats) == []
        assert detect_phrases([]) == []
        assert detect_drops(silence, RATE) == []


class TestBestEnergyWindow:
    def test_prefers_late_energy_peak(self):
        music = MusicAnalysis(
            path="/m.wav",
            duration=60.0,
            tempo=120.0,
            sections=[
                MusicSection(0.0, 40.0, 0.2, "low"),
                MusicSection(40.0, 60.0, 0.95, "high"),
            ],
        )
        start = best_energy_window(music, 10.0)
        assert start >= 40.0  # window sits in the loud tail, not the intro
        assert start + 10.0 <= 60.0 + 1e-9

    def test_window_contains_first_drop_with_lead_in(self):
        music = MusicAnalysis(
            path="/m.wav",
            duration=80.0,
            tempo=120.0,
            sections=[MusicSection(0.0, 80.0, 0.5, "mid")],
            drops=[45.0],
        )
        start = best_energy_window(music, 20.0)
        assert start <= 45.0 <= start + 20.0  # the drop is inside the window
        assert start == pytest.approx(45.0 - 0.15 * 20.0)  # 15% lead-in = 42.0

    def test_length_at_or_above_duration_returns_zero(self):
        music = MusicAnalysis(
            path="/m.wav",
            duration=30.0,
            tempo=120.0,
            sections=[MusicSection(0.0, 30.0, 0.5, "mid")],
        )
        assert best_energy_window(music, 30.0) == 0.0
        assert best_energy_window(music, 45.0) == 0.0

    def test_uniform_energy_no_drop_starts_at_zero(self):
        music = MusicAnalysis(
            path="/m.wav",
            duration=30.0,
            tempo=120.0,
            sections=[MusicSection(0.0, 30.0, 0.5, "mid")],
        )
        assert best_energy_window(music, 10.0) == 0.0  # ties keep the earliest


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
