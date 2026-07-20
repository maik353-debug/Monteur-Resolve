"""Publish to YouTube — upload the finished cut as a private draft (stdlib only).

The last mile of "media in, finished video out": after the Direct Export
(or a Resolve render) produced the MP4, one click sends it to the user's
OWN YouTube channel as a PRIVATE draft with the metadata prefilled, and
hands back the YouTube Studio link where the upload can be reviewed and
published. Monteur never publishes anything by itself.

The user owns the whole pipe
----------------------------
There is no Monteur cloud and no shared API project. The user brings
their own (free) Google Cloud project with a "Desktop app" OAuth client:
console.cloud.google.com -> new project -> enable "YouTube Data API v3"
-> OAuth consent screen (External, Testing, add yourself as a test user)
-> Credentials -> OAuth client ID -> Desktop app. A one-time ~10 minute
setup; the client id + secret live in ``~/.monteur/settings.json``
(:mod:`monteur.settings`, chmod 0600) like the Anthropic key does.

The private lock is a feature here
----------------------------------
Google requires an API audit before an OAuth project may set videos
public; unverified personal projects can upload but every video is
FORCED to private. That is exactly Monteur's draft workflow — the upload
lands as a private draft, the editor reviews it in YouTube Studio and
publishes it there. The UI says so honestly instead of pretending the
limitation does not exist. Quota reality (2026): ``videos.insert`` costs
1600 units of the default 10 000/day — about 6 uploads a day — and
YouTube additionally enforces a hidden per-channel daily upload cap that
surfaces as HTTP 429 after a few uploads. Both cases raise
:class:`QuotaExceeded` with one friendly message.

Protocol
--------
OAuth 2.0 for desktop apps (RFC 8252 loopback redirect): Monteur Studio's
own server IS the loopback target — ``monteur.web.server`` serves
``/api/youtube/callback`` on 127.0.0.1, and Google's desktop-app clients
accept any 127.0.0.1 port without pre-registration. Scope is exactly
``youtube.upload`` (upload + thumbnails, nothing else); ``access_type=
offline`` + ``prompt=consent`` so a refresh token always comes back. The
upload itself is Google's resumable protocol: one initiation POST
(``uploadType=resumable``) returns a session URL, then the file goes up
in ``Content-Range`` chunks (multiples of 256 KB); a 308 answer carries
the received range so an interrupted upload resumes instead of
restarting.

All HTTP goes through one injectable ``transport`` callable
``(url, data, headers, method) -> (status, headers, body)`` so the tests
never touch the network; the default is built on :mod:`urllib.request`.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

#: The one scope Monteur asks for: upload videos + set their thumbnails.
SCOPE = "https://www.googleapis.com/auth/youtube.upload"

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
UPLOAD_ENDPOINT = (
    "https://www.googleapis.com/upload/youtube/v3/videos"
    "?uploadType=resumable&part=snippet,status"
)
THUMBNAIL_ENDPOINT = (
    "https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId="
)

#: Resumable chunks must be multiples of 256 KB (Google's protocol rule).
CHUNK_MULTIPLE = 256 * 1024
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024

#: The friendly wording every quota/daily-cap failure carries — YouTube's
#: hidden per-channel daily cap (HTTP 429) and the API quota (403
#: quotaExceeded) look different on the wire but mean the same to an editor.
QUOTA_MESSAGE = "YouTube's daily upload limit reached — try again tomorrow."

# Retries for 5xx answers during the chunk loop (per chunk).
_MAX_RETRIES = 3

# Seam for tests: backoff sleeping goes through this module attribute.
_sleep = time.sleep


class MonteurYouTubeError(Exception):
    """A YouTube step failed; the message is user-ready."""


class TokenExpired(MonteurYouTubeError):
    """The access token was rejected (401) — refresh it and retry once."""


class QuotaExceeded(MonteurYouTubeError):
    """Daily upload quota/cap reached; carries :data:`QUOTA_MESSAGE`."""


# --- transport -----------------------------------------------------------


def _default_transport(url, data, headers, method):
    """The stdlib HTTP transport: ``(status, headers-dict, body-bytes)``.

    HTTP error statuses are RETURNED, not raised — the protocol handlers
    above decide what a 308/401/429 means. Only a transport-level failure
    (no network, DNS, TLS) raises, as a :class:`MonteurYouTubeError` with
    a plain-language message.
    """
    request = urllib.request.Request(
        url, data=data, headers=dict(headers or {}), method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, dict(exc.headers or {}), body
    except urllib.error.URLError as exc:
        raise MonteurYouTubeError(
            f"could not reach {urllib.parse.urlsplit(url).netloc}: "
            f"{exc.reason} — check your internet connection and try again"
        ) from exc


def _google_error(body: bytes, fallback: str) -> str:
    """A readable message out of Google's error JSON (both shapes)."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return fallback
    if not isinstance(data, dict):
        return fallback
    error = data.get("error")
    if isinstance(error, dict):  # API shape: {"error": {"message", "errors"}}
        message = str(error.get("message") or "").strip()
        return message or fallback
    if isinstance(error, str) and error:  # OAuth shape: {"error", "error_description"}
        description = str(data.get("error_description") or "").strip()
        return f"{error}: {description}" if description else error
    return fallback


