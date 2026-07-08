"""Beat-tracking QUALITY guards (monteur.music).

These tests synthesize songs whose beat times are known by construction and
hold the tracker to measured accuracy floors. They exist because the beat
grid is the heart of the product — every cut lands on it — and because each
scenario encodes a real-world failure mode that once broke the tracker:

* ``steady``      — alternating hi-hat accents made the 2-beat lag
                    autocorrelate stronger than the beat: the half-tempo trap.
* ``drift``       — live drummers and old recordings speed up; a fixed grid
                    walks off the pulse.
* ``syncopated``  — off-beat snares LOUDER than the beats pulled the greedy
                    tracker onto the off-beats: every cut exactly between
                    beats (F = 0.0 before the DP rebuild).
* ``weak_intro``  — pad-only intros gave the phase estimate nothing to hold
                    on to.
"""

from __future__ import annotations

import numpy as np
import pytest

from monteur.music import detect_beats, detect_downbeats

RATE = 22050


def _kick(length=0.10, freq=60.0):
    t = np.arange(int(length * RATE)) / RATE
    sweep = freq * np.exp(-t * 18)
    phase = 2 * np.pi * np.cumsum(sweep) / RATE
    return (np.sin(phase) * np.exp(-t * 30)).astype(np.float32)


def _snare(length=0.08):
    n = int(length * RATE)
    noise = np.random.default_rng(7).standard_normal(n).astype(np.float32)
    return noise * np.exp(-np.arange(n) / (0.02 * RATE)) * 0.7


def _hat(length=0.03):
    n = int(length * RATE)
    noise = np.random.default_rng(11).standard_normal(n).astype(np.float32)
    return noise * np.exp(-np.arange(n) / (0.008 * RATE)) * 0.35


def _pad(duration, freq=220.0):
    t = np.arange(int(duration * RATE)) / RATE
    return (0.15 * np.sin(2 * np.pi * freq * t)
            + 0.1 * np.sin(2 * np.pi * freq * 1.5 * t)).astype(np.float32)


def _place(track, sound, time, gain=1.0):
    i = int(time * RATE)
    j = min(i + sound.size, track.size)
    if i < track.size:
        track[i:j] += gain * sound[: j - i]


def make_track(kind, duration=30.0, bpm=120.0):
    """(samples, true_beat_times, true_downbeat_times) — beats known exactly."""
    track = _pad(duration)
    kick, snare, hat = _kick(), _snare(), _hat()

    beats = []
    if kind == "drift":
        t = 0.5
        while t < duration - 0.5:
            beats.append(t)
            t += 60.0 / (116 + 12 * (t / duration))  # 116 -> 128 BPM ramp
    else:
        t = 0.5
        while t < duration - 0.5:
            beats.append(t)
            t += 60.0 / bpm

    downbeats = beats[::4]
    for k, b in enumerate(beats):
        if kind == "weak_intro" and b < 8.0:
            continue
        _place(track, kick, b, gain=1.4 if k % 4 == 0 else 1.0)
        if kind == "syncopated":
            period = beats[k + 1] - b if k + 1 < len(beats) else 0.5
            _place(track, snare, b + period / 2, gain=1.6)  # LOUD off-beats
        if k % 2 == 1:
            _place(track, hat, b)

    rng = np.random.default_rng(3)
    track += 0.01 * rng.standard_normal(track.size).astype(np.float32)
    track /= max(np.abs(track).max(), 1e-6)
    if kind == "weak_intro":
        beats = [b for b in beats if b >= 8.0]
        downbeats = [d for d in downbeats if d >= 8.0]
    return track, beats, downbeats


def f_measure(detected, truth, tol=0.070):
    """(F, mean_abs_error_seconds) with one-to-one matching inside tol."""
    if not detected or not truth:
        return 0.0, float("inf")
    used = set()
    hits, errs = 0, []
    for t in truth:
        best, best_d = None, tol
        for i, d in enumerate(detected):
            if i not in used and abs(d - t) <= best_d:
                best, best_d = i, abs(d - t)
        if best is not None:
            used.add(best)
            hits += 1
            errs.append(best_d)
    if not hits:
        return 0.0, float("inf")
    precision, recall = hits / len(detected), hits / len(truth)
    return 2 * precision * recall / (precision + recall), float(np.mean(errs))


# Measured after the DP rebuild: 0.975/0.975/0.975/0.819 F, ~3 ms error.
# Floors leave margin for platform noise but catch any real regression.
_FLOORS = {
    "steady": (0.95, 0.90),  # (beat F, downbeat F)
    "drift": (0.95, 0.90),
    "syncopated": (0.95, 0.90),
    "weak_intro": (0.75, 0.75),
}


@pytest.mark.parametrize("kind", sorted(_FLOORS))
def test_beat_tracking_accuracy(kind):
    track, truth, truth_down = make_track(kind)
    tempo, beats = detect_beats(track, RATE)
    f, err = f_measure(beats, truth)
    beat_floor, down_floor = _FLOORS[kind]
    assert f >= beat_floor, f"{kind}: beat F {f:.3f} below {beat_floor}"
    assert err <= 0.006, f"{kind}: mean alignment error {err * 1000:.1f}ms > 6ms"
    assert 110 <= tempo <= 132, f"{kind}: tempo {tempo:.1f} off (octave error?)"

    downs = detect_downbeats(track, RATE, beats)
    fd, _ = f_measure(downs, truth_down)
    assert fd >= down_floor, f"{kind}: downbeat F {fd:.3f} below {down_floor}"


def test_syncopation_does_not_flip_the_phase():
    # The historical worst case: loud off-beat snares once pulled EVERY beat
    # onto the off-beat (F = 0.0) — cuts landed exactly between beats.
    track, truth, _ = make_track("syncopated")
    _, beats = detect_beats(track, RATE)
    period = 0.5
    offbeats = [b + period / 2 for b in truth]
    f_on, _ = f_measure(beats, truth)
    f_off, _ = f_measure(beats, offbeats)
    assert f_on > 3 * f_off, "the grid locked onto the off-beats"
