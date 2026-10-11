"""Multi-provider LLM router (WORKPLAN C6). Owner: Agent A.

Roles ("strategist", "writer", "editor", "vision") map to ordered
"provider:model" lists in ``settings.llm_roles`` (providers: ``groq``,
``gemini``). Groq rate limits are per model and Gemini has its own quota, so
the router spreads calls across models: each model gets its own token budget
(sliding 60 s token window in which each request fades out linearly, like Groq's
continuous refill, + requests/min + tokens/day + requests/day), a model that
is out of budget or returns 429 is skipped in favour of the next one in the role
list, and the router only waits (bounded) when every model for the role is busy.

Callers never pick a model directly; they ask for a role.

Budget accounting: before a call the router reserves ``prompt estimate +
max_tokens`` on the model; afterwards it corrects the reservation to the
provider-reported usage. Groq rejects a request whose prompt + max_tokens exceeds
the model's TPM (413), so ``max_tokens`` is shrunk to fit when needed.

Extras beyond the contract (used by ``brand_plan``):
- ``prompt_capacity(role, max_tokens)``: prompt tokens the first usable model of
  the role accepts with that completion budget (big on Gemini, ~4-6K on Groq).
- ``role_models(role)``: the usable (key present) models of a role, in order.
- ``track_calls()``: context manager collecting a ``CallRecord`` per LLM call.
- ``estimate_tokens(text)``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import json
import logging
import re
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TypeVar

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, ValidationError

from app.core.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

PROVIDERS = ("groq", "gemini")

# Free-tier defaults (verified 2026-10-10: Groq /openai/v1/models + console.groq.com/docs/rate-limits;
# Gemini limits are not published per model, these follow AI Studio's usual free tier and can be
# overridden via settings.llm_limits).
_GROQ_FREE = {"tpm": 8_000, "rpm": 30, "tpd": 200_000, "rpd": 1_000, "context": 131_072, "max_output": 65_536}
DEFAULT_LIMITS: dict[str, dict[str, int]] = {
    "groq:openai/gpt-oss-120b": dict(_GROQ_FREE),
    "groq:openai/gpt-oss-20b": dict(_GROQ_FREE),
    # qwen also has an output-tokens-per-minute cap (429 "Request too large ... (OTPM): Limit 1000").
    "groq:qwen/qwen3.8-27b": {**_GROQ_FREE, "max_output": 16_384, "otpm": 1_000},
}
_GEMINI_FLASH = {"tpm": 250_000, "rpm": 10, "tpd": 5_000_000, "rpd": 250, "context": 1_048_576, "max_output": 65_536}
_GEMINI_LITE = {**_GEMINI_FLASH, "rpm": 15, "rpd": 1_000}
_UNKNOWN_GROQ = {"tpm": 6_000, "rpm": 30, "tpd": 100_000, "rpd": 1_000, "context": 32_768, "max_output": 8_192}

# Tokens the provider adds around messages (chat template, gpt-oss harmony header ~80).
_MESSAGE_OVERHEAD = 120
# o200k is gpt-oss's own tokenizer; qwen/gemini count ~5% more on the same text.
_ESTIMATE_PAD = 1.05
# Never ask for fewer completion tokens than this share of what the caller wanted.
_MIN_COMPLETION_SHARE = 0.6
# Upper bound for waiting while every model of a role is busy.
MAX_WAIT_SECONDS = 100.0
_POLL_SECONDS = 4.0
_REQUEST_TIMEOUT = 150.0
# Gemini contexts are huge; cap what we report so callers keep prompts sane.
_MAX_REPORTED_CAPACITY = 120_000


class LLMUnavailableError(RuntimeError):
    """No model for the role is configured, or every model is exhausted/failing."""


class LLMOutputError(RuntimeError):
    """A model answered, but the output was unusable (invalid JSON / schema) after the repair retry.

    ``text`` holds the last raw output so callers can salvage partial results.
    """

    def __init__(self, message: str, text: str = "") -> None:
        super().__init__(message)
        self.text = text


class _BadOutput(Exception):
    """Provider-side JSON validation failure (Groq ``json_validate_failed``)."""

    def __init__(self, text: str, message: str, model: str = "") -> None:
        super().__init__(message)
        self.text = text
        self.model = model


# --------------------------------------------------------------------------- tokens


_encoder: Any = None


def estimate_tokens(text: str) -> int:
    global _encoder
    if _encoder is None:
        try:
            import tiktoken

            _encoder = tiktoken.get_encoding("o200k_base")
        except Exception:  # offline / missing file: fall back to a char ratio
            _encoder = False
    if _encoder:
        return int(len(_encoder.encode(text, disallowed_special=())) * _ESTIMATE_PAD) + 1
    return int(len(text) / 3.3) + 1


def _messages_tokens(messages: list[BaseMessage]) -> int:
    total = _MESSAGE_OVERHEAD
    for m in messages:
        if isinstance(m.content, str):
            total += estimate_tokens(m.content) + 8
        else:
            for part in m.content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += estimate_tokens(part.get("text", ""))
                else:
                    total += 300  # images: Gemini bills ~258 tokens per image
    return total


# --------------------------------------------------------------------------- budgets


@dataclass
class _Reservation:
    model: str
    tokens: int
    at: float
    entry: list  # [timestamp, tokens] inside the window deque (mutable for correction)
    out_entry: list | None = None  # [timestamp, output tokens] in the OTPM window


@dataclass
class TokenBudget:
    model: str
    tpm: int
    rpm: int
    tpd: int
    rpd: int
    context: int
    max_output: int
    otpm: int = 0  # output tokens/minute; 0 = no separate output limit
    window: deque = field(default_factory=deque)  # [ts, tokens]
    out_window: deque = field(default_factory=deque)  # [ts, output tokens]
    requests: deque = field(default_factory=deque)  # ts
    day: str = ""
    day_tokens: int = 0
    day_requests: int = 0
    cooldown_until: float = 0.0
    disabled_reason: str | None = None

    def _prune(self, now: float) -> None:
        while self.window and now - self.window[0][0] >= 60:
            self.window.popleft()
        while self.out_window and now - self.out_window[0][0] >= 60:
            self.out_window.popleft()
        while self.requests and now - self.requests[0] >= 60:
            self.requests.popleft()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.day:
            self.day, self.day_tokens, self.day_requests = today, 0, 0

    def used_minute(self, now: float) -> float:
        """Tokens still counted against the minute: each request fades out linearly over 60 s.

        Groq refills TPM continuously ("Used 5800, Requested 1200, try again in 10s"), so a
        request's tokens free up gradually, not all at once after a minute.
        """
        self._prune(now)
        return sum(tokens * max(0.0, 1 - (now - ts) / 60) for ts, tokens in self.window)

    def used_output_minute(self, now: float) -> float:
        self._prune(now)
        return sum(tokens * max(0.0, 1 - (now - ts) / 60) for ts, tokens in self.out_window)

    def per_request_output(self) -> int:
        """Largest completion a single request may ask for."""
        return min(self.max_output, self.otpm) if self.otpm else self.max_output

    def wait_time(self, tokens: int, now: float, output: int = 0) -> float:
        """Seconds until ``tokens`` (``output`` of them completion) fit; ``inf`` if they never will today."""
        self._prune(now)
        if self.disabled_reason or tokens > self.tpm or (self.otpm and output > self.otpm):
            return float("inf")
        if self.day_tokens + tokens > self.tpd or self.day_requests + 1 > self.rpd:
            return float("inf")
        wait = max(0.0, self.cooldown_until - now)
        if len(self.requests) + 1 > self.rpm:
            wait = max(wait, 60 - (now - self.requests[0]))
        if self.used_minute(now) + tokens > self.tpm:
            lo, hi = 0.0, 60.0  # used_minute only decreases with time; binary-search the wait
            for _ in range(20):
                mid = (lo + hi) / 2
                if self.used_minute(now + mid) + tokens <= self.tpm:
                    hi = mid
                else:
                    lo = mid
            wait = max(wait, hi)
        if self.otpm and output and self.used_output_minute(now) + output > self.otpm:
            lo, hi = 0.0, 60.0
            for _ in range(20):
                mid = (lo + hi) / 2
                if self.used_output_minute(now + mid) + output <= self.otpm:
                    hi = mid
                else:
                    lo = mid
            wait = max(wait, hi)
        return wait

    def try_reserve(self, tokens: int, now: float, output: int = 0) -> _Reservation | None:
        if self.wait_time(tokens, now, output) > 0:
            return None
        entry = [now, tokens]
        self.window.append(entry)
        out_entry = None
        if self.otpm:
            out_entry = [now, output]
            self.out_window.append(out_entry)
        self.requests.append(now)
        self.day_tokens += tokens
        self.day_requests += 1
        return _Reservation(self.model, tokens, now, entry, out_entry)

    def settle(self, res: _Reservation, actual: int | None, actual_output: int | None = None) -> None:
        """Correct a reservation to actual usage (None = keep the reservation)."""
        if res.out_entry is not None and actual_output is not None:
            res.out_entry[1] = max(0, actual_output)
        if actual is None:
            return
        delta = actual - res.tokens
        res.entry[1] = max(0, actual)
        self.day_tokens = max(0, self.day_tokens + delta)

    def release(self, res: _Reservation) -> None:
        """The request never ran (e.g. rejected before generation): refund most of it."""
        self.settle(res, min(res.tokens, 50), 0)

    def penalize(self, seconds: float) -> None:
        self.cooldown_until = max(self.cooldown_until, time.monotonic() + max(1.0, seconds))


_budgets: dict[str, TokenBudget] = {}


def _limits_for(model: str) -> dict[str, int]:
    provider, _, name = model.partition(":")
    if model in DEFAULT_LIMITS:
        base = dict(DEFAULT_LIMITS[model])
    elif provider == "gemini":
        base = dict(_GEMINI_LITE if "lite" in name else _GEMINI_FLASH)
    else:
        base = dict(_UNKNOWN_GROQ)
    base.update({k: int(v) for k, v in (settings.llm_limits.get(model) or {}).items()})
    return base


def get_budget(model: str) -> TokenBudget:
    budget = _budgets.get(model)
    if budget is None:
        budget = _budgets[model] = TokenBudget(model=model, **_limits_for(model))
    return budget


def reset_budgets() -> None:
    """Forget all usage (tests)."""
    _budgets.clear()


# --------------------------------------------------------------------------- roles


def _provider_key(provider: str) -> str | None:
    if provider == "groq":
        return settings.groq_api_key
    if provider == "gemini":
        return settings.google_api_key
    return None


def role_models(role: str, prefer: int = 0) -> list[str]:
    """Usable models for ``role`` (provider key present, not disabled), rotated by ``prefer``."""
    models = []
    for spec in settings.llm_roles.get(role, []):
        provider, sep, name = spec.partition(":")
        if not sep or provider not in PROVIDERS or not name:
            logger.warning("Ignoring malformed model spec %r for role %r", spec, role)
            continue
        if not _provider_key(provider):
            continue
        if get_budget(spec).disabled_reason:
            continue
        models.append(spec)
    if models and prefer:
        k = prefer % len(models)
        models = models[k:] + models[:k]
    return models


def prompt_capacity(role: str, max_tokens: int) -> int:
    """Prompt tokens the first usable model of ``role`` accepts next to ``max_tokens`` of output.

    0 when the role has no usable model. Groq's TPM is the binding limit there
    (prompt + max_tokens <= TPM per request); Gemini reports a large, capped value.
    """
    models = role_models(role)
    if not models:
        return 0
    b = get_budget(models[0])
    per_request = min(b.context, b.tpm)
    return max(0, min(per_request - max_tokens - _MESSAGE_OVERHEAD, _MAX_REPORTED_CAPACITY))


def output_capacity(role: str) -> int:
    """Largest completion one request may ask for on the role's first usable model (0 = none)."""
    models = role_models(role)
    return get_budget(models[0]).per_request_output() if models else 0


