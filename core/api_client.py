"""Simplified API client for convomemory.

Single API key, no pool. Compatible interface with statemem APIClient.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request

logger = logging.getLogger(__name__)


def _load_api_key() -> str:
    import os
    for name in ["OPENAI_API_KEY", "GPT_API_KEY_1"]:
        v = os.environ.get(name, "")
        if v:
            return v
    raise ValueError("No API key found. Set OPENAI_API_KEY in .env")


def _embed_texts(
    texts: list[str],
    *,
    api_key: str,
    base_url: str,
    model: str = "text-embedding-3-small",
) -> list[list[float]]:
    if not texts:
        return []
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {"model": model, "input": texts}
    data = json.dumps(payload).encode()
    url = base_url.rstrip("/") + "/embeddings"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode())
                items = sorted(body["data"], key=lambda x: x["index"])
                return [item["embedding"] for item in items]
        except Exception as exc:
            if attempt == 4:
                logger.warning("embed_texts failed: %s", exc)
                return [[] for _ in texts]
            if "429" in str(exc):
                wait = 60 * (attempt + 1)
                logger.warning("embed 429, retrying in %ds", wait)
                time.sleep(wait)
            else:
                time.sleep(2 ** attempt)
    return [[] for _ in texts]


class APIClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "gpt-4.1-mini",
        emb_model: str = "text-embedding-3-small",
    ) -> None:
        import os
        self._key = api_key or _load_api_key()
        resolved_url = base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self._base_url = resolved_url.rstrip("/")
        self._chat_url = self._base_url + "/chat/completions"
        self.model = model
        self.emb_model = emb_model

    def _call(self, messages: list[dict], max_tokens: int, temperature: float = 0.0) -> str:
        import requests
        r = requests.post(
            self._chat_url,
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
            json={"model": self.model, "messages": messages,
                  "max_tokens": max_tokens, "temperature": temperature},
            timeout=(10, 120),
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def _retry(self, fn, *args, **kwargs):
        for attempt in range(5):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                if attempt == 4:
                    return f"ERROR: {e}"
                if "429" in str(e):
                    wait = 60 * (attempt + 1)
                    logger.warning("429, retrying in %ds", wait)
                    time.sleep(wait)
                else:
                    time.sleep(1.5 * (attempt + 1))
        return "ERROR: exhausted"

    def gpt(self, system: str, user: str, max_tokens: int = 64, temperature: float = 0.0) -> str:
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
        return self._retry(self._call, msgs, max_tokens, temperature)

    def gpt_with_history(self, history: list[dict], user: str, max_tokens: int = 256) -> str:
        msgs = history + [{"role": "user", "content": user}]
        return self._retry(self._call, msgs, max_tokens)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return _embed_texts(texts, api_key=self._key,
                            base_url=self._base_url, model=self.emb_model)

    def embed_single(self, text: str) -> list[float]:
        result = self.embed_batch([text])
        return result[0] if result else []
