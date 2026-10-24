"""Jev (TypeSafe System One decision model) through OpenRouter's Decisions API.

Jev takes a state and typed questions and returns probabilities, never text. Use it for narrow
decisions over a typed, already-computed state. It must not compute numbers or see raw user text.
Not wired into the chat path yet; the alert pipeline will use it (see architecture.md).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("s2c.jev")

URL = "https://openrouter.ai/api/alpha/decisions"


class JevError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status


def noul(instructions: str, *, true: str, false: str) -> dict:
    """Yes/no question. Returns the probability of yes."""
    return {"type": "noul", "instructions": instructions, "criteria": {"true": true, "false": false}}


def choice(instructions: str, options: dict[str, str]) -> dict:
    """Pick one option. Include an explicit 'other' or 'none' option so the model can say nothing fits."""
    return {"type": "choice", "instructions": instructions, "criteria": options}


def score(instructions: str, levels: list[str]) -> dict:
    """Place on an ordered scale. Index 0 is the first level."""
    return {"type": "score", "instructions": instructions, "criteria": levels}


@dataclass
class Decision:
    answers: dict
    model: str
    cost: float
    input_tokens: int

    def p_yes(self, name: str) -> float:
        return float(self.answers[name]["noul"])

    def picked(self, name: str) -> tuple[str, float, dict]:
        a = self.answers[name]
        return a["choice"], float(a["confidence"]), a["probabilities"]

    def level(self, name: str) -> float:
        return float(self.answers[name]["score"])


class JevClient:
    def __init__(self, api_key: str, http: httpx.AsyncClient, model: str = "typesafe/jev-1.14"):
        self._key = api_key
        self._http = http
        self.model = model

    async def decide(self, state: str | dict | list, questions: dict[str, dict]) -> Decision:
        try:
            resp = await self._http.post(
                URL, json={"model": self.model, "state": state, "questions": questions},
                headers={"Authorization": f"Bearer {self._key}"}, timeout=60,
            )
        except httpx.TransportError as exc:
            raise JevError(0, f"network error ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            raise JevError(resp.status_code, resp.text[:200])
        body = resp.json()
        usage = body.get("usage") or {}
        return Decision(body["answers"], body.get("model", self.model), float(usage.get("cost") or 0.0),
                        int(usage.get("input_tokens") or 0))
