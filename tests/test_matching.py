"""Match precedence: priority, query/header/body matchers, path params."""
import pytest

from mockserver import build_config, create_app
from tests.conftest import make_client


def _config():
    return build_config(
        {
            "config": {"seed": 1, "cors": True},
            "routes": [
                # Generic list, lowest priority.
                {
                    "method": "GET",
                    "path": "/products",
                    "priority": 0,
                    "response": {"status": 200, "body": {"match": "generic"}},
                },
                # Query-specific, higher priority - must win.
                {
                    "method": "GET",
                    "path": "/products",
                    "priority": 10,
                    "match": {"query": {"category": "books"}},
                    "response": {"status": 200, "body": {"match": "books"}},
                },
                # Header matcher.
                {
                    "method": "GET",
                    "path": "/whoami",
                    "match": {"headers": {"X-Role": "admin"}},
                    "response": {"status": 200, "body": {"role": "admin"}},
                },
                {
                    "method": "GET",
                    "path": "/whoami",
                    "response": {"status": 200, "body": {"role": "guest"}},
                },
                # Body matcher.
                {
                    "method": "POST",
                    "path": "/login",
                    "match": {"body": {"user": "ada"}},
                    "response": {"status": 200, "body": {"ok": True}},
                },
                {
                    "method": "POST",
                    "path": "/login",
                    "response": {"status": 401, "body": {"ok": False}},
                },
                # Path params.
                {
                    "method": "GET",
                    "path": "/items/{id}",
                    "response": {"status": 200, "body": {"id": "{{ request.path.id }}"}},
                },
            ],
        }
    )


@pytest.mark.asyncio
async def test_priority_breaks_ties_between_same_path():
    app = create_app(_config())
    async with make_client(app) as client:
        r = await client.get("/products", params={"category": "books"})
        assert r.json() == {"match": "books"}


@pytest.mark.asyncio
async def test_generic_route_used_when_matcher_fails():
    app = create_app(_config())
    async with make_client(app) as client:
        r = await client.get("/products", params={"category": "toys"})
        assert r.json() == {"match": "generic"}

        r2 = await client.get("/products")
        assert r2.json() == {"match": "generic"}


@pytest.mark.asyncio
async def test_header_matching():
    app = create_app(_config())
    async with make_client(app) as client:
        admin = await client.get("/whoami", headers={"X-Role": "admin"})
        assert admin.json() == {"role": "admin"}

        guest = await client.get("/whoami")
        assert guest.json() == {"role": "guest"}


@pytest.mark.asyncio
async def test_body_matching():
    app = create_app(_config())
    async with make_client(app) as client:
        ok = await client.post("/login", json={"user": "ada", "pw": "x"})
        assert ok.status_code == 200
        assert ok.json() == {"ok": True}

        bad = await client.post("/login", json={"user": "eve"})
        assert bad.status_code == 401


@pytest.mark.asyncio
async def test_path_param_extraction():
    app = create_app(_config())
    async with make_client(app) as client:
        r = await client.get("/items/abc123")
        assert r.json() == {"id": "abc123"}


@pytest.mark.asyncio
async def test_unmatched_returns_404_with_hint():
    app = create_app(_config())
    async with make_client(app) as client:
        r = await client.get("/nope")
        assert r.status_code == 404
        assert r.json()["error"] == "no_mock_matched"


@pytest.mark.asyncio
async def test_method_mismatch_does_not_match_route():
    app = create_app(_config())
    async with make_client(app) as client:
        r = await client.delete("/products")
        assert r.status_code == 404
