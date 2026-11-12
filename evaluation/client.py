"""Clients the tests use to talk to the chatbot.

* ApiClient: real HTTP calls with `requests`. This is what the API tests use,
  so they exercise the full stack: HTTP, validation, serialisation, pipeline.
* LocalClient: calls the pipeline in-process. Used for the guardrail ablation
  (guardrails switched off), which you would never expose through the API.

Both return the same JSON-shaped dict, so the check functions accept either.
"""

from __future__ import annotations

import requests


class ApiClient:
    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def health(self) -> dict:
        response = self.session.get(f"{self.base_url}/health", timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def manuals(self) -> list[dict]:
        response = self.session.get(f"{self.base_url}/v1/manuals", timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def chat_raw(self, payload: dict) -> requests.Response:
        return self.session.post(f"{self.base_url}/v1/chat", json=payload, timeout=self.timeout)

    def chat(self, question: str, top_k: int | None = None) -> dict:
        payload = {"question": question} | ({"top_k": top_k} if top_k else {})
        response = self.chat_raw(payload)
        response.raise_for_status()
        return response.json()

    def evaluate(self, question: str, answer: str, contexts: list[str], use_llm_judge: bool = True) -> dict:
        response = self.session.post(
            f"{self.base_url}/v1/evaluate",
            json={"question": question, "answer": answer, "contexts": contexts, "use_llm_judge": use_llm_judge},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()


class LocalClient:
    def __init__(self, pipeline) -> None:
        self.pipeline = pipeline

    def chat(self, question: str, top_k: int | None = None) -> dict:
        return dict(self.pipeline.ask(question, top_k=top_k).__dict__)
