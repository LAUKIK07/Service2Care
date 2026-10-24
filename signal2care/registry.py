"""All the people the replay can play: the invented personas from sim/ and, when present, personas built from real datasets.

Real personas come from `signal2care.realdata`, which is optional. Its contract:
    available() -> {persona_id: {"name", "tag", "blurb", "source", "license", "citation", "channels": [...]}}
    ticks(persona_id) -> 2688 dicts in the same shape as sim.generator.generate():
        {"i", "ts", "day", "state", "quality", "v": {channel: number}}   (a missing channel is absent or None; never invented)
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from .sim.generator import generate
from .sim.personas import PERSONAS as SIM_PERSONAS

log = logging.getLogger("s2c.registry")
HUES = ["#FF385C", "#7C5CFA", "#0EA5E9", "#14B8A6", "#F59E0B", "#22A06B", "#E4572E", "#C026D3"]


@dataclass(frozen=True)
class PersonaInfo:
    id: str
    name: str
    tag: str
    blurb: str
    kind: str                  # synthetic | real
    source: str = ""
    license: str = ""
    citation: str = ""
    channels: tuple = ()
    hue: str = "#FF385C"
    linked: bool = False       # kept for compatibility; which persona a phone maps to is a setting now

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "tag": self.tag, "blurb": self.blurb, "kind": self.kind, "source": self.source,
                "license": self.license, "citation": self.citation, "channels": list(self.channels), "hue": self.hue}


class _Registry(Mapping):
    def __init__(self):
        self._info: dict[str, PersonaInfo] | None = None
        self._ticks: dict[str, list[dict]] = {}
        self._real = None

    def _load(self) -> dict[str, PersonaInfo]:
        if self._info is not None:
            return self._info
        info: dict[str, PersonaInfo] = {}
        for n, (pid, p) in enumerate(SIM_PERSONAS.items()):
            tag = p.name[p.name.find("(") + 1:p.name.find(")")].capitalize() if "(" in p.name else "Synthetic"
            info[pid] = PersonaInfo(pid, p.name.split(" (")[0], tag, p.description, "synthetic", "Seeded physiology simulator",
                                    "Invented", "", (), HUES[n % len(HUES)], p.linked)
        try:
            from . import realdata                                  # optional module
            self._real = realdata
            for n, (pid, m) in enumerate(realdata.available().items(), start=len(info)):
                info[pid] = PersonaInfo(pid, m.get("name", pid), m.get("tag", "Real data"), m.get("blurb", ""), "real", m.get("source", ""),
                                        m.get("license", ""), m.get("citation", ""), tuple(m.get("channels", ())), HUES[n % len(HUES)])
        except ImportError:
            pass
        except Exception:
            log.exception("real-data personas unavailable")
        self._info = info
        return info

    def reload(self) -> None:
        self._info, self._ticks = None, {}

    def ticks(self, pid: str) -> list[dict]:
        if pid not in self._ticks:
            info = self._load()[pid]
            self._ticks[pid] = generate(SIM_PERSONAS[pid]) if info.kind == "synthetic" else self._real.ticks(pid)
        return self._ticks[pid]

    def public(self) -> list[dict]:
        return [p.public() for p in self._load().values()]

    def __getitem__(self, key: str) -> PersonaInfo:
        return self._load()[key]

    def __iter__(self):
        return iter(self._load())

    def __len__(self) -> int:
        return len(self._load())


PERSONAS = _Registry()
