"""Anthropic Messages API adapter behind the ``LLMProvider`` seam (US-G4).

The ``anthropic`` sdk is an optional dependency this workspace deliberately
does not install (design §3: the LLM is a wording layer only, and no new
third-party dependency ships with Phase 5). The import therefore happens
lazily inside :meth:`AnthropicProvider.complete` — importing this module
always succeeds; constructing the provider always succeeds; only an actual
completion call needs the sdk, and a missing sdk surfaces as
:class:`~cricai_coaching.llm.LLMError` so callers fall back to the rule-based
writer like any other provider failure.

The adapter tracks the Messages API surface the tests stub (the
``FakeDetectionProvider``/``yolo_detect`` pattern — a fake ``anthropic``
module injected into ``sys.modules`` mirrors the exact names below, so sdk
drift shows up as a stub/adapter mismatch instead of only in production):

* ``anthropic.Anthropic(timeout=..., max_retries=..., api_key=..., base_url=...)``
* ``client.messages.create(model=..., max_tokens=..., system=..., messages=[...])``
* the returned message's ``content`` blocks (``block.type == "text"`` carry
  ``block.text``), ``model``, ``usage.input_tokens``/``output_tokens`` and
  ``stop_reason`` (``"refusal"`` means no usable wording)
* ``anthropic.APIError`` — the sdk's base error for status + connection
  failures; mapped to :class:`LLMError`.
"""

from __future__ import annotations

import importlib
from typing import Any

from cricai_coaching.llm import LLMError, LLMRequest, LLMResponse, canonical_payload

_INSTALL_HINT = (
    "the anthropic sdk is not installed; install it to use AnthropicProvider "
    "or inject FakeLLMProvider instead"
)

#: Default wording model. Config, not a code path — override per deployment.
DEFAULT_MODEL = "claude-opus-4-8"


def _import_anthropic() -> Any:
    """Lazy sdk import: module import never requires the package (US-G4)."""
    try:
        return importlib.import_module("anthropic")
    except ImportError as exc:
        raise LLMError(_INSTALL_HINT) from exc


def _prompt_text(request: LLMRequest) -> str:
    """Deterministic user-turn text: the prompt key + canonical payload JSON."""
    return f"{request.prompt_key}\n{canonical_payload(request.payload)}"


def _to_response(message: Any, *, fallback_model: str) -> LLMResponse:
    """Extract wording text + audit metadata from a Messages API response.

    A ``refusal`` stop reason or a completion without text blocks yields
    :class:`LLMError` — the caller's rule-based fallback handles it.
    """
    if getattr(message, "stop_reason", None) == "refusal":
        raise LLMError("anthropic refused the wording request")
    parts = [str(block.text) for block in message.content if getattr(block, "type", "") == "text"]
    if not parts:
        raise LLMError("anthropic returned no text blocks")
    usage_info = getattr(message, "usage", None)
    usage: dict[str, int] = {}
    if usage_info is not None:
        usage = {
            "input_tokens": int(getattr(usage_info, "input_tokens", 0)),
            "output_tokens": int(getattr(usage_info, "output_tokens", 0)),
        }
    return LLMResponse(
        text="".join(parts),
        model=str(getattr(message, "model", fallback_model)),
        usage=usage,
    )


class AnthropicProvider:
    """Real-model edge of the ``LLMProvider`` protocol (US-G4).

    One synchronous ``messages.create`` per wording call; the client is built
    on first use and cached. Every sdk failure — missing package, API error,
    refusal, empty completion — becomes :class:`LLMError`, keeping the
    deterministic-fallback contract identical to :class:`FakeLLMProvider`'s.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._base_url = base_url
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._client: Any | None = None

    def _get_client(self, sdk: Any) -> Any:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "timeout": self._timeout_s,
                "max_retries": self._max_retries,
            }
            if self._api_key is not None:
                kwargs["api_key"] = self._api_key
            if self._base_url is not None:
                kwargs["base_url"] = self._base_url
            self._client = sdk.Anthropic(**kwargs)
        return self._client

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Send one wording request; raise :class:`LLMError` on any failure."""
        sdk = _import_anthropic()
        client = self._get_client(sdk)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "messages": [{"role": "user", "content": _prompt_text(request)}],
        }
        if request.system:
            kwargs["system"] = request.system
        try:
            message = client.messages.create(**kwargs)
        except sdk.APIError as exc:
            raise LLMError(f"anthropic api error: {exc}") from exc
        return _to_response(message, fallback_model=self.model)
