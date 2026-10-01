"""LLM pool over OpenAI-compatible free tiers (Groq, Cerebras, ...).

Every (provider, model) pair has its own free quota, so calls are spread round-robin across all of them and an
endpoint that returns 429 is cooled down while the others keep working.
"""
import json
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
import random
import re
import threading
import time

from openai import OpenAI

MOCK = os.getenv("MOCK", "0") == "1"
FAST_MODEL = "fast"  # kept for compatibility: callers pass model=llm.FAST_MODEL for short turns

PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY",
             "openai/gpt-oss-120b,qwen/qwen3.8-27b,openai/gpt-oss-20b"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "gpt-oss-120b,llama3.1-8b"),
    "custom": (os.getenv("LLM_BASE_URL", ""), "LLM_API_KEY", os.getenv("LLM_MODEL", "")),
}


class Endpoint:
    def __init__(self, provider, base, key, model):
        self.provider, self.model, self.key = provider, model, key
        self.client = OpenAI(api_key=key, base_url=base, timeout=120, max_retries=0)
        self.cool_until = 0.0
        self.dead = False
        self.extra_ok = True
        self.calls = 0

    def __repr__(self):
        return f"{self.provider}:{self.model}"


def _build():
    eps = []
    for prov, (base, env_key, default_models) in PROVIDERS.items():
        keys = [k.strip() for k in os.getenv(env_key, "").split(",") if k.strip()]
        models = [m.strip() for m in os.getenv(f"{prov.upper()}_MODELS", default_models).split(",") if m.strip()]
        if not base:
            continue
        for k in keys:
            for m in models:
                eps.append(Endpoint(prov, base, k, m))
    return eps


ENDPOINTS = [] if MOCK else _build()
_lock = threading.Lock()
_rr = 0


def _next_endpoint():
    global _rr
    while True:
        with _lock:
            live = [e for e in ENDPOINTS if not e.dead]
            if not live:
                raise RuntimeError("No working LLM endpoint - check GROQ_API_KEY / CEREBRAS_API_KEY")
            now = time.time()
            ready = [e for e in live if e.cool_until <= now]
            if ready:
                _rr += 1
                return ready[_rr % len(ready)]
            wait = min(e.cool_until for e in live) - now
        time.sleep(max(0.5, min(wait, 20)))


def _extra(ep):
    if not ep.extra_ok:
        return {}
    if "gpt-oss" in ep.model:
        return {"reasoning_effort": "low"}
    if "qwen3" in ep.model and ep.provider == "groq":
        return {"reasoning_effort": "none"}
    return {}


def _retry_after(e):
    try:
        h = e.response.headers
        return float(h.get("retry-after") or 0)
    except Exception:  # noqa: BLE001
        return 0


def chat(messages, model=None, temperature=0.8, max_tokens=700, json_mode=False, mock=None):
    if MOCK:
        time.sleep(0.15)
        return mock() if callable(mock) else (mock or "Mock reply.")
    last = None
    for attempt in range(40):
        ep = _next_endpoint()
        kw = dict(_extra(ep))
        if json_mode:
            kw["response_format"] = {"type": "json_object"}
        try:
            r = ep.client.chat.completions.create(model=ep.model, messages=messages, temperature=temperature,
                                                  max_tokens=max_tokens + (600 if "gpt-oss" in ep.model else 0), **kw)
            ep.calls += 1
            text = (r.choices[0].message.content or "").strip()
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
            if not text:
                ep.cool_until = time.time() + 3
                continue
            return text
        except Exception as e:  # noqa: BLE001
            last = e
            msg = str(e).lower()
            status = getattr(e, "status_code", None)
            if status == 429 or "rate limit" in msg or "tokens per" in msg:
                cool = _retry_after(e) or 20
                if "per day" in msg or "tpd" in msg or "rpd" in msg:
                    cool = 3600
                ep.cool_until = time.time() + cool + random.random()
                continue
            if status in (400, 422) and ("reasoning" in msg or "unsupported" in msg or "not supported" in msg) and ep.extra_ok:
                ep.extra_ok = False
                continue
            if status == 400 and "json" in msg and json_mode:
                json_mode = False  # model couldn't produce strict json - fall back to parsing text
                continue
            if status in (401, 403, 404) or "model_not_found" in msg or "does not exist" in msg:
                print(f"[llm] disabling {ep}: {str(e)[:120]}")
                ep.dead = True
                continue
            ep.cool_until = time.time() + 5  # 5xx / timeout
            time.sleep(1)
    raise RuntimeError(f"LLM failed after retries: {last}")


def parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e > s:
            return json.loads(text[s:e + 1])
        raise


def chat_json(messages, mock=None, **kw):
    if MOCK:
        time.sleep(0.15)
        return mock() if callable(mock) else (mock or {})
    for _ in range(3):
        try:
            return parse_json(chat(messages, json_mode=True, **kw))
        except (ValueError, json.JSONDecodeError):
            continue
    raise RuntimeError("Model did not return valid JSON")


def stats():
    return [{"endpoint": repr(e), "calls": e.calls, "dead": e.dead, "cooling": max(0, round(e.cool_until - time.time()))}
            for e in ENDPOINTS]
