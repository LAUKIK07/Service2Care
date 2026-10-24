"""Deterministic safety gate. No model is involved in these decisions.

It runs before anything else, matches conservatively, and answers with fixed text.
"""
from __future__ import annotations

import re

from .prompts import CRISIS_TEXT, EMERGENCY_TEXT

_CRISIS = re.compile(
    r"suicid|kill myself|end my life|want to die|better off dead|self[- ]?harm|hurt myself"
    r"|no reason to live|don'?t want to be alive|do not want to be alive",
    re.I,
)
_EMERGENCY = re.compile(
    r"chest (pain|pressure|tightness)|heart attack|can'?t breathe|cannot breathe|struggling to breathe"
    r"|(severe|serious|extreme) (shortness of breath|trouble breathing|difficulty breathing)|not breathing"
    r"|\bstroke\b|face (is )?droop|slurred speech|sudden(ly)? (numb|weak)|overdos"
    r"|took too many (pills|tablets)|won'?t stop bleeding|severe bleeding|unconscious",
    re.I,
)


def classify(text: str) -> str | None:
    """Return fixed guidance text if the message needs it, else None."""
    if _CRISIS.search(text or ""):
        return CRISIS_TEXT
    if _EMERGENCY.search(text or ""):
        return EMERGENCY_TEXT
    return None
