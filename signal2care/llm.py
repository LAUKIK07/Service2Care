"""LLM client for any OpenAI-compatible endpoint (DeepInfra by default): chat and embeddings.

Chat takes an ordered list of models and falls through it. A model that rejects the request
(for example it does not accept images) or fails authentication is skipped for 10 minutes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx

log = logging.getLogger("s2c.llm")

BLOCK_SECONDS = 600
_THINK = re.compile(r"<think>.*?</think>", re.S)


class LLMError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status


class LLMUnavailable(LLMError):
    """No configured model can answer right now."""

    def __init__(self, message: str):
        super().__init__(0, message)


def _message(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error")
        return str(err.get("message") if isinstance(err, dict) else err or resp.text)[:200]
    except Exception:
        return resp.text[:200]


def extract_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating code fences and surrounding prose."""
    text = (text or "").strip()
    try:
        value = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object found")
        value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("JSON is not an object")
    return value


class LLMClient:
    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient, *, embed_model: str,
                 min_interval: float = 0.15):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._http = http
        self._embed_model = embed_model
        self._min = min_interval
        self._gate = asyncio.Lock()
        self._last = 0.0
        self._blocked: dict[str, float] = {}

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
        try:
            return await self._http.post(
                self._base + path, json=body, headers={"Authorization": f"Bearer {self._key}"}, timeout=120
            )
        except httpx.TransportError as exc:  # dropped connection, timeout, DNS: retryable
            raise LLMError(0, f"network error ({type(exc).__name__})") from exc

    async def chat(self, messages: list[dict], models: tuple[str, ...], *, max_tokens: int = 700,
                   temperature: float = 0.5, json_mode: bool = False) -> tuple[str, dict]:
        problems: list[str] = []
        for model in models:
            if self._is_blocked(model):
                problems.append(f"{model}: skipped (failed recently)")
                continue
            use_json = json_mode
            for attempt in range(3):
                body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens,
                              "temperature": temperature}
                if use_json:
                    body["response_format"] = {"type": "json_object"}
                try:
                    resp = await self._post("/chat/completions", body)
                except LLMError as exc:
                    if attempt == 2:
                        problems.append(f"{model}: {exc}")
                        break
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                if resp.status_code == 200:
                    data = resp.json()
                    msg = data["choices"][0]["message"]
                    content = _THINK.sub("", msg.get("content") or "").strip()
                    if content:
                        return content, {"model": model, **(data.get("usage") or {})}
                    problems.append(f"{model}: empty answer (finish={data['choices'][0].get('finish_reason')})")
                    break
                detail = _message(resp)
                if resp.status_code == 400 and use_json and "response_format" in detail.lower():
                    use_json = False  # this model has no JSON mode; the caller parses leniently
                    continue
                if resp.status_code == 400 and "image" in detail.lower():
                    self._block(model)
                    problems.append(f"{model}: {detail}")
                    break
                if resp.status_code in (401, 402, 403, 404):
                    self._block(model)
                    problems.append(f"{model}: {resp.status_code} {detail}")
                    break
                if resp.status_code == 429:
                    delay = float(resp.headers.get("retry-after", 2 ** attempt))
                    await asyncio.sleep(min(delay, 8))
                    continue
                if resp.status_code >= 500:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise LLMError(resp.status_code, f"{model}: {detail}")
            else:
                problems.append(f"{model}: gave up after retries")
        raise LLMUnavailable("; ".join(problems) or "no models configured")

    async def _request(self, body: dict) -> dict:
        """One chat call with retries for network errors, 429 and 5xx. Other errors raise at once."""
        last: LLMError | None = None
        for attempt in range(3):
            try:
                resp = await self._post("/chat/completions", body)
            except LLMError as exc:
                last = exc
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            if resp.status_code == 200:
                return resp.json()
            detail = _message(resp)
            if resp.status_code == 429:
                last = LLMError(429, detail)
                await asyncio.sleep(min(float(resp.headers.get("retry-after", 2 ** attempt)), 8))
                continue
            if resp.status_code >= 500:
                last = LLMError(resp.status_code, detail)
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            raise LLMError(resp.status_code, detail)
        raise last or LLMError(0, "gave up after retries")

    async def chat_with_tools(self, messages: list[dict], models: tuple[str, ...], tools: list[dict], run_tool, *,
                              max_rounds: int = 3, max_tokens: int = 700, temperature: float = 0.5) -> tuple[str, dict]:
        """Chat where the model may call tools. `run_tool(name, args)` is async and returns a JSON-able dict.

        The last round offers no tools, so the model must answer. Tool results are capped in size.
        """
        problems: list[str] = []
        for model in models:
            if self._is_blocked(model):
                problems.append(f"{model}: skipped (failed recently)")
                continue
            msgs = list(messages)
            trace: list[dict] = []
            try:
                for rnd in range(max_rounds + 1):
                    body: dict = {"model": model, "messages": msgs, "max_tokens": max_tokens, "temperature": temperature}
                    if rnd < max_rounds:
                        body["tools"] = tools
                        body["tool_choice"] = "auto"
                    data = await self._request(body)
                    msg = data["choices"][0]["message"]
                    calls = msg.get("tool_calls") or []
                    if not calls or rnd == max_rounds:
                        content = _THINK.sub("", msg.get("content") or "").strip()
                        if content:
                            return content, {"model": model, "tools": trace, **(data.get("usage") or {})}
                        problems.append(f"{model}: empty answer")
                        break
                    msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
                    for call in calls[:4]:
                        name = (call.get("function") or {}).get("name", "")
                        try:
                            args = json.loads((call.get("function") or {}).get("arguments") or "{}")
                            if not isinstance(args, dict):
                                args = {}
                        except ValueError:
                            args = {}
                        try:
                            result = await run_tool(name, args)
                        except Exception as exc:  # a failing tool must not kill the turn
                            log.warning("tool %s failed: %s", name, exc)
                            result = {"error": f"tool failed: {str(exc)[:120]}"}
                        trace.append({"name": name, "args": args})
                        msgs.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                     "content": json.dumps(result, default=str)[:6000]})
            except LLMError as exc:
                if exc.status in (400, 401, 402, 403, 404):
                    self._block(model)
                problems.append(f"{model}: {exc}")
        raise LLMUnavailable("; ".join(problems) or "no models configured")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self._is_blocked("embed"):
            raise LLMError(0, "embeddings failed recently")
        for attempt in range(2):
            try:
                resp = await self._post("/embeddings", {"model": self._embed_model, "input": texts,
                                                        "encoding_format": "float"})
                break
            except LLMError:
                if attempt == 1:
                    raise
                await asyncio.sleep(0.8)
        if resp.status_code != 200:
            if resp.status_code in (401, 402, 403, 404):
                self._block("embed")
            raise LLMError(resp.status_code, _message(resp))
        return [d["embedding"] for d in resp.json()["data"]]
