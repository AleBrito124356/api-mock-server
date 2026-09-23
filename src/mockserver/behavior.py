"""Chaos and latency injection.

A frontend that only ever talks to a fast, perfect backend breaks the first
time production adds 400ms of latency or returns a 503. This module lets a
mocks file simulate a bad network on purpose:

    latency:                 fixed or random delay per route or globally
      fixed_ms: 250
      random_ms: [100, 800]

    chaos:
      error_rate: 0.2        fraction of requests that fail
      error_status: 503
      error_body: {...}
      rate_limit:            token-bucket style sliding window
        limit: 60
        window_ms: 60000
        status: 429

Every random decision is drawn from a single seeded RNG, so a given seed
replays the exact same sequence of failures. Draw order is fixed:
rate-limit check (no RNG) -> error decision (one ``random()``) ->
latency (one ``uniform()`` only when ``random_ms`` is used).
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from random import Random
from typing import Any, Deque, Dict, Optional, Tuple

_DEFAULT_ERROR_BODY = {
    "error": "injected_failure",
    "message": "Chaos injection: this failure was simulated by api-mock-server.",
}


class BehaviorEngine:
    """Seeded latency, error and rate-limit decisions."""

    def __init__(self, seed: Optional[int] = None) -> None:
        self.seed = seed
        self.rng = Random(seed)
        self._buckets: Dict[str, Deque[float]] = defaultdict(deque)

    def reset(self) -> None:
        """Rewind the RNG to its seed and empty every rate-limit window.

        After a reset the same seeded sequence of failures and delays plays
        again from the start, which is what an e2e suite wants between tests.
        """
        self.rng = Random(self.seed)
        self._buckets.clear()

    # -- latency ---------------------------------------------------------- #
    def latency_seconds(self, spec: Optional[Dict[str, Any]]) -> float:
        if not spec:
            return 0.0
        if "fixed_ms" in spec:
            ms = float(spec["fixed_ms"])
        elif "random_ms" in spec:
            lo, hi = spec["random_ms"]
            ms = self.rng.uniform(float(lo), float(hi))
        elif "min_ms" in spec or "max_ms" in spec:
            lo = float(spec.get("min_ms", 0))
            hi = float(spec.get("max_ms", lo))
            ms = self.rng.uniform(lo, hi)
        else:
            ms = 0.0
        return max(0.0, ms / 1000.0)

    # -- errors ----------------------------------------------------------- #
    def should_error(self, spec: Optional[Dict[str, Any]]) -> bool:
        if not spec:
            return False
        rate = float(spec.get("error_rate", 0) or 0)
        if rate <= 0:
            return False
        if rate >= 1:
            return True
        return self.rng.random() < rate

    def error_response(self, spec: Optional[Dict[str, Any]]) -> Tuple[int, Any]:
        spec = spec or {}
        status = int(spec.get("error_status", 500))
        body = spec.get("error_body", _DEFAULT_ERROR_BODY)
        return status, body

    # -- rate limiting ---------------------------------------------------- #
    def rate_limited(self, key: str, spec: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Return the rate_limit spec if this call is over the limit, else None.

        Uses a sliding window of recent timestamps per key. A call that is
        allowed records its timestamp; a call that is blocked does not.
        """
        rl = (spec or {}).get("rate_limit")
        if not rl:
            return None
        limit = int(rl["limit"])
        window = float(rl.get("window_ms", 1000)) / 1000.0
        now = time.monotonic()
        bucket = self._buckets[key]
        while bucket and (now - bucket[0]) > window:
            bucket.popleft()
        if len(bucket) >= limit:
            return rl
        bucket.append(now)
        return None
