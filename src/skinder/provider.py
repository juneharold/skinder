"""Single-attempt LETSUR requests with strict output and price validation."""
import json
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from time import monotonic

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import ValidationError

from .matcher import MATCH_INSTRUCTIONS
from .models import MatchResult
from .settings import Settings


class ProviderError(Exception):
    def __init__(self, code: str, *, unknown: bool = False, error_ref: str | None = None):
        super().__init__(code)
        self.code, self.unknown, self.error_ref = code, unknown, error_ref


@dataclass(frozen=True)
class Pricing:
    input: Decimal
    output: Decimal
    currency: str = "unit"

    def as_dict(self):
        return {"input": str(self.input), "output": str(self.output), "currency": self.currency}

    def reserve(self, request: dict) -> Decimal:
        # Each UTF-8 byte is an upper bound on one tokenizer token. Include generous
        # framing overhead for messages and the JSON schema sent to the gateway.
        tokens = len(json.dumps(request, ensure_ascii=False).encode()) + 1024
        return (tokens * self.input + request["max_tokens"] * self.output).quantize(
            Decimal("0.00000001"), rounding=ROUND_CEILING)


@dataclass
class ProviderOutcome:
    result: MatchResult | None = None
    error_code: str | None = None
    unknown: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: Decimal | None = None
    currency: str | None = None
    request_id: str | None = None
    error_ref: str | None = None


def amount(value) -> Decimal:
    try:
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed < 0:
            raise ValueError
        return parsed
    except (InvalidOperation, ValueError, TypeError):
        raise ProviderError("pricing_unavailable") from None


def _request_payload(data: dict, model: str, settings: Settings) -> dict:
    return {
        "model": model,
        "messages": [{"role": "system", "content": MATCH_INSTRUCTIONS},
                     {"role": "user", "content": json.dumps(data, ensure_ascii=False)}],
        "stream": False, "max_tokens": settings.max_output_tokens,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "skill_recommendations", "strict": True,
            "schema": MatchResult.model_json_schema()}},
    }


def request_size(data: dict, model: str, settings: Settings) -> int:
    return len(json.dumps(_request_payload(data, model, settings), ensure_ascii=False).encode())


def build_request(data: dict, model: str, settings: Settings) -> dict:
    request = _request_payload(data, model, settings)
    if len(json.dumps(request, ensure_ascii=False).encode()) > settings.max_prompt_bytes:
        raise ProviderError("prompt_too_large")
    return request


class LetsurMatcher:
    def __init__(self, settings: Settings, *, client=None):
        self.settings = settings
        self.client = client or AsyncOpenAI(api_key=settings.letsur_api_key.get_secret_value(),
            base_url=settings.letsur_base_url, timeout=settings.letsur_timeout_seconds, max_retries=0)
        self._prices: dict[str, tuple[float, Pricing]] = {}

    async def pricing(self, model: str) -> Pricing:
        cached = self._prices.get(model)
        if cached and monotonic() - cached[0] < 300:
            return cached[1]
        try:
            response = await self.client.models.list()
            entry = next(item.model_dump() for item in response.data if item.id == model)
            price = parse_pricing(entry)
        except ProviderError:
            raise
        except (APIError, StopIteration, AttributeError, TypeError, ValueError):
            raise ProviderError("pricing_unavailable") from None
        self._prices[model] = (monotonic(), price)
        return price

    async def match(self, request: dict, eligible: set[str]) -> ProviderOutcome:
        try:
            response = await self.client.chat.completions.create(**request)
        except (APIConnectionError, APITimeoutError, TimeoutError):
            return ProviderOutcome(error_code="provider_outcome_unknown", unknown=True)
        except APIStatusError as exc:
            body = exc.body if isinstance(exc.body, dict) else {}
            return ProviderOutcome(error_code="provider_outcome_unknown" if exc.status_code >= 500 else "provider_rejected",
                unknown=exc.status_code >= 500, error_ref=body.get("error_ref"))
        outcome = ProviderOutcome(request_id=getattr(response, "_request_id", None) or response.id)
        usage = response.usage
        if usage:
            outcome.input_tokens, outcome.output_tokens = usage.prompt_tokens, usage.completion_tokens
        cost = (response.model_extra or {}).get("estimated_cost")
        if isinstance(cost, dict) and cost.get("currency") == "unit":
            try:
                outcome.cost, outcome.currency = amount(cost.get("amount")), "unit"
            except ProviderError:
                pass
        try:
            if len(response.choices) != 1:
                raise ValueError
            choice = response.choices[0]
            if choice.finish_reason != "stop" or choice.message.tool_calls or choice.message.refusal:
                raise ValueError
            result = MatchResult.model_validate_json(choice.message.content or "", strict=True)
            ids = [item.skill_id for item in result.recommendations]
            if len(ids) != len(set(ids)) or not set(ids) <= eligible:
                raise ValueError
            outcome.result = result
        except (ValueError, ValidationError):
            outcome.error_code = "invalid_recommendations"
        return outcome

    async def close(self):
        await self.client.close()


def parse_pricing(entry: dict) -> Pricing:
    pricing = entry.get("pricing")
    if not isinstance(pricing, dict):
        raise ProviderError("pricing_unavailable")
    components = pricing.get("usage")
    if pricing.get("currency") != "unit" or not isinstance(components, list):
        raise ProviderError("pricing_unavailable")
    values = []
    for name in ("input", "output"):
        matches = [item for item in components if item.get("component") == name]
        if len(matches) != 1:
            raise ProviderError("pricing_unavailable")
        component = matches[0]
        if (component.get("currency") != "unit" or component.get("unit") != "token_" + name
                or component.get("when") is not None):
            raise ProviderError("pricing_unavailable")
        values.append(amount(component.get("amount")))
    return Pricing(*values)
