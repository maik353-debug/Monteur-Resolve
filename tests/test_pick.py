"""Tests for the song matcher (monteur.pick)."""

from __future__ import annotations

from pathlib import Path

import pytest

from monteur.music import MusicAnalysis, MusicSection
from monteur.pick import list_songs, rank_songs, rate_song
from monteur.sift import ClipReport, Moment

from _demo import DEMO


def _music(
    duration=60.0, tempo=120.0, beat_jitter=0.0, drops=(), dynamic=True, path="/m/a.wav"
):
    beats = []
    t = 0.0
    k = 0
    while t < duration:
        beats.append(t + (beat_jitter if k % 2 else 0.0))
        t += 60.0 / tempo
        k += 1
    if dynamic:
        sections = [
            MusicSection(0.0, duration / 3, 0.2, "low"),
            MusicSection(duration / 3, 2 * duration / 3, 0.5, "mid"),
            MusicSection(2 * duration / 3, duration, 0.9, "high"),
        ]
    else:
        sections = [MusicSection(0.0, duration, 0.6, "mid")]
    return MusicAnalysis(
        path=path, duration=duration, tempo=tempo, beats=beats,
        sections=sections, drops=list(drops),
    )


def _footage(motion=6.0, material=40.0):
    """One clip whose moments sum to ``material`` seconds at given motion."""
    n = max(int(material / 4), 1)
    moments = [
        Moment(
            i * 5.0, i * 5.0 + material / n, 0.8,
            entry_motion=(motion, 0.0), exit_motion=(motion, 0.0),
        )
        for i in range(n)
    ]
    return [ClipReport(path="/f/ride.mp4", duration=material * 2, moments=moments)]


def test_clear_pulse_beats_woolly_pulse():
    clean = rate_song(_music(beat_jitter=0.0), _footage())
    woolly = rate_song(_music(beat_jitter=0.06), _footage())
    assert clean.parts["clarity"] > woolly.parts["clarity"]
    assert clean.score > woolly.score


def test_length_fit_prefers_song_that_needs_no_repeats():
    # A perfect fit means the song fits INSIDE the unique material — the
    # planner never lets a cut outgrow it with repeats off.
    fits = rate_song(_music(duration=40.0), _footage(material=40.0))
    slightly_long = rate_song(_music(duration=50.0), _footage(material=40.0))
    long = rate_song(_music(duration=180.0), _footage(material=40.0))
    assert fits.parts["length"] == 1.0
    assert any("nothing has to repeat" in r for r in fits.reasons)
    assert 0.6 < slightly_long.parts["length"] < 1.0
    assert long.parts["length"] < 0.6
    assert any("shortened cut" in r and "repeat" in r for r in long.reasons)


def test_length_fit_against_target_duration():
    song = _music(duration=70.0)
    ok = rate_song(song, _footage(material=10.0), target_duration=60.0)
    too_short = rate_song(_music(duration=30.0), _footage(material=10.0),
                          target_duration=60.0)
    assert ok.parts["length"] == 1.0
    assert too_short.parts["length"] < 0.6


def test_tempo_fit_matches_footage_motion():
    fast_song, slow_song = _music(tempo=150.0), _music(tempo=92.0)
    fast_footage, calm_footage = _footage(motion=9.0), _footage(motion=0.5)
    assert (
        rate_song(fast_song, fast_footage).parts["tempo"]
        > rate_song(slow_song, fast_footage).parts["tempo"]
    )
    assert (
        rate_song(slow_song, calm_footage).parts["tempo"]
        > rate_song(fast_song, calm_footage).parts["tempo"]
    )


def test_tempo_fit_folds_octaves():
    # 170 BPM cut every 2 beats feels like 85: fine for calm footage.
    high = rate_song(_music(tempo=170.0), _footage(motion=0.5))
    assert high.parts["tempo"] > 0.7


def test_drop_and_arc_signals():
    with_drop = rate_song(_music(drops=[40.0]), _footage())
    without = rate_song(_music(), _footage())
    assert with_drop.parts["drop"] == 1.0 and without.parts["drop"] == 0.0
    flat = rate_song(_music(dynamic=False), _footage())
    assert flat.parts["arc"] < with_drop.parts["arc"]


def test_no_beats_is_scored_honestly():
    beatless = MusicAnalysis(path="/m/pad.wav", duration=60.0, tempo=0.0)
    rating = rate_song(beatless, _footage())
    assert rating.parts["clarity"] == 0.0
    assert any("hard to cut to" in r for r in rating.reasons)


@pytest.mark.skipif(not DEMO.is_dir(), reason="demo footage not generated")
def test_rank_songs_end_to_end(tmp_path):
    import shutil

    music_dir = tmp_path / "songs"
    music_dir.mkdir()
    shutil.copy(DEMO / "song.wav", music_dir / "candidate.wav")
    (music_dir / "broken.mp3").write_bytes(b"not audio")

    from monteur.sift import sift_directory

    reports = sift_directory(str(DEMO))
    seen = []
    ratings = rank_songs(reports, music_dir, progress=lambda i, n, name: seen.append(name))
    assert [Path(r.path).name for r in ratings][0] == "candidate.wav"
    assert ratings[0].score > 0
    broken = next(r for r in ratings if r.path.endswith("broken.mp3"))
    assert broken.score == 0.0 and "could not analyze" in broken.reasons[0]
    assert seen == ["broken.mp3", "candidate.wav"]  # sorted order, both visited


def test_list_songs_filters_audio(tmp_path):
    (tmp_path / "a.mp3").write_bytes(b"x")
    (tmp_path / "b.WAV").write_bytes(b"x")
    (tmp_path / "clip.mp4").write_bytes(b"x")
    assert [p.name for p in list_songs(tmp_path)] == ["a.mp3", "b.WAV"]
    assert list_songs(tmp_path / "missing") == []
