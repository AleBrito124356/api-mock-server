"""Chaos determinism: seeded errors, latency, rate limiting."""
import pytest

from mockserver import build_config, create_app
from mockserver.behavior import BehaviorEngine
from tests.conftest import make_client


def test_same_seed_replays_identical_error_sequence():
    spec = {"error_rate": 0.5}
    a = BehaviorEngine(seed=123)
    b = BehaviorEngine(seed=123)
    seq_a = [a.should_error(spec) for _ in range(40)]
    seq_b = [b.should_error(spec) for _ in range(40)]
    assert seq_a == seq_b
    # A 50% rate over 40 draws should produce a mix, not all-or-nothing.
    assert 0 < sum(seq_a) < 40


def test_different_seed_diverges():
    spec = {"error_rate": 0.5}
    a = [BehaviorEngine(seed=1).should_error(spec) for _ in range(40)]
    b = [BehaviorEngine(seed=2).should_error(spec) for _ in range(40)]
    assert a != b


def test_error_rate_bounds():
    assert BehaviorEngine(seed=1).should_error({"error_rate": 0}) is False
    assert BehaviorEngine(seed=1).should_error({"error_rate": 1}) is True
    assert BehaviorEngine(seed=1).should_error(None) is False


def test_fixed_latency_is_exact():
    eng = BehaviorEngine(seed=1)
    assert eng.latency_seconds({"fixed_ms": 250}) == 0.25
    assert eng.latency_seconds(None) == 0.0


def test_random_latency_is_seeded_and_in_range():
    a = BehaviorEngine(seed=9).latency_seconds({"random_ms": [100, 200]})
    b = BehaviorEngine(seed=9).latency_seconds({"random_ms": [100, 200]})
    assert a == b
    assert 0.1 <= a <= 0.2


def test_rate_limit_blocks_after_limit():
    eng = BehaviorEngine(seed=1)
    spec = {"rate_limit": {"limit": 3, "window_ms": 60000}}
    results = [eng.rate_limited("k", spec) for _ in range(5)]
    # First 3 allowed (None), last 2 blocked (the rate_limit dict).
    assert results[0] is None and results[1] is None and results[2] is None
    assert results[3] is not None and results[4] is not None


@pytest.mark.asyncio
async def test_always_error_route_returns_configured_status():
    config = build_config(
        {
            "config": {"seed": 1},
            "routes": [
                {
                    "method": "GET",
                    "path": "/down",
                    "chaos": {"error_rate": 1, "error_status": 503, "error_body": {"e": "x"}},
                    "response": {"status": 200, "body": {"ok": True}},
                }
            ],
        }
    )
    async with make_client(create_app(config)) as client:
        r = await client.get("/down")
        assert r.status_code == 503
        assert r.json() == {"e": "x"}


@pytest.mark.asyncio
async def test_never_error_route_is_stable():
    config = build_config(
        {
            "config": {"seed": 1},
            "routes": [
                {
                    "method": "GET",
                    "path": "/up",
                    "chaos": {"error_rate": 0},
                    "response": {"status": 200, "body": {"ok": True}},
                }
            ],
        }
    )
    async with make_client(create_app(config)) as client:
        for _ in range(10):
            r = await client.get("/up")
            assert r.status_code == 200


@pytest.mark.asyncio
async def test_two_apps_same_seed_produce_same_statuses():
    def build():
        return build_config(
            {
                "config": {"seed": 77},
                "routes": [
                    {
                        "method": "GET",
                        "path": "/roll",
                        "chaos": {"error_rate": 0.5, "error_status": 500},
                        "response": {"status": 200, "body": {"ok": True}},
                    }
                ],
            }
        )

    async with make_client(create_app(build())) as c1:
        s1 = [(await c1.get("/roll")).status_code for _ in range(20)]
    async with make_client(create_app(build())) as c2:
        s2 = [(await c2.get("/roll")).status_code for _ in range(20)]
    assert s1 == s2
