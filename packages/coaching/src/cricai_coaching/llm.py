"""LLM provider seam (US-G4, design §3): the LLM is a wording layer only.

Providers see structured findings JSON, history summaries, rule texts and the
tone guide — never raw video, media bytes, or object-store keys; callers
sanitize payloads through the egress allow-list before building a request.
The deterministic :class:`FakeLLMProvider` is the only provider unit tests
use; the Anthropic adapter (story g3) lives behind a lazy import in
``cricai_coaching.llm_anthropic``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(Exception):
    """Provider failure (network, refusal, malformed output).

    Callers fall back to the rule-based writer on any ``LLMError`` — a report
    never depends on LLM availability (US-G4 fallback path).
    """


@dataclass(frozen=True)
class LLMRequest:
    """One wording call: a stable prompt key plus the structured payload.

    ``prompt_key`` names the template (it keys audit rows and canned fakes);
    ``payload`` is egress-sanitized structured data, never free-form media.
    """

    prompt_key: str
    payload: dict[str, Any]
    system: str = ""
    max_tokens: int = 1024


@dataclass(frozen=True)
class LLMResponse:
    """Provider output: the candidate wording plus audit metadata."""

    text: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)


class LLMProvider(Protocol):
    """Anything that can turn an :class:`LLMRequest` into a response."""

    def complete(self, request: LLMRequest) -> LLMResponse: ...


def canonical_payload(payload: dict[str, Any]) -> str:
    """Canonical JSON used in prompts and audit rows: sorted keys, minified."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class FakeLLMProvider:
    """Deterministic provider for tests (the FakePoseProvider pattern).

    ``canned`` injects exact outputs by prompt key; ``failures`` injects
    :class:`LLMError` by prompt key (exercising the fallback path). Anything
    else gets a template echo whose text is a pure function of
    ``(seed, prompt_key, payload)`` — identical inputs always word identically.
    """

    canned: dict[str, str] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    seed: int = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        if request.prompt_key in self.failures:
            raise LLMError(self.failures[request.prompt_key])
        body = canonical_payload(request.payload)
        if request.prompt_key in self.canned:
            text = self.canned[request.prompt_key]
        else:
            stamp = hashlib.sha256(f"{self.seed}:{request.prompt_key}:{body}".encode())
            text = f"[fake:{request.prompt_key}:{stamp.hexdigest()[:12]}] {body}"
        return LLMResponse(
            text=text,
            model=f"fake-llm-seed{self.seed}",
            usage={"input_chars": len(body), "output_chars": len(text)},
        )
