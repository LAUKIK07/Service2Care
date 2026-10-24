"""Literature lookup restricted to one trusted source: PubMed (NIH). No arbitrary web pages are fetched.

Each result carries what a careful reader needs: the study type (so a systematic review is not confused with a single small
study), the year, and the abstract sentences that contain numbers. The chat may only repeat a study's finding as "studies report ..."
with the PubMed id, and `guard.grounding_problems` checks that anything it quotes was in these results.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time

import httpx

log = logging.getLogger("s2c.research")

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "about", "does", "how", "are", "can", "what", "when", "why", "your", "any", "level", "levels"}
GRADES = [  # (publication type, plain description), strongest first
    ("Meta-Analysis", "meta-analysis (pools many studies)"),
    ("Systematic Review", "systematic review (searches all studies on a question)"),
    ("Randomized Controlled Trial", "randomized trial"),
    ("Clinical Trial", "clinical trial"),
    ("Observational Study", "observational study (shows association, not cause)"),
    ("Review", "narrative review (an expert summary)"),
]


_last = [0.0]


async def _get(http: httpx.AsyncClient, path: str, params: dict) -> httpx.Response:
    """NCBI allows 3 requests a second without a key. Space calls out and retry once on 429."""
    for attempt in range(3):
        wait = 0.45 - (time.monotonic() - _last[0])
        if wait > 0:
            await asyncio.sleep(wait)
        _last[0] = time.monotonic()
        r = await http.get(f"{BASE}/{path}", params=params, timeout=30)
        if r.status_code != 429:
            return r
        await asyncio.sleep(1.0 + attempt)
    return r


def grade_of(pubtypes: list[str]) -> str:
    for t, desc in GRADES:
        if t in pubtypes:
            return desc
    return "single study (shows association at most, not cause)"


def relevance(query: str, title: str, abstract: str) -> float:
    """Share of the question's content words that appear (by 5-letter stem) in the paper's title or abstract."""
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9\-]+", query) if len(w) > 2 and w.lower() not in _STOP]
    if not words:
        return 0.0
    hay = (title + " " + abstract).lower()
    return sum(1 for w in words if w[:5] in hay) / len(words)


def _sentences(abstract: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", abstract)
    keep = [p.strip() for p in parts if re.search(r"\d", p) or re.search(r"\b(associated|increase|decrease|higher|lower|reduced|improv)", p, re.I)]
    return [p[:320] for p in keep][:3]


async def search_pubmed(http: httpx.AsyncClient, query: str, n: int = 4, reviews_first: bool = True) -> list[dict]:
    query = re.sub(r"\s+", " ", query or "").strip()[:200]
    if not query:
        return []
    words = [w for w in re.findall(r"[A-Za-z0-9\-]+", query) if len(w) > 2 and w.lower() not in _STOP]
    attempts = [query] + [" ".join(words[:k]) for k in (5, 4, 3, 2) if len(words) > k]    # relax a too-specific query step by step
    ids: list[str] = []
    used = query
    for q in dict.fromkeys(attempts):
        term = f"({q}) AND humans[MeSH Terms] AND hasabstract"
        r = await _get(http, "esearch.fcgi", {"db": "pubmed", "term": term, "retmode": "json", "retmax": n * 3, "sort": "relevance"})
        ids = (r.json().get("esearchresult") or {}).get("idlist") or []
        if ids:
            used = q
            break
    if not ids:
        return []
    s = await _get(http, "esummary.fcgi", {"db": "pubmed", "id": ",".join(ids), "retmode": "json"})
    summ = s.json().get("result") or {}
    a = await _get(http, "efetch.fcgi", {"db": "pubmed", "id": ",".join(ids), "retmode": "xml"})
    abstracts: dict[str, str] = {}
    for art in re.findall(r"<PubmedArticle>.*?</PubmedArticle>", a.text, re.S):
        pm = re.search(r"<PMID[^>]*>(\d+)</PMID>", art)
        parts = re.findall(r"<AbstractText[^>]*>(.*?)</AbstractText>", art, re.S)
        if pm and parts:
            abstracts[pm.group(1)] = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", " ".join(parts)))).strip()
    papers = []
    for pid in ids:
        item = summ.get(pid) or {}
        ab = abstracts.get(pid)
        year = (item.get("pubdate") or "")[:4]
        if not ab or (year.isdigit() and int(year) < 2011):
            continue
        types = [t for t in (item.get("pubtype") or []) if isinstance(t, str)]
        rel = relevance(query, item.get("title") or "", ab)
        papers.append({"relevance": round(rel, 2), "pmid": pid, "title": item.get("title"), "journal": item.get("source"), "year": (item.get("pubdate") or "")[:4],
                       "study_type": grade_of(types), "pubtypes": types, "url": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
                       "key_sentences": _sentences(ab), "abstract": ab[:1500], "matched_query": used})
    papers = [p for p in papers if p["relevance"] >= 0.6]           # off-topic hits are dropped, not offered
    rank = {d: i for i, (_, d) in enumerate(GRADES)}
    if reviews_first:
        papers.sort(key=lambda p: rank.get(p["study_type"], 99))
    return papers[:n]
