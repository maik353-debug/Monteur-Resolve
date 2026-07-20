"""Tests for monteur.youtube — OAuth desktop flow + resumable upload.

Everything runs against a scripted fake transport (the module's one HTTP
seam), so no test ever touches Google. The CLI section drives ``monteur
upload`` with the module's functions monkeypatched.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from monteur import youtube
from monteur.youtube import (
    CHUNK_MULTIPLE,
    QUOTA_MESSAGE,
    MonteurYouTubeError,
    QuotaExceeded,
    TokenExpired,
    auth_url,
    exchange_code,
    refresh_access_token,
    set_thumbnail,
    upload_video,
)


class FakeTransport:
    """Scripted (status, headers, body) responses; records every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []  # (url, data, headers, method)

    def __call__(self, url, data, headers, method):
        self.calls.append((url, data, dict(headers or {}), method))
        if not self.responses:
            pytest.fail(f"unexpected extra request: {method} {url}")
        return self.responses.pop(0)


def _token_body(**fields) -> bytes:
    return json.dumps(fields).encode()


# ------------------------------------------------------------ auth_url


def test_auth_url_contents():
    url = auth_url("my-client-id", "http://127.0.0.1:8765/api/youtube/callback", "st4te")
    parsed = urllib.parse.urlsplit(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == youtube.AUTH_ENDPOINT
    query = dict(urllib.parse.parse_qsl(parsed.query))
    assert query["client_id"] == "my-client-id"
    assert query["redirect_uri"] == "http://127.0.0.1:8765/api/youtube/callback"
    assert query["scope"] == "https://www.googleapis.com/auth/youtube.upload"
    assert query["access_type"] == "offline"
    assert query["prompt"] == "consent"  # forces a refresh token every time
    assert query["state"] == "st4te"
    assert query["response_type"] == "code"


# ------------------------------------------------------------ token exchange


def test_exchange_code_posts_the_grant_and_returns_tokens():
    transport = FakeTransport(
        [(200, {}, _token_body(access_token="at", refresh_token="rt", expires_in=3599))]
    )
    tokens = exchange_code("cid", "csecret", "the-code", "http://127.0.0.1:1/cb",
                           transport=transport)
    assert tokens["refresh_token"] == "rt"
    url, data, headers, method = transport.calls[0]
    assert url == youtube.TOKEN_ENDPOINT
    assert method == "POST"
    fields = dict(urllib.parse.parse_qsl(data.decode()))
    assert fields == {
        "client_id": "cid",
        "client_secret": "csecret",
        "code": "the-code",
        "redirect_uri": "http://127.0.0.1:1/cb",
        "grant_type": "authorization_code",
    }
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"


def test_exchange_code_surfaces_googles_error_readably():
    transport = FakeTransport(
        [(400, {}, _token_body(error="invalid_grant", error_description="Bad code"))]
    )
    with pytest.raises(MonteurYouTubeError) as exc_info:
        exchange_code("cid", "cs", "x", "http://127.0.0.1:1/cb", transport=transport)
    message = str(exc_info.value)
    assert "invalid_grant" in message and "Bad code" in message


def test_refresh_access_token_happy_path():
    transport = FakeTransport([(200, {}, _token_body(access_token="fresh-at"))])
    token = refresh_access_token("cid", "cs", "rt", transport=transport)
    assert token == "fresh-at"
    fields = dict(urllib.parse.parse_qsl(transport.calls[0][1].decode()))
    assert fields["grant_type"] == "refresh_token"
    assert fields["refresh_token"] == "rt"


def test_refresh_failure_says_reconnect():
    transport = FakeTransport([(400, {}, _token_body(error="invalid_grant"))])
    with pytest.raises(MonteurYouTubeError) as exc_info:
        refresh_access_token("cid", "cs", "stale", transport=transport)
    assert "reconnect in settings" in str(exc_info.value)


def test_refresh_without_access_token_in_body_fails():
    transport = FakeTransport([(200, {}, _token_body(token_type="Bearer"))])
    with pytest.raises(MonteurYouTubeError):
        refresh_access_token("cid", "cs", "rt", transport=transport)


# ------------------------------------------------------------ resumable upload


SESSION = "https://upload.example/session-123"


@pytest.fixture()
def video(tmp_path):
    """A fake video file: 600 000 bytes of recognizable content."""
    path = tmp_path / "video.mp4"
    path.write_bytes(bytes(range(256)) * 2343 + b"x" * 192)  # 600_000 bytes
    return path


def _done_body(video_id="vid123", channel="My Channel") -> bytes:
    return json.dumps({"id": video_id, "snippet": {"channelTitle": channel}}).encode()


def test_upload_happy_path_chunks_and_ranges(video):
    total = 600_000
    transport = FakeTransport(
        [
            (200, {"Location": SESSION}, b""),
            (308, {"Range": "bytes=0-262143"}, b""),
            (308, {"Range": "bytes=0-524287"}, b""),
            (200, {}, _done_body()),
        ]
    )
    seen = []
    result = upload_video(
        "tok", str(video), title="My cut", description="desc",
        tags=["travel", "alps"], progress=lambda sent, t: seen.append((sent, t)),
        transport=transport, chunk_size=CHUNK_MULTIPLE,
    )
    assert result == {"video_id": "vid123", "channel": "My Channel"}

    # Initiation: resumable POST with the metadata and the length announced.
    url, data, headers, method = transport.calls[0]
    assert url == youtube.UPLOAD_ENDPOINT and method == "POST"
    assert "uploadType=resumable" in url and "part=snippet,status" in url
    assert headers["Authorization"] == "Bearer tok"
    assert headers["X-Upload-Content-Length"] == str(total)
    metadata = json.loads(data.decode())
    assert metadata["snippet"]["title"] == "My cut"
    assert metadata["snippet"]["tags"] == ["travel", "alps"]
    assert metadata["status"]["privacyStatus"] == "private"

    # Chunks: PUTs against the session URL with exact Content-Range headers.
    chunk_calls = transport.calls[1:]
    assert all(c[0] == SESSION and c[3] == "PUT" for c in chunk_calls)
    ranges = [c[2]["Content-Range"] for c in chunk_calls]
    assert ranges == [
        f"bytes 0-262143/{total}",
        f"bytes 262144-524287/{total}",
        f"bytes 524288-599999/{total}",
    ]
    # The bytes themselves round-trip in order.
    assert b"".join(c[1] for c in chunk_calls) == video.read_bytes()
    # Byte progress after every confirmed chunk, ending at total/total.
    assert seen == [(262144, total), (524288, total), (total, total)]


def test_upload_resumes_mid_chunk_from_the_308_range(video):
    transport = FakeTransport(
        [
            (200, {"Location": SESSION}, b""),
            # Google only kept the first 100 000 bytes of the first chunk.
            (308, {"Range": "bytes=0-99999"}, b""),
            (308, {"Range": "bytes=0-524287"}, b""),
            (200, {}, _done_body()),
        ]
    )
    upload_video("tok", str(video), title="t", transport=transport,
                 chunk_size=CHUNK_MULTIPLE)
    second = transport.calls[2]
    # The next chunk starts exactly after the confirmed byte, not on the grid.
    assert second[2]["Content-Range"].startswith("bytes 100000-")
    assert len(second[1]) == CHUNK_MULTIPLE


def test_upload_401_on_initiate_is_token_expired(video):
    transport = FakeTransport([(401, {}, b"")])
    with pytest.raises(TokenExpired):
        upload_video("tok", str(video), title="t", transport=transport,
                     chunk_size=CHUNK_MULTIPLE)


def test_upload_401_mid_upload_is_token_expired(video):
    transport = FakeTransport(
        [
            (200, {"Location": SESSION}, b""),
            (308, {"Range": "bytes=0-262143"}, b""),
            (401, {}, b""),
        ]
    )
    with pytest.raises(TokenExpired):
        upload_video("tok", str(video), title="t", transport=transport,
                     chunk_size=CHUNK_MULTIPLE)


def test_upload_429_is_quota_with_the_friendly_message(video):
    transport = FakeTransport(
        [(200, {"Location": SESSION}, b""), (429, {}, b"slow down")]
    )
    with pytest.raises(QuotaExceeded) as exc_info:
        upload_video("tok", str(video), title="t", transport=transport,
                     chunk_size=CHUNK_MULTIPLE)
    assert str(exc_info.value) == QUOTA_MESSAGE
    assert "try again tomorrow" in str(exc_info.value)


def test_upload_403_quota_reason_is_quota_too(video):
    body = json.dumps(
        {"error": {"errors": [{"reason": "quotaExceeded"}], "message": "Quota"}}
    ).encode()
    transport = FakeTransport([(403, {}, body)])
    with pytest.raises(QuotaExceeded) as exc_info:
        upload_video("tok", str(video), title="t", transport=transport,
                     chunk_size=CHUNK_MULTIPLE)
    assert str(exc_info.value) == QUOTA_MESSAGE


def test_upload_plain_403_is_not_quota(video):
    body = json.dumps({"error": {"message": "forbidden for another reason"}}).encode()
    transport = FakeTransport([(403, {}, body)])
    with pytest.raises(MonteurYouTubeError) as exc_info:
        upload_video("tok", str(video), title="t", transport=transport,
                     chunk_size=CHUNK_MULTIPLE)
    assert not isinstance(exc_info.value, QuotaExceeded)
    assert "forbidden for another reason" in str(exc_info.value)


def test_upload_5xx_retries_the_chunk_with_backoff(video, monkeypatch):
    sleeps = []
    monkeypatch.setattr(youtube, "_sleep", sleeps.append)
    transport = FakeTransport(
        [
            (200, {"Location": SESSION}, b""),
            (500, {}, b""),
            (503, {}, b""),
            (308, {"Range": "bytes=0-262143"}, b""),
            (308, {"Range": "bytes=0-524287"}, b""),
            (200, {}, _done_body()),
        ]
    )
    result = upload_video("tok", str(video), title="t", transport=transport,
                          chunk_size=CHUNK_MULTIPLE)
    assert result["video_id"] == "vid123"
    assert sleeps == [1, 2]  # exponential backoff between retries
    # The failed chunk was resent identically both times.
    first_three = [c[2]["Content-Range"] for c in transport.calls[1:4]]
    assert first_three == ["bytes 0-262143/600000"] * 3


def test_upload_gives_up_after_three_5xx_retries(video, monkeypatch):
    monkeypatch.setattr(youtube, "_sleep", lambda s: None)
    transport = FakeTransport(
        [(200, {"Location": SESSION}, b"")] + [(500, {}, b"")] * 4
    )
    with pytest.raises(MonteurYouTubeError):
        upload_video("tok", str(video), title="t", transport=transport,
                     chunk_size=CHUNK_MULTIPLE)
    assert len(transport.calls) == 5  # initiate + first try + 3 retries


def test_upload_stalled_308_without_progress_gives_up(video, monkeypatch):
    monkeypatch.setattr(youtube, "_sleep", lambda s: None)
    transport = FakeTransport(
        [(200, {"Location": SESSION}, b"")] + [(308, {}, b"")] * 4
    )
    with pytest.raises(MonteurYouTubeError) as exc_info:
        upload_video("tok", str(video), title="t", transport=transport,
                     chunk_size=CHUNK_MULTIPLE)
    assert "not making progress" in str(exc_info.value)


def test_upload_missing_location_is_a_clear_error(video):
    transport = FakeTransport([(200, {}, b"")])
    with pytest.raises(MonteurYouTubeError) as exc_info:
        upload_video("tok", str(video), title="t", transport=transport,
                     chunk_size=CHUNK_MULTIPLE)
    assert "session URL" in str(exc_info.value)


def test_upload_chunk_size_must_be_a_256k_multiple(video):
    with pytest.raises(ValueError):
        upload_video("tok", str(video), title="t",
                     transport=FakeTransport([]), chunk_size=CHUNK_MULTIPLE + 1)
    with pytest.raises(ValueError):
        upload_video("tok", str(video), title="t",
                     transport=FakeTransport([]), chunk_size=0)


def test_upload_privacy_is_validated(video):
    with pytest.raises(ValueError):
        upload_video("tok", str(video), title="t", privacy="public",
                     transport=FakeTransport([]), chunk_size=CHUNK_MULTIPLE)


def test_upload_small_file_single_chunk(tmp_path):
    path = tmp_path / "tiny.mp4"
    path.write_bytes(b"m" * 1000)
    transport = FakeTransport(
        [(200, {"Location": SESSION}, b""), (201, {}, _done_body("tiny1", ""))]
    )
    result = upload_video("tok", str(path), title="t", transport=transport,
                          chunk_size=CHUNK_MULTIPLE)
    assert result == {"video_id": "tiny1", "channel": ""}
    assert transport.calls[1][2]["Content-Range"] == "bytes 0-999/1000"


# ------------------------------------------------------------ set_thumbnail


def test_set_thumbnail_success_returns_empty_note(tmp_path):
    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"\xff\xd8jpegdata")
    transport = FakeTransport([(200, {}, b"{}")])
    assert set_thumbnail("tok", "vid123", str(image), transport=transport) == ""
    url, data, headers, method = transport.calls[0]
    assert "thumbnails/set" in url and "vid123" in url
    assert method == "POST"
    assert data == b"\xff\xd8jpegdata"
    assert headers["Content-Type"] == "image/jpeg"
    assert headers["Authorization"] == "Bearer tok"


