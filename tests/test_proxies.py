"""monteur.proxies — the playback-proxy pipeline behind Studio's players.

Real transcodes run against the generated demo footage (skipped cleanly
when it is missing); everything else — key discipline, cache pruning, the
env override, error paths — runs on scratch files.
"""

import os
import shutil
import time
from pathlib import Path

import pytest

from monteur.media import MonteurMediaError, probe

from _demo import DEMO

pytest.importorskip("numpy", reason="the [media] extra is not installed")

needs_demo = pytest.mark.skipif(
    not DEMO.is_dir(), reason="demo footage not generated in this environment"
)


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Every test gets its own proxy cache — never ~/.monteur/proxies."""
    monkeypatch.setenv("MONTEUR_PROXIES_PATH", str(tmp_path / "proxies"))


class TestPaths:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        from monteur import proxies

        monkeypatch.setenv("MONTEUR_PROXIES_PATH", str(tmp_path / "elsewhere"))
        assert proxies.proxies_dir() == tmp_path / "elsewhere"

    def test_default_is_under_home(self, monkeypatch):
        from monteur import proxies

        monkeypatch.delenv("MONTEUR_PROXIES_PATH", raising=False)
        assert proxies.proxies_dir() == Path.home() / ".monteur" / "proxies"

    def test_key_is_stable_for_an_unchanged_clip(self, tmp_path):
        from monteur import proxies

        clip = tmp_path / "a.mp4"
        clip.write_bytes(b"x" * 64)
        first = proxies.proxy_path(clip)
        assert first == proxies.proxy_path(clip)
        # the cache file is <hex>.mp4 inside the proxies dir
        assert first.parent == proxies.proxies_dir()
        assert first.suffix == ".mp4"
        assert len(first.stem) == 32 and all(
            c in "0123456789abcdef" for c in first.stem
        )

    def test_mtime_change_makes_a_new_key(self, tmp_path):
        from monteur import proxies

        clip = tmp_path / "a.mp4"
        clip.write_bytes(b"x" * 64)
        before = proxies.proxy_path(clip)
        os.utime(clip, ns=(1_000_000_000, 999_888_777_000_000_000))
        assert proxies.proxy_path(clip) != before

    def test_different_clips_get_different_keys(self, tmp_path):
        from monteur import proxies

        a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
        a.write_bytes(b"x")
        b.write_bytes(b"x")
        assert proxies.proxy_path(a) != proxies.proxy_path(b)

    def test_profile_is_part_of_the_key(self, tmp_path, monkeypatch):
        from monteur import proxies

        clip = tmp_path / "a.mp4"
        clip.write_bytes(b"x" * 64)
        before = proxies.proxy_path(clip)
        monkeypatch.setattr(proxies, "PROXY_PROFILE", "v999-test")
        assert proxies.proxy_path(clip) != before

    def test_missing_clip_is_a_media_error(self, tmp_path):
        from monteur import proxies

        with pytest.raises(MonteurMediaError):
            proxies.proxy_path(tmp_path / "nope.mp4")

    def test_fresh_proxy_is_soft(self, tmp_path):
        from monteur import proxies

        # missing clip -> None, never an error (the caller serves originals)
        assert proxies.fresh_proxy(tmp_path / "nope.mp4") is None
        clip = tmp_path / "a.mp4"
        clip.write_bytes(b"x" * 64)
        assert proxies.fresh_proxy(clip) is None  # no proxy transcoded yet
        # an EMPTY cache file is not fresh either
        target = proxies.proxy_path(clip)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")
        assert proxies.fresh_proxy(clip) is None
        target.write_bytes(b"stub")
        assert proxies.fresh_proxy(clip) == target


@needs_demo
class TestTranscode:
    def test_ensure_proxy_makes_a_real_playable_proxy(self):
        from monteur import proxies

        source = DEMO / "clip_A.mp4"
        proxy = proxies.ensure_proxy(source)
        assert proxy.is_file()
        assert proxy == proxies.proxy_path(source)
        # smaller than the original at the proxy profile's settings
        assert proxy.stat().st_size < source.stat().st_size
        info = probe(proxy)
        assert 0 < info.width <= 960  # 540p max, never upscaled
        assert info.has_audio  # the clip's own sound survives (AAC 96k)
        assert abs(info.duration - probe(source).duration) < 0.5
        # +faststart: the moov atom sits BEFORE mdat, so the browser can
        # start playing and byte-range seek before the download finishes
        head = proxy.read_bytes()
        assert head.index(b"moov") < head.index(b"mdat")

    def test_ensure_proxy_skips_when_fresh(self):
        from monteur import proxies

        source = DEMO / "clip_D.mp4"
        proxy = proxies.ensure_proxy(source)
        stamp = proxy.stat().st_mtime_ns
        again = proxies.ensure_proxy(source)
        assert again == proxy
        assert proxy.stat().st_mtime_ns == stamp  # no re-transcode

    def test_progress_fires_for_fresh_and_transcoded_alike(self):
        from monteur import proxies

        source = DEMO / "clip_D.mp4"
        calls = []
        proxies.ensure_proxy(source, progress=lambda *a: calls.append(a))
        proxies.ensure_proxy(source, progress=lambda *a: calls.append(a))
        assert calls == [(1, 1, "clip_D.mp4"), (1, 1, "clip_D.mp4")]

    def test_edited_clip_gets_a_new_proxy(self, tmp_path):
        from monteur import proxies

        copy = tmp_path / "edited.mp4"
        shutil.copyfile(DEMO / "clip_D.mp4", copy)
        first = proxies.ensure_proxy(copy)
        os.utime(copy, (1_000_000, 1_000_000))  # "the clip was replaced"
        second = proxies.ensure_proxy(copy)
        assert second != first
        assert second.is_file() and first.is_file()  # old one ages out via prune

    def test_ensure_proxies_batch_is_per_file_soft(self, tmp_path):
        from monteur import proxies

        broken = tmp_path / "broken.mp4"
        broken.write_text("this is not video data")
        paths = [DEMO / "clip_C.mp4", broken, DEMO / "clip_D.mp4"]
        seen = []
        made, errors = proxies.ensure_proxies(
            paths, progress=lambda done, total, name: seen.append((done, total, name))
        )
        assert set(Path(p).name for p in made) == {"clip_C.mp4", "clip_D.mp4"}
        assert list(errors) == [str(broken)]
        assert "ffmpeg" in errors[str(broken)]
        # sequential per-file progress, failures included
        assert seen == [
            (1, 3, "clip_C.mp4"), (2, 3, "broken.mp4"), (3, 3, "clip_D.mp4"),
        ]

    def test_ensure_proxies_honours_cancel(self):
        from monteur import proxies

        class Cancelled:
            def is_set(self):
                return True

        made, errors = proxies.ensure_proxies(
            [DEMO / "clip_A.mp4"], cancel=Cancelled()
        )
        assert made == {} and errors == {}


class TestErrors:
    def test_missing_file_raises(self, tmp_path):
        from monteur import proxies

        with pytest.raises(MonteurMediaError):
            proxies.ensure_proxy(tmp_path / "gone.mp4")

    def test_garbage_file_raises_with_ffmpeg_context(self, tmp_path):
        pytest.importorskip("imageio_ffmpeg")
        from monteur import proxies

        junk = tmp_path / "junk.mp4"
        junk.write_text("not a movie")
        with pytest.raises(MonteurMediaError) as exc_info:
            proxies.ensure_proxy(junk)
        assert "junk.mp4" in str(exc_info.value)
        # no half-written cache entry survives the failure
        assert proxies.fresh_proxy(junk) is None
        assert not list(proxies.proxies_dir().glob("*.part.mp4"))


class TestPrune:
    def _fill(self, proxies, sizes):
        """Fake proxy files with staggered mtimes (oldest first)."""
        directory = proxies.proxies_dir()
        directory.mkdir(parents=True, exist_ok=True)
        now = time.time()
        paths = []
        for index, size in enumerate(sizes):
            path = directory / f"{index:032x}.mp4"
            path.write_bytes(b"p" * size)
            os.utime(path, (now - 1000 + index, now - 1000 + index))
            paths.append(path)
        return paths

    def test_prune_removes_oldest_first_until_under_budget(self):
        from monteur import proxies

        paths = self._fill(proxies, [1000, 1000, 1000, 1000])
        budget_gb = 2500 / (1024 ** 3)  # room for two files + change
        removed = proxies.prune_proxies(max_gb=budget_gb)
        assert removed == paths[:2]  # oldest mtime first, exactly enough
        assert not paths[0].exists() and not paths[1].exists()
        assert paths[2].exists() and paths[3].exists()

    def test_prune_within_budget_removes_nothing(self):
        from monteur import proxies

        paths = self._fill(proxies, [10, 10])
        assert proxies.prune_proxies(max_gb=1) == []
        assert all(p.exists() for p in paths)

    def test_prune_without_a_cache_dir_is_quiet(self):
        from monteur import proxies

        assert not proxies.proxies_dir().exists()
        assert proxies.prune_proxies() == []


class TestCacheSizeAndClear:
    def _fill(self, proxies, sizes):
        directory = proxies.proxies_dir()
        directory.mkdir(parents=True, exist_ok=True)
        for index, size in enumerate(sizes):
            (directory / f"{index:032x}.mp4").write_bytes(b"p" * size)

    def test_cache_size_totals_bytes_and_count(self):
        from monteur import proxies

        self._fill(proxies, [100, 250, 50])
        info = proxies.cache_size()
        assert info == {"bytes": 400, "count": 3}

    def test_cache_size_without_dir_is_zero(self):
        from monteur import proxies

        assert proxies.cache_size() == {"bytes": 0, "count": 0}

    def test_cache_size_ignores_non_proxy_files(self):
        from monteur import proxies

        d = proxies.proxies_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "abc.mp4").write_bytes(b"pppp")
        (d / "notes.txt").write_bytes(b"ignore me")
        assert proxies.cache_size() == {"bytes": 4, "count": 1}

    def test_clear_removes_all_proxies(self):
        from monteur import proxies

        self._fill(proxies, [10, 20, 30])
        assert proxies.clear_proxies() == 3
        assert proxies.cache_size() == {"bytes": 0, "count": 0}

    def test_clear_without_dir_is_zero(self):
        from monteur import proxies

        assert proxies.clear_proxies() == 0


@needs_demo
class TestCli:
    def test_monteur_proxies_command(self, capsys):
        from monteur.cli import main

        main(["proxies", str(DEMO)])
        out = capsys.readouterr().out
        assert "Proxies ready for 4/4 clips" in out
        assert "[4/4]" in out

    def test_monteur_proxies_empty_folder_fails(self, tmp_path, capsys):
        from monteur.cli import main

        with pytest.raises(SystemExit):
            main(["proxies", str(tmp_path)])
