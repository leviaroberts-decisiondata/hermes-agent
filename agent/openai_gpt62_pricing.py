"""Request-level official GPT-6.2 USD estimates; never subscription charges.

Source: https://developers.openai.com/api/docs/pricing and
https://developers.openai.com/api/docs/guides/prompt-caching
Verified 2026-11-18; effective 2026-11-17. Writes replace ordinary input.
"""
from datetime import datetime, timezone
from decimal import Decimal
import re

PRICING_VERSION = "openai-standard:gpt-6.2:2026-11-17"
RATES = {
    "gpt-6.2-astra": tuple(map(Decimal, ("8", ".4", "10", "20", "40"))),
    "gpt-6.2-sol": tuple(map(Decimal, ("2", ".1", "2.5", "5", "10"))),
}


def model_key(model):
    match = re.fullmatch(r"(gpt-6\.2-(?:astra|sol))(?:-\d{4}-\d{2}-\d{2})?", model or "")
    return match.group(1) if match else None


def request_cost(model, usage, *, requested_at=None, service_tier="standard",
                 background_completion_window=None, regional_processing=False):
    """Canonical buckets are disjoint; output already includes reasoning.

    Unknown TTL, tier, date or inconsistent counts return None. Ultrafast
    promotion ends at midnight March 1, 2027 Pacific (08:00 UTC).
    """
    key = model_key(model)
    if not key:
        return None
    counts = (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
              usage.cache_write_tokens, usage.reasoning_tokens)
    if any(type(n) is not int or n < 0 for n in counts) or usage.reasoning_tokens > usage.output_tokens:
        return None
    total = usage.prompt_tokens
    if usage.reported_input_tokens is not None and total != usage.reported_input_tokens:
        return None
    write30, write12 = usage.cache_write_30m_tokens, usage.cache_write_12h_tokens
    if write30 is None and write12 is None:
        if usage.cache_write_tokens:
            return None
        write30 = write12 = 0
    else:
        write30, write12 = write30 or 0, write12 or 0
        if any(type(n) is not int or n < 0 for n in (write30, write12)) or write30 + write12 != usage.cache_write_tokens:
            return None
    at = None
    if requested_at is not None:
        try:
            at = requested_at if isinstance(requested_at, datetime) else datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
            if at.tzinfo is None or at < datetime(2026, 11, 17, tzinfo=timezone.utc):
                return None
        except (ValueError, TypeError, AttributeError):
            return None
    multipliers = {"standard": Decimal(1), "default": Decimal(1), "batch": Decimal(".5"),
                   "fast": Decimal(2), "priority": Decimal(2)}
    if service_tier == "ultrafast":
        if at is None:
            return None
        multiplier = Decimal(3 if at < datetime(2027, 3, 1, 8, tzinfo=timezone.utc) else 6)
    else:
        multiplier = multipliers.get(service_tier)
        if multiplier is None:
            return None
    if background_completion_window:
        if service_tier not in ("standard", "default") or background_completion_window not in ("2h", "8h"):
            return None
        multiplier = {"2h": Decimal(".7"), "8h": Decimal(".6")}[background_completion_window]
    if regional_processing:
        multiplier *= Decimal("1.1")
    ordinary, read, write_short, write_long, output = RATES[key]
    input_multiplier = Decimal(2 if total > 272000 else 1)
    output_multiplier = Decimal("1.5") if total > 272000 else Decimal(1)
    return multiplier * (input_multiplier * (
        usage.input_tokens * ordinary + usage.cache_read_tokens * read
        + write30 * write_short + write12 * write_long)
        + usage.output_tokens * output * output_multiplier) / Decimal(1000000)
