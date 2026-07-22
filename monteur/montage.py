"""Montage builder: best moments + music beats -> a first cut.

Takes the sifted footage (:mod:`monteur.sift`) and an analyzed song
(:mod:`monteur.music`) and lays out a rough cut on the beat grid: calm
sections cut slower (every few beats), high-energy sections cut faster.
The result is a Timeline — video from the footage on V1, the song on A1 —
ready for EDL/FCPXML export into Resolve.

Slotting algorithm
------------------
1. The montage length is ``min(song duration, max_duration)``.
2. A cut grid is walked beat by beat: in a "high" section the next cut is
   1 beat away, in "mid" 2 beats, in "low" 4 beats — as the section's BASE,
   not a metronome: a longer hold (~2x the base, capped so it never eats
   the section) opens each section and every 4th cut breathes at 2x (see
   Rhythm below). If an interval would be shorter than
   :data:`MIN_CUT_INTERVAL` (0.4 s) the beat step is doubled until it
   isn't (no strobing). With no beats at all, a fixed 2 s grid is used and
   noted. Cuts at/after the montage length are dropped; the last slot
   always ends exactly at the montage length.
3. Every moment from every report goes into one pool (keeping its clip
   path). CHRONOLOGICAL sorts the pool by (clip path, moment start) and
   fills slots left to right. BEST_FIRST sorts the pool by score descending
   and visits the slots in section-energy order (highest first, ties by
   record time), so the best material lands on the loudest music; entries
   are re-sorted by record time afterwards.
4. Reuse rules: on the first pass every pool moment serves exactly one
   slot, consuming a slot-length piece from its start. Once the pool runs
   short, unconsumed tails are sliced first — the pool is scanned cyclically
   for the next moment with unused material and the next non-overlapping
   slot-length piece is taken (a long moment thus splits into several
   pieces; distinct footage, never a repeat). Only when *no* moment has
   unused material left do the two modes part ways: with
   ``allow_repeats=True`` a moment is rewound and its footage repeated,
   and that is noted; with repeats OFF (the default) nothing is ever
   rewound — the montage ends at the last slot that fresh material could
   fill (see Repetition guard below).
5. Each entry takes ``slot length`` seconds starting at
   ``moment.start + consumed``. If the remaining piece is shorter than the
   slot it is padded by extending toward the clip's end; if even that is
   not enough, the short piece is kept (record stays on the grid) and a
   gap is noted.

Styles
------
``plan_montage(..., style=...)`` picks a :data:`STYLES` entry. "auto"
(the default) keeps the section-energy grid described above. A named style
instead maps a story arc — (share_of_duration, phase) pairs over
opening/build/climax/outro — onto the montage duration and cuts each phase
at its own beat density. Grid points still snap to musical positions:
phase boundaries snap to the nearest phrase start (falling back to
downbeats, then beats, when phrases are unknown), slow phases
(>= 4 beats per cut) place their cuts on downbeats, fast phases walk the
beat grid; with neither beats nor downbeats the fixed 2 s grid is used,
exactly as in "auto".

The "short" style is the anti-canon for vertical platforms: its arc is
hook (8%) -> punch (72%) -> loop (20%), and it never establishes — the
first cut stays at its base, absolutely capped at :data:`_MAX_HOOK_SECONDS`
(~2 s) via :attr:`MontageStyle.no_opening_hold`. Slot 0 is reserved for
the PATTERN INTERRUPT — the moment with the highest hook score (motion +
hero + score; the "opener" role preference does not apply) — and the LAST
slot prefers a moment from the hook's own scene group (else the closest
motion energy), so the ending cuts seamlessly back into the opening on
replay; both moves are noted ("hook: ...", "loop: ..."). :data:`PLATFORMS`
maps publish targets (youtube / short / reel / tiktok) onto canvas +
style + a length CAP, resolved at the CALLER layer by
:func:`resolve_platform` — ``plan_montage`` itself never takes a platform,
so the engine stays orthogonal.

Rhythm
------
Within a phase the beat step is a BASE, not a metronome — real editing
varies shot length deliberately, and a cut of nothing but equal-length
clips reads mechanical no matter how well it hits the beat. The grid
builders therefore apply a deterministic rhythm canon (no RNG; every cut
still lands on the beat/downbeat grid, quantized to whole units):

* **Establishing hold** — the montage's FIRST shot holds ~2x the opening
  base (:func:`_opening_hold`, capped at half the phase so it never eats
  it): the viewer must arrive.
* **Accelerando** — the build's cut lengths step down monotonically from
  the previous phase's base toward the following phase's (the trailer
  ramp); a split build ramps across the whole run.
* **Drop hold + stutter + recovery** — the slot ON the drop holds 2-4
  beats (:func:`_drop_hold`, aim 3x the climax base) — impact needs
  screen time — and ``_STUTTER_CUTS`` one-beat cuts directly before it
  sharpen the hit (only when the build ends fast enough to afford them).
  Right after the hold, ONE recovery cut at ~2x the climax base
  (blueprint 1.6) lets the peak land before the pattern re-accelerates.
  The "auto" style clears grid cuts inside ~2 beats after each
  drop-forced cut for the same reason.
* **Hot/cool phrase groups** — a LONG climax (two-plus 8-unit groups)
  alternates the style's own pattern with a cooled copy (multipliers
  doubled) in 8-unit groups (blueprint 1.6): peaks and valleys instead
  of uniform fire; a phrase boundary still re-anchors the cycle.
* **Pattern texture** — the other phases cycle a per-style multiplier
  pattern on their base (:attr:`MontageStyle.rhythm`; trailer aggressive,
  travel/wedding gentle, music_video punchy). A phrase boundary falling
  inside a cycle re-anchors it, so the pattern restarts with the music's
  own phrasing. All multipliers are >= 1, so ``pace`` keeps meaning
  "seconds per shot in the FASTEST phase".
* **Decelerando** — each outro cut is at least as long as the previous
  one and the FINAL shot is the longest (up to 2x the outro base, the
  remainder to the montage length — the total duration never changes).

The plan notes summarize what was applied in one ``rhythm: ...`` line.
No-music plans get the same canon on pseudo-beat units; "auto" gets the
gentler section-hold + breath treatment described in the slotting
algorithm above.

Drops
-----
With a named style that has a climax phase, the climax start is aligned to
the BEST in-range drop (blueprint 1.5: the heaviest by
:func:`monteur.music.drop_weight` — envelope jump into the hottest payoff;
ties keep the earliest, so single-drop songs behave exactly as always):
boundaries before it are scaled by ``drop / original``, boundaries after
it are scaled toward the end by ``(length - drop) / (length - original)``,
and the arc-squeeze floor then guarantees every squeezed neighbour phase
at least :data:`_ARC_MIN_PHASE_SHARE` (5%) of the montage — a deliberate
1.5 default change; the pin itself never moves. Only drops within 5%..95%
of the montage qualify — none in range, and a note explains the skip. In
"auto", every in-range drop forces a cut exactly on the drop and the slot
starting there is reserved for the unused moment with the highest
(highlight, score), so the impact lands on the strongest material. The
"short" style pins ONE cut the same way — on the best in-range drop,
which is exactly the drop :func:`monteur.music.best_energy_window` placed
inside the window with its 15% lead-in (co-designed, blueprint 1.5).
In a climax-bearing arc style the strongest SECONDARY drops (all in-range
drops except the climax pin) now force their own hard cut too (blueprint
2.1, :func:`_secondary_drops`): gated by a fraction of the climax drop's
weight (:data:`_SECONDARY_DROP_WEIGHT_FRACTION`) and kept
:data:`_SECONDARY_DROP_MIN_BEATS` beats clear of the climax and of each
other, capped at :data:`_SECONDARY_DROP_MAX`. The climax pin and its 1.5
arc-squeeze floors are untouched (the pin casts through the highlight
phase, not the drop-slot reservation). A secondary drop landing inside a
running phase-hold (the opening or a long climax hold) does not shred it:
the hold runs UP TO the drop, cuts hard on it, and the phase pattern
re-seats after — grid cuts inside ~2 beats after the drop are cleared and
:func:`_absorb_slivers` drops any short remainder before it. In all cases
the drop slot is a HOLD (see Rhythm above): a pinned climax opens on a
2-4 beat held shot, and "auto"/"short"/the secondary drops clear grid
cuts inside ~2 beats after the forced cut.

Loop seam (blueprint 1.5, "short" style): a windowed short chooses its
song-window END on a phrase boundary (:func:`_loop_seam_start` — the wrap
from the window's last note back to its first then connects musically;
the drop pin stays in range), and the LAST slot's casting earns an
exit→hook-entry motion-continuity bonus so the final shot hands its
motion back into the hook on replay. The notes narrate both halves.

Highlights and motion matching
------------------------------
In the phase named by ``style.prefer_highlights_in`` (usually "climax")
the candidate window is re-sorted by (highlight, score) instead of the
plain pool order, so audible peaks (cheers, laughter, action) land on the
musical peak. The ordering mode (CHRONOLOGICAL / BEST_FIRST) still decides
WHICH moments are in play; these refinements — and motion matching — only
break near-ties among the next few candidates: for each slot the next
K = 4 unconsumed pool items are scored with
``0.7 * order_preference + 0.3 * motion_continuity`` where order
preference is ``1 - position / K`` (earlier in the pool = higher) and
motion continuity is the cosine similarity between the previous slot's
exit motion and the candidate's entry motion (neutral 0 unless both
vectors exceed 0.5 px). With neutral motion the earliest candidate always
wins, so behavior without motion data is unchanged.

Energy-motion matching adds ``_ENERGY_MATCH_WEIGHT x (1 - |slot_energy -
candidate_motion|)`` to the same blend: slot energy comes from the song's
sections ("auto") or the arc phase's nominal energy (:data:`_PHASE_ENERGY`),
candidate motion is the moment's mean entry/exit motion magnitude
normalised to the pool's fastest moment. Loud passages meet moving
footage, calm passages calm footage; the full weight only tips the scale
at the energy extremes (a climax slot picks the moving shot over a static
one a single order position earlier), everywhere else it just leans.

Semantic casting
----------------
:mod:`monteur.vision` can annotate moments with what is IN the picture: a
one-line ``label``, a story ``role`` (opener/build/climax/closer), a
``hero`` strength (0..1, the poster shot) and a scene-similarity ``group``.
When at least one pool moment carries a role, hero or group, the slot
filling reads them — always as mild bonuses on the candidate blend above,
never as hard filters; moments without annotations behave exactly as
before. A slot in an arc phase prefers the matching role (opening ->
opener, build -> build, climax -> climax, outro -> closer), and the
montage's FIRST slot prefers an opener and its LAST slot a closer in every
style; a fitting role adds :data:`_ROLE_WEIGHT` (0.2) — enough to flip one
order position, never two. Drop-slot reservation adds :data:`_HERO_WEIGHT`
(0.5) x ``hero`` to the (highlight, score) key and climax-phase candidates
get the same hero bonus, so the real hero shot wins the drop even against
slightly better motion continuity. A candidate whose group matches an
already-filled neighbouring slot loses :data:`_GROUP_PENALTY` (0.25), so
two takes of the same scene never sit back to back while an alternative
exists. Labels ride along: ``MontageEntry.label`` feeds the video clip's
``"label"`` metadata and the title-slot markers ("0.4s of black — next:
<label>"), and a plan note reports what the casting actually did (e.g.
"semantic casting: 9 of 14 slots matched to roles, hero shot on the drop").

Time-of-day coherence
---------------------
:mod:`monteur.daylight` classifies each moment's time of day offline
("day" / "golden" / "night" on ``Moment.daylight``). The casting reads it
as two SOFT terms on the candidate blend — coherence is the law,
direction is direction:

* **Coherence (the law).** A candidate whose class differs from the
  previous cast slot's loses :data:`_DAYLIGHT_SWITCH_PENALTY` (0.15):
  footage sits in time-of-day blocks with rare, deliberate switches.
  Zero when either side is unknown, so unclassified material behaves
  exactly as before.
* **Direction (a decision).** The deterministic default block ORDER is
  the material's natural arc — day -> golden -> night filtered to the
  classes that exist, mapped over the montage proportionally to each
  class's material share (:func:`_daylight_targets`, only when >= 2
  classes exist). A candidate matching its slot's target class gains
  :data:`_DAYLIGHT_BLOCK_WEIGHT` (0.1). The composer
  (:mod:`monteur.compose`) may direct another order deliberately (a
  night teaser cold open) and explains it; an arrangement and pins win
  outright — arranged slots are never scored or flagged.

Never a hard sort: both terms are tie-breakers below one order step.
The notes carry the arc ("story: daylight arc day -> golden -> night
(soft)") and honest per-slot warnings when a cast slot sits against the
flow ("slot 14: night shot inside the day block").

Picture coherence (blueprint wave 3)
-----------------------------------
Waves 1-2 make the cut hit the SOUND; wave 3 makes the PICTURE cohere,
grounded in Walter Murch's Rule of Six: eye-trace and shot grammar are
LOW ranks that may be sacrificed for the higher ones (emotion / story /
rhythm). So all three terms below are TIE-BREAKERS on the same candidate
blend — each sized below one order step, each ZERO unless the offline
spatial pass (:mod:`monteur.spatial`) filled the moment, and none ever
applied to a reserved slot (drop / hook / loop) — there sync and the pin
win outright. A pool without the spatial signal casts byte-identically.

* **Eye-trace continuity (3.1).** The spatial pass estimates each shot's
  attention point (salience centroid) at its start and end
  (:attr:`~monteur.sift.Moment.entry_focus` / ``exit_focus``). A candidate
  whose entry point sits near the previous shot's exit point earns up to
  :data:`_EYE_TRACE_WEIGHT`, one that leaps across the frame loses it —
  the eye is carried across the cut. Suspended at a phase boundary, where
  a deliberate contrast is the accent. (This is the ON-SCREEN-POSITION
  half of eye-trace; the motion-DIRECTION half is the pre-existing
  ``_MOTION_WEIGHT`` continuity term above.)
* **Shot-size grammar (3.2).** The spatial pass classifies each shot
  wide / medium / close (:attr:`~monteur.sift.Moment.shot_size`).
  Establish -> develop -> pay off (wide -> medium -> close) earns
  :data:`_SHOT_GRAMMAR_WEIGHT`; two equally-sized neighbours pay
  :data:`_SHOT_GRAMMAR_EQUAL_PENALTY` (keep changing scale) — except a
  deliberate close->close intensification in the climax. Another contrast
  axis alongside hot/cool and daylight, composed not collided.
* **Visual rhyme / callback (3.3).** ONE deliberate echo: the closing
  slot is tipped toward the moment most visually kindred to the opening
  (:func:`_visual_kinship` over shot size + attention point, leaning on
  daylight and motion) by :data:`_RHYME_WEIGHT`, framing the video. Sparing
  by construction — one rhyme, one slot — and the echo is a DIFFERENT,
  still-unused moment, so zero-repeat holds. The note reads "rhyme: the
  closing shot echoes the opening (visual callback)".

Same-clip continuity
--------------------
The slot grid must not chop one continuing take into jump cuts. Two
mechanisms guard that (see :func:`_merge_continuity` and the jump-cut
guard in :func:`_fill`):

* **Continuity merge.** After casting, adjacent slots cast from the SAME
  clip whose source windows sit within :data:`_CONTINUITY_MAX_GAP` (3 s)
  of each other become ONE continuous shot: the merged entry plays from
  the first window's start straight across the bridge frames (the clip's
  own material between the windows), so nothing jumps. Unlike the calm
  merge below, no calmness or music-energy gate applies and the CLIMAX
  may merge internally (the same ride continuing over the drop is held,
  not re-cut) — but a merge still never crosses an act/section change
  (structure and smash-to-black dips survive), never absorbs a drop slot
  or the final entry, respects :data:`_MAX_CUT_SECONDS` and the
  zero-repeat promise, and leaves arranged entries alone.
* **Jump-cut guard.** During casting, a candidate that would sit next to
  a same-clip neighbour with a source gap under :data:`_JUMP_CUT_MIN_GAP`
  (8 s) pays :data:`_JUMP_CUT_PENALTY` — more than the group penalty, so
  a different clip up to two order positions later wins — unless the
  continuity merge would join the pair anyway. When the pool is too
  small to cast around it, the surviving visible jumps are counted into
  one honest note ("footage variety is low: ...").

Content-adaptive pacing
-----------------------
After casting, a slot-merge pass (:func:`_merge_calm_slots`) lets slow
content breathe: when two-plus ADJACENT slots on calm music (slot energy
<= :data:`_MERGE_MAX_SLOT_ENERGY`) are cast with calm material (motion
<= :data:`_MERGE_CALM_MOTION` of the pool's fastest and highlight <=
:data:`_MERGE_CALM_HIGHLIGHT`), the later entries are dropped and the
first one's record and source windows extend over them — but only when
its sifted moment really has the material and no other entry plays those
frames (the zero-repeat promise survives). Cuts stay on the grid: a
merge simply ends on a LATER existing cut time. The climax phase never
merges, a merge never crosses an act/section change (so it can never
swallow a smash-to-black dip), arranged and drop slots and the final
shot are immune, and the merged shot respects :data:`_MAX_CUT_SECONDS`.
With no motion data in the pool calmness is unknowable and the pass
changes nothing. A note reports the result ("pacing: 6 calm slots merged
into 3 longer shots ...").

Auto pace
---------
``pace=None`` (the default, recommended) does not mean "fixed style
bases": for arc styles the per-phase base is derived from the material
(:func:`_auto_pace_bias`). Two signals each slow every phase base one
notch (base x 2, at most two notches): a content mix whose calm share
(the calm-merge thresholds, weighted by moment seconds) reaches
:data:`_AUTO_PACE_CALM_SHARE` (60%), and a song whose duration-weighted
mean section energy sits at/below :data:`_AUTO_PACE_LOW_ENERGY` (0.35).
The arc-less "auto" style is not biased (its section grid already reads
the song's own density, and the merge passes adapt it to content);
"short" is not biased either (the vertical anti-canon never slows
down). An explicit ``pace`` is the override and skips the bias
entirely. On top of the base, real shot length still follows the music
(the beat grid), the clip (continuity + calm merges) and the local
tempo (the rhythm canon) — so the realized cut varies a lot by design.

Finishing
---------
A montage shorter than the song ends on a musical boundary:
``end_on_phrase=True`` (the default) snaps the requested length to the
nearest phrase start within ±12% (ties prefer the shorter cut; downbeats,
then beats, serve as fallbacks; the change is never allowed to exceed
12%, and a full-song montage is left alone). Styles with an outro phase
plan a 0.5 s fade-in and a fade-out of min(2 s, last outro slot) on
:class:`MontagePlan` (``fade_in`` / ``fade_out``); "auto" plans 0.5 s /
1 s. ``transitions="auto"`` (the default) decides every boundary PER
CUT from what meets there (:func:`_plan_finishing`): a same-clip
continuation always cuts hard, climax/"high" passages cut hard, a
daylight-block change dissolves (the soft time-lapse feel), and gentle
passages — >= 4 beats per cut, i.e. opening/outro, or "low" sections in
"auto" style — dissolve at scene-group changes while two takes of the
same group cut hard; gentle boundaries with no group knowledge keep the
classic dissolve INTO the entry: ``MontageEntry.transition`` =
min(0.5 s, half the slot length), always 0 for the montage's first
entry (its fade is ``fade_in``). The explicit modes (cuts / dissolves /
smash) stay blunt overrides.
:func:`montage_to_timeline` publishes dissolves as clip metadata
(``"transition"`` / ``"transition_frames"``) and the fades as timeline
metadata (``"fade_in_frames"`` / ``"fade_out_frames"``) so the EDL/FCPXML
writers can carry the dissolves into Resolve. Audio fades cannot ride
along in either export format; a plan note reminds the editor to apply
the music fade in Resolve.

Repetition guard
----------------
``allow_repeats=False`` (the default) is a PROMISE: zero repeated
moments — the checkbox says clips may not repeat, so they never do.
Three mechanisms enforce it:

* **Length cap.** ``plan_montage`` merges each clip's overlapping
  moments, sums the deduplicated material, and caps the montage length
  at exactly that unique material when the request exceeds it — the
  montage gets SHORTER instead of recycling. The end_on_phrase snap then
  refines the capped length and the strongest-window logic works from
  it. The note names the deal ("length reduced to ...s — shoot more or
  pass allow_repeats=True / --allow-repeats"). The cap never lengthens a
  montage and never applies when the request is already below it.
* **Pool trim.** Overlapping pool moments (sift output never overlaps,
  but hand-built reports and distilled timelines can) are trimmed per
  clip so no two moments claim the same frames.
* **Grid truncation.** Should the fill still run dry (padding losses,
  short clips), it NEVER rewinds: the grid is cut at the last slot fresh
  material could serve — every slot boundary already sits on the musical
  grid, and the ending additionally snaps down to a phrase/downbeat when
  one lies within tolerance — and an honest note lands in the plan
  ("length reduced to 28.5s: 19 distinct moments, no repeats allowed —
  shoot more or allow repeats").

``allow_repeats=True`` (CLI ``--allow-repeats``) keeps the old,
unlimited behavior: the full requested length, tails first, then honest
rewinds ("some footage repeats").

Perceived variety
-----------------
Even a repeat-free cut can FEEL repetitive when most shots come from one
clip. When more than :data:`_VARIETY_SHARE` (60%) of a cut's entries
share a single source clip, one deterministic note is appended:
"variety: N of M shots come from one clip — more footage would help".

Cut-ahead lead
--------------
Editors place cuts 1-2 frames BEFORE the beat so the incoming shot is
already on screen when the beat lands — a cut exactly ON the beat reads
late. ``cut_lead`` (default ``_DEFAULT_CUT_LEAD`` = 0.04 s, ~1 frame at
25 fps; 0 disables) shifts every interior cut point earlier by that
amount after the grid is built, clamped so ordering is preserved, no
slot drops below ``_LEAD_MIN_SLOT`` (0.25 s, or its own original length
if shorter), the first cut stays at 0 and the final boundary stays at
the montage length. Blueprint 1.7 refinements: ``plan_montage(fps=...)``
types the lead in frames (:func:`cut_lead_for` — exactly one frame at
the delivery rate, explicit leads quantized to whole frames; the one
shared decision), and DISSOLVING boundaries take no lead at all
(dissolve lead 0): after :func:`_plan_finishing` decides the dissolves,
:func:`_undo_lead_on_dissolves` moves each dissolving boundary back to
its unshifted grid position — a dissolve ramps ACROSS the beat, so
starting it early is starting it off the grid.

Frame hygiene (blueprint 1.7)
-----------------------------
No generated slot below ``_MIN_SLOT_SECONDS`` (~0.3 s): grid remainders,
phase-bound adjacency and drop-cut insertion run through
:func:`_absorb_slivers` (slivers merge into a neighbour, pinned cuts
win), the dip carving keeps a beat-aware remainder floor and the
no-repeats truncation drops a sub-floor tail shot. Dips, dissolves and
the renderers' title fades take BEAT-QUANTIZED durations via the one
shared :func:`quantize_finish` helper against :func:`plan_pulse` (the
plan's persisted downbeat marks — the tempo witness surgery and the
renderers share with the planner); beatless plans keep the classic
fixed values byte-for-byte.

Cut on action (peak-on-beat)
----------------------------
Blueprint 1.1: the image's accent and the music's accent must be the
SAME event. When a sifted moment carries an intra-moment envelope peak
(:attr:`monteur.sift.Moment.peak_time` — motion blended with audio
level, honest to ±0.25 s), the fill no longer plays the moment from its
head: the in-point is chosen so the peak lands exactly on the slot's
beat (``record_start + cut_lead``; slot 0's beat is 0), clamped to the
moment's bounds (:func:`_aim_start`). Drop HOLDS aim the peak at the
drop instant and extend PAST it through the pool's vetted slack (the
enclosing USABLE sift segment, capped by the next same-clip moment and
the clip length). Guarantees: skipped head material is remembered as a
reclaimable gap and served by the reuse phase — the zero-repeat promise
never burns it; pinned/arranged slots stay bit-identical; and a pool
without peak signals (hand-built reports, old distills) fills
byte-identically to before the aim existed. The "short" style's hook
slot additionally nudges its aimed in-point to the sharpest first frame
within the ±0.25 s window (blueprint 1.9 — frame 1 is the thumbnail).
The plan notes count what was aimed ("cut on action: N of M slots ...").

No-music plans and audio modes
------------------------------
``plan_montage(reports, music=None, max_duration=...)`` plans a cut with
no song at all (ride-POV videos where the clips' own engine sound IS the
soundtrack): the grid falls back to fixed intervals per style phase
(``beats_per_cut x _PSEUDO_BEAT`` = 0.75 s — slow phases every ~3 s,
fast every ~0.75 s), with no drops/phrases/sections; ``music_path`` is
"" and ``song_duration``/``music_start`` are 0. :func:`montage_to_timeline`
takes ``audio=``: "music" (song on A1, today's behavior), "mix" (song on
A1 plus each entry's own audio on A2) or "original" (no song clip; each
entry's own audio on A1). A no-music plan only renders with "original".

Adaptive music window
---------------------
The song does not have to play over the full length: ``plan.music_in`` /
``plan.music_out`` (record seconds; 0 = full length) carry an adaptive
music window. The TOOL decides when the music enters — never a rigid
per-style rule: :func:`decide_music_window` scores the song's own opening
character (:func:`monteur.music.intro_profile`, measured at the cut's
source window) against the style's openness to a dry cold open (the
scoring table sits above the function). An ambient intro starts at 0 in
every style; a hard, kick-driven intro slams in at the build start (a
trailer's dry open — and the mismatch penalty delays it under calm styles
too); "short" always starts at 0. A ``music_window=(in, out)`` kwarg
overrides (validated + snapped), and the composer may pick one of the
dossier's candidates. The record<->song mapping never changes — record t
always plays song time ``music_start + t`` — so every cut stays on the
beat; the exports simply mute the bed before ``music_in`` (with a short
musical fade-in at the entry in the ffmpeg renderers), and the SFX layer
anchors a riser ENDING exactly on the entry.

Deliberate silence (music gaps)
-------------------------------
"Bewusste Stille ist super, versehentlich nie": the song may break — on
purpose, never by accident. ``plan.music_gaps`` lists record-time windows
``(start, end)`` where the SONG is deliberately silent while the cut plays
on. Two silences are planned (``music_flow="deliberate"``, the default):

* **Under every smash-to-black dip** whose silence something CARRIES — a
  planned sub-drop/impact cue at the dip (a marker cue counts: it records
  the intent, and :mod:`monteur.elements` files it with a real braam/hit
  when a library is given; without the SFX layer there is no carrier and
  the song plays straight through the black, exactly as before). The gap
  is the dip window, extended to end on the FOLLOWING DOWNBEAT when one
  lies within ~1 beat past the dip end — the re-entry lands musically,
  not mid-bar.
* **One beat of absolute silence before the drop** — the first in-range
  drop, when the song is already playing there: the gap is
  ``[drop - 1 beat, drop)`` and the song re-enters EXACTLY on the drop.
  Never for the "short" style (60 seconds has no room to breathe), never
  when the beat before the drop reaches into the ``music_in`` dry open
  (no double silence), never on top of a dip gap. The cut itself carries
  this one — no cue required, and it never exceeds one beat.

Ordering: gaps are planned LAST in ``plan_montage`` — after
:func:`_plan_finishing` (which creates the dips), after :func:`_plan_sfx`
(whose sub-drop cues are the dip carriers) and after the arrangement cues.
:func:`monteur.elements.assign_elements` runs later (at the caller layer)
and only ever FILES existing cues or adds new ones, so a carrier at plan
time stays a carrier. The record<->song mapping never changes — record t
still plays song time ``music_start + t``; a gap only MUTES that span, so
after the gap the bed continues from ``music_start + gap end`` and every
beat stays exactly where it was. Every surface honors the gaps: the
timeline/FCPXML split the music bed into one clip per audible span
(:func:`music_bed_segments`), the Resolve append places one positioned
music clip per span, the ffmpeg renderers gate the bed's volume (50 ms
micro-fades against clicks, see :mod:`monteur.preview`), and Studio's
virtual playout volume-gates its free-running audio element. Notes
narrate every gap. ``music_flow="continuous"`` disables all gating and
plans byte-identically to before the field existed; plan surgery
(:func:`adjust_entry_boundary`, :func:`pin_entry`) prunes gaps whose dip
or drop vanished — a stale gap would be exactly the accidental silence
this feature exists to prevent.

SFX layer (film mode)
---------------------
``plan_montage(..., sfx=True)`` plans a sound-design layer on top of the
finished cut — for films where the effects carry the edit instead of (or
alongside) the music, e.g. ride-POV cuts with ``audio="original"``.
Monteur cannot render audio, so the deliverable is CUES: each
:class:`SfxCue` says when (``time``/``duration``), what (``kind``), what
to search for in an SFX library (``query``) and why (``note``). Placement
reads what the plan already knows:

* an **ambience** bed at 0 under the opening phase (the first
  ``_SFX_AUTO_OPENING`` = 4 s for "auto"); its query comes from the
  opening entries' vision labels ("mountain pass ambience"), falling back
  to the honest generic "outdoor ambience",
* a **riser** ENDING exactly on every act change (label changes only —
  the trailer's split build ramps inside one act and gets no riser),
  ``duration = min(2 s, prior phase / 2)``,
* an **impact** ON the climax start and ON every drop-forced cut —
  "auto"'s every-drop cuts, "short"'s pin, and (blueprint 2.1) the arc
  styles' secondary-drop cuts,
* a **sub-drop** under every smash-to-black dip (a title slot wants a
  boom),
* **whooshes** (0.6 s, centered on the cut) on up to 3 of the fastest
  cuts, each keeping 1 s clearance from every other cue.

Density is capped at ~1 cue per ``_SFX_SECONDS_PER_CUE`` (5 s) of cut:
whooshes are dropped first, then risers (the riser INTO the climax
survives longest, then earlier act changes); ambience/impact/sub-drop are
the backbone and always stay. Cues are sorted by time and reported in the
notes. :func:`montage_to_timeline` exports each cue as a Green timeline
marker ("SFX: <kind>" / "<query> — <note>"), which the EDL/FCPXML writers
and the Resolve bridge already carry. ``sfx=False`` (the default) plans
exactly as before.

On top of the cue markers, :mod:`monteur.elements` can place REAL files
from the user's own sound library: :class:`SfxCue` carries an optional
``file`` path plus a ``source_offset`` (seconds into the file — a riser
plays its LAST run-up seconds so the build ends at its climax, blueprint
1.3), and :func:`montage_to_timeline` renders filed cues as audio clips
on a dedicated SFX track ("A2" music/original, "A3" mix) while keeping
the marker. ``plan_to_dict`` writes ``file`` and ``source_offset`` only
when set, so plans without placed elements serialize exactly as before.

Arrangement (the editor's own scene order)
------------------------------------------
``plan_montage(..., arrangement=[...])`` lets the editor dictate the
CASTING ORDER while the engine keeps the craft: the grid (style, rhythm
canon, phases, drop logic) builds exactly as without an arrangement, and
the arranged scenes then claim the slots in the user's order from slot 0
upward. Each item is a dict ``{"clip": path, "start": seconds}`` plus an
optional ``"after"`` (``{"transition": "cut"|"dissolve"|"smash"}`` — or
the bare string) for the boundary INTO the next slot and an optional
``"sfx"`` (``"impact"|"whoosh"|"riser"``) cue at that boundary. The item
is matched to a sifted moment by clip + start overlap (the director's
matcher), snapped/trimmed to the slot's duration with the fill's own
rules, and marked consumed so the auto-fill of any REMAINING slots never
replays it first. More scenes than slots keeps the user's order and
drops the excess from the end, honestly noted. After building, a
deterministic consistency report lands in the notes under an
``arrangement:`` prefix — how many slots follow the order, trims onto
the beat grid, pacing flags (a calm scene on the drop; two takes of the
same scene back to back), the unplaced excess. ``arrangement=None``
(the default) is byte-identical to before — the arrangement is an
INPUT, not plan state, so :func:`plan_to_dict` is untouched. The
composer (:mod:`monteur.compose`) treats arranged slots as LOCKED: they
are flagged in the dossier and any cast for them is ignored.

Plan persistence & revision
---------------------------
:func:`plan_to_dict` / :func:`plan_from_dict` round-trip a full plan
through JSON (every field, entries, dips, SFX cues, notes, and — when a
composer set them — the ``title_texts`` for the dips) under a
``"monteur_plan"`` schema version — the save format behind ``monteur
create --save-plan`` and the input to the revision loop
(:mod:`monteur.revise`). :func:`pin_entry` is the revision's pinning
hook: it forces one entry verbatim into a plan, trimming or dropping
whatever the re-plan put in its way.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import asdict, dataclass, field, replace
from pathlib import PurePath

from monteur import reframe as _reframe
from monteur.model import AUDIO, VIDEO, Clip, Marker, Timeline, seconds_to_frames
from monteur.music import (
    MusicAnalysis,
    MusicSection,
    best_drop,
    best_energy_window,
    drop_weight,
    intro_profile,
)
from monteur.sift import USABLE, ClipReport, Moment

CHRONOLOGICAL = "chronological"  # keep footage order (travel/event films)
BEST_FIRST = "best_first"  # strongest material on the strongest sections

# How many beats between cuts, per section energy label.
BEATS_PER_CUT = {"high": 1, "mid": 2, "low": 4}
# Anti-strobe floor: a cut interval below this doubles the beat step.
MIN_CUT_INTERVAL = 0.4
# Grid interval used when the song has no detected beats.
FALLBACK_INTERVAL = 2.0

# A phase cutting every >= this many beats is "slow": its cuts go on downbeats.
_SLOW_PHASE_STEP = 4
# Downbeat detection assumes 4/4; slow phases cut every (step / this) downbeats.
_BEATS_PER_BAR = 4
# Rhythm (the anti-monotony canon; see the module docstring's Rhythm section):
# a stutter burst is this many consecutive one-beat cuts directly before the
# drop (only when the build is fast enough to afford it).
_STUTTER_CUTS = 3
# Breath in the canon (blueprint 1.6). After the drop hold the canon takes
# ONE recovery breath — a cut of this many x the climax base — before it
# re-accelerates into the pattern: peaks need valleys, and a drop followed
# by instant machine-gun cuts reads relentless, not climactic.
_RECOVERY_MULT = 2
# ...and a LONG climax (at least two phrase groups) alternates "hot"
# 8-unit groups (the style's own pattern) with "cool" ones (the pattern's
# multipliers doubled) instead of looping one flat 4-cycle — constant
# intensity is no intensity.
_CLIMAX_GROUP_UNITS = 8
_COOL_PATTERN_MULT = 2.0
# "auto" rhythm: a longer hold opens each music section (the first multiplier,
# capped by _opening_hold), then a breath every len(pattern)-th cut.
_AUTO_PATTERN = (2.0, 1.0, 1.0, 1.0)
# Perceived variety: when more than this share of a cut's entries come from
# ONE source clip, a note says more footage would help (see the module
# docstring's Perceived variety section).
_VARIETY_SHARE = 0.6
# No-music plans have no beat grid; each phase cuts on a fixed interval of
# (beats_per_cut x this nominal pseudo-beat) seconds — slow phases every ~3s,
# fast phases every ~0.75s.
_PSEUDO_BEAT = 0.75
# Cut-ahead lead (seconds, ~1 frame at 25 fps): interior cuts are shifted this
# far BEFORE the beat so the incoming shot is on screen when the beat lands.
# Blueprint 1.7 (typed fps-aware leads): 0.04 s is the SECONDS APPROXIMATION
# of one frame that stands when no fps is known; ``plan_montage(fps=...)``
# resolves the lead through :func:`cut_lead_for` instead — exactly one frame
# at the delivery rate, and any explicit lead quantized to whole frames.
# ONE decision, applied everywhere the lead is read.
_DEFAULT_CUT_LEAD = 0.04
# Lead shifting never squeezes a slot below this (seconds).
_LEAD_MIN_SLOT = 0.25
# Frame hygiene (blueprint 1.7): no generated slot below this floor —
# ~0.3 s (>= 2 frames at any delivery rate up to 60 fps *with margin*; a
# shot shorter than this reads as a glitch, not a cut). Enforced at every
# slot-producing site: grid remainders and phase-bound adjacency
# (:func:`_absorb_slivers`), drop-forced cut insertion (same pass), the
# dip carving (via :data:`_DIP_MIN_REMAINDER`, raised to this floor) and
# the no-repeats truncation's straddle trim. Pinned drops beat the floor
# (a sliver next to two protected boundaries stays, honestly).
_MIN_SLOT_SECONDS = 0.3
# J/L cuts (blueprint 2.3): at a chosen quiet scene transition the
# original-sound edit point is decoupled from the picture cut by this small,
# fps-aware lead/lag (~5 frames at 25 fps; quantized to whole frames per the
# ``cut_lead_for`` spirit). A J-cut brings the NEXT shot's audio in early,
# an L-cut lets the PREVIOUS shot's audio ring past — never at a
# peak-on-beat/drop cut, a music-gap edge, a placed-SFX cut or a climax
# boundary (see :func:`jl_audio_edits`).
_JL_LEAD_SECONDS = 0.2
# A boundary counts as "on" a drop / gap / SFX / cut position when it sits
# within this many seconds of it — one frame of slop at the coarsest rates.
_JL_ON_CUT_TOL = 0.05
# Never leave either side of a J/L seam a solo-audio sliver: both the
# shortened and the extended shot keep at least this much of their own,
# un-overlapped original sound.
_JL_MIN_SOLO = 0.4
# J/L is a spice, not a rule: at most this many boundaries per montage are
# decoupled, the highest-CONTRAST ones first (a real hot<->cool phrase
# change beats a flat continuity seam), and never two in a row.
_JL_MAX_CUTS = 4
# Loop seam (blueprint 1.5, "short" style): the LAST slot's casting earns a
# motion-continuity bonus for handing its EXIT motion back to the hook's
# ENTRY motion — the visual half of the loop seam. Sized like the fill's
# regular motion term (_MOTION_WEIGHT): it breaks ties, it does not
# overrule scores.
_LOOP_HANDBACK_WEIGHT = 0.3
# Arc-squeeze floor (blueprint 1.5): when the drop pin squeezes the phase
# boundaries on one side of the climax, every phase on that side keeps at
# least this share of the montage (redistributed from the side's larger
# phases). When even the side's whole span cannot afford everyone the
# floor, the proportional squeeze stands — the pin always wins.
_ARC_MIN_PHASE_SHARE = 0.05
# Peak-on-beat (blueprint 1.1): when a fresh moment carries a sifted
# ``peak_time``, its in-point is chosen so the peak lands on the slot's
# beat (record_start + cut lead; the montage's first slot has no lead).
# A skipped head at/above this many seconds is remembered as a reclaimable
# GAP instead of being burnt — the zero-repeat promise must not lose
# material to the aim; shorter slivers cannot serve a slot anyway.
_PEAK_GAP_MIN = MIN_CUT_INTERVAL
# First-frame gate (blueprint 1.9, "short" style only): the hook slot's
# aimed in-point may shift to a sharper first frame among the sifted
# quality samples within this window (the peak promise's own ±0.25 s
# honesty), and only for a real quality gain — frame 1 is the thumbnail,
# but the peak still rules.
_HOOK_GATE_WINDOW = 0.25
_HOOK_GATE_MIN_GAIN = 0.02
# Audio modes for montage_to_timeline.
_AUDIO_MODES = ("music", "mix", "original")
# Transition modes for plan_montage: how clips hand over to each other.
# "auto" = per-cut intelligence (same-clip continuations and climax/"high"
# passages cut hard, daylight-block changes and scene changes in calm
# passages dissolve; the trailer smashes to black at act changes),
# "cuts" = hard cuts only, "dissolves" = dissolve on every cut,
# "smash" = black title-slot gaps at act/section changes.
TRANSITION_MODES = ("auto", "cuts", "dissolves", "smash")
# Canvas presets for montage_to_timeline: shape x resolution.
CANVASES: dict[str, tuple[int, int]] = {
    "hd": (1920, 1080),  # 16:9 in HD
    "uhd": (3840, 2160),  # 16:9 in 4K
    "vertical": (1080, 1920),  # Shorts / Reels / TikTok 9:16 in HD
    "vertical-uhd": (2160, 3840),  # 9:16 in 4K
    "cine": (1920, 804),  # 2.39:1 cinemascope in HD
    "cine-uhd": (3840, 1608),  # 2.39:1 in 4K
}
# Drop alignment only when the drop falls inside this share of the montage.
_DROP_ALIGN_MARGIN = 0.05
# Secondary-drop forced cuts in arc styles (blueprint 2.1). In an arc style
# only the CLIMAX pins the best drop; the other in-range drops are the
# "secondary" drops. The strongest of them (up to _SECONDARY_DROP_MAX) force
# a hard cut EXACTLY on the drop — but only when they musically carry and
# sit far enough from the climax and from each other:
#   * weight gate: a secondary must weigh at least this FRACTION of the
#     climax drop's own weight (:func:`monteur.music.drop_weight`) — a
#     softer drop than the payoff would cut for no reason. A section-less
#     song weighs every drop 0.0, so nothing qualifies and the plan is
#     byte-identical (the fraction of 0 is 0, and weight must be > 0).
_SECONDARY_DROP_WEIGHT_FRACTION = 0.5
#   * spacing gate: a secondary must be at least this many BEATS from the
#     climax pin and from every already-accepted secondary — two hard drop
#     cuts on top of each other shred the hold instead of hitting twice.
_SECONDARY_DROP_MIN_BEATS = 8
#   * count cap: at most this many secondary drops force a cut, strongest
#     first — a trailer has one payoff and maybe one pre-drop, not five.
_SECONDARY_DROP_MAX = 2
# Candidate window (K): unconsumed pool items considered per slot.
_CANDIDATE_WINDOW = 4
# Blend weights for near-tie breaking among the candidate window.
_ORDER_WEIGHT = 0.7
_MOTION_WEIGHT = 0.3
# Below this magnitude (px) a motion vector counts as "no motion" (neutral).
_MOTION_MIN_MAGNITUDE = 0.5
# Semantic casting (vision annotations on moments; see the module docstring).
# The bonuses are sized against the candidate blend above: one order-
# preference step is _ORDER_WEIGHT / _CANDIDATE_WINDOW = 0.175 and the
# motion term peaks at ±_MOTION_WEIGHT = 0.3.
# A candidate whose role fits the slot (its arc phase, or the montage's
# first/last slot) gains this much: flips ONE order position, never two —
# a mild preference, not a filter.
# Energy-motion matching: a slot's music energy should meet footage with
# matching motion — loud passages get moving shots, calm passages calm ones.
# Sized like _ROLE_WEIGHT: enough to flip ONE order position, never two.
_ENERGY_MATCH_WEIGHT = 0.2
# Nominal music energy per arc phase (arc styles have no section data).
# hook/punch/loop are the "short" style's phases: the hook wants maximum
# motion (the pattern interrupt), the punch stays hot, the loop cools only
# slightly — a short never winds down like an outro does.
_PHASE_ENERGY = {
    "opening": 0.35, "build": 0.65, "climax": 1.0, "outro": 0.3,
    "hook": 1.0, "punch": 0.85, "loop": 0.5,
}
_ROLE_WEIGHT = 0.2
# Hook casting (the "short" style; see the module docstring): slot 0 is
# reserved for the PATTERN INTERRUPT — the moment with the highest
# hook_score = 0.5 x motion (normalised to the pool's fastest moment)
# + 0.3 x hero + 0.2 x score. The "opener" role preference does NOT apply
# to that slot: a short opens on the boldest image, not the prettiest.
_HOOK_MOTION_WEIGHT = 0.5
_HOOK_HERO_WEIGHT = 0.3
_HOOK_SCORE_WEIGHT = 0.2
# Hero bonus: this x moment.hero on drop-reserved and climax-phase slots.
# A full hero (1.0) outweighs the motion term plus one order step, so the
# real hero shot wins the drop even against better motion continuity.
_HERO_WEIGHT = 0.5
# A candidate whose scene group matches a neighbouring filled slot loses
# this much — two takes of the same scene back to back read like a jump
# cut; an alternative one order step behind wins instead.
_GROUP_PENALTY = 0.25
# A drop-slot moment at/above this hero level is called out in the notes.
_HERO_NOTE_LEVEL = 0.5
# Time-of-day coherence (Moment.daylight, filled offline by
# monteur.daylight). COHERENCE IS THE LAW: footage wants to sit in
# time-of-day BLOCKS with rare, deliberate switches. A candidate whose
# daylight class differs from the PREVIOUS cast slot's pays this penalty —
# sized like _ROLE_WEIGHT relatives: below one order step (0.175), so it
# breaks near-ties instead of overruling the pool order. Zero when either
# side is unknown; pins and arranged slots are never scored, so explicit
# choices always win.
_DAYLIGHT_SWITCH_PENALTY = 0.15
# DIRECTION IS DIRECTION: the block ORDER is a story decision. The
# deterministic default is the material's natural arc — day -> golden ->
# night, filtered to the classes that actually exist — mapped over the
# montage proportionally to each class's share of pool material (see
# _daylight_targets). A candidate matching its slot's target class gains
# this small bonus; the composer (monteur.compose) may choose another
# block order deliberately, and an arrangement always wins outright.
_DAYLIGHT_BLOCK_WEIGHT = 0.1
# The natural arc order the deterministic default follows.
_DAYLIGHT_ARC = ("day", "golden", "night")
# Against-the-flow warnings: at most this many per-slot notes before the
# remainder is summarized in one line.
_DAYLIGHT_NOTE_LIMIT = 3
# --- Picture coherence (blueprint wave 3) ------------------------------------------
# Murch's Rule of Six: eye-trace and shot grammar are LOW ranks that may
# be sacrificed for the higher ones (emotion/story/rhythm). Every term
# below is a TIE-BREAKER on the candidate blend — sized BELOW one order
# step (_ORDER_WEIGHT / _CANDIDATE_WINDOW = 0.175), zero unless the
# offline spatial signal (monteur.spatial) is present, and never applied
# to a reserved slot (drop / hook / loop): there sync and the pin win
# outright. Footage without the signal casts byte-identically.
#
# Eye-trace continuity (3.1): the eye is carried across a cut when the
# outgoing shot's exit attention point sits near the incoming shot's
# entry attention point. The term rewards a small on-screen distance and
# mildly penalizes a jarring leap — EXCEPT at a phase boundary, where a
# deliberate contrast is the accent (the term is suspended there).
_EYE_TRACE_WEIGHT = 0.12
# The full frame diagonal in 0..1 coordinates — the maximum attention
# distance, mapped to the full -_EYE_TRACE_WEIGHT penalty.
_EYE_TRACE_DIAG = 2.0 ** 0.5
# Shot-size grammar (3.2): establish (wide) -> develop (medium) -> pay off
# (close). A one-step progression earns this bonus; two equally-sized
# neighbours pay this penalty (the montage keeps changing scale) — with
# ONE exception, a deliberate close->close intensification in the climax.
_SHOT_GRAMMAR_WEIGHT = 0.12
_SHOT_GRAMMAR_EQUAL_PENALTY = 0.1
_SHOT_ORDER = {"wide": 0, "medium": 1, "close": 2}
# Visual rhyme / callback (3.3): a SINGLE deliberate echo — the closing
# shot rhymes with the opening (frames the video). Kindred = similar shot
# size + attention point (the new spatial signal), leaning on daylight and
# motion where known. The bonus tips the LAST slot toward the moment most
# like slot 0's; sparing by construction (one rhyme, one slot). Zero-repeat
# holds: the rhyme is a DIFFERENT, still-unused moment, never a duplicate.
_RHYME_WEIGHT = 0.15
_RHYME_MIN_KINSHIP = 0.6  # below this the pair is not kindred enough to rhyme
# Learned-preference casting bias (blueprint 4.3): a small additive term
# for a candidate whose shot size matches a preference the user's
# corrections established for this slot's phase. Sized well BELOW one order
# step (_ORDER_WEIGHT / _CANDIDATE_WINDOW = 0.175) and below the group
# penalty — a tie-breaker only, never overriding sync, the drop, the
# rhythm order, or zero-repeat. Empty preferences ⇒ zero bonus ⇒
# byte-identical to today.
_PREF_SHOT_WEIGHT = 0.08
# Content-adaptive pacing (the slot-merge pass; see the module docstring's
# Content-adaptive pacing section). Adjacent slots on calm music that are
# cast with calm material merge into one longer shot, so the cut count
# drops exactly where the content is slow.
# A slot merges only when its music energy (song section or nominal phase
# energy — the same numbers energy matching uses) is at/below this:
# "low"/"mid" sections and the gentle opening/outro phases qualify, the
# build/climax/hook/punch never do.
_MERGE_MAX_SLOT_ENERGY = 0.5
# A cast moment is "calm" when its motion (mean entry/exit magnitude
# normalised to the pool's fastest moment — the exact value energy
# matching scores with) is at/below this...
_MERGE_CALM_MOTION = 0.35
# ...and its audio highlight is at/below this (matches the arrangement's
# calm-on-the-drop threshold).
_MERGE_CALM_HIGHLIGHT = 0.3
# Same-clip continuity (the continuity-merge pass + the jump-cut guard;
# see the module docstring's Same-clip continuity section).
# Adjacent slots cast from the SAME clip whose source windows sit within
# this many seconds of each other are ONE continuing shot chopped in two:
# the merge joins them into one continuous take (bridging the gap with
# the clip's own in-between frames).
_CONTINUITY_MAX_GAP = 3.0
# The casting guard: a candidate that would put the same clip next to
# itself with a source gap under this many seconds pays the jump-cut
# penalty (unless the continuity merge would join the pair anyway) —
# a cut that jumps a few seconds INSIDE one scene reads as an error,
# not an edit.
_JUMP_CUT_MIN_GAP = 8.0
# Sized ABOVE the group penalty and well above one order step
# (_ORDER_WEIGHT / _CANDIDATE_WINDOW = 0.175): a different clip two
# order positions behind still wins over a same-scene jump cut.
_JUMP_CUT_PENALTY = 0.4
# A surviving same-clip boundary with a source skip at/above this many
# seconds is a VISIBLE jump cut (below it the shot simply continues);
# survivors are counted into one honest low-variety note.
_JUMP_CUT_VISIBLE_GAP = 0.25
# Auto pace (pace=None; see the module docstring's Auto pace section):
# the per-phase bases are derived, not fixed. Arc styles start from their
# beats_per_cut table and get biased one "notch" slower (base x 2) per
# signal that fires — at most two notches:
#   * content mix: at least _AUTO_PACE_CALM_SHARE of the pool's material
#     is calm (the _MERGE_CALM_* thresholds — the same numbers the
#     calm-merge pass uses); no motion data anywhere = no content signal;
#   * music density: the windowed song's duration-weighted mean section
#     energy is at/below _AUTO_PACE_LOW_ENERGY.
# The arc-less "auto" style is excluded (its section grid already reads
# the song's density directly, and the merge passes adapt it to content);
# so is "short" (the vertical anti-canon never slows down). An explicit
# ``pace`` overrides — the bias only runs when no pace was given.
_AUTO_PACE_CALM_SHARE = 0.6
_AUTO_PACE_LOW_ENERGY = 0.35
# Which vision role each arc phase asks for.
_ROLE_FOR_PHASE = {
    "opening": "opener",
    "build": "build",
    "climax": "climax",
    "outro": "closer",
}
# Musical ending: max relative change when snapping the length to a phrase.
_END_SNAP_TOLERANCE = 0.12
# Dissolve INTO a gentle-phase entry: min(this, half the slot length).
_MAX_DISSOLVE = 0.5
# Planned fades (seconds) for styles with an outro phase / for "auto".
_FADE_IN = 0.5
_MAX_FADE_OUT = 2.0
_AUTO_FADE_OUT = 1.0
# Smash to black: black-gap length at act changes, and the minimum slot
# length the shortened outgoing clip must keep. Blueprint 1.7: both are
# TARGETS, not gospel — when the plan knows its tempo (persisted downbeat
# marks, :func:`plan_pulse`) the dip length is beat-quantized through the
# shared :func:`quantize_finish` helper (nearest half-beat inside
# [_DIP_QUANT_MIN, _DIP_QUANT_MAX]) so the black spans a musical duration
# ending on the on-grid boundary, and the remainder floor rises to a
# half-beat (never below the raw floor). Beatless plans keep the classic
# fixed values byte-for-byte. _DIP_MIN_REMAINDER itself was raised
# 0.25 -> 0.3 to match the sliver floor (_MIN_SLOT_SECONDS): the carved
# outgoing remainder is a slot like any other.
_DIP_SECONDS = 0.4
_DIP_MIN_REMAINDER = _MIN_SLOT_SECONDS
_DIP_QUANT_MIN = 0.2
_DIP_QUANT_MAX = 0.8
# Samples per second in MontagePlan.music_energy (the timeline strip's
# energy lane): sample i covers record time i / MUSIC_ENERGY_RATE.
MUSIC_ENERGY_RATE = 2.0
# A dip "sits on" an entry boundary when start+length lands within this
# many seconds of the entry's record_start (tolerates the cut-lead shift).
_BOUNDARY_EPS = 0.05

# Arrangement (the editor's own scene order; see the module docstring).
# Valid "after" boundary requests and "sfx" boundary cues per item.
ARRANGEMENT_TRANSITIONS = ("cut", "dissolve", "smash")
ARRANGEMENT_SFX_KINDS = ("impact", "whoosh", "riser")
# A moment trimmed by less than this (seconds) is not worth a trim note.
_ARR_TRIM_NOTE_MIN = 0.05
# An arranged boundary cue is skipped when a same-kind cue already sits
# within this many seconds (don't double an impact the SFX layer planned).
_ARR_CUE_CLEARANCE = 0.5
# Calm-on-the-drop flag: an arranged moment counts as calm when its mean
# motion magnitude is at/below _MOTION_MIN_MAGNITUDE and its highlight is
# below this — flagged only when the pool holds a livelier alternative.
_ARR_CALM_HIGHLIGHT = 0.3
# Ready-to-paste library queries for arranged boundary cues (the same
# wording _plan_sfx uses, so assign_elements files them identically).
_ARR_SFX_QUERIES = {
    "impact": "cinematic impact hit",
    "whoosh": "whoosh transition fast",
    "riser": "riser build up",
}

# SFX layer (plan_montage(..., sfx=True)) — see the module docstring.
# Density cap: at most ~one cue per this many seconds of cut, so the plan
# never drowns in cues. Whooshes are dropped first, then risers; ambience,
# impacts and sub-drops are the backbone and always survive.
_SFX_SECONDS_PER_CUE = 5.0
# Riser length: min(this, half the phase it builds out of) — it must grow
# out of the prior act, not drown it.
_SFX_RISER_MAX = 2.0
# Impact hits ring out about this long (a length suggestion for the search,
# not a trim instruction).
_SFX_IMPACT_LENGTH = 1.0
# Whoosh length, centered on its cut, and how many at most (the montage's
# fastest cuts get them).
_SFX_WHOOSH_LENGTH = 0.6
_SFX_MAX_WHOOSHES = 3
# A whoosh keeps this much clearance (seconds) from every other cue, so two
# effects never pile onto the same moment.
_SFX_WHOOSH_CLEARANCE = 1.0
# "auto" has no opening phase; the ambience bed covers this many seconds.
_SFX_AUTO_OPENING = 4.0
# Label words too generic to search an SFX library with.
_SFX_STOPWORDS = frozenset(
    "the a an and of in on at to with into over under from through".split()
)

_EPS = 1e-6


@dataclass(frozen=True)
class MontageStyle:
    """An editorial cutting style: a story arc mapped onto the song."""

    key: str
    name: str
    description: str  # one line an editor understands
    # (share_of_duration, phase label "opening"/"build"/"climax"/"outro").
    # Empty arc = section-energy-driven ("auto"). A label may repeat in
    # consecutive entries; the beat step then ramps toward the next phase's
    # step ("trailer" uses this to accelerate through its split build).
    arc: list[tuple[float, str]]
    beats_per_cut: dict[str, int]  # phase label -> beats between cuts
    prefer_highlights_in: str = "climax"  # phase where highlights win slots
    # Smash to black: act changes cut to a short black gap (a title slot)
    # instead of running clip-to-clip — the classic trailer breath.
    smash_to_black: bool = False
    # Per-phase rhythm texture: a repeating cycle of multipliers on the
    # phase's base step (quantized to whole beats at build time; every
    # multiplier >= 1, so `pace` keeps meaning "fastest cut"). A missing
    # label cuts at the constant base — the canon moves (opening hold,
    # build accelerando, drop hold, stutter, outro decelerando) are applied
    # by the grid builders on top and are the same for every style.
    rhythm: dict[str, tuple[float, ...]] = field(default_factory=dict)
    # Anti-canon for social shorts: True DISABLES the establishing hold —
    # the montage's first cut stays at its phase's base, additionally
    # capped at :data:`_MAX_HOOK_SECONDS` absolute. Vertical viewers decide
    # in the first second; a short must hook, never establish. Consumed by
    # :func:`_style_rhythm_specs`; False keeps every existing style
    # byte-identical.
    no_opening_hold: bool = False


_ARC_STANDARD = [(0.15, "opening"), (0.35, "build"), (0.35, "climax"), (0.15, "outro")]

STYLES: dict[str, MontageStyle] = {
    "auto": MontageStyle(
        key="auto",
        name="Auto (section energy)",
        description=(
            "Follows the song's own energy: calm sections cut every 4 beats, mid "
            "every 2, loud every beat; a drop forces a cut with the strongest moment."
        ),
        arc=[],
        beats_per_cut={},
    ),
    "travel": MontageStyle(
        key="travel",
        name="Travel film",
        description=(
            "Scenic slow opening, steady build, beat-for-beat climax, calm outro "
            "(4/2/1/4 beats per cut over a 15/35/35/15 arc)."
        ),
        arc=list(_ARC_STANDARD),
        beats_per_cut={"opening": 4, "build": 2, "climax": 1, "outro": 4},
        # Gentle texture: the climax breathes every fourth cut.
        rhythm={"climax": (1, 1, 2, 1)},
    ),
    "wedding": MontageStyle(
        key="wedding",
        name="Wedding film",
        description=(
            "Gentle throughout — never faster than every 2 beats, so faces and "
            "gestures get room to breathe (4/2/2/4)."
        ),
        arc=list(_ARC_STANDARD),
        beats_per_cut={"opening": 4, "build": 2, "climax": 2, "outro": 4},
        # Gentle waltz breath: alternate 2- and 3-beat cuts at the peak.
        rhythm={"climax": (1, 1.5)},
    ),
    "music_video": MontageStyle(
        key="music_video",
        name="Music video",
        description=(
            "Fast throughout — cuts every 1-2 beats from the first bar for "
            "constant energy (2/1/1/2)."
        ),
        arc=list(_ARC_STANDARD),
        beats_per_cut={"opening": 2, "build": 1, "climax": 1, "outro": 2},
        # Punchy: an accented long cut early in the opening cycle and a
        # 2-beat slam every fourth climax cut.
        rhythm={"opening": (1, 2, 1, 1), "climax": (1, 1, 2, 1)},
    ),
    "trailer": MontageStyle(
        key="trailer",
        name="Trailer",
        description=(
            "Long tease, accelerating build (ramping from every 4 beats down "
            "to every beat), hard climax, snap outro (20/50/20/10 arc)."
        ),
        # The build is split in half so the beat step can ramp 2 -> 1.
        arc=[(0.2, "opening"), (0.25, "build"), (0.25, "build"), (0.2, "climax"), (0.1, "outro")],
        beats_per_cut={"opening": 4, "build": 2, "climax": 1, "outro": 4},
        smash_to_black=True,
        # Aggressive: three snaps, then a 2-beat slam.
        rhythm={"climax": (1, 1, 1, 2)},
    ),
    "short": MontageStyle(
        key="short",
        name="Social Short",
        description=(
            "Vertical 9:16 attention: a 1-beat hook up front (shorts do NOT "
            "establish), a relentless 1-2-beat punch body, and a short loop "
            "outro that returns to the hook's scene (8/72/20 arc)."
        ),
        # hook = the pattern interrupt, punch = the whole body, loop = a
        # short outro whose last shot cuts back into the hook (see the
        # hook/loop casting in _fill).
        arc=[(0.08, "hook"), (0.72, "punch"), (0.2, "loop")],
        beats_per_cut={"hook": 1, "punch": 1, "loop": 2},
        prefer_highlights_in="punch",
        # Punchy like the music video: a 2-beat slam every fourth punch cut.
        rhythm={"punch": (1, 1, 2, 1)},
        no_opening_hold=True,
    ),
}


# Platform presets: what "I'm making a TikTok" means in engine terms —
# pure data, resolved by the CALLER (web server, CLI) via
# :func:`resolve_platform`. plan_montage never sees a platform, so the
# engine stays orthogonal. "canvas" is a :data:`CANVASES` key, "style" a
# :data:`STYLES` key (None = the user's own choice stands), "max_seconds"
# a CAP on the requested/derived duration (min of both, never an
# extension; None = no cap).
PLATFORMS: dict[str, dict] = {
    "youtube": {"canvas": "uhd", "style": None, "max_seconds": None},
    "short": {"canvas": "vertical-uhd", "style": "short", "max_seconds": 60.0},
    "reel": {"canvas": "vertical-uhd", "style": "short", "max_seconds": 90.0},
    "tiktok": {"canvas": "vertical-uhd", "style": "short", "max_seconds": 60.0},
}


def resolve_platform(
    platform: str,
    style: str | None = None,
    canvas: str | None = None,
    max_duration: float | None = None,
) -> dict:
    """Resolve a :data:`PLATFORMS` preset onto the existing kwargs.

    The one shared precedence rule set (web server and CLI both call this,
    so a "Short" means the same thing everywhere):

    * **canvas** — the platform always sets it: the frame IS the platform
      (a 16:9 TikTok is not a TikTok). The incoming ``canvas`` is ignored.
    * **style** — an EXPLICIT style wins over the preset's: any incoming
      style other than ``None``/``""``/``"auto"`` (the default)/the
      preset's own style is kept, and a note explains that the platform
      then only sets the canvas and caps the length. Otherwise the
      preset's style applies; presets with ``style: None`` (YouTube)
      always keep the user's choice.
    * **duration** — ``max_seconds`` CAPS the request: the resolved value
      is ``min(requested, cap)``, or the cap itself when nothing was
      requested (the cap then bounds the song-derived length). Never an
      extension; an actual cap is noted.

    Returns ``{"style", "canvas", "max_duration", "notes"}`` — drop the
    notes into ``plan.notes`` after planning so the result says what the
    preset did. Unknown platforms raise ValueError listing the valid ones.
    """
    if platform not in PLATFORMS:
        valid = ", ".join(PLATFORMS)
        raise ValueError(f"unknown platform {platform!r}; valid platforms: {valid}")
    preset = PLATFORMS[platform]
    notes: list[str] = []
    resolved_style = style
    preset_style = preset["style"]
    if preset_style:
        if style in (None, "", "auto", preset_style):
            resolved_style = preset_style
        else:
            notes.append(
                f'platform "{platform}": keeping your "{style}" style — the '
                f"preset only sets the {preset['canvas']} canvas and caps "
                "the length"
            )
    cap = preset["max_seconds"]
    resolved_max = max_duration
    if cap is not None and (max_duration is None or max_duration > cap + _EPS):
        resolved_max = float(cap)
        notes.append(f'platform "{platform}": length capped at {cap:g}s')
    return {
        "style": resolved_style,
        "canvas": preset["canvas"],
        "max_duration": resolved_max,
        "notes": notes,
    }


@dataclass
class MontagePlan:
    """The chosen cut points before rendering to a timeline."""

    music_path: str
    duration: float  # seconds, montage length (may be shorter than the song)
    music_start: float = 0.0  # seconds into the song where the cut begins
    song_duration: float = 0.0  # seconds, full length of the source song (0 = unknown)
    fade_in: float = 0.0  # seconds, intended music/video fade-in
    fade_out: float = 0.0  # seconds, intended music/video fade-out
    entries: list["MontageEntry"] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # (start, length) of black gaps on V1 (smash-to-black title slots).
    dips: list[tuple[float, float]] = field(default_factory=list)
    # Planned sound-design cues (plan_montage(..., sfx=True); empty otherwise).
    sfx: list["SfxCue"] = field(default_factory=list)
    # Composed act-title texts, aligned with ``dips`` by index ("" = no text
    # for that dip). Filled by :mod:`monteur.compose` when Claude composes
    # the cut; :func:`monteur.resolve.titles_from_plan` prefers these over
    # its derived texts. Empty list (the default) = no override anywhere.
    title_texts: list[str] = field(default_factory=list)
    # --- timeline-strip metadata (all additive; empty = not available) -----
    # The story-arc phase spans in RECORD time: (start, end, label) with
    # label one of "opening"/"build"/"climax"/"outro". Filled by
    # plan_montage for arc styles (and no-music pseudo grids); the arc-less
    # "auto" style has no phases and leaves this empty. Serialized only
    # when set, so old plans (and auto plans) round-trip byte-identically.
    phases: list[tuple[float, float, str]] = field(default_factory=list)
    # The song's smoothed section energy under the montage window, sampled
    # at a fixed :data:`MUSIC_ENERGY_RATE` (2 samples/second): sample ``i``
    # is the 0..1 energy at record time ``i / MUSIC_ENERGY_RATE``, rounded
    # to 3 decimals — ~2 floats per montage second, small by construction.
    # Written only when the plan was built against analyzed music.
    music_energy: list[float] = field(default_factory=list)
    # Compact beat marks in RECORD time: DOWNBEATS only (bar starts, "the
    # one"), not every beat — a 3-minute song carries ~90 downbeats vs
    # ~360 beats. Rounded to 2 decimals. Written only when music exists.
    beat_marks: list[float] = field(default_factory=list)
    # Drop/chorus impact times in RECORD time (usually 0-3 values) — the
    # strip's accent markers. Written only when music exists and in range.
    drop_marks: list[float] = field(default_factory=list)
    # The song's tempo in BPM (0 = unknown / no analyzed music). The
    # timeline header reads it for the "N BPM" readout; serialized only
    # when set, tolerant like the other strip metadata.
    tempo: float = 0.0
    # --- adaptive music window (all additive; 0 = the full-length default) --
    # RECORD-time seconds where the music ENTERS (0 = with the first frame)
    # and where it ENDS (0 = the montage end). The record<->song mapping is
    # unchanged: record t always plays song time music_start + t — a
    # non-zero music_in simply mutes the song's first music_in seconds so
    # the cut opens dry and the music slams in ON the grid. Decided by
    # :func:`decide_music_window` (or the ``music_window`` override /
    # the composer); serialized only when set, tolerant like title_texts.
    music_in: float = 0.0
    music_out: float = 0.0
    # --- deliberate silence (all additive; empty = the song never breaks) --
    # RECORD-time windows (start, end) where the SONG is deliberately
    # silent while the cut plays on: under carried smash-to-black dips
    # (extended to the following downbeat) and for one beat before the
    # drop (see the module docstring's Deliberate silence section). The
    # record<->song mapping is unchanged — a gap only mutes its span, the
    # bed continues from music_start + end. Planned by _plan_music_gaps
    # (music_flow="deliberate"); serialized only when set, tolerant on
    # load like title_texts.
    music_gaps: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class MontageEntry:
    clip_path: str
    source_start: float  # seconds in the clip (file-relative, 0-based)
    source_end: float
    record_start: float  # seconds in the montage
    record_end: float
    score: float
    transition: float = 0.0  # seconds of dissolve INTO this entry (0 = cut)
    media_start: float = 0.0  # seconds: the file's embedded start timecode (0 if none)
    clip_duration: float = 0.0  # seconds: the source file's real duration (0 if unknown)
    label: str = ""  # one-line vision label of the chosen moment ("" if unseen)
    # J/L cut audio offsets (blueprint 2.3): the ORIGINAL-sound edit point,
    # decoupled from the picture cut at a chosen scene transition.
    # ``audio_lead`` > 0 (J-cut) = this shot's own audio starts that many
    # seconds BEFORE its picture cut (anticipation); ``audio_lag`` > 0
    # (L-cut) = this shot's own audio rings that many seconds PAST its
    # picture-out (continuity). Both overlap the neighbour's picture and
    # need matching source handles. Serialized only when set (the ``file``
    # pattern), so plans without J/L cuts keep their exact bytes.
    audio_lead: float = 0.0
    audio_lag: float = 0.0
    # Self-critique support (blueprint 4.1): the chosen moment's sifted
    # ``peak_time`` (:attr:`monteur.sift.Moment.peak_time`), in this clip's
    # own file coordinates — the same axis as ``source_start``. -1.0 means
    # "no peak signal / not a peak-cast slot". :func:`monteur.critique.critique`
    # maps it into record time (``record_start + (peak_source - source_start)``)
    # to score peak-on-beat coincidence WITHOUT a video re-decode. Held in
    # memory only: it is EXCLUDED from :func:`plan_to_dict` so the default
    # plan serialization stays byte-identical (a round-tripped plan simply
    # loses the signal — the refine loop critiques fresh in-memory plans).
    peak_source: float = -1.0
    # Self-critique support (blueprint 4.1/3.2): the cast moment's shot-size
    # class ("wide"/"medium"/"close", "" = unknown), so critique can score
    # shot-grammar violations (equal-size neighbours) from the plan alone.
    # In-memory only, EXCLUDED from :func:`plan_to_dict` (byte parity).
    shot_size: str = ""
    # Auto-reframe 9:16 (blueprint wave 3, spatial eye-trace): the cast
    # moment's attention point ``(x, y)`` in 0..1 source coordinates, averaged
    # from the moment's entry/exit focus. The export/Resolve renderers shift
    # the crop window so this point stays centred when 16:9 footage is cropped
    # to a vertical/cine canvas (:mod:`monteur.reframe`), instead of a dumb
    # centre-crop. ``None`` = no focus signal → the exact centre crop as
    # before. In-memory only, EXCLUDED from :func:`plan_to_dict` (byte parity):
    # a round-tripped plan simply loses the signal and center-crops, and the
    # reframe is a pure function of the plan geometry recomputed at render time.
    reframe_focus: tuple[float, float] | None = None


@dataclass
class SfxCue:
    """One planned sound-design cue — Monteur plans it, the editor drops it in.

    Monteur cannot render audio, so the deliverable is the CUE: when the
    effect goes, what kind it is, what to type into an SFX library (the
    ``query`` pastes straight into Artlist & co.) and why it is there.
    """

    time: float        # seconds in the cut
    duration: float    # suggested length of the effect
    kind: str          # "riser" | "impact" | "whoosh" | "sub-drop" | "ambience"
    query: str         # ready-to-paste SFX search terms ("whoosh transition fast")
    note: str          # one line WHY this cue is here ("act change into climax")
    # A concrete file from the user's sound library (monteur.elements): ""
    # (the default) keeps the cue a search-query marker; a set path makes
    # montage_to_timeline place it as a REAL audio clip on the SFX track.
    file: str = ""
    # Seconds INTO the file where playback starts (blueprint 1.3): a riser
    # longer than its run-up plays its LAST seconds (offset = file length
    # - play), so the build ends at the file's climax instead of losing it
    # to a tail trim; an impact whose run-in would start before record 0
    # skips just enough head that its peak still lands on the hit.
    # Serialized only when set (the ``file`` pattern), so plans without
    # offsets keep their exact bytes and old readers keep loading them.
    source_offset: float = 0.0


@dataclass(frozen=True)
class CastingBias:
    """The Wave-4 casting/ordering adjustments for one plan (blueprint 4.2/4.3).

    A single, small, deterministic bundle the engine folds in as
    TIE-BREAKERS — never over sync, the drop, the rhythm order or
    zero-repeat. Two sources produce one:

    * **Learned preferences** (4.3, :func:`monteur.preferences.casting_bias`):
      the abstract directions a user's corrections established — e.g.
      "close-ups at the climax" becomes a ``shot_size`` entry biasing close
      candidates in the climax phase, "fewer dissolves" sets
      ``fewer_dissolves``.
    * **The refine loop** (4.2, :func:`refine_plan`): when the critique
      reports shot-grammar violations it raises ``grammar_scale`` to push
      the ordering harder toward changing shot sizes.

    A NEUTRAL bias (``shot_size`` empty, ``fewer_dissolves`` false,
    ``grammar_scale`` 1.0) is byte-identical to passing none — the empty
    store / no-refine guarantee. Frozen and hashable so the refine loop can
    key its search on it.
    """

    #: (phase label or "*", shot size, additive weight) — the bonus a
    #: candidate of that shot size earns in that phase.
    shot_size: tuple[tuple[str, str, float], ...] = ()
    #: Suppress the weakest (plain gentle-passage) dissolves — a learned
    #: "fewer dissolves" preference. Scene/daylight dissolves are kept.
    fewer_dissolves: bool = False
    #: Multiplier on the shot-grammar ordering term (refine's grammar knob).
    grammar_scale: float = 1.0

    def is_neutral(self) -> bool:
        """True when the bias changes nothing (byte-parity fast path)."""
        return (
            not self.shot_size
            and not self.fewer_dissolves
            and abs(self.grammar_scale - 1.0) <= _EPS
        )

    def size_bonus(self, phase: str | None, cand_size: str) -> float:
        """Additive casting bonus for ``cand_size`` in ``phase`` (0 when none)."""
        if not cand_size or not self.shot_size:
            return 0.0
        total = 0.0
        for p, size, weight in self.shot_size:
            if size == cand_size and (p == "*" or p == phase):
                total += weight
        return total


# --- grid -------------------------------------------------------------------


def _mmss(seconds: float) -> str:
    """Format a position as M:SS (e.g. 61.0 -> "1:01")."""
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _window_music(music: MusicAnalysis, start: float, length: float) -> MusicAnalysis:
    """A view of ``music`` over ``[start, start + length]`` in montage time.

    Every musical position (beats, downbeats, phrases, drops) is shifted by
    ``-start`` and clipped to the window; sections are cropped to the window,
    shifted, and re-tiled so they still cover ``[0, length]`` exactly. The
    montage grid can then be built as usual — in montage-relative time — while
    only the song's strongest passage is in play.
    """
    end = start + length

    def shift(times: list[float]) -> list[float]:
        return [t - start for t in sorted(times) if start - _EPS <= t <= end + _EPS]

    sections: list[MusicSection] = []
    for s in music.sections:
        lo = max(s.start, start)
        hi = min(s.end, end)
        if hi - lo > _EPS:
            sections.append(MusicSection(lo - start, hi - start, s.energy, s.label))
    if sections:  # guarantee exact tiling of [0, length]
        sections[0].start = 0.0
        sections[-1].end = length
        for prev, nxt in zip(sections, sections[1:]):
            nxt.start = prev.end

    return MusicAnalysis(
        path=music.path,
        duration=length,
        tempo=music.tempo,
        beats=shift(music.beats),
        sections=sections,
        downbeats=shift(music.downbeats),
        phrases=shift(music.phrases),
        drops=shift(music.drops),
    )


def _label_at(sections: list[MusicSection], t: float) -> str:
    """Energy label of the section containing ``t`` ("mid" if uncovered)."""
    for s in sections:
        if s.start - _EPS <= t < s.end - _EPS:
            return s.label
    if sections and t >= sections[-1].end - _EPS:
        return sections[-1].label
    return "mid"


def _section_bounds_at(
    sections: list[MusicSection], t: float, length: float
) -> tuple[float, float]:
    """Span ``(start, end)`` of the section containing ``t``.

    Mirrors :func:`_label_at` exactly (same boundary semantics); an
    uncovered ``t`` — or no sections at all — spans the whole montage.
    """
    for s in sections:
        if s.start - _EPS <= t < s.end - _EPS:
            return s.start, s.end
    if sections and t >= sections[-1].end - _EPS:
        return sections[-1].start, length
    return 0.0, length


def _energy_at(sections: list[MusicSection], t: float) -> float:
    """Energy value of the section containing ``t`` (0.5 if uncovered)."""
    for s in sections:
        if s.start - _EPS <= t < s.end - _EPS:
            return s.energy
    if sections and t >= sections[-1].end - _EPS:
        return sections[-1].energy
    return 0.5


def _sample_energy(sections: list[MusicSection], length: float) -> list[float]:
    """The strip's energy lane: section energy at MUSIC_ENERGY_RATE, smoothed.

    Sample ``i`` is the energy at record time ``i / MUSIC_ENERGY_RATE``,
    lightly smoothed with a 3-tap moving average so the lane renders as a
    curve instead of section steps, rounded to 3 decimals. Empty when the
    song has no sections or the montage has no length.
    """
    if not sections or length <= _EPS:
        return []
    n = int(math.floor(length * MUSIC_ENERGY_RATE)) + 1
    raw = [_energy_at(sections, i / MUSIC_ENERGY_RATE) for i in range(n)]
    smoothed: list[float] = []
    for i in range(n):
        lo, hi = max(0, i - 1), min(n, i + 2)
        smoothed.append(round(sum(raw[lo:hi]) / (hi - lo), 3))
    return smoothed


def _nth_beat_after(beats: list[float], t: float, n: int) -> float | None:
    """The n-th beat strictly after ``t``, or None if beats run out."""
    i = bisect.bisect_right(beats, t + _EPS)
    j = i + n - 1
    return beats[j] if j < len(beats) else None


def _build_grid(
    music: MusicAnalysis,
    length: float,
    steps: dict[str, int] | None = None,
) -> tuple[list[float], list[str]]:
    """Cut times ``[0, ..., length]`` walked on the beat grid.

    ``steps`` overrides :data:`BEATS_PER_CUT` (used by the pace control).
    """
    lookup = steps or BEATS_PER_CUT
    notes: list[str] = []
    cuts = [0.0]
    beats = sorted(b for b in music.beats if b > _EPS or abs(b) <= _EPS)
    if not beats:
        notes.append(
            f"no beats detected; falling back to a fixed {FALLBACK_INTERVAL:g}s grid"
        )
        t = FALLBACK_INTERVAL
        while t < length - _EPS:
            cuts.append(t)
            t += FALLBACK_INTERVAL
    else:
        # Rhythm (gentle, for "auto"): a longer hold opens each music
        # section (capped so it never eats the section) and every fourth
        # cut of a section takes a 2x-base breath — deliberate variation
        # instead of one metronomic interval, still walked on the beats.
        cur = 0.0
        run_key: tuple[str, float] | None = None
        run_i = 0
        hold_cap = 1
        while True:
            label = _label_at(music.sections, cur)
            base = lookup.get(label, 2)
            sec_start, sec_end = _section_bounds_at(music.sections, cur, length)
            beat_s = 60.0 / music.tempo if music.tempo > 0 else _PSEUDO_BEAT
            if (label, sec_start) != run_key:
                run_key = (label, sec_start)
                run_i = 0
                lo = bisect.bisect_right(beats, cur + _EPS)
                hi = bisect.bisect_right(beats, min(sec_end, length) + _EPS)
                hold_cap = _cap_units(
                    _opening_hold(base, hi - lo), base, beat_s, _MAX_HOLD_SECONDS
                )
            step = max(1, round(_AUTO_PATTERN[run_i % len(_AUTO_PATTERN)] * base))
            step = _cap_units(step, base, beat_s, _MAX_CUT_SECONDS)
            if run_i == 0:
                step = min(step, max(1, hold_cap))  # section-opening hold, capped
            nxt = _nth_beat_after(beats, cur, step)
            # Anti-strobe: double the beat step until the interval is sane.
            while nxt is not None and nxt - cur < MIN_CUT_INTERVAL:
                step *= 2
                nxt = _nth_beat_after(beats, cur, step)
            if nxt is None or nxt >= length - _EPS:
                break  # beats ran out or past the end: close at `length`
            cuts.append(nxt)
            cur = nxt
            run_i += 1
        notes.append(
            "rhythm: a hold opens each music section, a breath every "
            f"{len(_AUTO_PATTERN)}th cut"
        )
    cuts.append(length)
    return cuts, notes


def _nearest(points: list[float], t: float) -> float:
    """Nearest value in a sorted, non-empty list (ties go to the earlier one)."""
    i = bisect.bisect_left(points, t)
    if i <= 0:
        return points[0]
    if i >= len(points):
        return points[-1]
    before, after = points[i - 1], points[i]
    return before if t - before <= after - t else after


def _snap_ending_length(music: MusicAnalysis, length: float) -> tuple[float | None, str]:
    """Musical boundary to end a truncated montage on, or (None, "").

    Looks for the boundary nearest to ``length`` — phrase starts first,
    falling back to downbeats, then beats — but only within
    ±``_END_SNAP_TOLERANCE`` (12%) of the requested length; equidistant
    candidates prefer the shorter montage. Returns (None, "") when no
    boundary qualifies or the nearest one IS the requested length (no
    change needed). The returned time never exceeds the song duration.
    """
    tolerance = _END_SNAP_TOLERANCE * length
    for cand, kind in (
        (music.phrases, "phrase"),
        (music.downbeats, "downbeat"),
        (music.beats, "beat"),
    ):
        pts = sorted(p for p in cand if _EPS < p <= music.duration + _EPS)
        if not pts:
            continue
        i = bisect.bisect_left(pts, length)
        neighbours = ([pts[i - 1]] if i > 0 else []) + ([pts[i]] if i < len(pts) else [])
        best: float | None = None
        for p in neighbours:  # shorter first: a tie keeps the shorter cut
            d = abs(p - length)
            if d <= tolerance + _EPS and (best is None or d < abs(best - length) - _EPS):
                best = p
        if best is not None:
            if abs(best - length) <= _EPS:
                return None, ""  # already on a boundary
            return best, kind
    return None, ""


def _phase_steps(style: MontageStyle) -> list[int]:
    """Beats-per-cut for every arc entry.

    A run of consecutive arc entries with the same label ramps linearly from
    that label's own step to the FOLLOWING phase's step — "trailer" uses this
    to accelerate through its split build. (The rhythm accelerando,
    :func:`_style_rhythm_specs`, refines this further: build cut lengths ramp
    per cut, from the previous phase's base to the next one's.)
    """
    labels = [lab for _, lab in style.arc]
    steps: list[int] = []
    i = 0
    while i < len(labels):
        j = i
        while j + 1 < len(labels) and labels[j + 1] == labels[i]:
            j += 1
        own = style.beats_per_cut.get(labels[i], 2)
        if j > i:
            nxt = style.beats_per_cut.get(labels[j + 1], own) if j + 1 < len(labels) else own
            span = j - i
            for r in range(span + 1):
                steps.append(max(1, round(own + (nxt - own) * r / span)))
        else:
            steps.append(own)
        i = j + 1
    return steps


# --- rhythm (the anti-monotony canon) ----------------------------------------
#
# Within a phase, cut lengths follow a deliberate texture instead of one
# constant interval: the montage opens on an establishing hold, the build
# accelerates from the opening's base toward the climax's, the drop gets a
# held shot (sharpened by a one-beat stutter burst before it when the build
# is fast enough), the climax cycles a per-style pattern re-anchored on
# phrase starts, and the outro decelerates into its longest final shot.
# Everything is quantized to whole grid units (beats, downbeats or
# pseudo-beats), fully deterministic, and never cuts faster than the
# phase's base — so `pace` keeps meaning "seconds per shot in the fastest
# phase" and every cut still lands on the musical grid.


# Absolute ceilings in SECONDS. Beat-relative rhythm explodes on slow
# paces: at pace 4s a trailer's opening base is already ~8 beats, and a
# 2x establishing hold became a 16-second opener in the field ("build
# ramps 32->8 beats"). Holds and cuts stay proportional on normal paces
# and hit these walls on extreme ones — never below the phase's base,
# so a deliberately slow pace still wins.
_MAX_HOLD_SECONDS = 6.0
_MAX_CUT_SECONDS = 8.0
# A no_opening_hold style's FIRST cut (the hook) never exceeds this many
# seconds, even when a slow pace inflates the phase base past it — the one
# place an absolute ceiling is allowed to undercut the base: a short's
# hook that arrives after 4 seconds is no hook at all.
_MAX_HOOK_SECONDS = 2.0


def _cap_units(value: int, base: int, unit_s: float, cap_s: float) -> int:
    """Clamp a unit count to an absolute seconds ceiling (never below base)."""
    if unit_s <= 0:
        return value
    return min(value, max(base, int(cap_s / unit_s)))


def _opening_hold(base: int, n_units: int) -> int:
    """Units for the establishing hold: ~2x the base, capped by the phase.

    The cap (half the phase's units, never below the base) keeps the hold
    from eating the whole phase; when the phase is too short the hold
    degrades to the plain base, i.e. no hold. Callers additionally cap by
    :data:`_MAX_HOLD_SECONDS` via :func:`_cap_units`.
    """
    return min(2 * base, max(base, n_units // 2))


def _drop_hold(base: int) -> int:
    """Units for the drop hold: aim 3x the base, clamped to 2..4 units.

    Never below the phase's own base, so the drop slot is never the
    shortest cut even when a slow pace inflates the base past the clamp.
    """
    return max(base, min(4, 3 * base))


def _phase_cut_lengths(
    n_units: int,
    base: int,
    pattern: tuple[float, ...] = (),
    *,
    first_hold: int = 0,
    recovery: int = 0,
    ramp_from: float | None = None,
    ramp_to: float | None = None,
    stutter: int = 0,
    decel: bool = False,
    phrase_units: tuple[int, ...] = (),
    max_len: int = 0,
    cool_pattern: tuple[float, ...] = (),
    group_units: int = 0,
) -> list[int]:
    """Cut lengths (whole grid units) for one phase — the rhythm kernel.

    ``n_units`` is how many grid units (beats / downbeats / pseudo-beats)
    the phase spans, ``base`` the phase's beats-per-cut in those units.
    Deterministic: the same inputs always yield the same list. Every
    length is a whole unit count >= 1; a phase's final slot is whatever
    remains to the phase boundary, so the sum may cover ``n_units``
    loosely (exactly, for ``decel``).

    * ``first_hold`` > 0 makes the FIRST cut exactly that long (clamped to
      the phase) — the establishing hold or the drop hold. ``recovery``
      > 0 (blueprint 1.6) then emits ONE recovery cut of that many units
      right after the hold — the post-peak breath before the pattern
      re-accelerates.
    * ``ramp_from``/``ramp_to`` (both set) generate the accelerando: a
      monotone run from one base toward the other, quantized per cut.
      ``stutter`` then reserves that many trailing one-unit cuts (the
      burst into the drop).
    * ``decel`` generates the outro: non-decreasing lengths summing to
      exactly ``n_units`` whose final entry — the last shot — is the
      longest, up to 2x the base.
    * ``pattern`` (otherwise) cycles multipliers on ``base``; a phrase
      boundary (``phrase_units``: cumulative unit offsets) re-anchors the
      cycle so the pattern restarts with the music's own phrasing.
      ``cool_pattern`` + ``group_units`` (blueprint 1.6) alternate the
      cycle between "hot" (``pattern``) and "cool" (``cool_pattern``)
      groups of ``group_units`` units — long climaxes breathe in phrase
      groups instead of looping one flat cycle; a group handover restarts
      the incoming cycle at its head (phrase re-anchors still win).
    * ``max_len`` > 0 clamps every emitted length (and the decel's final
      hold target) to that many units — the absolute-seconds ceiling
      (:data:`_MAX_CUT_SECONDS`) translated by the caller.
    """
    if n_units <= 0:
        return []
    base = max(1, base)
    lengths: list[int] = []
    consumed = 0
    if first_hold > 0:
        hold = max(1, min(first_hold, n_units))
        lengths.append(hold)
        consumed = hold
        if recovery > 0 and consumed < n_units:
            # Post-peak recovery (blueprint 1.6): one longer breath right
            # after the drop hold, before the pattern re-accelerates.
            breath = max(1, min(recovery, n_units - consumed))
            lengths.append(breath)
            consumed += breath
    if decel:
        remaining = n_units - consumed
        if remaining <= base:
            return _clamp_lengths(lengths, max_len)  # remainder IS the final hold
        last = min(2 * base, remaining)
        if max_len > 0:
            last = max(1, min(last, max_len))
        rest = remaining - last
        body = [base] * (rest // base)
        extra = rest % base
        if extra:
            if body:
                body[-1] += extra
            else:
                body = [extra]
        # The final hold is never emitted as a cut: the remainder slot up
        # to the phase boundary IS the montage's longest, final shot.
        return _clamp_lengths(lengths + body, max_len)
    if ramp_from is not None and ramp_to is not None:
        span = n_units - consumed - max(0, stutter)
        done = 0
        prev: int | None = None
        while done < span:
            f = done / span
            step = max(1, round(ramp_from + (ramp_to - ramp_from) * f))
            if prev is not None and ramp_from >= ramp_to:
                step = min(step, prev)  # accelerando never speeds back up
            lengths.append(step)
            prev = step
            done += step
            consumed += step
        lengths.extend([1] * min(max(0, stutter), n_units - consumed))
        return _clamp_lengths(lengths, max_len)
    pat = tuple(pattern) or (1.0,)
    cool = tuple(cool_pattern)

    def _cycle_at(units_done: int) -> tuple[float, ...]:
        """Hot or cool cycle for the group containing ``units_done``."""
        if cool and group_units > 0 and (units_done // group_units) % 2 == 1:
            return cool
        return pat

    i = 1 % len(pat) if first_hold > 0 else 0
    while consumed < n_units:
        cycle = _cycle_at(consumed)
        length = max(1, round(cycle[i % len(cycle)] * base))
        prev_consumed = consumed
        lengths.append(length)
        consumed += length
        if any(prev_consumed < pu <= consumed for pu in phrase_units):
            i = 0  # a phrase boundary re-anchors the cycle
        elif _cycle_at(consumed) is not cycle:
            i = 0  # a hot/cool group handover restarts the incoming cycle
        else:
            i += 1
    return _clamp_lengths(lengths, max_len)


def _clamp_lengths(lengths: list[int], max_len: int) -> list[int]:
    """Apply the absolute ceiling to every emitted cut length (0 = off)."""
    if max_len <= 0:
        return lengths
    return [max(1, min(x, max_len)) for x in lengths]


def _style_rhythm_specs(
    style: MontageStyle,
    steps: list[int],
    factors: list[int],
    n_units_list: list[int],
    phrase_units_list: list[tuple[int, ...]],
    pinned: set[int],
    unit_seconds_list: list[float] | None = None,
) -> tuple[list[dict], str]:
    """Per-arc-entry kwargs for :func:`_phase_cut_lengths`, plus a note.

    ``steps`` are the per-entry beat steps (:func:`_phase_steps`),
    ``factors`` the beats-per-unit of each entry's grid (1 = beats,
    ``_BEATS_PER_BAR`` = downbeats), ``n_units_list`` the units each phase
    spans and ``pinned`` the arc indices whose start was pinned to a drop.
    Encodes the canon: establishing hold on the montage's first phase —
    unless ``style.no_opening_hold`` (the shorts anti-canon: the first cut
    stays at the base, capped at :data:`_MAX_HOOK_SECONDS` absolute) —
    build runs ramping from the previous phase's base to the following
    phase's (with a stutter burst into a pinned climax when the ramp ends
    fast enough), drop hold on a pinned climax, per-style pattern texture,
    decelerando on a final outro. The note summarizes the rhythm in beats.

    ``unit_seconds_list`` (seconds per grid unit, per entry) activates the
    absolute ceilings: no hold beyond :data:`_MAX_HOLD_SECONDS`, no cut
    beyond :data:`_MAX_CUT_SECONDS` — beat-relative rhythm must not
    explode on slow paces (the 16-second-opener field bug).
    """
    labels = [lab for _, lab in style.arc]

    def unit_s(k: int) -> float:
        if not unit_seconds_list or k >= len(unit_seconds_list):
            return 0.0
        return float(unit_seconds_list[k])

    specs: list[dict] = []
    bits: list[str] = []
    stutter_used = False
    ramp_span: tuple[int, int] | None = None
    i = 0
    while i < len(labels):
        if labels[i] == "build":
            j = i
            while j + 1 < len(labels) and labels[j + 1] == "build":
                j += 1
            rf = float(steps[i - 1] if i > 0 else steps[i])
            rt = float(steps[j + 1] if j + 1 < len(labels) else steps[j])
            total = sum(n_units_list[i : j + 1]) or 1
            done = 0
            for k in range(i, j + 1):
                f0 = done / total
                done += n_units_list[k]
                f1 = done / total
                k_base = max(1, round(steps[k] / factors[k]))
                spec = {
                    "n_units": n_units_list[k],
                    "base": k_base,
                    "ramp_from": (rf + (rt - rf) * f0) / factors[k],
                    "ramp_to": (rf + (rt - rf) * f1) / factors[k],
                }
                if unit_s(k) > 0:
                    cap = _cap_units(10**6, k_base, unit_s(k), _MAX_CUT_SECONDS)
                    spec["max_len"] = cap
                    spec["ramp_from"] = min(spec["ramp_from"], float(cap))
                    spec["ramp_to"] = min(spec["ramp_to"], float(cap))
                if (
                    k == j
                    and j + 1 < len(labels)
                    and labels[j + 1] == "climax"
                    and (j + 1) in pinned
                    and rt <= 2
                    and n_units_list[k] > _STUTTER_CUTS + round(rf / factors[k])
                ):
                    spec["stutter"] = _STUTTER_CUTS
                    stutter_used = True
                specs.append(spec)
            if rf != rt:
                ramp_span = (int(rf), int(rt))
            i = j + 1
            continue
        label = labels[i]
        base = max(1, round(steps[i] / factors[i]))
        spec: dict = {
            "n_units": n_units_list[i],
            "base": base,
            "pattern": tuple(style.rhythm.get(label, ())),
        }
        if unit_s(i) > 0:
            spec["max_len"] = _cap_units(10**6, base, unit_s(i), _MAX_CUT_SECONDS)
        if len(spec["pattern"]) > 1 and phrase_units_list[i]:
            spec["phrase_units"] = phrase_units_list[i]
        if label == "climax" and i in pinned:
            spec["first_hold"] = _cap_units(
                _drop_hold(base), base, unit_s(i), _MAX_HOLD_SECONDS
            )
            bits.append(f"drop hold {spec['first_hold'] * factors[i]} beats")
            # Breath in the canon (blueprint 1.6): after the drop hold, ONE
            # recovery cut at ~2x the climax base before re-accelerating —
            # the valley that makes the next peak read as a peak.
            spec["recovery"] = _cap_units(
                _RECOVERY_MULT * base, base, unit_s(i), _MAX_CUT_SECONDS
            )
            bits.append(
                f"a {spec['recovery'] * factors[i]}-beat recovery breath after it"
            )
        if label == "climax" and n_units_list[i] >= 2 * _CLIMAX_GROUP_UNITS:
            # Breath in the canon (blueprint 1.6): a LONG climax alternates
            # hot and cool 8-unit phrase groups instead of one flat cycle.
            hot = spec["pattern"] or (1.0,)
            spec["cool_pattern"] = tuple(_COOL_PATTERN_MULT * m for m in hot)
            spec["group_units"] = _CLIMAX_GROUP_UNITS
            bits.append(
                f"hot/cool {_CLIMAX_GROUP_UNITS * factors[i]}-beat phrase "
                "groups in the climax"
            )
        if label == "outro" and i == len(labels) - 1:
            spec["decel"] = True
        specs.append(spec)
        i += 1
    if specs and "first_hold" not in specs[0]:
        if style.no_opening_hold:
            # Anti-canon (social shorts): NO establishing hold — the first
            # cut stays at the phase's base, capped at _MAX_HOOK_SECONDS
            # absolute. This cap deliberately undercuts the base on slow
            # paces: the hook must land inside the first ~2 seconds.
            first = specs[0]["base"]
            if unit_s(0) > 0:
                first = max(1, min(first, int(_MAX_HOOK_SECONDS / unit_s(0))))
            specs[0]["first_hold"] = first
            bits.insert(0, "no opening hold (the hook cuts at its base)")
        else:
            hold = _opening_hold(specs[0]["base"], specs[0]["n_units"])
            hold = _cap_units(hold, specs[0]["base"], unit_s(0), _MAX_HOLD_SECONDS)
            if hold > specs[0]["base"]:
                specs[0]["first_hold"] = hold
                bits.insert(0, f"opening hold {hold * factors[0]} beats")
    if ramp_span is not None:
        pos = 1 if bits and bits[0].startswith("opening hold") else 0
        bits.insert(pos, f"build ramps {ramp_span[0]}->{ramp_span[1]} beats")
    if stutter_used:
        bits.append(f"{_STUTTER_CUTS}-cut stutter into the drop")
    if any(spec.get("decel") for spec in specs):
        bits.append("outro decays to the longest shot")
    if any(len(spec.get("pattern", ())) > 1 for spec in specs):
        bits.append("pattern texture in between")
    note = ("rhythm: " + ", ".join(bits)) if bits else ""
    return specs, note


def _enforce_phase_floor(
    bounds: list[float], climax_i: int, length: float
) -> bool:
    """Arc-squeeze floor (blueprint 1.5): minimum shares around a drop pin.

    The pin scales the boundaries on each side of the climax
    proportionally; an extreme (but in-range) drop can crush a side's
    phases to slivers of story. Per SIDE of the pinned climax start
    (before: phases 0..climax_i tiling ``[0, drop]``; after: the
    climax+outro tiling ``[drop, length]``), every phase is raised to at
    least :data:`_ARC_MIN_PHASE_SHARE` x ``length``, the surplus taken
    from the side's above-floor phases proportionally to their surplus —
    the side's total (and the pin itself) never moves. A side whose whole
    span cannot afford every phase the floor keeps the proportional
    squeeze (the pin wins). Mutates ``bounds`` in place; returns whether
    anything changed.
    """
    floor = _ARC_MIN_PHASE_SHARE * length
    changed = False
    for lo_i, hi_i in ((0, climax_i), (climax_i, len(bounds) - 1)):
        n = hi_i - lo_i
        if n < 2:
            continue  # one phase fills the side: nothing to redistribute
        span = bounds[hi_i] - bounds[lo_i]
        if span < n * floor - _EPS:
            continue  # the side cannot afford the floor: the pin wins
        lens = [bounds[i + 1] - bounds[i] for i in range(lo_i, hi_i)]
        if all(ln >= floor - _EPS for ln in lens):
            continue
        raised = [max(ln, floor) for ln in lens]
        surplus = sum(r - floor for r in raised)
        scale = (span - n * floor) / surplus if surplus > _EPS else 0.0
        acc = bounds[lo_i]
        for k, r in enumerate(raised[:-1]):
            acc += floor + (r - floor) * scale
            bounds[lo_i + 1 + k] = acc
        changed = True
    return changed


def _build_style_grid(
    music: MusicAnalysis, length: float, style: MontageStyle
) -> tuple[list[float], list[tuple[float, float, str]], list[str]]:
    """Cut grid and phase spans ``(start, end, label)`` for a named style.

    Phase boundaries are the arc shares mapped onto ``length``, snapped to
    the nearest phrase start (falling back to downbeats, then beats). If the
    song has drops and the arc has a climax, the climax start is pinned to
    the first drop and the neighbouring boundaries are scaled proportionally
    (limits: first drop only, and only when it lies within 5%..95% of the
    montage — otherwise a note explains the skip). Slow phases
    (>= ``_SLOW_PHASE_STEP`` beats per cut) cut on downbeats, fast phases
    walk the beat grid — each with its phase's rhythm sequence
    (:func:`_phase_cut_lengths` via :func:`_style_rhythm_specs`: opening
    hold, build accelerando, drop hold + stutter, pattern texture, outro
    decelerando) on the phase's base step; with neither beats nor
    downbeats the fixed 2 s fallback grid is used, exactly as in "auto".
    """
    notes: list[str] = []
    labels = [lab for _, lab in style.arc]
    total_share = sum(share for share, _ in style.arc) or 1.0
    bounds = [0.0]
    acc = 0.0
    for share, _ in style.arc:
        acc += share
        bounds.append(length * acc / total_share)
    bounds[-1] = length

    # Drop = climax: pin the climax start to the BEST in-range drop
    # (blueprint 1.5 — the heaviest by musical weight, not merely the
    # first; a single-drop song behaves exactly as before).
    pinned: set[int] = set()
    drops = sorted(d for d in music.drops)
    if drops and "climax" in labels:
        climax_i = labels.index("climax")
        orig = bounds[climax_i]
        in_range = [
            d
            for d in drops
            if _DROP_ALIGN_MARGIN * length <= d <= (1 - _DROP_ALIGN_MARGIN) * length
        ]
        if not in_range:
            notes.append(
                f"drop at {drops[0]:.1f}s outside 5-95% of the montage; climax not aligned"
            )
        elif climax_i == 0 or orig <= _EPS or orig >= length - _EPS:
            notes.append("climax phase starts at the montage edge; drop alignment skipped")
        else:
            drop = best_drop(music, in_range)
            for i in range(1, climax_i):
                bounds[i] *= drop / orig
            bounds[climax_i] = drop
            for i in range(climax_i + 1, len(bounds) - 1):
                bounds[i] = length - (length - bounds[i]) * (length - drop) / (length - orig)
            pinned.add(climax_i)
            note = f"climax aligned to drop at {drop:.1f}s"
            if len(in_range) > 1:
                note += f" (the strongest of {len(in_range)})"
            notes.append(note)
            # Arc-squeeze floor (blueprint 1.5): an extreme pin must not
            # crush the neighbouring phases into slivers of story.
            if _enforce_phase_floor(bounds, climax_i, length):
                notes.append(
                    "drop pin: squeezed phases keep at least "
                    f"{_ARC_MIN_PHASE_SHARE:.0%} of the montage"
                )

    # Snap the remaining interior boundaries to musical positions:
    # phrases, else downbeats, else beats.
    snap_points: list[float] = []
    snapped_to = ""
    for cand, kind in (
        (music.phrases, "phrase starts"),
        (music.downbeats, "downbeats"),
        (music.beats, "beats"),
    ):
        pts = sorted(p for p in cand if _EPS < p < length - _EPS)
        if pts:
            snap_points, snapped_to = pts, kind
            break
    snapped = 0
    for i in range(1, len(bounds) - 1):
        if i in pinned or not snap_points:
            continue
        bounds[i] = _nearest(snap_points, bounds[i])
        snapped += 1
    for i in range(1, len(bounds)):  # keep boundaries monotonic
        bounds[i] = min(max(bounds[i], bounds[i - 1]), length)

    phases = [(bounds[i], bounds[i + 1], labels[i]) for i in range(len(labels))]

    beats = sorted(b for b in music.beats if b > -_EPS)
    downs = sorted(d for d in music.downbeats if d > -_EPS)
    pulse = beats or downs  # graceful: no beats -> walk downbeats instead
    cuts = [0.0]
    downbeat_cuts = 0
    if not pulse:
        notes.append(
            f"no beats detected; falling back to a fixed {FALLBACK_INTERVAL:g}s grid"
        )
        t = FALLBACK_INTERVAL
        while t < length - _EPS:
            cuts.append(t)
            t += FALLBACK_INTERVAL
    else:
        # Rhythm: each phase gets a deliberate cut-length sequence (whole
        # grid units) instead of one constant interval — establishing hold,
        # build accelerando, drop hold + stutter, pattern texture, outro
        # decelerando. Slow phases (>= _SLOW_PHASE_STEP) keep walking
        # downbeats, fast phases the beat grid, exactly as before.
        steps = _phase_steps(style)
        slow_flags = [s >= _SLOW_PHASE_STEP and bool(downs) for s in steps]
        unit_lists = [downs if slow else pulse for slow in slow_flags]
        factors = [_BEATS_PER_BAR if slow else 1 for slow in slow_flags]
        phrase_pts = sorted(p for p in music.phrases)
        n_units_list: list[int] = []
        phrase_units_list: list[tuple[int, ...]] = []
        for (p_start, p_end, _label), units in zip(phases, unit_lists):
            lo = bisect.bisect_right(units, p_start + _EPS)
            hi = bisect.bisect_left(units, p_end - _EPS)
            # Interior grid points plus one for the closing stretch to the
            # boundary — the phase's unit count even when the boundary
            # itself sits off the grid.
            n_units_list.append(
                max(0, hi - lo) + (1 if p_end - p_start > _EPS else 0)
            )
            in_phase = units[lo:hi]
            phrase_units_list.append(
                tuple(
                    bisect.bisect_left(in_phase, p - _EPS) + 1
                    for p in phrase_pts
                    if p_start + _EPS < p < p_end - _EPS
                )
            )
        beat_s = 60.0 / music.tempo if music.tempo > 0 else _PSEUDO_BEAT
        specs, rhythm_note = _style_rhythm_specs(
            style, steps, factors, n_units_list, phrase_units_list, pinned,
            unit_seconds_list=[f * beat_s for f in factors],
        )
        for (p_start, p_end, _label), units, slow, spec in zip(
            phases, unit_lists, slow_flags, specs
        ):
            cur = cuts[-1]
            for units_ahead in _phase_cut_lengths(**spec):
                n = units_ahead
                nxt = _nth_beat_after(units, cur, n)
                while nxt is not None and nxt - cur < MIN_CUT_INTERVAL:
                    n += 1  # anti-strobe: skip to a later grid point
                    nxt = _nth_beat_after(units, cur, n)
                if nxt is None or nxt >= p_end - _EPS:
                    break
                cuts.append(nxt)
                if slow:
                    downbeat_cuts += 1
                cur = nxt
            if p_end < length - _EPS and p_end > cuts[-1] + _EPS:
                cuts.append(p_end)  # the phase boundary itself is a cut
        if rhythm_note:
            notes.append(rhythm_note)
    cuts.append(length)

    if snapped and snapped_to:
        notes.append(f"{snapped} phase boundaries snapped to {snapped_to}")
    if downbeat_cuts:
        notes.append(f"{downbeat_cuts} cuts on downbeats")
    return cuts, phases, notes


def _secondary_drops(
    music: MusicAnalysis,
    in_range: list[float],
    climax_drop: float,
    beat_s: float,
) -> list[float]:
    """Secondary drops that earn a forced cut (blueprint 2.1).

    ``in_range`` are the in-range drops (already filtered to 5-95% of the
    montage); ``climax_drop`` is the one the climax pinned. Returns the
    subset of the OTHER in-range drops that force a hard cut on themselves,
    sorted by time — the strongest by :func:`monteur.music.drop_weight`,
    gated by :data:`_SECONDARY_DROP_WEIGHT_FRACTION` of the climax drop's
    weight, kept :data:`_SECONDARY_DROP_MIN_BEATS` beats clear of the
    climax and of each other, capped at :data:`_SECONDARY_DROP_MAX`. A
    section-less song weighs every drop 0.0, so nothing qualifies and the
    grid is byte-identical to before this feature.
    """
    climax_weight = drop_weight(music, climax_drop)
    if climax_weight <= _EPS:
        return []
    threshold = _SECONDARY_DROP_WEIGHT_FRACTION * climax_weight
    min_gap = _SECONDARY_DROP_MIN_BEATS * max(beat_s, _EPS)
    # Candidates: OTHER in-range drops that clear the climax and carry
    # enough weight, strongest first (ties earliest, mirroring best_drop).
    cands = sorted(
        (
            d
            for d in in_range
            if abs(d - climax_drop) >= min_gap - _EPS
            and drop_weight(music, d) >= threshold - _EPS
        ),
        key=lambda d: (-drop_weight(music, d), d),
    )
    chosen: list[float] = []
    for d in cands:
        if len(chosen) >= _SECONDARY_DROP_MAX:
            break
        if all(abs(d - c) >= min_gap - _EPS for c in chosen):
            chosen.append(d)
    return sorted(chosen)


def _build_pseudo_grid(
    length: float,
    style: MontageStyle,
    auto_steps: dict[str, int] | None = None,
) -> tuple[list[float], list[tuple[float, float, str]], list[str]]:
    """Cut grid and phase spans for a NO-MUSIC plan (pseudo-beat units).

    With no song there is no beat grid to walk, so each arc phase cuts on
    multiples of the ``_PSEUDO_BEAT`` (0.75 s): the phase's
    ``beats_per_cut`` is the base — slow phases ~3 s, fast phases ~0.75 s —
    and the same rhythm canon as the musical grids applies on top
    (:func:`_phase_cut_lengths`: opening hold, build accelerando, pattern
    texture, outro decelerando; no drops or phrases exist here). Phase
    boundaries are the raw arc shares mapped onto ``length`` (nothing
    musical to snap to). "auto" has no arc; it cuts on a flat "mid" interval
    (2 x _PSEUDO_BEAT = 1.5 s), or on the paced "high" step when
    ``auto_steps`` is given (see :func:`_apply_pace`).
    """
    notes = [f"no music: fixed intervals from a {_PSEUDO_BEAT:g}s pseudo-beat"]
    cuts = [0.0]
    phases: list[tuple[float, float, str]] = []
    if style.arc:
        labels = [lab for _, lab in style.arc]
        total_share = sum(share for share, _ in style.arc) or 1.0
        bounds = [0.0]
        acc = 0.0
        for share, _ in style.arc:
            acc += share
            bounds.append(length * acc / total_share)
        bounds[-1] = length
        phases = [(bounds[i], bounds[i + 1], labels[i]) for i in range(len(labels))]
        # The same rhythm canon as the musical grids, on pseudo-beat units:
        # establishing hold, build accelerando, texture, outro decelerando
        # (no drops or phrases exist without music).
        steps = _phase_steps(style)
        n_units_list = [
            int((p_end - p_start) / _PSEUDO_BEAT + _EPS) for p_start, p_end, _ in phases
        ]
        specs, rhythm_note = _style_rhythm_specs(
            style, steps, [1] * len(steps), n_units_list, [()] * len(steps), set(),
            unit_seconds_list=[_PSEUDO_BEAT] * len(steps),
        )
        for (p_start, p_end, _label), spec in zip(phases, specs):
            cur = cuts[-1]
            for units_ahead in _phase_cut_lengths(**spec):
                t = cur + max(units_ahead * _PSEUDO_BEAT, MIN_CUT_INTERVAL)
                if t >= p_end - _EPS:
                    break
                cuts.append(t)
                cur = t
            if p_end < length - _EPS and p_end > cuts[-1] + _EPS:
                cuts.append(p_end)  # the phase boundary itself is a cut
        if rhythm_note:
            notes.append(rhythm_note)
    else:
        # "auto" cuts on one flat interval: the "mid" default, but the paced
        # "high" step when a pace is set — with a single interval, the pace
        # IS the interval (rounded to whole pseudo-beats).
        step = auto_steps["high"] if auto_steps else BEATS_PER_CUT["mid"]
        interval = max(step * _PSEUDO_BEAT, MIN_CUT_INTERVAL)
        t = interval
        while t < length - _EPS:
            cuts.append(t)
            t += interval
    cuts.append(length)
    return cuts, phases, notes


def _pulse_interval(music: MusicAnalysis) -> float:
    """Seconds per beat: median beat spacing, else 60/tempo, else the pseudo-beat."""
    beats = sorted(b for b in music.beats if b > -_EPS)
    if len(beats) >= 2:
        gaps = sorted(b - a for a, b in zip(beats, beats[1:]) if b - a > _EPS)
        if gaps:
            return gaps[len(gaps) // 2]
    if music.tempo > _EPS:
        return 60.0 / music.tempo
    return _PSEUDO_BEAT


def _apply_pace(
    style: MontageStyle, pace: float, beat: float
) -> tuple[MontageStyle, dict[str, int], str]:
    """Scale a style's cutting speed to ``pace`` seconds per clip.

    ``pace`` is the approximate clip length the FASTEST phase should cut at;
    slower phases keep their proportion to it. Returns the adjusted style,
    the adjusted "auto" step table (for arc-less styles) and a plan note.
    The requested pace is rounded to whole beats (minimum one), so the
    realized interval follows the music, not the literal number.
    """
    desired = max(1, round(pace / beat))
    steps = {k: max(1, round(v * desired)) for k, v in BEATS_PER_CUT.items()}
    if style.beats_per_cut:
        base = min(style.beats_per_cut.values())
        factor = desired / max(1, base)
        style = replace(
            style,
            beats_per_cut={
                k: max(1, round(v * factor)) for k, v in style.beats_per_cut.items()
            },
        )
    note = (
        f"cut pace ~{pace:g}s: fastest cuts every {desired} "
        f"beat{'s' if desired != 1 else ''} (~{desired * beat:.1f}s)"
    )
    return style, steps, note


def _auto_pace_bias(
    reports: list[ClipReport], sections: list[MusicSection]
) -> tuple[int, str]:
    """Auto-pace notches (0..2) plus the note naming the signals.

    With ``pace=None`` the per-phase base is DERIVED, not fixed: each
    signal that fires slows every phase base one notch (base x 2, the
    natural beat-grid step — see the constants block above for the
    thresholds and which styles take part):

    * **content mix** — the share of calm material (motion at/below
      :data:`_MERGE_CALM_MOTION` of the pool's fastest AND highlight
      at/below :data:`_MERGE_CALM_HIGHLIGHT`, weighted by moment
      seconds) reaches :data:`_AUTO_PACE_CALM_SHARE`. A pool with no
      motion data anywhere gives no content signal (calmness is
      unknowable — the same rule the calm merge follows).
    * **music density** — the windowed song's duration-weighted mean
      section energy sits at/below :data:`_AUTO_PACE_LOW_ENERGY`.
      No sections (or no music) = no signal.

    Returns ``(0, "")`` when nothing fires — plans without the signals
    are byte-identical to before the bias existed.
    """
    signals: list[str] = []
    mags: list[float] = []
    for report in reports:
        for m in report.moments:
            mags.append(
                (math.hypot(*m.entry_motion) + math.hypot(*m.exit_motion)) / 2.0
            )
    peak = max(mags, default=0.0)
    if peak > _EPS:
        calm = total = 0.0
        i = 0
        for report in reports:
            for m in report.moments:
                length = max(0.0, m.end - m.start)
                total += length
                if (
                    mags[i] / peak <= _MERGE_CALM_MOTION + _EPS
                    and m.highlight <= _MERGE_CALM_HIGHLIGHT + _EPS
                ):
                    calm += length
                i += 1
        if total > _EPS and calm / total >= _AUTO_PACE_CALM_SHARE - _EPS:
            signals.append("calm footage dominates")
    weighted = sum((s.end - s.start) * s.energy for s in sections)
    span = sum(s.end - s.start for s in sections)
    if span > _EPS and weighted / span <= _AUTO_PACE_LOW_ENERGY + _EPS:
        signals.append("a quiet song")
    if not signals:
        return 0, ""
    notches = len(signals)
    note = (
        f"auto pace: {' and '.join(signals)} — cutting "
        f"{'two notches' if notches == 2 else 'one notch'} slower "
        "(set a pace to override)"
    )
    return notches, note


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping/touching [start, end] intervals (sorted result).

    Used by the repetition guard so moments that overlap WITHIN one clip are
    not double-counted as unique material.
    """
    merged: list[tuple[float, float]] = []
    for start, end in sorted((s, e) for s, e in intervals if e - s > _EPS):
        if merged and start <= merged[-1][1] + _EPS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _unique_material(reports: list[ClipReport]) -> float:
    """Total seconds of deduplicated moment material across all reports.

    Each clip's moment intervals are merged first (moments can overlap within
    a clip), then the merged spans are summed.
    """
    total = 0.0
    for report in reports:
        for start, end in _merge_intervals([(m.start, m.end) for m in report.moments]):
            total += end - start
    return total


