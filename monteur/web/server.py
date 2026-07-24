"""Monteur Studio — local web UI server.

A zero-dependency local server (stdlib only): serves the single-page app in
``app.html`` and a small JSON API on top of Monteur's analysis engine. Started
via ``monteur ui``. Binds to 127.0.0.1 — this is a local tool, not a network
service.

API (all JSON):

* ``POST /api/analyze``   {"filename", "content", "fps"?}      -> {"stats"}
* ``POST /api/compare``   {"a": <analyze payload>, "b": ...}   -> {"a", "b", "compare"}
* ``GET  /api/versions``                                        -> {"versions": [...]}
* ``POST /api/versions``  {analyze payload + "label"?}         -> {"version", "stats"}
* ``GET  /api/versions/<id>``                                   -> {"stats"}
* ``DELETE /api/versions/<id>``                                 -> {"ok": true}
* ``GET  /api/resolve/status``                                  -> {"connected", ...}
* ``POST /api/resolve/analyze`` {"timeline"?, "save"?}          -> {"stats", "version"?}
* ``POST /api/create/scan``   {"folder", "see"?}                -> {"job": id}
* ``POST /api/create/build``  {"folder", "music"?, "see"?,
  "ai_cut"?, "brief"?, "elements"?, "platform"?,
  "music_window"?: [in, out], ...}                              -> {"job": id}
* ``POST /api/create/pick``   {"folder", "music_dir",
  "max_duration"?}                                              -> {"job": id}
* ``POST /api/create/kit``    {build payload + "kit_dir"}       -> {"job": id}
* ``POST /api/create/revise`` {"plan_json", "folder", "brief",
  "pins"?, "fps"?, "audio"?, "canvas"?, "format"?,
  "elements"?}                                                  -> {"job": id}
* ``POST /api/create/direct`` {"plan_json", "folder" |
  "folders": [..], "music"?, "notes"?}                          -> {"job": id}
* ``POST /api/create/direct/apply`` {"plan_json", "folder",
  "review", "fps"?, "audio"?, "canvas"?, "format"?}             -> {"job": id}
* ``POST /api/create/distill`` {"timeline": {"filename",
  "content", "fps"?}, "music"?, "target"?, "style"?,
  "canvas"?, "audio"?, "format"?}                               -> {"job": id}
* ``POST /api/create/export`` {"plan_json", "fps"?, "audio"?,
  "canvas"?, "format"?}                                         -> build-result shape
* ``GET  /api/clipinfo?clip=&folder=&t0=&t1=``                   -> probe facts + vision
* ``POST /api/alternatives`` {"plan_json", "folder", "slot"}    -> {"slot", "alternatives"}
* ``POST /api/plan/adjust`` {"plan_json", + one of:
  {"slot", "transition"} | {"dip", "title"} | {"delete": slot} |
  {"move": slot, "to": index}; "fps"?, "audio"?, "canvas"?,
  "format"?}                                                     -> build-result shape
* ``GET  /api/thumb?clip=&t=&w=``                                -> JPEG bytes
* ``GET  /api/media?path=<abs>``                                 -> media bytes (Range-capable;
  serves the playback proxy when fresh, else the original file)
* ``POST /api/proxies`` {"folder"}                              -> {"job": id}
* ``POST /api/create/preview`` {"plan_json", "audio"?,
  "width"?}                                                     -> {"job": id}
* ``GET  /api/preview/<token>.mp4``                              -> MP4 bytes (Range-capable)
* ``POST /api/create/export-video`` {"plan_json", "target_dir",
  "name"?, "canvas"?, "audio"?, "quality"?, "fps"?}             -> {"job": id}
* ``GET  /api/drafts``                                          -> {"drafts": [...]}
* ``GET  /api/drafts/<id>``                                     -> the full draft
* ``POST /api/drafts``    the draft record                      -> the stored record
* ``DELETE /api/drafts/<id>``                                   -> {"deleted": bool}
* ``GET  /api/projects``                                        -> {"projects": [...]}
* ``POST /api/projects`` {"name", "options"?, "media"?,
    "plan"?, "notes"?}                                          -> the manifest
* ``GET  /api/projects/<id>``                                   -> the full manifest
* ``POST /api/projects/<id>`` manifest fields to update         -> the manifest
* ``DELETE /api/projects/<id>``                                 -> {"deleted": bool}
* ``GET  /api/projects/<id>/pool``                              -> {"clips": [...],
    "entries": [...]}  (the media pool resolved to clips + cached status)
* ``POST /api/projects/<id>/pool`` {"add":{path,kind}} |
    {"remove": path}                                            -> the resolved pool
* ``POST /api/projects/<id>/analyze`` {"clips":[abs paths],
    "see"?}                                                     -> {"job": id}
    (sift ONLY the selected clips; optional vision when see=true)
* ``POST /api/projects/<id>/see`` {"clips":[abs paths]}         -> {"job": id}
    (Claude-vision ONLY the selected clips — the staged "check" step)
* ``POST /api/projects/<id>/series`` {"series":N, "canvas"?}    -> {"job": id}
    (long form -> N vertical Shorts, extracted from the edit's beats)
* ``POST /api/projects/<id>/series/save``
    {"shorts":[{plan_json,label?}]}          -> {"created":[{id,name}]}
    (persist chosen Shorts as child projects on the same footage)
* ``POST /api/create/resolve`` {"plan_json", "fps"?, "name"?,
  "canvas"?, "audio"?}                                          -> {"job": id}
* ``POST /api/resolve/render`` {"timeline"?, "target_dir",
  "name"?, "preset"?}                                           -> {"job": id}
* ``POST /api/find``      {"folder", "query", "limit"?}         -> {"shots"} | {"error"}
* ``POST /api/coverage``  {"folder", "style"?, "brief"?,
  "target"?}                                                    -> {"job": id}
* ``POST /api/movie/load``    {"project_dir"}                   -> {"project", "progress"}
* ``POST /api/movie/new``     {"project_dir", "brief",
  "genre"?}                                                     -> {"job": id}
* ``POST /api/movie/assign``  {"project_dir", "scene",
  "folder"}                                                     -> {"project", "progress"}
* ``POST /api/movie/check``   {"project_dir", "scene", "see"?}  -> {"job": id}
* ``POST /api/movie/assemble`` {"project_dir", "fps"?,
  "canvas"?, "format"?}                                         -> {"job": id}
* ``GET  /api/jobs/<id>``                                       -> the job dict
* ``POST /api/jobs/<id>/cancel``                                -> {"ok": true}
* ``POST /api/pick``          {"kind": "folder"|"music"|"file"} -> {"path"} | {"error"}
* ``GET  /api/browse/list``   ?path=<dir>  -> {"path", "parent",
  "folders": [...], "files": [...]}  (the Explorer's directory listing)
* ``GET  /api/settings``                                        -> the settings view
* ``POST /api/settings``      {"backend"?, "api_key"?,
  "resolve_python"?}                                            -> the view after applying
* ``POST /api/settings/test``                                   -> {"job": id}
* ``GET  /api/resolve/diagnose``                                -> monteur.resolve.diagnose()
* ``POST /api/resolve/detect``                                  -> {"job": id}
* ``GET  /api/youtube/status``                                  -> {"configured", "connected", "channel"}
* ``POST /api/youtube/credentials`` {"client_id",
  "client_secret"}                                              -> the status view
* ``POST /api/youtube/connect``                                 -> {"auth_url", "redirect_uri"}
* ``GET  /api/youtube/callback?code&state``                      -> a tiny HTML page
* ``POST /api/youtube/disconnect``                              -> the status view
* ``POST /api/youtube/upload`` {"path", "title",
  "description"?, "tags"?, "privacy"?, "thumbnail"?}            -> {"job": id}
* ``POST /api/youtube/prefill`` {"plan_json", "name"?,
  "canvas"?}                                                    -> {"title", "description", "tags"}

Timeline content is passed as text (EDL/FCPXML are text formats); ``fps`` is
required for EDL files.

Scans, builds, picks and kits are cancellable BACKGROUND JOBS: the POST
returns a job id immediately, a daemon thread does the slow sifting/planning,
and the browser polls ``GET /api/jobs/<id>`` for live per-clip progress. A
successful scan is cached (folder + per-file mtimes), so a build/pick/kit
straight after a scan reuses the reports instead of sifting the same footage
twice.

``/api/create/pick`` ranks every song in ``music_dir`` against the sifted
footage (:mod:`monteur.pick`); per-song progress arrives as ``"song"``
entries. ``/api/create/kit`` builds the same montage plan a build would
(identical payload) and then writes a publish kit (:mod:`monteur.publish`)
into ``kit_dir`` — publish.md plus thumbnail JPEGs, returned inline as
base64 so the browser can show them.

``"see": true`` on scan/build asks Claude vision (:mod:`monteur.vision`) to
label the good moments after the sift. Vision is an upgrade, not a gate: a
missing anthropic package or API key (``MonteurVisionError``) never fails the
job — the result simply carries ``"vision_error"`` instead of annotations.

``"ai_cut": true`` on build/kit routes the planning through
:func:`monteur.compose.compose_montage` — Claude composes the cut: the
engine still builds the exact beat grid, dips and durations a plain build
would, then ONE Claude completion casts every slot, writes the act titles
for the black dips and a story arc into the plan notes ("story: ...",
"act 1: ..."). ``"brief"`` (the wizard's "What is this video?" text) is
handed to the composer as the editor's brief. Text-only, so it runs over
the user's Claude connection (Claude Code = no extra API cost) and is
sharpest after a "Let Claude watch" scan annotated the moments. Unlike
vision this IS a gate: the user explicitly asked for the AI cut, so a
``MonteurAIError`` fails the job with its own actionable message instead
of silently downgrading to the heuristic cut (the CLI's ``--ai-cut``
keeps the graceful fallback with a printed note). Both keys ride into the
draft autosave settings like every other wizard control.

``"elements"`` on build/kit/revise is the user's own sound library
(:mod:`monteur.elements`): the folder's snippets are classified OFFLINE
(impact / whoosh / riser / braam, cached next to the folder) under an
``"elements"`` progress stage and placed into the plan's SFX layer as
REAL audio clips — riser ending on the drop, impact on the drop and the
smash cuts — on their own audio track (A2, or A3 in "mix"). Sending
``"elements"`` with an explicit ``"sfx": false`` is a job error (the
cues ARE the places the elements go); without an ``"sfx"`` key the SFX
layer is enabled automatically. On revise, files carry over to
untouched cues (same kind + time) and the folder — when sent again —
re-places the rest.

``/api/create/revise`` is the Studio's revision loop
(:mod:`monteur.revise`): the build result carries the full plan as
``"plan_json"``; the browser sends it back with a one-sentence instruction
("zweite Hälfte ruhiger") and optional ``"pins"`` (record-time stamps of
shots that must stay). The job re-sifts the folder through the same scan
cache, re-analyzes the plan's own music, and re-plans exactly like ``monteur
revise`` does (style recovered from the plan's notes, length from the plan
itself, ``allow_repeats=True``, SFX from whether cues were planned). The
result looks like a build result PLUS the revised ``"plan_json"`` (so
revisions chain) and the parser's ``"rationale"`` line.

``/api/create/direct`` is the Studio's Director's-notes button
(:mod:`monteur.director`): the browser sends the current ``"plan_json"``
plus optional ``"notes"`` context, the job re-sifts the folder — or every
folder in a ``"folders"`` list; the Movie card sends its assigned scene
folders and the SCREENPLAY as the notes context — through the same scan
cache, re-analyzes the plan's own music (or the payload's ``"music"``
override) for the dossier, and asks Claude for a structured review of
the cut against editing craft. Text-only — it runs over the
user's Claude connection (Claude Code = no extra API cost) and is
sharpest when a "Let Claude watch" scan annotated the footage. Unlike
vision this IS a gate: a ``MonteurAIError`` fails the job with its own
actionable message. The result is ``{"review", "plan_json" (unchanged),
"applied": false}``. ``/api/create/direct/apply`` then applies the
review's replacement suggestions (``monteur.director.apply_review`` —
pure plan surgery, no AI, record grid untouched) and returns the
standard build-result shape (fresh timeline file + the improved
``"plan_json"`` + the applied notes), so the Studio's result card
replaces in place exactly like a revision does.

``/api/coverage`` is the Studio's pre-cut shot list — "what's still
missing?" (:mod:`monteur.coverage`): the browser sends the footage
``"folder"`` plus optional ``"style"`` (the step-2 selection), ``"brief"``
(the wizard's "What should this video become?" text) and ``"target"``
(seconds). A ``"coverage"`` job sifts through the same scan cache as
build/pick/kit (a cache hit after a see-scan carries the vision
annotations — exactly what makes the shot list sharp) and asks
:func:`monteur.coverage.missing_shots` for the gap list. Text-only, so
it runs over the user's Claude connection (Claude Code = no extra API
cost). Like Director's Notes this IS a gate: the user explicitly asked
for the coverage check, so a ``MonteurAIError`` fails the job with its
own actionable message. The result is ``{"coverage": {verdict,
coverage_score, have, missing, summary, basics, notes}}``; re-running
simply replaces the previous result in the UI.

``/api/find`` answers instantly (no job) from the ``.monteur-vision.json``
sidecar that a "Let Claude watch" scan leaves next to the footage
(:mod:`monteur.find`): offline word matching, no API call. An empty query is
a 400; a folder without a cache is a SOFT ``{"error": ...}`` (HTTP 200) so
the UI can explain how to get annotations instead of failing.

``/api/create/distill`` turns a finished cut into a short trailer
(:mod:`monteur.distill`) as a ``"distill"`` job: the uploaded timeline text
is parsed like ``/api/analyze`` (EDL needs fps), the cut's own shots become
the material (``probe_media=True`` — sources missing on this machine are
noted honestly, never fatal), optional music is analyzed with the usual
``"music"`` progress entry, and the trailer comes back serialized exactly
like a build result (audio auto-falls back to "original" when no music is
given).

``/api/create/export`` renders an existing ``"plan_json"`` straight to a
timeline file — SYNCHRONOUS, no job: :func:`monteur.montage.plan_from_dict`
-> :func:`monteur.montage.montage_to_timeline` -> writer is pure plan
surgery, no sift, no re-plan, so it answers instantly. It exists for the
Studio's draft-resume flow: a saved draft carries the full plan, and
resuming must rebuild the result card (and re-render on a format switch)
WITHOUT re-planning the cut. The response is the standard build-result
shape (``filename``/``content``/``plan``/``plan_json``; ``tempo`` is 0 —
the music is not re-analyzed). A bad or empty plan is a 400 with the
loader's message.

The inspector endpoints serve the Studio's shot inspector — all three are
instant (no jobs) and none of them ever sifts: they answer from the scan
cache a build/scan already filled, so a folder without a FRESH cache is a
404 with a "scan first" message, never a surprise multi-minute sift.
``GET /api/clipinfo?clip=&folder=&t0=&t1=`` returns one clip's probe facts
(width/height/fps/has_audio via :func:`monteur.media.probe`, cached by
path+mtime; probe failures degrade to zeros — facts are an upgrade, not a
gate) plus the sifted report's duration/usable_ratio and the vision fields
of the moment overlapping the optional ``t0``–``t1`` source window most
(``"moment"``: label/tags/role/hero/group/start/end/score, or null).
``POST /api/alternatives`` {"plan_json", "folder", "slot"} lists up to
:data:`_ALTERNATIVES_LIMIT` swap candidates for one slot, REUSING the
director's bench (:func:`monteur.director.review_context` — the strongest
moments no entry uses, same scoring, no duplicate logic) reordered so
same-clip / same-scene-group moments come first; each item carries the
clip's full path so the browser can thumb it. ``POST /api/plan/adjust``
{"plan_json", "slot", "transition"} is the inspector's boundary control:
:func:`monteur.montage.adjust_entry_boundary` tweaks ONE boundary
(cut/dissolve/smash — dissolve seconds by the planner's own 0.5 s rule, a
smash carves the planner's usual black dip, a cut removes one) and the
result renders through the same pure plan -> file path as /api/create/
export, so the response is the standard build-result shape and nothing is
ever re-planned. Engine ValueErrors (unknown transition, bad slot, a too-
short outgoing shot) surface as 400s with the engine's own message.

``/api/thumb`` and the ``/api/create/preview`` + ``/api/preview/<token>.mp4``
pair are the "Sehen ohne Resolve" surface on top of :mod:`monteur.preview`.
``GET /api/thumb?clip=<abs path>&t=<seconds>&w=<px, default 320>`` returns one
JPEG frame for the Studio's storyboard: frames are extracted once into a
per-server-run temp cache directory (``tempfile.mkdtemp``; keyed by the
clip's absolute path + mtime + time + width, so an unchanged clip never gets
extracted twice) and served with long ``Cache-Control`` headers. Like every
other endpoint here the clip is an absolute local path — Studio is a local
single-user tool. ANY failure (missing/bad params, unreadable clip, ffmpeg
missing) is a 404 carrying a tiny placeholder PNG, so a broken thumbnail
stays a gray tile in the UI instead of an error state.

``POST /api/create/preview`` renders the browser's current ``"plan_json"``
to a small real MP4 as a ``"preview"`` job (:func:`monteur.preview.
render_preview` — same source ranges, record gaps and music offset the
Resolve timeline would get; ~sub-second for short cuts). ``"audio"``
defaults like revise: the plan's music when it has a song, else
``"original"``. ``"width"`` defaults to 640. Per-segment engine progress
arrives as ``{"stage": "preview", "index", "total", "name"}`` job entries.
Finished previews live in one per-server-run temp directory, capped at THE
LATEST preview: after a successful render every older preview file is
deleted, so iterating on a cut never accumulates videos on disk (the fresh
job result simply carries a new URL). The result is ``{"url":
"/api/preview/<token>.mp4", "duration", "width"}``.

``POST /api/create/export-video`` is the Direct Export: the finished,
upload-ready video straight from Monteur's own engine
(:func:`monteur.preview.render_export`) — no Resolve anywhere in the
path. The browser sends the current ``"plan_json"`` plus ``"target_dir"``
(required, 400 when missing; created with parents) and optionally
``"name"`` (default ``monteur_export``; ``.mp4`` is appended when
missing), ``"canvas"`` (a :data:`monteur.montage.CANVASES` preset key,
default ``"uhd"``), ``"audio"`` (defaults like preview: the plan's music
when it has a song, else ``"original"``), ``"quality"`` (``"high"``, the
default, or ``"medium"`` — anything else is a 400) and ``"fps"``. The
``"export-video"`` job feeds the engine's staged progress through as
``{"stage": "export", "index", "total", "name"}`` entries (one per
segment, one for the audio bed, one for the final transitions + mux
pass). The result is ``{"path", "duration", "seconds", "notes"}`` —
``notes`` carries the engine's graceful degradations verbatim (dissolves
without source handles, skipped titles, missing SFX files). Rendering
comes from Monteur's own engine; the Resolve build/render pair stays the
path for grading and fine-tuning.

``GET /api/media?path=<abs path>`` is the playback surface behind the
Studio's moment player and virtual timeline playout (:mod:`monteur.
proxies`): it serves the clip's PLAYBACK PROXY when a fresh one exists in
the proxy cache (small uniform H.264/AAC, dense keyframes, +faststart —
what makes browser scrubbing instant) and falls back to the original file
otherwise, with the same single-range byte serving as the preview route
(206/416/200 + ``Accept-Ranges``), STREAMED in chunks so multi-gigabyte
originals never load into memory. The proxy is served as ``video/mp4``;
originals get a Content-Type from their suffix. Like ``/api/thumb`` the
path is an absolute local file that must exist (400 without a path, 404
when it does not exist) — Studio is a local single-user tool.

Proxies are prepared in the background: a successful scan job kicks a
``"proxies"`` job automatically (the scan result carries its id as
``"proxies_job"``), and ``POST /api/proxies {"folder"}`` (re)starts one
manually. The job runs :func:`monteur.proxies.ensure_proxies` — one
transcode per clip, skip-when-fresh, per-file progress as ``{"stage":
"proxy", "index", "total", "name"}`` entries — and prunes the cache
best-effort afterwards. Per-file failures are soft (the result's
``"errors"`` list names them; playback falls back to the original file);
the UI shows a quiet status line, and everything works without proxies.

``GET /api/preview/<token>.mp4`` serves that file WITH HTTP Range support —
``<video>`` seeking needs 206 partial responses: a valid ``Range:
bytes=a-b`` (also ``a-`` and ``-suffix`` forms; first range only) gets a
206 with ``Content-Range: bytes a-b/total`` and ``Accept-Ranges: bytes``,
an unsatisfiable range gets a 416 with ``Content-Range: bytes */total``,
no/malformed Range header gets the whole file as a plain 200. Tokens are
validated against the server's own naming (hex + ``.mp4``) so the route
can never serve anything but its own preview directory.

The ``/api/drafts`` endpoints are the Studio's WIP memory
(:mod:`monteur.drafts`, ``~/.monteur/drafts.json``): the Create wizard's
state — folder, music, settings, the full ``plan_json``, optional
pins/review — survives a browser reload as a draft record. All four are
instant, so none of them is a job: GET the light list (no plan_json), GET
one full record by id (404 when unknown), POST a record to save it (400
when ``folder``/``plan_json`` is missing; the stored record echoes back
with its stamped id/saved_at), DELETE by id. On top of the named drafts,
every SUCCESSFUL build, revise and direct-apply job writes the single
``"autosave"`` slot (folder, music and settings from its own request
payload, plus the fresh ``plan_json``) — best-effort by contract: an
autosave failure never fails the job. That slot is why a reload can always
offer "Continue where you left off" for the last good cut.

The ``/api/projects`` endpoints are the first-class project store
(:mod:`monteur.projects`, ``$MONTEUR_PROJECTS_PATH`` or
``~/.monteur/projects/``): each project is a FOLDER bundle
(``<root>/<id>/project.json`` + ``versions/`` / ``exports/``) that
references its media pool by absolute path — media is NEVER copied or moved.
GET the light list (id/name/timestamps/pool size/has-plan); GET runs
:func:`monteur.projects.migrate_drafts` first (idempotent, lossless) so
existing drafts show up as projects without ever touching ``drafts.json``.
POST ``{"name", "options"?, "media"?, "plan"?, "notes"?}`` creates one; GET
``/<id>`` loads the full manifest (404 when unknown); POST ``/<id>`` updates
the given manifest fields and bumps ``modified_at``; DELETE ``/<id>`` removes
the bundle (``{"deleted": bool}``) and NEVER the referenced media.

``/api/create/resolve`` builds the current cut as a real timeline in the
OPEN DaVinci Resolve project (a ``"resolve-build"`` job) — no file, no
import step; clips land at their true record positions (black title gaps
included) and the act titles arrive as real Text+ instead of being lost
in the FCPXML round-trip. Dissolves and the black fade-in/out cannot be
scripted in Resolve — those exist only in the downloaded file. The browser sends
the ``"plan_json"`` a build/revise result carries; the job rebuilds it
with :func:`monteur.montage.plan_from_dict` (a bad plan fails the job
with the loader's message) and hands it to
:func:`monteur.resolve.build_plan_isolated` — crash-safe by contract:
Resolve scripting runs in a disposable child process (honoring
``MONTEUR_RESOLVE_PYTHON``) and NEVER raises. Title specs come from
:func:`monteur.resolve.titles_from_plan` when the plan has dips. An
optional ``"canvas"`` (a :data:`monteur.montage.CANVASES` preset key —
the UI sends the wizard's selected shape) sizes the Resolve timeline
like the file download would; cinemascope presets also set "scale full
frame with crop" on the footage. A failure dict (Resolve not running,
scripting disabled, incompatible Python, native crash, timeout) fails
the job with the worker's own error message verbatim — it already
explains the fix. Success carries the created ``"timeline"`` name plus
any non-fatal title/canvas ``"warnings"``.

``/api/resolve/render`` is the LAST step of "media in, finished video
out": after "Build in DaVinci Resolve" put the timeline into the open
project, one more click renders it to a finished video file — no Deliver
page needed. A ``"resolve-render"`` job runs
:func:`monteur.resolve.render_isolated` (crash-safe by contract: the
streamed ``render`` worker command in a disposable child process; never
raises): ``"timeline"`` (default: the current one) is rendered into
``"target_dir"`` (required, 400 when missing; created with parents) as
``"name"`` (default ``monteur_render``) using ``"preset"`` quality
("2160p", the default, or "1080p" — anything else is a 400). Progress
arrives live: each new percent Resolve reports becomes a ``{"stage":
"render", "percent": N}`` job-progress entry, which the Studio job panel
turns into a real progress bar. The result is ``{"path", "seconds",
"preset"}`` — the finished file, the wall-clock render time and what was
actually chosen (a shipped preset name like "YouTube - 2160p", or the
"mp4/H264"-style format/codec fallback). Cancelling the job kills the
MONITORING child process (checked at the next progress line) — Resolve
itself keeps rendering; the UI says so honestly and points at Resolve's
Deliver page. A worker failure (Resolve closed meanwhile, no usable
preset/codec, a failed render job) fails the job with the worker's own
message verbatim.

The ``/api/settings`` endpoints manage how Monteur reaches Claude — the end
user has a finished application, not a CLI, so the backend choice and the
API key live in the UI, backed by :mod:`monteur.settings`
(``~/.monteur/settings.json``). The GET/POST view is ``{"backend":
"auto"|"api"|"claude-cli", "api_key_set", "api_key_hint" ("…" + last 4,
NEVER the key itself), "env_key_set", "cli_found", "backend_forced_by_env",
"effective": "api"|"claude-cli"|"none"}`` where ``effective`` is what
:func:`monteur.ai._resolve_backend` would pick right now — settings are read
per AI call, so a change applies to the very next request, no restart.
POST validates: ``backend`` must be auto/api/claude-cli (400 otherwise);
``api_key`` ``""`` clears the stored key, non-empty keys are stripped and
must not contain whitespace (400). ``/api/settings/test`` is the user's
"does my key / Claude Code work?" button: an ``"ai-test"`` job (the browser
must not freeze on a network round-trip) that runs one tiny completion
through the CURRENT effective backend and returns ``{"backend", "reply"}``
— a MonteurAIError fails the job with its own actionable message.

The same settings view also manages WHICH PYTHON runs the isolated DaVinci
Resolve worker (Resolve's native module needs a 64-bit ~3.6–3.11 and
hard-crashes newer interpreters — the exact situation an end user cannot
fix with environment variables). ``GET /api/settings`` additionally carries
``resolve_python`` (the saved path, "" = unset) and
``resolve_python_env_set`` (MONTEUR_RESOLVE_PYTHON present — the advanced
override that wins over the setting). ``POST /api/settings`` accepts
``resolve_python``: ``""`` clears it, a non-empty path must exist on disk
(400 otherwise). ``GET /api/resolve/diagnose`` returns
:func:`monteur.resolve.diagnose` verbatim — the settings panel shows its
``verdict`` instead of inventing client-side logic. ``POST
/api/resolve/detect`` is the one-click fix: a ``"resolve-detect"`` job
(probing several interpreters takes seconds) that runs
:func:`monteur.resolve.find_resolve_python`; when an interpreter is found
it is SAVED to settings right there — that is the point of the button —
and the job result carries ``{"found", "connected", "version", "probed",
"verdict"}`` with a FRESH post-save diagnosis verdict. Finding nothing is
information, not an error: the job still succeeds with ``"found": null``
and the probe report, and the UI shows the guided python.org install help.

The ``/api/youtube/*`` endpoints are the "Publish to YouTube" surface on
top of :mod:`monteur.youtube` (stdlib OAuth + resumable upload; the module
docstring carries the whole story — user-owned Google Cloud project,
private-only uploads from unverified projects, ~6 uploads/day quota).
``status`` reports ``configured`` (client id + secret saved), ``connected``
(refresh token saved) and the ``channel`` hint. ``credentials`` saves the
Desktop-app client pair (both stripped and non-empty, 400 otherwise;
sending both as ``""`` clears them AND disconnects — a new project makes
the old token meaningless). ``connect`` starts the RFC 8252 loopback flow:
the server generates a single-use ``state``, remembers it (module-level,
one Studio serves one user), and answers with the Google consent
``auth_url`` whose ``redirect_uri`` points back at THIS server —
``http://127.0.0.1:<own port>/api/youtube/callback`` (Google's desktop-app
clients accept any 127.0.0.1 port, so the running Studio IS the loopback
target; no second server, no copy-pasted codes). The ``callback`` GET is
opened by the BROWSER, not the app, so it answers with tiny HTML pages:
state mismatch/stale link -> a readable error page (400), a Google error
-> the error text, success -> the code is exchanged for tokens
(:func:`monteur.youtube.exchange_code`), the refresh token is saved to
settings, and a self-closing "YouTube connected — you can close this tab"
page renders. ``disconnect`` clears the refresh token (the credentials
stay — reconnecting is one click).

``upload`` validates up front (missing path/title, file not found, bad
privacy, not connected -> 400) and runs a ``"youtube-upload"`` job: a
fresh access token is minted from the stored refresh token, the file goes
up through the resumable protocol with byte progress as ``{"stage":
"upload", "sent", "total"}`` entries, a mid-upload ``TokenExpired`` gets
exactly ONE refresh+retry (still failing -> job error "reconnect in
settings"), and a ``QuotaExceeded`` fails the job with its friendly
daily-limit message verbatim. An optional ``thumbnail`` is set
best-effort — its failure is a result note, never a job error. The result
is ``{"video_id", "url" (the studio.youtube.com/video/<id>/edit review
link), "watch_url", "privacy", "channel", "notes"}``; the channel title
from the upload response is remembered in settings as the status hint.
``prefill`` is synchronous and offline (the AI-assisted path stays the
publish kit): title from the draft ``name`` or the plan's composed
"story:" note, description = story + the :func:`monteur.publish.
plan_chapters` chapter lines starting at 0:00, tags from
:func:`monteur.publish.plan_tags` — all deterministic, nothing invented.

The ``/api/movie/*`` endpoints drive the Studio's Movie view on top of
:mod:`monteur.movie`. ``load`` and ``assign`` are instant (no job; both
return the project dict, its shooting progress AND the deterministic
``"shoot_plan"`` — :func:`monteur.movie.shoot_plan`, the scene-aware
"what still has to be filmed" aggregation the Movie view's Shoot-plan
panel renders). ``new`` drafts a whole blueprint with Claude as a
``"movie"`` job — unlike vision this IS a gate: a screenplay cannot
degrade to offline, so a ``MonteurAIError`` fails the job with its
message. ``check`` sifts a scene's assigned folder (through the same
scan cache as build/pick/kit) as a ``"scene-check"`` job, optionally
lets Claude vision label it (``"see"``, soft-fail as above), and holds
the footage against the scene text with
:func:`monteur.movie.check_scene_footage` — honest hints, not verdicts.
The check is PERSISTED on its scene slot in movie.json
(:func:`monteur.movie.record_scene_check`; best-effort — an unwritable
folder becomes ``"persist_error"``, never a job error) so the shoot plan
can say checked-ok/checked-weak across restarts, and the job result
carries the refreshed ``"shoot_plan"``. ``assemble`` cuts the whole film
along the screenplay as a ``"movie-assemble"`` job
(:func:`monteur.movie.assemble_movie`): scenes without footage are
skipped (noted, never fatal — only a project with NO assigned scene
fails the job), the per-scene sifts run through the same scan cache, and
the finished timeline comes back serialized as FCPXML or EDL exactly
like a build result. The result ALSO carries the assembled film as
``"plan_json"`` (one no-music MontagePlan whose entries sit at absolute
film positions — assemble_movie rule 9) plus ``"fps"``, ``"canvas"``,
``"title"`` and the ``"scenes"`` marker list, which is what feeds the
movie result card the same plan-based toolchain the Create card has:
``/api/create/preview``, ``/api/thumb`` storyboard, ``/api/create/
export-video``, ``/api/create/resolve``, ``/api/create/direct`` (with
``"folders"`` + the screenplay as notes) and ``/api/youtube/*``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from monteur import __version__
from monteur.analysis import analyze_timeline, compare
from monteur.project import Project

_APP_HTML = Path(__file__).with_name("app.html")

# Writing a response to a socket the browser already closed raises one of these
# — very common on Windows (WinError 10053 ConnectionAbortedError / 10054
# ConnectionResetError). The client simply went away; it is not worth crashing a
# worker thread over. (ConnectionReset/Aborted/BrokenPipe are all subclasses of
# ConnectionError, but we spell them out for clarity / defensiveness.)
_CLIENT_GONE = (
    ConnectionError,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
)


def _install_diagnostic_hooks():
    """Install excepthooks that make an otherwise-silent crash VISIBLE.

    A crash inside a ThreadingHTTPServer worker thread (i.e. while handling a
    request) never reaches serve()'s main-thread ``except`` — it dies in the
    worker. Without a ``threading.excepthook`` that would print a traceback and
    vanish (or, worse, be swallowed). We install one here that always flushes,
    and chain to whatever was installed before.

    Returns ``(prev_threading_hook, prev_sys_hook)`` so serve() can restore the
    originals in its ``finally`` block — importing this module must not globally
    mutate the hooks (keeps the test suite clean).
    """
    prev_thread_hook = threading.excepthook
    prev_sys_hook = sys.excepthook

    def worker_hook(args):
        import traceback

        name = getattr(args.thread, "name", "?")
        print(
            f"Monteur Studio: uncaught error in worker thread {name}:",
            flush=True,
        )
        traceback.print_exception(
            args.exc_type, args.exc_value, args.exc_traceback
        )
        sys.stderr.flush()
        if prev_thread_hook is not None:
            try:
                prev_thread_hook(args)
            except Exception:  # noqa: BLE001 - a chained hook must not re-crash
                pass

    def main_hook(exc_type, exc_value, exc_tb):
        try:
            if prev_sys_hook is not None:
                prev_sys_hook(exc_type, exc_value, exc_tb)
        finally:
            sys.stderr.flush()
            sys.stdout.flush()

    threading.excepthook = worker_hook
    sys.excepthook = main_hook
    return prev_thread_hook, prev_sys_hook


def _restore_diagnostic_hooks(prev_thread_hook, prev_sys_hook) -> None:
    """Undo :func:`_install_diagnostic_hooks` (used by serve()'s finally)."""
    threading.excepthook = prev_thread_hook
    sys.excepthook = prev_sys_hook


class MonteurServer(ThreadingHTTPServer):
    """Hardened server class used by serve().

    * ``daemon_threads`` — worker threads don't keep the process alive on exit.
    * ``allow_reuse_address`` — restart-friendly (no TIME_WAIT bind failures).
    * ``handle_error`` — one concise line per bad request instead of
      socketserver's default multi-line stderr dump, so a single dropped
      connection never *looks* catastrophic.
    """

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, _CLIENT_GONE):
            return  # the client simply disconnected — not worth a line
        print(
            f"Monteur Studio: error serving {client_address} — "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        import traceback

        traceback.print_exc()  # a real error here deserves its full stack
        sys.stderr.flush()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _timeline_from_payload(payload: dict):
    """Parse an uploaded ``{"filename", "content", "fps"?}`` timeline text.

    The one reader every upload endpoint shares (analyze, compare, versions,
    distill): EDLs need ``fps``, FCPXMLs carry their own. Bad input raises
    ApiError(400) with a user-ready message.
    """
    from monteur.io import edl, fcpxml

    filename = payload.get("filename", "")
    content = payload.get("content")
    if not content:
        raise ApiError(400, "missing 'content'")
    suffix = Path(filename).suffix.lower()
    if suffix == ".edl":
        fps = payload.get("fps")
        if not fps:
            raise ApiError(400, "EDL files need 'fps'")
        timeline = edl.read_edl(content, fps=float(fps), name=Path(filename).stem)
    elif suffix in (".xml", ".fcpxml"):
        timeline = fcpxml.read_fcpxml(content)
        if not timeline.name:
            timeline.name = Path(filename).stem
    else:
        raise ApiError(400, f"unsupported file type: {filename!r} (use .edl or .fcpxml)")
    return timeline


def _analyze_payload(payload: dict):
    return analyze_timeline(_timeline_from_payload(payload))


# --- background jobs (scan/build) ---------------------------------------------
#
# Scans and builds run in daemon threads; the browser polls GET /api/jobs/<id>.
# The registry is module-level (one Studio process serves one user) and capped
# at the last _MAX_JOBS jobs — oldest FINISHED jobs are evicted first, running
# jobs are never dropped.

_MAX_JOBS = 20
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()

# Result of the last successful sift: {"folder", "mtimes": {path: mtime},
# "reports": [ClipReport]}. A build reuses it when the folder matches and no
# file changed on disk, so scan-then-build never sifts the same footage twice.
_SCAN_CACHE: dict = {}
_SCAN_CACHE_LOCK = threading.Lock()

# Per-CLIP sift reports, keyed by absolute path -> (mtime, ClipReport). The
# media pool's STAGED flow analyzes a chosen SUBSET of clips (not a whole
# folder), so results are remembered clip-by-clip: the pool listing reads a
# clip's status from here, "Let Claude check" reuses the same reports, and a
# build assembles a folder's reports from here when every clip is analyzed.
# Populated by every sift (folder scan AND subset analyze) via
# :func:`_remember_clip_reports`; freshness is per-clip by mtime, so an edited
# or replaced clip is a natural miss.
_CLIP_CACHE: dict[str, tuple[float, object]] = {}
_CLIP_CACHE_LOCK = threading.Lock()


def _remember_clip_reports(reports: list) -> None:
    """Cache each clip's report in memory AND on disk (subset-safe).

    The disk sidecar (``.monteur-sift.json`` next to the footage) is what makes
    re-opening a project skip the sift entirely — the in-memory cache alone is
    lost when the process exits.
    """
    with _CLIP_CACHE_LOCK:
        for report in reports:
            try:
                path = os.path.abspath(report.path)
                _CLIP_CACHE[path] = (os.path.getmtime(path), report)
            except (OSError, AttributeError):
                continue  # a vanished clip is simply not remembered
    try:
        from monteur import sift

        sift.remember_reports(reports)  # persist next to the footage (best-effort)
    except Exception:  # noqa: BLE001 — persistence is an upgrade, never a gate
        pass


def _cached_clip_report(path: str):
    """The cached report for one clip, or None unless present AND mtime-fresh.

    Two tiers: the in-memory cache first, then the on-disk sidecar
    (``.monteur-sift.json``) — so a project re-opened in a fresh process reuses
    the sift instead of re-crunching. A disk hit is promoted into memory.
    """
    ab = os.path.abspath(path)
    with _CLIP_CACHE_LOCK:
        entry = _CLIP_CACHE.get(ab)
    if entry:
        mtime, report = entry
        try:
            if os.path.getmtime(ab) == mtime:
                return report
        except OSError:
            return None
    # in-memory miss (or stale) -> the persisted sidecar
    try:
        from monteur import sift

        report = sift.recall_report(ab)
    except Exception:  # noqa: BLE001
        report = None
    if report is not None:
        try:
            with _CLIP_CACHE_LOCK:
                _CLIP_CACHE[ab] = (os.path.getmtime(ab), report)
        except OSError:
            pass
    return report

# One native file dialog at a time; Tk lives entirely inside a dedicated thread.
_PICK_LOCK = threading.Lock()

# --- "Sehen ohne Resolve": storyboard thumbnails + preview MP4s ---------------
#
# Both caches are per-server-run temp directories (tempfile.mkdtemp, created
# lazily on first use, left to the OS temp cleanup like the sift's own work
# dirs). The thumbnail cache is keyed by (absolute clip path, mtime, time,
# width) so repeats are instant; the preview dir is capped at the latest
# preview — every successful render deletes the older files.

_THUMB_DIR: str | None = None
_THUMB_LOCK = threading.Lock()

_PREVIEW_DIR: str | None = None
_PREVIEW_LOCK = threading.Lock()

# A preview file name is exactly what _run_preview_job writes: hex token + .mp4.
_PREVIEW_NAME_RE = re.compile(r"[0-9a-f]{16}\.mp4")

# 1x1 gray PNG — the body of every thumbnail 404, so a broken thumb renders
# as a quiet gray tile instead of a broken-image icon.
_THUMB_PLACEHOLDER = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\xf8\x0f"
    b"\x00\x01\x04\x01\x00}\xb4Q\xe9\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _thumb_dir() -> str:
    """The per-run thumbnail cache directory (created on first use)."""
    global _THUMB_DIR
    with _THUMB_LOCK:
        if _THUMB_DIR is None or not os.path.isdir(_THUMB_DIR):
            import tempfile

            _THUMB_DIR = tempfile.mkdtemp(prefix="monteur-thumbs-")
        return _THUMB_DIR


def _preview_dir() -> str:
    """The per-run preview directory (created on first use)."""
    global _PREVIEW_DIR
    with _PREVIEW_LOCK:
        if _PREVIEW_DIR is None or not os.path.isdir(_PREVIEW_DIR):
            import tempfile

            _PREVIEW_DIR = tempfile.mkdtemp(prefix="monteur-previews-")
        return _PREVIEW_DIR


def _moment_key(path: str, start: float) -> str:
    """Stable key for one moment's editor note: ``<abs clip path>|<start>``.

    The start is rounded to 0.01s so the same moment always maps to the same
    key across a re-open (the sift is deterministic from the same media). Used
    by the Moments review store (``project.moment_notes``): written by
    ``/moment-note``, read by ``/clips`` and the build's note application.
    """
    return f"{os.path.abspath(path)}|{round(float(start), 2):.2f}"


def _thumb_cache_path(clip: str, time_s: float, width: int) -> str:
    """Cache file for one (path, mtime, t, w) thumbnail request.

    The mtime is part of the key, so editing/replacing a clip naturally
    invalidates its cached frames. Raises OSError when the clip is gone.
    """
    import hashlib

    clip_abs = os.path.abspath(clip)
    mtime_ns = os.stat(clip_abs).st_mtime_ns
    key = f"{clip_abs}|{mtime_ns}|{time_s:.3f}|{width}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return os.path.join(_thumb_dir(), f"{digest}.jpg")


def _run_preview_job(job: dict, payload: dict) -> None:
    """Daemon-thread body for POST /api/create/preview (plan -> small MP4).

    Pure plan -> pixels through :func:`monteur.preview.render_preview`: no
    sift, no music analysis, no AI — just the ffmpeg passes, so even the
    job overhead is mostly ceremony. Audio defaults exactly like revise
    (the plan's music when it has a song, else "original"); the engine's
    per-segment progress callback feeds ``{"stage": "preview"}`` entries.
    On success the previous previews are deleted (the directory holds one
    finished preview at a time — the latest); a FAILED render deletes
    nothing, so the last good preview keeps playing.
    """
    from monteur.color import grade_from_dict
    from monteur.media import MediaCancelled, MonteurMediaError
    from monteur.montage import plan_from_dict

    try:
        plan = plan_from_dict(payload.get("plan_json") or {})  # bad -> ValueError
        if not plan.entries:
            raise ValueError("the plan has no entries — nothing to preview")
        # the same colour grade the export bakes, so the preview MATCHES it
        _grade_payload = payload.get("grade")
        grade = grade_from_dict(_grade_payload) if _grade_payload else None
        audio = payload.get("audio") or ("music" if plan.music_path else "original")
        if not plan.music_path and audio != "original":
            raise ValueError(f"the plan has no music; audio mode {audio!r} needs a song")
        try:
            width = int(payload.get("width") or 640)
        except (TypeError, ValueError):
            raise ValueError("'width' must be a number of pixels")
        width = max(160, min(1920, width))

        def progress(done: int, total: int, label: str) -> None:
            entry = {"stage": "preview", "index": done, "total": total, "name": label}
            with _JOBS_LOCK:
                job["progress"].append(entry)

        # Resolved at CALL time so tests can monkeypatch monteur.preview.
        from monteur.preview import render_preview

        directory = _preview_dir()
        token = secrets.token_hex(8)
        out_path = os.path.join(directory, f"{token}.mp4")
        result = render_preview(
            plan, out_path, width=width, audio=audio, progress=progress,
            grade=grade, cancel=job["cancel"],
        )
        # Cap the directory at THE latest preview: the fresh file replaces
        # every older one (best-effort — a locked file loses us nothing).
        for name in os.listdir(directory):
            if name != f"{token}.mp4" and _PREVIEW_NAME_RE.fullmatch(name):
                try:
                    os.remove(os.path.join(directory, name))
                except OSError:
                    pass
        job["result"] = {
            "url": f"/api/preview/{token}.mp4",
            "duration": result["duration"],
            "width": result["width"],
        }
        job["state"] = "done"
    except MediaCancelled:
        job["state"] = "cancelled"
    except (MonteurMediaError, ValueError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _run_export_video_job(job: dict, payload: dict) -> None:
    """Daemon-thread body for POST /api/create/export-video (plan -> MP4).

    The Direct Export: pure plan -> finished video through
    :func:`monteur.preview.render_export` — no sift, no re-plan, no
    Resolve. Audio defaults exactly like the preview job (the plan's
    music when it has a song, else "original"); the engine's staged
    progress callback feeds ``{"stage": "export"}`` entries the Studio
    job panel renders as a segments bar. The target directory is created
    with parents; the result carries the engine's honest ``notes``
    (missing dissolve handles, skipped titles, missing SFX files).
    """
    from monteur.media import MediaCancelled, MonteurMediaError
    from monteur.montage import plan_from_dict

    from monteur.color import grade_from_dict

    try:
        plan = plan_from_dict(payload.get("plan_json") or {})  # bad -> ValueError
        # a colour grade only when one was sent; absent -> None -> the export
        # is byte-identical to before grading existed
        _grade_payload = payload.get("grade")
        grade = grade_from_dict(_grade_payload) if _grade_payload else None
        if not plan.entries:
            raise ValueError("the plan has no entries — nothing to export")
        audio = payload.get("audio") or ("music" if plan.music_path else "original")
        if not plan.music_path and audio != "original":
            raise ValueError(f"the plan has no music; audio mode {audio!r} needs a song")
        target_dir = str(payload.get("target_dir") or "").strip()
        name = str(payload.get("name") or "").strip() or "monteur_export"
        if not name.lower().endswith(".mp4"):
            name += ".mp4"
        canvas = str(payload.get("canvas") or "uhd")
        quality = str(payload.get("quality") or "high")
        try:
            fps = float(payload.get("fps") or 25.0)
        except (TypeError, ValueError):
            raise ValueError("'fps' must be a number")
        os.makedirs(target_dir, exist_ok=True)
        out_path = os.path.join(target_dir, name)

        def progress(done: int, total: int, label: str) -> None:
            entry = {"stage": "export", "index": done, "total": total, "name": label}
            with _JOBS_LOCK:
                job["progress"].append(entry)

        # Resolved at CALL time so tests can monkeypatch monteur.preview.
        from monteur.preview import render_export

        result = render_export(
            plan, out_path, canvas=canvas, fps=fps, audio=audio,
            quality=quality, progress=progress, grade=grade, cancel=job["cancel"],
        )
        job["result"] = {
            "path": result["path"],
            "duration": result["duration"],
            "seconds": result["seconds"],
            "notes": list(result.get("notes") or []),
        }
        job["state"] = "done"
    except MediaCancelled:
        job["state"] = "cancelled"
    except (MonteurMediaError, ValueError, OSError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


# Content types for /api/media originals, by suffix (the proxy is always
# video/mp4). Unknown suffixes degrade to octet-stream — the browser then
# probes the bytes itself.
_MEDIA_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo", ".mts": "video/mp2t", ".m2ts": "video/mp2t",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".opus": "audio/ogg", ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
}


def _run_proxies_job(job: dict, folder: str) -> None:
    """Daemon-thread body for the background playback-proxy transcodes.

    One :func:`monteur.proxies.ensure_proxy` per video file in ``folder``
    (skip-when-fresh — a re-run after an unchanged scan is near-instant),
    per-file progress as ``{"stage": "proxy"}`` entries, then a
    best-effort cache prune. Per-file failures are SOFT: they land in the
    result's ``"errors"`` list and playback of those clips falls back to
    the original file — proxies are an upgrade, never a gate.

    ``monteur.proxies`` is resolved at CALL time via importlib (honours
    ``sys.modules``), so tests can monkeypatch ``ensure_proxies`` /
    ``prune_proxies`` without a single real transcode.
    """
    import importlib

    from monteur.media import MonteurMediaError, list_media

    try:
        paths = [str(p) for p in list_media(folder)]
        if not paths:
            raise MonteurMediaError(f"no video files found in {folder}")
        proxies = importlib.import_module("monteur.proxies")

        def progress(done: int, total: int, name: str) -> None:
            entry = {"stage": "proxy", "index": done, "total": total, "name": name}
            with _JOBS_LOCK:
                job["progress"].append(entry)

        made, errors = proxies.ensure_proxies(
            paths, progress=progress, cancel=job["cancel"]
        )
        if job["cancel"].is_set():
            job["state"] = "cancelled"
            return
        try:  # keep the cache bounded — best-effort by contract
            proxies.prune_proxies()
        except Exception:  # noqa: BLE001 — pruning must never fail the job
            pass
        job["result"] = {
            "ready": len(made),
            "total": len(paths),
            "errors": [
                f"{Path(path).name}: {message}"
                for path, message in errors.items()
            ],
        }
        job["state"] = "done"
    except (MonteurMediaError, ValueError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _start_proxies_job(folder: str) -> dict:
    """Create and start a ``"proxies"`` job for ``folder``; returns the job."""
    job = _new_job("proxies")
    threading.Thread(
        target=_run_proxies_job,
        args=(job, folder),
        name=f"monteur-proxies-{job['id']}",
        daemon=True,
    ).start()
    return job


def _new_job(kind: str, context: dict | None = None) -> dict:
    job = {
        "id": secrets.token_hex(4),
        "kind": kind,  # "scan" | "build" | "pick" | "kit" | "revise" | "direct" | "direct-apply" | "coverage" | "distill" | "preview" | "proxies" | "export-video" | "resolve-build" | "resolve-render" | "resolve-detect" | "movie" | "scene-check" | "movie-assemble" | "ai-test" | "youtube-upload"
        "state": "running",  # -> "done" | "error" | "cancelled"
        "progress": [],  # dicts: {"index","total","name","stage"[,"usable_ratio"]}
        "message": "",  # human-readable reason when state == "error"
        "result": None,  # dict when state == "done"
        "created": time.time(),
        "cancel": threading.Event(),
        # reattach hints (JSON-safe): the UI queries GET /api/jobs to find a
        # still-running job it navigated away from — folder/project for a scan,
        # the build body for a build — so nothing has to live in the browser.
        "context": dict(context or {}),
    }
    with _JOBS_LOCK:
        _JOBS[job["id"]] = job
        if len(_JOBS) > _MAX_JOBS:
            finished = sorted(
                (j for j in _JOBS.values() if j["state"] != "running"),
                key=lambda j: j["created"],
            )
            for old in finished:
                if len(_JOBS) <= _MAX_JOBS:
                    break
                del _JOBS[old["id"]]
    return job


def _job_view(job: dict) -> dict:
    """A JSON-safe snapshot of a job (everything but the cancel Event)."""
    with _JOBS_LOCK:
        view = {k: job[k] for k in ("id", "kind", "state", "message", "result", "created")}
        view["progress"] = list(job["progress"])
        view["context"] = dict(job.get("context") or {})
    return view


def _job_progress(job: dict):
    """A sift progress callback that appends per-clip entries to the job."""

    def callback(index, total, name, stage, report):
        entry = {"index": index, "total": total, "name": name, "stage": stage}
        if stage == "done" and report is not None:
            entry["usable_ratio"] = report.usable_ratio
        with _JOBS_LOCK:
            job["progress"].append(entry)

    return callback


def _remember_scan(folder: str, reports: list) -> None:
    """Cache a successful sift keyed by folder + per-file mtimes."""
    mtimes: dict[str, float] = {}
    try:
        for report in reports:
            mtimes[os.path.abspath(report.path)] = os.path.getmtime(report.path)
    except OSError:
        return  # a file vanished mid-scan — don't cache a stale picture
    with _SCAN_CACHE_LOCK:
        _SCAN_CACHE.clear()
        _SCAN_CACHE.update(
            folder=os.path.abspath(folder), mtimes=mtimes, reports=list(reports)
        )
    # Also remember each clip individually, so the pool's per-clip status and
    # a later subset "check" reuse a full folder scan without re-sifting.
    _remember_clip_reports(reports)


def _cached_reports(folder: str):
    """The cached sift reports for a folder, or None when none are fresh.

    Two sources, in order: the whole-folder scan cache (a matching folder with
    every file's mtime unchanged), then the per-clip cache — a build reuses
    subset-analyzed footage when EVERY clip in the folder has a fresh report
    (the staged "analyze selected" path, once the whole folder is covered)."""
    from monteur.media import MonteurMediaError, list_media

    with _SCAN_CACHE_LOCK:
        cache = dict(_SCAN_CACHE)
    try:
        media = [os.path.abspath(str(p)) for p in list_media(folder)]
    except MonteurMediaError:
        return None
    if not media:
        return None
    current = set(media)
    if cache and cache["folder"] == os.path.abspath(folder) and current == set(
        cache["mtimes"]
    ):
        try:
            if all(
                os.path.getmtime(path) == mtime
                for path, mtime in cache["mtimes"].items()
            ):
                return cache["reports"]
        except OSError:
            pass
    # Fall back to the per-clip cache: reuse only when the WHOLE folder is
    # covered by fresh per-clip reports (a partial analysis re-sifts on build).
    assembled = []
    for path in media:
        report = _cached_clip_report(path)
        if report is None:
            return None
        assembled.append(report)
    return assembled


# --- the media pool: resolve referenced files/folders -> clips + status ---
#
# The pool is the project's ``media_pool`` — absolute paths REFERENCED from
# disk (Resolve-style), never copied or moved. Resolving it to clip cards is
# deliberately CHEAP: expand each folder with :func:`list_media` (a directory
# listing), then read each clip's status entirely from caches we already
# keep — no re-sift, no re-decode, no probe. So the pool page reflects "we
# store knowledge, not video" instantly, however large the shoot.


def _vision_labeled_paths(folder: str) -> set[str]:
    """Absolute clip paths with a vision label in ``folder``'s cache.

    Reads the folder's ``.monteur-vision.json`` (one small file) and keys off
    each entry's clip path — the cache key is ``"<abspath>|<mtime>|<win>|
    <model>"`` (see :func:`monteur.vision._moment_key`). Missing/unreadable/
    malformed cache -> an empty set (labeling is an upgrade, never a gate).
    """
    from monteur.vision import CACHE_FILENAME

    cache_file = os.path.join(folder, CACHE_FILENAME)
    try:
        data = json.loads(Path(cache_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    labeled: set[str] = set()
    for key in data:
        head = str(key).split("|", 1)[0]
        if head:
            labeled.add(os.path.abspath(head))
    return labeled


def _report_is_labeled(report) -> bool:
    """True when a clip's report carries Claude/vision annotations.

    A plain sift never sets a moment's ``role`` or ``hero`` — those come only
    from the vision pass (monteur.vision). So a report with any moment carrying
    a role or a positive hero score has been Claude-checked.
    """
    for moment in getattr(report, "moments", None) or []:
        if str(getattr(moment, "role", "") or "").strip():
            return True
        try:
            if float(getattr(moment, "hero", 0.0) or 0.0) > 0.0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _resolve_pool(project) -> dict:
    """Expand a project's ``media_pool`` into ``{clips, entries}`` with status.

    Each folder entry becomes its member clips (:func:`list_media`); a file
    entry is itself. Every clip carries CHEAP status flags: ``sifted`` (THIS
    project's analysis store holds a report for the clip — carrying its
    ``usable_ratio``/``duration``), ``labeled`` (that report was Claude-checked,
    :func:`_report_is_labeled`), and ``proxy_fresh``
    (:func:`monteur.proxies.fresh_proxy`). Status is per-PROJECT — a sidecar
    another project left next to the footage never greens a fresh pool. Nothing
    here opens or decodes a clip.
    """
    from monteur import projects
    from monteur import proxies as proxies_mod
    from monteur.sift import list_media

    # Status reflects THIS PROJECT's stored analysis — not a sidecar that some
    # other project left next to the footage. So freshly-pooled footage reads
    # "Not analyzed" until you analyze it in this project, and it greens up
    # incrementally as each clip lands in the project's analysis store.
    proj_reports = {
        os.path.abspath(r.path): r for r in projects.load_reports(project)
    }

    clips: list[dict] = []
    entries: list[dict] = []
    seen: set[str] = set()
    for entry in project.media_pool:
        path = str(entry.get("path") or "")
        if not path:
            continue
        kind = entry.get("kind") if entry.get("kind") in ("file", "folder") else "file"
        if kind == "folder":
            try:
                members = [str(p) for p in list_media(path)]
            except Exception:  # noqa: BLE001 — a gone/bad folder lists as empty
                members = []
        else:
            members = [path]
        entries.append(
            {"path": os.path.abspath(path), "kind": kind, "clip_count": len(members)}
        )
        for member in members:
            ab = os.path.abspath(member)
            if ab in seen:
                continue  # a clip referenced twice (folder + file) shows once
            seen.add(ab)
            report = proj_reports.get(ab)
            clip = {
                "path": ab,
                "name": os.path.basename(ab),
                "kind": "file",
                "thumb": True,
                "sifted": report is not None,
                "proxy_fresh": proxies_mod.fresh_proxy(ab) is not None,
                # "Claude-checked" only when THIS project's stored report carries
                # vision annotations (role/hero come from the Claude pass, never
                # from a plain sift).
                "labeled": report is not None and _report_is_labeled(report),
            }
            if report is not None:
                clip["usable_ratio"] = report.usable_ratio
                clip["duration"] = report.duration
                clip["moments"] = len(report.moments)
            clips.append(clip)
    return {"clips": clips, "entries": entries}


# --- the shot inspector's instant endpoints ------------------------------
#
# clipinfo/alternatives answer from the scan cache only (never a sift — the
# inspector must stay instant), so both 404 with a "scan first" message when
# the cache is stale or for another folder. Probe facts are cached by
# (absolute path, mtime_ns): the same key discipline as the thumbnail cache,
# so replacing a clip naturally refreshes its facts.

#: How many swap candidates POST /api/alternatives returns at most.
_ALTERNATIVES_LIMIT = 6

_CLIPINFO_CACHE: dict[tuple[str, int], dict] = {}
_CLIPINFO_LOCK = threading.Lock()


def _probe_facts(path: str) -> dict:
    """width/height/fps/has_audio for one clip — cached, soft-failing.

    A clip that cannot be probed (ffmpeg missing, file unreadable) yields
    zeros/False instead of an error: the inspector's facts are an upgrade,
    not a gate, exactly like vision on a scan.
    """
    try:
        key = (os.path.abspath(path), os.stat(path).st_mtime_ns)
    except OSError:
        return {"width": 0, "height": 0, "fps": 0.0, "has_audio": False}
    with _CLIPINFO_LOCK:
        cached = _CLIPINFO_CACHE.get(key)
    if cached is not None:
        return dict(cached)
    facts = {"width": 0, "height": 0, "fps": 0.0, "has_audio": False}
    try:
        from monteur.media import MonteurMediaError, probe

        info = probe(path)
        facts = {
            "width": int(info.width),
            "height": int(info.height),
            "fps": float(info.fps),
            "has_audio": bool(info.has_audio),
        }
    except Exception:  # noqa: BLE001 — probe failure must stay a soft zero
        pass
    with _CLIPINFO_LOCK:
        _CLIPINFO_CACHE[key] = dict(facts)
    return facts


def _fresh_reports_or_404(folder: str) -> list:
    """The scan cache's reports for ``folder`` — or ApiError(404).

    The inspector endpoints never sift: without a fresh cache the honest
    answer is "scan first", instantly, not a surprise background sift.
    """
    if not folder:
        raise ApiError(400, "missing 'folder' (path to your footage)")
    reports = _cached_reports(folder)
    if reports is None:
        raise ApiError(
            404,
            "no fresh scan for this folder — build or scan first, then "
            "open the inspector again",
        )
    return reports


def _report_for_clip(reports: list, clip: str):
    """Match a clip path against the reports by absolute path or basename."""
    clip_abs = os.path.abspath(clip)
    name = Path(clip).name
    for report in reports:
        if os.path.abspath(report.path) == clip_abs:
            return report
    for report in reports:
        if Path(report.path).name == name:
            return report
    return None


def _plan_export_result(plan, payload: dict) -> dict:
    """Render an in-memory plan through the export path — the one shared
    plan -> build-result-shape serializer (used by /api/create/export and
    /api/plan/adjust). ``tempo`` is honestly 0: nothing re-listens here."""
    from monteur.io import write_edl, write_fcpxml
    from monteur.montage import montage_to_timeline, plan_to_dict

    audio = payload.get("audio") or ("music" if plan.music_path else "original")
    if not plan.music_path and audio != "original":
        raise ApiError(
            400, f"the plan has no music; audio mode {audio!r} needs a song"
        )
    fps = float(payload.get("fps") or 25)
    timeline_kwargs: dict = {"audio": audio}
    if payload.get("canvas"):
        timeline_kwargs["canvas"] = payload["canvas"]
    timeline = montage_to_timeline(plan, fps=fps, **timeline_kwargs)
    fmt = (payload.get("format") or "fcpxml").lower()
    if fmt == "edl":
        content, filename = write_edl(timeline), "monteur_montage.edl"
    else:
        content, filename = write_fcpxml(timeline), "monteur_montage.fcpxml"
    return {
        "filename": filename,
        "content": content,
        "plan": {
            "duration": plan.duration,
            "cuts": len(plan.entries),
            "tempo": 0,  # the music is not re-analyzed here
            "notes": plan.notes,
        },
        "plan_json": plan_to_dict(plan),
    }


def _persist_plan_edit(payload: dict, plan, label: str) -> None:
    """Persist a timeline edit back into its project — the durable timeline.

    The timeline (plan) is the project's asset: every editor edit (delete,
    move, trim, retitle) writes it to ``project.plan`` and snapshots the prior
    cut as a version, so the change survives a re-open and stays undoable.
    Best-effort and keyed off ``payload["project"]``; no project id (or an
    unknown project) is a no-op — the browser still receives the adjusted plan
    either way, so a persistence hiccup never blocks the edit.
    """
    pid = str(payload.get("project") or "")
    if not pid:
        return
    try:
        from monteur import projects
        from monteur.montage import plan_to_dict

        project = projects.load_project(pid)
        if project is None:
            return
        if project.has_plan:
            projects.add_version(project, project.plan, label=f"before {label}")
        project.plan = plan_to_dict(plan)
        projects.save_project(project)
    except Exception:  # noqa: BLE001 — persistence must never fail the edit
        pass


def _apply_vision(job: dict, reports: list) -> tuple[list, str]:
    """Let Claude vision annotate ``reports`` IN PLACE; soft-fail by contract.

    Returns ``(vision_notes, vision_error)`` — exactly one of them is truthy
    (or both empty when vision produced no notes). A missing anthropic
    package / API key (``MonteurVisionError``) or an absent vision module
    degrades to ``([], "<message>")``: the scan/build carries on with the
    un-annotated reports, because vision is an upgrade, not a gate.

    Vision's own progress stages ("frames"/"vision"/"cache") are folded into
    a single job-progress stage ``"vision"`` — the UI shows one kind of
    "Claude is watching" line either way.

    ``monteur.vision.analyze_reports`` is deliberately resolved at CALL time
    via :func:`importlib.import_module` (which honours ``sys.modules``,
    unlike ``import a.b as c``'s parent-attribute shortcut), so tests can
    either ``monkeypatch.setattr("monteur.vision.analyze_reports", fake)``
    or replace the whole module in ``sys.modules`` with a fake.
    """

    def progress(index, total, name, stage):
        # vision runs TWO passes over the same moments: "frames" (local keyframe
        # extraction — no API, no tokens) then "vision" (the actual Claude
        # calls). Keep the job-level stage "vision" (status line + grouping), but
        # carry the real sub-phase so the UI can label them distinctly — a
        # second 1/N..N/N counter is the frame prep, NOT a wasteful re-run.
        entry = {
            "stage": "vision", "phase": stage,
            "index": index, "total": total, "name": name,
        }
        with _JOBS_LOCK:
            job["progress"].append(entry)

    import importlib

    try:
        vision = importlib.import_module("monteur.vision")
    except ImportError as exc:  # an older/partial install — same soft contract
        return [], f"vision support is not installed: {exc}"
    try:
        notes = vision.analyze_reports(reports, progress=progress)
    except vision.MonteurVisionError as exc:
        return [], str(exc)
    return list(notes or []), ""


def _run_scan_job(job: dict, folder: str, see: bool = False) -> None:
    """Daemon-thread body for POST /api/create/scan."""
    from monteur.media import MonteurMediaError
    from monteur.sift import SiftCancelled, sift_directory

    try:
        reports = sift_directory(
            folder, progress=_job_progress(job), cancel=job["cancel"]
        )
        if not reports:
            raise MonteurMediaError(f"no video files found in {folder}")
        _remember_scan(folder, reports)
        result: dict = {}
        if see:
            # After _remember_scan: the cache holds these same report objects,
            # so in-place annotation lands in the cache too — a build straight
            # after a see-scan reuses the ANNOTATED reports.
            notes, error = _apply_vision(job, reports)
            if error:
                result["vision_error"] = error
            else:
                result["vision_notes"] = notes
        # asdict AFTER vision so the clip payload includes the annotations.
        result["clips"] = [asdict(r) for r in reports]
        # Every successful scan (and rescan) kicks the background proxy
        # transcodes, so playback is smooth by the time the user reaches
        # the storyboard. Best-effort: proxies never fail a scan.
        try:
            result["proxies_job"] = _start_proxies_job(folder)["id"]
        except Exception:  # noqa: BLE001 — proxies are an upgrade, not a gate
            pass
        job["result"] = result
        job["state"] = "done"
    except SiftCancelled:
        job["state"] = "cancelled"
    except (MonteurMediaError, ValueError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _run_transcribe_job(job: dict, folder: str) -> None:
    """Daemon-thread body for POST /api/create/transcribe.

    Transcribes every clip in ``folder`` (whisper), writing a ``<clip>.json``
    sidecar next to each — the same signal ``monteur find`` searches as spoken
    words. Needs a whisper backend installed; without one, transcribe raises a
    MonteurTranscribeError whose message says how to install it, surfaced to the
    UI verbatim.
    """
    from dataclasses import asdict

    from monteur.transcribe import MonteurTranscribeError, transcribe_directory

    try:
        results = transcribe_directory(folder)
        written = 0
        for media, transcript in results.items():
            out = Path(media).with_suffix(".json")
            if out.exists():
                continue
            payload = {
                "segments": [asdict(s) | {"start": s.start, "end": s.end} for s in transcript.segments],
                "language": transcript.language,
            }
            out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            written += 1
        job["result"] = {"transcribed": len(results), "written": written}
        job["state"] = "done"
    except (MonteurTranscribeError, FileNotFoundError, ValueError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _start_clip_proxies_job(clips: list) -> dict:
    """Kick a background proxies job for a SPECIFIC list of clips; return it.

    The staged pool analyzes a subset, so proxies follow that subset (not a
    whole folder). Best-effort like the folder proxies job: it never gates.
    """
    import importlib

    job = _new_job("proxies")

    def run() -> None:
        from monteur.media import MonteurMediaError

        try:
            paths = [str(p) for p in clips]
            if not paths:
                raise MonteurMediaError("no clips to proxy")
            proxies = importlib.import_module("monteur.proxies")

            def progress(done: int, total: int, name: str) -> None:
                with _JOBS_LOCK:
                    job["progress"].append(
                        {"stage": "proxy", "index": done, "total": total, "name": name}
                    )

            made, errors = proxies.ensure_proxies(
                paths, progress=progress, cancel=job["cancel"]
            )
            if job["cancel"].is_set():
                job["state"] = "cancelled"
                return
            try:
                proxies.prune_proxies()
            except Exception:  # noqa: BLE001 — pruning must never fail the job
                pass
            job["result"] = {
                "ready": len(made),
                "total": len(paths),
                "errors": [
                    f"{Path(path).name}: {message}"
                    for path, message in errors.items()
                ],
            }
            job["state"] = "done"
        except (MonteurMediaError, ValueError) as exc:
            job["message"] = str(exc)
            job["state"] = "error"
        except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
            job["message"] = f"internal error: {exc}"
            job["state"] = "error"

    threading.Thread(
        target=run, name=f"monteur-proxies-{job['id']}", daemon=True
    ).start()
    return job


def _analyze_selected(job: dict, clips: list, project_id: str = "") -> list:
    """Sift each of ``clips`` (a chosen subset), with per-clip job progress.

    Reuses :func:`monteur.sift.analyze_clip` — the SAME engine a folder scan
    runs, one clip at a time — so the staged "analyze selected" is honest sift
    data. Cancellation is threaded into every ffmpeg call; a set flag raises
    out (SiftCancelled / MediaCancelled) and the caller marks the job
    cancelled.

    Each clip's report is remembered AND stored in the project the moment it
    finishes — INCREMENTALLY — so cancelling partway keeps every clip analyzed
    so far (and the pool greens up one clip at a time), instead of an all-or-
    nothing write at the end."""
    from monteur.sift import analyze_clip, annotate_context

    reports = []
    total = len(clips)
    for index, path in enumerate(clips, start=1):
        name = os.path.basename(str(path))
        with _JOBS_LOCK:
            job["progress"].append(
                {"index": index, "total": total, "name": name, "stage": "start"}
            )
        report = analyze_clip(str(path), cancel=job["cancel"])
        # the offline context passes (time-of-day + shot grammar) — the same
        # ones the folder scan runs, so the composer's coherence laws (a
        # day->night arc, no daylight POV mid-night, no re-cut of one scene)
        # get their signal from the Footage tab too. Best-effort, per moment.
        annotate_context([report])
        reports.append(report)
        _remember_clip_reports([report])          # sidecar + memory, per clip
        _save_project_analysis(project_id, [report])  # project store, incremental
        with _JOBS_LOCK:
            job["progress"].append(
                {
                    "index": index,
                    "total": total,
                    "name": name,
                    "stage": "done",
                    "usable_ratio": report.usable_ratio,
                }
            )
    return reports


def _save_project_analysis(project_id: str, reports: list) -> None:
    """Store reports (with any Claude labels) IN the project — best-effort.

    The analysis is part of the project: the storyboard build reads it back and
    never re-scans the pool. A failure here never fails the analyse job (the
    footage sidecar is already written); it just means the build would ask to
    analyse again."""
    if not project_id or not reports:
        return
    try:
        from monteur import projects

        project = projects.load_project(project_id)
        if project is not None:
            projects.save_reports(project, reports)
    except Exception:  # noqa: BLE001 — storing analysis is never a gate
        pass


def _run_analyze_job(job: dict, clips: list, see: bool = False, project_id: str = "") -> None:
    """Daemon body for POST /api/projects/<id>/analyze — sift a SUBSET.

    Produces the SAME result shape as the folder scan ({"clips", optional
    "vision_*", "proxies_job"}) so the Studio's scan panel + heartbeat +
    Cancel + reload-reattach drive it unchanged. Optionally runs the vision
    pass on the just-analyzed clips when ``see`` is set. The reports (with any
    labels) are stored IN the project so the build reads them back, never
    re-scanning."""
    from monteur.media import MediaCancelled, MonteurMediaError
    from monteur.sift import SiftCancelled

    try:
        reports = _analyze_selected(job, clips, project_id)
        result: dict = {}
        if see and reports:
            notes, error = _apply_vision(job, reports)
            if error:
                result["vision_error"] = error
            else:
                result["vision_notes"] = notes
            _remember_clip_reports(reports)  # annotations landed in place
        _save_project_analysis(project_id, reports)  # analysis lives in the project
        result["clips"] = [asdict(r) for r in reports]
        try:
            result["proxies_job"] = _start_clip_proxies_job(
                [r.path for r in reports]
            )["id"]
        except Exception:  # noqa: BLE001 — proxies are an upgrade, not a gate
            pass
        job["result"] = result
        job["state"] = "done"
    except (SiftCancelled, MediaCancelled):
        job["state"] = "cancelled"
    except (MonteurMediaError, ValueError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _run_see_job(job: dict, clips: list, project_id: str = "") -> None:
    """Daemon body for POST /api/projects/<id>/see — Claude-check a SUBSET.

    Vision on ONLY the chosen clips (typically the good ones judged after
    analysis). Reuses a clip's cached report when fresh; sifts any that were
    not analyzed yet, so this step stands alone. The labeled reports are stored
    IN the project. Same result shape + panel plumbing as the analyze/scan
    jobs."""
    from monteur.media import MediaCancelled, MonteurMediaError
    from monteur.sift import SiftCancelled, analyze_clip, annotate_context

    try:
        reports = []
        for path in clips:
            if job["cancel"].is_set():
                raise SiftCancelled()
            report = _cached_clip_report(str(path))
            if report is None:
                report = analyze_clip(str(path), cancel=job["cancel"])
            reports.append(report)
        # time-of-day + shot grammar (cached/idempotent — a report that was
        # already analyzed keeps its classes; a freshly sifted one gains them)
        annotate_context(reports)
        _remember_clip_reports(reports)
        result: dict = {}
        notes, error = _apply_vision(job, reports)
        if error:
            result["vision_error"] = error
        else:
            result["vision_notes"] = notes
        _remember_clip_reports(reports)  # annotations landed in place
        _save_project_analysis(project_id, reports)  # analysis lives in the project
        result["clips"] = [asdict(r) for r in reports]
        job["result"] = result
        job["state"] = "done"
    except (SiftCancelled, MediaCancelled):
        job["state"] = "cancelled"
    except (MonteurMediaError, ValueError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _sift_or_cached(job: dict, folder: str) -> tuple[list, bool]:
    """Reports for ``folder``: the scan cache when fresh, else a full sift.

    Returns ``(reports, cached)``. A cache hit appends the usual
    ``{"stage": "cache"}`` progress entry; a miss sifts with per-clip
    progress and refreshes the cache — the one sift-or-reuse path every
    job kind (build, pick, kit) shares.
    """
    from monteur.media import MonteurMediaError
    from monteur.sift import sift_directory

    reports = _cached_reports(folder)
    if reports is not None:
        # A cache hit after a see-scan already carries the annotations
        # (vision annotates the cached report objects in place) — no
        # second vision pass, no double spend.
        with _JOBS_LOCK:
            job["progress"].append(
                {"stage": "cache", "name": "using previous scan"}
            )
        return reports, True
    reports = sift_directory(
        folder, progress=_job_progress(job), cancel=job["cancel"]
    )
    if not reports:
        raise MonteurMediaError(f"no video files found in {folder}")
    _remember_scan(folder, reports)
    return reports, False


def _validate_arrangement(payload: dict) -> None:
    """400 (ApiError) on a structurally malformed 'arrangement' payload.

    Called by the build/kit handlers at REQUEST time, so a broken client
    payload never becomes a half-started job. Each scene must fit the
    engine format ({"clip", "start", optional "after"/"sfx"}; extra
    display keys like "end"/"label" ride along untouched). Unknown clip
    PATHS are a job-time error (the engine names them) because the
    footage is only known after the sift. A missing/empty arrangement is
    fine — the key is simply removed.
    """
    if "arrangement" not in payload:
        return
    from monteur.montage import ARRANGEMENT_SFX_KINDS, ARRANGEMENT_TRANSITIONS

    raw = payload.get("arrangement")
    if not raw:
        payload.pop("arrangement", None)
        return
    if not isinstance(raw, list):
        raise ApiError(400, "'arrangement' must be a list of scenes")
    for n, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ApiError(400, f"arrangement scene {n}: must be an object")
        clip = item.get("clip")
        if not isinstance(clip, str) or not clip.strip():
            raise ApiError(
                400, f"arrangement scene {n}: missing 'clip' (the clip path)"
            )
        try:
            start = float(item.get("start"))
        except (TypeError, ValueError):
            raise ApiError(
                400, f"arrangement scene {n}: 'start' must be a number (seconds)"
            )
        if start < 0:
            raise ApiError(
                400, f"arrangement scene {n}: 'start' must not be negative"
            )
        after = item.get("after")
        if after is not None:
            transition = after.get("transition") if isinstance(after, dict) else after
            if transition not in ARRANGEMENT_TRANSITIONS:
                valid = ", ".join(ARRANGEMENT_TRANSITIONS)
                raise ApiError(
                    400,
                    f"arrangement scene {n}: 'after' transition must be one "
                    f"of {valid}",
                )
        sfx_kind = item.get("sfx")
        if sfx_kind is not None and sfx_kind != "":
            if sfx_kind not in ARRANGEMENT_SFX_KINDS:
                valid = ", ".join(ARRANGEMENT_SFX_KINDS)
                raise ApiError(
                    400, f"arrangement scene {n}: 'sfx' must be one of {valid}"
                )


def _validate_platform(payload: dict) -> None:
    """400 on an unknown "platform" BEFORE the job starts (build/kit).

    The actual resolution — canvas forced, "short" style unless an explicit
    style was chosen, length capped — happens in :func:`_plan_from_payload`
    via :func:`monteur.montage.resolve_platform`; here only the key is
    checked so a typo never becomes a half-started job. A missing/empty
    platform is fine — the key is simply removed.
    """
    if "platform" not in payload:
        return
    platform = payload.get("platform")
    if not platform:
        payload.pop("platform", None)
        return
    from monteur.montage import PLATFORMS

    if platform not in PLATFORMS:
        valid = ", ".join(PLATFORMS)
        raise ApiError(
            400, f"unknown platform {platform!r} — valid platforms: {valid}"
        )


def _fold_treatment_into_payload(payload: dict) -> None:
    """Fold a confirmed Regie-Vorschlag (``payload['treatment']``) into a build.

    The editable chips the user accepted become the composer's directive: the
    treatment weaves into the ``brief`` (so the composer is always TOLD the
    format / pacing / mood instead of falling back to a generic default) and
    fills ``style`` / ``platform`` / ``max_duration`` ONLY where the user left
    them open — an explicit control the user set always wins. A missing or
    empty treatment is a no-op. In place; safe to call unconditionally.
    """
    treatment = payload.get("treatment")
    if not isinstance(treatment, dict) or not treatment:
        return
    from monteur import treatment as _treatment_mod

    payload["brief"] = _treatment_mod.treatment_to_brief(
        treatment, str(payload.get("brief") or "")
    )
    # "auto" is the build's open/unset sentinel — the treatment's style fills it;
    # a style the user explicitly chose (travel, trailer, …) always wins.
    if str(payload.get("style") or "").strip() in ("", "auto") and treatment.get("style"):
        payload["style"] = str(treatment["style"])
    if not str(payload.get("platform") or "").strip() and treatment.get("platform"):
        payload["platform"] = str(treatment["platform"])
    if not payload.get("max_duration"):
        secs = _treatment_mod.treatment_max_seconds(treatment)
        if secs:
            payload["max_duration"] = secs


def _plan_from_payload(job: dict, payload: dict):
    """Shared plan construction for build and kit jobs.

    Runs the whole sift-or-cache -> vision -> music -> plan_montage pipeline
    on a build-shaped payload and returns ``(plan, reports, music, vision)``
    where ``vision`` is ``{"ran": bool, "notes": list, "error": str}``.
    Build and kit MUST stay on this one path so a kit always plans exactly
    the montage the build would have produced.
    """
    from monteur.montage import CHRONOLOGICAL, plan_montage
    from monteur.sift import SiftCancelled

    folder = payload.get("folder", "")
    music_path = payload.get("music") or ""
    see = bool(payload.get("see"))
    vision = {"ran": False, "notes": [], "error": ""}

    # Regie-Vorschlag: a confirmed treatment folds into the composer's brief and
    # fills style / platform / length where the user left them open (see helper).
    _fold_treatment_into_payload(payload)

    # Platform preset ("What are you making?"): resolved HERE, at the
    # caller layer — plan_montage never takes a platform. The payload is
    # updated in place so the same resolved canvas reaches the timeline
    # renderer and the drafts autosave. Precedence lives in ONE place
    # (monteur.montage.resolve_platform): the platform always sets the
    # canvas and caps the length; an explicit style wins over the preset's
    # "short", with a note explaining what was kept.
    platform_notes: list[str] = []
    if payload.get("platform"):
        from monteur.montage import resolve_platform

        raw_max = payload.get("max_duration")
        resolved = resolve_platform(
            str(payload["platform"]),
            style=payload.get("style") or None,
            canvas=payload.get("canvas") or None,
            max_duration=float(raw_max) if raw_max else None,
        )
        if resolved["style"]:
            payload["style"] = resolved["style"]
        payload["canvas"] = resolved["canvas"]
        if resolved["max_duration"] is not None:
            payload["max_duration"] = resolved["max_duration"]
        platform_notes = resolved["notes"]

    # The storyboard build reads the project's OWN stored analysis — the sift
    # + Claude labels saved when you analyzed the pool — and NEVER re-scans the
    # media pool here (that is the Footage tab's job). Labels are baked into the
    # stored reports, so there is no re-watch. Nothing analyzed -> a clear "go
    # analyze first" message, not a scan.
    project = None
    project_id = str(payload.get("project") or "")
    if project_id:
        from monteur import projects

        project = projects.load_project(project_id)
        if project is None:
            raise ValueError(f"unknown project {project_id!r}")
        reports = projects.load_reports(project)
        if not reports:
            raise ValueError(
                "No analyzed footage yet — open the Footage tab, analyze your "
                "clips (and Claude-check them), then build the storyboard. The "
                "storyboard is built from your analyzed clips, not a fresh scan."
            )
        # the editor's own per-clip notes (Clips review step) ride onto the
        # reports so the composer reads them (keyed by path, survives re-sift)
        notes_by_path = project.clip_notes or {}
        if notes_by_path:
            for report in reports:
                note = notes_by_path.get(os.path.abspath(report.path))
                if note:
                    report.user_note = str(note)
        # ...and the finer per-MOMENT marks (Moments inspector) ride onto the
        # individual moments (keyed by path + start): the editor's note and
        # rating land on each moment for the composer, and EXCLUDED moments are
        # dropped here so they can never reach the plan or the cut.
        moment_notes = project.moment_notes or {}
        moment_ratings = project.moment_ratings or {}
        moment_excludes = project.moment_excludes or {}
        if moment_notes or moment_ratings:
            for report in reports:
                for m in report.moments:
                    key = _moment_key(report.path, m.start)
                    if moment_notes.get(key):
                        m.user_note = str(moment_notes[key])
                    if moment_ratings.get(key):
                        m.user_rating = int(moment_ratings[key])
                        # a rating also nudges the heuristic score, so a loved
                        # moment wins (and a disliked one loses) even on a plain
                        # cut without the composer. 3 stars = neutral (no change)
                        m.score = max(0.0, min(1.0,
                            float(m.score) + (m.user_rating - 3) * 0.1))
        if moment_excludes:
            for report in reports:
                report.moments = [
                    m for m in report.moments
                    if not moment_excludes.get(_moment_key(report.path, m.start))
                ]
        with _JOBS_LOCK:
            job["progress"].append(
                {"stage": "cache", "name": f"using {len(reports)} analyzed clips"}
            )
    else:
        # Legacy folder path (CLI / clients without a project): sift-or-cache.
        reports, cached = _sift_or_cached(job, folder)
        if not cached and see:  # fresh sift: annotate before planning (soft-fail)
            vision["ran"] = True
            vision["notes"], vision["error"] = _apply_vision(job, reports)
    if job["cancel"].is_set():
        raise SiftCancelled("cancelled")

    music = None
    if music_path:
        if project is not None:
            from monteur import projects

            music = projects.load_music(project, music_path)  # no re-listen
        if music is None:
            from monteur.music import analyze_music

            with _JOBS_LOCK:
                job["progress"].append(
                    {"stage": "music", "name": Path(music_path).name}
                )
            music = analyze_music(music_path)
            if project is not None:
                from monteur import projects

                projects.save_music(project, music)  # persist for build + director
    if job["cancel"].is_set():
        raise SiftCancelled("cancelled")

    # allow_repeats / cut_lead / sfx / audio are forwarded ONLY when the
    # client sent them: a montage engine without those parameters then
    # raises a loud TypeError (surfaced as a job error) instead of silently
    # dropping the user's choice.
    plan_kwargs: dict = {
        "order": payload.get("order") or CHRONOLOGICAL,
        "style": payload.get("style") or "auto",
    }
    max_duration = payload.get("max_duration")
    plan_kwargs["max_duration"] = float(max_duration) if max_duration else None
    if "allow_repeats" in payload:
        plan_kwargs["allow_repeats"] = bool(payload["allow_repeats"])
    if "cut_lead" in payload:
        plan_kwargs["cut_lead"] = float(payload["cut_lead"])
    if "sfx" in payload:
        plan_kwargs["sfx"] = bool(payload["sfx"])
    elements_dir = str(payload.get("elements") or "")
    if elements_dir:
        # A sound-elements folder rides on the SFX layer — the cues are the
        # places the elements go. An explicit sfx=false contradicts it.
        if plan_kwargs.get("sfx") is False:
            raise ValueError(
                "a sound-elements folder needs the SFX layer — enable "
                "'Plan an SFX layer' or clear the elements folder"
            )
        plan_kwargs["sfx"] = True
    if payload.get("pace"):
        plan_kwargs["pace"] = float(payload["pace"])
    if payload.get("transitions"):
        plan_kwargs["transitions"] = payload["transitions"]
    if payload.get("arrangement"):
        # The editor's own scene order (the Arrange step). The handler
        # already 400-ed malformed payloads; unknown clip paths surface
        # here as the engine's own ValueError -> a clear job error.
        plan_kwargs["arrangement"] = payload["arrangement"]
    if payload.get("music_window") is not None:
        # The adaptive music window override, passed through untouched —
        # plan_montage validates and snaps it (bad values surface as the
        # engine's own ValueError -> a clear job error). Without it the
        # tool decides when the music enters.
        plan_kwargs["music_window"] = payload["music_window"]
    if payload.get("music_flow"):
        # Deliberate silence vs continuous song — passed through untouched;
        # plan_montage validates the mode (unknown values surface as the
        # engine's own ValueError -> a clear job error). Absent = the
        # engine's "deliberate" default.
        plan_kwargs["music_flow"] = str(payload["music_flow"])
    # Learned preferences (blueprint 4.3): fold the abstract signals the
    # user's earlier corrections established into THIS plan as small casting
    # tie-breakers. An empty store returns None, so a fresh user's cut is
    # byte-identical to today. Opt-out with {"learn": false} (the CLI kit /
    # deterministic-fixture callers set it to keep byte-parity explicit).
    if payload.get("learn", True):
        from monteur import preferences

        bias = preferences.casting_bias()
        if bias is not None:
            plan_kwargs["casting_bias"] = bias
    if payload.get("ai_cut"):
        # Claude composes the cut (monteur.compose): the engine still builds
        # the exact grid plan_montage would, then ONE Claude completion casts
        # the slots and titles the act breaks. The user explicitly asked for
        # the AI cut, so strict=True — a MonteurAIError propagates and fails
        # the job with its own actionable message (no silent downgrade; the
        # CLI's --ai-cut keeps the graceful fallback instead).
        from monteur.compose import compose_montage

        # The compose is one long Claude call. Stream its answer into the job's
        # progress so the storyboard build shows the cut being written live
        # (character count + a running tail) instead of a frozen 90s wait. The
        # entry dict is mutated in place; _job_view snapshots it on each poll.
        with _JOBS_LOCK:
            compose_entry = {
                "stage": "compose",
                "name": "Claude is composing the cut",
                "chars": 0,
                "thinking_chars": 0,
            }
            job["progress"].append(compose_entry)
        _script_chunks: list[str] = []
        _think_chunks: list[str] = []

        def _on_compose_text(chunk: str) -> None:
            _script_chunks.append(chunk)
            text = "".join(_script_chunks)
            with _JOBS_LOCK:
                compose_entry["chars"] = len(text)
                compose_entry["script"] = text[-4000:]  # a live tail, capped

        def _on_compose_thinking(chunk: str) -> None:
            # the reasoning phase (often the bulk on the CLI backend): its live
            # tail is the build's honest motion before the answer starts landing
            _think_chunks.append(chunk)
            text = "".join(_think_chunks)
            with _JOBS_LOCK:
                compose_entry["thinking_chars"] = len(text)
                compose_entry["thinking"] = text[-2000:]

        plan = compose_montage(
            reports,
            music,
            brief=str(payload.get("brief") or ""),
            strict=True,
            on_text=_on_compose_text,
            on_thinking=_on_compose_thinking,
            **plan_kwargs,
        )
    else:
        plan = plan_montage(reports, music, **plan_kwargs)
    if not plan.entries:
        raise ValueError("no usable material found — check the scan results")
    if platform_notes:
        plan.notes.extend(platform_notes)
    if elements_dir:
        _apply_elements(job, plan, music, elements_dir)
    return plan, reports, music, vision


def _apply_elements(job: dict, plan, music, elements_dir: str) -> None:
    """Scan the sound library and place its snippets into the plan's SFX layer.

    Shared by build/kit (via ``_plan_from_payload``) and revise. Adds an
    ``"elements"`` progress stage, then appends
    :func:`monteur.elements.assign_elements`' notes to the plan so the
    result card says what landed where. A missing folder or missing media
    dependencies raise :class:`MonteurMediaError` — the job's normal error
    path, with the scanner's own actionable message.
    """
    from monteur.elements import assign_elements, scan_elements

    with _JOBS_LOCK:
        job["progress"].append(
            {"stage": "elements", "name": Path(elements_dir).name or elements_dir}
        )
    plan.notes.extend(assign_elements(plan, music, scan_elements(elements_dir)))


# The build-payload keys the autosave remembers as the draft's "settings" —
# exactly what the browser needs to restore its wizard controls on resume.
_DRAFT_SETTING_KEYS = (
    "audio", "order", "style", "canvas", "transitions", "fps", "format",
    "max_duration", "pace", "allow_repeats", "sfx", "cut_lead", "see",
    "ai_cut", "brief", "elements", "platform", "arrangement", "music_window",
    "music_flow",
)


def _autosave_draft(payload: dict, result: dict) -> None:
    """Best-effort: remember a job's fresh cut in the drafts autosave slot.

    Called from the SUCCESS path of the build, revise and direct-apply jobs
    so a browser reload can always offer "Continue where you left off" with
    the last good cut (:mod:`monteur.drafts`, single ``"autosave"`` slot).
    Strictly an extra: any failure here (unwritable home directory, a
    malformed payload) is swallowed — an autosave must never fail the job
    that produced a perfectly good result.
    """
    try:
        from monteur import drafts

        plan_json = result.get("plan_json")
        if not isinstance(plan_json, dict):
            return
        drafts.save_draft(
            {
                "autosave": True,
                "name": "Auto-saved cut",
                "folder": payload.get("folder", ""),
                "music": payload.get("music")
                or str(plan_json.get("music_path") or ""),
                "settings": {
                    key: payload[key]
                    for key in _DRAFT_SETTING_KEYS
                    if key in payload
                },
                "plan_json": plan_json,
                # The song's tempo is not derivable from the plan (export
                # never re-listens) — remember it so a resumed card can
                # still show the BPM tile the build showed.
                "tempo": float((result.get("plan") or {}).get("tempo") or 0),
            }
        )
    except Exception:  # noqa: BLE001 — autosave is best-effort by contract
        pass


def _run_build_job(job: dict, payload: dict) -> None:
    """Daemon-thread body for POST /api/create/build."""
    from monteur.ai import MonteurAIError
    from monteur.media import MonteurMediaError
    from monteur.montage import montage_to_timeline, plan_to_dict
    from monteur.sift import SiftCancelled
    from monteur.io import write_edl, write_fcpxml

    try:
        plan, reports, music, vision = _plan_from_payload(job, payload)

        fps = float(payload.get("fps") or 25)
        # Audio mode resolution must not drop the ball for no-music plans:
        # without an explicit mode, a plan without music renders its clips'
        # own sound (plus placed SFX) instead of failing on a missing song.
        audio = payload.get("audio") or ("music" if plan.music_path else "original")
        timeline_kwargs: dict = {"audio": audio}
        if payload.get("canvas"):
            timeline_kwargs["canvas"] = payload["canvas"]
        timeline = montage_to_timeline(plan, fps=fps, **timeline_kwargs)
        fmt = (payload.get("format") or "fcpxml").lower()
        if fmt == "edl":
            content, filename = write_edl(timeline), "monteur_montage.edl"
        else:
            content, filename = write_fcpxml(timeline), "monteur_montage.fcpxml"
        result = {
            "filename": filename,
            "content": content,
            "plan": {
                "duration": plan.duration,
                "cuts": len(plan.entries),
                "tempo": music.tempo if music is not None else 0,
                "notes": plan.notes,
            },
            # The FULL plan, in the save-plan format — the browser hands it
            # back to /api/create/revise so the cut can be iterated on.
            "plan_json": plan_to_dict(plan),
        }
        if vision["error"]:
            result["vision_error"] = vision["error"]
        elif vision["ran"]:
            result["vision_notes"] = vision["notes"]
        _autosave_draft(payload, result)  # best-effort; never fails the job
        job["result"] = result
        job["state"] = "done"
    except SiftCancelled:
        job["state"] = "cancelled"
    except (MonteurAIError, MonteurMediaError, ValueError) as exc:
        # MonteurAIError only ever escapes here when "ai_cut" was explicitly
        # requested — the user asked for the AI cut and must see why it
        # failed instead of getting a silent heuristic downgrade.
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _run_series_job(job: dict, payload: dict) -> None:
    """Daemon-thread body for POST /api/create/series.

    One tour -> up to ``series`` genuinely different Shorts, zero moment
    repeated across the set (:func:`monteur.series.plan_series`). Reuses the
    exact sift/vision/music pipeline the single-cut build runs
    (:func:`_plan_from_payload`) so a series sees the same material a cut
    would; the single montage that call also produces is discarded. Each
    short carries its own full plan (save-plan format), its seed moment (for
    the picker's label + thumbnail) and the "why this short" note.
    """
    from monteur.ai import MonteurAIError
    from monteur.media import MonteurMediaError
    from monteur.montage import BEST_FIRST, plan_to_dict
    from monteur.series import plan_series
    from monteur.sift import SiftCancelled

    try:
        try:
            count = int(payload.get("series") or 0)
        except (TypeError, ValueError):
            raise ValueError("'series' must be a whole number of videos")
        if count < 2:
            raise ValueError("a series needs at least 2 videos")
        count = min(count, 8)  # a sane upper bound for one tour
        # reports + music from the shared pipeline (the single plan it also
        # builds is not used here — plan_series builds one plan per short)
        _plan, reports, music, vision = _plan_from_payload(job, payload)
        kwargs: dict = {}
        if payload.get("order") == "best_first":
            kwargs["order"] = BEST_FIRST
        if payload.get("transitions"):
            kwargs["transitions"] = str(payload["transitions"])
        if payload.get("allow_repeats") is not None:
            kwargs["allow_repeats"] = bool(payload["allow_repeats"])
        if payload.get("fps"):
            kwargs["fps"] = float(payload["fps"])
        # shorts are vertical by default; honour an explicit canvas choice
        canvas = str(payload.get("canvas") or "vertical-uhd")
        shorts = plan_series(reports, music, count=count, canvas=canvas, **kwargs)
        if not shorts:
            raise ValueError(
                "no usable moments for a series — the footage needs a few "
                "distinct strong moments to seed different Shorts"
            )
        out = []
        for i, short in enumerate(shorts):
            out.append({
                "index": i,
                "plan_json": plan_to_dict(short.plan),
                "seed": {
                    "clip_path": short.seed.clip_path,
                    "start": short.seed.start,
                    "end": short.seed.end,
                    "label": short.seed.label,
                    "score": short.seed.score,
                },
                "note": short.note,
                "canvas": short.canvas,
                "duration": short.plan.duration,
                "cuts": len(short.plan.entries),
            })
        result = {
            "shorts": out,
            "count": len(out),
            "requested": count,
            "tempo": music.tempo if music is not None else 0,
        }
        if vision["error"]:
            result["vision_error"] = vision["error"]
        elif vision["ran"]:
            result["vision_notes"] = vision["notes"]
        job["result"] = result
        job["state"] = "done"
    except SiftCancelled:
        job["state"] = "cancelled"
    except (MonteurAIError, MonteurMediaError, ValueError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _project_reports(project) -> tuple[list, list]:
    """Recall the PERSISTED sift reports for every clip in a project's pool.

    Returns ``(reports, missing)`` — ``missing`` is the pooled clip paths with
    no persisted sift yet (never analyzed). Never sifts here: the project ->
    shorts path reuses the analysis the project already saved, so a re-opened
    project makes shorts instantly and never re-crunches.
    """
    reports: list = []
    missing: list = []
    for clip in _resolve_pool(project)["clips"]:
        ab = clip["path"]
        report = _cached_clip_report(ab)
        if report is None:
            missing.append(ab)
        else:
            reports.append(report)
    return reports, missing


def _run_project_series_job(job: dict, project_id: str, payload: dict) -> None:
    """Daemon-thread body for POST /api/projects/<id>/series.

    Turns a finished long-form CUT into up to ``series`` genuinely different
    vertical Shorts, EXTRACTED from the beats the edit actually used
    (:func:`monteur.series.series_from_edit`). Reuses the project's persisted
    sift — no re-scan — and the long form's own song when it still exists.
    Each short carries its own full plan (save-plan format), its seed moment
    and a "why this short" note, the same shape the folder series returns.
    """
    from monteur import projects
    from monteur.media import MonteurMediaError
    from monteur.montage import BEST_FIRST, plan_to_dict
    from monteur.series import series_from_edit

    try:
        project = projects.load_project(project_id)
        if project is None:
            job["message"] = f"unknown project {project_id!r}"
            job["state"] = "error"
            return
        try:
            count = int(payload.get("series") or payload.get("count") or 0)
        except (TypeError, ValueError):
            raise ValueError("'series' must be a whole number of videos")
        if count < 2:
            raise ValueError("a series needs at least 2 videos")
        count = min(count, 8)

        reports, missing = _project_reports(project)
        if not reports:
            raise ValueError(
                "this project's footage isn't analyzed yet — open the Footage "
                "tab and analyze the clips first, then make Shorts"
            )

        # Reuse the long form's song when it still exists, so the shorts are
        # beat-synced; otherwise cut each short to the clips' own sound.
        music = None
        plan = project.plan if isinstance(project.plan, dict) else None
        music_path = str((plan or {}).get("music_path") or "")
        if music_path and os.path.isfile(music_path):
            try:
                from monteur.music import analyze_music

                music = analyze_music(music_path)
            except MonteurMediaError:
                music = None

        kwargs: dict = {}
        if payload.get("order") == "best_first":
            kwargs["order"] = BEST_FIRST
        if payload.get("transitions"):
            kwargs["transitions"] = str(payload["transitions"])
        if payload.get("fps"):
            kwargs["fps"] = float(payload["fps"])
        canvas = str(payload.get("canvas") or "vertical-uhd")

        shorts, from_edit = series_from_edit(
            reports, plan, music, count=count, canvas=canvas, **kwargs
        )
        if not shorts:
            raise ValueError(
                "no usable moments for a series — the cut needs a few distinct "
                "strong moments to seed different Shorts"
            )
        out = []
        for i, short in enumerate(shorts):
            out.append({
                "index": i,
                "plan_json": plan_to_dict(short.plan),
                "seed": {
                    "clip_path": short.seed.clip_path,
                    "start": short.seed.start,
                    "end": short.seed.end,
                    "label": short.seed.label,
                    "score": short.seed.score,
                },
                "note": short.note,
                "canvas": short.canvas,
                "duration": short.plan.duration,
                "cuts": len(short.plan.entries),
            })
        result = {
            "shorts": out,
            "count": len(out),
            "requested": count,
            "from_edit": from_edit,
            "analyzed": len(reports),
            "missing": len(missing),
            "tempo": music.tempo if music is not None else 0,
        }
        job["result"] = result
        job["state"] = "done"
    except (MonteurMediaError, ValueError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _save_series_shorts(project, shorts: list) -> list:
    """Persist chosen Shorts as CHILD projects referencing the same footage.

    Each ``{"plan_json", "label"?}`` becomes a new "cut" project named after
    the parent, carrying the parent's media pool (so a short can be re-opened,
    refined and exported on its own) and a note linking it home. Returns the
    created projects' ``{"id","name"}`` — the Studio opens them from there.
    """
    from monteur import projects

    created: list = []
    total = len(shorts)
    for i, item in enumerate(shorts, start=1):
        if not isinstance(item, dict):
            continue
        plan_json = item.get("plan_json")
        if not isinstance(plan_json, dict) or not plan_json:
            continue
        label = str(item.get("label") or "").strip() or f"Short {i} of {total}"
        name = f"{project.name} — {label}"
        note = f"Short cut from “{project.name}” (project {project.id})"
        child = projects.create_project(
            name,
            media_pool=[dict(e) for e in project.media_pool],
            plan=plan_json,
            notes=[note],
            options={"parent_project": project.id, "series_short": True},
            type="cut",
        )
        created.append({"id": child.id, "name": child.name})
    return created


def _run_pick_job(job: dict, payload: dict) -> None:
    """Daemon-thread body for POST /api/create/pick (rank candidate songs)."""
    from monteur.media import MonteurMediaError
    from monteur.pick import list_songs, rank_songs
    from monteur.sift import SiftCancelled

    folder = payload.get("folder", "")
    music_dir = payload.get("music_dir", "")
    try:
        # Check the songs before the (slow) sift so a wrong path fails fast.
        if not Path(music_dir).is_dir():
            raise MonteurMediaError(
                f"{music_dir} is not a folder — point Monteur at the folder "
                "holding your candidate songs"
            )
        if not list_songs(music_dir):
            raise MonteurMediaError(
                f"no audio files found in {music_dir} — looking for "
                ".mp3, .wav, .m4a, .aac, .flac or .ogg"
            )
        reports, _ = _sift_or_cached(job, folder)
        if job["cancel"].is_set():
            raise SiftCancelled("pick cancelled")

        max_duration = payload.get("max_duration")
        target = float(max_duration) if max_duration else None

        def progress(index, total, name):
            entry = {"stage": "song", "index": index, "total": total, "name": name}
            with _JOBS_LOCK:
                job["progress"].append(entry)

        ratings = rank_songs(
            reports, music_dir, target_duration=target, progress=progress
        )
        job["result"] = {
            "ranking": [
                {
                    "path": r.path,
                    "name": Path(r.path).name,
                    "score": r.score,
                    "tempo": r.tempo,
                    "duration": r.duration,
                    "parts": dict(r.parts),
                    "reasons": list(r.reasons),
                }
                for r in ratings
            ]
        }
        job["state"] = "done"
    except SiftCancelled:
        job["state"] = "cancelled"
    except (MonteurMediaError, ValueError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


# The kit endpoint returns thumbnails inline; cap how many JPEGs get
# base64-encoded into one job result (publish_kit's own default is also 6).
_KIT_MAX_THUMBS = 6


def _run_kit_job(job: dict, payload: dict) -> None:
    """Daemon-thread body for POST /api/create/kit (plan + publish kit)."""
    from monteur.ai import MonteurAIError
    from monteur.media import MonteurMediaError
    from monteur.sift import SiftCancelled

    kit_dir = payload.get("kit_dir", "")
    try:
        # The exact plan path a build takes — see _plan_from_payload.
        plan, reports, _music, vision = _plan_from_payload(job, payload)

        with _JOBS_LOCK:
            job["progress"].append({"stage": "kit", "name": "writing publish kit"})
        from monteur.publish import publish_kit

        try:
            notes = publish_kit(plan, reports, kit_dir, brief="")
        except Exception as exc:  # noqa: BLE001 — surfaced as a clear job error
            raise ValueError(f"could not write the publish kit: {exc}")

        kit_path = Path(kit_dir)
        publish_md = kit_path / "publish.md"
        thumbs = []
        thumb_dir = kit_path / "thumbs"
        if thumb_dir.is_dir():
            import base64

            for thumb in sorted(thumb_dir.glob("*.jpg"))[:_KIT_MAX_THUMBS]:
                try:
                    data = thumb.read_bytes()
                except OSError:
                    continue  # a vanished thumbnail loses its preview, not the kit
                thumbs.append(
                    {
                        "name": thumb.name,
                        "data_b64": base64.b64encode(data).decode("ascii"),
                    }
                )
        result = {
            "kit_dir": str(kit_path.resolve()),
            "notes": list(notes),
            "publish_md": (
                publish_md.read_text(encoding="utf-8") if publish_md.exists() else ""
            ),
            "thumbs": thumbs,
        }
        if vision["error"]:
            result["vision_error"] = vision["error"]
        elif vision["ran"]:
            result["vision_notes"] = vision["notes"]
        job["result"] = result
        job["state"] = "done"
    except SiftCancelled:
        job["state"] = "cancelled"
    except (MonteurAIError, MonteurMediaError, ValueError) as exc:
        # MonteurAIError: the kit plans exactly like a build, so an explicit
        # "ai_cut" failure surfaces here with its actionable message too.
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _run_revise_job(job: dict, payload: dict) -> None:
    """Daemon-thread body for POST /api/create/revise (the revision loop).

    Mirrors ``monteur revise`` (cli.cmd_revise) exactly: the plan file/dict
    stores the cut, not the run flags, so the re-plan recovers what it can —
    the style from the plan's own notes (:func:`monteur.revise.
    style_from_plan`), the length from the plan itself (authoritative;
    ``allow_repeats=True`` keeps the repetition guard from re-capping it),
    and the SFX layer from whether cues were planned. The footage is
    re-sifted through the same scan cache as build/pick/kit, and the plan's
    OWN music is re-analyzed when it has one. Placed sound elements survive:
    files carry over onto same-kind/same-time cues (untouched regions), and
    a payload ``"elements"`` folder re-places the replanned rest.
    """
    from monteur.media import MonteurMediaError
    from monteur.montage import montage_to_timeline, plan_from_dict, plan_to_dict
    from monteur.revise import parse_revision, revise_plan, style_from_plan
    from monteur.sift import SiftCancelled
    from monteur.io import write_edl, write_fcpxml

    try:
        plan = plan_from_dict(payload.get("plan_json") or {})  # bad -> ValueError
        if not plan.entries:
            raise ValueError("the plan has no entries — nothing to revise")
        try:
            pins = [float(t) for t in payload.get("pins") or []]
        except (TypeError, ValueError):
            raise ValueError("'pins' must be a list of record times in seconds")
        audio = payload.get("audio") or ("music" if plan.music_path else "original")
        if not plan.music_path and audio != "original":
            raise ValueError(f"the plan has no music; audio mode {audio!r} needs a song")

        revision = parse_revision(str(payload.get("brief") or ""))

        reports, _cached = _sift_or_cached(job, payload.get("folder", ""))
        if job["cancel"].is_set():
            raise SiftCancelled("cancelled")

        music = None
        if plan.music_path:
            from monteur.music import analyze_music

            with _JOBS_LOCK:
                job["progress"].append(
                    {"stage": "music", "name": Path(plan.music_path).name}
                )
            music = analyze_music(plan.music_path)
        if job["cancel"].is_set():
            raise SiftCancelled("cancelled")

        revise_kwargs: dict = {}
        if payload.get("music_flow"):
            # A continuous-song build stays continuous through the revision
            # (the plan file stores the cut, not the run flags — the browser
            # re-sends the build's own choice; plan_montage validates it).
            revise_kwargs["music_flow"] = str(payload["music_flow"])
        revised = revise_plan(
            plan, reports, music, revision, pinned=pins,
            style=style_from_plan(plan),
            max_duration=plan.duration,
            allow_repeats=True,
            sfx=bool(plan.sfx),
            **revise_kwargs,
        )
        if not revised.entries:
            raise ValueError(
                "the revision left no entries — check the scan results"
            )

        # Placed sound elements survive the revision: untouched regions get
        # their files back (same kind + time), and when the browser still
        # knows the library folder the replanned regions are re-placed too.
        if any(cue.file for cue in plan.sfx):
            from monteur.elements import carry_element_files

            carried = carry_element_files(plan, revised)
            if carried and not payload.get("elements"):
                revised.notes.append(
                    f"{carried} sound element{'s' if carried != 1 else ''} "
                    "carried over from the previous cut"
                )
        if payload.get("elements"):
            _apply_elements(job, revised, music, str(payload["elements"]))

        fps = float(payload.get("fps") or 25)
        timeline_kwargs: dict = {"audio": audio}
        if payload.get("canvas"):
            timeline_kwargs["canvas"] = payload["canvas"]
        timeline = montage_to_timeline(revised, fps=fps, **timeline_kwargs)
        fmt = (payload.get("format") or "fcpxml").lower()
        if fmt == "edl":
            content, filename = write_edl(timeline), "monteur_montage.edl"
        else:
            content, filename = write_fcpxml(timeline), "monteur_montage.fcpxml"
        result = {
            "filename": filename,
            "content": content,
            "plan": {
                "duration": revised.duration,
                "cuts": len(revised.entries),
                "tempo": music.tempo if music is not None else 0,
                "notes": revised.notes,
            },
            # The revised plan in the same save format, so revisions chain.
            "plan_json": plan_to_dict(revised),
            # One line saying how the instruction was read — honesty first.
            "rationale": revision.rationale,
        }
        _autosave_draft(payload, result)  # best-effort; never fails the job
        job["result"] = result
        job["state"] = "done"
    except SiftCancelled:
        job["state"] = "cancelled"
    except (MonteurMediaError, ValueError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _run_direct_job(job: dict, payload: dict) -> None:
    """Daemon-thread body for POST /api/create/direct (director's notes).

    Mirrors ``monteur direct`` (cli.cmd_direct): rebuild the plan from the
    browser's ``"plan_json"``, re-sift the folder — or every folder in a
    ``"folders"`` list (the Movie card sends its assigned scene folders) —
    through the same scan cache as build/revise (a cache hit after a
    see-scan carries the vision annotations, which is exactly what makes
    the review sharp), re-analyze
    the plan's own music — the payload's ``"music"`` wins when set — and
    ask :func:`monteur.director.direct_cut` for the review. The review is
    a GATE like the movie blueprint: a ``MonteurAIError`` fails the job
    with its own actionable message (it already explains keys/CLI/logins).
    The plan itself is returned UNCHANGED — applying is a separate,
    explicit step (:func:`_run_direct_apply_job`).

    ``direct_cut`` is resolved at CALL time via the ``from``-import below,
    so tests can monkeypatch it on :mod:`monteur.director` without any AI
    backend.
    """
    from monteur.ai import MonteurAIError
    from monteur.media import MonteurMediaError
    from monteur.montage import plan_from_dict, plan_to_dict
    from monteur.sift import SiftCancelled

    try:
        plan = plan_from_dict(payload.get("plan_json") or {})  # bad -> ValueError
        if not plan.entries:
            raise ValueError("the plan has no entries — nothing to review")

        # Director's notes analyzes the TIMELINE as built — it reads the
        # project's stored analysis (the sift + Claude labels saved when you
        # analyzed the pool) and its persisted music, so it NEVER re-sifts the
        # footage or re-listens to the song. The legacy/CLI path (no project)
        # still sifts the folder(s).
        project = None
        project_id = str(payload.get("project") or "")
        if project_id:
            from monteur import projects

            project = projects.load_project(project_id)

        if project is not None:
            reports = projects.load_reports(project)
            if not reports:
                raise ValueError(
                    "no analyzed footage yet — analyze your clips in the "
                    "Footage tab, then ask for director's notes"
                )
            with _JOBS_LOCK:
                job["progress"].append(
                    {"stage": "cache", "name": "reading your composed timeline"}
                )
        else:
            # One folder (the Create card) or several (the Movie card sends
            # every assigned scene folder): sift each once, concatenate.
            folders = [
                str(f).strip() for f in (payload.get("folders") or []) if str(f).strip()
            ] or [payload.get("folder", "")]
            reports = []
            for folder in dict.fromkeys(folders):  # de-duped, order kept
                folder_reports, _cached = _sift_or_cached(job, folder)
                reports.extend(folder_reports)
                if job["cancel"].is_set():
                    raise SiftCancelled("cancelled")

        # Music: reuse the project's persisted analysis (no re-listen); on a
        # miss, analyze once and persist it into the project.
        music = None
        music_path = payload.get("music") or plan.music_path
        if music_path:
            if project is not None:
                music = projects.load_music(project, music_path)
            if music is None:
                from monteur.music import analyze_music

                with _JOBS_LOCK:
                    job["progress"].append(
                        {"stage": "music", "name": Path(music_path).name}
                    )
                music = analyze_music(music_path)
                if project is not None:
                    projects.save_music(project, music)
        if job["cancel"].is_set():
            raise SiftCancelled("cancelled")

        with _JOBS_LOCK:
            job["progress"].append(
                {"stage": "direct", "name": "Claude is reviewing the cut"}
            )
        from monteur.director import direct_cut

        review = direct_cut(
            plan, reports, music, notes=str(payload.get("notes") or "")
        )
        job["result"] = {
            "review": review,
            # The plan is untouched — the browser keeps iterating on it.
            "plan_json": plan_to_dict(plan),
            "applied": False,
        }
        job["state"] = "done"
    except SiftCancelled:
        job["state"] = "cancelled"
    except (MonteurAIError, MonteurMediaError, ValueError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _run_coverage_job(job: dict, payload: dict) -> None:
    """Daemon-thread body for POST /api/coverage (the pre-cut shot list).

    Mirrors ``monteur missing`` (cli.cmd_missing): sift the folder through
    the same scan cache as build/pick/kit (a cache hit after a see-scan
    carries the vision annotations, which is exactly what makes the shot
    list sharp), then ask :func:`monteur.coverage.missing_shots` which
    shots are still missing. Like Director's Notes this is a GATE: the
    user explicitly asked for the coverage check, so a ``MonteurAIError``
    fails the job with its own actionable message.

    ``missing_shots`` is resolved at CALL time via the ``from``-import
    below, so tests can monkeypatch it on :mod:`monteur.coverage` without
    any AI backend.
    """
    from monteur.ai import MonteurAIError
    from monteur.media import MonteurMediaError
    from monteur.sift import SiftCancelled

    try:
        try:
            target_raw = payload.get("target")
            target = float(target_raw) if target_raw else None
        except (TypeError, ValueError):
            raise ValueError("'target' must be a number of seconds")

        reports, _cached = _sift_or_cached(job, payload.get("folder", ""))
        if job["cancel"].is_set():
            raise SiftCancelled("cancelled")

        with _JOBS_LOCK:
            job["progress"].append(
                {"stage": "coverage", "name": "Claude is checking your coverage"}
            )
        from monteur.coverage import missing_shots

        result = missing_shots(
            reports,
            style=str(payload.get("style") or "auto"),
            brief=str(payload.get("brief") or ""),
            target_seconds=target,
        )
        job["result"] = {"coverage": result}
        job["state"] = "done"
    except SiftCancelled:
        job["state"] = "cancelled"
    except (MonteurAIError, MonteurMediaError, ValueError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _run_direct_apply_job(job: dict, payload: dict) -> None:
    """Daemon-thread body for POST /api/create/direct/apply.

    Pure plan surgery, no AI: :func:`monteur.director.apply_review` swaps
    the reviewed slots' sources (record grid, transitions, dips and SFX
    stay bit-identical; pinned or unmatchable suggestions are skipped with
    a note) and the improved plan is rendered through the SAME output
    pathway a revise job uses, so the result has the standard build-result
    shape plus the new ``"plan_json"`` and the ``"notes"`` of what was
    applied. Nothing is re-planned. The music is NOT re-analyzed (the
    grid is untouched), so the result's ``tempo`` is 0.

    ``apply_review`` is resolved at CALL time via the ``from``-import
    below, so tests can monkeypatch it on :mod:`monteur.director`.
    """
    from monteur.media import MonteurMediaError
    from monteur.montage import montage_to_timeline, plan_from_dict, plan_to_dict
    from monteur.sift import SiftCancelled
    from monteur.io import write_edl, write_fcpxml

    try:
        plan = plan_from_dict(payload.get("plan_json") or {})  # bad -> ValueError
        if not plan.entries:
            raise ValueError("the plan has no entries — nothing to improve")
        review = payload.get("review")
        if not isinstance(review, dict):
            raise ValueError("'review' must be the director's-notes dict")
        audio = payload.get("audio") or ("music" if plan.music_path else "original")
        if not plan.music_path and audio != "original":
            raise ValueError(f"the plan has no music; audio mode {audio!r} needs a song")

        reports, _cached = _sift_or_cached(job, payload.get("folder", ""))
        if job["cancel"].is_set():
            raise SiftCancelled("cancelled")

        from monteur.director import apply_review

        improved, notes = apply_review(plan, review, reports)

        fps = float(payload.get("fps") or 25)
        timeline_kwargs: dict = {"audio": audio}
        if payload.get("canvas"):
            timeline_kwargs["canvas"] = payload["canvas"]
        timeline = montage_to_timeline(improved, fps=fps, **timeline_kwargs)
        fmt = (payload.get("format") or "fcpxml").lower()
        if fmt == "edl":
            content, filename = write_edl(timeline), "monteur_montage.edl"
        else:
            content, filename = write_fcpxml(timeline), "monteur_montage.fcpxml"
        result = {
            "filename": filename,
            "content": content,
            "plan": {
                "duration": improved.duration,
                "cuts": len(improved.entries),
                "tempo": 0,
                "notes": improved.notes,
            },
            # The improved plan in the save format, so iterations chain.
            "plan_json": plan_to_dict(improved),
            # What was actually applied/skipped — honesty first.
            "notes": notes,
            "applied": True,
        }
        _autosave_draft(payload, result)  # best-effort; never fails the job
        job["result"] = result
        job["state"] = "done"
    except SiftCancelled:
        job["state"] = "cancelled"
    except (MonteurMediaError, ValueError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _run_distill_job(job: dict, payload: dict, timeline) -> None:
    """Daemon-thread body for POST /api/create/distill (cut -> trailer).

    ``timeline`` was already parsed by the request handler (bad uploads are
    a 400, not a job error). ``probe_media`` stays True: sources that exist
    on this machine get their real durations, missing ones are noted
    honestly by :func:`monteur.distill.timeline_to_reports` — never fatal.
    """
    from monteur.distill import distill
    from monteur.media import MonteurMediaError
    from monteur.montage import montage_to_timeline
    from monteur.sift import SiftCancelled
    from monteur.io import write_edl, write_fcpxml

    try:
        music = None
        music_path = payload.get("music") or ""
        if music_path:
            from monteur.music import analyze_music

            with _JOBS_LOCK:
                job["progress"].append(
                    {"stage": "music", "name": Path(music_path).name}
                )
            music = analyze_music(music_path)
        if job["cancel"].is_set():
            raise SiftCancelled("cancelled")

        target = float(payload.get("target") or 60.0)
        plan = distill(
            timeline, music, target=target,
            style=payload.get("style") or "trailer",
        )
        if not plan.entries:
            raise ValueError("no usable material found in the cut — nothing to distill")

        # Like the CLI: no music means the trailer keeps the clips' own sound.
        audio = payload.get("audio") or ("music" if music_path else "original")
        if music is None and audio != "original":
            audio = "original"
        fps = timeline.fps if timeline.fps > 0 else 25.0
        timeline_kwargs: dict = {"audio": audio}
        if payload.get("canvas"):
            timeline_kwargs["canvas"] = payload["canvas"]
        out = montage_to_timeline(plan, fps=fps, **timeline_kwargs)
        fmt = (payload.get("format") or "fcpxml").lower()
        if fmt == "edl":
            content, filename = write_edl(out), "monteur_trailer.edl"
        else:
            content, filename = write_fcpxml(out), "monteur_trailer.fcpxml"
        job["result"] = {
            "filename": filename,
            "content": content,
            "plan": {
                "duration": plan.duration,
                "cuts": len(plan.entries),
                "tempo": music.tempo if music is not None else 0,
                "notes": plan.notes,
            },
        }
        job["state"] = "done"
    except SiftCancelled:
        job["state"] = "cancelled"
    except (MonteurMediaError, ValueError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _run_resolve_build_job(job: dict, payload: dict) -> None:
    """Daemon-thread body for POST /api/create/resolve (plan -> Resolve).

    Mirrors ``monteur create --into-resolve`` (cli) and the MCP server's
    ``into_resolve`` path: the plan is rebuilt from the browser's
    ``"plan_json"`` (bad input -> ValueError -> job error), title specs are
    derived from the plan's own dips (:func:`monteur.resolve.
    titles_from_plan` — plans without dips pass ``None``), the optional
    ``"canvas"`` preset key is forwarded so the Resolve timeline gets the
    wizard's chosen resolution (cine presets also auto-set the crop
    scaling), and the build
    runs through :func:`monteur.resolve.build_plan_isolated`, which NEVER
    raises: Resolve scripting lives in a disposable child process, so even
    a native crash comes back as a graceful ``{"ok": False}`` dict. Its
    error message is surfaced verbatim as the job error — it already
    explains fixes like MONTEUR_RESOLVE_PYTHON.

    ``build_plan_isolated`` / ``titles_from_plan`` are resolved at CALL
    time via the ``from``-import below, so tests can monkeypatch them on
    :mod:`monteur.resolve` without ever needing a running Resolve.
    """
    from monteur.montage import plan_from_dict
    from monteur.resolve import build_plan_isolated, titles_from_plan

    try:
        plan = plan_from_dict(payload.get("plan_json") or {})  # bad -> ValueError
        if not plan.entries:
            raise ValueError("the plan has no entries — nothing to build")
        fps = float(payload.get("fps") or 25)
        name = str(payload.get("name") or "Monteur Montage")
        canvas = payload.get("canvas") or None
        # The audio mode picks the sound layout (song bed, camera sound,
        # SFX track) — a no-music plan defaults to its clips' own sound.
        audio = str(
            payload.get("audio") or ("music" if plan.music_path else "original")
        )
        titles = titles_from_plan(plan) if plan.dips else None
        with _JOBS_LOCK:
            job["progress"].append(
                {"stage": "resolve", "name": "building the timeline in Resolve"}
            )
        built = build_plan_isolated(
            plan, fps=fps, name=name, titles=titles, canvas=canvas, audio=audio
        )
        if not built.get("ok"):
            # Never raises by contract — a failure dict carries the worker's
            # user-ready message (Resolve not running, scripting disabled,
            # incompatible interpreter, timeout). Pass it through verbatim.
            job["message"] = str(
                built.get("error")
                or "DaVinci Resolve could not build the timeline."
            )
            job["state"] = "error"
            return
        job["result"] = {
            "timeline": built.get("timeline") or name,
            "warnings": [str(w) for w in built.get("warnings") or []],
        }
        job["state"] = "done"
    except ValueError as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


class _RenderCancelled(Exception):
    """Raised inside the render job's progress callback to honour a cancel.

    ``render_isolated`` documents that an exception raised by the caller's
    own ``progress`` callback propagates AFTER the worker child process is
    killed — so raising here terminates the monitoring child. Resolve
    itself keeps rendering (Monteur only watches); the UI copy says so.
    """


def _run_resolve_render_job(job: dict, payload: dict) -> None:
    """Daemon-thread body for POST /api/resolve/render (timeline -> video).

    Runs :func:`monteur.resolve.render_isolated` — crash-safe by contract
    (the streamed worker child does every Resolve call and never raises) —
    and feeds its live percent callbacks into the job's progress entries as
    ``{"stage": "render", "percent": N}``, the shape the Studio job panel
    renders as a determinate bar. The callback doubles as the cancel seam:
    when the job's cancel event is set it raises :class:`_RenderCancelled`,
    which kills the monitoring child. That only stops the WATCHING —
    Resolve keeps rendering, which the UI states honestly. A failure dict
    fails the job with the worker's message verbatim (it already names the
    fix, e.g. Resolve was closed meanwhile).

    ``render_isolated`` is resolved at CALL time via the ``from``-import
    below, so tests can monkeypatch it on :mod:`monteur.resolve` without a
    running Resolve.
    """
    from monteur.resolve import render_isolated

    try:
        target_dir = str(payload.get("target_dir") or "").strip()
        name = str(payload.get("name") or "").strip() or "monteur_render"
        preset = payload.get("preset") or None
        timeline = payload.get("timeline") or None
        with _JOBS_LOCK:
            job["progress"].append(
                {"stage": "resolve", "name": "starting the render in Resolve"}
            )

        def progress(percent: int) -> None:
            if job["cancel"].is_set():
                raise _RenderCancelled("cancelled")
            with _JOBS_LOCK:
                job["progress"].append(
                    {"stage": "render", "percent": int(percent), "name": "rendering"}
                )

        result = render_isolated(
            timeline, target_dir, name, preset=preset, progress=progress
        )
        if job["cancel"].is_set():
            # Cancelled between the last progress line and the result.
            job["state"] = "cancelled"
            return
        if not result.get("ok"):
            job["message"] = str(
                result.get("error")
                or "DaVinci Resolve could not render the video."
            )
            job["state"] = "error"
            return
        job["result"] = {
            "path": result.get("path"),
            "seconds": result.get("seconds"),
            "preset": result.get("preset"),
        }
        job["state"] = "done"
    except _RenderCancelled:
        job["state"] = "cancelled"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _movie_module():
    """Resolve :mod:`monteur.movie` at CALL time (same seam as vision).

    ``importlib.import_module`` honours ``sys.modules``, so tests can either
    ``monkeypatch.setattr("monteur.movie.generate_movie", fake)`` or replace
    the whole module — mirroring how the scan worker resolves vision.
    """
    import importlib

    return importlib.import_module("monteur.movie")


def _register_movie_recent(project_dir: str, project) -> None:
    """Index a Movie project in the recents store as a lightweight pointer
    (type "movie", the film's title + its folder path), so it shows on the
    Home next to cuts and under the Movie filter. The movie folder is never
    copied — the pointer just reopens it by path. A stable id (hashed path)
    means reopening the same movie updates one entry, not a duplicate.
    Best-effort: any failure here must never break the movie endpoint.
    """
    try:
        import hashlib

        from monteur import projects

        abspath = os.path.abspath(os.path.expanduser(str(project_dir)))
        if not abspath:
            return
        pid = "mv" + hashlib.sha1(abspath.encode("utf-8")).hexdigest()[:14]
        name = str(getattr(project, "title", "") or os.path.basename(abspath) or "Movie")
        existing = projects.load_project(pid)
        if existing is not None:
            existing.name = name
            existing.type = "movie"
            existing.options["movie_path"] = abspath
            projects.save_project(existing)  # bumps modified_at
        else:
            projects.create_project(
                name, type="movie", project_id=pid,
                options={"movie_path": abspath},
            )
    except Exception:  # noqa: BLE001 — a recents pointer is never load-bearing
        pass


def _movie_payload(movie, project) -> dict:
    """The response body every movie endpoint shares: project + progress
    + the deterministic shoot plan (:func:`monteur.movie.shoot_plan` —
    no AI, no sifting, so it rides along for free and the Movie view's
    Shoot-plan panel refreshes with every load/assign)."""
    return {
        "project": movie.project_to_dict(project),
        "progress": movie.project_progress(project),
        "shoot_plan": movie.shoot_plan(project),
    }


def _run_movie_job(job: dict, payload: dict) -> None:
    """Daemon-thread body for POST /api/movie/new (draft a blueprint).

    Unlike vision, the screenplay is a GATE: there is no offline fallback
    for writing a movie, so a MonteurAIError fails the job with its message
    (which already tells the user about packages/keys/briefs).
    """
    from monteur.ai import MonteurAIError

    movie = _movie_module()
    try:
        with _JOBS_LOCK:
            job["progress"].append(
                {"stage": "movie", "name": "drafting the screenplay"}
            )
        project = movie.generate_movie(
            payload.get("brief", ""), genre=payload.get("genre", "")
        )
        paths = movie.save_project(project, payload.get("project_dir", ""))
        _register_movie_recent(payload.get("project_dir", ""), project)  # index on the Home
        result = _movie_payload(movie, project)
        result["paths"] = [str(p) for p in paths]
        job["result"] = result
        job["state"] = "done"
    except (MonteurAIError, OSError, ValueError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _run_scene_check_job(job: dict, payload: dict) -> None:
    """Daemon-thread body for POST /api/movie/check.

    Sifts the scene's assigned folder through the same cache path as
    build/pick/kit, optionally lets Claude vision label the moments
    (``"see"``, soft-fail — the technical check still runs), then holds the
    footage against the scene text with movie.check_scene_footage.
    """
    from monteur.media import MonteurMediaError
    from monteur.sift import SiftCancelled

    movie = _movie_module()
    try:
        project = movie.load_project(payload.get("project_dir", ""))
        scene_number = int(payload.get("scene", 0))
        scene = next(
            (s for s in project.scenes if s.number == scene_number), None
        )
        if scene is None:
            raise ValueError(
                f"no scene {scene_number} in this project — it has "
                f"{len(project.scenes)} scene(s)"
            )
        if not scene.folder:
            raise ValueError(
                f"scene {scene.number} has no footage folder assigned yet — "
                "assign one, then check"
            )
        reports, _cached = _sift_or_cached(job, scene.folder)
        if job["cancel"].is_set():
            raise SiftCancelled("cancelled")
        result: dict = {}
        if payload.get("see"):
            # Reports that already carry labels (a see-scan, or a previous
            # check on the same folder) don't need a second vision pass.
            annotated = any(
                m.label or m.tags for r in reports for m in r.moments
            )
            if not annotated:
                _notes, error = _apply_vision(job, reports)
                if error:
                    result["vision_error"] = error
        result["scene"] = scene.number
        result["check"] = movie.check_scene_footage(scene, reports)
        result["clips"] = [
            {"name": Path(r.path).name, "usable_ratio": r.usable_ratio}
            for r in reports
        ]
        # Persist the check on its scene slot (movie.json) so the shoot
        # plan can read checked-ok/checked-weak across restarts. Best
        # effort: an unwritable project folder must not fail a check that
        # already produced its result.
        movie.record_scene_check(project, scene.number, result["check"])
        try:
            movie.save_project(project, payload.get("project_dir", ""))
        except OSError as exc:
            result["persist_error"] = (
                f"could not remember this check in movie.json: {exc}"
            )
        result["shoot_plan"] = movie.shoot_plan(project)
        job["result"] = result
        job["state"] = "done"
    except SiftCancelled:
        job["state"] = "cancelled"
    except (MonteurMediaError, ValueError, FileNotFoundError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _title_slug(title: str) -> str:
    """A filesystem-friendly filename stem from a project title."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")
    return slug or "movie"


def _run_movie_assemble_job(job: dict, payload: dict) -> None:
    """Daemon-thread body for POST /api/movie/assemble (cut the film).

    :func:`monteur.movie.assemble_movie` walks the screenplay and sifts each
    scene's assigned folder itself, driven by a ``sift_cache`` dict
    ``{folder: [ClipReport]}``. The server pre-populates that dict from the
    module scan cache for every assigned folder whose cached sift is still
    fresh (same mtime check as build/pick/kit), so a Check followed by an
    Assemble never re-sifts the shared folder — each hit is announced with
    the usual ``{"stage": "cache"}`` progress entry. Folders the engine had
    to sift fresh are fed back through :func:`_remember_scan` afterwards
    (the module cache keeps its "last successful sift" semantics), so a
    Check straight after an Assemble is free too.

    Engine progress arrives through one callback: ``"scene"`` entries (one
    per scene, name = the scene heading, index/total over all scenes) plus
    the usual per-clip sift stages. A ValueError from the engine (e.g. no
    scene has footage assigned) is user-ready and fails the job with its
    message.
    """
    from monteur.media import MonteurMediaError
    from monteur.sift import SiftCancelled
    from monteur.io import write_edl, write_fcpxml

    movie = _movie_module()
    try:
        project = movie.load_project(payload.get("project_dir", ""))

        sift_cache: dict = {}
        for scene in project.scenes:
            if not scene.folder or scene.folder in sift_cache:
                continue
            reports = _cached_reports(scene.folder)
            if reports is not None:
                sift_cache[scene.folder] = reports
                with _JOBS_LOCK:
                    job["progress"].append(
                        {"stage": "cache", "name": "using previous scan"}
                    )
        prefilled = set(sift_cache)

        def progress(index, total, name, stage, report=None):
            entry = {"index": index, "total": total, "name": name, "stage": stage}
            if stage == "done" and report is not None:
                entry["usable_ratio"] = report.usable_ratio
            with _JOBS_LOCK:
                job["progress"].append(entry)

        fps = float(payload.get("fps") or 25)
        canvas = payload.get("canvas") or "uhd"
        timeline, notes, film_plan = movie.assemble_movie(
            project,
            fps=fps,
            canvas=canvas,
            progress=progress,
            sift_cache=sift_cache,
        )
        # The engine has no cancel seam; honour a cancel that arrived while
        # it ran by discarding the result (nothing was written anywhere).
        if job["cancel"].is_set():
            raise SiftCancelled("cancelled")
        # Freshly sifted folders feed the module cache back — like a scan,
        # the cache ends up holding the last folder sifted in this job.
        for folder, reports in sift_cache.items():
            if folder not in prefilled and reports:
                _remember_scan(folder, reports)

        fmt = (payload.get("format") or "fcpxml").lower()
        if fmt == "edl":
            content, ext = write_edl(timeline), "edl"
        else:
            content, ext = write_fcpxml(timeline), "fcpxml"
        # The engine drops one "Scene N: ..." marker per scene that actually
        # made it into the film (skipped scenes get a note, not a marker).
        scene_markers = [
            marker
            for marker in timeline.markers
            if str(getattr(marker, "name", "")).startswith("Scene ")
        ]
        from monteur.montage import plan_to_dict

        job["result"] = {
            "filename": f"{_title_slug(project.title)}.{ext}",
            "content": content,
            "notes": list(notes),
            "duration_seconds": timeline.duration_seconds,
            "scenes_used": len(scene_markers),
            # The assembled film as one plan (movie.assemble_movie rule 9):
            # this is what plugs the result card into every plan-based
            # engine — preview, storyboard thumbs, direct export, Resolve
            # build, director's notes, YouTube prefill.
            "plan_json": plan_to_dict(film_plan),
            "fps": fps,
            "canvas": canvas,
            "title": project.title,
            # Scene starts for the storyboard's scene chips (the plan
            # itself carries no scene structure — the markers do).
            "scenes": [
                {
                    "name": str(marker.name),
                    "start_seconds": marker.frame / fps,
                    "note": str(getattr(marker, "note", "") or ""),
                }
                for marker in scene_markers
            ],
        }
        job["state"] = "done"
    except SiftCancelled:
        job["state"] = "cancelled"
    except (MonteurMediaError, ValueError, FileNotFoundError, OSError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


# --- YouTube upload connection (monteur.youtube) -------------------------------
#
# The OAuth loopback handshake needs one piece of server-side memory: the
# single-use `state` (CSRF guard) and the exact redirect_uri the consent URL
# was built with (the token exchange must repeat it verbatim). Module-level
# like the job registry — one Studio process serves one user.

_YT_OAUTH: dict = {"state": "", "redirect_uri": ""}
_YT_OAUTH_LOCK = threading.Lock()


def _youtube_status_view() -> dict:
    """The JSON view GET status and the credential/disconnect POSTs share.

    Never contains the client secret or any token — only whether they are
    set, plus the channel-title hint remembered from the last upload.
    """
    from monteur.settings import (
        youtube_channel,
        youtube_client_id,
        youtube_client_secret,
        youtube_refresh_token,
    )

    return {
        "configured": bool(youtube_client_id() and youtube_client_secret()),
        "connected": bool(youtube_refresh_token()),
        "channel": youtube_channel(),
    }


def _run_youtube_upload_job(job: dict, payload: dict) -> None:
    """Daemon-thread body for POST /api/youtube/upload (file -> private draft).

    Mints a fresh access token from the stored refresh token, uploads with
    byte progress (``{"stage": "upload", "sent", "total"}`` entries), and
    honours the typed errors of :mod:`monteur.youtube`: a mid-upload
    ``TokenExpired`` gets exactly one refresh+retry, ``QuotaExceeded``
    fails the job with the friendly daily-limit message verbatim. The
    optional thumbnail is best-effort — its note lands in the result, a
    failure never fails the upload that already succeeded.

    ``monteur.youtube``'s functions are resolved at CALL time via the
    module attribute below, so tests monkeypatch them without any Google.
    """
    from monteur import youtube
    from monteur.settings import (
        save_settings,
        youtube_client_id,
        youtube_client_secret,
        youtube_refresh_token,
    )

    try:
        path = str(payload.get("path") or "")
        title = str(payload.get("title") or "").strip()
        description = str(payload.get("description") or "")
        tags = [str(t).strip() for t in (payload.get("tags") or []) if str(t).strip()]
        privacy = str(payload.get("privacy") or "private")
        thumbnail = str(payload.get("thumbnail") or "").strip()
        client_id, client_secret = youtube_client_id(), youtube_client_secret()
        refresh_token = youtube_refresh_token()

        def fresh_token() -> str:
            with _JOBS_LOCK:
                job["progress"].append(
                    {"stage": "auth", "name": "refreshing the YouTube connection"}
                )
            return youtube.refresh_access_token(
                client_id, client_secret, refresh_token
            )

        def progress(sent: int, total: int) -> None:
            entry = {
                "stage": "upload",
                "sent": int(sent),
                "total": int(total),
                "name": Path(path).name,
            }
            with _JOBS_LOCK:
                job["progress"].append(entry)

        def upload(token: str) -> dict:
            return youtube.upload_video(
                token, path, title=title, description=description,
                tags=tags, privacy=privacy, progress=progress,
            )

        token = fresh_token()
        try:
            uploaded = upload(token)
        except youtube.TokenExpired:
            # One refresh + retry by contract; a second TokenExpired falls
            # through to the "reconnect in settings" handler below.
            token = fresh_token()
            uploaded = upload(token)

        notes: list[str] = []
        if thumbnail:
            note = youtube.set_thumbnail(token, uploaded["video_id"], thumbnail)
            if note:
                notes.append(note)  # best-effort by contract — never fatal
        if uploaded.get("channel"):
            try:  # a pure display hint — failing to save it loses nothing
                save_settings({"youtube_channel": str(uploaded["channel"])})
            except OSError:
                pass
        video_id = uploaded["video_id"]
        job["result"] = {
            "video_id": video_id,
            # The review link is the point: the upload is a PRIVATE draft.
            "url": f"https://studio.youtube.com/video/{video_id}/edit",
            "watch_url": f"https://www.youtube.com/watch?v={video_id}",
            "privacy": privacy,
            "channel": str(uploaded.get("channel") or ""),
            "notes": notes,
        }
        job["state"] = "done"
    except youtube.QuotaExceeded as exc:
        job["message"] = str(exc)  # the friendly daily-limit wording, verbatim
        job["state"] = "error"
    except youtube.TokenExpired:
        job["message"] = (
            "your YouTube connection expired — reconnect in Monteur's settings"
        )
        job["state"] = "error"
    except (youtube.MonteurYouTubeError, OSError, ValueError) as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


# --- AI connection settings (backend choice + API key, managed in the UI) ------


def _app_version() -> str:
    """The version of the currently loaded monteur (payload-aware)."""
    import monteur

    return str(getattr(monteur, "__version__", "0") or "0")


def _app_data_root() -> Path:
    """A persistent, user-writable root for the windowed app's working files.

    ``~/.monteur/studio`` (next to settings/projects/payloads), created on
    demand — so an installed build writes state to the user profile, never into
    its read-only install folder. Honors ``MONTEUR_SETTINGS_PATH`` so a
    relocated data dir keeps everything together.
    """
    from monteur.settings import settings_path

    root = settings_path().parent / "studio"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path.home() / ".monteur" / "studio"
    return root


def _settings_view() -> dict:
    """The JSON view GET and POST /api/settings share.

    Never contains the key itself — only ``api_key_set`` and a "…" + last-4
    ``api_key_hint`` so the UI can say WHICH key is saved without ever
    round-tripping the secret. ``effective`` is what the next AI call would
    actually use, computed by the same resolver the calls go through.

    ``resolve_python`` / ``resolve_python_env_set`` cover the DaVinci
    Resolve worker interpreter: the saved path ("" = unset) and whether the
    MONTEUR_RESOLVE_PYTHON environment override is active (in which case
    the UI disables its Resolve-Python controls, like the AI section does
    for a forced backend).
    """
    from monteur import ai as ai_mod
    from monteur.settings import ai_backend, api_key, resolve_python, update_channel

    key = api_key()
    try:
        effective = ai_mod._resolve_backend()
    except ai_mod.MonteurAIError:
        effective = "none"
    return {
        "backend": ai_backend(),
        "api_key_set": bool(key),
        "api_key_hint": ("…" + key[-4:]) if key else "",
        "env_key_set": bool(
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        ),
        "cli_found": ai_mod._cli_path() is not None,
        "backend_forced_by_env": bool(os.environ.get(ai_mod.BACKEND_ENV, "").strip()),
        "effective": effective,
        "resolve_python": resolve_python(),
        "resolve_python_env_set": bool(os.environ.get("MONTEUR_RESOLVE_PYTHON")),
        "update_channel": update_channel(),
        "app_version": _app_version(),
    }


def _run_resolve_detect_job(job: dict) -> None:
    """Daemon-thread body for POST /api/resolve/detect (find a worker Python).

    Runs :func:`monteur.resolve.find_resolve_python` — every candidate is
    probed in a disposable child process, so even a native-crashing
    interpreter cannot hurt the server — and, when a compatible interpreter
    turns up, SAVES it to settings immediately: the user clicked one button
    and Monteur must remember the answer. The result carries the full probe
    report plus a fresh post-save :func:`monteur.resolve.diagnose` verdict
    for the UI to display verbatim. Finding nothing is a SUCCESSFUL job
    with ``"found": None`` — that is information (the UI shows the guided
    python.org install help), not an error.

    ``find_resolve_python`` / ``diagnose`` are resolved at call time via
    the ``from``-import below so tests can monkeypatch them on
    :mod:`monteur.resolve` without probing real interpreters.
    """
    from monteur.resolve import diagnose, find_resolve_python
    from monteur.settings import save_settings

    try:
        with _JOBS_LOCK:
            job["progress"].append(
                {"stage": "detect", "name": "probing Python installations"}
            )
        report = find_resolve_python()
        found = report.get("found")
        if found:
            # THE point of the button: one click finds AND remembers it.
            save_settings({"resolve_python": found})
        probed = list(report.get("probed") or [])
        found_probe = next((p for p in probed if p.get("path") == found), {})
        fresh = diagnose()  # AFTER the save — the verdict reflects the new state
        job["result"] = {
            "found": found,
            "connected": bool(report.get("connected")),
            "version": str(found_probe.get("version") or ""),
            "probed": probed,
            "verdict": str(fresh.get("verdict") or ""),
        }
        job["state"] = "done"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _run_ai_test_job(job: dict) -> None:
    """Daemon-thread body for POST /api/settings/test (one tiny completion).

    The user's "does my key / Claude Code work?" button: resolve the
    backend exactly like a real call would, then ask for one word through
    it. A MonteurAIError (no backend, bad key, CLI not logged in, network)
    fails the job with its own message — those messages already explain
    the fix, so they go to the UI verbatim.
    """
    from monteur import ai as ai_mod

    try:
        backend = ai_mod._resolve_backend()
        with _JOBS_LOCK:
            job["progress"].append({"stage": "ai-test", "name": backend})
        reply = ai_mod.complete(
            "Reply with the single word OK.", max_tokens=200, effort="low"
        )
        job["result"] = {"backend": backend, "reply": reply.strip()}
        job["state"] = "done"
    except ai_mod.MonteurAIError as exc:
        job["message"] = str(exc)
        job["state"] = "error"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"internal error: {exc}"
        job["state"] = "error"


def _run_update_job(job: dict) -> None:
    """Daemon-thread body for POST /api/update/install.

    Checks for the newest release, downloads the platform build into the
    staging dir and marks it pending. The actual swap is deferred to the next
    launch (``update.apply_pending``). A source checkout, or a release with no
    downloadable build, finishes the job with a clear message instead of an
    error.
    """
    from monteur import update as update_mod
    from monteur.settings import update_channel

    try:
        # A git checkout updates in place with `git pull --ff-only` (safe: it
        # never rewrites or discards local work) — no download, no exe swap.
        # The running process still holds the old code, so we ask for a restart.
        git_root = update_mod.git_root()
        if git_root is not None:
            with _JOBS_LOCK:
                job["progress"].append({"stage": "pulling"})
            res = update_mod.git_pull(git_root)
            job["result"] = {
                "available": True,
                "staged": res.applied,
                "current": update_mod.current_version(),
                "latest": update_mod.current_version(),
                "message": res.message,
            }
            job["state"] = "done"  # a blocked pull is informational, not a crash
            return
        with _JOBS_LOCK:
            job["progress"].append({"stage": "checking"})
        info = update_mod.check(channel=update_channel())
        if info.error and not info.latest:
            job["message"] = info.error
            job["state"] = "error"
            return
        if not info.available:
            job["result"] = {"available": False, "message": "You're already on the latest version.",
                             "current": info.current, "latest": info.latest}
            job["state"] = "done"
            return
        if info.mode != "frozen":
            # a dev/source run: nothing to install, point them at the right path
            job["result"] = {
                "available": True, "staged": False, "current": info.current, "latest": info.latest,
                "message": "Update available — this is a source install, so update with "
                           "'git pull' or 'pip install -U monteur'.",
                "url": info.url,
            }
            job["state"] = "done"
            return
        if info.kind == "payload":
            # the usual, small update — download + verify + unpack the app
            # payload; the launcher runs it on the next start (no exe swap)
            with _JOBS_LOCK:
                job["progress"].append({"stage": "downloading", "name": info.payload_name})
            version = update_mod.install_payload(info)
            job["result"] = {
                "available": True, "staged": True, "current": info.current, "latest": version,
                "message": f"Monteur {version} installed. Restart Monteur to use it.",
            }
            job["state"] = "done"
            return
        if info.kind == "exe" and info.download_url:
            # a full shell update (deps changed): stage the executable, swap at start
            with _JOBS_LOCK:
                job["progress"].append({"stage": "downloading", "name": info.asset_name})
            update_mod.download(info)
            job["result"] = {
                "available": True, "staged": True, "current": info.current, "latest": info.latest,
                "message": f"Monteur {info.latest} downloaded. Restart Monteur to finish installing.",
            }
            job["state"] = "done"
            return
        job["result"] = {"available": True, "staged": False, "latest": info.latest,
                         "message": "This release has no installable build for your platform yet.",
                         "url": info.url}
        job["state"] = "done"
    except Exception as exc:  # noqa: BLE001 — a job thread must never die silently
        job["message"] = f"could not install the update: {exc}"
        job["state"] = "error"


_AUDIO_FILETYPES = [
    ("Audio files", "*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.aiff *.aif *.wma"),
    ("All files", "*.*"),
]

_NO_DIALOG_ERROR = (
    "no native file dialog available on this system — paste the path instead"
)


def _native_pick(kind: str) -> dict:
    """Open a native file/folder dialog on THIS machine (Studio is local).

    Tk is created, used and destroyed entirely inside one dedicated thread —
    Tk objects are not thread-portable — and _PICK_LOCK serialises dialogs so
    two concurrent picks can't fight over the screen. A headless machine
    (tkinter missing, no display) degrades to a soft ``{"error": ...}`` that
    the UI turns into a "paste the path" hint; it is NOT an HTTP error.
    """
    outcome: dict = {}

    def run_dialog() -> None:
        try:
            import tkinter
            from tkinter import filedialog

            root = tkinter.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)  # dialog must not hide behind the browser
            except Exception:  # noqa: BLE001 — purely cosmetic
                pass
            try:
                if kind == "folder":
                    picked = filedialog.askdirectory(parent=root, title="Choose your footage folder")
                elif kind == "music":
                    picked = filedialog.askopenfilename(
                        parent=root, title="Choose a song", filetypes=_AUDIO_FILETYPES
                    )
                else:
                    picked = filedialog.askopenfilename(parent=root, title="Choose a file")
            finally:
                root.destroy()
            # Cancelled dialogs return "" (or an empty tuple on some platforms).
            outcome["path"] = str(picked) if picked else ""
        except Exception:  # noqa: BLE001 — headless/no-display: soft fallback
            outcome["error"] = _NO_DIALOG_ERROR

    with _PICK_LOCK:
        thread = threading.Thread(target=run_dialog, name="monteur-pick", daemon=True)
        thread.start()
        thread.join()
    return outcome or {"error": _NO_DIALOG_ERROR}


class MonteurHandler(BaseHTTPRequestHandler):
    server_version = f"MonteurStudio/{__version__}"
    project: Project  # set by serve()

    # -- plumbing ---------------------------------------------------------

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # keep the terminal quiet

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_GONE:
            pass  # client closed the socket mid-response — nothing to do

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError(400, "empty request body")
        if length > 64 * 1024 * 1024:
            raise ApiError(413, "request too large")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, f"invalid JSON body: {exc}")

    def _dispatch(self, method: str) -> None:
        try:
            handler = self._route(method)
            handler()
        except ApiError as exc:
            self._send_json({"error": exc.message}, status=exc.status)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception as exc:  # a genuine handler bug — surface it, don't hide it
            import traceback

            print(
                f"Monteur Studio: unhandled error in {method} {self.path}:",
                flush=True,
            )
            traceback.print_exc()
            sys.stderr.flush()
            self._send_json({"error": f"internal error: {exc}"}, status=500)

    def _route(self, method: str):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        routes = {
            ("GET", "/"): self._app,
            ("GET", "/favicon.ico"): self._favicon,
            ("GET", "/api/versions"): self._versions_list,
            ("POST", "/api/versions"): self._versions_add,
            ("POST", "/api/analyze"): self._analyze,
            ("POST", "/api/compare"): self._compare,
            ("GET", "/api/resolve/status"): self._resolve_status,
            ("GET", "/api/resolve/diagnose"): self._resolve_diagnose,
            ("POST", "/api/resolve/detect"): self._resolve_detect,
            ("POST", "/api/resolve/analyze"): self._resolve_analyze,
            ("POST", "/api/resolve/render"): self._resolve_render,
            ("POST", "/api/assembly/plan"): self._assembly_plan,
            ("POST", "/api/assembly/export"): self._assembly_export,
            ("POST", "/api/create/scan"): self._create_scan,
            ("POST", "/api/create/transcribe"): self._create_transcribe,
            ("POST", "/api/create/build"): self._create_build,
            ("POST", "/api/create/series"): self._create_series,
            ("POST", "/api/create/pick"): self._create_pick,
            ("POST", "/api/create/kit"): self._create_kit,
            ("POST", "/api/create/revise"): self._create_revise,
            ("POST", "/api/create/direct"): self._create_direct,
            ("POST", "/api/create/direct/apply"): self._create_direct_apply,
            ("POST", "/api/create/treatment"): self._create_treatment,
            ("POST", "/api/create/distill"): self._create_distill,
            ("POST", "/api/create/export"): self._create_export,
            ("GET", "/api/clipinfo"): self._clipinfo,
            ("POST", "/api/alternatives"): self._alternatives,
            ("POST", "/api/plan/adjust"): self._plan_adjust,
            ("POST", "/api/create/preview"): self._create_preview,
            ("POST", "/api/create/export-video"): self._create_export_video,
            ("GET", "/api/thumb"): self._thumb,
            ("GET", "/api/media"): self._media,
            ("GET", "/api/browse/list"): self._browse_list,
            ("POST", "/api/proxies"): self._proxies_start,
            ("POST", "/api/create/resolve"): self._create_resolve,
            ("GET", "/api/drafts"): self._drafts_list,
            ("POST", "/api/drafts"): self._drafts_save,
            ("GET", "/api/projects"): self._projects_list,
            ("POST", "/api/projects"): self._projects_create,
            ("GET", "/api/preferences"): self._preferences_get,
            ("POST", "/api/preferences/reset"): self._preferences_reset,
            ("POST", "/api/preferences/signal"): self._preferences_signal,
            ("POST", "/api/find"): self._find,
            ("POST", "/api/coverage"): self._coverage,
            ("POST", "/api/movie/load"): self._movie_load,
            ("POST", "/api/movie/new"): self._movie_new,
            ("POST", "/api/movie/assign"): self._movie_assign,
            ("POST", "/api/movie/check"): self._movie_check,
            ("POST", "/api/movie/assemble"): self._movie_assemble,
            ("POST", "/api/pick"): self._pick,
            ("GET", "/api/settings"): self._settings_get,
            ("POST", "/api/settings"): self._settings_set,
            ("GET", "/api/prefs"): self._prefs_get,
            ("POST", "/api/prefs"): self._prefs_set,
            ("GET", "/api/favorites"): self._favorites_get,
            ("POST", "/api/favorites"): self._favorites_set,
            ("POST", "/api/settings/test"): self._settings_test,
            ("GET", "/api/youtube/status"): self._youtube_status,
            ("POST", "/api/youtube/credentials"): self._youtube_credentials,
            ("POST", "/api/youtube/connect"): self._youtube_connect,
            ("GET", "/api/youtube/callback"): self._youtube_callback,
            ("POST", "/api/youtube/disconnect"): self._youtube_disconnect,
            ("POST", "/api/youtube/upload"): self._youtube_upload,
            ("POST", "/api/youtube/prefill"): self._youtube_prefill,
            ("GET", "/api/update/check"): self._update_check,
            ("POST", "/api/update/install"): self._update_install,
            ("GET", "/api/cache"): self._cache_get,
            ("POST", "/api/cache/clear"): self._cache_clear,
            ("GET", "/api/jobs"): self._jobs_list,
        }
        if (method, path) in routes:
            return routes[(method, path)]
        if path.startswith("/api/preview/") and method == "GET":
            name = path[len("/api/preview/"):]
            if name and "/" not in name:
                return lambda: self._preview_file(name)
        if path.startswith("/api/versions/"):
            tail = path.rsplit("/", 1)[1]
            if tail.isdigit():
                vid = int(tail)
                if method == "GET":
                    return lambda: self._versions_get(vid)
                if method == "DELETE":
                    return lambda: self._versions_delete(vid)
        if path.startswith("/api/drafts/"):
            draft_id = path[len("/api/drafts/"):]
            if draft_id and "/" not in draft_id:
                if method == "GET":
                    return lambda: self._drafts_get(draft_id)
                if method == "DELETE":
                    return lambda: self._drafts_delete(draft_id)
        if path.startswith("/api/projects/"):
            rest = path[len("/api/projects/"):]
            if rest.endswith("/pool"):
                pool_id = rest[: -len("/pool")]
                if pool_id and "/" not in pool_id:
                    if method == "GET":
                        return lambda: self._project_pool_get(pool_id)
                    if method == "POST":
                        return lambda: self._project_pool_update(pool_id)
            elif rest.endswith("/analyze") and method == "POST":
                pid = rest[: -len("/analyze")]
                if pid and "/" not in pid:
                    return lambda: self._project_analyze(pid)
            elif rest.endswith("/see") and method == "POST":
                pid = rest[: -len("/see")]
                if pid and "/" not in pid:
                    return lambda: self._project_see(pid)
            elif rest.endswith("/clips") and method == "GET":
                pid = rest[: -len("/clips")]
                if pid and "/" not in pid:
                    return lambda: self._project_clips(pid)
            elif rest.endswith("/clip-note") and method == "POST":
                pid = rest[: -len("/clip-note")]
                if pid and "/" not in pid:
                    return lambda: self._project_clip_note(pid)
            elif rest.endswith("/moment-note") and method == "POST":
                pid = rest[: -len("/moment-note")]
                if pid and "/" not in pid:
                    return lambda: self._project_moment_note(pid)
            elif rest.endswith("/moment-mark") and method == "POST":
                pid = rest[: -len("/moment-mark")]
                if pid and "/" not in pid:
                    return lambda: self._project_moment_mark(pid)
            elif rest.endswith("/series/save") and method == "POST":
                pid = rest[: -len("/series/save")]
                if pid and "/" not in pid:
                    return lambda: self._project_series_save(pid)
            elif rest.endswith("/series") and method == "POST":
                pid = rest[: -len("/series")]
                if pid and "/" not in pid:
                    return lambda: self._project_series(pid)
            elif rest.endswith("/versions") and method == "GET":
                pid = rest[: -len("/versions")]
                if pid and "/" not in pid:
                    return lambda: self._project_versions(pid)
            elif "/versions/" in rest and rest.endswith("/restore") and method == "POST":
                pid, _, tail = rest.partition("/versions/")
                vid = tail[: -len("/restore")]
                if pid and vid and "/" not in pid and "/" not in vid:
                    return lambda: self._project_version_restore(pid, vid)
            elif "/versions/" in rest and rest.endswith("/changes") and method == "GET":
                pid, _, tail = rest.partition("/versions/")
                vid = tail[: -len("/changes")]
                if pid and vid and "/" not in pid and "/" not in vid:
                    return lambda: self._project_version_changes(pid, vid)
            elif rest and "/" not in rest:
                project_id = rest
                if method == "GET":
                    return lambda: self._projects_get(project_id)
                if method == "POST":
                    return lambda: self._projects_update(project_id)
                if method == "DELETE":
                    return lambda: self._projects_delete(project_id)
        if path.startswith("/api/jobs/"):
            parts = path[len("/api/jobs/"):].split("/")
            if len(parts) == 1 and parts[0] and method == "GET":
                return lambda: self._jobs_get(parts[0])
            if len(parts) == 2 and parts[1] == "cancel" and method == "POST":
                return lambda: self._jobs_cancel(parts[0])
        raise ApiError(404, f"no route for {method} {path}")

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    # -- endpoints --------------------------------------------------------

    def _app(self) -> None:
        # Inject the saved UI preferences (theme, fps, 'see') into the page so
        # the client reads them SYNCHRONOUSLY at load — no browser localStorage,
        # and no theme flash. A missing marker just leaves the {} default.
        from monteur.settings import ui_prefs

        try:
            text = _APP_HTML.read_text(encoding="utf-8")
            prefs_json = json.dumps(ui_prefs()).replace("<", "\\u003c")
            text = text.replace(
                "window.__MONTEUR_PREFS__ = {};",
                f"window.__MONTEUR_PREFS__ = {prefs_json};",
                1,
            )
            body = text.encode("utf-8")
        except OSError:
            body = _APP_HTML.read_bytes()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_GONE:
            pass  # browser closed the tab mid-load — not an error

    def _favicon(self) -> None:
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
            '<rect width="16" height="16" rx="3" fill="#2a78d6"/>'
            '<path d="M3 11 6.5 6l2.5 3 2-2.5L13 11z" fill="#fff"/></svg>'
        ).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(svg)))
            self.end_headers()
            self.wfile.write(svg)
        except _CLIENT_GONE:
            pass  # client went away — nothing to send to

    def _analyze(self) -> None:
        stats = _analyze_payload(self._read_json())
        self._send_json({"stats": asdict(stats)})

    def _compare(self) -> None:
        payload = self._read_json()
        if "a" not in payload or "b" not in payload:
            raise ApiError(400, "compare needs 'a' and 'b'")
        stats_a = _analyze_payload(payload["a"])
        stats_b = _analyze_payload(payload["b"])
        self._send_json(
            {
                "a": asdict(stats_a),
                "b": asdict(stats_b),
                "compare": compare(stats_a, stats_b),
            }
        )

    def _versions_list(self) -> None:
        self._send_json({"versions": self.project.versions()})

    def _versions_add(self) -> None:
        payload = self._read_json()
        stats = _analyze_payload(payload)
        entry = self.project.add_version(
            stats,
            label=payload.get("label", ""),
            source_file=payload.get("filename", ""),
            saved_at=time.strftime("%Y-%m-%d %H:%M"),
        )
        entry = {k: v for k, v in entry.items() if k != "stats"}
        self._send_json({"version": entry, "stats": asdict(stats)})

    def _versions_get(self, version_id: int) -> None:
        try:
            stats = self.project.get_stats(version_id)
        except KeyError as exc:
            raise ApiError(404, exc.args[0])
        self._send_json({"stats": asdict(stats)})

    def _versions_delete(self, version_id: int) -> None:
        self.project.delete_version(version_id)
        self._send_json({"ok": True})

    def _assembly_inputs(self, payload: dict):
        from monteur.io import read_srt, read_whisper_json
        from monteur.assembly import TakeSource
        from monteur.screenplay import parse_fountain
        from monteur.transcribe import scene_take_from_name

        script = payload.get("script") or {}
        if not script.get("content"):
            raise ApiError(400, "missing script content")
        screenplay = parse_fountain(script["content"])
        takes = []
        for item in payload.get("takes") or []:
            filename = item.get("filename", "")
            content = item.get("content", "")
            if not content:
                continue
            stem = Path(filename).stem
            if filename.lower().endswith(".json"):
                transcript = read_whisper_json(content, source_name=stem)
            else:
                transcript = read_srt(content, source_name=stem)
            scene_hint, take_hint = scene_take_from_name(filename)
            takes.append(
                TakeSource(
                    name=stem, transcript=transcript,
                    scene_hint=scene_hint, take_hint=take_hint,
                )
            )
        if not takes:
            raise ApiError(400, "no readable take transcripts (.srt/.json) provided")
        forced = {
            int(k): v for k, v in (payload.get("forced") or {}).items() if str(v)
        }
        return screenplay, takes, forced

    def _assembly_plan(self) -> None:
        from monteur.assembly import plan_assembly
        from monteur.screenplay import DIALOGUE

        payload = self._read_json()
        screenplay, takes, forced = self._assembly_inputs(payload)
        plan = plan_assembly(
            screenplay, takes,
            max_takes_per_scene=int(payload.get("max_takes") or 1),
            forced=forced,
        )
        scenes = [
            {
                "heading": s.heading,
                "number": s.number,
                "dialogue": [
                    {"index": i, "character": e.character, "text": e.text}
                    for i, e in enumerate(s.elements)
                    if e.kind == DIALOGUE
                ],
            }
            for s in screenplay.scenes
        ]
        self._send_json(
            {
                "screenplay": {"title": screenplay.title, "scenes": scenes},
                "plan": asdict(plan),
                "coverage": plan.coverage(),
                "takes": [t.name for t in takes],
            }
        )

    def _assembly_export(self) -> None:
        from monteur.assembly import assembly_to_timeline, plan_assembly
        from monteur.io import write_edl, write_fcpxml

        payload = self._read_json()
        screenplay, takes, forced = self._assembly_inputs(payload)
        fps = float(payload.get("fps") or 25)
        plan = plan_assembly(
            screenplay, takes,
            max_takes_per_scene=int(payload.get("max_takes") or 1),
            forced=forced,
        )
        handles_raw = payload.get("handles")
        timeline = assembly_to_timeline(
            plan, takes, fps=fps,
            handles=0.5 if handles_raw is None else float(handles_raw),
        )
        if not timeline.clips:
            raise ApiError(422, "nothing matched — no segments to export")
        fmt = (payload.get("format") or "fcpxml").lower()
        if fmt == "edl":
            content, filename = write_edl(timeline), "monteur_assembly.edl"
        elif fmt == "fcpxml":
            content, filename = write_fcpxml(timeline), "monteur_assembly.fcpxml"
        else:
            raise ApiError(400, f"unknown format {fmt!r} (use 'edl' or 'fcpxml')")
        self._send_json({"filename": filename, "content": content})

    def _create_scan(self) -> None:
        payload = self._read_json()
        folder = payload.get("folder", "")
        if not folder:
            raise ApiError(400, "missing 'folder' (path to your footage)")
        job = _new_job("scan", {"folder": folder})
        threading.Thread(
            target=_run_scan_job,
            args=(job, folder, bool(payload.get("see"))),
            name=f"monteur-scan-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _create_transcribe(self) -> None:
        """POST /api/create/transcribe — whisper the folder into <clip>.json sidecars."""
        payload = self._read_json()
        folder = payload.get("folder", "")
        if not folder:
            raise ApiError(400, "missing 'folder' (path to your footage)")
        job = _new_job("transcribe")
        threading.Thread(
            target=_run_transcribe_job,
            args=(job, folder),
            name=f"monteur-transcribe-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _create_build(self) -> None:
        payload = self._read_json()
        # the storyboard builds from a PROJECT's analyzed clips; a bare folder
        # is the legacy/CLI path. One of the two must be present.
        if not payload.get("project") and not payload.get("folder"):
            raise ApiError(400, "missing 'project' (or 'folder') to build from")
        _validate_arrangement(payload)
        _validate_platform(payload)
        job = _new_job("build", {"body": payload})
        threading.Thread(
            target=_run_build_job,
            args=(job, payload),
            name=f"monteur-build-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _create_series(self) -> None:
        payload = self._read_json()
        if not payload.get("folder"):
            raise ApiError(400, "missing 'folder' (path to your footage)")
        _validate_platform(payload)
        job = _new_job("series")
        threading.Thread(
            target=_run_series_job,
            args=(job, payload),
            name=f"monteur-series-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _create_pick(self) -> None:
        payload = self._read_json()
        if not payload.get("folder"):
            raise ApiError(400, "missing 'folder' (path to your footage)")
        if not payload.get("music_dir"):
            raise ApiError(400, "missing 'music_dir' (folder with candidate songs)")
        job = _new_job("pick")
        threading.Thread(
            target=_run_pick_job,
            args=(job, payload),
            name=f"monteur-pick-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _create_kit(self) -> None:
        payload = self._read_json()
        if not payload.get("folder"):
            raise ApiError(400, "missing 'folder' (path to your footage)")
        if not payload.get("kit_dir"):
            raise ApiError(400, "missing 'kit_dir' (folder to write the publish kit into)")
        _validate_arrangement(payload)
        _validate_platform(payload)
        job = _new_job("kit")
        threading.Thread(
            target=_run_kit_job,
            args=(job, payload),
            name=f"monteur-kit-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _create_revise(self) -> None:
        payload = self._read_json()
        if not payload.get("folder"):
            raise ApiError(400, "missing 'folder' (path to your footage)")
        if not isinstance(payload.get("plan_json"), dict):
            raise ApiError(
                400, "missing 'plan_json' (the plan a build result carries)"
            )
        if not str(payload.get("brief") or "").strip():
            raise ApiError(
                400, "missing 'brief' (say what should change, in one sentence)"
            )
        job = _new_job("revise")
        threading.Thread(
            target=_run_revise_job,
            args=(job, payload),
            name=f"monteur-revise-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _create_direct(self) -> None:
        payload = self._read_json()
        folders = payload.get("folders")
        if folders is not None and not (
            isinstance(folders, list)
            and folders
            and all(str(f or "").strip() for f in folders)
        ):
            raise ApiError(
                400, "'folders' must be a non-empty list of footage folders"
            )
        # director's notes reads a PROJECT's stored analysis; a bare folder is
        # the legacy/CLI path. One of the two must be present.
        if not payload.get("project") and not payload.get("folder") and not folders:
            raise ApiError(400, "missing 'project' (or 'folder') to review from")
        if not isinstance(payload.get("plan_json"), dict):
            raise ApiError(
                400, "missing 'plan_json' (the plan a build result carries)"
            )
        job = _new_job("direct")
        threading.Thread(
            target=_run_direct_job,
            args=(job, payload),
            name=f"monteur-direct-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _create_treatment(self) -> None:
        """POST /api/create/treatment {project, music?, brief?} → {treatment}.

        The Regie-Vorschlag: Claude reads the project's OWN stored analysis
        (moments + vision labels) and the chosen music, and proposes a creative
        treatment — format, style, pacing energy, mood, platform, length, grade
        and the opening hook — grounded in what the footage actually is. So
        "say nothing" no longer means "generic default"; it means "Claude looks
        at your material and suggests". Synchronous (one bounded Claude call),
        returning the normalized treatment the Studio renders as editable chips.
        Never fails the request: an unreachable backend yields a neutral default
        whose rationale says so.
        """
        from monteur import projects
        from monteur import treatment as _treatment

        payload = self._read_json()
        project_id = str(payload.get("project") or "")
        if not project_id:
            raise ApiError(400, "missing 'project' to propose a treatment from")
        project = projects.load_project(project_id)
        if project is None:
            raise ApiError(404, f"unknown project {project_id!r}")
        reports = projects.load_reports(project)
        if not reports:
            raise ApiError(
                400,
                "No analyzed footage yet — open the Footage tab, analyze your "
                "clips, then ask for a Regie-Vorschlag.",
            )
        music = None
        music_path = str(payload.get("music") or "")
        if music_path:
            try:
                music = projects.load_music(project, music_path)
            except Exception:
                music = None
        brief = str(payload.get("brief") or "")
        result = _treatment.propose_treatment(reports, music, brief=brief)
        self._send_json({"treatment": result})

    def _coverage(self) -> None:
        payload = self._read_json()
        if not payload.get("folder"):
            raise ApiError(400, "missing 'folder' (path to your footage)")
        job = _new_job("coverage")
        threading.Thread(
            target=_run_coverage_job,
            args=(job, payload),
            name=f"monteur-coverage-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _create_direct_apply(self) -> None:
        payload = self._read_json()
        if not payload.get("folder"):
            raise ApiError(400, "missing 'folder' (path to your footage)")
        if not isinstance(payload.get("plan_json"), dict):
            raise ApiError(
                400, "missing 'plan_json' (the plan a build result carries)"
            )
        if not isinstance(payload.get("review"), dict):
            raise ApiError(
                400, "missing 'review' (the director's notes to apply)"
            )
        job = _new_job("direct-apply")
        threading.Thread(
            target=_run_direct_apply_job,
            args=(job, payload),
            name=f"monteur-direct-apply-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _create_distill(self) -> None:
        payload = self._read_json()
        timeline_payload = payload.get("timeline")
        if not isinstance(timeline_payload, dict):
            raise ApiError(
                400, "missing 'timeline' (the finished cut as {filename, content, fps?})"
            )
        # Parse here so bad uploads are a 400 with a clear message, exactly
        # like /api/analyze — only real work runs in the job thread.
        timeline = _timeline_from_payload(timeline_payload)
        job = _new_job("distill")
        threading.Thread(
            target=_run_distill_job,
            args=(job, payload, timeline),
            name=f"monteur-distill-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _create_resolve(self) -> None:
        payload = self._read_json()
        if not isinstance(payload.get("plan_json"), dict):
            raise ApiError(
                400, "missing 'plan_json' (the plan a build result carries)"
            )
        job = _new_job("resolve-build")
        threading.Thread(
            target=_run_resolve_build_job,
            args=(job, payload),
            name=f"monteur-resolve-build-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _create_export(self) -> None:
        """POST /api/create/export — plan_json -> timeline file, no re-plan.

        Synchronous by design: :func:`monteur.montage.montage_to_timeline`
        is pure plan -> timeline (no sift, no music analysis, no AI), so
        the answer is instant and a background job would only add latency.
        This is the draft-resume path — the Studio rebuilds a saved cut's
        result card (and re-renders on a format switch) from the stored
        plan without ever re-planning it. The response is the standard
        build-result shape; ``tempo`` is 0 because nothing re-listens to
        the song. A ValueError from the plan loader or renderer surfaces
        as a 400 via the dispatcher.
        """
        from monteur.montage import plan_from_dict

        payload = self._read_json()
        if not isinstance(payload.get("plan_json"), dict):
            raise ApiError(
                400, "missing 'plan_json' (the plan a build result carries)"
            )
        plan = plan_from_dict(payload["plan_json"])  # bad -> ValueError -> 400
        if not plan.entries:
            raise ApiError(400, "the plan has no entries — nothing to export")
        self._send_json(_plan_export_result(plan, payload))

    # -- the shot inspector (clip facts, swap bench, boundary control) ------

    def _clipinfo(self) -> None:
        """GET /api/clipinfo?clip=&folder=&t0=&t1= — one clip's facts, instant.

        Probe facts (cached by path+mtime; zeros when unprobeable) plus the
        sifted report's duration/usable_ratio and the vision fields of the
        moment overlapping the optional t0–t1 source window most. Answers
        from the scan cache only — no cache is a 404 saying "scan first".
        """
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(self.path).query)

        def param(name: str) -> str:
            return (query.get(name) or [""])[0]

        clip = param("clip").strip()
        if not clip:
            raise ApiError(400, "missing 'clip' (the clip's path)")
        reports = _fresh_reports_or_404(param("folder").strip())
        report = _report_for_clip(reports, clip)
        if report is None:
            raise ApiError(404, f"no clip named {Path(clip).name!r} in the scan")
        try:
            t0 = float(param("t0") or 0.0)
            t1 = float(param("t1") or 0.0)
        except (TypeError, ValueError):
            raise ApiError(400, "'t0'/'t1' must be numbers (seconds)")

        # The moment the source window overlaps most (fallback: none).
        best, best_ov = None, 0.0
        if t1 > t0:
            for moment in report.moments:
                ov = min(moment.end, t1) - max(moment.start, t0)
                if ov > best_ov + 1e-6:
                    best, best_ov = moment, ov
        moment_view = None
        if best is not None:
            moment_view = {
                "start": best.start,
                "end": best.end,
                "score": best.score,
                "label": best.label,
                "tags": list(best.tags),
                "role": best.role,
                "hero": best.hero,
                "group": best.group,
            }
        facts = _probe_facts(report.path)
        self._send_json(
            {
                "clip": report.path,
                "name": Path(report.path).name,
                "duration": report.duration,
                "media_start": report.media_start,
                "usable_ratio": report.usable_ratio,
                "moment": moment_view,
                **facts,
            }
        )

    def _alternatives(self) -> None:
        """POST /api/alternatives {"plan_json","folder","slot"} — swap bench.

        Reuses the director's bench (monteur.director.review_context): the
        strongest moments NO entry uses, same scoring — reordered so
        moments from the slot's own clip or scene group come first, capped
        at _ALTERNATIVES_LIMIT. Instant: scan cache only, no sift, no AI.
        """
        from monteur.director import review_context
        from monteur.montage import plan_from_dict

        payload = self._read_json()
        if not isinstance(payload.get("plan_json"), dict):
            raise ApiError(
                400, "missing 'plan_json' (the plan a build result carries)"
            )
        plan = plan_from_dict(payload["plan_json"])  # bad -> ValueError -> 400
        try:
            slot = int(payload.get("slot"))
        except (TypeError, ValueError):
            raise ApiError(400, "'slot' must be an entry index (0-based)")
        if slot < 0 or slot >= len(plan.entries):
            raise ApiError(
                400,
                f"slot {slot + 1} is not in this plan "
                f"(it has {len(plan.entries)} entries)",
            )
        reports = _fresh_reports_or_404(str(payload.get("folder") or ""))

        # The director's dossier: its bench IS the swap material (strongest
        # unused moments; review_context owns the scoring and the cap).
        context = review_context(plan, reports)
        slot_info = context["slots"][slot]
        slot_clip = str(slot_info.get("clip") or "")
        slot_group = str(slot_info.get("group") or "")

        def kin(item: dict) -> int:
            if item.get("clip") == slot_clip:
                return 0
            if slot_group and item.get("group") == slot_group:
                return 0
            return 1

        bench = sorted(context["bench"], key=kin)  # stable: keeps bench order
        by_name = {Path(r.path).name: r.path for r in reports}
        alternatives = [
            {
                "clip": by_name.get(str(item.get("clip") or ""), item.get("clip")),
                "name": str(item.get("clip") or ""),
                "start": item.get("start", 0.0),
                "end": item.get("end", 0.0),
                "score": item.get("score", 0.0),
                "label": str(item.get("label") or ""),
                "role": str(item.get("role") or ""),
                "hero": float(item.get("hero") or 0.0),
                "group": str(item.get("group") or ""),
                "same_scene": kin(item) == 0,
            }
            for item in bench[:_ALTERNATIVES_LIMIT]
        ]
        self._send_json({"slot": slot, "alternatives": alternatives})

    def _plan_adjust(self) -> None:
        """POST /api/plan/adjust — one plan tweak, rendered like export.

        Four modes, all pure plan surgery (no re-plan, no sift), all
        rendered through the same plan -> file path as /api/create/export
        so the response is the standard build-result shape:

        * boundary — ``{"slot", "transition"}``:
          monteur.montage.adjust_entry_boundary does the surgery
          (transition set/cleared by the planner's own 0.5 s rule, black
          dip carved or removed). Engine ValueErrors surface as 400s.
        * title — ``{"dip", "title"}``: edits ``plan.title_texts[dip]``
          (the text on that black dip's title card) in place — padded to
          the dips so the alignment invariant holds, "" clears the title.
        * delete — ``{"delete": <slot>}``: monteur.montage.delete_entry
          removes that entry and re-flows the record grid contiguously.
        * move — ``{"move": <slot>, "to": <index>}``:
          monteur.montage.move_entry reorders the entry and re-flows the
          grid. Engine ValueErrors (bad index, empty result) are 400s.
        * resync_audio — ``{"resync_audio": true}``:
          monteur.montage.resync_audio re-lays the SFX layer onto the
          current cut so the sounds land on the drops again after an edit.
        """
        from monteur.montage import (
            ARRANGEMENT_TRANSITIONS,
            adjust_entry_boundary,
            delete_entry,
            move_entry,
            plan_from_dict,
            resync_audio,
        )

        payload = self._read_json()
        if not isinstance(payload.get("plan_json"), dict):
            raise ApiError(
                400, "missing 'plan_json' (the plan a build result carries)"
            )
        if "title" in payload or "dip" in payload:
            self._plan_adjust_title(payload)
            return
        if isinstance(payload.get("sfx"), dict):
            self._plan_adjust_sfx(payload)
            return
        if payload.get("resync_audio"):
            # re-lay the SFX layer onto the CURRENT cut (after a delete/move
            # the sounds no longer land on the drops) — pure plan surgery
            plan = plan_from_dict(payload["plan_json"])  # bad -> ValueError -> 400
            if not plan.entries:
                raise ApiError(400, "the plan has no entries — nothing to re-lay")
            adjusted = resync_audio(plan)
            _persist_plan_edit(payload, adjusted, "resync")
            self._send_json(_plan_export_result(adjusted, payload))
            return
        if "delete" in payload:
            try:
                slot = int(payload.get("delete"))
            except (TypeError, ValueError):
                raise ApiError(400, "'delete' must be an entry index (0-based)")
            plan = plan_from_dict(payload["plan_json"])  # bad -> ValueError -> 400
            # The engine's own validation (bad slot, deleting the last
            # entry) raises ValueError -> the dispatcher's 400.
            adjusted = delete_entry(plan, slot)
            _persist_plan_edit(payload, adjusted, "delete")
            self._send_json(_plan_export_result(adjusted, payload))
            return
        if "move" in payload:
            try:
                slot = int(payload.get("move"))
                to = int(payload.get("to"))
            except (TypeError, ValueError):
                raise ApiError(
                    400, "'move' and 'to' must be entry indices (0-based)"
                )
            plan = plan_from_dict(payload["plan_json"])  # bad -> ValueError -> 400
            adjusted = move_entry(plan, slot, to)
            _persist_plan_edit(payload, adjusted, "move")
            self._send_json(_plan_export_result(adjusted, payload))
            return
        transition = str(payload.get("transition") or "")
        if transition not in ARRANGEMENT_TRANSITIONS:
            valid = ", ".join(ARRANGEMENT_TRANSITIONS)
            raise ApiError(400, f"'transition' must be one of: {valid}")
        try:
            slot = int(payload.get("slot"))
        except (TypeError, ValueError):
            raise ApiError(400, "'slot' must be an entry index (0-based)")
        plan = plan_from_dict(payload["plan_json"])  # bad -> ValueError -> 400
        if not plan.entries:
            raise ApiError(400, "the plan has no entries — nothing to adjust")
        # The engine's own validation (bad slot, slot 0, too-short smash,
        # unremovable dip) raises ValueError -> the dispatcher's 400.
        adjusted = adjust_entry_boundary(plan, slot, transition)
        # Learn from the correction (blueprint 4.3): a change TO a hard cut
        # is the abstract "fewer dissolves" signal; a change to a dissolve
        # the opposite. Record only when the direction actually changed the
        # boundary (the old entry carried a dissolve iff transition > 0).
        if 0 <= slot < len(plan.entries):
            was_dissolve = plan.entries[slot].transition > 1e-6
            if transition == "cut" and was_dissolve:
                self._learn_signal("transition", "*", "cut")
            elif transition == "dissolve" and not was_dissolve:
                self._learn_signal("transition", "*", "dissolve")
        _persist_plan_edit(payload, adjusted, "transition")
        self._send_json(_plan_export_result(adjusted, payload))

    def _plan_adjust_title(self, payload: dict) -> None:
        """The title mode of /api/plan/adjust — pure title_texts surgery.

        Sets the composed act title on ONE black dip: ``title_texts`` is
        padded with "" up to the dips (keeping the dips <-> titles
        alignment), then ``title_texts[dip]`` becomes the given text.
        Entries, dips, cues — everything else — stay bit-identical; a
        ``title:`` note says what changed. Renders like an export.
        """
        from monteur.montage import plan_from_dict

        title = payload.get("title")
        if not isinstance(title, str):
            raise ApiError(400, "'title' must be a string (the title card's text)")
        try:
            dip = int(payload.get("dip"))
        except (TypeError, ValueError):
            raise ApiError(400, "'dip' must be a black-dip index (0-based)")
        plan = plan_from_dict(payload["plan_json"])  # bad -> ValueError -> 400
        if not plan.dips:
            raise ApiError(
                400, "the plan has no black dips — no title slots to edit"
            )
        if dip < 0 or dip >= len(plan.dips):
            raise ApiError(
                400,
                f"dip {dip + 1} is not in this plan "
                f"(it has {len(plan.dips)} black dips)",
            )
        titles = [str(text) for text in plan.title_texts]
        while len(titles) < len(plan.dips):
            titles.append("")
        titles[dip] = title.strip()
        plan.title_texts = titles
        # optional per-dip animation, aligned with the dips like the titles are
        _VALID_ANIMS = ("none", "fade", "slide", "type")
        if "title_anim" in payload:
            anim = str(payload.get("title_anim") or "none").strip().lower()
            if anim not in _VALID_ANIMS:
                raise ApiError(
                    400, f"'title_anim' must be one of: {', '.join(_VALID_ANIMS)}"
                )
            anims = [str(a or "none") for a in plan.title_anims]
            while len(anims) < len(plan.dips):
                anims.append("none")
            anims[dip] = anim
            plan.title_anims = anims
        plan.notes = list(plan.notes) + [
            f"title: dip {dip + 1} reads {titles[dip]!r}"
            if titles[dip]
            else f"title: dip {dip + 1} cleared"
        ]
        _persist_plan_edit(payload, plan, "retitle")
        self._send_json(_plan_export_result(plan, payload))

    def _plan_adjust_sfx(self, payload: dict) -> None:
        """The SFX mode of /api/plan/adjust — pure sound-cue surgery.

        ``payload["sfx"]`` is ``{"action": "add"|"update"|"delete", ...}``:

        * add — ``{time[, duration, kind, query, note]}`` appends a cue.
        * update — ``{index[, time, duration, kind, query, note]}`` edits one.
        * delete — ``{index}`` removes one.

        The index is into the TIME-SORTED SFX layer (what the UI shows).
        Engine ValueErrors (bad index/kind/number) surface as 400s. Renders
        like an export and persists into the project like every other edit.
        """
        from monteur.montage import (
            add_sfx_cue,
            delete_sfx_cue,
            plan_from_dict,
            update_sfx_cue,
        )

        spec = payload["sfx"]
        action = str(spec.get("action") or "")
        plan = plan_from_dict(payload["plan_json"])  # bad -> ValueError -> 400
        try:
            if action == "add":
                adjusted = add_sfx_cue(
                    plan,
                    time=spec.get("time", 0.0),
                    duration=spec.get("duration", 0.5),
                    kind=str(spec.get("kind", "impact")),
                    query=str(spec.get("query", "")),
                    note=str(spec.get("note", "")),
                )
            elif action == "update":
                fields = {
                    k: spec[k]
                    for k in ("time", "duration", "kind", "query", "note")
                    if k in spec
                }
                adjusted = update_sfx_cue(plan, spec.get("index"), **fields)
            elif action == "delete":
                adjusted = delete_sfx_cue(plan, spec.get("index"))
            else:
                raise ApiError(
                    400, "'sfx.action' must be one of: add, update, delete"
                )
        except ValueError as exc:
            raise ApiError(400, str(exc))
        _persist_plan_edit(payload, adjusted, "sfx")
        self._send_json(_plan_export_result(adjusted, payload))

    # -- "Sehen ohne Resolve": thumbnails + preview player -------------------

    def _send_bytes(
        self, body: bytes, content_type: str, status: int = 200, headers=None
    ) -> None:
        """Send raw bytes (images/video) — the binary sibling of _send_json."""
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_GONE:
            pass  # client closed the socket mid-response — nothing to do

    def _thumb(self) -> None:
        """GET /api/thumb?clip=<abs path>&t=<seconds>&w=<px> — one JPEG frame.

        Cache-first: the frame for (clip path, clip mtime, t, w) is extracted
        once into the per-run cache dir and re-served from disk afterwards,
        with long client cache headers on top. EVERY failure — missing or
        malformed params, a clip that doesn't exist, ffmpeg unavailable — is
        a 404 with a tiny placeholder PNG the UI can render as a quiet gray
        tile; a thumbnail must never surface as an error state.
        """
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(self.path).query)

        def param(name: str) -> str:
            values = query.get(name) or [""]
            return values[0]

        def placeholder() -> None:
            self._send_bytes(
                _THUMB_PLACEHOLDER, "image/png", status=404,
                headers={"Cache-Control": "no-store"},
            )

        clip = param("clip").strip()
        if not clip:
            placeholder()
            return
        try:
            time_s = max(0.0, float(param("t") or 0.0))
            width = max(16, min(1920, int(float(param("w") or 320))))
        except (TypeError, ValueError):
            placeholder()
            return
        try:
            cache_path = _thumb_cache_path(clip, time_s, width)  # OSError: gone
            if not os.path.isfile(cache_path):
                # Resolved at CALL time so tests can monkeypatch
                # monteur.preview.extract_thumbnail.
                from monteur.media import MonteurMediaError
                from monteur.preview import extract_thumbnail

                # Extract to a private name, then move into place atomically:
                # two concurrent requests for one frame can't serve halves.
                partial = f"{cache_path}.{secrets.token_hex(4)}.part.jpg"
                try:
                    extract_thumbnail(clip, time_s, partial, width=width)
                    os.replace(partial, cache_path)
                except MonteurMediaError:
                    placeholder()
                    return
                finally:
                    if os.path.exists(partial):
                        try:
                            os.remove(partial)
                        except OSError:
                            pass
            body = Path(cache_path).read_bytes()
        except OSError:
            placeholder()
            return
        self._send_bytes(
            body, "image/jpeg",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    def _create_preview(self) -> None:
        payload = self._read_json()
        if not isinstance(payload.get("plan_json"), dict):
            raise ApiError(
                400, "missing 'plan_json' (the plan a build result carries)"
            )
        job = _new_job("preview")
        threading.Thread(
            target=_run_preview_job,
            args=(job, payload),
            name=f"monteur-preview-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _create_export_video(self) -> None:
        """POST /api/create/export-video — the Direct Export job.

        Validation happens here (a missing ``plan_json``/``target_dir``
        or an unknown ``quality`` is a 400 with a user-ready message);
        the actual render runs as an ``"export-video"`` job — see
        :func:`_run_export_video_job`.
        """
        payload = self._read_json()
        if not isinstance(payload.get("plan_json"), dict):
            raise ApiError(
                400, "missing 'plan_json' (the plan a build result carries)"
            )
        if not str(payload.get("target_dir") or "").strip():
            raise ApiError(
                400, "missing 'target_dir' (the folder for the finished video)"
            )
        quality = payload.get("quality")
        if quality not in (None, "", "high", "medium"):
            raise ApiError(400, "'quality' must be 'high' or 'medium'")
        job = _new_job("export-video")
        threading.Thread(
            target=_run_export_video_job,
            args=(job, payload),
            name=f"monteur-export-video-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _send_file_range(
        self, path: str, content_type: str, missing_message: str = ""
    ) -> None:
        """Serve a local file with single-range support, streamed in chunks.

        The one shared byte-serving path behind ``/api/preview/<token>.mp4``
        and ``/api/media`` — ``<video>``/``<audio>`` seeking needs true 206
        partial responses, and the stdlib handler has none: ``bytes=a-b`` /
        ``bytes=a-`` / ``bytes=-suffix`` get a 206 with ``Content-Range``,
        an unsatisfiable range gets a 416, anything malformed falls back to
        the full 200 (per RFC 7233 an unparseable Range header may be
        ignored). The body is STREAMED from disk in 512 KiB chunks — media
        originals can be multi-gigabyte camera files and must never be read
        into memory whole. A file that cannot be opened is a 404 carrying
        ``missing_message`` (or a generic line).
        """
        try:
            handle = open(path, "rb")
        except OSError:
            raise ApiError(
                404, missing_message or f"cannot read {Path(path).name}"
            )
        with handle:
            size = os.fstat(handle.fileno()).st_size
            headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
            status = 200
            start, end = 0, size - 1
            range_header = (self.headers.get("Range") or "").strip()
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if match and (match.group(1) or match.group(2)):
                if match.group(1):
                    start = int(match.group(1))
                    end = (
                        min(int(match.group(2)), size - 1)
                        if match.group(2) else size - 1
                    )
                else:  # suffix form: the last N bytes
                    start = max(0, size - int(match.group(2)))
                    end = size - 1
                if start >= size or start > end:
                    self._send_bytes(
                        b"", content_type, status=416,
                        headers={"Content-Range": f"bytes */{size}", **headers},
                    )
                    return
                status = 206
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            length = max(0, end - start + 1) if size else 0
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(512 * 1024, remaining))
                    if not chunk:
                        break  # truncated on disk mid-send — stop honestly
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except _CLIENT_GONE:
                pass  # client closed the socket mid-stream — nothing to do

    def _preview_file(self, name: str) -> None:
        """GET /api/preview/<token>.mp4 — serve a finished preview with Range.

        Byte serving via :meth:`_send_file_range`. The name must match the
        server's own token naming — this route can only ever serve the
        preview directory.
        """
        if not _PREVIEW_NAME_RE.fullmatch(name):
            raise ApiError(404, f"no preview {name!r}")
        path = Path(_preview_dir()) / name
        self._send_file_range(
            str(path), "video/mp4",
            missing_message=(
                "this preview is gone — it was replaced by a newer one; "
                "render the preview again"
            ),
        )

    def _media(self) -> None:
        """GET /api/media?path=<abs path> — Range-capable media serving.

        The playback surface behind the moment player and the virtual
        timeline playout: serves the clip's PLAYBACK PROXY when a fresh
        one exists (:func:`monteur.proxies.fresh_proxy` — small H.264,
        dense keyframes, +faststart, always ``video/mp4``) and falls back
        to the original file otherwise (Content-Type by suffix), so
        playback works with or without proxies. Like ``/api/thumb`` the
        path is an absolute local file that must exist — Studio is a
        local single-user tool (the server binds to 127.0.0.1).
        """
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(self.path).query)
        raw = (query.get("path") or [""])[0].strip()
        if not raw:
            raise ApiError(400, "missing 'path' (absolute path to a media file)")
        path = os.path.abspath(raw)
        if not os.path.isfile(path):
            raise ApiError(404, f"no such media file: {raw}")
        serve_path = path
        content_type = _MEDIA_TYPES.get(
            Path(path).suffix.lower(), "application/octet-stream"
        )
        try:
            import importlib

            proxies = importlib.import_module("monteur.proxies")
            fresh = proxies.fresh_proxy(path)
        except Exception:  # noqa: BLE001 — proxies are an upgrade, not a gate
            fresh = None
        if fresh is not None:
            serve_path, content_type = str(fresh), "video/mp4"
        self._send_file_range(serve_path, content_type)

    def _proxies_start(self) -> None:
        """POST /api/proxies {"folder"} — (re)start the proxy transcodes."""
        payload = self._read_json()
        folder = str(payload.get("folder") or "").strip()
        if not folder:
            raise ApiError(400, "missing 'folder' (path to your footage)")
        job = _start_proxies_job(folder)
        self._send_json({"job": job["id"]})

    # -- drafts (the Create wizard's WIP memory — monteur.drafts) -----------

    def _drafts_list(self) -> None:
        from monteur import drafts

        self._send_json({"drafts": drafts.list_drafts()})

    def _drafts_get(self, draft_id: str) -> None:
        from monteur import drafts

        record = drafts.load_draft(draft_id)
        if record is None:
            raise ApiError(404, f"unknown draft {draft_id!r}")
        self._send_json(record)

    def _drafts_save(self) -> None:
        # drafts.save_draft validates the minimal resumable shape (folder +
        # plan_json) and raises ValueError with a user-ready message — the
        # dispatcher turns that into the 400 this endpoint promises.
        from monteur import drafts

        self._send_json(drafts.save_draft(self._read_json()))

    def _drafts_delete(self, draft_id: str) -> None:
        from monteur import drafts

        self._send_json({"deleted": drafts.delete_draft(draft_id)})

    # -- projects (first-class Cut projects — monteur.projects) -------------

    def _projects_list(self) -> None:
        # migrate_drafts is idempotent and lossless (drafts.json is only
        # read) — running it on every list means existing drafts surface as
        # projects without a separate migration step, and re-runs add nothing.
        from monteur import projects

        projects.migrate_drafts()
        self._send_json({"projects": projects.list_projects()})

    def _projects_create(self) -> None:
        from monteur import projects

        payload = self._read_json()
        project = projects.create_project(
            payload.get("name") or "",
            options=payload.get("options") if isinstance(payload.get("options"), dict) else None,
            media_pool=payload.get("media") if isinstance(payload.get("media"), list) else None,
            plan=payload.get("plan") if isinstance(payload.get("plan"), dict) else None,
            notes=payload.get("notes") if isinstance(payload.get("notes"), list) else None,
        )
        self._send_json(projects.project_to_dict(project))

    def _projects_get(self, project_id: str) -> None:
        from monteur import projects

        project = projects.load_project(project_id)
        if project is None:
            raise ApiError(404, f"unknown project {project_id!r}")
        self._send_json(projects.project_to_dict(project))

    def _projects_update(self, project_id: str) -> None:
        from monteur import projects

        project = projects.load_project(project_id)
        if project is None:
            raise ApiError(404, f"unknown project {project_id!r}")
        payload = self._read_json()
        if isinstance(payload.get("name"), str) and payload["name"].strip():
            project.name = payload["name"].strip()
        if isinstance(payload.get("options"), dict):
            project.options = dict(payload["options"])
        if isinstance(payload.get("media_pool"), list):
            project.media_pool = [
                projects._normalize_pool_entry(entry)
                for entry in payload["media_pool"]
                if isinstance(entry, dict) and entry.get("path")
            ]
        if "plan" in payload:
            plan = payload["plan"]
            project.plan = plan if isinstance(plan, dict) and plan else None
            # snapshot every distinct cut so a past version is never lost
            # (add_version dedupes against the last snapshot, so autosave is safe)
            if project.plan:
                projects.add_version(project, project.plan)
        if isinstance(payload.get("exports"), list):
            project.exports = [e for e in payload["exports"] if isinstance(e, dict)]
        if isinstance(payload.get("notes"), list):
            project.notes = [str(n) for n in payload["notes"]]
        projects.save_project(project)
        self._send_json(projects.project_to_dict(project))

    def _projects_delete(self, project_id: str) -> None:
        from monteur import projects

        self._send_json({"deleted": projects.delete_project(project_id)})

    def _project_versions(self, project_id: str) -> None:
        """GET /api/projects/<id>/versions — the saved-cut history, newest first."""
        from monteur import projects

        project = projects.load_project(project_id)
        if project is None:
            raise ApiError(404, f"unknown project {project_id!r}")
        self._send_json({"versions": projects.list_versions(project)})

    def _project_version_restore(self, project_id: str, version_id: str) -> None:
        """POST /api/projects/<id>/versions/<vid>/restore — bring a past cut back."""
        from monteur import projects

        project = projects.load_project(project_id)
        if project is None:
            raise ApiError(404, f"unknown project {project_id!r}")
        if not projects.restore_version(project, version_id):
            raise ApiError(404, f"unknown version {version_id!r}")
        self._send_json(projects.project_to_dict(project))

    def _project_version_changes(self, project_id: str, version_id: str) -> None:
        """GET /api/projects/<id>/versions/<vid>/changes — what changed since <vid>.

        Diffs the snapshot against the project's CURRENT plan — the handoff list
        (added / removed / re-trimmed / retimed / transition, plus length +
        tempo) a sound or VFX editor needs.
        """
        from monteur import changelist, projects

        project = projects.load_project(project_id)
        if project is None:
            raise ApiError(404, f"unknown project {project_id!r}")
        old = projects.load_version(project, version_id)
        if old is None:
            raise ApiError(404, f"unknown version {version_id!r}")
        cl = changelist.diff_plans(old, project.plan or {})
        self._send_json(cl.to_dict())

    def _project_pool_get(self, project_id: str) -> None:
        """GET /api/projects/<id>/pool — the media pool resolved to clips.

        Expands the project's referenced files/folders into clip cards, each
        with cheap cached status (sifted / proxy_fresh / labeled). No media is
        opened or decoded — the pool page shows KNOWLEDGE, not video.
        """
        from monteur import projects

        project = projects.load_project(project_id)
        if project is None:
            raise ApiError(404, f"unknown project {project_id!r}")
        self._send_json(_resolve_pool(project))

    def _project_pool_update(self, project_id: str) -> None:
        """POST /api/projects/<id>/pool {"add":{path,kind}} | {"remove":path}.

        Adds or removes a REFERENCE in the media pool — the file/folder on
        disk is never touched, only the project's reference list. Returns the
        re-resolved pool so the page re-renders in one round trip.
        """
        from monteur import projects

        project = projects.load_project(project_id)
        if project is None:
            raise ApiError(404, f"unknown project {project_id!r}")
        payload = self._read_json()
        add = payload.get("add")
        remove = payload.get("remove")
        if isinstance(add, dict) and str(add.get("path") or "").strip():
            projects.add_to_pool(project, add["path"], add.get("kind"))
        elif isinstance(add, str) and add.strip():
            projects.add_to_pool(project, add)
        elif isinstance(remove, str) and remove.strip():
            projects.remove_from_pool(project, remove)
        else:
            raise ApiError(
                400,
                "expected {'add': {'path','kind'?}} or {'remove': '<path>'}",
            )
        self._send_json(_resolve_pool(project))

    def _pool_clip_list(self, project_id: str) -> tuple[list, bool]:
        """Validate a {clips:[abs paths], see?} body against a project's pool.

        The clips must be a non-empty list of paths that the project actually
        references (a file entry or a member of a folder entry) — so an
        analyze/see request can only sift footage already in the pool. Returns
        ``(chosen_abs_paths, see)``."""
        from monteur import projects

        project = projects.load_project(project_id)
        if project is None:
            raise ApiError(404, f"unknown project {project_id!r}")
        payload = self._read_json()
        raw = payload.get("clips")
        if not isinstance(raw, list) or not raw:
            raise ApiError(400, "missing 'clips' (a non-empty list of clip paths)")
        wanted = [os.path.abspath(str(p)) for p in raw if str(p).strip()]
        if not wanted:
            raise ApiError(400, "missing 'clips' (a non-empty list of clip paths)")
        pooled = {c["path"] for c in _resolve_pool(project)["clips"]}
        chosen = [p for p in wanted if p in pooled]
        if not chosen:
            raise ApiError(
                400,
                "none of the selected clips are in this project's pool — add "
                "them first",
            )
        return chosen, bool(payload.get("see"))

    def _project_analyze(self, project_id: str) -> None:
        """POST /api/projects/<id>/analyze {clips:[paths], see?} — sift a subset.

        Analyzes ONLY the selected clips (the staged pool's primary action),
        as a cancellable job the scan panel drives; optionally runs vision when
        ``see`` is set. Returns ``{"job": id}``."""
        clips, see = self._pool_clip_list(project_id)
        job = _new_job("scan", {"project": project_id})
        threading.Thread(
            target=_run_analyze_job,
            args=(job, clips, see, project_id),
            name=f"monteur-analyze-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _project_see(self, project_id: str) -> None:
        """POST /api/projects/<id>/see {clips:[paths]} — Claude-check a subset.

        Runs the vision pass on ONLY the selected clips (typically the good
        ones judged after analysis), as its own explicit cancellable job.
        Returns ``{"job": id}``."""
        clips, _see = self._pool_clip_list(project_id)
        job = _new_job("scan", {"project": project_id})
        threading.Thread(
            target=_run_see_job,
            args=(job, clips, project_id),
            name=f"monteur-see-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _project_clips(self, project_id: str) -> None:
        """GET /api/projects/<id>/clips — the analyzed MOMENTS, grouped by clip.

        The moments are the stretches the sift pulls out — the ones that
        actually land in the cut — so this is what the review step shows. Each
        clip carries its list of moments, and each moment carries what Claude
        saw in THAT stretch (label, tags, role, hero, daylight, shot size), its
        time span, and the editor's own per-moment note. Reads the PROJECT's
        stored analysis (``projects.load_reports``), so it works on a re-opened
        project without a fresh scan."""
        from monteur import projects

        project = projects.load_project(project_id)
        if project is None:
            raise ApiError(404, f"unknown project {project_id!r}")
        reports = projects.load_reports(project)
        notes = project.moment_notes or {}
        ratings = project.moment_ratings or {}
        excludes = project.moment_excludes or {}
        clips = []
        for report in reports:
            moments = []
            for m in sorted(report.moments, key=lambda m: m.start):
                key = _moment_key(report.path, m.start)
                moments.append({
                    "start": round(float(m.start), 2),
                    "end": round(float(m.end), 2),
                    "score": round(float(m.score or 0.0), 3),
                    "label": str(m.label or ""),
                    "tags": [str(t) for t in (m.tags or []) if t],
                    "role": str(m.role or ""),
                    "hero": round(float(m.hero or 0.0), 2),
                    "daylight": str(m.daylight or ""),
                    "shot_size": str(m.shot_size or ""),
                    "note": str(notes.get(key, "")),
                    "rating": int(ratings.get(key, 0) or 0),
                    "exclude": bool(excludes.get(key, False)),
                })
            clips.append({
                "path": report.path,
                "name": os.path.basename(report.path),
                "duration": round(float(report.duration), 2),
                "usable_ratio": round(float(report.usable_ratio), 3),
                "labeled": _report_is_labeled(report),
                "moments": moments,
            })
        self._send_json({"clips": clips})

    def _project_clip_note(self, project_id: str) -> None:
        """POST /api/projects/<id>/clip-note {path, note} — set an editor note.

        Stores the editor's own note for one clip (keyed by absolute path in
        ``project.clip_notes``), so it survives re-analysis and rides into the
        composer's dossier. An empty note clears the entry."""
        from monteur import projects

        project = projects.load_project(project_id)
        if project is None:
            raise ApiError(404, f"unknown project {project_id!r}")
        payload = self._read_json()
        path = str(payload.get("path") or "").strip()
        if not path:
            raise ApiError(400, "missing 'path' (the clip to annotate)")
        note = str(payload.get("note") or "").strip()
        key = os.path.abspath(path)
        notes = dict(project.clip_notes or {})
        if note:
            notes[key] = note
        else:
            notes.pop(key, None)
        project.clip_notes = notes
        projects.save_project(project)
        self._send_json({"ok": True, "path": key, "note": note})

    def _project_moment_note(self, project_id: str) -> None:
        """POST /api/projects/<id>/moment-note {path, start, note} — annotate one moment.

        Stores the editor's own note for one moment (keyed by clip path + start
        in ``project.moment_notes``), so it survives re-analysis and rides onto
        that exact moment in the composer's dossier. An empty note clears it."""
        from monteur import projects

        project = projects.load_project(project_id)
        if project is None:
            raise ApiError(404, f"unknown project {project_id!r}")
        payload = self._read_json()
        path = str(payload.get("path") or "").strip()
        if not path:
            raise ApiError(400, "missing 'path' (the clip the moment is in)")
        if payload.get("start") is None:
            raise ApiError(400, "missing 'start' (the moment's start time)")
        try:
            start = float(payload.get("start"))
        except (TypeError, ValueError):
            raise ApiError(400, "'start' must be a number (the moment's start time)")
        note = str(payload.get("note") or "").strip()
        key = _moment_key(path, start)
        notes = dict(project.moment_notes or {})
        if note:
            notes[key] = note
        else:
            notes.pop(key, None)
        project.moment_notes = notes
        projects.save_project(project)
        self._send_json({"ok": True, "key": key, "note": note})

    def _project_moment_mark(self, project_id: str) -> None:
        """POST /api/projects/<id>/moment-mark {path, start, note?, rating?, exclude?}.

        The Moments inspector's one write: a PARTIAL update of a moment's editor
        marks (keyed by clip path + start). Only the fields present in the body
        change — ``note`` (str, "" clears), ``rating`` (1..5, 0 clears the
        override), ``exclude`` (bool). Everything survives re-analysis and rides
        onto the moment before the composer reads it (an excluded moment is
        dropped before planning)."""
        from monteur import projects

        project = projects.load_project(project_id)
        if project is None:
            raise ApiError(404, f"unknown project {project_id!r}")
        payload = self._read_json()
        path = str(payload.get("path") or "").strip()
        if not path:
            raise ApiError(400, "missing 'path' (the clip the moment is in)")
        if payload.get("start") is None:
            raise ApiError(400, "missing 'start' (the moment's start time)")
        try:
            start = float(payload.get("start"))
        except (TypeError, ValueError):
            raise ApiError(400, "'start' must be a number (the moment's start time)")
        key = _moment_key(path, start)
        result = {"ok": True, "key": key}
        if "note" in payload:
            note = str(payload.get("note") or "").strip()
            notes = dict(project.moment_notes or {})
            if note:
                notes[key] = note
            else:
                notes.pop(key, None)
            project.moment_notes = notes
            result["note"] = note
        if "rating" in payload:
            try:
                rating = int(payload.get("rating") or 0)
            except (TypeError, ValueError):
                raise ApiError(400, "'rating' must be a whole number 0..5")
            if not 0 <= rating <= 5:
                raise ApiError(400, "'rating' must be between 0 and 5 (0 clears it)")
            ratings = dict(project.moment_ratings or {})
            if rating:
                ratings[key] = rating
            else:
                ratings.pop(key, None)
            project.moment_ratings = ratings
            result["rating"] = rating
        if "exclude" in payload:
            exclude = bool(payload.get("exclude"))
            excludes = dict(project.moment_excludes or {})
            if exclude:
                excludes[key] = True
            else:
                excludes.pop(key, None)
            project.moment_excludes = excludes
            result["exclude"] = exclude
        projects.save_project(project)
        self._send_json(result)

    def _project_series(self, project_id: str) -> None:
        """POST /api/projects/<id>/series {series:N, canvas?} — long form -> Shorts.

        Extracts up to N genuinely different vertical Shorts from the beats
        this project's cut actually used, reusing its persisted sift (no
        re-scan). Returns ``{"job": id}``; the job result carries the shorts'
        plans, seeds and notes."""
        payload = self._read_json()
        job = _new_job("series")
        threading.Thread(
            target=_run_project_series_job,
            args=(job, project_id, payload),
            name=f"monteur-project-series-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _project_series_save(self, project_id: str) -> None:
        """POST /api/projects/<id>/series/save {shorts:[{plan_json,label?}]}.

        Persists the chosen Shorts as CHILD projects that reference the same
        footage, so each opens in the timeline for its own refinement and
        export. Returns ``{"created": [{"id","name"}]}``."""
        from monteur import projects

        project = projects.load_project(project_id)
        if project is None:
            raise ApiError(404, f"unknown project {project_id!r}")
        payload = self._read_json()
        shorts = payload.get("shorts")
        if not isinstance(shorts, list) or not shorts:
            raise ApiError(400, "missing 'shorts' (a non-empty list of {plan_json})")
        created = _save_series_shorts(project, shorts)
        if not created:
            raise ApiError(400, "no valid shorts to save (each needs a 'plan_json')")
        self._send_json({"created": created})

    def _learn_signal(self, family: str, context: str, direction: str) -> None:
        """Record a correction signal, best-effort (learning never blocks an edit)."""
        try:
            from monteur import preferences

            preferences.record_signal(family, context, direction)
        except Exception:  # pragma: no cover - learning is never a gate
            pass

    def _preferences_get(self) -> None:
        """GET /api/preferences — the inspectable "what Monteur learned" panel."""
        from monteur import preferences

        self._send_json(preferences.inspect())

    def _preferences_reset(self) -> None:
        """POST /api/preferences/reset — forget everything Monteur learned."""
        from monteur import preferences

        self._send_json({"reset": preferences.reset()})

    def _preferences_signal(self) -> None:
        """POST /api/preferences/signal {family, context, direction}.

        The Studio's hook for a committed correction whose direction only
        the board knows (e.g. a swap to a closer shot: ``{"shot_size",
        "climax", "close"}``). Records the ABSTRACT signal — never the
        literal clip — and returns the refreshed inspection view. One
        signal tips nothing; only a REPEATED signal folds into the next
        plan (blueprint 4.3).
        """
        from monteur import preferences

        payload = self._read_json()
        try:
            preferences.record_signal(
                str(payload.get("family") or ""),
                str(payload.get("context") or "*"),
                str(payload.get("direction") or ""),
            )
        except ValueError as exc:  # missing family/direction
            raise ApiError(400, str(exc))
        self._send_json(preferences.inspect())

    def _find(self) -> None:
        """Search the vision cache — instant and offline, so no job."""
        from monteur.find import search_footage

        payload = self._read_json()
        folder = payload.get("folder", "")
        if not folder:
            raise ApiError(400, "missing 'folder' (path to your footage)")
        try:
            limit = int(payload.get("limit") or 20)
        except (TypeError, ValueError):
            raise ApiError(400, "'limit' must be a number")
        try:
            shots = search_footage(folder, str(payload.get("query") or ""), limit=limit)
        except ValueError as exc:  # empty/unusable query
            raise ApiError(400, str(exc))
        except FileNotFoundError as exc:
            # No vision cache yet — a soft error (HTTP 200): the UI turns it
            # into "turn on Let Claude watch", not into a failure.
            self._send_json({"error": str(exc)})
            return
        self._send_json({"shots": [asdict(shot) for shot in shots]})

    # -- movie endpoints (the Studio's Movie view) --------------------------

    def _movie_dir(self, payload: dict) -> str:
        project_dir = str(payload.get("project_dir") or "").strip()
        if not project_dir:
            raise ApiError(400, "missing 'project_dir' (the movie project folder)")
        return project_dir

    def _movie_scene_number(self, payload: dict) -> int:
        try:
            return int(payload.get("scene"))
        except (TypeError, ValueError):
            raise ApiError(400, "missing or invalid 'scene' (the scene number)")

    def _movie_load(self) -> None:
        payload = self._read_json()
        project_dir = self._movie_dir(payload)
        movie = _movie_module()
        try:
            project = movie.load_project(project_dir)
        except (FileNotFoundError, ValueError, OSError) as exc:
            raise ApiError(400, str(exc))
        _register_movie_recent(project_dir, project)  # index it on the Home
        self._send_json(_movie_payload(movie, project))

    def _movie_new(self) -> None:
        payload = self._read_json()
        self._movie_dir(payload)
        if not str(payload.get("brief") or "").strip():
            raise ApiError(400, "missing 'brief' (the film idea + constraints)")
        job = _new_job("movie")
        threading.Thread(
            target=_run_movie_job,
            args=(job, payload),
            name=f"monteur-movie-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _movie_assign(self) -> None:
        payload = self._read_json()
        project_dir = self._movie_dir(payload)
        scene_number = self._movie_scene_number(payload)
        movie = _movie_module()
        try:
            project = movie.load_project(project_dir)
            movie.assign_scene(
                project, scene_number, str(payload.get("folder") or "")
            )
            movie.save_project(project, project_dir)
        except (FileNotFoundError, ValueError, OSError) as exc:
            raise ApiError(400, str(exc))
        self._send_json(_movie_payload(movie, project))

    def _movie_check(self) -> None:
        payload = self._read_json()
        self._movie_dir(payload)
        self._movie_scene_number(payload)
        job = _new_job("scene-check")
        threading.Thread(
            target=_run_scene_check_job,
            args=(job, payload),
            name=f"monteur-scene-check-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _movie_assemble(self) -> None:
        payload = self._read_json()
        self._movie_dir(payload)
        job = _new_job("movie-assemble")
        threading.Thread(
            target=_run_movie_assemble_job,
            args=(job, payload),
            name=f"monteur-movie-assemble-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _find_job(self, job_id: str) -> dict:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if job is None:
            raise ApiError(404, f"unknown job {job_id!r}")
        return job

    def _jobs_get(self, job_id: str) -> None:
        self._send_json(_job_view(self._find_job(job_id)))

    def _jobs_list(self) -> None:
        """GET /api/jobs?state=running[&kind=scan] — the job registry, filtered.

        The single source of truth for "what is still running", so the UI can
        reattach to a job it navigated away from (a scan the footage page left
        behind, a build the storyboard left behind) WITHOUT storing anything in
        the browser. Newest first; the ``cancel`` Event is never serialised."""
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(self.path).query)
        want_state = (query.get("state") or [""])[0].strip()
        want_kind = (query.get("kind") or [""])[0].strip()
        with _JOBS_LOCK:
            snapshot = list(_JOBS.values())  # copy out, then view without the lock
        snapshot.sort(key=lambda j: j["created"], reverse=True)
        views = [
            _job_view(job)
            for job in snapshot
            if (not want_state or job["state"] == want_state)
            and (not want_kind or job["kind"] == want_kind)
        ]
        self._send_json({"jobs": views})

    def _jobs_cancel(self, job_id: str) -> None:
        # Setting the event on a finished job is a harmless no-op — the
        # response is {"ok": true} either way, so the UI never has to race
        # its cancel button against job completion.
        self._find_job(job_id)["cancel"].set()
        self._send_json({"ok": True})

    def _pick(self) -> None:
        payload = self._read_json()
        kind = payload.get("kind", "")
        if kind not in ("folder", "music", "file"):
            raise ApiError(400, "'kind' must be 'folder', 'music' or 'file'")
        self._send_json(_native_pick(kind))

    def _browse_list(self) -> None:
        """GET /api/browse/list?path=<dir> — an in-app directory listing.

        The Explorer panel of the Media workspace: the current folder's
        SUBFOLDERS and VIDEO FILES, so the user can navigate the disk and drag
        clips into the pool without a native OS dialog. Deterministic and
        offline — folders come from ``os.listdir`` (directories only), video
        files from :func:`monteur.sift.list_media` (by extension, never a
        probe). An empty ``path`` starts at the home directory; a missing or
        unreadable directory is a soft 400, never a crash. Hidden entries
        (dotfiles) are skipped. Returns ``{path, parent, folders, files}``
        where ``parent`` is ``""`` at the filesystem root.
        """
        from urllib.parse import parse_qs, urlsplit

        from monteur.media import MonteurMediaError
        from monteur.sift import list_media

        query = parse_qs(urlsplit(self.path).query)
        raw = (query.get("path") or [""])[0].strip()
        start = os.path.expanduser(raw) if raw else os.path.expanduser("~")
        path = os.path.abspath(start)
        if not os.path.isdir(path):
            raise ApiError(400, f"not a directory: {raw or path}")
        try:
            names = sorted(os.listdir(path), key=lambda s: s.lower())
        except OSError as exc:
            raise ApiError(400, f"cannot read directory: {exc}")
        folders = []
        for name in names:
            if name.startswith("."):
                continue
            full = os.path.join(path, name)
            try:
                if os.path.isdir(full):
                    folders.append({"path": full, "name": name})
            except OSError:  # noqa: PERF203 — a bad entry is skipped, not fatal
                continue
        try:
            media = list_media(path)
        except MonteurMediaError:
            media = []
        files = [
            {"path": str(p), "name": p.name}
            for p in media
            if not p.name.startswith(".")
        ]
        parent = os.path.dirname(path)
        if parent == path:  # filesystem root — no further up
            parent = ""
        self._send_json(
            {"path": path, "parent": parent, "folders": folders, "files": files}
        )

    # -- AI connection settings ---------------------------------------------

    def _prefs_get(self) -> None:
        """GET /api/prefs -> {prefs: {...}}. The small global UI preferences
        (theme, fps, 'see'), also injected into the page at serve time."""
        from monteur.settings import ui_prefs

        self._send_json({"prefs": ui_prefs()})

    def _prefs_set(self) -> None:
        """POST /api/prefs {prefs: {key: value}} — merge scalar UI preferences
        into settings.json (server-side, not browser localStorage)."""
        from monteur.settings import save_settings, ui_prefs

        payload = self._read_json()
        incoming = payload.get("prefs")
        if not isinstance(incoming, dict):
            raise ApiError(400, "'prefs' must be an object of preference key/values")
        merged = ui_prefs()
        for key, value in incoming.items():
            if isinstance(key, str) and isinstance(value, (str, int, float, bool)):
                merged[key] = value
        save_settings({"ui_prefs": merged})
        self._send_json({"prefs": merged})

    def _favorites_get(self) -> None:
        """GET /api/favorites -> {favorites: [paths]}. The footage-folder
        favourites, persisted in settings.json so they survive a relaunch (the
        old browser-localStorage store was lost on a port/origin change)."""
        from monteur.settings import folder_favorites

        self._send_json({"favorites": folder_favorites()})

    def _favorites_set(self) -> None:
        """POST /api/favorites {favorites: [paths]} — replace the whole list.
        The client sends the full list on every add/remove; the server
        normalises (strings, stripped, de-duped, capped) and saves it."""
        from monteur.settings import (
            _MAX_FOLDER_FAVORITES,
            folder_favorites,
            save_settings,
        )

        payload = self._read_json()
        raw = payload.get("favorites")
        if not isinstance(raw, list):
            raise ApiError(400, "'favorites' must be a list of folder paths")
        cleaned: list[str] = []
        for p in raw:
            if isinstance(p, str) and p.strip() and p.strip() not in cleaned:
                cleaned.append(p.strip())
            if len(cleaned) >= _MAX_FOLDER_FAVORITES:
                break
        save_settings({"folder_favorites": cleaned})
        self._send_json({"favorites": folder_favorites()})

    def _settings_get(self) -> None:
        self._send_json(_settings_view())

    def _settings_set(self) -> None:
        from monteur.settings import save_settings

        payload = self._read_json()
        updates: dict = {}
        if "backend" in payload:
            backend = str(payload.get("backend") or "").strip().lower()
            if backend not in ("auto", "api", "claude-cli"):
                raise ApiError(
                    400, "'backend' must be 'auto', 'api' or 'claude-cli'"
                )
            updates["ai_backend"] = backend
        if "api_key" in payload:
            raw = payload.get("api_key")
            if not isinstance(raw, str):
                raise ApiError(400, "'api_key' must be a string ('' clears it)")
            key = raw.strip()
            if any(ch.isspace() for ch in key):
                raise ApiError(
                    400,
                    "that doesn't look like an API key — keys are one "
                    "unbroken string with no spaces",
                )
            updates["api_key"] = key
        if "resolve_python" in payload:
            raw_path = payload.get("resolve_python")
            if not isinstance(raw_path, str):
                raise ApiError(
                    400, "'resolve_python' must be a string ('' clears it)"
                )
            resolve_path = raw_path.strip()
            if resolve_path and not os.path.isfile(resolve_path):
                raise ApiError(
                    400,
                    f"there is no file at {resolve_path!r} — point this at a "
                    "Python program (like C:\\Program Files\\Python311\\"
                    "python.exe), or use “Find a compatible Python” "
                    "and let Monteur locate one",
                )
            updates["resolve_python"] = resolve_path
        if "update_channel" in payload:
            chan = str(payload.get("update_channel") or "").strip().lower()
            if chan not in ("stable", "dev"):
                raise ApiError(400, "'update_channel' must be 'stable' or 'dev'")
            updates["update_channel"] = chan
        if updates:
            save_settings(updates)
        self._send_json(_settings_view())

    def _settings_test(self) -> None:
        job = _new_job("ai-test")
        threading.Thread(
            target=_run_ai_test_job,
            args=(job,),
            name=f"monteur-ai-test-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    # -- YouTube upload connection (monteur.youtube) --------------------------

    def _youtube_status(self) -> None:
        self._send_json(_youtube_status_view())

    def _youtube_credentials(self) -> None:
        """POST /api/youtube/credentials — save (or clear) the OAuth client.

        Both values stripped; both must be non-empty together — or both
        empty, which clears them AND disconnects (a token minted for the
        old Google project is meaningless under a new one).
        """
        from monteur.settings import save_settings

        payload = self._read_json()
        raw_id = payload.get("client_id")
        raw_secret = payload.get("client_secret")
        if not isinstance(raw_id, str) or not isinstance(raw_secret, str):
            raise ApiError(
                400,
                "'client_id' and 'client_secret' must be strings "
                "('' for both clears them)",
            )
        client_id, client_secret = raw_id.strip(), raw_secret.strip()
        if bool(client_id) != bool(client_secret):
            raise ApiError(
                400,
                "paste BOTH the client id and the client secret — Google's "
                "Desktop-app credentials come as a pair (send both empty "
                "to clear them)",
            )
        updates = {
            "youtube_client_id": client_id,
            "youtube_client_secret": client_secret,
        }
        if not client_id:  # clearing the project disconnects the channel too
            updates["youtube_refresh_token"] = ""
            updates["youtube_channel"] = ""
        save_settings(updates)
        self._send_json(_youtube_status_view())

    def _youtube_connect(self) -> None:
        """POST /api/youtube/connect — start the loopback OAuth flow.

        The running server IS the loopback target (RFC 8252): the consent
        URL redirects to this server's own port on 127.0.0.1, which
        Google's desktop-app clients accept without pre-registration. The
        single-use state (and the redirect_uri the exchange must repeat)
        are remembered module-side until the callback arrives.
        """
        from monteur import youtube
        from monteur.settings import youtube_client_id, youtube_client_secret

        client_id, client_secret = youtube_client_id(), youtube_client_secret()
        if not (client_id and client_secret):
            raise ApiError(
                400,
                "add your Google OAuth client id and secret first — "
                "Settings → YouTube explains the one-time setup",
            )
        state = secrets.token_urlsafe(16)
        redirect_uri = (
            f"http://127.0.0.1:{self.server.server_address[1]}"
            "/api/youtube/callback"
        )
        with _YT_OAUTH_LOCK:
            _YT_OAUTH["state"] = state
            _YT_OAUTH["redirect_uri"] = redirect_uri
        self._send_json(
            {
                "auth_url": youtube.auth_url(client_id, redirect_uri, state),
                "redirect_uri": redirect_uri,
            }
        )

    def _youtube_html(self, heading: str, text: str, status: int = 200,
                      close: bool = False) -> None:
        """A tiny self-contained HTML page — the callback talks to a browser
        tab Google opened, not to the app's fetch()."""
        script = "<script>window.close();</script>" if close else ""
        body = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{heading}</title></head>"
            "<body style='font-family: system-ui, sans-serif; margin: 3em;"
            " max-width: 36em;'>"
            f"<h1 style='font-size: 1.2em;'>{heading}</h1><p>{text}</p>"
            f"{script}</body></html>"
        )
        self._send_bytes(
            body.encode("utf-8"), "text/html; charset=utf-8", status=status
        )

    def _youtube_callback(self) -> None:
        """GET /api/youtube/callback?code&state — Google's loopback redirect.

        Validates the single-use state, exchanges the code for tokens,
        stores the refresh token, and renders a self-closing page. Every
        failure renders a plain readable page instead of JSON — the only
        reader is a human in a browser tab.
        """
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(self.path).query)

        def param(name: str) -> str:
            return (query.get(name) or [""])[0]

        if param("error"):
            self._youtube_html(
                "YouTube not connected",
                f"Google reported: {param('error')}. You can close this tab "
                "and try again from Monteur's settings.",
                status=400,
            )
            return
        code, state = param("code"), param("state")
        with _YT_OAUTH_LOCK:
            expected = _YT_OAUTH.get("state") or ""
            redirect_uri = _YT_OAUTH.get("redirect_uri") or ""
            matched = bool(code) and bool(expected) and state == expected
            if matched:
                _YT_OAUTH["state"] = ""  # single use — a replay must fail
        if not matched:
            self._youtube_html(
                "YouTube not connected",
                "This sign-in link is stale or did not come from this "
                "Monteur. Start again from Settings → Connect YouTube.",
                status=400,
            )
            return
        from monteur import youtube
        from monteur.settings import (
            save_settings,
            youtube_client_id,
            youtube_client_secret,
        )

        try:
            tokens = youtube.exchange_code(
                youtube_client_id(), youtube_client_secret(), code, redirect_uri
            )
        except youtube.MonteurYouTubeError as exc:
            self._youtube_html("YouTube not connected", str(exc), status=502)
            return
        refresh_token = str(tokens.get("refresh_token") or "")
        if not refresh_token:
            self._youtube_html(
                "YouTube not connected",
                "Google returned no refresh token. Remove Monteur's access "
                "at myaccount.google.com/permissions, then connect again.",
                status=502,
            )
            return
        save_settings({"youtube_refresh_token": refresh_token})
        self._youtube_html(
            "YouTube connected",
            "YouTube connected — you can close this tab and go back "
            "to Monteur Studio.",
            close=True,
        )

    def _youtube_disconnect(self) -> None:
        from monteur.settings import save_settings

        save_settings({"youtube_refresh_token": "", "youtube_channel": ""})
        self._send_json(_youtube_status_view())

    def _youtube_upload(self) -> None:
        """POST /api/youtube/upload — validate, then a "youtube-upload" job.

        400s live here (missing path/title, file not found, bad privacy,
        not connected); the upload itself runs in
        :func:`_run_youtube_upload_job`.
        """
        from monteur.settings import (
            youtube_client_id,
            youtube_client_secret,
            youtube_refresh_token,
        )

        payload = self._read_json()
        path = str(payload.get("path") or "").strip()
        if not path:
            raise ApiError(400, "missing 'path' (the finished video file)")
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ApiError(
                400, "missing 'title' (the video needs a name on YouTube)"
            )
        if not os.path.isfile(path):
            raise ApiError(400, f"there is no video file at {path!r}")
        privacy = str(payload.get("privacy") or "private")
        if privacy not in ("private", "unlisted"):
            raise ApiError(400, "'privacy' must be 'private' or 'unlisted'")
        if not (youtube_client_id() and youtube_client_secret()
                and youtube_refresh_token()):
            raise ApiError(
                400,
                "YouTube is not connected — open Settings → YouTube and "
                "connect your channel first",
            )
        tags = payload.get("tags")
        if isinstance(tags, str):  # the UI's comma field, pre-split for the job
            payload["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
        elif tags is not None and not isinstance(tags, list):
            raise ApiError(400, "'tags' must be a list or a comma-separated string")
        payload["path"], payload["title"], payload["privacy"] = path, title, privacy
        job = _new_job("youtube-upload")
        threading.Thread(
            target=_run_youtube_upload_job,
            args=(job, payload),
            name=f"monteur-youtube-upload-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _youtube_prefill(self) -> None:
        """POST /api/youtube/prefill — deterministic metadata suggestions.

        Synchronous and offline (the AI-assisted copy stays the publish
        kit's job): title from the draft name or the plan's composed
        "story:" note, description = story + the plan's own chapter lines
        (starting 0:00, nothing invented), tags mined by
        :func:`monteur.publish.plan_tags`.

        A vertical short gets YouTube's Shorts routing: when the optional
        ``"canvas"`` is a ``vertical*`` preset AND the plan runs 60 s or
        less, " #Shorts" is appended to the title (inside the 100-char
        limit) and "#Shorts" becomes the description's first line. Any
        other canvas or length keeps the metadata untouched — a 16:9
        video must never be routed as a Short.
        """
        from monteur.montage import plan_from_dict
        from monteur.publish import plan_chapters, plan_tags

        payload = self._read_json()
        if not isinstance(payload.get("plan_json"), dict):
            raise ApiError(
                400, "missing 'plan_json' (the plan a build result carries)"
            )
        plan = plan_from_dict(payload["plan_json"])  # bad -> ValueError -> 400
        story = ""
        for note in plan.notes:
            if isinstance(note, str) and note.lower().startswith("story:"):
                story = note[len("story:"):].strip()
                break
        name = str(payload.get("name") or "").strip()
        chapters = plan_chapters(plan)
        chapter_lines = [
            f"{int(c.start) // 60}:{int(c.start) % 60:02d} {c.title}"
            for c in chapters
        ]
        parts = []
        if story:
            parts.append(story)
        if chapter_lines:
            parts.append("\n".join(chapter_lines))
        title = (name or story)[:100]  # YouTube's title limit
        canvas = str(payload.get("canvas") or "")
        if canvas.startswith("vertical") and plan.duration <= 60.0 + 1e-6:
            # Shorts routing: the tag in the title AND the description's
            # first line tells YouTube this vertical <= 60s cut is a Short.
            base = (name or story).strip()
            if "#shorts" not in base.lower():
                title = (
                    f"{base[: 100 - len(' #Shorts')]} #Shorts" if base else "#Shorts"
                )
            parts.insert(0, "#Shorts")
        self._send_json(
            {
                "title": title,
                "description": "\n\n".join(parts),
                "tags": plan_tags(plan),
            }
        )

    # -- in-app updates (monteur.update) --------------------------------------

    def _update_check(self) -> None:
        """GET /api/update/check — is there a newer version? Never errors hard.

        A git checkout compares against its upstream branch (an in-app update
        is just `git pull`); a frozen/wheel build checks GitHub Releases.
        """
        from monteur import update as update_mod
        from monteur.settings import update_channel

        if update_mod.git_root() is not None:
            self._send_json(update_mod.git_check().to_dict())
            return
        self._send_json(update_mod.check(channel=update_channel()).to_dict())

    def _update_install(self) -> None:
        """POST /api/update/install — download + stage the latest build.

        Runs in a job (the download can be large); the swap itself happens at
        the next launch via ``update.apply_pending`` (a process can't overwrite
        its own running executable). A source checkout has nothing to install
        and says so.
        """
        job = _new_job("update")
        threading.Thread(
            target=_run_update_job,
            args=(job,),
            name=f"monteur-update-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    # -- proxy cache (monteur.proxies) ----------------------------------------

    def _cache_get(self) -> None:
        """GET /api/cache — proxy cache size + count for the Settings display."""
        from monteur import proxies

        info = proxies.cache_size()
        self._send_json({"proxy_bytes": info["bytes"], "proxy_count": info["count"]})

    def _cache_clear(self) -> None:
        """POST /api/cache/clear — delete every cached proxy (they re-transcode)."""
        from monteur import proxies

        removed = proxies.clear_proxies()
        info = proxies.cache_size()
        self._send_json({"removed": removed, "proxy_bytes": info["bytes"], "proxy_count": info["count"]})

    def _resolve_status(self) -> None:
        # Isolated in a child process: Resolve's native module can hard-crash
        # (access violation) under an incompatible Python, and that would take
        # the whole server down. resolve_status_isolated never raises.
        from monteur.resolve import resolve_status_isolated

        self._send_json(resolve_status_isolated())

    def _resolve_diagnose(self) -> None:
        # The full self-check behind the settings panel's Resolve section:
        # interpreter + source, info probe, live status and the plain-language
        # verdict the UI shows verbatim. Child-process isolated; never raises.
        from monteur.resolve import diagnose

        self._send_json(diagnose())

    def _resolve_detect(self) -> None:
        job = _new_job("resolve-detect")
        threading.Thread(
            target=_run_resolve_detect_job,
            args=(job,),
            name=f"monteur-resolve-detect-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _resolve_render(self) -> None:
        """POST /api/resolve/render — render the built timeline to a file.

        Validation happens here (a missing ``target_dir`` or an unknown
        ``preset`` is a 400 with a user-ready message); the actual render
        monitoring runs as a ``"resolve-render"`` job — see
        :func:`_run_resolve_render_job`, including the honest cancel
        semantics (cancel stops the monitoring; Resolve keeps rendering).
        """
        payload = self._read_json()
        if not str(payload.get("target_dir") or "").strip():
            raise ApiError(
                400, "missing 'target_dir' (the folder for the finished video)"
            )
        preset = payload.get("preset")
        if preset not in (None, "", "2160p", "1080p"):
            raise ApiError(400, "'preset' must be '2160p' or '1080p'")
        job = _new_job("resolve-render")
        threading.Thread(
            target=_run_resolve_render_job,
            args=(job, payload),
            name=f"monteur-resolve-render-{job['id']}",
            daemon=True,
        ).start()
        self._send_json({"job": job["id"]})

    def _resolve_analyze(self) -> None:
        from monteur.resolve import MonteurResolveError, read_timeline_isolated

        payload = self._read_json()
        try:
            timeline = read_timeline_isolated(payload.get("timeline"))
        except MonteurResolveError as exc:
            raise ApiError(502, str(exc))
        stats = analyze_timeline(timeline)
        response: dict = {"stats": asdict(stats)}
        if payload.get("save"):
            entry = self.project.add_version(
                stats,
                label=payload.get("label", ""),
                source_file="DaVinci Resolve",
                saved_at=time.strftime("%Y-%m-%d %H:%M"),
            )
            response["version"] = {k: v for k, v in entry.items() if k != "stats"}
        self._send_json(response)


def serve(
    port: int = 8765,
    project_root: str = ".",
    open_browser: bool = True,
    ready: threading.Event | None = None,
    on_bind=None,
) -> None:
    """Run Monteur Studio until interrupted.

    ``on_bind`` (optional) is called with the bound server object right before
    the serve loop starts — a small seam so callers/tests can shut the server
    down cleanly from another thread.
    """
    # faulthandler turns a C-level access violation (the prime suspect for
    # "process just vanishes with no Python exception" while serving) into a
    # printed native traceback instead of a silent death. Idempotent + guarded
    # so enabling it can never itself take the server down.
    _crash_log = None  # kept alive for the server's lifetime (faulthandler holds its fd)
    try:
        import faulthandler

        if not faulthandler.is_enabled():
            # Log native crashes to a DURABLE FILE, always — not just the console
            # (which vanishes when the window/terminal closes), so a crash can
            # actually be found and sent for diagnosis. Falls back to stderr only
            # if the file can't be opened.
            try:
                crash_path = Path(project_root) / "monteur-crash.log"
                crash_path.parent.mkdir(parents=True, exist_ok=True)
                _crash_log = open(crash_path, "a", buffering=1)  # noqa: SIM115 - lives with the server
                faulthandler.enable(file=_crash_log, all_threads=True)
                print(f"(Native-crash log: {crash_path})", flush=True)
            except OSError:
                try:
                    faulthandler.enable(all_threads=True)  # stderr
                except (ValueError, AttributeError, OSError):
                    target = getattr(sys, "__stderr__", None)
                    if target is not None:
                        faulthandler.enable(file=target, all_threads=True)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never break startup
        print(
            f"(Note: could not enable native crash reporting: {exc})", flush=True
        )

    # Make worker-thread and main-thread crashes visible; restore on the way out
    # so importing/embedding this module does not permanently mutate the hooks.
    prev_hooks = _install_diagnostic_hooks()

    handler = type("BoundHandler", (MonteurHandler,), {"project": Project(project_root)})
    server = None
    for candidate in range(port, port + 10):
        try:
            server = MonteurServer(("127.0.0.1", candidate), handler)
            break
        except OSError as exc:
            bind_error = exc
    if server is None:
        _restore_diagnostic_hooks(*prev_hooks)
        raise OSError(
            f"ports {port}-{port + 9} are all in use ({bind_error}) — "
            f"is another Monteur Studio still running?"
        )
    if server.server_address[1] != port:
        print(f"Port {port} is busy — using {server.server_address[1]} instead.", flush=True)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Monteur Studio running at {url}", flush=True)
    # keep the proxy cache bounded even between generations — deleting projects
    # leaves their proxies (a shared, project-independent cache), so enforce the
    # size cap on startup, not only after a proxy build. Best-effort, off-thread.
    try:
        from monteur import proxies as _proxies

        threading.Thread(target=_proxies.prune_proxies, name="monteur-proxy-prune", daemon=True).start()
    except Exception:  # noqa: BLE001 - cache upkeep must never block startup
        pass
    print("Leave this window open. Press Ctrl+C here to stop.", flush=True)
    if ready is not None:
        ready.set()
    if on_bind is not None:
        on_bind(server)
    if open_browser:
        _open_browser_safely(url)
    try:
        server.serve_forever()
        print("\nMonteur Studio exited unexpectedly (the serve loop returned "
              "on its own).", flush=True)
    except KeyboardInterrupt:
        print("\nMonteur Studio stopped (Ctrl+C).", flush=True)
    except BaseException as exc:  # noqa: BLE001 - surface EVERY exit reason
        import traceback

        print(f"\nMonteur Studio stopped via {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        raise
    finally:
        server.server_close()
        _restore_diagnostic_hooks(*prev_hooks)


class _WindowControls:
    """The JS bridge for a frameless window's custom title bar.

    Exposed to the page as ``window.pywebview.api`` — the caption buttons in
    app.html call ``minimize()`` / ``toggle_maximize()`` / ``close()``. Every
    call resolves the live pywebview window and swallows any error, so a
    platform quirk in one control can never wedge the whole window. Takes the
    ``webview`` module so it is unit-testable with a stub.
    """

    def __init__(self, webview_module) -> None:
        self._wv = webview_module
        self._maximized = False

    def _window(self):
        windows = getattr(self._wv, "windows", None) or []
        return windows[0] if windows else None

    def minimize(self) -> None:
        win = self._window()
        if win is not None:
            try:
                win.minimize()
            except Exception:  # noqa: BLE001 — a caption button must never crash
                pass

    def toggle_maximize(self) -> None:
        win = self._window()
        if win is None:
            return
        try:
            if self._maximized:
                restore = getattr(win, "restore", None)
                if restore:
                    restore()
            else:
                maximize = getattr(win, "maximize", None)
                if maximize:
                    maximize()
            self._maximized = not self._maximized
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        win = self._window()
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass

    def restart(self) -> None:
        """Relaunch the app and close this window — finishes a staged update.

        The new process runs the launcher again, which picks the newest payload
        on disk (the one just installed). Best-effort: if relaunch fails we do
        NOT close the window, so the user is never left with nothing.
        """
        try:
            import subprocess
            import sys

            # A frozen build IS the launcher (sys.executable = monteur.exe); a
            # source run is `python -m monteur <args>`, so rebuild that form —
            # otherwise the relaunch would run `python ui …` and die instantly.
            from monteur.procio import NO_WINDOW

            frozen = bool(getattr(sys, "frozen", False))
            base = [sys.executable] if frozen else [sys.executable, "-m", "monteur"]
            subprocess.Popen(base + sys.argv[1:], **NO_WINDOW)  # noqa: S603 - our own app
        except Exception:  # noqa: BLE001 — never strand the user
            return
        self.close()

    def open_url(self, url: str) -> None:
        """Open a link in the system browser (for the release page, etc.)."""
        try:
            import webbrowser

            if isinstance(url, str) and url.startswith(("http://", "https://")):
                webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    def resize(self, width, height) -> None:
        """Resize the frameless window — the page's edge grips call this.

        A frameless window has no OS resize border, so app.html draws its own
        edge/corner grips (Electron-style) and drives the size through here.
        Clamped to the same minimum the window was created with; any bad value
        or backend quirk is swallowed so a resize drag can never wedge the app.
        """
        win = self._window()
        if win is None:
            return
        try:
            w = max(900, int(width))
            h = max(600, int(height))
        except (TypeError, ValueError):
            return
        try:
            win.resize(w, h)
        except Exception:  # noqa: BLE001 — a resize drag must never crash the app
            pass


#: Minimum time (seconds) the splash stays on screen even when the server binds
#: instantly — otherwise a fast machine swaps to the app before the splash is
#: ever really seen. Just long enough to register the brand, not a stall.
_SPLASH_MIN_SECONDS = 2.5


def _brand_asset(name: str) -> "Path | None":
    """The packaged brand asset ``packaging/<name>`` (repo/editable installs), or
    None when it isn't reachable (e.g. a non-editable wheel without the file)."""
    from pathlib import Path as _Path

    cand = _Path(__file__).resolve().parents[2] / "packaging" / name
    return cand if cand.is_file() else None


def _splash_bgmark() -> str:
    """The brand mark as a big, faint background ``<img>`` (data URI, so the
    splash stays self-contained), or "" when the PNG asset isn't found."""
    png = _brand_asset("monteur.png")
    if png is None:
        return ""
    try:
        import base64

        data = base64.b64encode(png.read_bytes()).decode("ascii")
    except OSError:
        return ""
    return f'<img class="bgmark" src="data:image/png;base64,{data}" alt="">'


#: Instant splash shown in the pywebview window WHILE the server binds and the
#: app page loads — the blank gap before it was what read as "nothing happened
#: when I launched". Self-contained (no server, no external assets): the brand
#: mark large and faded behind the wordmark + a film-strip loader, all pure CSS.
#: Swapped for the real app (``window.load_url``) once the server is ready.
_SPLASH_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    background: #1b1b1e; color: #d6d7dc; overflow: hidden;
    font-family: "Segoe UI", -apple-system, system-ui, sans-serif;
    display: flex; align-items: center; justify-content: center;
    -webkit-user-select: none; user-select: none;
  }
  .bgmark {
    position: fixed; top: 50%; left: 50%;
    width: 440px; height: 440px; object-fit: contain;
    transform: translate(-50%, -56%); opacity: 0.10; pointer-events: none;
    animation: breathe 4.5s ease-in-out infinite;
  }
  @keyframes breathe { 0%, 100% { opacity: 0.07; } 50% { opacity: 0.14; } }
  .splash { position: relative; z-index: 1; text-align: center; animation: rise 0.6s ease both; }
  @keyframes rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
  .mark {
    font-size: 40px; font-weight: 700; letter-spacing: 0.18em;
    padding-left: 0.18em; color: #ececf0;
  }
  .sub {
    margin-top: 6px; font-size: 13px; font-weight: 600; letter-spacing: 0.42em;
    padding-left: 0.42em; color: #e8823c; text-transform: uppercase;
  }
  .strip { margin: 28px auto 0; display: flex; gap: 6px; justify-content: center; }
  .frame {
    width: 22px; height: 15px; border-radius: 2px;
    background: #33333b; box-shadow: inset 0 0 0 1px #3f3f48;
    animation: flick 1.1s ease-in-out infinite;
  }
  .frame:nth-child(1) { animation-delay: 0s; }
  .frame:nth-child(2) { animation-delay: 0.11s; }
  .frame:nth-child(3) { animation-delay: 0.22s; }
  .frame:nth-child(4) { animation-delay: 0.33s; }
  .frame:nth-child(5) { animation-delay: 0.44s; }
  .frame:nth-child(6) { animation-delay: 0.55s; }
  .frame:nth-child(7) { animation-delay: 0.66s; }
  @keyframes flick {
    0%, 100% { background: #33333b; box-shadow: inset 0 0 0 1px #3f3f48; transform: scaleY(1); }
    40% { background: #e8823c; box-shadow: inset 0 0 0 1px #f0a062, 0 0 10px rgba(232,130,60,0.5); transform: scaleY(1.25); }
  }
  .status { margin-top: 22px; font-size: 12px; color: #6c6c76; letter-spacing: 0.05em; }
</style></head>
<body>
  __BGMARK__
  <div class="splash">
    <div class="mark">MONTEUR</div>
    <div class="sub">Studio</div>
    <div class="strip">
      <span class="frame"></span><span class="frame"></span><span class="frame"></span>
      <span class="frame"></span><span class="frame"></span><span class="frame"></span>
      <span class="frame"></span>
    </div>
    <div class="status">__STATUS__</div>
  </div>
</body></html>"""


def _splash_html(status: str = "Starting up&hellip;") -> str:
    """The splash page with the brand mark embedded and a status line."""
    return _SPLASH_TEMPLATE.replace("__BGMARK__", _splash_bgmark()).replace(
        "__STATUS__", status
    )


def serve_app(
    port: int = 8765,
    project_root: str = ".",
    title: str = "Monteur Studio",
    size: tuple[int, int] = (1280, 820),
) -> None:
    """Run Monteur Studio in a native desktop window instead of a browser.

    Wraps the same local Studio in a pywebview window — its OWN window, no
    address bar, no browser chrome (WebView2 on Windows, WebKit on macOS,
    GTK/Qt on Linux). The HTTP server runs on a daemon thread; the window's
    UI loop owns the main thread (pywebview requires it), so when the window
    closes the process — and with it the daemon server — exits.

    Without pywebview installed this falls back to the browser
    (``serve(open_browser=True)``) with a one-line pointer to the ``[app]``
    extra, so the launcher always does something sensible.
    """
    # The packaged app's working directory is its install folder (Program
    # Files), which is read-only for a per-user run. Everything persistent
    # already lives under ~/.monteur; point project_root (the analysis version
    # store + crash log) there too, so a windowed launch NEVER writes next to
    # the executable. An explicit --project still wins.
    if project_root in (".", "", None):
        project_root = str(_app_data_root())

    # a staged update installs here — startup is the only safe moment to swap
    # the running executable. On a source checkout this is a no-op advisory.
    try:
        from monteur import update as _update

        applied = _update.apply_pending()
        if applied and applied.applied and applied.relaunch:
            import subprocess

            from monteur.procio import NO_WINDOW

            print(applied.message, flush=True)
            subprocess.Popen(applied.relaunch, **NO_WINDOW)  # noqa: S603 - our own exe
            return
    except Exception:  # noqa: BLE001 - an update must never block startup
        pass

    try:
        import webview
    except ImportError:
        print(
            "(pywebview isn't installed — opening Monteur in your browser "
            "instead. For a native window: pip install 'monteur[app]'.)",
            flush=True,
        )
        serve(port=port, project_root=project_root, open_browser=True)
        return

    # Windows: give the process its OWN taskbar identity so the button shows
    # Monteur's icon (below) grouped as Monteur, not lumped under python.exe.
    # (The guaranteed clean taskbar icon still ships with the packaged .exe,
    # which embeds the same .ico — this is the best a scripted launch can do.)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Monteur.Studio"
            )
        except Exception:  # noqa: BLE001 - cosmetic only, never block startup
            pass

    ready = threading.Event()
    holder: dict = {}

    def _on_bind(server) -> None:
        holder["url"] = f"http://127.0.0.1:{server.server_address[1]}/"

    thread = threading.Thread(
        target=serve,
        kwargs={
            "port": port,
            "project_root": project_root,
            "open_browser": False,
            "ready": ready,
            "on_bind": _on_bind,
        },
        name="monteur-server",
        daemon=True,
    )
    thread.start()

    # Show the window IMMEDIATELY with the splash (no wait), so a launch looks
    # instant instead of "nothing happening" while the server binds and the
    # page loads. frameless: app.html draws its own Fluent title bar (drag
    # region + caption buttons) and drives min/maximize/close through this
    # js_api bridge — the splash rides in the same frameless window.
    window = webview.create_window(
        title,
        html=_splash_html(),
        width=size[0],
        height=size[1],
        min_size=(900, 600),
        frameless=True,
        resizable=True,   # frameless drops OS resize borders — app.html draws its
                          # own edge grips that drive _WindowControls.resize()
        easy_drag=False,  # the title bar's -webkit-app-region owns dragging
        js_api=_WindowControls(webview),
    )

    def _swap_to_app() -> None:
        # runs on a pywebview worker thread once the GUI loop is up: wait for
        # the server, hold the splash a beat so it's actually seen, then
        # navigate the same window from the splash to the app.
        import time

        started = time.monotonic()
        if ready.wait(timeout=20) and holder.get("url"):
            shortfall = _SPLASH_MIN_SECONDS - (time.monotonic() - started)
            if shortfall > 0:
                time.sleep(shortfall)
            print(
                f"Monteur Studio window open ({holder['url']}). Close it to stop.",
                flush=True,
            )
            window.load_url(holder["url"])
        else:
            print("Monteur Studio's server did not start in time.", flush=True)
            window.load_html(
                _splash_html(
                    "Monteur could not start its engine. "
                    "Close this window (Alt+F4) and try again."
                )
            )

    # Pass the brand icon to pywebview where the backend accepts it (window /
    # alt-tab / taskbar on the GTK/Qt/Windows backends); older builds without
    # the kwarg just fall back to the iconless start.
    ico = _brand_asset("monteur.ico")
    try:
        if ico is not None:
            webview.start(_swap_to_app, icon=str(ico))
        else:
            webview.start(_swap_to_app)
    except TypeError:  # pywebview too old for the icon kwarg
        webview.start(_swap_to_app)


def _open_browser_safely(url: str) -> None:
    """Open the browser without ever taking the server down with it."""
    def _open() -> None:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - a browser failure must not matter
            print(f"(Could not open a browser automatically — visit {url} yourself.)",
                  flush=True)

    threading.Thread(target=_open, daemon=True).start()