def test_set_thumbnail_failure_is_a_note_never_a_raise(tmp_path):
    image = tmp_path / "thumb.png"
    image.write_bytes(b"png")
    body = json.dumps({"error": {"message": "needs phone verification"}}).encode()
    transport = FakeTransport([(403, {}, body)])
    note = set_thumbnail("tok", "vid123", str(image), transport=transport)
    assert "thumbnail not set" in note
    assert "needs phone verification" in note
    assert "YouTube Studio" in note


def test_set_thumbnail_missing_file_is_a_note(tmp_path):
    note = set_thumbnail("tok", "vid123", str(tmp_path / "gone.jpg"),
                         transport=FakeTransport([]))
    assert note.startswith("thumbnail not set")


# ------------------------------------------------------------ the CLI


@pytest.fixture()
def connected_settings(tmp_path, monkeypatch):
    """Scratch settings with a full YouTube connection stored."""
    from monteur.settings import save_settings

    monkeypatch.setenv("MONTEUR_SETTINGS_PATH", str(tmp_path / "settings.json"))
    save_settings(
        {
            "youtube_client_id": "cid",
            "youtube_client_secret": "cs",
            "youtube_refresh_token": "rt",
        }
    )


@pytest.fixture()
def video_file(tmp_path):
    path = tmp_path / "final.mp4"
    path.write_bytes(b"video" * 100)
    return str(path)


