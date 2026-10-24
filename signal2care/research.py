"""Literature lookup restricted to one trusted source: PubMed (NIH). No arbitrary web pages are fetched."""
from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger("s2c.research")

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


async def search_pubmed(http: httpx.AsyncClient, query: str, n: int = 3) -> list[dict]:
    query = re.sub(r"\s+", " ", query or "").strip()[:200]
    if not query:
        return []
    r = await http.get(f"{BASE}/esearch.fcgi", params={"db": "pubmed", "term": query, "retmode": "json",
                                                       "retmax": n, "sort": "relevance"}, timeout=20)
    ids = (r.json().get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return []
    s = await http.get(f"{BASE}/esummary.fcgi", params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"}, timeout=20)
    result = s.json().get("result") or {}
    papers = []
    for pid in ids:
        item = result.get(pid) or {}
        abstract = ""
        try:
            a = await http.get(f"{BASE}/efetch.fcgi", params={"db": "pubmed", "id": pid, "rettype": "abstract",
                                                              "retmode": "text"}, timeout=20)
            abstract = re.sub(r"\s+", " ", a.text).strip()[:900]
        except httpx.HTTPError:
            pass
        papers.append({"title": item.get("title"), "journal": item.get("source"),
                       "year": (item.get("pubdate") or "")[:4], "pmid": pid,
                       "url": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/", "abstract_excerpt": abstract})
    return papers
