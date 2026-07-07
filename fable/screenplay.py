"""Screenplay model and Fountain parser.

Fountain (https://fountain.io) is the plain-text screenplay format. Fable
reads a screenplay to know, scene by scene, which dialogue lines the film
needs — the reference the assembly engine matches take transcripts against.
"""

from __future__ import annotations

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


def parse_fountain(text: str) -> Screenplay:
    """Parse Fountain (or plain-text screenplay) into a Screenplay."""
    raise NotImplementedError
