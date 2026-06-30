"""OllamaProvider — calls a local ollama server for structured extraction.

Requires ollama to be running at CAIRN_OLLAMA_HOST (default 127.0.0.1:11434).
Uses ollama's ``format=<schema>`` JSON mode so the model is grammar-constrained
to produce valid JSON matching the supplied schema.

Connection check: ``admin llm-ping`` calls ``ping()`` before any extraction.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import LLMProvider, ValidationError

_DEFAULT_HOST = "http://127.0.0.1:11434"
_DEFAULT_MODEL = "qwen2.5:32b-instruct-q4_K_M"


class OllamaProvider(LLMProvider):
    def __init__(self, model: str = _DEFAULT_MODEL) -> None:
        self._model = model
        self._host = os.environ.get("CAIRN_OLLAMA_HOST", _DEFAULT_HOST).rstrip("/")

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def model(self) -> str:
        return self._model

    def complete_structured(
        self,
        prompt: str,
        *,
        schema: dict,
        system: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> dict:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = json.dumps({
            "model": self._model,
            "stream": False,
            "format": schema,
            "messages": messages,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }).encode()

        req = urllib.request.Request(
            f"{self._host}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                body = json.loads(resp.read())
        except urllib.error.URLError as exc:
            raise ValidationError(f"ollama unreachable: {exc}") from exc

        raw = body.get("message", {}).get("content", "")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"ollama returned non-JSON: {exc}: {raw[:200]}") from exc

        if not isinstance(result, dict):
            raise ValidationError(f"ollama returned non-object JSON: {type(result)}")

        return result

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def ping(self) -> dict:
        """Check connectivity and whether the configured model is available.

        Returns a dict with keys 'ok' (bool), 'model', 'available_models', 'error'.
        """
        try:
            req = urllib.request.Request(
                f"{self._host}/api/tags",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            return {"ok": False, "model": self._model, "available_models": [], "error": str(exc)}

        available = [m["name"] for m in data.get("models", [])]
        model_ok = any(m == self._model or m.startswith(self._model.split(":")[0]) for m in available)
        return {
            "ok": model_ok,
            "model": self._model,
            "available_models": available,
            "error": None if model_ok else f"model '{self._model}' not found; run: ollama pull {self._model}",
        }
