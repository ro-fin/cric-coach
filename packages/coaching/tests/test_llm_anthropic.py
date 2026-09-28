"""AnthropicProvider adapter tests (US-G4): lazy import, request mapping,
response text extraction and error taxonomy — all against a stub ``anthropic``
module injected into ``sys.modules`` (the yolo_detect stub pattern; never a
real network call)."""

import sys
import types
from typing import Any

import pytest
from cricai_coaching.llm import LLMError, LLMProvider, LLMRequest
from cricai_coaching.llm_anthropic import DEFAULT_MODEL, AnthropicProvider


class _FakeBlock:
    def __init__(self, type_: str, text: str | None = None) -> None:
        self.type = type_
        if text is not None:
            self.text = text


class _FakeUsage:
    def __init__(self, input_tokens: int = 11, output_tokens: int = 5) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeMessage:
    def __init__(
        self,
        blocks: list[_FakeBlock],
        *,
        model: str | None = "claude-fake-1",
        usage: _FakeUsage | None = None,
        stop_reason: str = "end_turn",
    ) -> None:
        self.content = blocks
        if model is not None:
            self.model = model
        if usage is not None:
            self.usage = usage
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, message: _FakeMessage | None, error: Exception | None) -> None:
        self._message = message
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeMessage:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        assert self._message is not None
        return self._message


class _FakeClient:
    def __init__(
        self,
        init_kwargs: dict[str, Any],
        message: _FakeMessage | None,
        error: Exception | None,
    ) -> None:
        self.init_kwargs = init_kwargs
        self.messages = _FakeMessages(message, error)


def _install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    message: _FakeMessage | None = None,
    api_error_message: str | None = None,
) -> tuple[Any, list[_FakeClient]]:
    """Inject a stub ``anthropic`` module mirroring the sdk surface the
    adapter uses: ``Anthropic(**kwargs).messages.create(...)`` + ``APIError``."""
    module = types.ModuleType("anthropic")
    created: list[_FakeClient] = []

    class APIError(Exception):
        pass

    error = APIError(api_error_message) if api_error_message is not None else None

    def factory(**kwargs: Any) -> _FakeClient:
        client = _FakeClient(kwargs, message, error)
        created.append(client)
        return client

    setattr(module, "APIError", APIError)  # noqa: B010 - ModuleType attrs via setattr for mypy
    setattr(module, "Anthropic", factory)  # noqa: B010
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return module, created


def _request(**overrides: Any) -> LLMRequest:
    defaults: dict[str, Any] = {
        "prompt_key": "report_wording",
        "payload": {"b": 2, "a": 1},
        "system": "word the template",
        "max_tokens": 512,
    }
    defaults.update(overrides)
    return LLMRequest(**defaults)


def test_provider_satisfies_the_llm_provider_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sdk(monkeypatch, message=_FakeMessage([_FakeBlock("text", "worded")]))
    provider: LLMProvider = AnthropicProvider()
    assert provider.complete(_request()).text == "worded"


def test_module_imports_and_constructs_without_the_sdk_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)  # forces ImportError on import
    provider = AnthropicProvider()  # construction never touches the sdk
    with pytest.raises(LLMError, match="anthropic sdk is not installed"):
        provider.complete(_request())


def test_request_maps_model_tokens_system_and_canonical_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, created = _install_fake_sdk(monkeypatch, message=_FakeMessage([_FakeBlock("text", "ok")]))
    provider = AnthropicProvider(model="claude-test-9")
    provider.complete(_request())
    (call,) = created[0].messages.calls
    assert call["model"] == "claude-test-9"
    assert call["max_tokens"] == 512
    assert call["system"] == "word the template"
    assert call["messages"] == [{"role": "user", "content": 'report_wording\n{"a":1,"b":2}'}]


def test_empty_system_is_omitted_from_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _, created = _install_fake_sdk(monkeypatch, message=_FakeMessage([_FakeBlock("text", "ok")]))
    AnthropicProvider().complete(_request(system=""))
    (call,) = created[0].messages.calls
    assert "system" not in call


def test_client_kwargs_default_to_timeout_and_retries_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, created = _install_fake_sdk(monkeypatch, message=_FakeMessage([_FakeBlock("text", "ok")]))
    AnthropicProvider().complete(_request())
    assert created[0].init_kwargs == {"timeout": 30.0, "max_retries": 2}


def test_explicit_api_key_and_base_url_reach_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, created = _install_fake_sdk(monkeypatch, message=_FakeMessage([_FakeBlock("text", "ok")]))
    provider = AnthropicProvider(
        api_key="sk-test", base_url="http://lan-proxy:8083", timeout_s=5.0, max_retries=0
    )
    provider.complete(_request())
    assert created[0].init_kwargs == {
        "timeout": 5.0,
        "max_retries": 0,
        "api_key": "sk-test",
        "base_url": "http://lan-proxy:8083",
    }


def test_client_is_cached_across_completions(monkeypatch: pytest.MonkeyPatch) -> None:
    _, created = _install_fake_sdk(monkeypatch, message=_FakeMessage([_FakeBlock("text", "ok")]))
    provider = AnthropicProvider()
    provider.complete(_request())
    provider.complete(_request())
    assert len(created) == 1
    assert len(created[0].messages.calls) == 2


def test_text_blocks_are_joined_and_non_text_blocks_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _FakeMessage(
        [_FakeBlock("thinking"), _FakeBlock("text", "part one "), _FakeBlock("text", "part two")],
        usage=_FakeUsage(input_tokens=21, output_tokens=8),
    )
    _install_fake_sdk(monkeypatch, message=message)
    response = AnthropicProvider().complete(_request())
    assert response.text == "part one part two"
    assert response.model == "claude-fake-1"
    assert response.usage == {"input_tokens": 21, "output_tokens": 8}


def test_missing_usage_yields_empty_usage_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sdk(monkeypatch, message=_FakeMessage([_FakeBlock("text", "ok")]))
    assert AnthropicProvider().complete(_request()).usage == {}


def test_missing_model_falls_back_to_the_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sdk(monkeypatch, message=_FakeMessage([_FakeBlock("text", "ok")], model=None))
    assert AnthropicProvider().complete(_request()).model == DEFAULT_MODEL


def test_completion_without_text_blocks_raises_llm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sdk(monkeypatch, message=_FakeMessage([_FakeBlock("thinking")]))
    with pytest.raises(LLMError, match="no text blocks"):
        AnthropicProvider().complete(_request())


def test_refusal_stop_reason_raises_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    message = _FakeMessage([_FakeBlock("text", "partial")], stop_reason="refusal")
    _install_fake_sdk(monkeypatch, message=message)
    with pytest.raises(LLMError, match="refused"):
        AnthropicProvider().complete(_request())


def test_api_errors_map_to_llm_error_with_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    module, _ = _install_fake_sdk(monkeypatch, api_error_message="429 rate limited")
    with pytest.raises(LLMError, match="anthropic api error: 429 rate limited") as excinfo:
        AnthropicProvider().complete(_request())
    assert isinstance(excinfo.value.__cause__, module.APIError)
