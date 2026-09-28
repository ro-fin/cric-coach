"""Provider-seam contract tests (US-G4): determinism, canned outputs, failures."""

import pytest
from cricai_coaching.llm import (
    FakeLLMProvider,
    LLMError,
    LLMProvider,
    LLMRequest,
    canonical_payload,
)


def _request(**payload: object) -> LLMRequest:
    return LLMRequest(prompt_key="daily_report", payload=dict(payload))


def test_fake_provider_satisfies_the_protocol() -> None:
    provider: LLMProvider = FakeLLMProvider()
    assert provider.complete(_request(n=3)).model == "fake-llm-seed0"


def test_canned_output_wins_for_its_prompt_key() -> None:
    provider = FakeLLMProvider(canned={"daily_report": "Watch the ball onto the bat."})
    assert provider.complete(_request(n=3)).text == "Watch the ball onto the bat."


def test_template_echo_is_deterministic_for_identical_inputs() -> None:
    first = FakeLLMProvider(seed=7).complete(_request(metric="control_pct", value=61.0))
    second = FakeLLMProvider(seed=7).complete(_request(metric="control_pct", value=61.0))
    assert first == second


def test_template_echo_varies_with_seed_payload_and_prompt_key() -> None:
    base = FakeLLMProvider(seed=0).complete(_request(n=3))
    assert FakeLLMProvider(seed=1).complete(_request(n=3)).text != base.text
    assert FakeLLMProvider(seed=0).complete(_request(n=4)).text != base.text
    other_key = LLMRequest(prompt_key="weekly_report", payload={"n": 3})
    assert FakeLLMProvider(seed=0).complete(other_key).text != base.text


def test_template_echo_carries_the_canonical_payload() -> None:
    response = FakeLLMProvider().complete(_request(b=2, a=1))
    assert response.text.endswith('{"a":1,"b":2}')


def test_injected_failure_raises_llm_error() -> None:
    provider = FakeLLMProvider(failures={"daily_report": "provider timeout"})
    with pytest.raises(LLMError, match="provider timeout"):
        provider.complete(_request(n=3))


def test_usage_reports_input_and_output_sizes() -> None:
    response = FakeLLMProvider().complete(_request(n=3))
    assert response.usage["input_chars"] == len('{"n":3}')
    assert response.usage["output_chars"] == len(response.text)


def test_canonical_payload_sorts_keys_and_stringifies_non_json_scalars() -> None:
    assert canonical_payload({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert canonical_payload({"when": object()}).startswith('{"when":"<object object')


def test_request_defaults_are_wording_layer_sized() -> None:
    request = _request()
    assert request.system == ""
    assert request.max_tokens == 1024
