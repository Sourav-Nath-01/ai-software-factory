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
    - Before each LLM call, we "consume" an estimated token count
    - If the bucket doesn't have enough, we sleep until it refills

Usage:
    bucket = get_rate_limiter("gemini/gemini-2.0-flash")
    wait = bucket.consume(estimated_tokens=2000)
    # if wait > 0, we already slept — just proceed with the LLM call
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


# ── Provider-specific limits ───────────────────────────────────
# Based on publicly documented free-tier limits

PROVIDER_LIMITS: dict[str, dict] = {
    "gemini_flash": {
        "tpm": 1_000_000,   # gemini-2.0-flash free tier
        "rpm": 15,          # free tier is 15 requests/min
    },
    "gemini_pro": {
        "tpm": 1_000_000,
        "rpm": 15,
    },
    "groq_small": {
        "tpm": 30_000,      # llama-3.1-8b-instant
        "rpm": 30,
    },
    "groq_large": {
        "tpm": 12_000,      # llama-3.3-70b-versatile
        "rpm": 30,
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
    """Map a model string to its provider limits."""
    m = model.lower()
    if "gemini-2.0-flash" in m or "gemini-flash" in m:
        return PROVIDER_LIMITS["gemini_flash"]
    if "gemini" in m:
        return PROVIDER_LIMITS["gemini_pro"]
    if "llama-3.3-70b" in m:
        return PROVIDER_LIMITS["groq_large"]
    if "groq/" in m or "llama" in m:
        return PROVIDER_LIMITS["groq_small"]
    if "gpt" in m or "openai/" in m:
        return PROVIDER_LIMITS["openai"]
    return PROVIDER_LIMITS["default"]


def _estimate_tokens(text: str) -> int:
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

    def consume(self, text: str) -> float:
        """
        Estimate tokens for `text`, then block until the bucket can serve them.

        Returns:
            float: total seconds slept (0 if no wait was needed)
        """
        needed = _estimate_tokens(text)
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

            # Only sleep if the wait is meaningful (> 0.2s)
            if rpm_wait > 0.2:
                time.sleep(rpm_wait)
                total_wait += rpm_wait


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
