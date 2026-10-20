import asyncio
import json

import httpx
import pytest

from autoreply import llm as llm_module
from autoreply.llm import LLMError, OpenRouterClient

K1, K2 = "sk-or-key-one-aaaaaaaaaaaa", "sk-or-key-two-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def fast_sleep(_):
        return None
    monkeypatch.setattr(llm_module.asyncio, "sleep", fast_sleep)


@pytest.fixture
def cfg(settings):
    settings.update({"openrouter_api_keys": [K1, K2], "openrouter_model": "m/a",
                     "openrouter_fallback_models": ["m/b"]})
    return settings


def ok(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def run(cfg, handler, messages=None):
    client = OpenRouterClient(cfg, transport=httpx.MockTransport(handler))
    result = asyncio.run(client.chat(messages or [{"role": "user", "content": "hi"}]))
    return client, result


def test_success_strips_thinking_and_disables_reasoning(cfg):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return ok("<think>hmm</think> Salom!")

    client, result = run(cfg, handler)
    assert result == "Salom!"
    assert seen["auth"] == f"Bearer {K1}"
    assert seen["body"]["model"] == "m/a"
    assert seen["body"]["reasoning"]["enabled"] is False
    assert client.last_model == "m/a"


def test_rate_limited_key_fails_over_to_next_key(cfg):
    used = []

    def handler(request):
        key = request.headers["authorization"].split()[1]
        used.append(key)
        if key == K1:
            return httpx.Response(429, json={"error": {"message": "rate"}}, headers={"retry-after": "30"})
        return ok("ikkinchi kalit")

    client, result = run(cfg, handler)
    assert result == "ikkinchi kalit"
    assert used == [K1, K2]
    status = {s["key"][-4:]: s["status"] for s in client.key_status()}
    assert status[K1[-4:]] == "cooldown" and status[K2[-4:]] == "ok"

    # the parked key is skipped on the next request
    used.clear()
    asyncio.run(client.chat([{"role": "user", "content": "yana"}]))
    assert used == [K2]


def test_model_unavailable_falls_back_to_next_model(cfg):
    models = []

    def handler(request):
        model = json.loads(request.content)["model"]
        models.append(model)
        if model == "m/a":
            return httpx.Response(404, json={"error": {"message": "No endpoints"}})
        return ok("zaxira model")

    client, result = run(cfg, handler)
    assert result == "zaxira model" and models == ["m/a", "m/b"]
    assert client.last_model == "m/b"


def test_reasoning_field_dropped_if_rejected(cfg):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "reasoning" in body:
            return httpx.Response(400, json={"error": {"message": "reasoning is not supported"}})
        return ok("ok")

    _, result = run(cfg, handler)
    assert result == "ok" and "reasoning" not in bodies[-1]


def test_images_stripped_when_no_model_accepts_them(cfg):
    def handler(request):
        body = json.loads(request.content)
        if isinstance(body["messages"][-1]["content"], list):
            return httpx.Response(400, json={"error": {"message": "image input not supported"}})
        return ok("matn bilan javob")

    msg = [{"role": "user", "content": [
        {"type": "text", "text": "qara"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]}]
    _, result = run(cfg, handler, msg)
    assert result == "matn bilan javob"


def test_all_keys_bad_raises(cfg):
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    with pytest.raises(LLMError):
        run(cfg, handler)


def test_no_keys(settings):
    settings.update({"openrouter_api_keys": []})
    with pytest.raises(LLMError):
        run(settings, lambda r: ok("x"))


def test_moonlight_is_last_resort(cfg):
    cfg.update({"moonlight_enabled": True, "moonlight_base_url": "https://abdulhodiy-mzwax.hf.space",
                "moonlight_api_key": ""})
    cfg.boot.hf_token = "hf_owner"
    seen = {}

    def handler(request):
        if request.url.host == "abdulhodiy-mzwax.hf.space":
            seen["auth"] = request.headers["authorization"]
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"success": True, "answer": "MoonLight javobi"})
        return httpx.Response(429, json={"error": {"message": "limit"}})

    msgs = [{"role": "system", "content": "Sen yordamchisan"},
            {"role": "user", "content": "salom"}, {"role": "assistant", "content": "va alaykum"},
            {"role": "user", "content": "qalaysan?"}]
    client, result = run(cfg, handler, msgs)
    assert result == "MoonLight javobi" and client.last_model == "moonlight"
    assert seen["path"] == "/api/ai/ask" and seen["auth"] == "Bearer hf_owner"
    assert seen["body"]["question"].endswith("qalaysan?") and "Sen yordamchisan" in seen["body"]["question"]
    assert seen["body"]["history"] == [{"role": "user", "content": "salom"},
                                       {"role": "assistant", "content": "va alaykum"}]


def test_moonlight_openai_compatible_v1(cfg):
    cfg.update({"moonlight_enabled": True, "moonlight_base_url": "https://moon.example/v1",
                "moonlight_api_key": "ml-key"})

    def handler(request):
        if request.url.host == "moon.example":
            assert request.url.path == "/v1/chat/completions"
            assert request.headers["authorization"] == "Bearer ml-key"
            return ok("v1 javob")
        return httpx.Response(429, json={"error": {"message": "limit"}})

    assert run(cfg, handler)[1] == "v1 javob"


def test_moonlight_disabled_still_raises(cfg):
    cfg.update({"moonlight_enabled": False, "moonlight_base_url": "https://moon.example/v1"})

    def handler(request):
        assert request.url.host != "moon.example"
        return httpx.Response(429, json={"error": {"message": "limit"}})

    with pytest.raises(LLMError):
        run(cfg, handler)
