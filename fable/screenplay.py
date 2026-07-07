"""Screenplay model and Fountain parser.

Fountain (https://fountain.io) is the plain-text screenplay format. Fable
reads a screenplay to know, scene by scene, which dialogue lines the film
needs — the reference the assembly engine matches take transcripts against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ACTION = "action"
DIALOGUE = "dialogue"


@dataclass
class Element:
    """One block in a scene: a piece of action or one character's speech."""

    kind: str  # ACTION | DIALOGUE
    text: str
    character: str = ""  # DIALOGUE only, normalized upper-case
    parenthetical: str = ""  # DIALOGUE only, e.g. "(quiet)"


@dataclass
class Scene:
    heading: str  # e.g. "INT. KITCHEN - NIGHT"
    number: str = ""  # e.g. "12" if the script numbers scenes
    elements: list[Element] = field(default_factory=list)

    def dialogue(self) -> list[Element]:
        return [e for e in self.elements if e.kind == DIALOGUE]

    def characters(self) -> list[str]:
        seen: dict[str, None] = {}
        for e in self.dialogue():
            seen.setdefault(e.character, None)
        return list(seen)


@dataclass
class Screenplay:
    title: str = ""
    scenes: list[Scene] = field(default_factory=list)

    def scene_by_number(self, number: str) -> Scene | None:
        for scene in self.scenes:
            if scene.number == str(number):
                return scene
        return None


# --- Fountain parsing helpers -----------------------------------------------

# INT / EXT / EST / INT./EXT. / INT/EXT / I/E followed by "." or space.
_HEADING_RE = re.compile(r"^(?:INT\.?/EXT|INT|EXT|EST|I/E)[. ]", re.IGNORECASE)
# Trailing scene number, e.g. "#12#" or "#12A#".
_SCENE_NUMBER_RE = re.compile(r"\s*#([A-Za-z0-9.\-]+)#\s*$")
# Boneyard comments (may span lines) and notes (inline or block).
_BONEYARD_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_NOTE_RE = re.compile(r"\[\[.*?\]\]", re.DOTALL)
# Title-page "Key: value" line.
_TITLE_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z ]*):\s*(.*)$")
_TITLE_PAGE_KEYS = {
    "title", "credit", "author", "authors", "source", "draft date",
    "date", "contact", "notes", "copyright", "revision",
}
# Punctuation permitted inside a character cue besides letters/digits.
_CUE_PUNCT = " .'-()^&,#/"


def _clean_text(s: str) -> str:
    """Strip emphasis markup and collapse whitespace."""
    s = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", s)
    s = re.sub(r"_(.+?)_", r"\1", s)
    return " ".join(s.split())


def _match_scene_heading(line: str) -> tuple[str | None, str]:
    """Return (heading, scene_number); heading is None if not a heading."""
    s = line
    number = ""
    forced = s.startswith(".") and not s.startswith("..")
    if not forced and not _HEADING_RE.match(s):
        return None, ""
    m = _SCENE_NUMBER_RE.search(s)
    if m:
        number = m.group(1)
        s = s[: m.start()]
    if forced:
        s = s[1:]
    return _clean_text(s).upper(), number


def _is_ignorable(line: str) -> bool:
    """Transitions, centered text, sections, synopses, page breaks."""
    if line.startswith(("#", "=", ">")):
        return True
    return line == line.upper() and line.endswith("TO:")


def _is_character_cue(line: str) -> bool:
    s = line.strip()
    if not s or s[0] in ".!>#=~":
        return False
    if s.startswith("@"):  # forced character
        return True
    if s != s.upper() or s.endswith("TO:") or _HEADING_RE.match(s):
        return False
    if not all(ch.isalnum() or ch in _CUE_PUNCT for ch in s):
        return False
    base = re.sub(r"\(.*?\)", "", s).replace("^", "").strip()
    return any(ch.isalpha() for ch in base)


def _normalize_character(cue: str) -> str:
    """"ANNA (V.O.) ^" -> "ANNA"; "@McClane" -> "MCCLANE"."""
    s = cue.strip()
    if s.startswith("@"):
        s = s[1:]
    if s.endswith("^"):
        s = s[:-1]
    s = re.sub(r"\(.*?\)", " ", s)
    return " ".join(s.upper().split())


def _consume_title_page(lines: list[str], screenplay: Screenplay) -> int:
    """Parse a leading title page; return the index where the body starts."""
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return i
    m = _TITLE_KEY_RE.match(lines[i])
    if not m or m.group(1).strip().lower() not in _TITLE_PAGE_KEYS:
        return 0  # no title page; the body starts at the top
    key = ""
    values: dict[str, str] = {}
    while i < len(lines) and lines[i].strip():
        line = lines[i]
        m = _TITLE_KEY_RE.match(line)
        if m and not line[:1].isspace():
            key = m.group(1).strip().lower()
            values[key] = m.group(2).strip()
        elif line[:1].isspace() and key:  # indented continuation line
            values[key] = (values[key] + " " + line.strip()).strip()
        else:
            break
        i += 1
    screenplay.title = _clean_text(values.get("title", ""))
    return i


def parse_fountain(text: str) -> Screenplay:
    """Parse Fountain (or plain-text screenplay) into a Screenplay."""
    screenplay = Screenplay()
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    text = _BONEYARD_RE.sub("", text)
    text = _NOTE_RE.sub("", text)
    lines = text.split("\n")

    i = _consume_title_page(lines, screenplay)
    n = len(lines)
    # Implicit untitled scene for body content before the first heading; it
    # is only appended to the screenplay once it receives an element.
    current = Scene(heading="")
    attached = False

    def add(element: Element) -> None:
        nonlocal attached
        if not attached:
            screenplay.scenes.append(current)
            attached = True
        current.elements.append(element)

    while i < n:
        if not lines[i].strip():
            i += 1
            continue
        j = i
        while j < n and lines[j].strip():
            j += 1
        block = [lines[k].strip() for k in range(i, j)]
        i = j

        heading, number = _match_scene_heading(block[0])
        if heading is not None:
            current = Scene(heading=heading, number=number)
            screenplay.scenes.append(current)
            attached = True
            block = block[1:]  # rare: text glued to the heading block
            if not block:
                continue

        block = [ln for ln in block if not _is_ignorable(ln)]
        if not block:
            continue

        if len(block) >= 2 and _is_character_cue(block[0]):
            parens: list[str] = []
            spoken: list[str] = []
            for ln in block[1:]:
                if ln.startswith("(") and ln.endswith(")"):
                    parens.append(_clean_text(ln))
                else:
                    spoken.append(ln)
            add(Element(
                kind=DIALOGUE,
                text=_clean_text(" ".join(spoken)),
                character=_normalize_character(block[0]),
                parenthetical=" ".join(parens),
            ))
            continue

        action = _clean_text(" ".join(
            ln[1:] if ln.startswith("!") else ln for ln in block
        ))
        if action:
            add(Element(kind=ACTION, text=action))

    return screenplay
