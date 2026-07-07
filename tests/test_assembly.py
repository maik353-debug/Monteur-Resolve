"""Tests for the auto-assembly engine (fable.assembly).

All fixtures are synthetic and built in code: Screenplay/Scene/Element
objects constructed directly (parse_fountain is not used) and transcripts
with hand-placed timings so windows and frame numbers can be asserted
exactly.
"""

from fable.assembly import (
    AssemblyPlan,
    SceneAssembly,
    Segment,
    TakeSource,
    assembly_to_timeline,
    match_takes_to_scene,
    plan_assembly,
)
from fable.model import Transcript, TranscriptSegment
from fable.screenplay import ACTION, DIALOGUE, Element, Scene, Screenplay


def _seg(index, start, end, text):
    return TranscriptSegment(index=index, start=start, end=end, text=text)


def _take(name, segs, scene_hint=""):
    return TakeSource(
        name=name, transcript=Transcript(segments=segs), scene_hint=scene_hint
    )


LINE1 = "Hello there, how are you doing today?"
LINE2 = "I am fine, thank you very much."
LINE3 = "Let's go to the market before it rains."


def _three_line_scene(number="1"):
    return Scene(
        heading="INT. KITCHEN - NIGHT",
        number=number,
        elements=[
            Element(kind=DIALOGUE, text=LINE1, character="ANNA"),
            Element(kind=DIALOGUE, text=LINE2, character="BEN"),
            Element(kind=DIALOGUE, text=LINE3, character="ANNA"),
        ],
    )


def test_best_take_wins_and_windows_are_exact():
    scene = _three_line_scene()
    take_a = _take(
        "S1_T01",
        [
            _seg(0, 0.0, 2.0, "Hello there, how are you doing today?"),
            _seg(1, 2.5, 4.5, "I am fine, thank you very much."),
            _seg(2, 5.0, 7.5, "Let's go to the market before it rains."),
        ],
    )
    take_b = _take(
        "S1_T02",
        [
            _seg(0, 0.0, 2.0, "Hello there, how are you doing today?"),
            _seg(1, 2.5, 5.0, "Let's go to the market before it rains."),
        ],
    )

    matches, scores = match_takes_to_scene(scene, [take_a, take_b])

    a_matches = sorted(
        (m for m in matches if m.take == "S1_T01"), key=lambda m: m.element_index
    )
    assert [(m.element_index, m.start, m.end) for m in a_matches] == [
        (0, 0.0, 2.0),
        (1, 2.5, 4.5),
        (2, 5.0, 7.5),
    ]
    assert all(m.similarity > 0.95 for m in a_matches)
    assert a_matches[0].text == "Hello there, how are you doing today?"

    b_matches = {m.element_index for m in matches if m.take == "S1_T02"}
    assert b_matches == {0, 2}

    score_a = next(s for s in scores if s.take == "S1_T01")
    score_b = next(s for s in scores if s.take == "S1_T02")
    assert score_a.coverage == 1.0
    assert abs(score_b.coverage - 2 / 3) < 1e-9
    assert score_a.total > score_b.total

    plan = plan_assembly(Screenplay(scenes=[scene]), [take_a, take_b])
    assert plan.scenes[0].segments[0].take == "S1_T01"


def test_restart_counts_fluff_and_later_attempt_wins():
    line = "The red fox jumps over the lazy dog tonight."
    scene = Scene(
        heading="INT. YARD - DAY",
        number="1",
        elements=[Element(kind=DIALOGUE, text=line, character="ANNA")],
    )
    take = _take(
        "S1_T01",
        [
            _seg(0, 0.0, 1.8, "The red fox uh the red--"),
            _seg(1, 2.2, 4.6, "The red fox jumps over the lazy dog tonight."),
        ],
    )

    matches, scores = match_takes_to_scene(scene, [take])

    assert len(matches) == 1
    assert (matches[0].start, matches[0].end) == (2.2, 4.6)
    assert matches[0].similarity > 0.95
    assert scores[0].fluffs >= 1


def test_scene_hint_routes_takes():
    scene1 = _three_line_scene(number="1")
    scene2 = Scene(
        heading="EXT. STREET - DAY",
        number="2",
        elements=[Element(kind=DIALOGUE, text=LINE1, character="ANNA")],
    )
    hinted = _take(
        "S2_T01", [_seg(0, 0.0, 2.0, LINE1)], scene_hint="02"
    )
    unhinted = _take("ROAM_T01", [_seg(0, 0.0, 2.0, LINE1)])

    plan = plan_assembly(Screenplay(scenes=[scene1, scene2]), [hinted, unhinted])

    scene1_takes = {s.take for s in plan.scenes[0].take_scores}
    scene2_takes = {s.take for s in plan.scenes[1].take_scores}
    assert scene1_takes == {"ROAM_T01"}
    assert scene2_takes == {"S2_T01", "ROAM_T01"}


