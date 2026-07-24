"""Tests for monteur/waveform.py — the timeline's amplitude envelope.

The music lane used to draw only section energy (2 samples/s), which reads as
one block: at that resolution a beat is invisible. These pin the property that
matters — the envelope RESOLVES the beats — plus the cheap-and-safe contract
around it (per-file caching, windowing, normalisation, graceful emptiness).
"""

from __future__ import annotations

import math
import struct
import wave

import pytest

from monteur.media import MonteurMediaError
from monteur.waveform import ENVELOPE_RATE, MAX_BUCKETS, clear_cache, peaks

try:  # the decode needs numpy + ffmpeg, like every other media test
    import numpy  # noqa: F401

    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

needs_numpy = pytest.mark.skipif(not HAVE_NUMPY, reason="numpy not installed")


def click_track(path, seconds=4.0, bpm=120.0, rate=22050):
    """A wav with a sharp click on every beat and near-silence between."""
    beat = 60.0 / bpm
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = []
        for i in range(int(rate * seconds)):
            t = i / rate
            amp = 0.95 * math.exp(-(t % beat) * 40) + 0.02
            value = max(-1.0, min(1.0, amp * math.sin(2 * math.pi * 220 * t)))
            frames.append(struct.pack("<h", int(value * 32767)))
        w.writeframes(b"".join(frames))
    return path


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


@needs_numpy
def test_the_envelope_resolves_the_beats(tmp_path):
    # THE point of the feature: a beat has to be visible as a peak.
    song = click_track(tmp_path / "click.wav", seconds=4.0, bpm=120.0)
    values = peaks(song, duration=4.0, buckets=200)
    assert len(values) == 200
    assert max(values) == pytest.approx(1.0)  # normalised to the window
    # beats sit every 0.5 s -> every 25 buckets across 4 s
    on_beat = [values[i] for i in range(0, 200, 25)]
    off_beat = [values[i + 12] for i in range(0, 200 - 12, 25)]
    assert min(on_beat) > 0.5
    assert max(off_beat) < 0.2
    assert sum(on_beat) / len(on_beat) > 4 * (sum(off_beat) / len(off_beat))


@needs_numpy
def test_a_window_reads_only_its_own_span(tmp_path):
    # a song that is loud early and quiet late: the late window must be quiet
    # RELATIVE to itself (each window normalises to its own peak) but its raw
    # shape must differ from the early one
    rate = 22050
    song = tmp_path / "ramp.wav"
    with wave.open(str(song), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = []
        for i in range(int(rate * 4.0)):
            t = i / rate
            # first half: a steady tone. second half: sparse clicks with real
            # silence between them (a modulation faster than the bucket width
            # would be smoothed away by design — peaks are kept, shape is not).
            amp = 0.9 if t < 2.0 else 0.9 * math.exp(-((t - 2.0) % 0.5) * 40)
            frames.append(
                struct.pack("<h", int(amp * math.sin(2 * math.pi * 220 * t) * 32767))
            )
        w.writeframes(b"".join(frames))
    early = peaks(song, start=0.0, duration=2.0, buckets=64)
    late = peaks(song, start=2.0, duration=2.0, buckets=64)
    assert early != late
    # the steady early half is flat; the clicking late half is not
    assert max(early) - min(early) < 0.3
    assert max(late) - min(late) > 0.5


@needs_numpy
def test_buckets_are_honoured_and_capped(tmp_path):
    song = click_track(tmp_path / "click.wav", seconds=2.0)
    assert len(peaks(song, duration=2.0, buckets=37)) == 37
    assert len(peaks(song, duration=2.0, buckets=0)) == 1  # floor
    assert len(peaks(song, duration=2.0, buckets=99999)) == MAX_BUCKETS


@needs_numpy
def test_a_window_past_the_song_is_zeros_not_an_error(tmp_path):
    # a plan whose music was swapped for something shorter still draws a lane
    song = click_track(tmp_path / "click.wav", seconds=1.0)
    values = peaks(song, start=30.0, duration=5.0, buckets=50)
    assert values == [0.0] * 50


@needs_numpy
def test_the_decode_is_cached_per_file(tmp_path, monkeypatch):
    song = click_track(tmp_path / "click.wav", seconds=2.0)
    import monteur.waveform as wf

    calls = []
    real = wf.read_audio

    def counting(path, **kw):
        calls.append(path)
        return real(path, **kw)

    monkeypatch.setattr(wf, "read_audio", counting)
    peaks(song, duration=2.0, buckets=100)
    peaks(song, duration=2.0, buckets=800)   # a different zoom...
    peaks(song, start=0.5, duration=1.0, buckets=100)  # ...and a different window
    assert len(calls) == 1, "every window/zoom must resample the cached envelope"


@needs_numpy
def test_a_rewritten_song_is_picked_up(tmp_path):
    # the cache keys on mtime+size, so re-rendering a track is seen without a
    # restart — a stale waveform under a new song would be a lie
    song = tmp_path / "click.wav"
    click_track(song, seconds=2.0, bpm=120.0)
    first = peaks(song, duration=2.0, buckets=64)
    click_track(song, seconds=2.0, bpm=40.0)  # far fewer clicks
    assert peaks(song, duration=2.0, buckets=64) != first


def test_a_missing_file_raises_media_error(tmp_path):
    with pytest.raises(MonteurMediaError):
        peaks(tmp_path / "nope.wav", duration=1.0)


def test_envelope_rate_is_dense_enough_for_a_fast_beat():
    # 200 buckets/s means even a 200 bpm beat is ~60 buckets wide
    assert ENVELOPE_RATE / (200.0 / 60.0) > 50