def _is_quota_error(status: int, body: bytes) -> bool:
    """429 always; 403 only when Google's reason says quota/upload limit."""
    if status == 429:
        return True
    if status != 403:
        return False
    text = body.decode("utf-8", errors="replace")
    return any(
        marker in text
        for marker in ("quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded")
    )


# --- OAuth (desktop-app flow, RFC 8252 loopback) ---------------------------


def auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """The Google consent URL the browser opens to connect a channel.

    ``access_type=offline`` + ``prompt=consent`` force a refresh token in
    the exchange EVERY time (Google otherwise omits it on re-consent),
    so reconnecting always yields a fresh long-lived connection.
    """
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{AUTH_ENDPOINT}?{query}"


def _token_request(fields: dict, transport, failure: str) -> dict:
    data = urllib.parse.urlencode(fields).encode("ascii")
    status, _headers, body = (transport or _default_transport)(
        TOKEN_ENDPOINT,
        data,
        {"Content-Type": "application/x-www-form-urlencoded"},
        "POST",
    )
    if status != 200:
        raise MonteurYouTubeError(f"{failure}: {_google_error(body, f'HTTP {status}')}")
    try:
        tokens = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise MonteurYouTubeError(f"{failure}: unreadable token response") from exc
    if not isinstance(tokens, dict):
        raise MonteurYouTubeError(f"{failure}: unreadable token response")
    return tokens


def exchange_code(
    client_id: str, client_secret: str, code: str, redirect_uri: str, transport=None
) -> dict:
    """Trade the loopback ``code`` for tokens; returns Google's token dict
    (``access_token``, ``refresh_token``, ``expires_in``, ...)."""
    return _token_request(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        transport,
        "could not connect YouTube",
    )


def refresh_access_token(
    client_id: str, client_secret: str, refresh_token: str, transport=None
) -> str:
    """A fresh short-lived access token from the stored refresh token."""
    tokens = _token_request(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        transport,
        "your YouTube connection is no longer valid — reconnect in settings",
    )
    token = str(tokens.get("access_token") or "")
    if not token:
        raise MonteurYouTubeError(
            "your YouTube connection is no longer valid — reconnect in settings"
        )
    return token


# --- resumable upload ------------------------------------------------------


def _parse_range_end(headers: dict) -> int | None:
    """The last byte index Google confirmed, from a 308's Range header."""
    for name, value in (headers or {}).items():
        if str(name).lower() == "range":
            match = str(value).rsplit("-", 1)
            try:
                return int(match[1])
            except (IndexError, ValueError):
                return None
    return None


