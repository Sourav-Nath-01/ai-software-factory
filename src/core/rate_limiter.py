"""Proactive Token Bucket Rate Limiter.

Instead of reacting to 429 (Too Many Requests) errors AFTER they happen,
this module estimates the token count of an outgoing request and proactively
sleeps until the bucket has enough capacity.

This is the correct architectural pattern for shared LLM API pools:
  - Reactive (old): send request → get 429 → parse wait time → sleep → retry
  - Proactive (new): estimate tokens → check bucket → sleep if needed → send

Architecture:
    TokenBucket uses a refilling token bucket algorithm:
    - Each provider has a max capacity (tokens per minute)
    - Tokens refill at a constant rate proportional to the RPM limit
    - Before each LLM call, we reserve the estimated prompt tokens PLUS the
      output budget (providers count both against TPM)
    - If the bucket doesn't have enough, we sleep until it refills
    - After the call, reconcile() refunds what the response didn't use

Usage:
    bucket = get_rate_limiter("groq/openai/gpt-oss-120b")
    wait = bucket.consume(prompt_text, extra_tokens=max_tokens)
    ...call the LLM...
    bucket.reconcile(reserved=..., actual=usage.total_tokens)
"""
from __future__ import annotations

import threading
import time
from typing import Callable


# ── Provider-specific limits ───────────────────────────────────
# Free-tier limits change often; Groq reports yours in the
# x-ratelimit-limit-tokens response header.

PROVIDER_LIMITS: dict[str, dict] = {
    "groq": {
        "tpm": 8_000,       # openai/gpt-oss-120b, openai/gpt-oss-20b free tier (plus 200K tokens/day per model)
        "rpm": 30,
    },
    "gemini": {
        "tpm": 250_000,     # gemini-2.5-flash free tier
        "rpm": 10,
    },
    "openai": {
        "tpm": 200_000,     # gpt-4o-mini tier-1
        "rpm": 500,
    },
    "default": {
        "tpm": 100_000,
        "rpm": 60,
    },
}


def _resolve_limits(model: str) -> dict:
    """Map a model string to its provider limits.

    The provider prefix decides, not the model name: "groq/openai/gpt-oss-120b"
    contains "gpt" but is served under Groq's limits.
    """
    m = model.lower()
    if m.startswith("groq/"):
        return PROVIDER_LIMITS["groq"]
    if m.startswith("gemini/") or "gemini" in m:
        return PROVIDER_LIMITS["gemini"]
    if m.startswith("openai/") or "/" not in m:
        return PROVIDER_LIMITS["openai"]
    return PROVIDER_LIMITS["default"]


def estimate_tokens(text: str) -> int:
    """
    Fast, dependency-free token count estimate.
    Approximation: 1 token ≈ 4 characters (works well for English/code).
    Using tiktoken would be more accurate but adds a heavy dependency.
    """
    return max(1, len(text) // 4)


# ── Token Bucket ───────────────────────────────────────────────

class TokenBucket:
    """
    Thread-safe token bucket rate limiter for a single LLM provider.

    Tokens refill continuously over time at rate = tpm / 60 tokens/second.
    Each LLM call consumes an estimated token count from the bucket.
    If the bucket is empty, we sleep until enough tokens are available.
    """

    def __init__(self, tpm: int, rpm: int):
        self._tpm      = tpm
        self._rpm      = rpm
        self._capacity = tpm                     # max tokens in bucket
        self._tokens   = float(tpm)              # current tokens (start full)
        self._refill_rate = tpm / 60.0           # tokens added per second
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

        # Request tracking for RPM (simple sliding window)
        self._request_times: list[float] = []

    def _refill(self):
        """Add tokens based on elapsed time since last refill (call with lock held)."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        added = elapsed * self._refill_rate
        self._tokens = min(self._capacity, self._tokens + added)
        self._last_refill = now

    def _check_rpm(self) -> float:
        """
        Returns seconds to wait if RPM limit would be exceeded, else 0.
        Uses a 60-second sliding window.
        """
        now = time.monotonic()
        # Remove requests older than 60s
        self._request_times = [t for t in self._request_times if now - t < 60]
        if len(self._request_times) >= self._rpm:
            # Must wait until the oldest request drops out of the window
            oldest = self._request_times[0]
            wait = 60.0 - (now - oldest) + 0.5  # +0.5s safety margin
            return max(0.0, wait)
        return 0.0

    @property
    def capacity(self) -> int:
        return self._capacity

    def consume(self, text: str, sleep: Callable[[float], None] = time.sleep,
                extra_tokens: int = 0) -> float:
        """
        Estimate tokens for `text` (+ `extra_tokens`), then block until the bucket can serve them.

        Args:
            sleep: how to wait — callers pass an interruptible sleep so a
                   cancelled run doesn't sit out a long rate-limit wait.
            extra_tokens: output budget to reserve (providers count output against TPM too).

        Returns:
            float: total seconds slept (0 if no wait was needed)
        """
        # A request bigger than the whole bucket could never be served and would
        # wait forever — cap it so it waits for a full bucket at most.
        needed = min(estimate_tokens(text) + extra_tokens, self._capacity)
        total_wait = 0.0

        while True:
            with self._lock:
                self._refill()

                # Check RPM limit first
                rpm_wait = self._check_rpm()
                if rpm_wait > 0:
                    pass  # sleep outside lock
                elif self._tokens >= needed:
                    # Enough capacity — consume tokens and record request
                    self._tokens -= needed
                    self._request_times.append(time.monotonic())
                    return total_wait
                else:
                    # Not enough tokens — calculate wait time
                    deficit = needed - self._tokens
                    rpm_wait = deficit / self._refill_rate

            # Sleep outside the lock; a minimum of 0.2s avoids busy-spinning on tiny deficits
            wait = max(rpm_wait, 0.2)
            sleep(wait)
            total_wait += wait

    def reconcile(self, reserved: int, actual: int):
        """After a call, return reserved-but-unused tokens (or charge an overrun)."""
        with self._lock:
            self._refill()
            self._tokens = min(self._capacity, self._tokens + (reserved - actual))


# ── Registry ──────────────────────────────────────────────────

_buckets: dict[str, TokenBucket] = {}
_bucket_lock = threading.Lock()


def get_rate_limiter(model: str) -> TokenBucket:
    """Get or create a TokenBucket for the given model string."""
    with _bucket_lock:
        if model not in _buckets:
            limits = _resolve_limits(model)
            _buckets[model] = TokenBucket(tpm=limits["tpm"], rpm=limits["rpm"])
        return _buckets[model]


def reset_buckets():
    """Reset all buckets (useful for testing)."""
    with _bucket_lock:
        _buckets.clear()