# --------------------------------------------------------------------------- call records


@dataclass
class CallRecord:
    role: str
    model: str
    prompt_estimate: int
    max_tokens: int
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    finish_reason: str | None
    seconds: float
    attempt: int = 0
    error: str | None = None


_records: contextvars.ContextVar[list[CallRecord] | None] = contextvars.ContextVar("llm_call_records", default=None)


@contextlib.contextmanager
def track_calls() -> Iterator[list[CallRecord]]:
    """Collect a CallRecord for every LLM call made inside the block (incl. gathered tasks)."""
    records: list[CallRecord] = []
    token = _records.set(records)
    try:
        yield records
    finally:
        _records.reset(token)


def _record(rec: CallRecord) -> None:
    records = _records.get()
    if records is not None:
        records.append(rec)


# --------------------------------------------------------------------------- providers


@dataclass
class _Result:
    text: str
    model: str
    finish_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None


def _groq_kwargs(name: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if name.startswith("openai/gpt-oss"):
        # gpt-oss always reasons; effort controls how much (reasoning tokens count toward TPM).
        kwargs["reasoning_effort"] = settings.groq_reasoning_effort
        kwargs["reasoning_format"] = "hidden"
    elif name.startswith("qwen/"):
        # qwen3.x: keep any thinking out of the content (it would break JSON).
        kwargs["reasoning_format"] = "hidden"
    return kwargs


def _gemini_kwargs(name: str) -> dict[str, Any]:
    if name.startswith("gemini-2"):
        # 2.5 Flash/Flash-Lite: thinking budget in tokens (0 = off on Flash/Lite).
        return {"thinking_budget": 0 if "flash" in name else 1024}
    # Gemini 3+ ("-latest" aliases resolve to 3.x): thinking levels; "minimal" is rejected by 3.8 Flash.
    return {"thinking_level": "low"}


def _make_chat(model: str, max_tokens: int, temperature: float, json_mode: bool):
    provider, _, name = model.partition(":")
    if provider == "groq":
        from langchain_groq import ChatGroq

        kwargs = _groq_kwargs(name)
        if json_mode:
            kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}
        return ChatGroq(
            model=name,
            api_key=settings.groq_api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            max_retries=0,  # the router handles 429s by switching models
            timeout=_REQUEST_TIMEOUT,
            **kwargs,
        )
    from langchain_google_genai import ChatGoogleGenerativeAI

    kwargs = _gemini_kwargs(name)
    if json_mode:
        kwargs["response_mime_type"] = "application/json"
    return ChatGoogleGenerativeAI(
        model=name,
        google_api_key=settings.google_api_key,
        temperature=temperature,
        max_output_tokens=max_tokens,
        max_retries=0,
        timeout=_REQUEST_TIMEOUT,
        **kwargs,
    )


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and part.get("type") == "text":
            parts.append(part.get("text", ""))
    return "".join(parts)