def upload_video(
    access_token: str,
    path: str,
    *,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    privacy: str = "private",
    progress=None,
    transport=None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> dict:
    """Resumable upload of ``path``; returns ``{"video_id", "channel"}``.

    Protocol handling by status code:

    * initiation 200 -> the session URL arrives in the ``Location`` header;
    * chunk 308 -> Google echoes the received range; the next chunk starts
      right after it (mid-chunk resumes included);
    * chunk 200/201 -> done, the body carries the video resource (id +
      ``snippet.channelTitle`` — the free channel hint, no extra API call);
    * 401 -> :class:`TokenExpired` (the caller refreshes once and retries);
    * 429 / 403-quota -> :class:`QuotaExceeded` with the friendly message;
    * 5xx -> the chunk is retried up to 3 times with backoff.

    ``progress(bytes_sent, total)`` fires after every confirmed chunk.
    """
    if chunk_size <= 0 or chunk_size % CHUNK_MULTIPLE:
        raise ValueError(
            f"chunk_size must be a positive multiple of {CHUNK_MULTIPLE} "
            f"bytes (256 KB), not {chunk_size}"
        )
    if privacy not in ("private", "unlisted"):
        raise ValueError(f"privacy must be 'private' or 'unlisted', not {privacy!r}")
    send = transport or _default_transport
    total = os.path.getsize(path)

    metadata = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": [str(t) for t in (tags or []) if str(t).strip()],
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    body = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
    status, headers, resp_body = send(
        UPLOAD_ENDPOINT,
        body,
        {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
            "X-Upload-Content-Length": str(total),
            "X-Upload-Content-Type": "video/*",
        },
        "POST",
    )
    if status == 401:
        raise TokenExpired("YouTube rejected the access token")
    if _is_quota_error(status, resp_body):
        raise QuotaExceeded(QUOTA_MESSAGE)
    if status != 200:
        raise MonteurYouTubeError(
            "could not start the YouTube upload: "
            + _google_error(resp_body, f"HTTP {status}")
        )
    session_url = next(
        (v for k, v in headers.items() if str(k).lower() == "location"), ""
    )
    if not session_url:
        raise MonteurYouTubeError(
            "could not start the YouTube upload: no upload session URL came back"
        )

    offset = 0
    retries = 0
    with open(path, "rb") as handle:
        while True:
            handle.seek(offset)
            chunk = handle.read(chunk_size)
            end = offset + len(chunk) - 1
            status, headers, resp_body = send(
                session_url,
                chunk,
                {
                    "Authorization": f"Bearer {access_token}",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{total}",
                },
                "PUT",
            )
            if status in (200, 201):
                if progress is not None:
                    progress(total, total)
                try:
                    video = json.loads(resp_body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    video = {}
                video_id = str(video.get("id") or "")
                if not video_id:
                    raise MonteurYouTubeError(
                        "the upload finished but YouTube returned no video id"
                    )
                snippet = video.get("snippet") or {}
                return {
                    "video_id": video_id,
                    "channel": str(snippet.get("channelTitle") or ""),
                }
            if status == 308:
                confirmed = _parse_range_end(headers)
                # No Range header on a 308 = nothing of this chunk arrived.
                advanced = confirmed + 1 if confirmed is not None else offset
                if advanced <= offset:
                    # The chunk brought no new bytes — treat it like a
                    # retryable hiccup so a stuck session cannot loop forever.
                    retries += 1
                    if retries > _MAX_RETRIES:
                        raise MonteurYouTubeError(
                            "the YouTube upload is not making progress — "
                            "try again"
                        )
                else:
                    offset = advanced
                    retries = 0
                if progress is not None:
                    progress(min(offset, total), total)
                continue
            if status == 401:
                raise TokenExpired("YouTube rejected the access token mid-upload")
            if _is_quota_error(status, resp_body):
                raise QuotaExceeded(QUOTA_MESSAGE)
            if 500 <= status < 600 and retries < _MAX_RETRIES:
                retries += 1
                _sleep(2 ** (retries - 1))
                continue
            raise MonteurYouTubeError(
                "the YouTube upload failed: "
                + _google_error(resp_body, f"HTTP {status}")
            )


def set_thumbnail(access_token: str, video_id: str, image_path: str, transport=None) -> str:
    """Best-effort custom thumbnail; returns ``""`` on success, else a note.

    NEVER raises — a failed thumbnail must not fail an upload that already
    succeeded (unverified channels also need phone verification before
    custom thumbnails are allowed at all; the note says what happened).
    """
    try:
        data = open(image_path, "rb").read()
        suffix = os.path.splitext(image_path)[1].lower()
        content_type = "image/png" if suffix == ".png" else "image/jpeg"
        status, _headers, body = (transport or _default_transport)(
            THUMBNAIL_ENDPOINT + urllib.parse.quote(video_id),
            data,
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": content_type,
            },
            "POST",
        )
        if status == 200:
            return ""
        return (
            "thumbnail not set: "
            + _google_error(body, f"HTTP {status}")
            + " — you can set it in YouTube Studio"
        )
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        return f"thumbnail not set: {exc} — you can set it in YouTube Studio"
