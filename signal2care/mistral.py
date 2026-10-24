"""Mistral client: chat (with model fallback), embeddings, OCR.

Free mode is limited to about 1 request per second, so calls are spaced. A model that
answers 403 (tier not allowed) or reports a 0 per-minute limit is skipped for 10 minutes.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger("s2c.mistral")

BLOCK_SECONDS = 600


class MistralError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status


class LLMUnavailable(MistralError):
    """No configured chat model can answer right now."""

    def __init__(self, message: str):
        super().__init__(0, message)


def _error_code(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("code", ""))
    except Exception:
        return ""


class MistralClient:
    BASE = "https://api.mistral.ai/v1"

    def __init__(self, api_key: str, http: httpx.AsyncClient, *, embed_model: str, ocr_model: str,
                 min_interval: float = 1.1):
        self._key = api_key
        self._http = http
        self._embed_model = embed_model
        self._ocr_model = ocr_model
        self._min = min_interval
        self._gate = asyncio.Lock()
        self._last = 0.0
        self._blocked: dict[str, float] = {}

    # ---- plumbing
    def _is_blocked(self, name: str) -> bool:
        return self._blocked.get(name, 0.0) > time.monotonic()

    def _block(self, name: str) -> None:
        self._blocked[name] = time.monotonic() + BLOCK_SECONDS

    async def _post(self, path: str, body: dict) -> httpx.Response:
        async with self._gate:
            wait = self._min - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
        return await self._http.post(
            self.BASE + path, json=body, headers={"Authorization": f"Bearer {self._key}"}, timeout=120
        )

    # ---- chat
    async def chat(self, messages: list[dict], models: tuple[str, ...], *, max_tokens: int = 700,
                   temperature: float = 0.5, json_mode: bool = False) -> tuple[str, dict]:
        problems: list[str] = []
        for model in models:
            if self._is_blocked(model):
                problems.append(f"{model}: skipped (blocked recently)")
                continue
            for attempt in range(3):
                body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens,
                              "temperature": temperature}
                if json_mode:
                    body["response_format"] = {"type": "json_object"}
                resp = await self._post("/chat/completions", body)
                if resp.status_code == 200:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    if isinstance(content, list):
                        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                    return content or "", {"model": model, **(data.get("usage") or {})}
                if resp.status_code == 403:
                    self._block(model)
                    problems.append(f"{model}: 403 {_error_code(resp)}")
                    break
                if resp.status_code == 429:
                    if resp.headers.get("x-ratelimit-limit-req-minute") == "0":
                        self._block(model)
                        problems.append(f"{model}: per-minute limit is 0")
                        break
                    delay = float(resp.headers.get("retry-after", 2 ** attempt))
                    await asyncio.sleep(min(delay, 8))
                    continue
                if resp.status_code >= 500:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise MistralError(resp.status_code, f"{model}: {resp.text[:200]}")
            else:
                problems.append(f"{model}: gave up after retries")
        raise LLMUnavailable("; ".join(problems) or "no models configured")

    # ---- embeddings
    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self._is_blocked("embed"):
            raise MistralError(0, "embeddings blocked recently")
        resp = await self._post("/embeddings", {"model": self._embed_model, "input": texts})
        if resp.status_code != 200:
            if resp.status_code in (403, 429):
                self._block("embed")
            raise MistralError(resp.status_code, resp.text[:200])
        return [d["embedding"] for d in resp.json()["data"]]

    # ---- OCR
    async def ocr(self, data_url: str, kind: str = "image_url") -> str:
        """kind is 'image_url' for images or 'document_url' for PDFs."""
        if self._is_blocked("ocr"):
            raise MistralError(0, "ocr blocked recently")
        resp = await self._post("/ocr", {"model": self._ocr_model, "document": {"type": kind, kind: data_url}})
        if resp.status_code != 200:
            if resp.status_code in (403, 429):
                self._block("ocr")
            raise MistralError(resp.status_code, resp.text[:200])
        pages = resp.json().get("pages") or []
        return "\n\n".join((p.get("markdown") or "").strip() for p in pages).strip()