def test_no_candidate_takes_leaves_scene_empty_with_note():
    scene = _three_line_scene(number="1")
    hinted_elsewhere = _take("S9_T01", [_seg(0, 0.0, 2.0, LINE1)], scene_hint="9")

    plan = plan_assembly(Screenplay(scenes=[scene]), [hinted_elsewhere])

    sa = plan.scenes[0]
    assert sa.segments == []
    assert sa.unmatched_lines == [0, 1, 2]
    assert any(n.startswith("no takes matched scene") for n in sa.notes)


def test_second_take_fills_missing_line():
    scene = _three_line_scene()
    take_a = _take(
        "S1_T01",
        [
            _seg(0, 0.0, 2.0, LINE1),
            _seg(1, 2.5, 4.5, LINE2),
        ],
    )
    take_b = _take("S1_T02", [_seg(0, 1.0, 3.5, LINE3)])

    solo = plan_assembly(Screenplay(scenes=[scene]), [take_a, take_b])
    assert solo.scenes[0].unmatched_lines == [2]

    plan = plan_assembly(
        Screenplay(scenes=[scene]), [take_a, take_b], max_takes_per_scene=2
    )
    sa = plan.scenes[0]
    assert sa.unmatched_lines == []
    covered = {
        (idx, seg.take) for seg in sa.segments for idx in seg.element_indexes
    }
    assert covered == {(0, "S1_T01"), (1, "S1_T01"), (2, "S1_T02")}
    assert any("covers 2/3" in n for n in sa.notes)
    assert any("covers 1/3" in n for n in sa.notes)


def test_unmatched_line_reported_with_note():
    scene = _three_line_scene()
    take = _take(
        "S1_T01",
        [
            _seg(0, 0.0, 2.0, LINE1),
            _seg(1, 2.5, 4.5, LINE2),
        ],
    )

    plan = plan_assembly(Screenplay(scenes=[scene]), [take])

    sa = plan.scenes[0]
    assert sa.unmatched_lines == [2]
    assert any("line 2 missing everywhere" in n for n in sa.notes)


def test_adjacent_lines_merge_into_one_segment():
    scene = Scene(
        heading="INT. KITCHEN - NIGHT",
        number="1",
        elements=[
            Element(kind=ACTION, text="Anna stirs the pot."),
            Element(kind=DIALOGUE, text=LINE1, character="ANNA"),
            Element(kind=DIALOGUE, text=LINE2, character="BEN"),
        ],
    )
    take = _take(
        "S1_T01",
        [
            _seg(0, 0.0, 2.0, LINE1),
            _seg(1, 3.0, 5.0, LINE2),  # 1.0s gap < 1.5s -> merges
        ],
    )

    plan = plan_assembly(Screenplay(scenes=[scene]), [take])

    sa = plan.scenes[0]
    assert len(sa.segments) == 1
    seg = sa.segments[0]
    assert seg.element_indexes == [1, 2]  # indexes into scene.elements
    assert (seg.start, seg.end) == (0.0, 5.0)
    assert LINE1 in seg.text and LINE2 in seg.text


def test_wide_gap_produces_two_segments():
    scene = _three_line_scene()
    take = _take(
        "S1_T01",
        [
            _seg(0, 0.0, 2.0, LINE1),
            _seg(1, 4.0, 6.0, LINE2),  # 2.0s gap >= 1.5s -> separate segments
        ],
    )

    plan = plan_assembly(Screenplay(scenes=[scene]), [take])
    assert len(plan.scenes[0].segments) == 2


def test_scene_without_dialogue_gets_note():
    scene = Scene(
        heading="EXT. FIELD - DAWN",
        number="1",
        elements=[Element(kind=ACTION, text="Wind moves the grass.")],
    )
    plan = plan_assembly(Screenplay(scenes=[scene]), [])
    sa = plan.scenes[0]
    assert sa.segments == []
    assert "scene has no dialogue" in sa.notes