def cut_lead_for(fps: float | None, requested: float | None = None) -> float:
    """The ONE fps-aware cut-lead decision (blueprint 1.7, typed leads).

    * ``fps`` known, no explicit request → exactly ONE frame (``1/fps``):
      the editor's "cut a frame or two before the beat", typed in frames
      instead of approximated in seconds.
    * ``fps`` known, explicit ``requested`` seconds → the request
      quantized to WHOLE frames (never below one frame unless the request
      was 0, which stays 0 — "disable" keeps meaning disable).
    * ``fps`` unknown → the request, or the classic
      :data:`_DEFAULT_CUT_LEAD` seconds approximation (~1 frame at 25 fps).

    Every consumer of a cut lead resolves it through this function, so the
    lead means the same thing in the planner, the CLI and the web layer.
    """
    if fps is not None and fps <= 0:
        raise ValueError("fps must be positive")
    if fps is None:
        return _DEFAULT_CUT_LEAD if requested is None else max(0.0, requested)
    frame = 1.0 / fps
    if requested is None:
        return frame
    if requested <= 0:
        return 0.0
    return max(frame, round(requested * fps) * frame)


def plan_pulse(plan: MontagePlan) -> float:
    """Seconds per BEAT from the plan's own persisted downbeat marks.

    The one tempo witness every consumer of a finished plan has — the
    planner's finishing pass, the arrangement's boundary requests,
    :func:`adjust_entry_boundary`'s surgery and the renderers' title
    fades all quantize against these same marks (blueprint 1.7: ONE
    shared helper, no site left behind), so a boundary adjusted after
    planning gets exactly the length the planner would have chosen.
    Returns 0.0 when the plan carries fewer than two marks (no-music
    plans, hand-built plans, songs without downbeats) — quantization is
    then off and the classic fixed values stand byte-for-byte.
    """
    marks = sorted(float(t) for t in getattr(plan, "beat_marks", []) or [])
    gaps = sorted(b - a for a, b in zip(marks, marks[1:]) if b - a > _EPS)
    if not gaps:
        return 0.0
    return gaps[len(gaps) // 2] / _BEATS_PER_BAR


def quantize_finish(
    seconds: float, pulse: float, *, max_s: float | None = None
) -> float:
    """Snap a dip/dissolve/fade DURATION onto the beat grid (blueprint 1.7).

    The shared quantization helper behind every finishing duration: the
    smash-to-black dip length (all three carving sites), the dissolve
    length (:func:`_plan_finishing`, the arrangement's ``after`` requests,
    :func:`adjust_entry_boundary`), the dip remainder floor and the
    renderers' title fade. Because the boundary these finishes hang off is
    already ON the musical grid, a half-beat-multiple duration puts their
    OTHER edge on a beat subdivision too — the dip starts a musical unit
    before the act, the dissolve ramps in over a musical unit.

    ``seconds`` is the craft target; the result is the nearest positive
    multiple of half a ``pulse``. ``max_s`` is a hard craft ceiling (half
    the slot, the 0.5 s dissolve cap): when the nearest multiple exceeds
    it, the largest multiple at/below it wins, and when not even half a
    beat fits, the raw target survives (too short to quantize is too
    short to matter). ``pulse <= 0`` — no tempo witness — returns
    ``seconds`` unchanged, so beatless plans stay byte-identical.
    """
    if pulse <= _EPS or seconds <= _EPS:
        return seconds
    unit = pulse / 2.0
    k = max(1, round(seconds / unit))
    q = k * unit
    if max_s is not None and q > max_s + _EPS:
        k = int((max_s + _EPS) / unit)
        if k < 1:
            return min(seconds, max_s)  # not even half a beat fits
        q = k * unit
    return q


def _dip_seconds(plan: MontagePlan) -> float:
    """The smash-to-black dip length for THIS plan (blueprint 1.7).

    :data:`_DIP_SECONDS` beat-quantized through :func:`quantize_finish`
    against the plan's own pulse, clamped to
    [:data:`_DIP_QUANT_MIN`, :data:`_DIP_QUANT_MAX`]. All three carving
    sites (the style's act changes, the arrangement's ``smash`` requests,
    :func:`adjust_entry_boundary`) read this one value, so a dip carved
    by surgery is indistinguishable from a planned one.
    """
    q = quantize_finish(_DIP_SECONDS, plan_pulse(plan), max_s=_DIP_QUANT_MAX)
    return min(max(q, _DIP_QUANT_MIN), _DIP_QUANT_MAX)


def _dip_min_remainder(plan: MontagePlan) -> float:
    """The floor the carved-down outgoing slot must keep (blueprint 1.7).

    At least the sliver floor (:data:`_DIP_MIN_REMAINDER` ==
    :data:`_MIN_SLOT_SECONDS`), raised to a half-beat when the plan knows
    its tempo — the remainder is a slot like any other and should stay a
    musical duration.
    """
    pulse = plan_pulse(plan)
    if pulse <= _EPS:
        return _DIP_MIN_REMAINDER
    return max(_DIP_MIN_REMAINDER, pulse / 2.0)


def _dissolve_seconds(plan: MontagePlan, entry: "MontageEntry") -> float:
    """The dissolve length INTO ``entry`` (blueprint 1.7, shared by all
    three dissolve sites): the classic ``min(0.5 s, half the slot)``
    craft rule, beat-quantized through :func:`quantize_finish` — the
    craft rule stays the ceiling, the beat grid picks the value under it.
    """
    target = min(_MAX_DISSOLVE, (entry.record_end - entry.record_start) / 2.0)
    return quantize_finish(target, plan_pulse(plan), max_s=target)


def _absorb_slivers(
    cuts: list[float], protected: set[float] | frozenset[float] = frozenset()
) -> list[float]:
    """Remove interior cut boundaries that leave a slot under the floor.

    Blueprint 1.7, sliver elimination: no generated slot may be shorter
    than :data:`_MIN_SLOT_SECONDS` — grid remainders (a beat just before
    the montage end), phase-boundary cuts landing next to a grid cut, and
    drop-forced cut insertion all can produce one. Deterministic
    absorption: a sliver merges into the PRECEDING slot (its left
    boundary is removed); when that boundary is ``protected`` (a pinned
    drop cut, a phase bound the caller wants kept) the sliver merges into
    the FOLLOWING slot instead; when both edges are protected the sliver
    stays — pins beat the floor, honestly. The first and last boundary
    (montage start/end) are always protected.
    """
    if len(cuts) <= 2:
        return list(cuts)
    prot = {round(p, 6) for p in protected}
    prot.add(round(cuts[0], 6))
    prot.add(round(cuts[-1], 6))
    out = list(cuts)
    i = 0
    while i < len(out) - 1:
        if out[i + 1] - out[i] >= _MIN_SLOT_SECONDS - _EPS:
            i += 1
            continue
        if round(out[i], 6) not in prot:
            del out[i]  # the sliver joins the slot before it
            i = max(i - 1, 0)
        elif round(out[i + 1], 6) not in prot:
            del out[i + 1]  # ...or the one after it
        else:
            i += 1  # both edges pinned: the sliver survives, honestly
    return out


def _apply_cut_lead(cuts: list[float], lead: float) -> list[float]:
    """Shift every INTERIOR cut point ``lead`` seconds earlier.

    Editors place cuts 1-2 frames before the beat so the incoming shot is
    already on screen when the beat lands. The first cut stays at 0 and the
    final boundary stays at the montage length; each shifted cut is clamped
    so ordering is preserved and no slot is squeezed below
    ``min(_LEAD_MIN_SLOT, its original length)``.
    """
    if lead <= _EPS or len(cuts) <= 2:
        return list(cuts)
    shifted = [cuts[0]]
    for i in range(1, len(cuts) - 1):
        floor = shifted[-1] + min(_LEAD_MIN_SLOT, cuts[i] - cuts[i - 1])
        shifted.append(min(max(cuts[i] - lead, floor), cuts[i]))
    shifted.append(cuts[-1])
    return shifted


def _undo_lead_on_dissolves(
    entries: list["MontageEntry"],
    shifted_cuts: list[float],
    raw_cuts: list[float],
    protected: int = 0,
) -> int:
    """Dissolve lead 0 (blueprint 1.7): dissolving boundaries lose the lead.

    The cut-ahead lead serves HARD cuts — the incoming shot must already
    be standing when the beat lands. A dissolve is a ramp ACROSS the
    beat; shifting its boundary early just starts the ramp off the grid.
    Because :func:`_plan_finishing` decides the dissolves AFTER the grid
    lead was applied, the clean reorder the blueprint names would have to
    rebuild the whole fill — this documented workaround is equivalent:
    each boundary that ended up dissolving is moved BACK to its unshifted
    grid position (looked up in the pre-lead grid), the outgoing entry
    playing ``lead`` seconds longer and the incoming one starting that
    much later (sources move 1:1, so the incoming peak aim — placed
    against ``record_start + lead`` — still lands its peak exactly on
    the beat). Honest fallbacks: a boundary whose outgoing clip has no
    material left (``clip_duration`` known and exhausted), a boundary
    separated by a black dip, and the first ``protected`` entries (the
    editor's arrangement stays bit-identical) all keep their lead.
    Returns how many boundaries moved.
    """
    if not entries or len(shifted_cuts) != len(raw_cuts):
        return 0
    back = {
        round(s, 6): r
        for s, r in zip(shifted_cuts, raw_cuts)
        if r > s + _EPS
    }
    moved = 0
    for i in range(max(1, protected + 1), len(entries)):
        entry = entries[i]
        if entry.transition <= _EPS:
            continue
        raw = back.get(round(entry.record_start, 6))
        if raw is None:
            continue
        prev = entries[i - 1]
        if abs(prev.record_end - entry.record_start) > _EPS:
            continue  # a dip sits on this boundary: the smash keeps its lead
        delta = raw - entry.record_start
        if delta <= _EPS or entry.record_end - raw < _MIN_SLOT_SECONDS - _EPS:
            continue  # never squeeze the incoming slot under the sliver floor
        if prev.clip_duration > _EPS and prev.source_end + delta > prev.clip_duration + _EPS:
            continue  # the outgoing clip has no material for the extension
        prev.record_end = raw
        prev.source_end += delta
        entry.record_start = raw
        entry.source_start = min(entry.source_start + delta, entry.source_end)
        moved += 1
    return moved


# --- slot filling -------------------------------------------------------------


@dataclass
class _PoolItem:
    clip_path: str
    clip_duration: float
    moment: Moment
    media_start: float = 0.0  # seconds: the file's embedded start timecode
    consumed: float = 0.0  # seconds of the moment already placed
    uses: int = 0
    # Drop-hold slack (peak-on-beat, blueprint 1.1): how far past the
    # moment's end the source may extend when a drop HOLD is aimed at the
    # peak — the end of the enclosing USABLE sift segment, capped by the
    # next moment of the same clip (zero-repeat) and the clip length.
    # 0.0 = unknown (hand-built pools): the aim then stays inside the
    # moment, exactly like every non-drop slot.
    slack_end: float = 0.0
    # Reclaimable head material the peak aim skipped over: [lo, hi) spans
    # BEFORE the consumed cursor that were never on screen. The reuse
    # phase slices them before rewinding anything — the zero-repeat
    # bookkeeping must not burn skipped heads. Always empty while no
    # moment carries a peak, so peak-less pools behave byte-identically.
    gaps: list[list[float]] = field(default_factory=list)

    @property
    def remaining(self) -> float:
        tail = self.moment.end - (self.moment.start + self.consumed)
        return max(0.0, tail) + sum(hi - lo for lo, hi in self.gaps)

    # Vision annotations (see monteur.vision). getattr keeps Moment objects
    # from before the vision fields existed working: the defaults mean "not
    # seen", which disables all semantic casting for that moment.

    @property
    def role(self) -> str:
        return getattr(self.moment, "role", "")

    @property
    def hero(self) -> float:
        return getattr(self.moment, "hero", 0.0)

    @property
    def group(self) -> str:
        return getattr(self.moment, "group", "")

    @property
    def label(self) -> str:
        return getattr(self.moment, "label", "")

    @property
    def daylight(self) -> str:
        return getattr(self.moment, "daylight", "")

    # Spatial annotations (see monteur.spatial, blueprint wave 3). getattr
    # keeps pre-wave-3 Moment objects working: the defaults ("" / None)
    # mean "not analysed", which disables eye-trace, shot grammar and
    # rhyme scoring for that moment (byte-identical to before).

    @property
    def shot_size(self) -> str:
        return getattr(self.moment, "shot_size", "")

    @property
    def entry_focus(self) -> tuple[float, float] | None:
        return getattr(self.moment, "entry_focus", None)

    @property
    def exit_focus(self) -> tuple[float, float] | None:
        return getattr(self.moment, "exit_focus", None)


def _focus_distance(
    a: tuple[float, float] | None, b: tuple[float, float] | None
) -> float | None:
    """On-screen distance between two attention points, or None if unknown.

    Both points are (x, y) in 0..1 frame coordinates; the result is a plain
    Euclidean distance in 0.._EYE_TRACE_DIAG. None when either side is
    missing (a flat frame or a pre-wave-3 moment), so eye-trace scoring
    stays neutral instead of guessing.
    """
    if a is None or b is None:
        return None
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _shot_grammar_step(prev_size: str, cand_size: str, in_climax: bool) -> float:
    """Grammar bonus/penalty for casting ``cand_size`` after ``prev_size``.

    Establish -> develop -> pay off (wide -> medium -> close) earns
    :data:`_SHOT_GRAMMAR_WEIGHT`; two equally-sized neighbours pay
    :data:`_SHOT_GRAMMAR_EQUAL_PENALTY` (keep changing scale) — except a
    deliberate close->close intensification inside the climax, which is
    free. Any other pair (a bigger jump, or a step back to a wider shot,
    a legitimate reset) is neutral. Zero when either size is unknown.
    """
    if not prev_size or not cand_size:
        return 0.0
    if prev_size == cand_size:
        if cand_size == "close" and in_climax:
            return 0.0  # deliberate close->close intensification at the peak
        return -_SHOT_GRAMMAR_EQUAL_PENALTY
    if _SHOT_ORDER.get(cand_size, -1) == _SHOT_ORDER.get(prev_size, -1) + 1:
        return _SHOT_GRAMMAR_WEIGHT  # one step along the establish->pay-off arc
    return 0.0


def _visual_kinship(a: "_PoolItem", b: "_PoolItem") -> float:
    """How visually kindred two cast moments are, 0..1 (for a rhyme).

    Leans on the new spatial signal (shot size + attention point) with
    daylight and motion energy as light support. Returns 0 when neither
    moment carries a spatial signal, so a rhyme is only ever drawn between
    moments the wave-3 pass actually saw — footage without it never rhymes
    and casts byte-identically.
    """
    if not (a.shot_size or a.entry_focus or a.exit_focus):
        return 0.0
    if not (b.shot_size or b.entry_focus or b.exit_focus):
        return 0.0
    score = 0.0
    weight = 0.0
    if a.shot_size and b.shot_size:
        score += 0.5 * (1.0 if a.shot_size == b.shot_size else 0.0)
        weight += 0.5
    dist = _focus_distance(a.entry_focus, b.entry_focus)
    if dist is not None:
        score += 0.3 * max(0.0, 1.0 - dist / _EYE_TRACE_DIAG)
        weight += 0.3
    if a.daylight and b.daylight:
        score += 0.1 * (1.0 if a.daylight == b.daylight else 0.0)
        weight += 0.1
    mag_a = (math.hypot(*a.moment.entry_motion) + math.hypot(*a.moment.exit_motion)) / 2.0
    mag_b = (math.hypot(*b.moment.entry_motion) + math.hypot(*b.moment.exit_motion)) / 2.0
    if mag_a > _EPS or mag_b > _EPS:
        peak = max(mag_a, mag_b)
        score += 0.1 * (1.0 - abs(mag_a - mag_b) / peak)
        weight += 0.1
    return score / weight if weight > _EPS else 0.0


def _pick_reuse(
    pool: list[_PoolItem],
    start: int,
    held: set[int] | frozenset[int] = frozenset(),
    jumpy=None,
) -> _PoolItem | None:
    """First pool item (cyclic scan from ``start``) with unconsumed material.

    Indices in ``held`` (reserved for a not-yet-served drop slot) are skipped
    so their material stays fresh for the drop.

    ``jumpy`` (optional predicate on a pool item) is the reuse phase's
    jump-cut guard: a first scan skips items whose next slice would sit as
    a same-scene jump next to an already-cast neighbour; when EVERY item
    with material is jumpy the plain scan decides (material beats craft —
    the survivors are then counted into the low-variety note).
    """
    n = len(pool)
    fallback: _PoolItem | None = None
    for k in range(n):
        idx = (start + k) % n
        if idx in held:
            continue
        item = pool[idx]
        if item.remaining <= _EPS:
            continue
        if jumpy is not None and jumpy(item):
            if fallback is None:
                fallback = item
            continue
        return item
    return fallback


def _trim_overlapping_pool(pool: list[_PoolItem]) -> list[_PoolItem]:
    """Per clip, trim pool moments so no two claim the same frames.

    The zero-repeat promise (``allow_repeats=False``) must hold even for
    pools whose moments overlap within a clip — sift output never does,
    but hand-built reports and distilled timelines can. Walking each
    clip's moments in (start, end) order, a moment starting before the
    clip's running high-water mark is trimmed to start there (a COPY;
    the caller's reports are never mutated) and a moment fully inside
    already-claimed footage drops out. Pool order is preserved for the
    survivors; non-overlapping pools come back untouched (the common
    case costs one sorted pass).
    """
    order = sorted(
        range(len(pool)),
        key=lambda i: (pool[i].clip_path, pool[i].moment.start, pool[i].moment.end),
    )
    claimed_to: dict[str, float] = {}  # clip path -> high-water mark (seconds)
    trimmed: dict[int, _PoolItem | None] = {}
    for i in order:
        item = pool[i]
        mark = claimed_to.get(item.clip_path)
        lo = item.moment.start if mark is None else max(item.moment.start, mark)
        if item.moment.end - lo <= _EPS:
            trimmed[i] = None  # fully inside already-claimed footage
            continue
        if lo > item.moment.start + _EPS:
            trimmed[i] = replace(item, moment=replace(item.moment, start=lo))
        claimed_to[item.clip_path] = max(
            claimed_to.get(item.clip_path, 0.0), item.moment.end
        )
    return [trimmed.get(i, it) for i, it in enumerate(pool) if trimmed.get(i, it) is not None]


def _aim_start(
    item: _PoolItem, slot_len: float, lead: float, drop: bool = False
) -> float | None:
    """Peak-on-beat in-point (blueprint 1.1), or None for the plain head.

    A FRESH moment (nothing consumed yet) with a sifted ``peak_time``
    chooses its source start so the peak lands on the slot's beat —
    ``record_start + lead`` (the cut-ahead lead; the montage's first slot
    has no lead). Clamps, in order:

    * never before ``moment.start`` (the zero-repeat promise and the pool
      trim both reason from the moment's own bounds);
    * for a normal slot, never later than ``moment.end - slot_len`` — the
      slot stays inside the sifted material wherever it fits;
    * for a ``drop`` HOLD (usually longer than the ~1 s moment), the
      ceiling widens to ``slack_end - slot_len`` when the pool knows its
      slack (the enclosing USABLE segment, capped by the next same-clip
      moment): the peak then hits the drop instant and the hold extends
      PAST it through vetted material.

    Returns None — today's behavior, byte-identical — when the moment has
    no peak signal, is partially consumed (reuse slices stay
    chronological), or the clamped aim IS the plain head anyway.
    """
    moment = item.moment
    peak = getattr(moment, "peak_time", -1.0)
    if peak is None or peak < 0 or item.consumed > _EPS:
        return None
    hi = moment.end - slot_len
    if drop and item.slack_end > moment.end + _EPS:
        hi = max(hi, item.slack_end - slot_len)
    hi = max(moment.start, hi)
    desired = min(max(peak - lead, moment.start), hi)
    if desired <= moment.start + _EPS:
        return None
    return desired


def _first_frame_gate(item: _PoolItem, aimed: float, slot_len: float) -> float:
    """Nudge the hook slot's aimed in-point to a sharper first frame (1.9).

    Frame 1 of a short IS the feed thumbnail. Among the moment's sifted
    ``frame_quality`` samples within ``±_HOOK_GATE_WINDOW`` of the aimed
    start (the peak promise's own ±0.25 s honesty) — and inside the same
    clamps the aim used — the sharpest/brightest frame wins, but only for
    a real gain (> ``_HOOK_GATE_MIN_GAIN``) over the frame at the aimed
    start itself: the peak rules, the gate breaks near-ties.
    """
    quality = getattr(item.moment, "frame_quality", None)
    if not quality:
        return aimed
    moment = item.moment
    hi = max(moment.start, moment.end - slot_len)
    # A start after the peak itself would push the peak OFF screen — the
    # gate may only trade run-in, never the peak (the peak rules).
    peak = getattr(moment, "peak_time", aimed)
    window = [
        (t, q)
        for t, q in quality
        if abs(t - aimed) <= _HOOK_GATE_WINDOW + _EPS
        and moment.start - _EPS <= t <= min(hi, peak) + _EPS
    ]
    if not window:
        return aimed
    base_q = min(window, key=lambda tq: (abs(tq[0] - aimed), tq[0]))[1]
    best_t, best_q = max(window, key=lambda tq: (tq[1], -abs(tq[0] - aimed), -tq[0]))
    if best_q > base_q + _HOOK_GATE_MIN_GAIN and best_t > moment.start + _EPS:
        return best_t
    return aimed


def _peek_slice(item: _PoolItem, slot_len: float) -> tuple[float, float, int | None]:
    """The next un-aimed slice ``(src_start, src_end, gap_index)`` — no mutation.

    Reclaimed head gaps (skipped by a peak aim, never played) serve first:
    the first gap that fits a full slot, else — once the tail is gone —
    the LONGEST gap as a short piece (a gap is never padded: material
    after it is already on screen). Without gaps this is exactly the
    classic head-cursor slice, padding toward the clip's end when the
    moment's tail runs short — byte-identical to the pre-peak behavior.
    ``gap_index`` says which gap served (None = the cursor), so
    :func:`_commit_slice` can book the consumption.
    """
    moment = item.moment
    for k, (lo, hi) in enumerate(item.gaps):
        if hi - lo >= slot_len - _EPS:
            return lo, lo + slot_len, k
    tail = moment.end - (moment.start + item.consumed)
    if item.gaps and tail <= _EPS:
        k = max(range(len(item.gaps)), key=lambda i: item.gaps[i][1] - item.gaps[i][0])
        lo, hi = item.gaps[k]
        return lo, hi, k
    src_start = moment.start + item.consumed
    src_end = min(src_start + slot_len, moment.end)
    if src_end - src_start < slot_len - _EPS:
        # Pad the short piece by extending toward the clip's end.
        src_end = max(src_end, min(src_start + slot_len, item.clip_duration))
    return src_start, src_end, None


def _commit_slice(
    item: _PoolItem, src_start: float, src_end: float, gap_index: int | None
) -> None:
    """Book a :func:`_peek_slice` result on the item (cursor or gap)."""
    if gap_index is None:
        item.consumed = src_end - item.moment.start
        return
    lo, hi = item.gaps[gap_index]
    if src_end >= hi - _EPS:
        del item.gaps[gap_index]
    else:
        item.gaps[gap_index][0] = src_end


def _motion_continuity(
    prev_exit: tuple[float, float] | None, entry: tuple[float, float]
) -> float:
    """Cosine similarity between exit and entry motion, in [-1, 1].

    Neutral 0 when there is no previous entry or either vector's magnitude
    is at or below ``_MOTION_MIN_MAGNITUDE`` px (i.e. effectively static).
    """
    if prev_exit is None:
        return 0.0
    ax, ay = prev_exit
    bx, by = entry
    mag_a = math.hypot(ax, ay)
    mag_b = math.hypot(bx, by)
    if mag_a <= _MOTION_MIN_MAGNITUDE or mag_b <= _MOTION_MIN_MAGNITUDE:
        return 0.0
    return (ax * bx + ay * by) / (mag_a * mag_b)


def _phase_label_at(phases: list[tuple[float, float, str]], t: float) -> str | None:
    """Phase label of the arc phase containing ``t`` (None if no phases)."""
    for start, end, label in phases:
        if start - _EPS <= t < end - _EPS:
            return label
    if phases and t >= phases[-1][1] - _EPS:
        return phases[-1][2]
    return None


def _wanted_roles(
    slot_idx: int,
    n_slots: int,
    phases: list[tuple[float, float, str]] | None,
    rec_start: float,
) -> set[str]:
    """Vision roles a slot asks for.

    The slot's arc phase maps through :data:`_ROLE_FOR_PHASE`; on top of
    that the montage's FIRST slot always asks for an "opener" and its LAST
    slot for a "closer", in every style (also the arc-less "auto") — with
    ONE exception: a montage that opens on a "hook" phase (the "short"
    style) never asks for an opener. Shorts do not establish; slot 0 is
    cast by hook score instead (see :func:`_fill`).
    """
    wanted: set[str] = set()
    if slot_idx == 0 and not (phases and phases[0][2] == "hook"):
        wanted.add("opener")
    if slot_idx == n_slots - 1:
        wanted.add("closer")
    if phases:
        role = _ROLE_FOR_PHASE.get(_phase_label_at(phases, rec_start) or "")
        if role:
            wanted.add(role)
    return wanted


def _daylight_targets(
    slots: list[tuple[float, float]], pool: list[_PoolItem]
) -> list[str]:
    """Per-slot target daylight class for the deterministic story arc.

    The default block ORDER is the material's natural arc — day -> golden
    -> night (:data:`_DAYLIGHT_ARC`) filtered to the classes that actually
    exist in the pool. Each present class gets a contiguous block of
    record time proportional to its share of the pool's classified
    material; a slot's target is the class whose block contains the
    slot's midpoint. Returns ``[]`` (no targeting) when fewer than two
    classes exist — a one-class shoot has no arc to tell.
    """
    share: dict[str, float] = {}
    for item in pool:
        if item.daylight:
            share[item.daylight] = share.get(item.daylight, 0.0) + max(
                0.0, item.moment.end - item.moment.start
            )
    order = [c for c in _DAYLIGHT_ARC if share.get(c, 0.0) > _EPS]
    if len(order) < 2 or not slots:
        return []
    total = sum(share[c] for c in order)
    origin = slots[0][0]
    length = slots[-1][1] - origin
    if total <= _EPS or length <= _EPS:
        return []
    bounds: list[tuple[float, str]] = []
    acc = 0.0
    for c in order:
        acc += share[c] / total * length
        bounds.append((acc, c))
    targets: list[str] = []
    for start, end in slots:
        mid = (start + end) / 2.0 - origin
        for bound, c in bounds:
            if mid <= bound + _EPS:
                targets.append(c)
                break
        else:
            targets.append(order[-1])
    return targets


def _fill(
    slots: list[tuple[float, float]],
    slot_order: list[int],
    pool: list[_PoolItem],
    phases: list[tuple[float, float, str]] | None = None,
    highlight_phase: str | None = None,
    drop_slots: set[int] | frozenset[int] = frozenset(),
    semantic: bool = False,
    slot_energies: list[float] | None = None,
    pre_used: set[int] | frozenset[int] = frozenset(),
    preset: dict[int, "_PoolItem"] | None = None,
    hook_loop: bool = False,
    allow_repeats: bool = True,
    slot_contexts: list[str] | None = None,
    cut_lead: float = 0.0,
    casting_bias: "CastingBias | None" = None,
) -> tuple[list[MontageEntry], list[str], float | None]:
    """Assign pool moments to slots.

    Peak-on-beat (blueprint 1.1): a fresh moment carrying a sifted
    ``peak_time`` does not simply play from its head — its in-point is
    chosen so the peak lands on the slot's beat, ``record_start +
    cut_lead`` (the montage's first slot has no lead), clamped to the
    moment's bounds; a drop HOLD may extend past the moment through the
    pool's vetted slack (see :func:`_aim_start`). Head material the aim
    skips is remembered as a reclaimable gap and served by the reuse
    phase before anything rewinds — the zero-repeat bookkeeping never
    burns a skipped head. The "short" style's hook slot additionally
    gates its aimed in-point by first-frame quality
    (:func:`_first_frame_gate`, blueprint 1.9). Pools without peak
    signals fill byte-identically to before the aim existed; arranged
    (preset) slots are never re-aimed.

    The first pass still consumes every pool moment exactly once, in pool
    order — the ordering mode decides WHICH moments are in play — with two
    craft refinements that only reorder the next few candidates:

    * Drop slots are reserved up front for the unused moment with the
      highest (highlight, score), so the drop hits the strongest material.
    * For every other slot the next ``_CANDIDATE_WINDOW`` (K = 4) unconsumed
      pool items compete. Inside ``highlight_phase`` they are first re-sorted
      by (highlight, score) so audible peaks win the musical peak. The pick
      maximises ``0.7 * order_preference + 0.3 * motion_continuity`` where
      order preference is ``1 - position / K`` (earlier = higher) and motion
      continuity is the cosine similarity between the previous slot's exit
      motion and the candidate's entry motion (see
      :func:`_motion_continuity`). With neutral motion the earliest
      candidate always wins, so behavior without motion data is unchanged.

    ``slot_energies`` (one value 0..1 per slot, from the song's sections or
    the arc's nominal phase energy) adds an energy-motion matching term to
    the blend: ``_ENERGY_MATCH_WEIGHT x (1 - |slot_energy - motion|)`` where
    motion is the candidate's mean entry/exit motion magnitude normalised to
    the pool's fastest moment. Loud passages meet moving footage, calm
    passages calm footage. With an all-static pool the term is equal for
    every candidate, so behavior without motion data is unchanged.

    Reuse (pool exhausted) slices unconsumed tails first — a cyclic scan
    for the next moment with unused material; distinct footage, never a
    repeat — and a drop slot still grabs the best remaining material.
    Only when NO moment has unused material left do the modes differ:
    ``allow_repeats=True`` rewinds a moment and repeats its footage
    (noted); ``allow_repeats=False`` NEVER rewinds — filling stops, every
    entry at/after the first unservable slot is dropped, and the third
    return value carries that slot's record start so the caller can cut
    the plan there (None everywhere else). The zero-repeat promise beats
    the requested length.

    ``pre_used`` (pool indices an arrangement already placed) keeps those
    moments out of the first pass — their unconsumed TAILS stay available
    to the reuse scan, exactly like any other consumed moment. ``preset``
    (slot index -> the pool item an arrangement put there) seeds the
    neighbour bookkeeping, so motion continuity and the same-scene
    penalty see the arranged slots. Both default to empty, which is
    byte-identical to the behavior before they existed.

    ``hook_loop=True`` (the "short" style — its arc opens on a "hook"
    phase) reserves two slots up front, before the drop reservation:

    * **Slot 0 — the hook.** The unused moment with the highest
      ``hook_score = _HOOK_MOTION_WEIGHT x motion + _HOOK_HERO_WEIGHT x
      hero + _HOOK_SCORE_WEIGHT x score`` (motion = the moment's mean
      entry/exit magnitude normalised to the pool's fastest; ties go to
      the earlier pool position). The "opener" role preference does NOT
      apply — a short opens on the pattern interrupt, not the prettiest
      establishing shot (:func:`_wanted_roles` skips "opener" for hook
      openings).
    * **The LAST slot — the loop.** Prefers a moment from the hook's own
      scene ``group`` (highest score among them), so the ending cuts
      seamlessly back into the opening on replay; without a group match
      it takes the moment whose motion energy is closest to the hook's.
      With neither signal (no groups, an all-static pool) the slot is
      left to the normal fill — graceful, no fake note.

    Both reservations skip slots an arrangement already cast (``preset``)
    and are reported in the notes ("hook: ...", "loop: ...").

    ``slot_contexts`` (one act/section label per slot, or None) feeds the
    JUMP-CUT GUARD: a candidate that would sit next to a same-clip
    neighbour with a source gap inside ``±_JUMP_CUT_MIN_GAP`` (forward
    skips AND nearby rewinds; beyond that the cut reads as another
    scene) pays :data:`_JUMP_CUT_PENALTY` — bigger than the group
    penalty, so a different clip up to two order positions behind wins
    instead — UNLESS the continuity merge (:func:`_merge_continuity`)
    would join the pair anyway (gap within :data:`_CONTINUITY_MAX_GAP`,
    same context, not into a drop/final slot, merged span within the
    cut ceiling, play reaching the later window): a joinable
    continuation is one shot, not a jump. The REUSE phase applies the
    same test: :func:`_pick_reuse` first scans for material that does
    not jump against an already-cast neighbour and falls back to the
    plain cyclic scan only when everything left is jumpy.

    ``semantic=True`` (any pool moment carries vision annotations) layers
    the semantic-casting bonuses onto the candidate blend: a fitting role
    adds ``_ROLE_WEIGHT``, climax-phase candidates add ``_HERO_WEIGHT`` x
    hero (drop reservation weighs hero the same way), and a candidate whose
    scene group matches an already-filled neighbouring slot loses
    ``_GROUP_PENALTY`` (see the module docstring). A note reports what the
    casting actually changed. With all-default annotations every bonus is
    zero, so behavior is exactly the unannotated one.
    """
    entries: list[MontageEntry] = []
    notes: list[str] = []
    n = len(pool)
    # Energy-motion matching: candidate motion magnitudes normalised to the
    # pool's fastest moment (empty = all static = term disabled). Hook
    # casting reads the same normalisation.
    motion_norm: list[float] = []
    if slot_energies is not None or hook_loop:
        mags = [
            (math.hypot(*it.moment.entry_motion) + math.hypot(*it.moment.exit_motion)) / 2.0
            for it in pool
        ]
        peak = max(mags, default=0.0)
        if peak > _EPS:
            motion_norm = [m / peak for m in mags]
    # Time-of-day coherence (see the module docstring): active as soon as
    # any pool moment carries a daylight class. day_targets is the
    # deterministic block arc ([] when fewer than two classes exist —
    # the switch penalty still applies, there is just no arc to follow).
    daylight_active = any(it.daylight for it in pool)
    day_targets: list[str] = _daylight_targets(slots, pool) if daylight_active else []
    # Picture coherence (blueprint wave 3): each term is a tie-breaker,
    # live only while the offline spatial signal is present, so a pool
    # without it casts byte-identically.
    eye_trace_active = any(
        it.entry_focus is not None or it.exit_focus is not None for it in pool
    )
    grammar_active = any(it.shot_size for it in pool)
    rhyme_active = eye_trace_active or grammar_active
    last_slot = len(slots) - 1
    rhyme_note_done = False
    rewound = False
    short_at: float | None = None  # record start of the first unservable slot
    # Pool indices not yet placed, in pool order (an arrangement's picks
    # are already placed and start consumed).
    unused = [i for i in range(n) if i not in pre_used]
    reserved: dict[int, int] = {}  # slot index -> pool index held for it
    taken = preset or {}
    if hook_loop and unused and slots and 0 not in taken:
        # Hook casting ("short" style): slot 0 takes the PATTERN INTERRUPT
        # — the boldest moment by motion + hero + score, opener role be
        # damned. Reserved FIRST, before the drop reservation: nothing
        # outranks the hook on a vertical platform.
        def _hook_score(idx: int) -> float:
            motion = motion_norm[idx] if motion_norm else 0.0
            return (
                _HOOK_MOTION_WEIGHT * motion
                + _HOOK_HERO_WEIGHT * pool[idx].hero
                + _HOOK_SCORE_WEIGHT * pool[idx].moment.score
            )

        pos = max(range(len(unused)), key=lambda p: (_hook_score(unused[p]), -unused[p]))
        hook_idx = unused.pop(pos)
        reserved[0] = hook_idx
        notes.append("hook: opening on the boldest moment (motion + hero + score)")
        # Loop ending: the LAST slot prefers a moment from the hook's own
        # scene group — the short then cuts seamlessly back into its
        # opening on replay — or, without one, the closest motion energy
        # to the hook's. With neither signal the normal fill decides.
        last = len(slots) - 1
        if last > 0 and unused and last not in taken:
            hook_group = pool[hook_idx].group
            # Loop seam, visual half (blueprint 1.5): the LAST shot's EXIT
            # motion should hand back into the hook's ENTRY motion — the
            # replay wrap then reads as one continuous move. The bonus is
            # a tie-breaker (_LOOP_HANDBACK_WEIGHT, sized like the fill's
            # motion term); with no usable vectors it is exactly 0 and the
            # classic picks stand byte-identically.
            hook_entry = pool[hook_idx].moment.entry_motion

            def _handback(idx: int) -> float:
                return _motion_continuity(pool[idx].moment.exit_motion, hook_entry)

            same_scene = [
                p for p in range(len(unused))
                if hook_group and pool[unused[p]].group == hook_group
            ]
            loop_pos: int | None = None
            loop_note = ""
            if same_scene:
                loop_pos = max(
                    same_scene,
                    key=lambda p: (
                        pool[unused[p]].moment.score
                        + _LOOP_HANDBACK_WEIGHT * _handback(unused[p]),
                        -unused[p],
                    ),
                )
                loop_note = "loop: last shot matches the hook's scene"
            elif motion_norm:
                hook_motion = motion_norm[hook_idx]
                loop_pos = max(
                    range(len(unused)),
                    key=lambda p: (
                        _LOOP_HANDBACK_WEIGHT * _handback(unused[p])
                        - abs(motion_norm[unused[p]] - hook_motion),
                        -unused[p],
                    ),
                )
                loop_note = "loop: last shot matches the hook's motion energy"
            if loop_pos is not None:
                if _handback(unused[loop_pos]) > _EPS:
                    loop_note += " — and hands its motion back to the hook"
                notes.append(loop_note)
                reserved[last] = unused.pop(loop_pos)
    for drop_slot in sorted(drop_slots):
        if not unused:
            break
        if drop_slot in reserved:
            continue  # the hook/loop reservation got there first
        pos = max(
            range(len(unused)),
            key=lambda p: (
                # Hero shots belong on the drop: hero weighs in next to the
                # audio highlight (identical to before when hero is 0).
                pool[unused[p]].moment.highlight + _HERO_WEIGHT * pool[unused[p]].hero,
                pool[unused[p]].moment.score,
                -unused[p],  # ties: earliest in pool order
            ),
        )
        reserved[drop_slot] = unused.pop(pos)
    held = set(reserved.values())  # kept out of reuse until their drop is served

    by_slot: dict[int, _PoolItem] = dict(preset or {})
    # Source windows already placed, per slot — the jump-cut guard reads
    # its neighbours here (arranged slots are the editor's own order and
    # are not guarded against).
    windows: dict[int, tuple[str, float, float]] = {}
    same_scene_avoided = 0
    aimed_slots = 0  # slots whose in-point was peak-aimed (blueprint 1.1)
    for visit, slot_idx in enumerate(slot_order):
        rec_start, rec_end = slots[slot_idx]
        slot_len = rec_end - rec_start
        # The beat this slot serves sits cut_lead AFTER the (shifted) cut;
        # the montage's first slot starts ON its beat.
        lead = cut_lead if slot_idx > 0 else 0.0

        def _is_jumpy(clip: str, cand_start: float, cand_end: float) -> bool:
            """Would this source window sit as a same-scene jump cut next
            to an already-cast neighbour of ``slot_idx``? (False when the
            continuity merge would join the pair into one shot.)"""
            for ns in (slot_idx - 1, slot_idx + 1):
                w = windows.get(ns)
                if not w or w[0] != clip:
                    continue
                if ns < slot_idx:
                    gap = cand_start - w[2]
                    earlier, later = ns, slot_idx
                    early_src, late_src = w[1], cand_start
                else:
                    gap = w[1] - cand_end
                    earlier, later = slot_idx, ns
                    early_src, late_src = cand_start, w[1]
                if abs(gap) > _JUMP_CUT_MIN_GAP + _EPS:
                    continue  # far apart (either way): reads as another scene
                # A nearby rewind (small negative gap) is always a jump
                # cut; a small forward gap is fine only when the continuity
                # merge would join the pair into one shot (same rules).
                span = slots[later][1] - slots[earlier][0]
                if (
                    -_EPS <= gap <= _CONTINUITY_MAX_GAP + _EPS
                    and later not in drop_slots
                    and later != len(slots) - 1
                    and (
                        not slot_contexts
                        or slot_contexts[earlier] == slot_contexts[later]
                    )
                    and span <= _MAX_CUT_SECONDS + _EPS
                    and early_src + span >= late_src - _EPS
                ):
                    continue  # the continuity merge joins them: one shot
                return True
            return False

        if slot_idx in reserved:
            item = pool[reserved[slot_idx]]  # drop slot: strongest moment
            held.discard(reserved[slot_idx])
        elif unused:
            # First pass: choose among the next K unconsumed pool items.
            window = unused[:_CANDIDATE_WINDOW]
            if (
                highlight_phase
                and phases
                and _phase_label_at(phases, rec_start) == highlight_phase
            ):
                window = sorted(
                    window,
                    key=lambda i: (-pool[i].moment.highlight, -pool[i].moment.score, i),
                )
            prev = by_slot.get(slot_idx - 1)
            prev_exit = prev.moment.exit_motion if prev is not None else None
            prev_daylight = prev.daylight if prev is not None else ""
            # Picture coherence (blueprint wave 3): the previous slot's
            # spatial signal, and whether this slot opens a new arc phase
            # (a deliberate accent boundary where the eye-trace term steps
            # aside for a welcome contrast).
            prev_shot_size = prev.shot_size if prev is not None else ""
            prev_exit_focus = prev.exit_focus if prev is not None else None
            in_climax = bool(phases) and _phase_label_at(phases, rec_start) == "climax"
            # Learned-preference casting bias (blueprint 4.3): the slot's
            # phase label steers a shot-size preference to the phase it was
            # learned for ("close-ups at the climax"). None (no phases) still
            # matches a "*" preference.
            bias_phase = _phase_label_at(phases, rec_start) if phases else None
            accent_boundary = bool(
                phases
                and prev is not None
                and _phase_label_at(phases, slots[slot_idx - 1][0])
                != _phase_label_at(phases, rec_start)
            )
            # Semantic casting: mild per-candidate adjustments (see the
            # module docstring). Bonuses (role fit, climax hero) and the
            # scene-variety penalty are kept apart so we can honestly note
            # when the penalty actually diverted a pick.
            sem_bonus: dict[int, float] = {}
            sem_penalty: dict[int, float] = {}
            if semantic:
                wanted = _wanted_roles(slot_idx, len(slots), phases, rec_start)
                neighbour_groups = {
                    by_slot[j].group
                    for j in (slot_idx - 1, slot_idx + 1)
                    if j in by_slot and by_slot[j].group
                }
                for idx in window:
                    bonus = 0.0
                    if pool[idx].role and pool[idx].role in wanted:
                        bonus += _ROLE_WEIGHT
                    if in_climax:
                        bonus += _HERO_WEIGHT * pool[idx].hero
                    sem_bonus[idx] = bonus
                    if pool[idx].group and pool[idx].group in neighbour_groups:
                        sem_penalty[idx] = _GROUP_PENALTY

            # Jump-cut guard: a same-clip neighbour with a small source
            # gap that the continuity merge would NOT join reads as a
            # visible jump inside one scene — penalize the candidate so
            # a different clip wins while the pool has one. Peak-aimed
            # candidates are judged at their AIMED start.
            jump_pen: dict[int, float] = {}
            for idx in window:
                cand_start = pool[idx].moment.start + pool[idx].consumed
                aim = _aim_start(pool[idx], slot_len, lead, drop=slot_idx in drop_slots)
                if aim is not None:
                    cand_start = aim
                if _is_jumpy(pool[idx].clip_path, cand_start, cand_start + slot_len):
                    jump_pen[idx] = _JUMP_CUT_PENALTY

            def _blend(pos: int, idx: int) -> float:
                score = (
                    _ORDER_WEIGHT * (1.0 - pos / _CANDIDATE_WINDOW)
                    + _MOTION_WEIGHT
                    * _motion_continuity(prev_exit, pool[idx].moment.entry_motion)
                    + sem_bonus.get(idx, 0.0)
                )
                if motion_norm and slot_energies is not None:
                    score += _ENERGY_MATCH_WEIGHT * (
                        1.0 - abs(slot_energies[slot_idx] - motion_norm[idx])
                    )
                # Time-of-day coherence: a switch away from the previous
                # slot's class costs a little (zero when either side is
                # unknown), and matching the arc's target class for this
                # slot earns a little — both soft, both tie-breakers.
                if daylight_active and pool[idx].daylight:
                    if prev_daylight and pool[idx].daylight != prev_daylight:
                        score -= _DAYLIGHT_SWITCH_PENALTY
                    if day_targets and pool[idx].daylight == day_targets[slot_idx]:
                        score += _DAYLIGHT_BLOCK_WEIGHT
                # Eye-trace continuity (3.1, Murch rule 4): reward a small
                # on-screen distance between the previous shot's exit
                # attention point and this candidate's entry point (the eye
                # is carried across the cut); mildly penalize a leap. Never
                # at a phase boundary, where a deliberate contrast is the
                # accent. A tie-breaker, zero when either point is unknown.
                if eye_trace_active and not accent_boundary:
                    dist = _focus_distance(prev_exit_focus, pool[idx].entry_focus)
                    if dist is not None:
                        score += _EYE_TRACE_WEIGHT * (1.0 - 2.0 * dist / _EYE_TRACE_DIAG)
                # Shot-size grammar (3.2): establish -> develop -> pay off;
                # penalize two equal sizes adjacent, except a deliberate
                # close->close intensification in the climax.
                if grammar_active:
                    grammar_scale = casting_bias.grammar_scale if casting_bias else 1.0
                    score += grammar_scale * _shot_grammar_step(
                        prev_shot_size, pool[idx].shot_size, in_climax
                    )
                # Learned-preference casting bias (blueprint 4.3): a small
                # tie-breaker toward the shot size the user's corrections
                # favour in this phase. Zero for an empty/neutral store, so
                # the default plan is byte-identical.
                if casting_bias is not None:
                    score += casting_bias.size_bonus(bias_phase, pool[idx].shot_size)
                # Visual rhyme (3.3): the closing shot echoes the opening —
                # tip the last slot toward the moment most kindred to slot
                # 0's. One rhyme, one slot; the candidate is a different,
                # still-unused moment, so zero-repeat holds.
                if rhyme_active and slot_idx == last_slot and 0 in by_slot:
                    kinship = _visual_kinship(by_slot[0], pool[idx])
                    if kinship >= _RHYME_MIN_KINSHIP:
                        score += _RHYME_WEIGHT * kinship
                return score - jump_pen.get(idx, 0.0)

            best = max(
                enumerate(window),
                key=lambda pi: _blend(*pi) - sem_penalty.get(pi[1], 0.0),
            )[1]
            if sem_penalty:
                unguarded = max(enumerate(window), key=lambda pi: _blend(*pi))[1]
                if unguarded != best:
                    same_scene_avoided += 1
            # Visual rhyme note (3.3): report the ONE closing echo when the
            # last slot actually landed on a moment kindred to the opening.
            if (
                rhyme_active
                and not rhyme_note_done
                and slot_idx == last_slot
                and slot_idx != 0
                and 0 in by_slot
                and _visual_kinship(by_slot[0], pool[best]) >= _RHYME_MIN_KINSHIP
            ):
                notes.append(
                    "rhyme: the closing shot echoes the opening (visual callback)"
                )
                rhyme_note_done = True
            unused.remove(best)
            item = pool[best]
        else:
            item = None
            if slot_idx in drop_slots:  # late drop slot: best remaining tail
                leftovers = [
                    it for i, it in enumerate(pool) if i not in held and it.remaining > _EPS
                ]
                if leftovers:
                    item = max(
                        leftovers,
                        key=lambda it: (
                            it.moment.highlight + _HERO_WEIGHT * it.hero,
                            it.moment.score,
                        ),
                    )
            def _reuse_jumpy(it: _PoolItem) -> bool:
                start_s = _peek_slice(it, slot_len)[0]
                return _is_jumpy(it.clip_path, start_s, start_s + slot_len)

            if item is None:
                item = _pick_reuse(pool, visit % n, held, jumpy=_reuse_jumpy)
            if item is None and not allow_repeats and held:
                # Repeats are off and only drop-held material remains:
                # releasing a reservation beats ending the cut early (the
                # drop slot then pads or gaps — but never repeats).
                item = _pick_reuse(pool, visit % n, jumpy=_reuse_jumpy)
            if item is None and not allow_repeats:
                # Zero-repeat promise: never rewind. The montage ends at
                # the first slot fresh material cannot serve; entries the
                # (possibly energy-ordered) fill already placed at/after
                # that point are dropped, and the caller cuts the plan.
                short_at = min(slots[s][0] for s in slot_order[visit:])
                entries = [e for e in entries if e.record_start < short_at - _EPS]
                break
            if item is None:  # everything consumed: rewind and repeat footage
                idx = visit % n
                for k in range(n):  # don't rewind a held (drop-reserved) moment
                    if (idx + k) % n not in held:
                        idx = (idx + k) % n
                        break
                item = pool[idx]
                item.consumed = 0.0
                rewound = True
        by_slot[slot_idx] = item
        moment = item.moment
        aimed = _aim_start(item, slot_len, lead, drop=slot_idx in drop_slots)
        if aimed is not None and hook_loop and slot_idx == 0:
            # First-frame gate (blueprint 1.9): the hook's frame 1 is the
            # thumbnail — nudge the aimed start to a sharper frame.
            aimed = _first_frame_gate(item, aimed, slot_len)
        if aimed is not None:
            # Peak-on-beat (blueprint 1.1): play from the aimed in-point;
            # the skipped head stays reclaimable (a gap, never burnt).
            head = moment.start + item.consumed
            if aimed - head >= _PEAK_GAP_MIN - _EPS:
                item.gaps.append([head, aimed])
            src_start = aimed
            src_end = min(src_start + slot_len, moment.end)
            if src_end - src_start < slot_len - _EPS:
                # Pad the short piece by extending toward the clip's end
                # (a drop hold's slack ceiling was vetted by _aim_start).
                src_end = max(src_end, min(src_start + slot_len, item.clip_duration))
            item.consumed = max(item.consumed, src_end - moment.start)
            aimed_slots += 1
        else:
            src_start, src_end, gap_index = _peek_slice(item, slot_len)
            _commit_slice(item, src_start, src_end, gap_index)
        if src_end - src_start < slot_len - _EPS:
            notes.append(
                f"gap at {rec_start:.2f}s: only {src_end - src_start:.2f}s of "
                f"source for a {slot_len:.2f}s slot"
            )
        item.uses += 1
        windows[slot_idx] = (item.clip_path, src_start, src_end)
        entries.append(
            MontageEntry(
                clip_path=item.clip_path,
                source_start=src_start,
                source_end=src_end,
                record_start=rec_start,
                record_end=rec_end,
                score=moment.score,
                media_start=item.media_start,
                clip_duration=item.clip_duration,
                label=item.label,
                # Blueprint 4.1: carry the chosen moment's peak (in file
                # coordinates) so critique can score peak-on-beat from the
                # plan alone. In-memory only (not serialized).
                peak_source=getattr(moment, "peak_time", -1.0),
                shot_size=item.shot_size,
                # Auto-reframe 9:16: carry the cast moment's attention point
                # (averaged entry/exit focus) so the vertical/cine export can
                # keep the subject framed instead of centre-cropping. In-memory
                # only (not serialized), like peak_source/shot_size above.
                reframe_focus=_reframe.average_focus(
                    item.entry_focus, item.exit_focus
                ),
            )
        )
    if aimed_slots:
        notes.append(
            f"cut on action: {aimed_slots} of {len(slots)} slot"
            f"{'s' if len(slots) != 1 else ''} aim the picture's peak at the beat"
        )
    if len(slot_order) > n:
        if allow_repeats:
            msg = f"material ran short: {len(slot_order)} slots for {n} moments; moments reused"
            if rewound:
                msg += " (some footage repeats)"
        else:
            msg = (
                f"material ran short: {len(slot_order)} slots for {n} moments; "
                "long moments split into extra pieces (nothing repeats)"
            )
        notes.append(msg)
    if semantic:
        pieces: list[str] = []
        if any(it.role for it in pool):
            matched = sum(
                1
                for idx, it in by_slot.items()
                if it.role and it.role in _wanted_roles(idx, len(slots), phases, slots[idx][0])
            )
            pieces.append(f"{matched} of {len(slots)} slots matched to roles")
        heroes = sum(
            1
            for idx in drop_slots
            if idx in by_slot and by_slot[idx].hero >= _HERO_NOTE_LEVEL
        )
        if heroes:
            pieces.append(
                "hero shot on the drop" if heroes == 1 else f"hero shots on {heroes} drops"
            )
        if same_scene_avoided:
            pieces.append(
                f"{same_scene_avoided} same-scene cut"
                + ("s" if same_scene_avoided != 1 else "")
                + " avoided"
            )
        if pieces:
            notes.append("semantic casting: " + ", ".join(pieces))
    if day_targets:
        arc = " -> ".join(dict.fromkeys(day_targets))
        notes.append(f"story: daylight arc {arc} (soft)")
        # Honest warnings: cast slots sitting against the flow. Arranged
        # slots (preset) are the editor's own order and are never flagged.
        preset_slots = set(preset or {})
        against = [
            (idx, by_slot[idx].daylight, day_targets[idx])
            for idx in sorted(by_slot)
            if idx not in preset_slots
            and (short_at is None or slots[idx][0] < short_at - _EPS)
            and by_slot[idx].daylight
            and by_slot[idx].daylight != day_targets[idx]
        ]
        for idx, got, want in against[:_DAYLIGHT_NOTE_LIMIT]:
            notes.append(f"slot {idx + 1}: {got} shot inside the {want} block")
        if len(against) > _DAYLIGHT_NOTE_LIMIT:
            notes.append(
                f"daylight: {len(against) - _DAYLIGHT_NOTE_LIMIT} more "
                "slots sit against the arc"
            )
    return entries, notes, short_at


# --- post-cast coalescing (continuity + content-adaptive pacing) ----------------


def _match_pool_moment(entry: MontageEntry, pool: list[_PoolItem]) -> int | None:
    """Pool index of the moment ``entry`` was cast from (largest source
    overlap in the same clip), or None when nothing overlaps."""
    best: int | None = None
    best_ov = 0.0
    for i, item in enumerate(pool):
        if item.clip_path != entry.clip_path:
            continue
        ov = min(item.moment.end, entry.source_end) - max(
            item.moment.start, entry.source_start
        )
        if ov > best_ov + _EPS:
            best, best_ov = i, ov
    return best


def _extension_overlaps(
    entries: list[MontageEntry],
    clip: str,
    lo: float,
    hi: float,
    absorbed: set[int],
) -> bool:
    """True when (lo, hi) of ``clip`` is on screen in another entry."""
    for k, other in enumerate(entries):
        if k in absorbed or other.clip_path != clip:
            continue
        if min(other.source_end, hi) - max(other.source_start, lo) > _EPS:
            return True
    return False


def _boundary_context(
    entries: list[MontageEntry],
    i: int,
    phases: list[tuple[float, float, str]],
    sections: list[MusicSection],
) -> str:
    """The act/section an entry sits in — merges never cross these."""
    if phases:
        return _phase_label_at(phases, entries[i].record_start) or ""
    if sections:
        return _label_at(sections, entries[i].record_start)
    return ""


def _merge_continuity(
    entries: list[MontageEntry],
    slot_of: list[int],
    phases: list[tuple[float, float, str]],
    sections: list[MusicSection],
    drop_slots: set[int] | frozenset[int],
    protected: int,
) -> tuple[list[MontageEntry], list[int], str | None]:
    """Join adjacent same-clip slots into ONE continuous shot (deterministic).

    When the casting puts two-plus ADJACENT slots on material from the
    SAME clip whose source windows sit within :data:`_CONTINUITY_MAX_GAP`
    seconds of each other, the viewer sees one continuing scene chopped
    by jump cuts — the exact field complaint. This pass replaces the run
    with one shot that plays CONTINUOUSLY from the first window's start
    across the full merged record span: the bridge material (the clip's
    own frames between the windows) is played instead of jumped over, so
    the source stays 1:1 with the record and the shot ends
    ``total gap`` seconds before the last window's end (that tail is
    simply released). Cut boundaries only DISAPPEAR — every surviving
    cut still sits on the musical grid.

    Rules, and how they differ from the calm merge
    (:func:`_merge_calm_slots`) — that pass is about PACING (calm
    content on calm music earns longer shots), this one is about
    CONTINUITY (one take must read as one take), so the gates differ:

    * same ``clip_path`` and every joined boundary's source gap within
      ``[0, _CONTINUITY_MAX_GAP]`` (a negative gap is a replay, never a
      continuity) — no calmness requirement, no music-energy gate, and
      no motion data needed;
    * the CLIMAX may merge internally: when the same ride continues
      over the drop's aftermath, holding the shot beats re-cutting it
      (the calm merge never touches the climax — a pacing merge there
      would drain the peak; a continuity merge there just keeps the
      ride). Crossing an act/section-label change is still forbidden
      (:func:`_boundary_context`), so structure — and every
      smash-to-black dip, which is carved AT those changes — survives;
    * a drop slot is never ABSORBED (the drop keeps its own fresh hit)
      but may itself absorb its followers (the drop hold grows);
    * arranged entries (``entries[0..protected-1]``) and the final entry
      (the cast closer/loop shot) never take part; the merged shot stays
      at/below :data:`_MAX_CUT_SECONDS`;
    * the bridge frames must not be on screen in any other entry — the
      zero-repeat promise survives; the accumulated drift (source end vs
      the last absorbed window's end) stays within
      :data:`_CONTINUITY_MAX_GAP`; and the continuous play must REACH
      each absorbed window's own material — a join that would show only
      bridge frames is replacement, not continuity, and is refused.

    ``slot_of[i]`` is entry i's original slot index (the fill's 1:1
    tiling); the returned list maps each SURVIVING entry to the slot it
    STARTS in, so the calm merge can still look up slot energies and
    drop slots afterwards. Returns ``(entries, slot_of, note)`` — the
    note ("continuity: N same-scene cuts joined ...") is None when
    nothing merged.
    """
    n = len(entries)
    if n < 2:
        return entries, slot_of, None

    result: list[MontageEntry] = []
    result_slots: list[int] = []
    joined = 0
    groups = 0
    i = 0
    while i < n:
        entry = entries[i]
        if i < protected:
            result.append(entry)
            result_slots.append(slot_of[i])
            i += 1
            continue
        ctx = _boundary_context(entries, i, phases, sections)
        absorbed: set[int] = {i}
        new_record_end = entry.record_end
        new_source_end = entry.source_end
        j = i + 1
        while (
            j < n - 1  # the last entry is never absorbed (the cast closer)
            and j >= protected
            and slot_of[j] not in drop_slots  # the drop keeps its own hit
            and entries[j].clip_path == entry.clip_path
            and _boundary_context(entries, j, phases, sections) == ctx
            and abs(entries[j].record_start - new_record_end) <= _EPS
            and entries[j].record_end - entry.record_start
            <= _MAX_CUT_SECONDS + _EPS
        ):
            gap = entries[j].source_start - entries[j - 1].source_end
            if gap < -_EPS or gap > _CONTINUITY_MAX_GAP + _EPS:
                break  # a replay, or too far apart to be the same ride
            want_end = entry.source_start + (
                entries[j].record_end - entry.record_start
            )
            drift = entries[j].source_end - want_end
            if drift < -_EPS or drift > _CONTINUITY_MAX_GAP + _EPS:
                break  # material missing, or drifted too far off the cast
            if want_end < entries[j].source_start - _EPS:
                break  # the continuous play would never reach this slot's
                # cast material — that is replacement, not continuity
            if _extension_overlaps(
                entries, entry.clip_path, new_source_end, want_end, absorbed | {j}
            ):
                break  # the bridge would repeat material another slot plays
            absorbed.add(j)
            new_record_end = entries[j].record_end
            new_source_end = want_end
            j += 1
        if len(absorbed) > 1:
            result.append(
                replace(entry, record_end=new_record_end, source_end=new_source_end)
            )
            result_slots.append(slot_of[i])
            joined += len(absorbed) - 1
            groups += 1
            i = j
        else:
            result.append(entry)
            result_slots.append(slot_of[i])
            i += 1

    if not groups:
        return entries, slot_of, None
    note = (
        f"continuity: {joined} same-scene cut{'s' if joined != 1 else ''} "
        f"joined into {groups} longer shot{'s' if groups != 1 else ''} "
        "(one take reads as one take)"
    )
    return result, result_slots, note


def _merge_calm_slots(
    entries: list[MontageEntry],
    pool: list[_PoolItem],
    slot_energies: list[float],
    phases: list[tuple[float, float, str]],
    sections: list[MusicSection],
    drop_slots: set[int] | frozenset[int],
    protected: int,
    slot_of: list[int] | None = None,
) -> tuple[list[MontageEntry], str | None]:
    """Merge adjacent calm-on-calm slots into longer shots (deterministic).

    The grid cuts phases/sections at a musical density, but a slow scene
    (getting ready, a quiet landscape) must not be chopped at the same
    rate as a fast one — content decides, not the metronome alone. When
    two or more ADJACENT slots on calm music (slot energy at/below
    :data:`_MERGE_MAX_SLOT_ENERGY`) are cast with calm material (motion
    at/below :data:`_MERGE_CALM_MOTION` of the pool's fastest, highlight
    at/below :data:`_MERGE_CALM_HIGHLIGHT`), the later entries are
    dropped and the first one's record AND source windows extend over
    them — the remaining cut still lands exactly on a later grid cut, so
    every boundary stays musical.

    Hard rules, in the order they gate a merge:

    * no motion signal anywhere in the pool = calmness unknowable = no
      merges (plans from motionless reports stay byte-identical);
    * ``entries[0..protected-1]`` (the editor's arrangement) and drop
      slots never take part — explicit choices and the drop hit stay;
    * the climax phase never merges (belt and braces on top of its 1.0
      energy), and a merge never crosses a phase or section-label change
      — so it can never swallow an act boundary or a smash-to-black dip;
    * the LAST entry is never absorbed (it is the cast closer/loop shot);
    * the merged shot stays at/below :data:`_MAX_CUT_SECONDS`;
    * the absorber's own sifted moment must really HAVE the extra
      material (the source never extends past ``moment.end``), and the
      extension must not overlap material any other entry plays — the
      zero-repeat promise survives; otherwise the split is kept.

    ``slot_of`` maps each entry to the slot it STARTS in (identity when
    None — the fill's 1:1 tiling); the continuity merge
    (:func:`_merge_continuity`) runs first and hands its surviving map
    through, so slot energies and drop slots stay correctly addressed.

    Returns ``(entries, note)`` — the note ("pacing: N calm slots merged
    into M longer shots ...") is None when nothing merged.
    """
    n = len(entries)
    if n < 2:
        return entries, None
    if slot_of is None:
        slot_of = list(range(n))
    mags = [
        (math.hypot(*it.moment.entry_motion) + math.hypot(*it.moment.exit_motion)) / 2.0
        for it in pool
    ]
    peak = max(mags, default=0.0)
    if peak <= _EPS:
        return entries, None  # no motion data anywhere: calmness unknowable

    # Back-match each entry to the pool moment it was cast from (largest
    # source overlap in the same clip) — the calm signals live there.
    infos: list[tuple[Moment, bool] | None] = []
    for entry in entries:
        best = _match_pool_moment(entry, pool)
        if best is None:
            infos.append(None)
        else:
            calm = (
                mags[best] / peak <= _MERGE_CALM_MOTION + _EPS
                and pool[best].moment.highlight <= _MERGE_CALM_HIGHLIGHT + _EPS
            )
            infos.append((pool[best].moment, calm))

    def context(i: int) -> str:
        return _boundary_context(entries, i, phases, sections)

    def blocked(i: int) -> bool:
        return (
            i < protected
            or slot_of[i] in drop_slots
            or (phases and _phase_label_at(phases, entries[i].record_start) == "climax")
        )

    def calm(i: int) -> bool:
        return (
            infos[i] is not None
            and infos[i][1]
            and slot_energies[slot_of[i]] <= _MERGE_MAX_SLOT_ENERGY + _EPS
        )

    result: list[MontageEntry] = []
    merged_slots = 0
    merged_groups = 0
    i = 0
    while i < n:
        entry = entries[i]
        if blocked(i) or not calm(i):
            result.append(entry)
            i += 1
            continue
        moment = infos[i][0]  # type: ignore[index] — calm(i) guarantees infos[i]
        ctx = context(i)
        absorbed: set[int] = {i}
        new_record_end = entry.record_end
        new_source_end = entry.source_end
        j = i + 1
        while (
            j < n - 1  # the last entry is never absorbed (the cast closer)
            and not blocked(j)
            and calm(j)
            and context(j) == ctx
            and abs(entries[j].record_start - new_record_end) <= _EPS
            and entries[j].record_end - entry.record_start <= _MAX_CUT_SECONDS + _EPS
        ):
            want_end = entry.source_start + (entries[j].record_end - entry.record_start)
            if want_end > moment.end + _EPS:
                break  # the moment does not have the material: keep the split
            if _extension_overlaps(
                entries, entry.clip_path, new_source_end, want_end, absorbed | {j}
            ):
                break  # extending would repeat material another slot plays
            absorbed.add(j)
            new_record_end = entries[j].record_end
            new_source_end = want_end
            j += 1
        if len(absorbed) > 1:
            result.append(
                replace(entry, record_end=new_record_end, source_end=new_source_end)
            )
            merged_slots += len(absorbed)
            merged_groups += 1
        else:
            result.append(entry)
        i = j if len(absorbed) > 1 else i + 1

    if not merged_groups:
        return entries, None
    note = (
        f"pacing: {merged_slots} calm slots merged into {merged_groups} "
        f"longer shot{'s' if merged_groups != 1 else ''} "
        "(calm scenes get room to breathe)"
    )
    return result, note


# --- arrangement (the editor's own scene order) --------------------------------


def _resolve_arrangement(
    arrangement: list, pool: list[_PoolItem]
) -> list[dict]:
    """Validate raw arrangement items against the pool — the engine's gate.

    Returns one dict per item: ``indices`` (the pool indices of that clip's
    moments), ``want`` (requested source start), ``after`` ("" or a
    :data:`ARRANGEMENT_TRANSITIONS` value) and ``sfx`` ("" or an
    :data:`ARRANGEMENT_SFX_KINDS` value). Raises ValueError with a clear,
    complete message on structural problems or unknown clips (ALL unknown
    names are listed, not just the first).
    """
    by_clip: dict[str, list[int]] = {}
    for idx, item in enumerate(pool):
        by_clip.setdefault(item.clip_path, []).append(idx)
        by_clip.setdefault(PurePath(item.clip_path).name, []).append(idx)

    items: list[dict] = []
    unknown: list[str] = []
    for n, raw in enumerate(arrangement, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"arrangement: scene {n} must be an object")
        clip = str(raw.get("clip") or "").strip()
        if not clip:
            raise ValueError(f"arrangement: scene {n} is missing 'clip'")
        try:
            want = float(raw.get("start", 0.0))
        except (TypeError, ValueError):
            raise ValueError(
                f"arrangement: scene {n} needs a numeric 'start' (seconds)"
            )
        after_raw = raw.get("after")
        if isinstance(after_raw, dict):
            after = str(after_raw.get("transition") or "")
        else:
            after = str(after_raw or "")
        if after and after not in ARRANGEMENT_TRANSITIONS:
            valid = ", ".join(ARRANGEMENT_TRANSITIONS)
            raise ValueError(
                f"arrangement: scene {n} has unknown transition {after!r}; "
                f"valid: {valid}"
            )
        sfx_kind = str(raw.get("sfx") or "")
        if sfx_kind and sfx_kind not in ARRANGEMENT_SFX_KINDS:
            valid = ", ".join(ARRANGEMENT_SFX_KINDS)
            raise ValueError(
                f"arrangement: scene {n} has unknown sfx {sfx_kind!r}; "
                f"valid: {valid}"
            )
        indices = by_clip.get(clip) or by_clip.get(PurePath(clip).name)
        if not indices:
            unknown.append(clip)
            indices = []
        items.append(
            {"clip": clip, "want": want, "after": after, "sfx": sfx_kind,
             "indices": indices}
        )
    if unknown:
        names = ", ".join(repr(PurePath(c).name) for c in dict.fromkeys(unknown))
        raise ValueError(f"arrangement: no clip named {names} in the footage")
    return items


def _cast_arrangement(
    items: list[dict],
    pool: list[_PoolItem],
    slots: list[tuple[float, float]],
    drop_slots: set[int] | frozenset[int],
    duration: float,
) -> tuple[list[MontageEntry], dict[int, _PoolItem], list[str]]:
    """Place the arranged scenes onto slots 0..k-1 (the user's order).

    ``k = min(len(items), len(slots))``. Each item is matched to the
    best-overlapping moment of its clip (the director's matcher: largest
    overlap with ``[start, start + slot]``, then nearest centre, then
    higher score), snapped into the moment and trimmed/padded to the
    slot's exact duration with the fill's own rules. Consumption is
    recorded on the pool item so the auto-fill never replays the placed
    piece first. Returns the entries, the slot -> pool-item map (the
    fill's ``preset``) and the consistency notes: trims onto the grid,
    a calm scene on the drop, same-scene adjacency, unplaced excess.
    """
    k = min(len(items), len(slots))
    entries: list[MontageEntry] = []
    preset: dict[int, _PoolItem] = {}
    notes: list[str] = []
    # Is there any lively material at all? Only then is "calm on the
    # drop" a real flag rather than a statement about the whole pool.
    lively_exists = any(
        (math.hypot(*it.moment.entry_motion) + math.hypot(*it.moment.exit_motion)) / 2.0
        > _MOTION_MIN_MAGNITUDE
        or it.moment.highlight >= _ARR_CALM_HIGHLIGHT
        for it in pool
    )
    for i in range(k):
        item = items[i]
        rec_start, rec_end = slots[i]
        slot_len = rec_end - rec_start
        want = item["want"]
        centre = want + slot_len / 2.0
        best = min(
            item["indices"],
            key=lambda idx: (
                -max(
                    0.0,
                    min(pool[idx].moment.end, want + slot_len)
                    - max(pool[idx].moment.start, want),
                ),
                abs((pool[idx].moment.start + pool[idx].moment.end) / 2.0 - centre),
                -pool[idx].moment.score,
                idx,
            ),
        )
        pick = pool[best]
        moment = pick.moment
        # Snap into the moment, duration-preserving; pad toward the clip's
        # end when the moment tail is short — the fill's own rules.
        src_start = min(max(want, moment.start), max(moment.start, moment.end - slot_len))
        src_start = max(0.0, src_start)
        src_end = min(src_start + slot_len, moment.end)
        if src_end - src_start < slot_len - _EPS:
            src_end = max(src_end, min(src_start + slot_len, pick.clip_duration))
        if src_end - src_start < slot_len - _EPS:
            notes.append(
                f"gap at {rec_start:.2f}s: only {src_end - src_start:.2f}s of "
                f"source for a {slot_len:.2f}s slot"
            )
        pick.consumed = max(pick.consumed, src_end - moment.start)
        pick.uses += 1
        preset[i] = pick
        entries.append(
            MontageEntry(
                clip_path=pick.clip_path,
                source_start=src_start,
                source_end=src_end,
                record_start=rec_start,
                record_end=rec_end,
                score=moment.score,
                media_start=pick.media_start,
                clip_duration=pick.clip_duration,
                label=pick.label,
            )
        )
        moment_len = moment.end - moment.start
        placed = src_end - src_start
        if moment_len > placed + _ARR_TRIM_NOTE_MIN:
            notes.append(
                f"arrangement: scene {i + 1} trimmed {moment_len:.1f}s -> "
                f"{placed:.1f}s to sit on the beat grid"
            )
        if i in drop_slots and lively_exists:
            motion = (
                math.hypot(*moment.entry_motion) + math.hypot(*moment.exit_motion)
            ) / 2.0
            if (
                motion <= _MOTION_MIN_MAGNITUDE
                and moment.highlight < _ARR_CALM_HIGHLIGHT
            ):
                notes.append(
                    f"arrangement: scene {i + 1} is a calm moment on the drop "
                    "— a high-energy scene would hit harder"
                )
    for i in range(k - 1):
        a, b = preset[i], preset[i + 1]
        if a.group and a.group == b.group:
            notes.append(
                f"arrangement: scenes {i + 1} and {i + 2} are takes of the "
                "same scene back to back"
            )
    order_note = f"arrangement: {k} of {len(slots)} slots follow your order"
    if k < len(slots):
        order_note += f"; the remaining {len(slots) - k} filled automatically"
    notes.insert(0, order_note)
    if len(items) > len(slots):
        excess = len(items) - len(slots)
        notes.append(
            f"arrangement: {excess} scene{'s' if excess != 1 else ''} did not "
            f"fit the {duration:.0f}s target — raise the length or drop scenes"
        )
    return entries, preset, notes


def _arrangement_boundaries(
    plan: MontagePlan,
    entries: list[MontageEntry],
    items: list[dict],
    k: int,
) -> None:
    """Apply the arranged "after" requests to the finished plan (in place).

    Runs AFTER :func:`_plan_finishing`, so the requests override the
    style's own habits at exactly the user's boundaries: ``"cut"`` forces
    a hard cut even in a dissolve-happy phase, ``"dissolve"`` dissolves
    into the next slot (the existing length rule: min 0.5 s, half the
    slot), ``"smash"`` dips to black exactly like a style's act change
    (skipped without a note only when the slot is too short to give up
    the dip, or a dip already sits on that boundary). One summary note
    counts what was applied.
    """
    cuts = dissolves = smashes = 0
    # Blueprint 1.7: the arrangement's boundaries quantize through the
    # same shared helpers as the planner's own finishing — a user-asked
    # dissolve/smash is indistinguishable from a planned one.
    dip_len = _dip_seconds(plan)
    remainder_floor = _dip_min_remainder(plan)
    for i in range(min(k, len(entries) - 1)):
        after = items[i]["after"]
        if not after:
            continue
        incoming = entries[i + 1]
        if after == "cut":
            incoming.transition = 0.0
            cuts += 1
        elif after == "dissolve":
            incoming.transition = _dissolve_seconds(plan, incoming)
            dissolves += 1
        elif after == "smash":
            outgoing = entries[i]
            if any(abs(ds - outgoing.record_end) <= 0.25 + _EPS for ds, _ in plan.dips):
                continue  # the style already dipped this boundary
            slot = outgoing.record_end - outgoing.record_start
            if slot - dip_len < remainder_floor:
                plan.notes.append(
                    f"arrangement: scene {i + 1} is too short for a smash to "
                    "black — kept the straight cut"
                )
                continue
            outgoing.record_end -= dip_len
            outgoing.source_end -= dip_len
            plan.dips.append((outgoing.record_end, dip_len))
            smashes += 1
    if plan.dips:
        plan.dips.sort(key=lambda d: d[0])
    pieces = [
        f"{count} {word}"
        for count, word in (
            (cuts, "forced cuts" if cuts != 1 else "forced cut"),
            (dissolves, "dissolves" if dissolves != 1 else "dissolve"),
            (smashes, "smashes to black" if smashes != 1 else "smash to black"),
        )
        if count
    ]
    if pieces:
        plan.notes.append("arrangement: boundaries — " + ", ".join(pieces))


def _arrangement_cues(
    plan: MontagePlan,
    slots: list[tuple[float, float]],
    items: list[dict],
    k: int,
) -> None:
    """Add the arranged "sfx" boundary cues to the plan (in place).

    Runs AFTER :func:`_plan_sfx`, so a cue the SFX layer already planned
    at that boundary (same kind within ``_ARR_CUE_CLEARANCE``) is never
    doubled. An impact hits ON the boundary, a whoosh centers on it, a
    riser ENDS exactly on it — the same shapes the SFX layer plans. Cues
    are marker/file cues like any other (:mod:`monteur.elements` files
    them when a sound library is given); the note counts what was added.
    """
    added = 0
    for i in range(k):
        kind = items[i]["sfx"]
        if not kind:
            continue
        boundary = slots[i][1]
        if boundary >= plan.duration - _EPS:
            continue  # the montage's end is no boundary to accent
        if kind == "impact":
            time = boundary
            length = min(_SFX_IMPACT_LENGTH, plan.duration - boundary)
        elif kind == "whoosh":
            time = max(0.0, boundary - _SFX_WHOOSH_LENGTH / 2.0)
            length = _SFX_WHOOSH_LENGTH
        else:  # riser: build out of the arranged slot, ending on the boundary
            length = min(_SFX_RISER_MAX, slots[i][1] - slots[i][0])
            time = max(0.0, boundary - length)
        if length <= _EPS:
            continue
        if any(
            cue.kind == kind and abs(cue.time - time) < _ARR_CUE_CLEARANCE - _EPS
            for cue in plan.sfx
        ):
            continue  # the SFX layer already covers this boundary
        plan.sfx.append(
            SfxCue(
                time=time,
                duration=length,
                kind=kind,
                query=_ARR_SFX_QUERIES[kind],
                note=f"your arrangement — after scene {i + 1}",
            )
        )
        added += 1
    if added:
        plan.sfx.sort(key=lambda c: c.time)
        plan.notes.append(
            f"arrangement: {added} sound cue{'s' if added != 1 else ''} at "
            "your boundaries"
        )


# --- adaptive music window (the tool decides when the music enters) -------------
#
# Field feedback: "the song always plays over the full length — in a trailer
# it might only start in part 2, and the TOOL must decide, not a per-style
# rule". The decision is a SCORE, never a rigid rule:
#
#   delay_pull = _WINDOW_INTRO_PULL[intro label] x _WINDOW_STYLE_OPENNESS[style]
#   music enters late  iff  delay_pull >= _WINDOW_THRESHOLD  (and a musical
#   candidate exists)
#
# Scoring table (the intro label comes from monteur.music.intro_profile,
# measured at the cut's own source window):
#
#   intro \ style   trailer  music_video  auto   travel  wedding  short
#   ambient (0.0)     0.00      0.00      0.00    0.00     0.00    0.00
#   moderate (0.4)    0.36      0.30      0.24    0.24     0.22    0.00
#   hard (1.0)        0.90      0.75      0.60    0.60     0.55    0.00
#
# Threshold 0.5: an AMBIENT intro starts at 0 under every style (even a
# trailer's cold open earns nothing from silencing an ambient pad), a HARD
# intro is delayed everywhere except "short" (60 seconds has no room for a
# dry open — the one absolute in the table) — including the calm styles,
# where the delay is the mismatch penalty (hard music slamming over a calm
# opening reads wrong). MODERATE intros always start at 0. Styles absent
# from the table weigh in at the "auto" openness. Candidates are musical:
# 0.0, the first act boundary and the build start, each snapped to the
# nearest downbeat (falling back to phrase starts, then beats) in RECORD
# time; the arc-less "auto" style has no boundaries and always stays at 0.

_WINDOW_INTRO_PULL = {"ambient": 0.0, "moderate": 0.4, "hard": 1.0}
_WINDOW_STYLE_OPENNESS = {
    "trailer": 0.9,
    "music_video": 0.75,
    "auto": 0.6,
    "travel": 0.6,
    "wedding": 0.55,
    "short": 0.0,
}
_WINDOW_THRESHOLD = 0.5
# A music entry earlier than this is not worth the move — start at 0.
_WINDOW_MIN_IN = 2.0
# Deliberate silence (music gaps; see the module docstring's section).
# How the song relates to the cut's dramatic breaks: "deliberate" (default)
# plans music_gaps under carried dips and before the drop, "continuous"
# keeps the song running through everything (zero gaps, byte-identical
# plans to before the field existed).
MUSIC_FLOW_MODES = ("deliberate", "continuous")
# A dip's silence ends on the FOLLOWING downbeat when one lies within this
# many beats past the dip end (re-entry lands musically, not mid-bar);
# further away, the song re-enters right at the cut out of the black.
_GAP_DOWNBEAT_EXTEND_BEATS = 1.0
# The pre-drop breath: exactly this many beats of silence, re-entry ON the
# drop. Never more — a longer hole before the hit reads like an error.
_PRE_DROP_GAP_BEATS = 1.0
# Accidental-silence guard: a dip gap needs a CARRIER — a planned cue of
# one of these kinds whose window comes within this many seconds of the
# dip window (a marker cue counts; monteur.elements files it later).
_GAP_CARRIER_KINDS = frozenset({"sub-drop", "impact"})
_GAP_CARRIER_TOLERANCE = 0.5
# The music must enter within the first half of the cut, whatever the arc says.
_WINDOW_MAX_SHARE = 0.5
# Candidates snap to the nearest downbeat/phrase/beat within this (seconds).
_WINDOW_SNAP = 1.5


def _snap_record_time(
    music: MusicAnalysis, t: float, *, music_start: float = 0.0, limit: float | None = None
) -> tuple[float, str]:
    """Snap a RECORD-time position to the nearest downbeat/phrase/beat.

    Musical positions live in song time; record time shifts by
    ``music_start``. Returns ``(snapped, kind)`` — the original ``t`` with
    kind ``""`` when no grid point lies within :data:`_WINDOW_SNAP`.
    """
    hi = limit if limit is not None else music.duration - music_start
    for cand, kind in (
        (music.downbeats, "downbeat"),
        (music.phrases, "phrase"),
        (music.beats, "beat"),
    ):
        pts = sorted(p - music_start for p in cand if _EPS < p - music_start < hi - _EPS)
        if not pts:
            continue
        nearest = _nearest(pts, t)
        if abs(nearest - t) <= _WINDOW_SNAP + _EPS:
            return nearest, kind
        # This grid exists but its nearest point is too far — try a denser one.
    return t, ""


def music_window_candidates(
    music: MusicAnalysis,
    phases: list[tuple[float, float, str]],
    *,
    music_start: float = 0.0,
) -> list[dict]:
    """The musical positions where the music could enter, in RECORD time.

    Always contains ``{"time": 0.0, "label": "with the first frame"}``.
    With arc ``phases``: the first act boundary and the build start (they
    coincide on the standard arcs), each snapped to the nearest downbeat
    (fallback: phrase starts, then beats) and kept only when it lands at
    or after :data:`_WINDOW_MIN_IN` and inside the first
    :data:`_WINDOW_MAX_SHARE` of the cut. Deterministic and duplicate-free;
    the composer sees exactly this list in its dossier.
    """
    candidates: list[dict] = [{"time": 0.0, "label": "with the first frame", "snap": ""}]
    if not phases:
        return candidates
    duration = phases[-1][1]
    raw: list[tuple[float, str]] = []
    build = next((s for s, _e, lab in phases if lab == "build"), None)
    if build is not None and build > _EPS:
        raw.append((build, "build start"))
    if len(phases) > 1:
        raw.append((phases[0][1], "first act boundary"))
    seen: list[float] = [0.0]
    for t, label in raw:
        snapped, kind = _snap_record_time(
            music, t, music_start=music_start, limit=duration
        )
        if snapped < _WINDOW_MIN_IN - _EPS or snapped > duration * _WINDOW_MAX_SHARE + _EPS:
            continue
        if any(abs(snapped - s) <= 0.25 for s in seen):
            continue
        seen.append(snapped)
        candidates.append({"time": snapped, "label": label, "snap": kind})
    return candidates


def decide_music_window(
    music: MusicAnalysis,
    style: str,
    phases: list[tuple[float, float, str]],
    *,
    music_start: float = 0.0,
) -> tuple[float, str]:
    """When should the music enter? Returns ``(music_in, note)``.

    Implements the scoring table above: the song's own opening character
    (:func:`monteur.music.intro_profile`, measured at ``music_start`` —
    the cut's source window) sets the pull toward a delayed entry, the
    style weighs it, and only a score at/above :data:`_WINDOW_THRESHOLD`
    delays the music — onto the build start (preferred) or the first act
    boundary from :func:`music_window_candidates`. ``(0.0, "")`` means
    the music plays from the first frame; the note (only for a delayed
    entry) narrates the decision.
    """
    candidates = [
        c for c in music_window_candidates(music, phases, music_start=music_start)
        if c["time"] > _EPS
    ]
    if not candidates:
        return 0.0, ""
    profile = intro_profile(music, start=music_start)
    pull = _WINDOW_INTRO_PULL.get(profile["label"], 0.4) * _WINDOW_STYLE_OPENNESS.get(
        style, _WINDOW_STYLE_OPENNESS["auto"]
    )
    if pull < _WINDOW_THRESHOLD - _EPS:
        return 0.0, ""
    chosen = next((c for c in candidates if c["label"] == "build start"), candidates[0])
    where = chosen["label"] + (f", snapped to {chosen['snap']}" if chosen["snap"] else "")
    note = (
        f"music enters at {chosen['time']:.1f}s ({where}): the song opens "
        f"{profile['label']}, the cut opens dry"
    )
    return float(chosen["time"]), note


# Ending the music early (decide_music_out): only when the song's tail
# under the cut is CLEARLY limp — a trailing "low"-energy stretch whose
# outro profile reads "ambient" — and only conservatively: the music must
# keep at least this share of the montage, the cut must save at least
# _OUT_MIN_SAVING seconds, and only arc'd styles (an outro exists to close
# on the picture; "auto" and "short" never end the music early).
_OUT_MIN_SHARE = 0.7
_OUT_MIN_SAVING = 2.0


def decide_music_out(
    music: MusicAnalysis,
    style: str,
    phases: list[tuple[float, float, str]],
    duration: float,
    *,
    music_in: float = 0.0,
) -> tuple[float, str]:
    """Should the music end before the picture? Returns ``(music_out, note)``.

    The outro sibling of :func:`decide_music_window`, deliberately
    conservative: only when the song's ending under the cut is a long
    ambient fade the cut should not drag through. ``music`` is the
    record-time (windowed) analysis; ``duration`` the montage length.
    Conditions, all required:

    * the style is arc'd with an outro (the trailer/travel/wedding/
      music_video family — never "auto", never "short");
    * :func:`monteur.music.outro_profile` measured at the cut's end
      window reads ``"ambient"`` — the tail is clearly limp;
    * the trailing "low"-energy stretch reaches the montage end and its
      start lies inside the outro phase, at/after
      :data:`_OUT_MIN_SHARE` of the montage, after ``music_in``, and
      saves at least :data:`_OUT_MIN_SAVING` seconds;
    * the cut point snaps to a downbeat/phrase/beat when one is near
      (:func:`_snap_record_time`).

    ``(0.0, "")`` means the music plays to the end — the default and the
    overwhelmingly common case.
    """
    from monteur.music import outro_profile

    arc_style = STYLES.get(style)
    if arc_style is None or "outro" not in [lab for _, lab in arc_style.arc]:
        return 0.0, ""
    if duration <= _EPS or not music.sections:
        return 0.0, ""
    profile = outro_profile(music, end=duration)
    # "Clearly limp" is BOTH verdicts: the ambient label AND a tail whose
    # pulse has died (a quiet tail that still drives beats is an outro
    # groove, not a fade — the cut rides it out).
    if profile["label"] != "ambient" or profile["onset_density"] >= 1.0 - _EPS:
        return 0.0, ""
    # The trailing "low" run under the cut: where the song goes limp.
    limp: float | None = None
    for section in music.sections:
        if section.start >= duration - _EPS:
            break
        if section.label == "low":
            if limp is None:
                limp = section.start
        else:
            limp = None
    if limp is None or limp <= _EPS:
        return 0.0, ""  # no limp tail, or the whole cut is quiet — leave it
    outro_start = next((s for s, _e, lab in phases if lab == "outro"), None)
    if outro_start is None or limp < outro_start - _EPS:
        return 0.0, ""
    snapped, _kind = _snap_record_time(music, limp, music_start=0.0, limit=duration)
    # Snap only FORWARD (or in place): ending the music before the limp
    # start would clip a still-hot note — a moment of limp tail playing
    # past the musical boundary is harmless, the reverse is not.
    out = snapped if snapped >= limp - _EPS else limp
    if out < max(outro_start, _OUT_MIN_SHARE * duration) - _EPS:
        return 0.0, ""
    if duration - out < _OUT_MIN_SAVING - _EPS or out <= music_in + _EPS:
        return 0.0, ""
    note = (
        f"music ends at {out:.1f}s: the song's tail is a long ambient "
        "fade — the cut closes on the picture"
    )
    return float(out), note


def _loop_seam_start(
    music: MusicAnalysis, length: float, start: float
) -> tuple[float, float, str]:
    """Shift a short's song window so its END sits on a phrase boundary.

    Loop seam (blueprint 1.5): a short loops — the window's last note
    wraps back to its first, and that wrap only connects MUSICALLY when
    the window ends where a phrase ends (the next phrase would begin,
    i.e. the music resolves back toward a section head — the same place
    the window start lives near). The window keeps its length (the cut's
    duration is untouched); only ``music_start`` shifts, by at most
    ±:data:`_END_SNAP_TOLERANCE` (12%) of the length. Falls back from
    phrase starts to downbeats; the shift is refused (returns ``start``
    unchanged, kind ``""``) when no boundary is near enough or when it
    would push the window's BEST drop out of the pinnable 5–95% range —
    the drop pin and the seam are co-designed with
    :data:`monteur.music._WINDOW_DROP_LEAD`, and the pin wins.

    Returns ``(new_start, seam_point_in_song_time, boundary_kind)``.
    """
    end = start + length
    tolerance = _END_SNAP_TOLERANCE * length
    max_start = max(0.0, music.duration - length)
    drop = best_drop(music, sorted(music.drops)) if music.drops else None
    for cand, kind in ((music.phrases, "phrase"), (music.downbeats, "downbeat")):
        pts = sorted(p for p in cand if _EPS < p <= music.duration + _EPS)
        best: float | None = None
        for p in pts:
            d = abs(p - end)
            if d > tolerance + _EPS:
                continue
            if not (0.0 - _EPS <= p - length <= max_start + _EPS):
                continue
            if drop is not None:
                rec = drop - (p - length)  # the drop in the shifted record time
                if not (
                    _DROP_ALIGN_MARGIN * length
                    <= rec
                    <= (1 - _DROP_ALIGN_MARGIN) * length
                ):
                    continue  # the seam must not cost the drop pin
            if best is None or d < abs(best - end) - _EPS:
                best = p  # ties keep the earlier boundary (checked in order)
        if best is not None:
            if abs(best - end) <= _EPS:
                return start, best, kind  # already seated on the boundary
            return min(max(best - length, 0.0), max_start), best, kind
    return start, 0.0, ""


# Reuse detection (repeats off): two entries share material when their
# source windows on the same clip overlap by at least this share of the
# shorter window — identical picks overlap fully, padding slivers don't.
_REUSE_OVERLAP_SHARE = 0.5


def _shares_material(a: "MontageEntry", b: "MontageEntry") -> bool:
    """True when two entries put (near-)identical frames on screen twice.

    Same clip and the source windows overlap by at least
    :data:`_REUSE_OVERLAP_SHARE` of the shorter window. Used by the
    composer's and the revision's zero-repeat enforcement.
    """
    if a.clip_path != b.clip_path:
        return False
    ov = min(a.source_end, b.source_end) - max(a.source_start, b.source_start)
    shorter = min(a.source_end - a.source_start, b.source_end - b.source_start)
    return shorter > _EPS and ov >= _REUSE_OVERLAP_SHARE * shorter - _EPS


def _find_unused_window(
    reports: list[ClipReport],
    used: list[tuple[str, float, float]],
    needed: float,
    min_piece: float = MIN_CUT_INTERVAL,
) -> tuple[ClipReport, Moment, float, float] | None:
    """First unused span of sifted moment material — the re-source helper.

    ``used`` lists (clip_path, source_start, source_end) windows already
    on screen. Walking the reports and their moments in order, the moment
    spans minus the used windows yield free gaps; the FIRST gap at least
    ``needed`` seconds long wins. When none is big enough, the longest
    gap of at least ``min_piece`` seconds is returned instead (the caller
    keeps the record slot and takes the shorter source — the fill's own
    gap semantics). Returns ``(report, moment, start, length)`` with
    ``length <= needed``, or None when no usable span remains.
    Deterministic; used by the composer's and the revision's zero-repeat
    enforcement (repeats off must survive recasting and region splices).
    """
    best: tuple[float, ClipReport, Moment, float] | None = None
    for report in reports:
        clip_used = sorted(
            (lo, hi) for c, lo, hi in used if c == report.path and hi - lo > _EPS
        )
        for moment in report.moments:
            gaps: list[tuple[float, float]] = []
            cursor = moment.start
            for lo, hi in clip_used:
                if hi <= cursor + _EPS or lo >= moment.end - _EPS:
                    continue
                if lo > cursor + _EPS:
                    gaps.append((cursor, lo))
                cursor = max(cursor, hi)
            if moment.end > cursor + _EPS:
                gaps.append((cursor, moment.end))
            for lo, hi in gaps:
                length = hi - lo
                if length >= needed - _EPS:
                    return report, moment, lo, needed
                if length >= min_piece - _EPS and (
                    best is None or length > best[0] + _EPS
                ):
                    best = (length, report, moment, lo)
    if best is not None:
        return best[1], best[2], best[3], best[0]
    return None


def _shorten_no_repeats(
    plan: MontagePlan,
    entries: list[MontageEntry],
    music: MusicAnalysis,
    short_at: float,
    n_moments: int,
) -> list[MontageEntry]:
    """Cut the plan where fresh material ran out (no-repeats truncation).

    ``short_at`` is the record start of the first slot the fill could not
    serve without repeating footage. The montage ends there — every slot
    boundary already sits on the musical grid — and additionally snaps
    DOWN onto a phrase/downbeat/beat via :func:`_snap_ending_length` when
    one lies within tolerance below (never up: there is no material past
    the cut). Entries at/after the cut are dropped, an entry straddling
    it is trimmed 1:1 in source, and the plan's duration, phases and
    strip metadata shrink to match; the honest note names the deal.
    """
    cut = short_at
    snapped, _kind = _snap_ending_length(music, cut)
    if snapped is not None and snapped < cut - _EPS:
        survivors = [e for e in entries if e.record_start < snapped - _EPS]
        if survivors and snapped - survivors[-1].record_start >= MIN_CUT_INTERVAL - _EPS:
            cut = snapped
    kept = [e for e in entries if e.record_start < cut - _EPS]
    if kept and kept[-1].record_end > cut + _EPS:
        last = kept[-1]
        delta = last.record_end - cut
        last.record_end = cut
        last.source_end = max(last.source_start, last.source_end - delta)
    if (
        kept
        and kept[-1].record_end - kept[-1].record_start < _MIN_SLOT_SECONDS - _EPS
    ):
        # Sliver floor (blueprint 1.7): the straddle trim must not leave a
        # sub-floor tail shot — the montage ends at the previous boundary
        # (still a grid cut) instead of on a glitch-length final frame.
        cut = kept[-1].record_start
        kept.pop()
    plan.duration = cut
    plan.phases = [
        (s, min(e, cut), lab) for s, e, lab in plan.phases if s < cut - _EPS
    ]
    if plan.music_energy:
        plan.music_energy = plan.music_energy[
            : int(math.floor(cut * MUSIC_ENERGY_RATE)) + 1
        ]
    plan.beat_marks = [t for t in plan.beat_marks if t <= cut + _EPS]
    plan.drop_marks = [t for t in plan.drop_marks if t <= cut + _EPS]
    plan.notes.append(
        f"length reduced to {cut:.1f}s: {n_moments} distinct moments, "
        "no repeats allowed — shoot more or allow repeats"
    )
    return kept


# --- public API ---------------------------------------------------------------


def plan_montage(
    reports: list[ClipReport],
    music: MusicAnalysis | None = None,
    order: str = CHRONOLOGICAL,
    max_duration: float | None = None,
    style: str = "auto",
    end_on_phrase: bool = True,
    allow_repeats: bool = False,
    cut_lead: float = _DEFAULT_CUT_LEAD,
    pace: float | None = None,
    transitions: str = "auto",
    *,
    sfx: bool = False,
    arrangement: list[dict] | None = None,
    music_window: tuple[float, float] | list[float] | None = None,
    music_flow: str = "deliberate",
    fps: float | None = None,
    casting_bias: "CastingBias | None" = None,
) -> MontagePlan:
    """Distribute the best moments across the song, in a cutting style.

    ``style`` selects a :data:`STYLES` entry. "auto" (the default) keeps the
    section-energy beat grid; a named style cuts on its story arc instead
    (see the module docstring for the grid, drop, highlight and motion
    rules). Unknown styles raise ValueError listing the valid ones.

    Moments annotated by :mod:`monteur.vision` (role / hero / group / label)
    steer the fill — roles gravitate to their arc phases, hero shots to the
    drop, same-scene takes apart (the module docstring's Semantic casting
    section has the weights). Unannotated moments plan exactly as before.

    ``music=None`` plans a cut with no song at all (ride-POV videos whose
    own sound is the point): ``max_duration`` is then required, the grid
    uses fixed per-phase intervals (``beats_per_cut x _PSEUDO_BEAT``; see
    :func:`_build_pseudo_grid`) and the plan carries ``music_path`` "" —
    render it with ``montage_to_timeline(..., audio="original")``.

    ``allow_repeats`` (default False) controls the repetition guard —
    False is a promise of ZERO repeated moments: a montage longer than
    the deduplicated moment material is capped to exactly that material
    (shorter cut, honest note), overlapping pool moments are trimmed so
    no two claim the same frames, and the fill never rewinds — should it
    still run dry, the grid is cut at the last fillable slot instead
    (see the module docstring's Repetition guard section). The cap runs
    before the phrase snap and the strongest-window choice, never
    lengthens the montage, and never applies when the request is already
    below it. ``allow_repeats=True`` plans the full requested length and
    repeats footage knowingly, exactly as before.

    ``pace`` (seconds, optional) is an OVERRIDE on how fast the montage
    cuts: the approximate clip length of the FASTEST phase, rounded to
    whole beats; slower phases scale proportionally, so the style's arc
    dynamics are kept. ``None`` (the default, recommended) is Auto: the
    engine derives the pace from the music, the footage and the local
    tempo — arc styles additionally bias their bases slower on
    calm-dominated material and quiet songs (see the module docstring's
    Auto pace section). Values that are not positive raise ValueError.
    The anti-strobe floor (:data:`MIN_CUT_INTERVAL`) still applies to
    very small paces.

    ``transitions`` picks how clips hand over (:data:`TRANSITION_MODES`):
    ``"auto"`` (default, recommended) decides PER CUT from the content —
    same-clip continuations and climax/"high" passages cut hard,
    daylight-block changes and scene changes in calm passages dissolve,
    and the trailer still smashes to black at act changes (the module
    docstring's Finishing section has the full matrix). The explicit
    modes are overrides: ``"cuts"`` is hard cuts only; ``"dissolves"``
    dissolves on every cut; ``"smash"`` forces black title-slot gaps at
    act changes (for "auto" style: at the song's section changes).
    Unknown values raise ValueError listing the four.

    ``cut_lead`` (default 0.04 s, ~1 frame at 25 fps; 0 disables) shifts
    every interior cut earlier so the incoming shot lands ON the beat
    instead of starting there — cuts exactly on the beat read late (see
    :func:`_apply_cut_lead` for the clamping rules). ``fps``
    (keyword-only, default None) makes the lead FPS-AWARE (blueprint
    1.7, resolved through :func:`cut_lead_for` — the one shared
    decision): with a known delivery rate the default lead is exactly
    one frame (``1/fps`` — byte-identical at 25 fps) and an explicit
    ``cut_lead`` is quantized to whole frames; ``None`` keeps the 0.04 s
    seconds approximation. Dissolving boundaries take NO lead either way
    (dissolve lead 0, blueprint 1.7): a dissolve is a ramp ACROSS the
    beat, so after :func:`_plan_finishing` decides the dissolves each
    dissolving boundary is moved back onto its unshifted grid position
    (the documented workaround for finishing running after the grid
    lead; see :func:`_undo_lead_on_dissolves`).

    ``end_on_phrase`` (default True) gives a truncated montage a musical
    ending: when the montage is shorter than the song, the length is
    snapped to the nearest phrase start (fallback: downbeats, then beats)
    within ±12% of the request — ties prefer the shorter cut, larger changes
    are never made, and a full-song montage is left alone. The plan also
    carries the intended fades (``fade_in`` / ``fade_out``) and per-entry
    dissolves for gentle phases (see the module docstring's Finishing
    section).

    ``arrangement`` (keyword-only, default None) hands the CASTING ORDER
    to the editor: an ordered list of ``{"clip", "start"}`` dicts (plus
    optional ``"after"`` transitions and ``"sfx"`` boundary cues) claims
    the slots from 0 upward in exactly that order while the grid, rhythm
    and finishing stay the engine's — see the module docstring's
    Arrangement section for matching, trimming, excess handling and the
    ``arrangement:`` consistency notes. Malformed items and unknown clips
    raise ValueError naming the problem. ``None`` is byte-identical to
    before.

    ``sfx`` (keyword-only, default False) additionally plans a sound-design
    layer: ``plan.sfx`` is filled with :class:`SfxCue` entries — ambience
    under the opening, risers into act changes, impacts on the climax/drop
    cuts, sub-drops under smash-to-black dips, whooshes on the fastest cuts
    (the module docstring's SFX layer section has the exact rules and the
    density cap). False leaves ``plan.sfx`` empty and everything else
    byte-identical to before.

    ``music_window`` (keyword-only, default None) overrides the adaptive
    music-window decision: ``(music_in, music_out)`` in RECORD seconds
    (0 = "from the first frame" / "to the montage end"). ``music_in`` is
    snapped to the nearest downbeat/phrase/beat; values outside the cut,
    a music_out at/before music_in, or a window without music raise
    ValueError. ``None`` lets :func:`decide_music_window` score it (music
    present only): an ambient opening keeps the music at 0 in every style,
    a hard opening delays it onto the build in dramatic (and, as the
    mismatch penalty, calm) styles, "short" always starts at 0 — the
    module-level scoring table has the exact numbers. A delayed entry is
    noted; a 0-entry plan is byte-identical to plans from before the
    window existed.

    ``music_flow`` (keyword-only, default ``"deliberate"``) governs the
    plan's deliberate silences (:data:`MUSIC_FLOW_MODES`): the default
    plans ``music_gaps`` — the song breaks under smash-to-black dips that
    carry a sub-drop/impact cue (re-entering on the following downbeat)
    and for exactly one beat before the first in-range drop (re-entering
    ON the hit); the module docstring's Deliberate silence section has
    the full rules and the accidental-silence guard. ``"continuous"``
    plans zero gaps and is byte-identical to plans from before the field
    existed. Unknown values raise ValueError listing both.

    ``casting_bias`` (keyword-only, default None) folds the Wave-4 casting
    tie-breakers into the cut: learned user preferences
    (:func:`monteur.preferences.casting_bias`) and the refine loop's
    grammar knob, bundled as a :class:`CastingBias`. Applied ONLY as small
    tie-breakers below one order step — never over sync, the drop, the
    rhythm order or zero-repeat. ``None`` (and a neutral bias) is
    byte-identical to before, so the default one-shot plan is unchanged
    (the empty-store / no-refine guarantee).
    """
    if style not in STYLES:
        valid = ", ".join(sorted(STYLES))
        raise ValueError(f"unknown style {style!r}; valid styles: {valid}")
    chosen = STYLES[style]
    if music is None and max_duration is None:
        raise ValueError("without music, pass max_duration")
    if pace is not None and pace <= 0:
        raise ValueError("pace must be positive (approximate seconds per clip)")
    if transitions not in TRANSITION_MODES:
        valid = ", ".join(TRANSITION_MODES)
        raise ValueError(
            f"unknown transitions {transitions!r}; valid modes: {valid}"
        )
    if arrangement is not None and not isinstance(arrangement, list):
        raise ValueError("arrangement must be a list of scene objects")
    if music_flow not in MUSIC_FLOW_MODES:
        valid = ", ".join(MUSIC_FLOW_MODES)
        raise ValueError(
            f"unknown music_flow {music_flow!r}; valid modes: {valid}"
        )
    if fps is not None:
        # Typed fps-aware lead (blueprint 1.7): one frame at the delivery
        # rate, explicit requests quantized to whole frames. fps=25 is
        # byte-identical to the classic 0.04 s approximation.
        cut_lead = cut_lead_for(
            fps, None if cut_lead == _DEFAULT_CUT_LEAD else cut_lead
        )
    window_override: tuple[float, float] | None = None
    if music_window is not None:
        if music is None:
            raise ValueError("music_window needs music — a no-music plan has no song to delay")
        try:
            w_in, w_out = float(music_window[0]), float(music_window[1])
        except (TypeError, ValueError, IndexError):
            raise ValueError(
                "music_window must be (music_in, music_out) in seconds "
                "(0 = full length)"
            )
        if w_in < 0 or w_out < 0:
            raise ValueError("music_window times must not be negative")
        if w_out > _EPS and w_out <= w_in + _EPS:
            raise ValueError(
                "music_window: music_out must lie after music_in "
                "(or be 0 for the montage end)"
            )
        window_override = (w_in, w_out)

    if music is None:
        requested = max_duration
    else:
        requested = (
            music.duration if max_duration is None else min(music.duration, max_duration)
        )

    # Repetition guard: with repeats off, the montage never outgrows the
    # distinct material — it gets shorter instead of recycling. Runs BEFORE
    # the phrase snap and best_energy_window so both refine the capped length.
    length = requested
    repeat_note: str | None = None
    unique_material = _unique_material(reports)
    if not allow_repeats and unique_material > _EPS and requested > unique_material + _EPS:
        length = unique_material
        repeat_note = (
            f"length reduced to {length:.0f}s (was {requested:.0f}s): only "
            f"{unique_material:.0f}s of distinct footage, no repeats allowed "
            "— shoot more or pass allow_repeats=True / --allow-repeats for "
            "the full length"
        )

    end_note: str | None = None
    if (
        music is not None
        and end_on_phrase
        and _EPS < length < music.duration - _EPS
    ):
        snapped_length, boundary_kind = _snap_ending_length(music, length)
        if snapped_length is not None:
            end_note = f"length snapped to {boundary_kind} at {snapped_length:.1f}s"
            length = snapped_length

    # A montage cut shorter than the song uses the song's strongest passage,
    # not its intro: shift the whole grid onto [music_start, music_start+length].
    music_start = 0.0
    seam_note: str | None = None
    if music is not None and _EPS < length < music.duration - _EPS:
        music_start = best_energy_window(music, length)
        if chosen.arc and chosen.arc[0][1] == "hook":
            # Loop seam (blueprint 1.5, "short"): the window END lands on
            # a phrase boundary so the loop's wrap connects musically —
            # the window shifts, the drop (and its _WINDOW_DROP_LEAD
            # lead-in) stays pinnable inside it.
            music_start, seam_point, seam_kind = _loop_seam_start(
                music, length, music_start
            )
            if seam_kind:
                seam_note = (
                    f"loop seam: the song window ends on the {seam_kind} "
                    f"boundary at {seam_point:.1f}s — the ending wraps "
                    "back into the hook"
                )
    if music is None:
        grid_music = MusicAnalysis(path="", duration=max(length, 0.0), tempo=0.0)
    elif music_start > _EPS:
        grid_music = _window_music(music, music_start, length)
    else:
        grid_music = music

    plan = MontagePlan(
        music_path=music.path if music is not None else "",
        duration=max(length, 0.0),
        music_start=music_start,
        song_duration=music.duration if music is not None else 0.0,
    )
    plan.notes.append(f'style "{chosen.key}": {chosen.name}')
    if repeat_note:
        plan.notes.append(repeat_note)
    if end_note:
        plan.notes.append(end_note)
    if music_start > _EPS:
        plan.notes.append(
            f"using the song's strongest {length:.0f}s (from {_mmss(music_start)})"
        )
    if seam_note:
        plan.notes.append(seam_note)
    if length <= _EPS:
        plan.notes.append("montage length is zero; nothing planned")
        return plan

    # Cut pace: scale every phase's beat step so the fastest phase cuts at
    # ~`pace` seconds per clip. pace=None is AUTO — the style's bases,
    # biased by what the footage and the song actually are
    # (_auto_pace_bias): calm-dominated material and/or a quiet song cut
    # one notch slower per signal. The arc-less "auto" style reads the
    # song's density directly (and the merge passes adapt it to content),
    # and the "short" anti-canon never slows down — neither is biased.
    auto_steps: dict[str, int] | None = None
    if pace is not None:
        beat = _pulse_interval(grid_music) if music is not None else _PSEUDO_BEAT
        chosen, auto_steps, pace_note = _apply_pace(chosen, pace, beat)
        plan.notes.append(pace_note)
    elif chosen.arc and not chosen.no_opening_hold:
        notches, bias_note = _auto_pace_bias(
            reports, grid_music.sections if music is not None else []
        )
        if notches:
            factor = 2**notches
            chosen = replace(
                chosen,
                beats_per_cut={
                    k: v * factor for k, v in chosen.beats_per_cut.items()
                },
            )
            plan.notes.append(bias_note)

    phases: list[tuple[float, float, str]] = []
    highlight_phase: str | None = None
    drop_starts: list[float] = []
    if music is None:
        cuts, phases, grid_notes = _build_pseudo_grid(length, chosen, auto_steps)
        if chosen.arc:
            highlight_phase = chosen.prefer_highlights_in
    elif chosen.arc:
        cuts, phases, grid_notes = _build_style_grid(grid_music, length, chosen)
        highlight_phase = chosen.prefer_highlights_in
        if phases and phases[0][2] == "hook":
            # Short drop pin (blueprint 1.5): the hook/punch/loop arc has
            # no climax phase to align, but the drop still deserves a cut
            # — pinned on the BEST in-range drop, which is exactly the
            # drop best_energy_window placed inside this window with its
            # _WINDOW_DROP_LEAD (15%) lead-in (co-designed: the window
            # carries the drop, the pin cuts on it, the loop seam keeps
            # both). The slot starting there is reserved for the
            # strongest moment via the drop-slot machinery, and grid cuts
            # inside ~2 beats after the pin are cleared — the hit holds,
            # exactly like "auto"'s drop-forced cuts.
            in_range = [
                d
                for d in sorted(grid_music.drops)
                if _DROP_ALIGN_MARGIN * length <= d <= (1 - _DROP_ALIGN_MARGIN) * length
            ]
            pin = best_drop(grid_music, in_range) if in_range else None
            if pin is not None:
                if not any(abs(c - pin) <= _EPS for c in cuts):
                    bisect.insort(cuts, pin)
                drop_starts.append(pin)
                hold = 2 * _pulse_interval(grid_music)
                cuts = [
                    c
                    for c in cuts
                    if c >= length - _EPS
                    or abs(c - pin) <= _EPS
                    or not (pin + _EPS < c < pin + hold - _EPS)
                ]
                grid_notes.append(
                    f"short: cut pinned on the drop at {pin:.1f}s; "
                    "strongest moment assigned"
                )
        elif phases:
            # Secondary-drop forced cuts (blueprint 2.1): a climax-bearing
            # arc style pins ONLY the climax on the best drop; the strongest
            # SECONDARY drops now force their own hard cut too. The climax
            # pin (and its 1.5 arc-squeeze floors) is untouched — it is not
            # in ``drop_starts`` and casts through the highlight phase, not
            # the drop-slot reservation. Each secondary is a HOLD like the
            # climax: the phase-hold it lands in runs UP TO the drop, cuts
            # hard on it, then re-seats — grid cuts inside ~2 beats after
            # the drop are cleared (below), and _absorb_slivers drops any
            # short remainder before it, so a running opening/climax hold is
            # cleared cleanly instead of shredded into slivers.
            climax_start = next(
                (s for s, _e, lab in phases if lab == "climax"), None
            )
            in_range = [
                d
                for d in sorted(grid_music.drops)
                if _DROP_ALIGN_MARGIN * length <= d <= (1 - _DROP_ALIGN_MARGIN) * length
            ]
            # Only when the climax actually pinned an in-range drop: that is
            # the well-defined state 2.1 describes, and it keeps styles whose
            # climax could not pin (drop out of range, climax at an edge)
            # byte-identical.
            if (
                climax_start is not None
                and in_range
                and any(abs(climax_start - d) <= _EPS for d in in_range)
            ):
                beat_s = 60.0 / grid_music.tempo if grid_music.tempo > 0 else _PSEUDO_BEAT
                secondaries = _secondary_drops(
                    grid_music, in_range, climax_start, beat_s
                )
                for d in secondaries:
                    if not any(abs(c - d) <= _EPS for c in cuts):
                        bisect.insort(cuts, d)
                    drop_starts.append(d)
                    grid_notes.append(
                        f"secondary drop at {d:.1f}s forces a cut; "
                        "strongest unused moment assigned"
                    )
                if drop_starts:
                    # Phase-hold clearing: the drop slot is a 2-beat HOLD, so
                    # grid cuts inside ~2 beats after each secondary drop are
                    # cleared (the montage end, the climax pin and other drop
                    # cuts always survive). The climax start is NOT a
                    # drop_start, so its own hold and arc-squeeze floors are
                    # left exactly as the grid built them.
                    hold = 2 * _pulse_interval(grid_music)
                    cuts = [
                        c
                        for c in cuts
                        if c >= length - _EPS
                        or any(abs(c - d) <= _EPS for d in drop_starts)
                        or not any(
                            d + _EPS < c < d + hold - _EPS for d in drop_starts
                        )
                    ]
    else:
        cuts, grid_notes = _build_grid(grid_music, length, auto_steps)
        # Auto style: every in-range drop forces a cut exactly on the drop;
        # the slot starting there is reserved for the strongest moment.
        for d in sorted({d for d in grid_music.drops if _EPS < d < length - _EPS}):
            if not any(abs(c - d) <= _EPS for c in cuts):
                bisect.insort(cuts, d)
            drop_starts.append(d)
            grid_notes.append(f"cut forced at drop {d:.1f}s; strongest moment assigned")
        if drop_starts:
            # The drop slot is a HOLD: impact needs screen time, so grid
            # cuts inside the first ~2 beats after each drop are cleared
            # (the montage end and other drop cuts always survive).
            hold = 2 * _pulse_interval(grid_music)
            cuts = [
                c
                for c in cuts
                if c >= length - _EPS
                or any(abs(c - d) <= _EPS for d in drop_starts)
                or not any(d + _EPS < c < d + hold - _EPS for d in drop_starts)
            ]
    plan.notes.extend(grid_notes)
    # Timeline-strip metadata (additive; see the field docs on MontagePlan):
    # the arc phases in record time plus a compact picture of the music —
    # smoothed section energy at MUSIC_ENERGY_RATE, downbeats and drops.
    # grid_music is already windowed onto [0, length] (record time).
    plan.phases = [(float(s), float(e), str(lab)) for s, e, lab in phases]
    if music is not None:
        plan.music_energy = _sample_energy(grid_music.sections, length)
        plan.beat_marks = [
            round(t, 2) for t in grid_music.downbeats if -_EPS <= t <= length + _EPS
        ]
        plan.drop_marks = [
            round(t, 2) for t in grid_music.drops if -_EPS <= t <= length + _EPS
        ]
        plan.tempo = round(float(getattr(grid_music, "tempo", 0.0)), 2)
    # Sliver elimination (blueprint 1.7): no generated slot under the
    # ~0.3 s floor, from ANY producing site — grid remainders, phase
    # bounds landing next to grid cuts, drop-forced insertions. Pinned
    # drop cuts and phase starts are protected; a sliver is absorbed
    # into its preceding slot (into the following one when the left
    # edge is protected).
    cuts = _absorb_slivers(
        cuts, set(drop_starts) | {s for s, _e, _l in phases[1:]}
    )
    # Cut-ahead lead: interior cuts move slightly BEFORE their beat so the
    # incoming shot is on screen when the beat lands. Drop-slot matching
    # below tolerates the shift (slots start cut_lead before their drop).
    # ``raw_cuts`` (the unshifted grid) survives for the dissolve-lead-0
    # workaround (blueprint 1.7, _undo_lead_on_dissolves).
    raw_cuts = list(cuts)
    cuts = _apply_cut_lead(cuts, cut_lead)
    slots = list(zip(cuts, cuts[1:]))
    drop_slots = {
        i
        for i, (s, _) in enumerate(slots)
        if any(abs(s - d) <= cut_lead + _EPS for d in drop_starts)
    }
    # Pool build. slack_end (peak-on-beat, blueprint 1.1) is how far past
    # each moment a drop HOLD may extend: the enclosing USABLE sift
    # segment where the report carries segments, capped by the next
    # moment of the same clip (its material is spoken for — zero-repeat)
    # and the clip length. Only ever read when a peak is aimed.
    pool: list[_PoolItem] = []
    for r in reports:
        usable = [s for s in r.segments if s.label == USABLE]
        starts = sorted(m.start for m in r.moments)
        for m in r.moments:
            slack = r.duration if r.duration > 0 else m.end
            seg = next(
                (
                    s
                    for s in usable
                    if s.start - _EPS <= m.start and m.end <= s.end + _EPS
                ),
                None,
            )
            if seg is not None:
                slack = min(slack, seg.end)
            nxt = next((s for s in starts if s > m.start + _EPS), None)
            if nxt is not None:
                slack = min(slack, nxt)
            pool.append(
                _PoolItem(
                    r.path, r.duration, m,
                    media_start=r.media_start,
                    slack_end=max(slack, 0.0),
                )
            )
    if not allow_repeats:
        # Zero-repeat promise: overlapping moments (possible in hand-built
        # reports and distilled timelines) must not enter the cut twice.
        pool = _trim_overlapping_pool(pool)
    if not slots or not pool:
        plan.notes.append("no slots or no moments; nothing planned")
        return plan

    if order == CHRONOLOGICAL:
        pool.sort(key=lambda it: (it.clip_path, it.moment.start))
        slot_order = list(range(len(slots)))
    elif order == BEST_FIRST:
        pool.sort(key=lambda it: (-it.moment.score, it.clip_path, it.moment.start))
        slot_order = sorted(
            range(len(slots)),
            key=lambda i: (-_energy_at(grid_music.sections, slots[i][0]), slots[i][0]),
        )
    else:
        raise ValueError(f"unknown order: {order!r}")

    # Arrangement: the editor's scenes claim slots 0..k-1 in the given
    # order; the auto-fill below only serves what remains. Placed moments
    # start consumed, so their material is never replayed first.
    arr_items: list[dict] = []
    arr_entries: list[MontageEntry] = []
    arr_preset: dict[int, _PoolItem] = {}
    arr_pre_used: frozenset[int] = frozenset()
    if arrangement:
        arr_items = _resolve_arrangement(arrangement, pool)
        arr_entries, arr_preset, arr_notes = _cast_arrangement(
            arr_items, pool, slots, drop_slots, plan.duration
        )
        arr_k = len(arr_entries)
        placed = list(arr_preset.values())
        arr_pre_used = frozenset(
            i for i, it in enumerate(pool) if any(it is p for p in placed)
        )
        slot_order = [s for s in slot_order if s >= arr_k]
        drop_slots = {d for d in drop_slots if d >= arr_k}

    # Semantic casting kicks in only when the vision pass annotated at least
    # one pool moment (labels alone still ride along, but change nothing).
    semantic = any(it.role or it.hero > _EPS or it.group for it in pool)
    # Energy-motion matching: what the music does in each slot, 0..1. Arc
    # styles use the phase's nominal energy; the arc-less "auto" style reads
    # the song's sections (no-music auto plans have neither and skip it).
    slot_energies: list[float] | None = None
    if phases:
        slot_energies = [
            _PHASE_ENERGY.get(_phase_label_at(phases, s) or "", 0.5) for s, _ in slots
        ]
    elif music is not None and grid_music.sections:
        slot_energies = [_energy_at(grid_music.sections, s) for s, _ in slots]
    # Act/section label per slot — the jump-cut guard's "would the
    # continuity merge join this pair?" check needs the same context the
    # merge itself gates on.
    slot_contexts: list[str] | None = None
    if phases:
        slot_contexts = [_phase_label_at(phases, s) or "" for s, _ in slots]
    elif music is not None and grid_music.sections:
        slot_contexts = [_label_at(grid_music.sections, s) for s, _ in slots]
    entries, fill_notes, short_at = _fill(
        slots, slot_order, pool, phases, highlight_phase, drop_slots,
        semantic=semantic, slot_energies=slot_energies,
        pre_used=arr_pre_used, preset=arr_preset or None,
        slot_contexts=slot_contexts,
        # Hook/loop casting: any style whose arc OPENS on a "hook" phase
        # (the "short" style) gets the pattern-interrupt slot 0 and the
        # loop-friendly last slot (see _fill's docstring).
        hook_loop=bool(phases) and phases[0][2] == "hook",
        allow_repeats=allow_repeats,
        cut_lead=cut_lead,
        # Learned preferences / refine tweaks (blueprint 4.3/4.2): folded in
        # as tie-breakers. None (the default) is byte-identical to before.
        casting_bias=casting_bias if casting_bias and not casting_bias.is_neutral() else None,
    )
    entries = arr_entries + entries
    entries.sort(key=lambda e: e.record_start)
    if short_at is not None:
        # No-repeats truncation: the fill ran out of fresh material — cut
        # the plan at the last fillable slot instead of recycling footage.
        entries = _shorten_no_repeats(plan, entries, grid_music, short_at, len(pool))
    # Post-cast coalescing. Entries tile the slots 1:1 at this point, so
    # entry index == slot index; slot_of keeps the mapping alive across
    # the passes. First same-clip CONTINUITY (adjacent slots that are one
    # continuing take become one shot — see _merge_continuity), then
    # content-adaptive PACING (adjacent calm slots on calm music merge
    # into longer shots — see _merge_calm_slots).
    merge_sections = grid_music.sections if music is not None else []
    slot_of = list(range(len(entries)))
    entries, slot_of, continuity_note = _merge_continuity(
        entries, slot_of, phases, merge_sections, drop_slots, len(arr_entries)
    )
    merge_note: str | None = None
    if slot_energies is not None:
        entries, merge_note = _merge_calm_slots(
            entries, pool, slot_energies, phases, merge_sections,
            drop_slots, len(arr_entries), slot_of=slot_of,
        )
    plan.entries = entries
    plan.notes.extend(fill_notes)
    if continuity_note:
        plan.notes.append(continuity_note)
    if merge_note:
        plan.notes.append(merge_note)
    # Honest low-variety note: same-clip boundaries that SURVIVED the
    # continuity merge with a visible source skip are jump cuts the guard
    # could not cast away (the pool was too small) — say so once.
    jump_survivors = 0
    for prev, nxt in zip(entries, entries[1:]):
        if prev.clip_path != nxt.clip_path:
            continue
        gap = nxt.source_start - prev.source_end
        # A visible skip inside the scene — forwards or backwards. Beyond
        # ~8s either way the cut reads as another scene, below ~0.25s the
        # shot simply continues; in between it reads as an error.
        if _JUMP_CUT_VISIBLE_GAP - _EPS <= abs(gap) <= _JUMP_CUT_MIN_GAP + _EPS:
            jump_survivors += 1
    if jump_survivors:
        plan.notes.append(
            f"footage variety is low: same-scene jump cuts were unavoidable "
            f"in {jump_survivors} spot{'s' if jump_survivors != 1 else ''} "
            "— more footage would help"
        )
    if arrangement:
        plan.notes.extend(arr_notes)
    used = sum(1 for it in pool if it.uses)
    plan.notes.append(f"{len(entries)} slots filled, {used} of {len(pool)} moments used")
    # Perceived variety: a cut leaning on one clip feels repetitive even
    # with zero repeated frames — say so once, deterministically.
    if len(entries) >= 2:
        per_clip: dict[str, int] = {}
        for e in entries:
            per_clip[e.clip_path] = per_clip.get(e.clip_path, 0) + 1
        top = max(per_clip.values())
        if top > _VARIETY_SHARE * len(entries) + _EPS:
            plan.notes.append(
                f"variety: {top} of {len(entries)} shots come from one clip "
                "— more footage would help"
            )
    # Adaptive music window: the tool decides when the music enters (the
    # override wins; "auto" has no arc boundaries and always stays at 0).
    if music is not None:
        if window_override is not None:
            w_in, w_out = window_override
            if w_in >= plan.duration - _EPS:
                raise ValueError(
                    f"music_window: music_in {w_in:g}s is at/after the "
                    f"{plan.duration:.1f}s montage end"
                )
            w_out = min(w_out, plan.duration) if w_out > _EPS else 0.0
            snap_kind = ""
            if w_in > _EPS:
                w_in, snap_kind = _snap_record_time(
                    grid_music, w_in, music_start=0.0, limit=plan.duration
                )
            if w_out > _EPS and w_out <= w_in + _EPS:
                raise ValueError(
                    "music_window: music_out must lie after music_in "
                    "(or be 0 for the montage end)"
                )
            if w_in > _EPS or w_out > _EPS:
                plan.music_in = w_in
                plan.music_out = w_out
                pieces = []
                if w_in > _EPS:
                    entered = f"enters at {w_in:.1f}s"
                    if snap_kind:
                        entered += f" (snapped to {snap_kind})"
                    pieces.append(entered)
                if w_out > _EPS:
                    pieces.append(f"ends at {w_out:.1f}s")
                plan.notes.append("music window: " + ", ".join(pieces) + " (your setting)")
        else:
            decided_in, window_note = decide_music_window(
                music, chosen.key, plan.phases, music_start=music_start
            )
            if decided_in > _EPS:
                plan.music_in = decided_in
                plan.notes.append(window_note)
            # A limp song tail: when the song's ending under the cut is a
            # long ambient fade, the music may end early (arc'd styles
            # only, conservative — see decide_music_out).
            decided_out, out_note = decide_music_out(
                grid_music, chosen.key, plan.phases, plan.duration,
                music_in=plan.music_in,
            )
            if decided_out > _EPS:
                plan.music_out = decided_out
                plan.notes.append(out_note)
    # Per-cut transitions read the cast moments' scene groups and daylight
    # classes (both soft annotations; None when the pool carries neither).
    entry_semantics: list[tuple[str, str]] | None = None
    if any(it.group or it.daylight for it in pool):
        entry_semantics = []
        for entry in entries:
            match = _match_pool_moment(entry, pool)
            entry_semantics.append(
                (pool[match].group, pool[match].daylight)
                if match is not None
                else ("", "")
            )
    _plan_finishing(
        plan, entries, grid_music, chosen, phases, transitions,
        entry_semantics=entry_semantics,
        casting_bias=casting_bias if casting_bias and not casting_bias.is_neutral() else None,
    )
    if arr_entries:
        _arrangement_boundaries(plan, entries, arr_items, len(arr_entries))
    if cut_lead > _EPS:
        # Dissolve lead 0 (blueprint 1.7): after every transition decision
        # (finishing + arrangement), dissolving boundaries return to their
        # unshifted grid position — before the SFX layer reads the cuts.
        _undo_lead_on_dissolves(entries, cuts, raw_cuts, protected=len(arr_entries))
    if sfx:
        _plan_sfx(plan, phases, drop_starts)
    if arr_entries:
        _arrangement_cues(plan, slots, arr_items, len(arr_entries))
    # Deliberate silence LAST: the gaps read the finished dips (from
    # _plan_finishing / the arrangement) and their carrier cues (from
    # _plan_sfx / _arrangement_cues) — see the module docstring's
    # Deliberate silence section for the ordering contract with
    # monteur.elements.assign_elements.
    if music is not None and music_flow == "deliberate":
        _plan_music_gaps(plan, grid_music, style)
    return plan


def _plan_finishing(
    plan: MontagePlan,
    entries: list[MontageEntry],
    music: MusicAnalysis,
    style: MontageStyle,
    phases: list[tuple[float, float, str]],
    transitions: str = "auto",
    entry_semantics: list[tuple[str, str]] | None = None,
    casting_bias: "CastingBias | None" = None,
) -> None:
    """Set the plan's fades, dissolves and smash-to-black dips (in place).

    Styles with an outro phase get ``fade_in`` = 0.5 s and ``fade_out`` =
    min(2 s, last outro slot length); "auto" gets 0.5 s / 1 s — fades
    apply in every transition mode.

    ``transitions`` = "auto" is PER-CUT intelligence: every boundary is
    decided from what actually meets there, in this order:

    * **same-clip continuation** (the incoming entry plays the same clip
      as the outgoing one — post-merge, i.e. a continuation the
      continuity pass could not join) → hard cut, always: dissolving a
      shot into itself reads as a ghost;
    * **climax phase / "high" section** → hard cuts only;
    * **daylight-block change** (``entry_semantics``: the entries' cast
      daylight classes, both known, differ) → a dissolve — the soft
      time-lapse feel of day handing over to golden hour;
    * **gentle passage** (>= ``_SLOW_PHASE_STEP`` beats per cut; "low"
      sections in "auto"): a scene-group CHANGE dissolves (min 0.5 s,
      half the slot); two takes of the SAME group cut hard (a dissolve
      inside one scene is the jump cut's uglier cousin). Boundaries
      without group knowledge keep the classic gentle-phase dissolve;
    * everything else cuts hard. A style with ``smash_to_black`` (the
      trailer) additionally dips to black at act changes, unchanged.

    ``entry_semantics`` (one ``(group, daylight)`` per entry, from the
    cast moments' vision/daylight annotations; None = none known) feeds
    the two content rules; without it the behavior is the classic
    gentle-phase rule plus the same-clip hard cut. The explicit modes
    are unchanged overrides: "dissolves" dissolves into EVERY entry,
    "cuts" plans neither dissolves nor dips, "smash" forces the dips
    (at act changes; for the arc-less "auto" style at the song's
    section changes) without dissolves. Notes summarize what was
    decided and remind that the music fade-out must be applied in
    Resolve (the export formats can't carry it).
    """
    if not entries:
        return
    arc_labels = [lab for _, lab in style.arc]
    if "outro" in arc_labels:
        plan.fade_in = _FADE_IN
        last = entries[-1]
        plan.fade_out = min(_MAX_FADE_OUT, last.record_end - last.record_start)
    elif not style.arc:  # "auto"
        plan.fade_in = _FADE_IN
        plan.fade_out = _AUTO_FADE_OUT

    dissolves = 0
    scene_dissolves = 0  # dissolves earned by a confirmed scene-group change
    daylight_dissolves = 0  # dissolves earned by a daylight-block change
    continuation_cuts = 0  # gentle boundaries cut hard: the shot continues
    same_scene_cuts = 0  # gentle boundaries cut hard: same group either side
    for i in range(1, len(entries)):  # entry 0's fade is fade_in, not a dissolve
        entry = entries[i]
        prev = entries[i - 1]
        reason = ""
        if transitions == "dissolves":
            want = True
        elif transitions != "auto":
            want = False  # "cuts" and "smash" plan no dissolves
        else:
            if style.arc:
                label = _phase_label_at(phases, entry.record_start)
                gentle = (
                    label is not None
                    and style.beats_per_cut.get(label, 2) >= _SLOW_PHASE_STEP
                )
                high = label == "climax"
            else:
                s_label = _label_at(music.sections, entry.record_start)
                gentle = s_label == "low"
                high = s_label == "high"
            prev_group, prev_day = (
                entry_semantics[i - 1] if entry_semantics else ("", "")
            )
            group, day = entry_semantics[i] if entry_semantics else ("", "")
            if entry.clip_path == prev.clip_path:
                want = False  # a continuing take never dissolves into itself
                if gentle:
                    continuation_cuts += 1
            elif high:
                want = False  # the peak cuts hard, whatever the content says
            elif prev_day and day and prev_day != day:
                want = True
                reason = "daylight"
            elif gentle:
                if prev_group and group and prev_group == group:
                    want = False  # same scene, different take: cut
                    same_scene_cuts += 1
                else:
                    want = True
                    if prev_group and group:
                        reason = "scene"
            else:
                want = False
            # Learned "fewer dissolves" preference (blueprint 4.3): drop the
            # WEAKEST dissolves — the plain gentle-passage ones with no scene
            # or daylight reason — to a hard cut. Meaningful scene/daylight
            # dissolves are kept; the peak already cuts hard. A tie-breaker,
            # never touching the explicit "dissolves"/"cuts" overrides.
            if (
                want
                and reason == ""
                and casting_bias is not None
                and casting_bias.fewer_dissolves
            ):
                want = False
        if want:
            # Beat-quantized dissolve length (blueprint 1.7): the classic
            # min(0.5s, half the slot) ceiling, snapped to the beat grid
            # via the shared helper — identical at all three dissolve
            # sites (here, the arrangement, adjust_entry_boundary).
            entry.transition = _dissolve_seconds(plan, entry)
            if entry.transition > _EPS:
                dissolves += 1
                if reason == "daylight":
                    daylight_dissolves += 1
                elif reason == "scene":
                    scene_dissolves += 1
    smart = (
        scene_dissolves or daylight_dissolves or continuation_cuts or same_scene_cuts
    )
    if transitions == "auto" and smart:
        has_high = "climax" in arc_labels or (
            not style.arc and any(s.label == "high" for s in music.sections)
        )
        plain = dissolves - scene_dissolves - daylight_dissolves
        bits: list[str] = []
        if scene_dissolves:
            bits.append(
                f"{scene_dissolves} dissolve{'s' if scene_dissolves != 1 else ''} "
                "at scene changes"
            )
        if daylight_dissolves:
            bits.append(f"{daylight_dissolves} at daylight changes")
        if plain:
            bits.append(f"{plain} in gentle passages")
        if continuation_cuts or same_scene_cuts:
            bits.append("hard cuts where the scene continues")
        if has_high:
            bits.append("hard cuts in the climax")
        plan.notes.append("transitions: " + ", ".join(bits))
    elif dissolves:
        plan.notes.append(
            f"{dissolves} dissolves"
            + (" in gentle phases" if transitions == "auto" else " on every cut")
        )
    if transitions == "cuts":
        plan.notes.append("transitions: hard cuts only")

    # Smash to black: at every act change, the outgoing clip gives up its
    # last _DIP_SECONDS to a black gap — the incoming act then HITS out of
    # black. Each gap is a natural title slot (exported as a marker).
    smash = transitions == "smash" or (
        transitions == "auto" and style.smash_to_black
    )
    if smash:
        # Beat-quantized dip length + remainder floor (blueprint 1.7):
        # the same _dip_seconds/_dip_min_remainder every carving site uses.
        dip_len = _dip_seconds(plan)
        remainder_floor = _dip_min_remainder(plan)
        if phases:
            bounds = [p_start for p_start, _, _ in phases[1:]]
        else:  # arc-less "auto": the song's section changes are the acts
            bounds = [s.start for s in music.sections[1:]]
        for bound in bounds:
            outgoing = min(
                entries, key=lambda e: abs(e.record_end - bound), default=None
            )
            if outgoing is None:
                continue
            # Tolerate the cut-lead shift; anything further off means the
            # boundary landed inside a slot, not on a cut — no dip there.
            if abs(outgoing.record_end - bound) > 0.25 + _EPS:
                continue
            slot = outgoing.record_end - outgoing.record_start
            if slot - dip_len < remainder_floor:
                continue
            outgoing.record_end -= dip_len
            outgoing.source_end -= dip_len
            plan.dips.append((outgoing.record_end, dip_len))
        if plan.dips:
            plan.notes.append(
                f"{len(plan.dips)} smash-cuts to black at act changes "
                f"({dip_len:g}s each) — title slots, exported as markers"
            )

    if plan.fade_in > _EPS or plan.fade_out > _EPS:
        plan.notes.append(
            f"fades to black: {plan.fade_in:g}s in, {plan.fade_out:g}s out "
            "(in the FCPXML export; fade the music itself in Resolve)"
        )


def _ambience_query(entries: list[MontageEntry], span: float) -> str:
    """Search terms for the opening ambience bed.

    Built from the vision labels of the entries inside the opening span:
    the first two distinct meaningful words (stopwords and sub-3-letter
    words dropped) plus "ambience" — a label "over the mountain pass"
    makes "mountain pass ambience". Entries carry only the label, not the
    vision tags, so the label's own words are the honest source; without
    labels (no --see) the generic "outdoor ambience" is used.
    """
    words: list[str] = []
    for entry in entries:
        if entry.record_start >= span - _EPS:
            break
        for raw in entry.label.lower().split():
            word = raw.strip(".,!?;:()[]'\"-")
            if len(word) < 3 or word in _SFX_STOPWORDS or word in words:
                continue
            words.append(word)
            if len(words) == 2:
                return f"{words[0]} {words[1]} ambience"
    if words:
        return f"{words[0]} ambience"
    return "outdoor ambience"


def _plan_sfx(
    plan: MontagePlan,
    phases: list[tuple[float, float, str]],
    drop_starts: list[float],
) -> None:
    """Plan the sound-design cue layer onto a filled plan (in place).

    Reads only what the plan already knows — the arc ``phases``, the
    "auto" style's drop-forced cut times (``drop_starts``), the dips and
    the entries — and fills ``plan.sfx`` per the module docstring's SFX
    layer section: ambience at 0, risers ending on act changes, impacts
    on the climax start and drop cuts, sub-drops under the dips, whooshes
    centered on the fastest cuts. The density cap (~1 cue per
    ``_SFX_SECONDS_PER_CUE``) trims whooshes first, then risers
    (into-the-climax survives longest, then earlier act changes);
    ambience/impact/sub-drop cues always stay, even if the cut is so
    short they alone exceed the cap. The result is sorted by time and
    reported in the notes.
    """
    if not plan.entries or plan.duration <= _EPS:
        return
    duration = plan.duration

    # 1. Opening ambience: a bed under the first shots, sized to the opening
    #    phase (arc styles) or the first few seconds ("auto" has no phases).
    opening = phases[0][1] - phases[0][0] if phases else min(_SFX_AUTO_OPENING, duration)
    essential: list[SfxCue] = []
    if opening > _EPS:
        essential.append(
            SfxCue(
                time=0.0,
                duration=opening,
                kind="ambience",
                query=_ambience_query(plan.entries, opening),
                note="opening",
            )
        )

    # 2. Risers into act changes: a build ENDING exactly on the boundary.
    #    Only real act changes count — the trailer's split build ramps
    #    inside one act and gets no riser there.
    riser_items: list[tuple[str, SfxCue]] = []  # (incoming phase, cue)
    for (p_start, p_end, p_label), (_, _, n_label) in zip(phases, phases[1:]):
        if n_label == p_label or not (_EPS < p_end < duration - _EPS):
            continue
        length = min(_SFX_RISER_MAX, (p_end - p_start) / 2.0)
        if length <= _EPS:
            continue
        riser_items.append(
            (
                n_label,
                SfxCue(
                    time=p_end - length,
                    duration=length,
                    kind="riser",
                    query="riser build up",
                    note=f"{p_label} -> {n_label}",
                ),
            )
        )

    # 3. Impacts: ON the climax start (arc styles; when a drop pinned the
    #    climax this IS the drop) and ON every drop-forced cut — "auto"'s
    #    every-drop cuts, "short"'s pin, and the arc styles' secondary-drop
    #    cuts (blueprint 2.1; all carried in ``drop_starts``).
    climax_start = next((s for s, _, lab in phases if lab == "climax"), None)
    if climax_start is not None and _EPS < climax_start < duration - _EPS:
        essential.append(
            SfxCue(
                time=climax_start,
                duration=min(_SFX_IMPACT_LENGTH, duration - climax_start),
                kind="impact",
                query="cinematic impact hit",
                note="climax start",
            )
        )
    for drop in drop_starts:
        essential.append(
            SfxCue(
                time=drop,
                duration=min(_SFX_IMPACT_LENGTH, duration - drop),
                kind="impact",
                query="cinematic impact hit",
                note="cut on the drop",
            )
        )

    # 4. Sub-drops under the smash-to-black dips: the black wants a boom,
    #    and the title (the dip IS a title slot) lands on it.
    for dip_start, dip_len in plan.dips:
        essential.append(
            SfxCue(
                time=dip_start,
                duration=dip_len,
                kind="sub-drop",
                query="sub drop boom",
                note="title slot",
            )
        )

    # Density cap: ~1 cue per _SFX_SECONDS_PER_CUE seconds of cut. Risers
    # are trimmed to the room left by the backbone (into-the-climax riser
    # first, then earlier act changes); whooshes only fill what remains.
    max_cues = max(1, math.ceil(duration / _SFX_SECONDS_PER_CUE))
    room = max_cues - len(essential)
    if len(riser_items) > room:
        riser_items.sort(key=lambda it: (it[0] != "climax", it[1].time))
        riser_items = riser_items[: max(0, room)]
    cues = essential + [cue for _, cue in riser_items]

    # 5. Whooshes on the fastest cuts (shortest slots), centered on the cut,
    #    each clear of every already-placed cue so effects never pile up.
    def _distance(cue: SfxCue, t: float) -> float:
        return max(cue.time - t, t - (cue.time + cue.duration), 0.0)

    room = min(max_cues - len(cues), _SFX_MAX_WHOOSHES)
    for entry in sorted(
        plan.entries, key=lambda e: (e.record_end - e.record_start, e.record_start)
    ):
        if room <= 0:
            break
        cut = entry.record_start
        if not (_EPS < cut < duration - _EPS):
            continue
        if any(_distance(c, cut) < _SFX_WHOOSH_CLEARANCE - _EPS for c in cues):
            continue
        cues.append(
            SfxCue(
                time=max(0.0, cut - _SFX_WHOOSH_LENGTH / 2.0),
                duration=_SFX_WHOOSH_LENGTH,
                kind="whoosh",
                query="whoosh transition fast",
                note="fast cut",
            )
        )
        room -= 1

    # A delayed music entry IS the tension ramp of the cold open: a riser
    # cue must END exactly on it (the trailer moment — dry open, riser,
    # slam). The nearest act-change riser is retimed onto the entry; when
    # none sits close enough a dedicated cue is added (it is backbone, not
    # subject to the density cap). monteur.elements anchors the real file
    # to this cue.
    if plan.music_in > _EPS:
        near = min(
            (c for c in cues if c.kind == "riser"),
            key=lambda c: abs(c.time + c.duration - plan.music_in),
            default=None,
        )
        if near is not None and abs(near.time + near.duration - plan.music_in) <= 1.0 + _EPS:
            near.time = max(0.0, plan.music_in - near.duration)
            near.duration = plan.music_in - near.time
            near.note += " — into the music entry"
        else:
            length = min(_SFX_RISER_MAX, plan.music_in)
            if length > _EPS:
                cues.append(
                    SfxCue(
                        time=plan.music_in - length,
                        duration=length,
                        kind="riser",
                        query="riser build up",
                        note="into the music entry",
                    )
                )

    cues.sort(key=lambda c: c.time)
    plan.sfx = cues
    plan.notes.append(
        f"sfx layer: {len(cues)} cues planned "
        "(markers on the timeline; queries for your SFX library)"
    )


def music_window_bounds(plan: MontagePlan) -> tuple[float, float]:
    """``(music_in, music_end)`` in record seconds, clamped into the montage.

    The one shared reading of the plan's adaptive music window, used by
    every export surface: ``music_in`` 0 = from the first frame,
    ``music_out`` 0 = to the montage end; a degenerate window (end at or
    before start after clamping) falls back to the full length —
    defensive, a hand-edited plan must never yield a zero-length bed.
    """
    duration = max(0.0, plan.duration)
    w_in = min(max(getattr(plan, "music_in", 0.0) or 0.0, 0.0), duration)
    w_out = getattr(plan, "music_out", 0.0) or 0.0
    w_end = min(w_out, duration) if w_out > _EPS else duration
    if w_end <= w_in + _EPS:
        return 0.0, duration
    return w_in, w_end


# Backwards-friendly private alias used inside this module.
_music_window_bounds = music_window_bounds


def music_bed_gaps(plan: MontagePlan) -> list[tuple[float, float]]:
    """The plan's deliberate silences, clamped into the music window.

    The one shared reading of ``plan.music_gaps`` for every export
    surface: gaps are clipped to ``music_window_bounds`` (a silence
    outside the audible window is already silent), sorted, merged when
    they touch, and zero-length remainders dropped. Defensive like
    :func:`music_window_bounds` — hand-edited plans must never yield a
    negative or overlapping mute.
    """
    w_in, w_end = music_window_bounds(plan)
    merged: list[tuple[float, float]] = []
    for lo, hi in sorted(getattr(plan, "music_gaps", []) or []):
        lo = max(float(lo), w_in)
        hi = min(float(hi), w_end)
        if hi - lo <= _EPS:
            continue
        if merged and lo <= merged[-1][1] + _EPS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def music_bed_segments(plan: MontagePlan) -> list[tuple[float, float]]:
    """The record windows where the song is AUDIBLE: window minus gaps.

    ``[(start, end), ...]`` in record seconds — the music window
    (:func:`music_window_bounds`) with the plan's deliberate silences
    (:func:`music_bed_gaps`) cut out. The record<->song mapping holds on
    every segment: a segment starting at record ``t`` reads the song from
    ``music_start + t``, so the bed after a gap CONTINUES (the gap's
    source span is skipped too) and every beat stays where it was. A plan
    without gaps yields the single full-window segment — the exact old
    behavior.
    """
    w_in, w_end = music_window_bounds(plan)
    segments: list[tuple[float, float]] = []
    cursor = w_in
    for lo, hi in music_bed_gaps(plan):
        if lo - cursor > _EPS:
            segments.append((cursor, lo))
        cursor = max(cursor, hi)
    if w_end - cursor > _EPS:
        segments.append((cursor, w_end))
    if not segments:
        segments.append((w_in, w_end))  # defensive: never a silent-only bed
    return segments


def _plan_music_gaps(plan: MontagePlan, music: MusicAnalysis, style: str) -> None:
    """Plan the deliberate silences onto a finished plan (in place).

    Runs LAST in :func:`plan_montage` (``music_flow="deliberate"``), after
    the dips, the SFX layer and the arrangement cues exist — the module
    docstring's Deliberate silence section has the design. ``music`` is
    the record-time (windowed) analysis. Two moves:

    1. Under every smash-to-black dip that something CARRIES — a planned
       sub-drop/impact cue at the dip (:data:`_GAP_CARRIER_KINDS` within
       :data:`_GAP_CARRIER_TOLERANCE`; a marker cue counts — it records
       the intent and :mod:`monteur.elements` files it with a real
       braam/hit later, and assign_elements only ever adds to the cue
       list, so the carrier survives). The gap ends on the following
       downbeat when one lies within :data:`_GAP_DOWNBEAT_EXTEND_BEATS`
       past the dip end. No carrier = no gap: the song plays through the
       black exactly as before — silence never happens by accident.
    2. One beat before the FIRST in-range drop, re-entry exactly ON the
       drop. Skipped for "short", skipped when the beat would reach into
       the ``music_in`` dry open (no double silence), skipped on top of a
       dip gap, and never longer than :data:`_PRE_DROP_GAP_BEATS` beats.

    Notes narrate every gap. Idempotent input state: reads only
    ``plan.dips`` / ``plan.sfx`` / the music grid, writes only
    ``plan.music_gaps`` and notes.
    """
    w_in, w_end = music_window_bounds(plan)
    if w_end - w_in <= _EPS:
        return
    beat = _pulse_interval(music)
    downbeats = sorted(music.downbeats)
    gaps: list[tuple[float, float]] = []
    notes: list[str] = []

    def carried(dip_start: float, dip_end: float) -> bool:
        return any(
            cue.kind in _GAP_CARRIER_KINDS
            and cue.time <= dip_end + _GAP_CARRIER_TOLERANCE + _EPS
            and cue.time + max(cue.duration, 0.0)
            >= dip_start - _GAP_CARRIER_TOLERANCE - _EPS
            for cue in plan.sfx
        )

    # 1. Under every carried smash-to-black dip, extended to the downbeat.
    for dip_start, dip_len in sorted(plan.dips):
        dip_end = dip_start + dip_len
        if not carried(dip_start, dip_end):
            continue  # nothing carries the silence — the song plays through
        lo = max(dip_start, w_in)
        hi = min(dip_end, w_end)
        if hi - lo <= _EPS:
            continue  # the dip sits in the dry open / past the music end
        on_downbeat = False
        nxt = next((d for d in downbeats if d > dip_end + _EPS), None)
        if (
            nxt is not None
            and nxt <= dip_end + _GAP_DOWNBEAT_EXTEND_BEATS * beat + _EPS
            and nxt <= w_end + _EPS
        ):
            hi = min(nxt, w_end)
            on_downbeat = abs(hi - nxt) <= _EPS
        if gaps and lo <= gaps[-1][1] + _EPS:
            continue  # defensive: dips never overlap, but stay ordered
        gaps.append((lo, hi))
        notes.append(
            f"silence: {hi - lo:.1f}s under the act title at {lo:.1f}s — "
            + (
                "music re-enters on the downbeat"
                if on_downbeat
                else "music re-enters at the cut"
            )
        )

    # 2. One beat of absolute silence before the drop (first in-range one).
    if style != "short" and len(music.beats) >= 2:
        drop = next(
            (d for d in sorted(music.drops) if w_in + _EPS < d < w_end - _EPS),
            None,
        )
        if drop is not None:
            lo = drop - _PRE_DROP_GAP_BEATS * beat
            overlaps_dip_gap = any(
                lo < g_hi + _EPS and drop > g_lo - _EPS for g_lo, g_hi in gaps
            )
            # Never into the dry open (double silence), never off the front.
            if lo >= w_in + _EPS and lo > _EPS and not overlaps_dip_gap:
                gaps.append((lo, drop))
                notes.append(
                    f"silence: 1 beat before the drop at {drop:.1f}s — "
                    "re-entry on the hit"
                )

    if not gaps:
        return
    gaps.sort(key=lambda g: g[0])
    plan.music_gaps = gaps
    plan.notes.extend(notes)


def _prune_music_gaps(
    plan: MontagePlan, *, protect: tuple[float, float] | None = None
) -> None:
    """Drop deliberate silences whose reason no longer exists (in place).

    The accidental-silence guard for plan SURGERY (boundary adjustments,
    pinning, region splices): a gap is kept only while a dip still starts
    at its start (within :data:`_BOUNDARY_EPS` tolerance) or a drop mark
    still sits at its end — a gap whose dip or drop was edited away would
    be exactly the accidental silence the feature exists to prevent.
    ``protect=(lo, hi)`` additionally drops any gap overlapping that
    record window (a pinned shot claims its time, song included).
    Surgery never ADDS gaps: an inserted dip has no carrier cue, so the
    song plays through it until the next full re-plan.
    """
    if not plan.music_gaps:
        return
    tolerance = 0.25 + _EPS  # the dip-gap start tolerance (cut-lead safe)

    def keep(lo: float, hi: float) -> bool:
        if protect is not None and lo < protect[1] - _EPS and hi > protect[0] + _EPS:
            return False
        if any(abs(d_start - lo) <= tolerance for d_start, _len in plan.dips):
            return True
        return any(abs(d - hi) <= tolerance for d in plan.drop_marks)

    plan.music_gaps = [(lo, hi) for lo, hi in plan.music_gaps if keep(lo, hi)]


def _jl_beat_seconds(plan: MontagePlan) -> float:
    """A beat-length estimate for J/L tolerances (from the plan alone).

    ``beat_marks`` are downbeats (~4 beats apart); their median spacing / 4
    is the beat. With fewer than two marks fall back to a musical default.
    """
    marks = sorted(t for t in plan.beat_marks)
    gaps = sorted(b - a for a, b in zip(marks, marks[1:]) if b - a > _EPS)
    if gaps:
        return gaps[len(gaps) // 2] / _BEATS_PER_BAR
    return _PSEUDO_BEAT


def jl_audio_edits(
    plan: MontagePlan, fps: float = 25.0
) -> tuple[dict[int, tuple[float, float]], list[str]]:
    """Decide the J/L cut audio offsets for a plan (blueprint 2.3).

    Returns ``({entry_index: (lead, lag)}, notes)`` — deterministic, from
    the plan's OWN data (entries, phases, drop_marks, music_gaps, placed
    SFX), so it survives a save/load round-trip. ``lead`` on an entry
    (J-cut) brings its own audio in that many seconds early; ``lag``
    (L-cut) rings it that many seconds past its picture-out. An entry that
    already carries a non-zero ``audio_lead``/``audio_lag`` (a hand-
    authored or revised plan) is respected verbatim and its boundary is
    never recomputed — "hand-built plans without J/L stay byte-identical",
    and hand-built plans WITH J/L keep exactly what they set.

    A boundary between shot *i* and *i+1* earns a J or an L cut ONLY where
    it serves and never breaks a promise:

    * the picture cut is a HARD cut between DIFFERENT clips (a dissolve, a
      same-clip continuation or a black-dip gap is left alone);
    * it is not on a drop (``drop_marks``) — a drop-forced cut stays synced;
    * it is not on a music-gap edge (the deliberate silence is sacred);
    * no placed SFX/impact element sits on the cut;
    * neither side is a ``climax`` phase — the peak-on-beat coincidences
      live there, and sync is sacred (a conservative, plan-only proxy for
      "never at a peak-on-beat cut").

    Direction reads the arc energy (:data:`_PHASE_ENERGY`): a hot→cool
    handover L-cuts (the hot tail rings into the calm), a cool→hot one
    J-cuts (the coming energy anticipated); an even/quiet continuity
    defaults to a gentle L-cut. The lead/lag is the small, fps-quantized
    :data:`_JL_LEAD_SECONDS`, clamped so each shot keeps
    :data:`_JL_MIN_SOLO` of un-overlapped audio and so the overlap has the
    source handles it needs (the incoming shot's head for a J, the
    outgoing shot's tail for an L; an unknown ``clip_duration`` blocks the
    L-cut honestly). Empty result = no decoupling, byte-identical exports.
    """
    edits: dict[int, tuple[float, float]] = {}
    notes: list[str] = []
    entries = plan.entries
    # Respect any hand-authored offsets verbatim (and mark their boundaries
    # as spoken-for so the auto pass never doubles them).
    handled: set[int] = set()
    for i, e in enumerate(entries):
        if e.audio_lead > _EPS or e.audio_lag > _EPS:
            edits[i] = (max(0.0, e.audio_lead), max(0.0, e.audio_lag))
            handled.add(i)
            if e.audio_lead > _EPS:
                handled.add(i - 1)
            if e.audio_lag > _EPS:
                handled.add(i + 1)
    if len(entries) < 2:
        return edits, notes

    beat_s = _jl_beat_seconds(plan)
    frame = 1.0 / fps if fps > 0 else 0.0
    base = _JL_LEAD_SECONDS
    if frame > 0:  # fps-aware: quantize to whole frames (cut_lead_for spirit)
        base = max(frame, round(_JL_LEAD_SECONDS / frame) * frame)

    def on_any(t: float, marks) -> bool:
        return any(abs(t - float(m)) <= _JL_ON_CUT_TOL + _EPS for m in marks)

    gap_edges = [g for pair in plan.music_gaps for g in pair]
    sfx_times = [c.time for c in plan.sfx if getattr(c, "file", "")]
    # Pass 1: collect every eligible boundary with its direction, the
    # concrete lead/lag it could take, and its energy contrast (the
    # selection key — a real hot<->cool change earns the cut before a flat
    # continuity seam).
    cands: list[tuple[float, int, int, str, float]] = []  # (-contrast, i, ...)
    for i in range(len(entries) - 1):
        if i in handled or (i + 1) in handled:
            continue
        prev, nxt = entries[i], entries[i + 1]
        cut = prev.record_end
        if abs(nxt.record_start - cut) > _JL_ON_CUT_TOL:
            continue  # a gap (black dip) sits between them — not a cut
        if nxt.transition > _EPS:
            continue  # a dissolve, not a hard cut
        if prev.clip_path == nxt.clip_path:
            continue  # a continuing take — no scene transition to decouple
        if on_any(cut, plan.drop_marks) or on_any(cut, gap_edges):
            continue  # sync / silence edges are sacred
        if on_any(cut, sfx_times):
            continue  # a placed SFX/impact owns this cut
        lab_out = _phase_label_at(plan.phases, prev.record_start)
        lab_in = _phase_label_at(plan.phases, nxt.record_start)
        if lab_out == "climax" or lab_in == "climax":
            continue  # peaks live in the climax; leave the sync alone
        prev_len = prev.record_end - prev.record_start
        next_len = nxt.record_end - nxt.record_start
        room = min(prev_len, next_len) - _JL_MIN_SOLO
        if room <= _EPS:
            continue  # too tight to decouple without a solo-audio sliver
        amount = min(base, room)
        e_out = _PHASE_ENERGY.get(lab_out or "", 0.5)
        e_in = _PHASE_ENERGY.get(lab_in or "", 0.5)
        if e_in > e_out + _EPS:
            # cool -> hot: J-cut, the next shot's audio anticipates. Needs
            # the incoming shot's head handles (file-relative source before
            # its in-point).
            lead = min(amount, max(0.0, nxt.source_start))
            if lead <= _EPS:
                continue
            cands.append((-(e_in - e_out), i, i + 1, "J", lead))
        else:
            # hot -> cool or even: L-cut, the previous shot's audio rings
            # past. Needs the outgoing shot's tail handles; an unknown clip
            # duration blocks it honestly (no invented material).
            if prev.clip_duration <= _EPS:
                continue
            lag = min(amount, max(0.0, prev.clip_duration - prev.source_end))
            if lag <= _EPS:
                continue
            cands.append((-(e_out - e_in), i, i, "L", lag))
    # Pass 2: J/L is a spice — take the highest-contrast boundaries first
    # (ties earliest), cap at _JL_MAX_CUTS, and never two seams in a row.
    cands.sort(key=lambda c: (c[0], c[1]))
    used_boundaries: set[int] = set()
    n_j = n_l = 0
    for _key, i, target, kind, amount in cands:
        if n_j + n_l >= _JL_MAX_CUTS:
            break
        if (i - 1) in used_boundaries or (i + 1) in used_boundaries:
            continue  # keep decoupled seams apart
        if kind == "J":
            existing = edits.get(target, (0.0, 0.0))
            edits[target] = (amount, existing[1])
            n_j += 1
        else:
            existing = edits.get(target, (0.0, 0.0))
            edits[target] = (existing[0], amount)
            n_l += 1
        used_boundaries.add(i)
    if n_j or n_l:
        bits = []
        if n_j:
            bits.append(f"{n_j} J-cut{'s' if n_j != 1 else ''}")
        if n_l:
            bits.append(f"{n_l} L-cut{'s' if n_l != 1 else ''}")
        notes.append(
            "J/L cuts: " + " + ".join(bits)
            + f" — original sound decoupled {base:.02f}s from the picture "
            "at quiet transitions (music bed and grid unmoved)"
        )
    return edits, notes


def montage_to_timeline(
    plan: MontagePlan,
    fps: float,
    name: str = "Monteur Montage",
    audio: str = "music",
    canvas: str = "hd",
    jl_cuts: bool = False,
) -> Timeline:
    """Render a MontagePlan as a Timeline (footage on V1, sound per ``audio``).

    ``audio`` picks what plays under the pictures:

    * ``"music"`` (default) — the song on A1, exactly as before. A plan
      with an adaptive music window (``music_in`` / ``music_out``) places
      the A1 clip at record ``music_in`` for ``(music_out or duration) -
      music_in`` seconds, sourced from ``music_start + music_in`` — the
      record<->song mapping is unchanged, the bed is simply silent under
      the dry open. A plan with deliberate silences (``music_gaps``)
      splits the bed into ONE A1 clip per audible span
      (:func:`music_bed_segments`); each post-gap clip continues from
      ``music_start + its record start``, so the beat grid holds.
    * ``"mix"`` — the song on A1 PLUS one A2 audio clip per video entry
      carrying the clip's own sound (same source range and source_name as
      the video entry), e.g. engine sound recorded straight into the clips.
    * ``"original"`` — NO song clip; each entry's own audio on A1 (the
      ride-POV mode, and the only valid mode for a no-music plan).

    Any other value raises ValueError listing the three; ``"music"``/
    ``"mix"`` raise ValueError when the plan has no ``music_path``.

    ``canvas`` picks the timeline's shape and resolution from
    :data:`CANVASES`: ``"hd"`` (default, 1920x1080) / ``"uhd"``
    (3840x2160) for 16:9, ``"vertical"`` / ``"vertical-uhd"`` for 9:16
    Shorts/Reels, ``"cine"`` / ``"cine-uhd"`` for 2.39:1 cinemascope.
    Unknown values raise ValueError listing the presets. Footage keeps
    its own aspect ratio — reframe in Resolve after import. A ``cine*``
    canvas appends a note to the plan explaining that 16:9 footage shows
    SIDE bars (pillarbox) until Resolve's Image Scaling is set to "Scale
    full frame with crop" — that setting fills the width and yields the
    classic top/bottom cinema bars on a 16:9 export.

    A plan with ``dips`` (smash-to-black title slots) leaves black gaps on
    V1 and drops a "Title slot" marker on each gap. Entries with a vision
    ``label`` carry it as clip metadata (``"label"``); when the entry right
    after a dip has one, the marker's note names it ("0.4s of black —
    next: <label>") instead of the generic title reminder. A composed act
    title (``plan.title_texts``, from :mod:`monteur.compose`) wins over
    both: the marker then reads "0.4s of black — title: <text>".

    Entries with a dissolve (``transition`` > 0) carry it in the video
    clip's metadata (``"transition"`` = ``"dissolve"``,
    ``"transition_frames"`` = the length in frames) so the EDL/FCPXML
    writers can emit it; the plan's fades land in ``timeline.metadata``
    as ``"fade_in_frames"`` / ``"fade_out_frames"``.

    A plan with an SFX layer (``plan.sfx``, from ``plan_montage(...,
    sfx=True)``) gets one Green marker per cue at the cue's start frame —
    name ``"SFX: <kind>"``, note ``"<query> — <note>"`` — so the planned
    sound design shows up right on the timeline in Resolve.

    Cues WITH a concrete ``file`` (a placed sound element,
    :mod:`monteur.elements`) additionally become REAL audio clips on a
    dedicated SFX track: ``"A3"`` in ``"mix"`` mode (the song owns A1 and
    the camera sound A2) and ``"A2"`` otherwise (``"music"``: song on A1;
    ``"original"``: camera sound on A1). Each clip records at ``cue.time``
    for ``min(cue.duration, file duration, montage end)`` — the file's
    real duration is probed when the media tooling is available and rides
    along as ``media_duration_seconds`` for the writers, exactly like
    entries do. The Green marker stays (it documents the intent); cues
    without a file stay marker-only.

    ``jl_cuts`` (blueprint 2.3, default False = byte-identical) decouples
    the ORIGINAL-audio edit from the picture cut at chosen quiet
    transitions (mix/original modes only): the own-audio clip's record and
    source edges shift by :func:`jl_audio_edits`' small fps-quantized
    lead/lag so it OVERLAPS the neighbour's picture (FCPXML writes it as a
    connected clip Resolve round-trips natively). A plan already carrying
    hand-authored ``audio_lead``/``audio_lag`` applies them even with
    ``jl_cuts=False``. The music bed and the picture grid never move.
    """
    if audio not in _AUDIO_MODES:
        valid = ", ".join(_AUDIO_MODES)
        raise ValueError(f"unknown audio mode {audio!r}; valid modes: {valid}")
    if audio in ("music", "mix") and not plan.music_path:
        raise ValueError(
            f'plan has no music; audio mode {audio!r} needs a song — '
            'use audio="original"'
        )
    if canvas not in CANVASES:
        valid = ", ".join(sorted(CANVASES))
        raise ValueError(f"unknown canvas {canvas!r}; valid canvases: {valid}")
    width, height = CANVASES[canvas]
    if canvas.startswith("cine"):
        # A 2.39:1 timeline fits 16:9 footage with SIDE bars by default,
        # which is never what "cinemascope" means to anyone. Tell the
        # editor the one Resolve setting that produces the cinema look.
        hint = (
            "cine canvas: 16:9 footage shows side bars until Resolve "
            "fills the width — set Project Settings > Image Scaling > "
            '"Scale full frame with crop" (crops top/bottom; the classic '
            "cinema bars appear when you export or view in 16:9)"
        )
        if hint not in plan.notes:
            plan.notes.append(hint)
    timeline = Timeline(name=name, fps=fps, width=width, height=height)
    own_audio_track = {"mix": "A2", "original": "A1"}.get(audio)
    # J/L cuts (blueprint 2.3): decouple the original-audio edit from the
    # picture cut at chosen transitions. Applied only when asked (or when
    # the plan already carries hand-authored offsets) so default timelines
    # keep the audio clip's record/source ranges identical to the video's.
    jl_edits: dict[int, tuple[float, float]] = {}
    if own_audio_track and (
        jl_cuts or any(e.audio_lead or e.audio_lag for e in plan.entries)
    ):
        jl_edits, jl_notes = jl_audio_edits(plan, fps)
        for note in jl_notes:
            if note not in plan.notes:
                plan.notes.append(note)
    for e_index, entry in enumerate(plan.entries):
        stem = PurePath(entry.clip_path).stem
        rec_in = seconds_to_frames(entry.record_start, fps)
        rec_out = seconds_to_frames(entry.record_end, fps)
        src_in = seconds_to_frames(entry.source_start, fps)
        src_len = entry.source_end - entry.source_start
        rec_len = entry.record_end - entry.record_start
        if abs(src_len - rec_len) < _EPS:
            # Keep source and record durations frame-exact together.
            src_out = src_in + (rec_out - rec_in)
        else:
            src_out = seconds_to_frames(entry.source_end, fps)
        clip = Clip(
            name=stem,
            track="V1",
            kind=VIDEO,
            source_in=src_in,
            source_out=src_out,
            record_in=rec_in,
            record_out=rec_out,
            source_name=stem,
            source_file=entry.clip_path,
        )
        # Real source metadata for the exporters: the file's embedded start
        # timecode and true duration. Resolve refuses to link media whose
        # claimed source ranges don't match the actual file, so the FCPXML/EDL
        # writers shift source positions by media_start at write time
        # (source_in/source_out stay file-relative here).
        clip.metadata["media_start_seconds"] = entry.media_start
        clip.metadata["media_duration_seconds"] = entry.clip_duration
        if entry.label:
            # The vision label travels with the clip so exports and the web
            # UI can say WHAT each cut shows, not just where it came from.
            clip.metadata["label"] = entry.label
        transition_frames = round(entry.transition * fps)
        if transition_frames > 0:
            clip.metadata["transition"] = "dissolve"
            clip.metadata["transition_frames"] = transition_frames
        timeline.clips.append(clip)
        if own_audio_track:
            # The entry's own sound (DJI Mic engine audio etc.): same source
            # range and source_name as the video entry, on A2 ("mix") or A1
            # ("original"). A J/L cut (blueprint 2.3) shifts THIS audio
            # clip's edges off the picture cut: a J-cut (lead) pulls the
            # head earlier (source + record), an L-cut (lag) pushes the tail
            # later — the clip then overlaps the neighbour's picture and
            # FCPXML writes it as a connected clip (Resolve round-trips it).
            lead, lag = jl_edits.get(e_index, (0.0, 0.0))
            a_rec_in, a_rec_out = rec_in, rec_out
            a_src_in, a_src_out = src_in, src_out
            if lead > _EPS:
                lead_f = seconds_to_frames(lead, fps)
                a_rec_in -= lead_f
                a_src_in -= lead_f
            if lag > _EPS:
                lag_f = seconds_to_frames(lag, fps)
                a_rec_out += lag_f
                a_src_out += lag_f
            timeline.clips.append(
                Clip(
                    name=stem,
                    track=own_audio_track,
                    kind=AUDIO,
                    source_in=a_src_in,
                    source_out=a_src_out,
                    record_in=a_rec_in,
                    record_out=a_rec_out,
                    source_name=stem,
                    source_file=entry.clip_path,
                    metadata={
                        "media_start_seconds": entry.media_start,
                        "media_duration_seconds": entry.clip_duration,
                    },
                )
            )
    if plan.fade_in > _EPS:
        timeline.metadata["fade_in_frames"] = seconds_to_frames(plan.fade_in, fps)
    if plan.fade_out > _EPS:
        timeline.metadata["fade_out_frames"] = seconds_to_frames(plan.fade_out, fps)
    if audio != "original":
        music_stem = PurePath(plan.music_path).stem
        # Adaptive music window + deliberate silence: the song enters at
        # record music_in, ends at music_out (0 = the montage end), and
        # breaks over the plan's music_gaps — ONE clip per audible span
        # (music_bed_segments). The record<->song mapping is untouched —
        # record t always plays song time music_start + t — so a segment
        # after a gap CONTINUES from music_start + segment start (the
        # gap's source span is skipped too) and every cut stays on the
        # beat; a delayed entry just mutes the bed under the dry open.
        for seg_index, (seg_lo, seg_hi) in enumerate(music_bed_segments(plan)):
            rec_in_frames = seconds_to_frames(seg_lo, fps)
            rec_out_frames = seconds_to_frames(seg_hi, fps)
            duration_frames = rec_out_frames - rec_in_frames
            if duration_frames <= 0:
                continue
            # The music clip starts at the song offset the cut was built
            # against, so a short montage plays the song's strongest
            # passage rather than its intro.
            music_in = seconds_to_frames(plan.music_start + seg_lo, fps)
            # Keep the source range inside the song: if independent rounding
            # of the offset and the length would read one frame past the
            # end, shift the start back so the clip length stays exact and
            # never over-reads the media.
            if plan.song_duration > 0:
                song_end = seconds_to_frames(plan.song_duration, fps)
                if music_in + duration_frames > song_end:
                    music_in = max(0, song_end - duration_frames)
            timeline.clips.append(
                Clip(
                    name=music_stem,
                    track="A1",
                    kind=AUDIO,
                    source_in=music_in,
                    source_out=music_in + duration_frames,
                    record_in=rec_in_frames,
                    record_out=rec_out_frames,
                    source_name=music_stem,
                    source_file=plan.music_path,
                    # Music has no embedded start timecode we can probe
                    # here, so no media_start_seconds; the real song length
                    # still lets the FCPXML writer claim an honest asset
                    # duration.
                    metadata={"media_duration_seconds": plan.song_duration},
                )
            )
            if seg_index == 0:
                timeline.markers.append(
                    Marker(frame=rec_in_frames, name=f"Cut to {music_stem}")
                )
    for dip_index, (dip_start, dip_len) in enumerate(plan.dips):
        # A composed act title (monteur.compose) names the marker outright;
        # otherwise, when the vision pass labeled the shot that hits out of
        # the black, the title-slot marker says what comes next.
        composed = (
            plan.title_texts[dip_index].strip()
            if dip_index < len(plan.title_texts)
            else ""
        )
        incoming = next(
            (
                e
                for e in plan.entries
                if abs(e.record_start - (dip_start + dip_len)) <= 1e-3
            ),
            None,
        )
        if composed:
            note = f"{dip_len:g}s of black — title: {composed}"
        elif incoming is not None and incoming.label:
            note = f"{dip_len:g}s of black — next: {incoming.label}"
        else:
            note = f"{dip_len:g}s of black — drop a title here"
        timeline.markers.append(
            Marker(
                frame=seconds_to_frames(dip_start, fps),
                name="Title slot",
                note=note,
                color="Blue",
            )
        )
    sfx_track = "A3" if audio == "mix" else "A2"
    for cue in plan.sfx:
        # The planned sound-design layer rides along as Green markers: the
        # editor sees WHERE each effect goes and gets the search query to
        # paste into the SFX library right in the marker note.
        timeline.markers.append(
            Marker(
                frame=seconds_to_frames(cue.time, fps),
                name=f"SFX: {cue.kind}",
                note=f"{cue.query} — {cue.note}",
                color="Green",
            )
        )
        if not cue.file:
            continue
        # A placed sound element becomes a REAL clip on the SFX track. The
        # file's honest duration (probed like the entries' metadata is)
        # bounds the clip and feeds the writers; without media tooling the
        # cue's own duration is trusted. ``source_offset`` (blueprint 1.3:
        # a riser plays its LAST run-up seconds, a shifted impact skips
        # just enough head that its peak still hits) becomes the clip's
        # source_in, and the play length shrinks by what the offset ate.
        file_duration = _probe_media_duration(cue.file)
        offset = max(0.0, getattr(cue, "source_offset", 0.0) or 0.0)
        length = min(cue.duration, plan.duration - cue.time)
        if file_duration > 0:
            length = min(length, max(0.0, file_duration - offset))
        if length <= _EPS:
            continue
        rec_in = seconds_to_frames(cue.time, fps)
        len_frames = seconds_to_frames(length, fps)
        if len_frames <= 0:
            continue
        src_in = seconds_to_frames(offset, fps)
        stem = PurePath(cue.file).stem
        metadata: dict = {}
        if file_duration > 0:
            metadata["media_duration_seconds"] = file_duration
        timeline.clips.append(
            Clip(
                name=stem,
                track=sfx_track,
                kind=AUDIO,
                source_in=src_in,
                source_out=src_in + len_frames,
                record_in=rec_in,
                record_out=rec_in + len_frames,
                source_name=stem,
                source_file=cue.file,
                metadata=metadata,
            )
        )
    return timeline


def _probe_media_duration(path: str) -> float:
    """A media file's real duration in seconds, 0.0 when it can't be probed.

    Best-effort by design: montage_to_timeline must keep working on plans
    whose element files are elsewhere (another machine, a test fixture) and
    in environments without the media extra — the cue's own duration then
    stands in.
    """
    try:
        from monteur.media import MonteurMediaError, probe
    except ImportError:  # pragma: no cover - media.py is part of the package
        return 0.0
    try:
        return max(0.0, float(probe(path).duration))
    except MonteurMediaError:
        return 0.0


# --- plan persistence & the revision hook ---------------------------------------

# Schema version written by plan_to_dict and required by plan_from_dict.
# Bump when the saved shape changes incompatibly.
PLAN_FORMAT_VERSION = 1


def plan_to_dict(plan: MontagePlan) -> dict:
    """A JSON-ready dict of the full plan — the revision loop's save format.

    Everything round-trips through :func:`plan_from_dict`: every scalar
    field, the entries, the smash-to-black dips, the SFX cues and the notes.
    The ``"monteur_plan"`` key carries :data:`PLAN_FORMAT_VERSION` so a
    future Monteur can refuse (or migrate) old files instead of misreading
    them. ``title_texts`` (the composed act titles, :mod:`monteur.compose`)
    is written only when set, so plans without it serialize exactly as
    before; :func:`plan_from_dict` tolerates its absence. Likewise a cue's
    ``file`` (a concrete sound element, :mod:`monteur.elements`) is written
    only when set — plans without placed elements stay byte-identical to
    plans saved before the field existed.
    """
    data = {
        "monteur_plan": PLAN_FORMAT_VERSION,
        "music_path": plan.music_path,
        "duration": plan.duration,
        "music_start": plan.music_start,
        "song_duration": plan.song_duration,
        "fade_in": plan.fade_in,
        "fade_out": plan.fade_out,
        "entries": [
            {
                key: value
                for key, value in asdict(entry).items()
                # Only-when-set fields (J/L audio offsets, blueprint 2.3):
                # plans without decoupled audio edits serialize exactly as
                # before the fields existed, and old readers keep loading them.
                if not (key in ("audio_lead", "audio_lag") and not value)
                # peak_source / shot_size (blueprint 4.1) are in-memory
                # critique aids, and reframe_focus (auto-reframe 9:16) is an
                # in-memory render aid — NEVER serialized, so the default plan
                # stays byte-identical.
                and key not in ("peak_source", "shot_size", "reframe_focus")
            }
            for entry in plan.entries
        ],
        "notes": list(plan.notes),
        "dips": [[start, length] for start, length in plan.dips],
        "sfx": [
            {
                key: value
                for key, value in asdict(cue).items()
                # Only-when-set fields (file, source_offset): plans without
                # placed/offset elements serialize exactly as before.
                if not (key in ("file", "source_offset") and not value)
            }
            for cue in plan.sfx
        ],
    }
    if plan.title_texts:
        data["title_texts"] = [str(text) for text in plan.title_texts]
    # The adaptive music window is written only when set (tolerant like
    # title_texts): full-length plans serialize exactly as before.
    if plan.music_in > 0:
        data["music_in"] = plan.music_in
    if plan.music_out > 0:
        data["music_out"] = plan.music_out
    # Deliberate silences, only when set (title_texts pattern): plans
    # without gaps — and every music_flow="continuous" plan — serialize
    # exactly as before the field existed.
    if plan.music_gaps:
        data["music_gaps"] = [[start, end] for start, end in plan.music_gaps]
    # Timeline-strip metadata (phases / music_energy / beat_marks /
    # drop_marks) is written only when set — exactly like title_texts, so
    # plans saved before the strip existed (and plans without music or
    # phases) stay byte-identical.
    if plan.phases:
        data["phases"] = [[start, end, label] for start, end, label in plan.phases]
    if plan.music_energy:
        data["music_energy"] = [float(v) for v in plan.music_energy]
    if plan.beat_marks:
        data["beat_marks"] = [float(t) for t in plan.beat_marks]
    if plan.drop_marks:
        data["drop_marks"] = [float(t) for t in plan.drop_marks]
    if plan.tempo:
        data["tempo"] = plan.tempo
    return data


def plan_from_dict(data: dict) -> MontagePlan:
    """Rebuild a MontagePlan from :func:`plan_to_dict` output.

    Raises ValueError with a clear message when the dict is not a Monteur
    plan at all (no ``"monteur_plan"`` key), was written by an unsupported
    schema version, or is structurally malformed.
    """
    version = data.get("monteur_plan")
    if version is None:
        raise ValueError(
            "not a Monteur plan: the 'monteur_plan' version key is missing "
            "(plans are written by 'monteur create --save-plan')"
        )
    if version != PLAN_FORMAT_VERSION:
        raise ValueError(
            f"unsupported plan version {version!r}; this Monteur reads "
            f"version {PLAN_FORMAT_VERSION}"
        )
    try:
        return MontagePlan(
            music_path=data["music_path"],
            duration=float(data["duration"]),
            music_start=float(data.get("music_start", 0.0)),
            song_duration=float(data.get("song_duration", 0.0)),
            fade_in=float(data.get("fade_in", 0.0)),
            fade_out=float(data.get("fade_out", 0.0)),
            entries=[MontageEntry(**entry) for entry in data.get("entries", [])],
            notes=list(data.get("notes", [])),
            dips=[(float(start), float(length)) for start, length in data.get("dips", [])],
            sfx=[SfxCue(**cue) for cue in data.get("sfx", [])],
            # Version-tolerant: plans saved before the composer existed (and
            # plans without composed titles) simply have no such key.
            title_texts=[str(text) for text in data.get("title_texts", [])],
            # Same tolerance for the timeline-strip metadata: plans saved
            # before the strip existed simply have none of these keys.
            phases=[
                (float(start), float(end), str(label))
                for start, end, label in data.get("phases", [])
            ],
            music_energy=[float(v) for v in data.get("music_energy", [])],
            beat_marks=[float(t) for t in data.get("beat_marks", [])],
            drop_marks=[float(t) for t in data.get("drop_marks", [])],
            tempo=float(data.get("tempo", 0.0)),
            # Tolerant like title_texts: plans saved before the adaptive
            # music window existed simply have neither key.
            music_in=float(data.get("music_in", 0.0)),
            music_out=float(data.get("music_out", 0.0)),
            # Tolerant like title_texts: plans saved before deliberate
            # silence existed simply have no such key.
            music_gaps=[
                (float(start), float(end))
                for start, end in data.get("music_gaps", [])
            ],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"malformed plan JSON: {exc}") from exc


def adjust_entry_boundary(
    plan: MontagePlan, slot: int, transition: str
) -> MontagePlan:
    """Change ONE boundary of the cut — pure plan surgery, no re-plan.

    ``slot`` is the 0-based index into ``plan.entries`` (record order) and
    ``transition`` says how the cut INTO that entry should read — one of
    :data:`ARRANGEMENT_TRANSITIONS`:

    * ``"cut"`` — hard cut: the entry's dissolve is cleared, and a black
      dip sitting on the boundary is removed (the outgoing shot gets its
      carved-off tail back — the exact reverse of the smash below).
    * ``"dissolve"`` — the entry dissolves in with the planner's own rule:
      ``min(_MAX_DISSOLVE, half the slot length)``, beat-quantized through
      the shared :func:`quantize_finish` helper against the plan's own
      pulse (blueprint 1.7 — the surgery contract: an adjusted boundary
      gets exactly the length :func:`_plan_finishing` would have chosen).
      A dip on the boundary is removed first — a shot cannot both smash
      out of black and dissolve.
    * ``"smash"`` — the classic trailer breath: the OUTGOING entry gives
      up its last :func:`_dip_seconds` (the :data:`_DIP_SECONDS` target,
      beat-quantized like the planner's carving — same contract) to a
      black gap (a title slot) and the entry hits out of black; its
      dissolve is cleared. Already smashed boundaries are left alone
      (noted, not an error).

    Everything else — the record grid, every other entry, the SFX cues —
    stays bit-identical; ``title_texts`` stays aligned with ``dips`` (an
    inserted dip gets "" when titles exist, a removed dip drops its
    title). The original plan object is never modified; the returned plan
    carries a ``boundary:`` note saying what changed.

    Raises ValueError for an unknown transition, a slot outside the plan,
    slot 0 (the first shot's boundary is the montage fade-in, not a cut),
    a smash whose outgoing slot is too short (the same
    :data:`_DIP_MIN_REMAINDER` rule the planner uses), and a dip removal
    whose outgoing clip has no source material left to grow back into.
    """
    if transition not in ARRANGEMENT_TRANSITIONS:
        valid = ", ".join(ARRANGEMENT_TRANSITIONS)
        raise ValueError(
            f"unknown transition {transition!r}; valid transitions: {valid}"
        )
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        raise ValueError("slot must be an entry index (0-based)")
    if slot < 0 or slot >= len(plan.entries):
        raise ValueError(
            f"slot {slot + 1} is not in this plan (it has {len(plan.entries)} entries)"
        )
    if slot == 0:
        raise ValueError(
            "the first shot has no incoming cut — its boundary is the "
            "montage fade-in"
        )

    entries = [replace(e) for e in plan.entries]
    dips = list(plan.dips)
    titles = list(plan.title_texts)
    entry = entries[slot]
    prev = entries[slot - 1]
    bound = entry.record_start
    added: list[str] = []

    # A black dip already sitting on this boundary (its END = the entry's
    # record_start, within the cut-lead tolerance)?
    dip_at = next(
        (
            k
            for k, (d_start, d_len) in enumerate(dips)
            if abs(d_start + d_len - bound) <= _BOUNDARY_EPS + _EPS
        ),
        None,
    )

    if transition == "smash":
        # Blueprint 1.7 (the adjust_entry_boundary contract): surgery
        # carves with the SAME beat-quantized dip length and remainder
        # floor the planner used — one shared helper, so an adjusted
        # boundary is indistinguishable from a planned one.
        dip_len = _dip_seconds(plan)
        if dip_at is not None:
            added.append(
                f"boundary: slot {slot + 1} already smashes in from black — kept"
            )
        else:
            length = prev.record_end - prev.record_start
            if length - dip_len < _dip_min_remainder(plan):
                raise ValueError(
                    f"slot {slot} is too short ({length:.2f}s) to give up "
                    f"{dip_len:g}s for a smash to black"
                )
            prev.record_end -= dip_len
            prev.source_end -= dip_len
            insert_at = bisect.bisect_left(
                [d_start for d_start, _ in dips], prev.record_end
            )
            dips.insert(insert_at, (prev.record_end, dip_len))
            if titles:
                titles.insert(insert_at, "")
            added.append(
                f"boundary: smash to black into slot {slot + 1} "
                f"({dip_len:g}s title gap)"
            )
        entry.transition = 0.0
    else:  # "cut" / "dissolve"
        if dip_at is not None:
            d_start, d_len = dips[dip_at]
            if abs(prev.record_end - d_start) > _BOUNDARY_EPS + _EPS:
                raise ValueError(
                    f"the black dip before slot {slot + 1} does not sit on the "
                    "previous shot's cut — it cannot be removed here"
                )
            room = (
                prev.clip_duration - prev.source_end
                if prev.clip_duration > _EPS
                else d_len  # unknown clip length: trust the carve-off's origin
            )
            if room + _EPS < d_len:
                raise ValueError(
                    f"cannot remove the black dip before slot {slot + 1}: "
                    f"{PurePath(prev.clip_path).name} has only {max(room, 0.0):.2f}s "
                    f"of source left after its cut"
                )
            prev.record_end += d_len
            prev.source_end += d_len
            del dips[dip_at]
            if dip_at < len(titles):
                del titles[dip_at]
            added.append(f"boundary: removed the black dip before slot {slot + 1}")
        if transition == "dissolve":
            # The planner's own rule, beat-quantized (blueprint 1.7) —
            # the shared _dissolve_seconds keeps the surgery contract.
            entry.transition = _dissolve_seconds(plan, entry)
            added.append(
                f"boundary: dissolve into slot {slot + 1} ({entry.transition:g}s)"
            )
        else:
            entry.transition = 0.0
            added.append(f"boundary: hard cut into slot {slot + 1}")

    adjusted = replace(
        plan,
        entries=entries,
        dips=dips,
        title_texts=titles,
        notes=list(plan.notes) + added,
        sfx=list(plan.sfx),
        phases=list(plan.phases),
        music_energy=list(plan.music_energy),
        beat_marks=list(plan.beat_marks),
        drop_marks=list(plan.drop_marks),
        music_gaps=list(plan.music_gaps),
    )
    # Accidental-silence guard: a removed dip takes its deliberate silence
    # with it (a gap without its dip is exactly the accident the feature
    # forbids); an inserted dip gets NO gap — surgery plans no carrier cue.
    _prune_music_gaps(adjusted)
    return adjusted


def pin_entry(plan: MontagePlan, entry: MontageEntry) -> None:
    """Force ``entry`` into the plan verbatim — the revision pinning hook.

    Used by :mod:`monteur.revise` for shots the editor wants untouched: a
    COPY of ``entry`` (exact source material AND record window) is inserted
    and everything in its way yields. Entries overlapping the pinned record
    window are trimmed to make room (an entry spanning the whole window is
    split in two; source positions move 1:1 with the record trim, clamped to
    the entry's own material), entries fully inside it are dropped, and dips
    overlapping it are dropped — the pinned shot covers that time. The
    right-hand remainder of a split entry loses its dissolve (``transition``
    = 0): the cut out of the pinned shot is a hard cut. Entries stay sorted
    by record time; SFX cues and notes are left alone.
    """
    lo, hi = entry.record_start, entry.record_end
    kept: list[MontageEntry] = []
    for e in plan.entries:
        if e.record_end <= lo + _EPS or e.record_start >= hi - _EPS:
            kept.append(e)
            continue
        if e.record_start < lo - _EPS:  # the part before the pinned window
            head_end = min(e.source_end, e.source_start + (lo - e.record_start))
            kept.append(replace(e, record_end=lo, source_end=head_end))
        if e.record_end > hi + _EPS:  # the part after the pinned window
            tail_start = min(e.source_end, e.source_start + (hi - e.record_start))
            kept.append(
                replace(e, record_start=hi, source_start=tail_start, transition=0.0)
            )
    kept.append(replace(entry))
    kept.sort(key=lambda e: e.record_start)
    plan.entries = kept
    plan.dips = [
        (start, length)
        for start, length in plan.dips
        if start + length <= lo + _EPS or start >= hi - _EPS
    ]
    # Accidental-silence guard: the pinned shot claims its time, song
    # included — gaps overlapping it (and gaps whose dip just vanished)
    # are dropped, never left behind as unexplained silence.
    _prune_music_gaps(plan, protect=(lo, hi))
