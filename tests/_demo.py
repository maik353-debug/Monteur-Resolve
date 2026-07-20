"""Location of the synthetic demo footage used by the end-to-end tests.

The footage is NOT in the repository (it is a few MB of generated media)
— run ``python tests/make_demo_footage.py`` once to create it under
``tests/.demo-footage`` (gitignored). Tests that need it skip cleanly
when the folder is missing. ``MONTEUR_DEMO_FOOTAGE`` overrides the
location.
"""

from __future__ import annotations

import os
from pathlib import Path

_override = os.environ.get("MONTEUR_DEMO_FOOTAGE", "")
DEMO = Path(_override) if _override else Path(__file__).resolve().parent / ".demo-footage"