def test_assembly_to_timeline_frames_pairs_and_markers():
    plan = AssemblyPlan(
        scenes=[
            SceneAssembly(
                scene_index=0,
                heading="INT. KITCHEN - NIGHT",
                segments=[
                    Segment(
                        take="A",
                        start=0.3,
                        end=2.1,
                        element_indexes=[0],
                        text="Hello there, how are you doing today? Great.",
                    )
                ],
            ),
            SceneAssembly(
                scene_index=1,
                heading="EXT. STREET - DAY",
                segments=[
                    Segment(
                        take="A",
                        start=5.1,
                        end=6.5,
                        element_indexes=[1],
                        text="Bye.",
                    )
                ],
            ),
        ]
    )
    take = _take("A", [_seg(0, 0.0, 6.8, "irrelevant, sets transcript duration")])

    tl = assembly_to_timeline(plan, [take], fps=25, handles=0.5)

    v = tl.video_clips()
    a = tl.audio_clips()
    assert len(v) == 2 and len(a) == 2
    assert all(c.track == "V1" for c in v)
    assert all(c.track == "A1" for c in a)

    # Clip 1: handle clamped at 0 -> source 0.0..2.6s = frames 0..65
    assert (v[0].source_in, v[0].source_out) == (0, 65)
    assert (v[0].record_in, v[0].record_out) == (0, 65)
    # Clip 2: end handle clamped to transcript duration 6.8s -> 4.6..6.8s
    assert (v[1].source_in, v[1].source_out) == (115, 170)
    assert (v[1].record_in, v[1].record_out) == (65, 120)  # back-to-back

    for vc, ac in zip(v, a):
        assert (ac.source_in, ac.source_out) == (vc.source_in, vc.source_out)
        assert (ac.record_in, ac.record_out) == (vc.record_in, vc.record_out)
        assert ac.source_name == vc.source_name == "A"

    assert len(v[0].name) == 40  # trimmed to first 40 chars of segment text

    assert [(m.frame, m.name) for m in tl.markers] == [
        (0, "INT. KITCHEN - NIGHT"),
        (65, "EXT. STREET - DAY"),
    ]


def test_coverage_arithmetic():
    plan = AssemblyPlan(
        scenes=[
            SceneAssembly(
                scene_index=0,
                heading="A",
                segments=[Segment(take="T", start=0, end=1, element_indexes=[0, 1])],
                unmatched_lines=[2],
            ),
            SceneAssembly(
                scene_index=1,
                heading="B",
                segments=[Segment(take="T", start=0, end=1, element_indexes=[3])],
                unmatched_lines=[],
            ),
        ]
    )
    assert plan.coverage() == 0.75
    assert AssemblyPlan().coverage() == 0.0


class TestForcedTakes:
    def test_pinned_take_wins_over_score(self):
        from fable.assembly import TakeSource, plan_assembly
        from fable.model import Transcript, TranscriptSegment
        from fable.screenplay import DIALOGUE, Element, Scene, Screenplay

        scene = Scene(
            heading="INT. TEST",
            number="1",
            elements=[
                Element(kind=DIALOGUE, text="The quick brown fox jumps over the dog.", character="A"),
                Element(kind=DIALOGUE, text="And the cat watches from the window sill.", character="B"),
            ],
        )
        good = TakeSource(
            name="T1",
            transcript=Transcript(segments=[
                TranscriptSegment(1, 0.0, 3.0, "The quick brown fox jumps over the dog."),
                TranscriptSegment(2, 3.5, 6.5, "And the cat watches from the window sill."),
            ]),
        )
        worse = TakeSource(
            name="T2",
            transcript=Transcript(segments=[
                TranscriptSegment(1, 0.0, 3.0, "The quick brown fox jumps over a dog."),
            ]),
        )
        plan = plan_assembly(Screenplay(scenes=[scene]), [good, worse], forced={0: "T2"})
        assert all(seg.take == "T2" for seg in plan.scenes[0].segments)
        assert any("pinned by editor" in n for n in plan.scenes[0].notes)

    def test_pinned_take_missing_noted(self):
        from fable.assembly import TakeSource, plan_assembly
        from fable.model import Transcript, TranscriptSegment
        from fable.screenplay import DIALOGUE, Element, Scene, Screenplay

        scene = Scene(
            heading="INT. TEST",
            elements=[Element(kind=DIALOGUE, text="Hello there old friend of mine.", character="A")],
        )
        take = TakeSource(
            name="T1",
            transcript=Transcript(segments=[
                TranscriptSegment(1, 0.0, 2.0, "Hello there old friend of mine."),
            ]),
        )
        plan = plan_assembly(Screenplay(scenes=[scene]), [take], forced={0: "NOPE"})
        assert any("not available" in n for n in plan.scenes[0].notes)
        assert plan.scenes[0].segments[0].take == "T1"