_THINK_RE = re.compile(r"<think>.*?(</think>|$)", flags=re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


async def _call(model: str, messages: list[BaseMessage], max_tokens: int, temperature: float, json_mode: bool) -> _Result:
    chat = _make_chat(model, max_tokens, temperature, json_mode)
    response = await chat.ainvoke(messages)
    meta = response.response_metadata or {}
    usage = response.usage_metadata if isinstance(response, AIMessage) else None
    finish = meta.get("finish_reason")
    reasoning = None
    if usage:
        reasoning = (usage.get("output_token_details") or {}).get("reasoning")
    return _Result(
        text=strip_reasoning(_content_text(response.content)),
        model=model,
        finish_reason=str(finish) if finish is not None else None,
        input_tokens=usage.get("input_tokens") if usage else None,
        output_tokens=usage.get("output_tokens") if usage else None,
        reasoning_tokens=reasoning,
    )


# --------------------------------------------------------------------------- error classification


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        value = headers.get("retry-after")
        try:
            return float(value) if value is not None else None
        except ValueError:
            return None
    match = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", str(exc))
    if match:
        return int(match.group(1) or 0) * 60 + float(match.group(2))
    match = re.search(r"retry in ([\d.]+)s", str(exc), flags=re.IGNORECASE)
    return float(match.group(1)) if match else None


def _classify(exc: Exception) -> str:
    """'rate' (429: cool down, try next), 'fatal' (disable model), 'skip' (try next), 'bad_output'."""
    from langchain_core.exceptions import (
        ContextOverflowError,
        ModelAuthenticationError,
        ModelNotFoundError,
        ModelPermissionDeniedError,
        ModelRateLimitError,
    )

    try:
        import groq
    except ImportError:  # pragma: no cover
        groq = None  # type: ignore[assignment]

    if isinstance(exc, ModelRateLimitError):
        return "rate"
    if isinstance(exc, (ModelAuthenticationError, ModelPermissionDeniedError, ModelNotFoundError)):
        return "fatal"
    if isinstance(exc, ContextOverflowError):
        return "skip"
    if groq is not None:
        if isinstance(exc, groq.RateLimitError):
            return "rate"
        if isinstance(exc, (groq.AuthenticationError, groq.PermissionDeniedError, groq.NotFoundError)):
            return "fatal"
        if isinstance(exc, groq.APIStatusError):
            if exc.status_code == 429:
                return "rate"
            if exc.status_code == 400 and "json_validate_failed" in str(exc):
                return "bad_output"
            if exc.status_code == 400 and "model_decommissioned" in str(exc):
                return "fatal"
            return "skip"  # 413 too large, 5xx, 498 capacity, other 400s
    text = str(exc).lower()
    if "429" in text or "resource_exhausted" in text or "quota" in text:
        return "rate"
    return "skip"


_TOO_LARGE_RE = re.compile(r"Request too large.*?\((OTPM|TPM)\): Limit (\d+)", re.DOTALL)


def _learn_request_limit(budget: TokenBudget, exc: Exception) -> bool:
    """Groq 429 "Request too large ... (OTPM|TPM): Limit N": remember N; True if it was that kind."""
    match = _TOO_LARGE_RE.search(str(exc))
    if not match:
        return False
    kind, limit = match.group(1), int(match.group(2))
    if kind == "OTPM" and limit and (not budget.otpm or limit < budget.otpm):
        budget.otpm = limit
        logger.warning("Learned %s output limit: %d tokens/minute", budget.model, limit)
    elif kind == "TPM" and limit and limit < budget.tpm:
        budget.tpm = limit
        logger.warning("Learned %s token limit: %d tokens/minute", budget.model, limit)
    return True


def _failed_generation(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            return str(err.get("failed_generation") or "")
    return ""


# --------------------------------------------------------------------------- core loop


async def _run(
    role: str,
    messages: list[BaseMessage],
    *,
    max_tokens: int,
    temperature: float,
    prefer: int,
    json_mode: bool,
    attempt: int = 0,
    exclude: frozenset[str] = frozenset(),
    max_wait: float | None = None,
    min_output: int | None = None,
) -> _Result:
    models = [m for m in role_models(role, prefer) if m not in exclude] or role_models(role, prefer)
    if not models:
        raise LLMUnavailableError(
            f"No LLM is configured for role {role!r} (set GROQ_API_KEY or GOOGLE_API_KEY, check LLM_ROLES)"
        )
    prompt_est = _messages_tokens(messages)
    deadline = time.monotonic() + (max_wait if max_wait is not None else MAX_WAIT_SECONDS)
    failed: set[str] = set()
    last_error: Exception | None = None

    while True:
        now = time.monotonic()
        waits: list[float] = []
        for model in models:
            if model in failed:
                continue
            budget = get_budget(model)
            if budget.disabled_reason:
                failed.add(model)
                continue
            per_request = min(budget.tpm, budget.context)
            want = min(max_tokens, budget.per_request_output())
            if want < (min_output or max(256, int(max_tokens * _MIN_COMPLETION_SHARE))):
                logger.info("LLM %s skips %s: output cap %d < needed %d", role, model, want, max_tokens)
                failed.add(model)
                continue
            room = per_request - prompt_est
            if room < want:
                if room < (min_output or max(256, int(want * _MIN_COMPLETION_SHARE))):
                    logger.info("LLM %s skips %s: prompt ~%d tokens too large (limit %d)", role, model, prompt_est, per_request)
                    failed.add(model)
                    continue
                want = room
            need = prompt_est + want
            reservation = budget.try_reserve(need, now, want)
            if reservation is None:
                waits.append(budget.wait_time(need, now, want))
                continue

            started = time.monotonic()
            try:
                result = await _call(model, messages, want, temperature, json_mode)
            except asyncio.CancelledError:
                budget.settle(reservation, prompt_est)
                raise
            except Exception as exc:  # noqa: BLE001 - classified below
                kind = _classify(exc)
                elapsed = time.monotonic() - started
                _record(CallRecord(role, model, prompt_est, want, None, None, None, None, elapsed, attempt, f"{kind}: {type(exc).__name__}"))
                if kind == "bad_output":
                    budget.settle(reservation, prompt_est + want // 2)
                    raise _BadOutput(_failed_generation(exc), str(exc)[:300], model) from exc
                last_error = exc
                if kind == "rate" and _learn_request_limit(budget, exc):
                    # "Request too large": this request can never fit this model; others may.
                    budget.release(reservation)
                    failed.add(model)
                    logger.warning("LLM %s: %s cannot take this request (%s); trying next model", role, model, str(exc)[:160])
                    continue
                if kind == "rate":
                    wait = _retry_after(exc) or 20.0
                    budget.release(reservation)
                    budget.penalize(wait)
                    logger.warning("LLM %s: %s rate-limited (retry after %.0fs); trying next model", role, model, wait)
                    continue
                budget.release(reservation)
                failed.add(model)
                if kind == "fatal":
                    budget.disabled_reason = f"{type(exc).__name__}: {str(exc)[:200]}"
                    logger.error("LLM %s: disabling %s: %s", role, model, budget.disabled_reason)
                else:
                    logger.warning("LLM %s: %s failed (%s: %s); trying next model", role, model, type(exc).__name__, str(exc)[:300])
                continue

            elapsed = time.monotonic() - started
            actual = None
            if result.input_tokens is not None and result.output_tokens is not None:
                actual = result.input_tokens + result.output_tokens
            budget.settle(reservation, actual, result.output_tokens)
            rec = CallRecord(
                role, model, prompt_est, want, result.input_tokens, result.output_tokens,
                result.reasoning_tokens, result.finish_reason, elapsed, attempt,
            )
            _record(rec)
            logger.info(
                "LLM %s -> %s: prompt ~%d (actual %s) + out %s (reasoning %s) / max %d, finish=%s, %.1fs",
                role, model, prompt_est, result.input_tokens, result.output_tokens,
                result.reasoning_tokens, want, result.finish_reason, elapsed,
            )
            return result

        if all(m in failed for m in models):
            raise LLMUnavailableError(
                f"Every model for role {role!r} failed"
                + (f" (last error: {type(last_error).__name__}: {str(last_error)[:200]})" if last_error else "")
            )
        wait = min(waits) if waits else _POLL_SECONDS
        remaining = deadline - time.monotonic()
        if wait == float("inf") or wait > remaining:
            raise LLMUnavailableError(
                f"All models for role {role!r} are out of free-tier budget right now; try again in a minute"
            )
        logger.info("LLM %s: every model busy, waiting %.1fs", role, min(wait, _POLL_SECONDS) if wait > _POLL_SECONDS else wait)
        await asyncio.sleep(min(max(wait, 0.2), _POLL_SECONDS) + 0.1)


# --------------------------------------------------------------------------- JSON helpers


def repair_json(text: str, max_fixes: int = 25) -> Any:
    """Parse near-valid JSON from LLMs: stray closing brackets, missing commas, '"{key":' typos, trailing junk.

    Greedy: at each parse error try a few local edits (delete a nearby '}' / ']', insert a comma) and keep
    the one that parses furthest. Raises json.JSONDecodeError if it cannot be repaired.
    """
    text = re.sub(r'"\{(\w+)"\s*:', r'{"\1":', text)
    text = re.sub(r'(?<=[,\[])\s*"\{"', '{"', text)
    last_exc: json.JSONDecodeError | None = None
    for _ in range(max_fixes):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            last_exc = exc
            if exc.msg.startswith("Extra data"):
                text = text[: exc.pos]
                continue
            candidates = []
            lo = max(0, exc.pos - 400)
            for j in range(exc.pos - 1, lo - 1, -1):
                if text[j] in "}]":
                    candidates.append(text[:j] + text[j + 1 :])
                    if len(candidates) >= 6:
                        break
            candidates.append(text[: exc.pos] + "," + text[exc.pos :])
            best, best_pos = None, exc.pos
            for cand in candidates:
                try:
                    return json.loads(cand)
                except json.JSONDecodeError as e2:
                    if e2.pos > best_pos + 1:
                        best, best_pos = cand, e2.pos
            if best is None:
                break
            text = best
    raise last_exc or json.JSONDecodeError("unrepairable", text, 0)


def _extract_json(text: str) -> Any:
    text = strip_reasoning(text)
    fenced = re.search(r"```(?:json)?\s*\n(.*?)\n?```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    end = max(text.rfind("}"), text.rfind("]"))
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return repair_json(text[start : end + 1])
    raise json.JSONDecodeError("No JSON object found", text, 0)


def _validate(schema: type[T], text: str) -> T:
    data = _extract_json(text)
    if isinstance(data, list):
        # Some models return the bare list when the schema has a single list field.
        list_fields = [n for n, f in schema.model_fields.items() if "list" in str(f.annotation)]
        if len(list_fields) == 1:
            data = {list_fields[0]: data}
    return schema.model_validate(data)


def salvage_objects(text: str, key: str) -> list[dict[str, Any]]:
    """Complete objects from a broken/truncated ``{"<key>": [ {...}, {...}, ...`` JSON answer."""
    text = strip_reasoning(text or "")
    match = re.search(r'"' + re.escape(key) + r'"\s*:\s*\[', text)
    if not match:
        return []
    decoder = json.JSONDecoder()
    pos, out = match.end(), []
    while pos < len(text):
        while pos < len(text) and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(text) or text[pos] != "{":
            break
        try:
            obj, pos = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            out.append(obj)
    return out


# --------------------------------------------------------------------------- public API


async def invoke_text(
    role: str,
    messages: list[BaseMessage],
    *,
    max_tokens: int,
    temperature: float = 0.7,
    prefer: int = 0,
    max_wait: float | None = None,
) -> str:
    """Run ``messages`` on the first available model for ``role``; return the text.

    ``prefer`` rotates the start of the role's model list, so parallel writer
    groups (prefer=0, 1, 2) land on different models.
    """
    result = await _run(role, messages, max_tokens=max_tokens, temperature=temperature, prefer=prefer, json_mode=False,
                        max_wait=max_wait)
    if result.finish_reason in ("length", "MAX_TOKENS"):
        logger.warning("LLM %s output from %s hit max_tokens=%d and may be cut off", role, result.model, max_tokens)
    return result.text


async def invoke_json(
    role: str,
    messages: list[BaseMessage],
    schema: type[T],
    *,
    max_tokens: int,
    temperature: float = 0.7,
    prefer: int = 0,
    max_wait: float | None = None,
    min_output: int | None = None,
) -> T:
    """Like ``invoke_text`` but in JSON mode, validated into ``schema`` (one repair retry).

    The repair retry re-sends the original messages plus a note naming the error,
    on the next model of the role when there is one (a fresh sample on another
    model is cheaper and more reliable than echoing a long broken answer back).
    Raises ``LLMOutputError`` if both attempts are unusable.
    """
    error = ""
    last_text = ""
    used: set[str] = set()
    for attempt in range(2):
        msgs = list(messages)
        if attempt:
            msgs.append(
                HumanMessage(
                    "A previous attempt at this answer was rejected: "
                    f"{error[:400]}\nReturn ONLY one valid JSON object with the requested fields. "
                    "Keep it complete and well under the length limit: shorten text rather than stopping mid-object."
                )
            )
        try:
            result = await _run(
                role, msgs, max_tokens=max_tokens, temperature=temperature if not attempt else max(0.3, temperature - 0.2),
                prefer=prefer + attempt, json_mode=True, attempt=attempt, exclude=frozenset(used), max_wait=max_wait,
                min_output=min_output,
            )
        except _BadOutput as bad:
            used.add(bad.model)
            last_text = bad.text or last_text
            if bad.text:
                try:
                    return _validate(schema, bad.text)
                except (ValueError, ValidationError):
                    pass
            error = "invalid JSON (the provider's JSON validation rejected it)"
            if bad.text:
                try:
                    json.loads(strip_reasoning(bad.text))
                except json.JSONDecodeError as jexc:
                    logger.info("LLM %s rejected JSON near: %r (%s)", role, bad.text[max(0, jexc.pos - 120) : jexc.pos + 60], jexc.msg)
            logger.warning("LLM %s JSON attempt %d rejected by provider: %s", role, attempt + 1, str(bad)[:200])
            continue
        used.add(result.model)
        last_text = result.text
        try:
            return _validate(schema, result.text)
        except json.JSONDecodeError as exc:
            cut = result.finish_reason in ("length", "MAX_TOKENS")
            error = "the JSON was cut off (too long)" if cut else f"invalid JSON: {exc}"
            logger.info("LLM %s bad JSON near: %r", role, exc.doc[max(0, exc.pos - 120) : exc.pos + 60] if exc.doc else "")
        except ValidationError as exc:
            error = "schema errors: " + "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:6]
            )
        logger.warning(
            "LLM %s JSON attempt %d from %s unusable (finish=%s): %s", role, attempt + 1, result.model, result.finish_reason, error[:300]
        )
    raise LLMOutputError(f"{role} returned unusable JSON twice: {error[:300]}", last_text)


async def invoke_vision_json(prompt: str, images: list[tuple[bytes, str]]) -> dict[str, Any] | None:
    """Ask the "vision" role about ``images`` (bytes, mime type); return parsed JSON.

    Returns None when no vision model is configured or available, so callers can
    fall back to a non-LLM choice. Only Gemini models are used (multimodal).
    """
    all_models = role_models("vision")
    models = [m for m in all_models if m.startswith("gemini:")]
    if not models or not images:
        return None
    non_vision = frozenset(m for m in all_models if m not in models)
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for data, mime in images:
        b64 = base64.b64encode(data).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime or 'image/jpeg'};base64,{b64}"}})
    messages: list[BaseMessage] = [HumanMessage(content=content)]
    try:
        result = await _run(
            "vision", messages, max_tokens=1024, temperature=0.2, prefer=0, json_mode=True, exclude=non_vision
        )
        data = _extract_json(result.text)
    except Exception as exc:  # noqa: BLE001 - vision is optional; callers fall back
        logger.warning("Vision call failed (%s: %s); falling back", type(exc).__name__, str(exc)[:200])
        return None
    if isinstance(data, list):
        return {"items": data}
    return data if isinstance(data, dict) else None
