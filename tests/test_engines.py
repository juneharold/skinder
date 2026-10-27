"""Provider transport tests use the real OpenAI client against a local mock transport."""
import json
from decimal import Decimal

import httpx
import pytest
from openai import AsyncOpenAI

from skinder.provider import LetsurMatcher, Pricing, ProviderError, build_request, parse_pricing


def pricing_entry():
    return {"id": "gpt-6.1-sol", "pricing": {"currency": "unit", "usage": [
        {"component": "input", "unit": "token_input", "amount": 0.000002, "currency": "unit", "when": None},
        {"component": "output", "unit": "token_output", "amount": 0.00001, "currency": "unit", "when": None}]}}


def completion(content, **changes):
    value = {"id": "test", "object": "chat.completion", "created": 0, "model": "gpt-6.1-sol",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(content)}}],
        "usage": {"prompt_tokens": 42, "completion_tokens": 13, "total_tokens": 55},
        "estimated_cost": {"amount": "0.00021400", "currency": "unit"}}
    value.update(changes)
    return value


def matcher(settings, handler):
    client = AsyncOpenAI(api_key="test", base_url="https://test.invalid/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return LetsurMatcher(settings, client=client)


async def test_provider_validates_output_and_preserves_exact_cost(settings):
    def handler(request):
        if request.url.path.endswith("models"):
            return httpx.Response(200, json={"object": "list", "data": [pricing_entry()]})
        return httpx.Response(200, json=completion({"recommendations": []}))
    provider = matcher(settings, handler)
    prices = await provider.pricing(settings.letsur_model)
    assert prices == Pricing(Decimal("0.000002"), Decimal("0.00001"))
    result = await provider.match(build_request({}, settings.letsur_model, settings), set())
    assert result.result.recommendations == []
    assert result.cost == Decimal("0.00021400")
    assert result.request_id == "test"
    assert (result.input_tokens, result.output_tokens) == (42, 13)
    await provider.close()


@pytest.mark.parametrize("payload", [
    {"recommendations": [{"skill_id": "invented", "task_fit": "yes", "taste_fit": "yes", "caveat": None}]},
    {"recommendations": [{"skill_id": "valid", "task_fit": "yes", "taste_fit": "yes", "caveat": None}] * 2},
    {"recommendations": [], "extra": "not allowed"},
    {"recommendations": [{"skill_id": "valid", "task_fit": 123, "taste_fit": "yes", "caveat": None}]},
])
async def test_invalid_output_still_retains_billed_usage(settings, payload):
    provider = matcher(settings, lambda request: httpx.Response(200, json=completion(payload)))
    result = await provider.match(build_request({}, settings.letsur_model, settings), {"valid"})
    assert result.error_code == "invalid_recommendations"
    assert result.result is None
    assert result.cost == Decimal("0.00021400")
    await provider.close()


@pytest.mark.parametrize("finish", ["length", "content_filter", "tool_calls"])
async def test_noncomplete_output_rejected(settings, finish):
    payload = completion({"recommendations": []})
    payload["choices"][0]["finish_reason"] = finish
    provider = matcher(settings, lambda request: httpx.Response(200, json=payload))
    assert (await provider.match(build_request({}, settings.letsur_model, settings), set())).error_code == "invalid_recommendations"
    await provider.close()


async def test_provider_does_not_retry_ambiguous_failure(settings):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"type": "gateway", "error_ref": "safe-ref"})
    provider = matcher(settings, handler)
    result = await provider.match(build_request({}, settings.letsur_model, settings), set())
    assert result.unknown and result.cost is None
    assert result.error_ref == "safe-ref"
    assert len(calls) == 1
    await provider.close()


@pytest.mark.parametrize("change", [None, {}, {"currency": "unit", "usage": []},
    {"currency": "usd", "usage": []}])
def test_missing_prices_fail_closed(change):
    with pytest.raises(ProviderError):
        parse_pricing({"pricing": change})


def test_reservation_counts_utf8_and_all_output_tokens(settings):
    request = build_request({"task": "한글" * 100}, settings.letsur_model, settings)
    price = Pricing(Decimal("0.000002"), Decimal("0.00001"))
    assert price.reserve(request) >= (Decimal(len(json.dumps(request, ensure_ascii=False).encode())) * price.input
        + settings.max_output_tokens * price.output)
