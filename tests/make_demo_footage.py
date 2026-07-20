"""Generate the synthetic demo footage the end-to-end tests run against.

Usage::

    python tests/make_demo_footage.py            # writes tests/.demo-footage
    MONTEUR_DEMO_FOOTAGE=/elsewhere python tests/make_demo_footage.py

Produces four small video clips and one song:

* ``clip_A.mp4`` / ``clip_C.mp4`` — 8 s of bright, moving test pattern
  (fully usable footage with detectable moments)
* ``clip_B.mp4`` — 8 s whose first ~3.5 s are near-black, so the sift
  flags part of it as too dark (``usable_ratio < 1``)
* ``clip_D.mp4`` — 6 s, a different look so casting has variety
* ``song.wav`` — 60 s at 120 BPM: kick pattern with a quiet intro, a
  louder "chorus" and a drop, so beat/section/drop detection all have
  something real to find

Needs the ``[media]`` extra (numpy + bundled ffmpeg). The output folder
is gitignored; regenerate at will — content is deterministic apart from
encoder noise.
"""

from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _demo import DEMO  # noqa: E402


def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - fall back to a system ffmpeg
        return "ffmpeg"


def _clip(out: Path, seconds: float, extra_filter: str = "") -> None:
    """One bright test-pattern clip with motion and a quiet audio bed."""
    vf = "format=yuv420p" if not extra_filter else f"{extra_filter},format=yuv420p"
    cmd = [
        _ffmpeg(),
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=duration={seconds}:size=640x360:rate=25",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=330:duration={seconds}",
        "-filter:v",
        vf,
        "-filter:a",
        "volume=0.2",
        "-shortest",
        str(out),
    ]
    subprocess.run(cmd, check=True)


def _song(out: Path, seconds: float = 60.0, bpm: float = 120.0) -> None:
    import numpy as np

    rate = 44100
    n = int(seconds * rate)
    t = np.arange(n) / rate
    audio = np.zeros(n, dtype=np.float64)

    # Kicks on every beat: a short decaying 60 Hz thump plus a click.
    period = 60.0 / bpm
    beat = 0.0
    while beat < seconds:
        i = int(beat * rate)
        length = min(int(0.12 * rate), n - i)
        if length <= 0:
            break
        lt = np.arange(length) / rate
        audio[i : i + length] += np.sin(2 * np.pi * 60.0 * lt) * np.exp(-lt / 0.03)
        click = min(int(0.005 * rate), n - i)
        audio[i : i + click] += 0.6
        beat += period

    # A sustained pad so sections have body; louder in the "chorus".
    pad = 0.08 * np.sin(2 * np.pi * 220.0 * t)
    level = np.ones(n) * 0.45
    level[t < 8.0] = 0.2  # quiet intro
    level[(t >= 24.0) & (t < 40.0)] = 1.0  # chorus / drop territory
    audio = audio * level + pad * level

    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = audio / peak * 0.9
    pcm = (audio * 32767).astype("<i2")

    with wave.open(str(out), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(pcm.tobytes())


def main() -> None:
    DEMO.mkdir(parents=True, exist_ok=True)
    _clip(DEMO / "clip_A.mp4", 8.0)
    # First ~3.5 s near-black: the sift must flag part of B as too dark.
    _clip(DEMO / "clip_B.mp4", 8.0, "eq=brightness=-0.95:enable='lt(t,3.5)'")
    _clip(DEMO / "clip_C.mp4", 8.0, "hue=h=120")
    _clip(DEMO / "clip_D.mp4", 6.0, "negate")
    _song(DEMO / "song.wav")
    print(f"demo footage written to {DEMO}")


if __name__ == "__main__":
    main()
