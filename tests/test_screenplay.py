"""Tests for the Fountain screenplay parser."""

from fable.screenplay import ACTION, DIALOGUE, parse_fountain

FIXTURE = """\
Title: The Long Night
Credit: Written by
Author: M. Example
Draft date: 2026-07-01

INT. KITCHEN - NIGHT #1#

A kettle *whistles* on the stove. [[check prop]]
ANNA, 30s, stares out the window.

ANNA (V.O.)
(quietly)
I never wanted to come back here.
Not after everything.

BEN
You said that last time.

ANNA ^
(sharp)
And I meant it.

CUT TO:

EXT. GARDEN - CONTINUOUS #2A#

/* editor: maybe trim
this whole beat */
Ben follows her out.

.FLASHBACK - GARDEN, TEN YEARS AGO

= They bury the box.

# Act One

> THE END <
"""


def test_title_page():
    sp = parse_fountain(FIXTURE)
    assert sp.title == "The Long Night"


def test_scene_count_headings_numbers():
    sp = parse_fountain(FIXTURE)
    assert [s.heading for s in sp.scenes] == [
        "INT. KITCHEN - NIGHT",
        "EXT. GARDEN - CONTINUOUS",
        "FLASHBACK - GARDEN, TEN YEARS AGO",
    ]
    assert [s.number for s in sp.scenes] == ["1", "2A", ""]


def test_scene_by_number():
    sp = parse_fountain(FIXTURE)
    assert sp.scene_by_number("2A").heading == "EXT. GARDEN - CONTINUOUS"
    assert sp.scene_by_number("1").heading == "INT. KITCHEN - NIGHT"
    assert sp.scene_by_number("99") is None


def test_action_block_joined_emphasis_and_note_stripped():
    scene = parse_fountain(FIXTURE).scenes[0]
    first = scene.elements[0]
    assert first.kind == ACTION
    # Two lines of one block joined by a space; *emphasis* and [[note]] gone.
    assert first.text == (
        "A kettle whistles on the stove. ANNA, 30s, stares out the window."
    )


def test_dialogue_character_normalized_from_vo_extension():
    scene = parse_fountain(FIXTURE).scenes[0]
    anna = scene.elements[1]
    assert anna.kind == DIALOGUE
    assert anna.character == "ANNA"  # "(V.O.)" stripped
    assert anna.parenthetical == "(quietly)"
    # Parenthetical excluded from text; lines joined with a single space.
    assert anna.text == "I never wanted to come back here. Not after everything."


def test_plain_dialogue_and_dual_dialogue_caret():
    scene = parse_fountain(FIXTURE).scenes[0]
    ben = scene.elements[2]
    assert (ben.kind, ben.character, ben.text) == (
        DIALOGUE, "BEN", "You said that last time."
    )
    dual = scene.elements[3]
    assert dual.character == "ANNA"  # trailing "^" stripped
    assert dual.parenthetical == "(sharp)"
    assert dual.text == "And I meant it."
    assert scene.characters() == ["ANNA", "BEN"]


def test_transition_section_synopsis_centered_not_elements():
    sp = parse_fountain(FIXTURE)
    all_text = " | ".join(e.text for s in sp.scenes for e in s.elements)
    assert "CUT TO" not in all_text
    assert "Act One" not in all_text
    assert "bury the box" not in all_text
    assert "THE END" not in all_text


def test_boneyard_stripped_forced_heading_scene():
    sp = parse_fountain(FIXTURE)
    garden = sp.scenes[1]
    assert [e.text for e in garden.elements] == ["Ben follows her out."]
    assert "maybe trim" not in garden.elements[0].text
    flashback = sp.scenes[2]  # forced "." heading, leading dot stripped
    assert flashback.heading == "FLASHBACK - GARDEN, TEN YEARS AGO"
    assert flashback.elements == []


def test_crlf_and_bom_tolerated():
    text = "\ufeff" + FIXTURE.replace("\n", "\r\n")
    sp = parse_fountain(text)
    assert sp.title == "The Long Night"
    assert len(sp.scenes) == 3
    assert sp.scenes[0].elements[1].character == "ANNA"


def test_plain_text_fallback_single_untitled_scene():
    sp = parse_fountain(
        "Two people argue in a kitchen.\n"
        "\n"
        "ANNA\n"
        "Give it back.\n"
        "\n"
        "BEN\n"
        "No.\n"
    )
    assert len(sp.scenes) == 1
    scene = sp.scenes[0]
    assert scene.heading == ""
    assert [e.kind for e in scene.elements] == [ACTION, DIALOGUE, DIALOGUE]
    assert scene.characters() == ["ANNA", "BEN"]
    assert scene.elements[1].text == "Give it back."


def test_parentheticals_folded_and_forced_character():
    sp = parse_fountain(
        "INT. HALL - DAY\n"
        "\n"
        "CARL\n"
        "(beat)\n"
        "Fine.\n"
        "(to Anna)\n"
        "Take it.\n"
        "\n"
        "@McCLANE\n"
        "Yippee ki-yay.\n"
        "\n"
        "..and the hall empties.\n"
    )
    assert len(sp.scenes) == 1
    carl, mcclane, tail = sp.scenes[0].elements
    assert carl.parenthetical == "(beat) (to Anna)"  # later ones folded in
    assert carl.text == "Fine. Take it."
    assert mcclane.character == "MCCLANE"  # "@" stripped, upper-cased
    assert mcclane.text == "Yippee ki-yay."
    assert tail.kind == ACTION  # ".." is not a forced heading


def test_title_page_continuation_lines():
    sp = parse_fountain(
        "Title:\n"
        "    _**Brick & Steel**_\n"
        "    Full Retired\n"
        "Credit: Written by\n"
        "Author: X\n"
        "\n"
        "INT. GARAGE - DAY\n"
        "\n"
        "He waits.\n"
    )
    assert sp.title == "Brick & Steel Full Retired"
    assert len(sp.scenes) == 1
    assert sp.scenes[0].heading == "INT. GARAGE - DAY"


def test_uppercase_line_alone_is_action_not_cue():
    sp = parse_fountain(
        "INT. VAULT - DAY\n"
        "\n"
        "SILENCE\n"
        "\n"
        "Then an alarm.\n"
    )
    kinds = [e.kind for e in sp.scenes[0].elements]
    assert kinds == [ACTION, ACTION]  # cue needs a following non-blank line
