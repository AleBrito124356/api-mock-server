"""CORS: browsers must be able to write to stateful resources and read headers."""
import pytest

from mockserver import build_config, create_app
from tests.conftest import make_client

ORIGIN = "http://localhost:5173"


def _app(cors=True, extra_routes=None):
    return create_app(
        build_config(
            {
                "config": {"cors": cors},
                "routes": [
                    {"method": "GET", "path": "/ping", "name": "ping", "response": {"body": {"ok": True}}},
                    {
                        "method": "ANY",
                        "path": "/anything",
                        "response": {"status": 200, "body": {"any": True}},
                    },
                ]
                + (extra_routes or []),
                "resources": [
                    {"name": "todos", "path": "/todos", "seed": [{"id": 1, "title": "a", "done": False}]}
                ],
            }
        )
    )


def _preflight_headers(method, headers="content-type"):
    return {
        "Origin": ORIGIN,
        "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": headers,
    }


@pytest.mark.asyncio
async def test_preflight_on_collection_is_204_not_405():
    async with make_client(_app()) as client:
        r = await client.options("/todos", headers=_preflight_headers("POST"))
        assert r.status_code == 204
        assert r.headers["access-control-allow-origin"] == ORIGIN
        assert "POST" in r.headers["access-control-allow-methods"]
        assert r.headers["access-control-allow-headers"] == "content-type"
        assert r.headers["access-control-allow-credentials"] == "true"


@pytest.mark.asyncio
async def test_preflight_on_item_for_patch_and_delete():
    async with make_client(_app()) as client:
        for method in ("PATCH", "DELETE", "PUT"):
            r = await client.options("/todos/1", headers=_preflight_headers(method, "content-type, authorization"))
            assert r.status_code == 204, method
            assert method in r.headers["access-control-allow-methods"]
            # Authorization cannot be covered by a "*" wildcard, so it is echoed.
            assert "authorization" in r.headers["access-control-allow-headers"]


@pytest.mark.asyncio
async def test_js_style_json_post_then_succeeds_and_exposes_headers():
    async with make_client(_app()) as client:
        pre = await client.options("/todos", headers=_preflight_headers("POST"))
        assert pre.status_code == 204
        r = await client.post("/todos", json={"title": "from the browser"}, headers={"Origin": ORIGIN})
        assert r.status_code == 201
        assert r.headers["access-control-allow-origin"] == ORIGIN
        exposed = r.headers["access-control-expose-headers"]
        assert "location" in exposed

        listing = await client.get("/todos", headers={"Origin": ORIGIN})
        assert "x-total-count" in listing.headers["access-control-expose-headers"]
        assert "Origin" in listing.headers["vary"]


@pytest.mark.asyncio
async def test_any_route_does_not_swallow_preflight():
    async with make_client(_app()) as client:
        r = await client.options("/anything", headers=_preflight_headers("PUT"))
        assert r.status_code == 204
        assert r.content == b""


@pytest.mark.asyncio
async def test_explicit_options_route_still_wins():
    extra = [{"method": "OPTIONS", "path": "/custom", "response": {"status": 200, "body": {"custom": True}}}]
    async with make_client(_app(extra_routes=extra)) as client:
        r = await client.options("/custom", headers=_preflight_headers("POST"))
        assert r.status_code == 200
        assert r.json() == {"custom": True}


@pytest.mark.asyncio
async def test_plain_options_on_resource_reports_allow():
    async with make_client(_app()) as client:
        r = await client.options("/todos/1")
        assert r.status_code == 204
        assert "PATCH" in r.headers["allow"]


@pytest.mark.asyncio
async def test_no_origin_gets_wildcard_and_cors_can_be_disabled():
    async with make_client(_app()) as client:
        r = await client.get("/ping")
        assert r.headers["access-control-allow-origin"] == "*"
        assert r.headers["x-matched-route"] == "ping"
    async with make_client(_app(cors=False)) as client:
        r = await client.get("/ping", headers={"Origin": ORIGIN})
        assert "access-control-allow-origin" not in r.headers
