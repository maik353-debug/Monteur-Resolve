import pytest

from fable.model import (
    Clip,
    Timeline,
    format_timecode,
    parse_timecode,
)


class TestTimecode:
    def test_roundtrip_25fps(self):
        for tc in ["00:00:00:00", "01:02:03:04", "10:59:59:24"]:
            assert format_timecode(parse_timecode(tc, 25), 25) == tc

    def test_parse_basic(self):
        assert parse_timecode("00:00:01:00", 25) == 25
        assert parse_timecode("00:01:00:00", 24) == 24 * 60
        assert parse_timecode("01:00:00:00", 30) == 30 * 3600

    def test_out_of_range_frame(self):
        with pytest.raises(ValueError):
            parse_timecode("00:00:00:25", 25)

    def test_malformed(self):
        with pytest.raises(ValueError):
            parse_timecode("not a timecode", 25)

    def test_drop_frame_roundtrip(self):
        # Known drop-frame identity: 00:01:00;02 is the first frame after
        # the 1-minute drop at 29.97.
        # Frame numbers 0 and 1 are dropped at each minute, so ;02 follows
        # 00:00:59;29 directly: 1800 real frames have elapsed.
        frames = parse_timecode("00:01:00;02", 29.97)
        assert frames == 1800
        assert format_timecode(frames, 29.97) == "00:01:00;02"

    def test_drop_frame_ten_minute_boundary(self):
        # Every 10th minute does NOT drop: 00:10:00;00 is valid.
        frames = parse_timecode("00:10:00;00", 29.97)
        assert format_timecode(frames, 29.97) == "00:10:00;00"

    def test_fractional_ndf_rate(self):
        # 23.976 uses 24-frame numbering.
        assert parse_timecode("00:00:01:00", 23.976) == 24


class TestTimeline:
    def _timeline(self):
        return Timeline(
            name="t",
            fps=25,
            clips=[
                Clip(name="a", record_in=0, record_out=100),
                Clip(name="b", record_in=100, record_out=250),
                Clip(name="c", record_in=250, record_out=300),
                Clip(name="mx", track="A1", kind="audio", record_in=0, record_out=300),
            ],
        )

    def test_duration(self):
        assert self._timeline().duration == 300
        assert self._timeline().duration_seconds == 12.0

    def test_cuts(self):
        assert self._timeline().cuts() == [100, 250]

    def test_track_filters(self):
        t = self._timeline()
        assert [c.name for c in t.video_clips()] == ["a", "b", "c"]
        assert [c.name for c in t.audio_clips()] == ["mx"]
        assert set(t.tracks()) == {"V1", "A1"}

    def test_clip_overlap(self):
        a = Clip(name="a", record_in=0, record_out=100)
        b = Clip(name="b", record_in=50, record_out=150)
        c = Clip(name="c", record_in=100, record_out=200)
        assert a.overlaps(b)
        assert not a.overlaps(c)
