"""LLM providers behind one tiny interface.

Model ids look like ``provider:model`` e.g. ``ollama:qwen2.5:3b``, ``groq:llama-3.1-8b-instant``,
``openai:my-model`` (any OpenAI-compatible server) or ``mock:offline-demo``.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import httpx

from . import config
from .tokens import count_tokens


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    model: str
    estimated: bool = False   # True when token counts were estimated instead of reported by the provider


class LLMError(RuntimeError):
    pass


def _est_prompt_tokens(messages: list[dict]) -> int:
    return sum(count_tokens(m.get("content", "")) + 4 for m in messages)


# ---------------------------------------------------------------------------------------- Ollama
class OllamaLLM:
    provider = "ollama"

    def __init__(self, model: str):
        self.model = model

    def chat(self, messages, temperature=0.2, top_p=0.95, max_tokens=512, json_mode=False) -> LLMResult:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "top_p": top_p, "num_predict": max_tokens, "num_ctx": 4096},
            "keep_alive": "30m",
        }
        if json_mode:
            body["format"] = "json"
        t0 = time.time()
        try:
            r = httpx.post(f"{config.OLLAMA_HOST}/api/chat", json=body, timeout=300)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(
                f"Could not reach Ollama at {config.OLLAMA_HOST} ({exc}). Is it running, and have you pulled '{self.model}'?"
            ) from exc
        data = r.json()
        text = data.get("message", {}).get("content", "")
        return LLMResult(
            text=text,
            prompt_tokens=int(data.get("prompt_eval_count") or _est_prompt_tokens(messages)),
            completion_tokens=int(data.get("eval_count") or count_tokens(text)),
            latency_ms=int((time.time() - t0) * 1000),
            model=f"ollama:{self.model}",
            estimated="prompt_eval_count" not in data,
        )

    def stream(self, messages, temperature=0.2, top_p=0.95, max_tokens=512):
        """Yield ("token", text) as the model writes, then ("done", LLMResult)."""
        body = {
            "model": self.model, "messages": messages, "stream": True, "keep_alive": "30m",
            "options": {"temperature": temperature, "top_p": top_p, "num_predict": max_tokens, "num_ctx": 4096},
        }
        t0, text, last = time.time(), "", {}
        try:
            with httpx.stream("POST", f"{config.OLLAMA_HOST}/api/chat", json=body, timeout=httpx.Timeout(300, connect=10)) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    piece = data.get("message", {}).get("content", "")
                    if piece:
                        text += piece
                        yield ("token", piece)
                    if data.get("done"):
                        last = data
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach Ollama at {config.OLLAMA_HOST} ({exc}). Is it running, and have you pulled '{self.model}'?") from exc
        yield ("done", LLMResult(
            text=text, prompt_tokens=int(last.get("prompt_eval_count") or _est_prompt_tokens(messages)),
            completion_tokens=int(last.get("eval_count") or count_tokens(text)), latency_ms=int((time.time() - t0) * 1000),
            model=f"ollama:{self.model}", estimated="prompt_eval_count" not in last))


# ------------------------------------------------------------------------- OpenAI-compatible
class OpenAICompatLLM:
    def __init__(self, provider: str, model: str):
        self.provider = provider
        self.model = model
        if provider == "groq":
            self.base, self.key = "https://api.groq.com/openai/v1", config.GROQ_API_KEY
        else:
            self.base, self.key = config.OPENAI_BASE_URL.rstrip("/"), config.OPENAI_API_KEY

    def chat(self, messages, temperature=0.2, top_p=0.95, max_tokens=512, json_mode=False) -> LLMResult:
        if not self.base:
            raise LLMError("OPENAI_BASE_URL is not set.")
        body = {"model": self.model, "messages": messages, "temperature": temperature, "top_p": top_p, "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        t0 = time.time()
        try:
            r = httpx.post(f"{self.base}/chat/completions", json=body, headers=headers, timeout=120)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"{self.provider} returned {exc.response.status_code}: {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach {self.provider}: {exc}") from exc
        data = r.json()
        text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage") or {}
        return LLMResult(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens") or _est_prompt_tokens(messages)),
            completion_tokens=int(usage.get("completion_tokens") or count_tokens(text)),
            latency_ms=int((time.time() - t0) * 1000),
            model=f"{self.provider}:{self.model}",
            estimated=not usage,
        )


def iter_chat(llm, messages, **kw):
    """Uniform streaming interface: ("token", text) events then ("done", LLMResult). Falls back to one chunk."""
    if hasattr(llm, "stream"):
        yield from llm.stream(messages, **kw)
        return
    res = llm.chat(messages, **kw)
    yield ("token", res.text)
    yield ("done", res)


# -------------------------------------------------------------------------------------- registry
def get_llm(model_id: str):
    provider, _, model = (model_id or "mock:offline-demo").partition(":")
    if provider == "mock":
        from .simulator import SimulatedLLM

        return SimulatedLLM()
    if provider == "ollama":
        return OllamaLLM(model)
    if provider in ("groq", "openai"):
        return OpenAICompatLLM(provider, model)
    raise LLMError(f"Unknown provider '{provider}'")


_groq_cache: dict = {"t": 0.0, "ids": None}


def _groq_models() -> list[str]:
    """Ask Groq which chat models exist right now (their lineup changes), fall back to a static list."""
    if _groq_cache["ids"] and time.time() - _groq_cache["t"] < 300:
        return _groq_cache["ids"]
    ids = ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"]
    try:
        r = httpx.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"}, timeout=4)
        r.raise_for_status()
        live = sorted(m["id"] for m in r.json().get("data", []) if not re.search(r"whisper|guard|tts|playai|orpheus|embed|safeguard", m["id"], re.I))
        if live:
            ids = live
    except Exception:  # noqa: BLE001
        pass
    _groq_cache.update(t=time.time(), ids=ids)
    return ids


_models_cache: dict = {"t": 0.0, "value": None}


def list_models(force: bool = False) -> dict:
    """Discover what is available. Never raises: unreachable providers are reported, not fatal."""
    if not force and _models_cache["value"] and time.time() - _models_cache["t"] < 8:
        return _models_cache["value"]
    providers = [
        {
            "id": "mock",
            "label": "Offline simulator",
            "available": True,
            "note": "Rule-based stand-in so the app works with no model installed. Use a real model for real results.",
            "models": [{"id": "mock:offline-demo", "name": "offline-demo"}],
        }
    ]
    ollama = {"id": "ollama", "label": "Ollama (local)", "available": False, "models": [], "note": ""}
    try:
        r = httpx.get(f"{config.OLLAMA_HOST}/api/tags", timeout=1.5)
        r.raise_for_status()
        names = sorted(m["name"] for m in r.json().get("models", []) if "embed" not in m["name"])
        ollama["available"] = True
        ollama["models"] = [{"id": f"ollama:{n}", "name": n} for n in names]
        if not names:
            ollama["note"] = "Ollama is running but has no chat models. Try: ollama pull qwen2.5:3b"
    except httpx.HTTPError:
        ollama["note"] = f"Ollama not detected at {config.OLLAMA_HOST}. Install it, then: ollama pull qwen2.5:3b"
    providers.append(ollama)
    if config.GROQ_API_KEY:
        providers.append(
            {
                "id": "groq",
                "label": "Groq (hosted open models)",
                "available": True,
                "note": "",
                "models": [{"id": f"groq:{m}", "name": m} for m in _groq_models()],
            }
        )
    if config.OPENAI_BASE_URL:
        providers.append(
            {
                "id": "openai",
                "label": "OpenAI-compatible",
                "available": True,
                "note": config.OPENAI_BASE_URL,
                "models": [{"id": f"openai:{m}", "name": m} for m in (config.__dict__.get("OPENAI_MODEL", "") or "default",)],
            }
        )
    _models_cache.update(t=time.time(), value={"providers": providers})
    return _models_cache["value"]
