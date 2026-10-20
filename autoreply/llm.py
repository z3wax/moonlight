"""OpenRouter client with seamless failover across several API keys and models.

For every request the client walks ``[primary model, *fallback models]`` and,
for each model, the API keys that are not cooling down. A key that hits a rate
limit, runs out of credit or is rejected is parked for a while and the next
one is used - the person in the chat never sees the failure.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://openrouter.ai/api/v1"
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    pass


def mask_key(key: str) -> str:
    return f"{key[:9]}…{key[-4:]}" if len(key) > 16 else "…" + key[-4:]


@dataclass
class KeyState:
    key: str
    cooldown_until: float = 0.0
    last_error: str = ""
    last_ok: float = 0.0
    successes: int = 0
    failures: int = 0
    history: list = field(default_factory=list)

    @property
    def available(self) -> bool:
        return time.time() >= self.cooldown_until

    def park(self, seconds: float, reason: str) -> None:
        self.cooldown_until = time.time() + seconds
        self.last_error = reason
        self.failures += 1

    def public(self) -> dict:
        now = time.time()
        return {
            "key": mask_key(self.key),
            "status": "ok" if self.available else "cooldown",
            "cooldown_seconds": max(0, int(self.cooldown_until - now)),
            "last_error": self.last_error,
            "last_ok": self.last_ok,
            "successes": self.successes,
            "failures": self.failures,
        }


def has_images(messages: list[dict]) -> bool:
    return any(isinstance(m.get("content"), list) for m in messages)


def strip_images(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            texts = [p.get("text", "") for p in content if p.get("type") == "text"]
            content = "\n".join(t for t in texts if t) + "\n[rasm biriktirilgan]"
        out.append({**m, "content": content})
    return out


class OpenRouterClient:
    def __init__(self, settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self._keys: dict[str, KeyState] = {}
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            transport=transport,
            headers={
                "HTTP-Referer": "https://github.com/z3wax/moon",
                "X-Title": "Telegram AutoReply",
            },
        )
        self._models_cache: tuple[float, list[dict]] = (0.0, [])
        self.last_model = ""
        self.last_latency = 0.0
        self.last_error = ""
        self.total_requests = 0

    async def close(self) -> None:
        await self._client.aclose()

    # --- key bookkeeping --------------------------------------------------

    def _states(self) -> list[KeyState]:
        keys = [k for k in self.s.openrouter_api_keys if k]
        for k in keys:
            self._keys.setdefault(k, KeyState(k))
        for k in list(self._keys):
            if k not in keys:
                del self._keys[k]
        return [self._keys[k] for k in keys]

    def _usable_keys(self) -> list[KeyState]:
        states = self._states()
        ready = [s for s in states if s.available]
        if ready:
            return ready
        # Everything is parked: try the one that frees up first anyway.
        return sorted(states, key=lambda s: s.cooldown_until)[:1]

    def key_status(self) -> list[dict]:
        return [s.public() for s in self._states()]

    def models(self, model: str | None = None) -> list[str]:
        ordered = [model or self.s.openrouter_model, *self.s.openrouter_fallback_models]
        seen, out = set(), []
        for m in ordered:
            if m and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    # --- requests ---------------------------------------------------------

    def _payload(self, model: str, messages: list[dict], max_tokens: int | None,
                 temperature: float | None, reasoning: bool) -> dict:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": self.s.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.s.max_tokens,
        }
        if reasoning and self.s.reasoning_disabled:
            payload["reasoning"] = {"enabled": False, "exclude": True}
        return payload

    async def _post(self, key: str, payload: dict) -> httpx.Response:
        return await self._client.post(
            "/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
            timeout=self.s.llm_timeout,
        )

    async def chat(self, messages: list[dict], model: str | None = None, *,
                   max_tokens: int | None = None, temperature: float | None = None) -> str:
        if not self._states() and not (self.s.moonlight_enabled and self.s.moonlight_base_url):
            raise LLMError("OpenRouter API kaliti kiritilmagan (admin panel → AI).")
        errors: list[str] = []
        for round_no in range(2):
            for current in self.models(model):
                text = await self._try_model(current, messages, max_tokens, temperature, errors)
                if text:
                    return text
            if has_images(messages):
                # No model accepted the image - answer from text alone.
                messages = strip_images(messages)
            elif round_no == 0:
                await asyncio.sleep(3)
        text = await self._try_moonlight(strip_images(messages), max_tokens, temperature, errors)
        if text:
            return text
        self.last_error = "; ".join(errors[-4:])
        raise LLMError("Barcha kalit/modellar ishlamadi: " + self.last_error)

    async def _try_moonlight(self, messages, max_tokens, temperature, errors) -> str:
        """Last resort: the owner's own OpenAI-compatible MoonLight endpoint."""
        base = (self.s.moonlight_base_url or "").rstrip("/")
        if not (self.s.moonlight_enabled and base):
            return ""
        headers = {}
        if self.s.moonlight_api_key:
            headers["Authorization"] = f"Bearer {self.s.moonlight_api_key}"
        payload = {
            "model": self.s.moonlight_model or "moonlight-v1",
            "messages": messages,
            "temperature": self.s.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.s.max_tokens,
        }
        started = time.monotonic()
        self.total_requests += 1
        try:
            response = await self._client.post(
                f"{base}/chat/completions", json=payload, headers=headers,
                timeout=self.s.llm_timeout,
            )
        except httpx.HTTPError as exc:
            errors.append(f"moonlight: tarmoq ({exc.__class__.__name__})")
            return ""
        if response.status_code != 200:
            errors.append(f"moonlight: {response.status_code} {response.text[:120]}")
            return ""
        text, error = self._extract(response)
        if not text:
            errors.append(f"moonlight: {error or 'bo`sh javob'}")
            return ""
        log.warning("OpenRouter ishlamadi - javob MoonLight'dan olindi")
        self.last_model = f"moonlight:{payload['model']}"
        self.last_latency = time.monotonic() - started
        return text

    async def _try_model(self, model, messages, max_tokens, temperature, errors) -> str:
        reasoning = True
        for state in self._usable_keys():
            for _ in range(2):  # second pass only to drop the reasoning field
                started = time.monotonic()
                self.total_requests += 1
                try:
                    response = await self._post(
                        state.key, self._payload(model, messages, max_tokens, temperature, reasoning)
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    errors.append(f"{model}: tarmoq ({exc.__class__.__name__})")
                    return ""  # network/provider issue: move on to the next model
                code = response.status_code
                body = response.text[:300]
                if code == 200:
                    text, error = self._extract(response)
                    if text:
                        state.last_ok = time.time()
                        state.successes += 1
                        state.last_error = ""
                        self.last_model = model
                        self.last_latency = time.monotonic() - started
                        return text
                    errors.append(f"{model}: {error or 'bo`sh javob'}")
                    return ""
                if code == 400 and reasoning and "reason" in body.lower():
                    reasoning = False
                    continue
                if code in (401, 403):
                    state.park(3600, f"{code}: kalit rad etildi")
                    log.warning("Kalit %s rad etildi (%s) - keyingisiga o'tildi",
                                mask_key(state.key), code)
                elif code == 402:
                    state.park(3600, "402: kredit tugagan")
                    log.warning("Kalit %s: kredit tugagan - keyingisiga o'tildi", mask_key(state.key))
                elif code == 429:
                    wait = _retry_after(response) or 60
                    state.park(min(wait, 900), "429: limit")
                    log.info("Kalit %s limitga yetdi (%s s) - keyingisiga o'tildi",
                             mask_key(state.key), int(wait))
                else:
                    # 400/404 (model unavailable, image unsupported), 5xx: try the next model.
                    errors.append(f"{model}: {code} {body[:120]}")
                    return ""
                errors.append(f"{model}: {code} ({mask_key(state.key)})")
                break  # next key
        return ""

    @staticmethod
    def _extract(response: httpx.Response) -> tuple[str, str]:
        try:
            data = response.json()
        except ValueError:
            return "", "JSON emas"
        if isinstance(data, dict) and data.get("error"):
            return "", str(data["error"])[:200]
        try:
            content = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            return "", "javob formati noto'g'ri"
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return THINK_RE.sub("", content).strip(), ""

    async def list_models(self) -> list[dict]:
        """All OpenRouter models (public endpoint), cached for an hour."""
        cached_at, models = self._models_cache
        if models and time.time() - cached_at < 3600:
            return models
        response = await self._client.get("/models", timeout=30)
        response.raise_for_status()
        out = []
        for m in response.json().get("data", []):
            pricing = m.get("pricing") or {}
            arch = m.get("architecture") or {}
            params = m.get("supported_parameters") or []
            try:
                prompt_price = float(pricing.get("prompt") or 0)
                completion_price = float(pricing.get("completion") or 0)
            except (TypeError, ValueError):
                prompt_price = completion_price = -1
            out.append({
                "id": m.get("id", ""),
                "name": m.get("name") or m.get("id", ""),
                "context": m.get("context_length") or 0,
                "free": prompt_price == 0 and completion_price == 0,
                "vision": "image" in (arch.get("input_modalities") or []),
                "reasoning": "reasoning" in params or "include_reasoning" in params,
                "price_in": prompt_price * 1_000_000 if prompt_price > 0 else 0,
                "price_out": completion_price * 1_000_000 if completion_price > 0 else 0,
            })
        out.sort(key=lambda m: (not m["free"], m["name"].lower()))
        self._models_cache = (time.time(), out)
        return out

    async def check_key(self, key: str) -> dict:
        """Ask OpenRouter about a key (limits/usage) without spending tokens."""
        try:
            response = await self._client.get(
                "/key", headers={"Authorization": f"Bearer {key}"}, timeout=15
            )
        except httpx.HTTPError as exc:
            return {"ok": False, "error": str(exc)}
        if response.status_code != 200:
            return {"ok": False, "error": f"{response.status_code}: {response.text[:200]}"}
        data = response.json().get("data", {})
        return {
            "ok": True,
            "label": data.get("label"),
            "usage": data.get("usage"),
            "limit": data.get("limit"),
            "is_free_tier": data.get("is_free_tier"),
        }


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return float(value) if value else None
    except ValueError:
        return None