def _run_upload(argv):
    from monteur.cli import main

    main(["upload"] + argv)


def test_cli_upload_happy_path(connected_settings, video_file, monkeypatch, capsys):
    calls = {"refresh": 0}

    def fake_refresh(client_id, client_secret, refresh_token, transport=None):
        calls["refresh"] += 1
        assert (client_id, client_secret, refresh_token) == ("cid", "cs", "rt")
        return "at"

    def fake_upload(token, path, *, title, description, tags, privacy, progress):
        assert token == "at" and path == video_file
        assert title == "My Alps Cut"
        assert tags == ["travel", "alps"]
        assert privacy == "private"
        progress(50, 100)
        progress(100, 100)
        return {"video_id": "vid42", "channel": "My Channel"}

    monkeypatch.setattr(youtube, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(youtube, "upload_video", fake_upload)
    _run_upload([video_file, "--title", "My Alps Cut", "--tags", "travel, alps"])
    out = capsys.readouterr().out
    assert calls["refresh"] == 1
    assert "Uploaded as a private draft on My Channel" in out
    assert "https://studio.youtube.com/video/vid42/edit" in out
    assert "https://www.youtube.com/watch?v=vid42" in out
    assert "Uploading…" in out  # the one-line byte progress


def test_cli_upload_reads_the_description_file(
    connected_settings, video_file, tmp_path, monkeypatch
):
    desc = tmp_path / "desc.txt"
    desc.write_text("The story.\n\n0:00 Start", encoding="utf-8")
    received = {}

    monkeypatch.setattr(youtube, "refresh_access_token", lambda *a, **k: "at")

    def fake_upload(token, path, *, title, description, tags, privacy, progress):
        received["description"] = description
        return {"video_id": "v", "channel": ""}

    monkeypatch.setattr(youtube, "upload_video", fake_upload)
    _run_upload([video_file, "--title", "T", "--description-file", str(desc)])
    assert received["description"] == "The story.\n\n0:00 Start"


def test_cli_upload_token_expired_refreshes_once_and_retries(
    connected_settings, video_file, monkeypatch, capsys
):
    calls = {"refresh": 0, "upload": 0}
    monkeypatch.setattr(
        youtube, "refresh_access_token",
        lambda *a, **k: [calls.__setitem__("refresh", calls["refresh"] + 1), "at"][1],
    )

    def fake_upload(token, path, **kwargs):
        calls["upload"] += 1
        if calls["upload"] == 1:
            raise TokenExpired("stale")
        return {"video_id": "v2", "channel": ""}

    monkeypatch.setattr(youtube, "upload_video", fake_upload)
    _run_upload([video_file, "--title", "T"])
    assert calls == {"refresh": 2, "upload": 2}
    assert "v2" in capsys.readouterr().out


def test_cli_upload_quota_exits_with_the_friendly_message(
    connected_settings, video_file, monkeypatch, capsys
):
    monkeypatch.setattr(youtube, "refresh_access_token", lambda *a, **k: "at")

    def fake_upload(*args, **kwargs):
        raise QuotaExceeded(QUOTA_MESSAGE)

    monkeypatch.setattr(youtube, "upload_video", fake_upload)
    with pytest.raises(SystemExit) as exc_info:
        _run_upload([video_file, "--title", "T"])
    assert exc_info.value.code == 1
    assert QUOTA_MESSAGE in capsys.readouterr().err


def test_cli_upload_not_connected_is_a_clean_failure(
    tmp_path, video_file, monkeypatch, capsys
):
    monkeypatch.setenv("MONTEUR_SETTINGS_PATH", str(tmp_path / "empty-settings.json"))
    with pytest.raises(SystemExit) as exc_info:
        _run_upload([video_file, "--title", "T"])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "not connected" in err and "Settings" in err


def test_cli_upload_missing_file_fails(connected_settings, tmp_path, capsys):
    with pytest.raises(SystemExit):
        _run_upload([str(tmp_path / "nope.mp4"), "--title", "T"])
    assert "no video file" in capsys.readouterr().err


def test_cli_upload_thumbnail_note_is_printed(
    connected_settings, video_file, tmp_path, monkeypatch, capsys
):
    thumb = tmp_path / "t.jpg"
    thumb.write_bytes(b"jpg")
    monkeypatch.setattr(youtube, "refresh_access_token", lambda *a, **k: "at")
    monkeypatch.setattr(
        youtube, "upload_video",
        lambda *a, **k: {"video_id": "v", "channel": ""},
    )
    monkeypatch.setattr(
        youtube, "set_thumbnail",
        lambda token, video_id, image_path, transport=None: (
            f"thumbnail not set: {image_path} rejected"
        ),
    )
    _run_upload([video_file, "--title", "T", "--thumbnail", str(thumb)])
    assert "thumbnail not set" in capsys.readouterr().out


def test_cli_upload_unlisted_wording(connected_settings, video_file, monkeypatch, capsys):
    monkeypatch.setattr(youtube, "refresh_access_token", lambda *a, **k: "at")
    monkeypatch.setattr(
        youtube, "upload_video", lambda *a, **k: {"video_id": "v", "channel": ""}
    )
    _run_upload([video_file, "--title", "T", "--privacy", "unlisted"])
    assert "Uploaded as unlisted" in capsys.readouterr().out


def test_cli_upload_parser_defaults():
    from monteur.cli import build_parser

    args = build_parser().parse_args(["upload", "v.mp4", "--title", "T"])
    assert args.video == "v.mp4"
    assert args.privacy == "private"
    assert args.tags == ""
    assert args.thumbnail == ""
    assert args.description_file == ""
