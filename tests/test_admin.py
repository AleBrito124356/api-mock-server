"""Admin API: request journal, verification, reset, response sequences."""
import os

import pytest

from mockserver import build_config, create_app, load_config
from mockserver.validate import validate_data
from tests.conftest import make_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOP = os.path.join(ROOT, "examples", "shop-api", "mocks.yaml")


def _app(extra=None):
    data = {
        "config": {"seed": 5},
        "routes": [
            {"name": "create-order", "method": "POST", "path": "/orders",
             "response": {"status": 201, "body": {"id": "{{ seq.order(1000) }}"}}},
            {"name": "roll", "method": "GET", "path": "/roll",
             "chaos": {"error_rate": 0.5, "error_status": 503}, "response": {"body": {"ok": True}}},
            {"name": "job", "method": "GET", "path": "/jobs/{id}", "sequence": "stick", "responses": [
                {"status": 202, "body": {"state": "pending"}},
                {"status": 202, "body": {"state": "pending"}},
                {"status": 200, "body": {"state": "done", "id": "{{ request.path.id }}"}},
            ]},
            {"name": "light", "method": "GET", "path": "/light", "sequence": "cycle", "responses": [
                {"body": "red"}, {"body": "green"}, {"body": "amber"},
            ]},
            {"method": "GET", "path": "/limited",
             "chaos": {"rate_limit": {"limit": 1, "window_ms": 60000}}, "response": {"body": 1}},
        ],
        "resources": [{"name": "todos", "path": "/todos", "seed": [{"id": 1, "title": "seeded"}]}],
    }
    data["config"].update(extra or {})
    return create_app(build_config(data))


@pytest.mark.asyncio
async def test_calls_land_in_the_journal_with_the_matched_route():
    async with make_client(_app()) as client:
        await client.post("/orders?source=web", json={"item": "Keyboard", "qty": 2})
        await client.get("/todos/1")
        await client.get("/nothing-here")

        r = await client.get("/__mock__/requests")
        journal = r.json()
        assert journal["count"] == 3
        order, todo, miss = journal["requests"]
        assert order["method"] == "POST" and order["path"] == "/orders"
        assert order["query"] == {"source": "web"}
        assert order["body"] == {"item": "Keyboard", "qty": 2}
        assert order["matched"] == {"type": "route", "name": "create-order"}
        assert order["status"] == 201
        assert order["headers"]["content-type"] == "application/json"
        assert order["id"] < todo["id"] < miss["id"]
        assert todo["matched"] == {"type": "resource", "name": "todos", "operation": "get"}
        assert miss["matched"] is None and miss["status"] == 404


@pytest.mark.asyncio
async def test_journal_filters():
    async with make_client(_app()) as client:
        await client.post("/orders", json={"a": 1})
        await client.post("/orders", json={"a": 2})
        await client.get("/todos")
        await client.get("/todos/1")

        by_route = (await client.get("/__mock__/requests", params={"route": "create-order"})).json()
        assert by_route["count"] == 2
        assert [e["body"]["a"] for e in by_route["requests"]] == [1, 2]

        by_method = (await client.get("/__mock__/requests", params={"method": "get"})).json()
        assert by_method["count"] == 2
        globbed = (await client.get("/__mock__/requests", params={"path": "/todos/*"})).json()
        assert [e["path"] for e in globbed["requests"]] == ["/todos/1"]
        exact = (await client.get("/__mock__/requests", params={"path": "/todos"})).json()
        assert exact["count"] == 1
        created = (await client.get("/__mock__/requests", params={"status": "201"})).json()
        assert created["count"] == 2
        newest = (await client.get("/__mock__/requests", params={"limit": "1"})).json()
        assert newest["requests"][0]["path"] == "/todos/1"
        since = (await client.get("/__mock__/requests", params={"since": by_route["requests"][1]["id"]})).json()
        assert since["count"] == 2


@pytest.mark.asyncio
async def test_admin_calls_are_not_journaled_and_journal_can_be_cleared():
    async with make_client(_app()) as client:
        await client.get("/todos")
        await client.get("/__mock__")
        await client.get("/__mock__/routes")
        assert (await client.get("/__mock__/requests")).json()["count"] == 1
        cleared = await client.delete("/__mock__/requests")
        assert cleared.json() == {"cleared": 1}
        assert (await client.get("/__mock__/requests")).json()["count"] == 0


@pytest.mark.asyncio
async def test_reset_restores_seed_data_sequences_and_chaos():
    async with make_client(_app()) as client:
        statuses_before = [(await client.get("/roll")).status_code for _ in range(12)]
        assert set(statuses_before) == {200, 503}
        await client.post("/todos", json={"title": "temp"})
        await client.delete("/todos/1")
        assert (await client.post("/orders", json={})).json()["id"] == 1000
        assert (await client.post("/orders", json={})).json()["id"] == 1001
        assert (await client.get("/limited")).status_code == 200
        assert (await client.get("/limited")).status_code == 429

        r = await client.post("/__mock__/reset")
        assert r.status_code == 200
        assert r.json()["resources"] == ["todos"]

        todos = (await client.get("/todos")).json()
        assert todos == [{"id": 1, "title": "seeded"}]
        assert (await client.post("/orders", json={})).json()["id"] == 1000
        assert (await client.get("/limited")).status_code == 200
        statuses_after = [(await client.get("/roll")).status_code for _ in range(12)]
        assert statuses_after == statuses_before
        # The journal only holds what happened after the reset.
        assert (await client.get("/__mock__/requests")).json()["count"] == 15


@pytest.mark.asyncio
async def test_stick_sequence_for_polling():
    async with make_client(_app()) as client:
        codes = []
        for _ in range(4):
            r = await client.get("/jobs/42")
            codes.append(r.status_code)
        assert codes == [202, 202, 200, 200]
        assert r.json() == {"state": "done", "id": "42"}
        assert r.headers["x-matched-route"] == "job"


@pytest.mark.asyncio
async def test_cycle_sequence_and_reset_rewinds_it():
    async with make_client(_app()) as client:
        colors = [(await client.get("/light")).text for _ in range(4)]
        assert colors == ["red", "green", "amber", "red"]
        await client.post("/__mock__/reset")
        assert (await client.get("/light")).text == "red"


@pytest.mark.asyncio
async def test_chaos_errors_do_not_advance_a_sequence():
    config = build_config({"config": {"seed": 3}, "routes": [{
        "name": "s", "path": "/s", "chaos": {"error_rate": 0.5, "error_status": 503},
        "responses": [{"body": 1}, {"body": 2}, {"body": 3}],
    }]})
    async with make_client(create_app(config)) as client:
        served = []
        for _ in range(20):
            r = await client.get("/s")
            if r.status_code == 200:
                served.append(r.json())
        assert served[:3] == [1, 2, 3]


@pytest.mark.asyncio
async def test_routes_listing_has_hits_and_sequence_state():
    async with make_client(_app()) as client:
        await client.get("/jobs/1")
        await client.get("/jobs/1")
        listing = (await client.get("/__mock__/routes")).json()
        job = next(r for r in listing["routes"] if r["name"] == "job")
        assert job["hits"] == 2 and job["responses"] == 3
        assert job["sequence"] == "stick" and job["next_response"] == 2
        assert listing["resources"] == [{"name": "todos", "path": "/todos", "items": 1}]


@pytest.mark.asyncio
async def test_index_lists_admin_endpoints_and_unknown_admin_paths_404():
    async with make_client(_app()) as client:
        index = (await client.get("/__mock__")).json()
        assert any("/__mock__/reset" in line for line in index["admin"])
        assert index["journal"]["enabled"] is True
        unknown = await client.get("/__mock__/nope")
        assert unknown.status_code == 404
        assert unknown.json()["error"] == "unknown_admin_endpoint"
        wrong = await client.get("/__mock__/reset")
        assert wrong.status_code == 405 and wrong.headers["allow"] == "POST"


@pytest.mark.asyncio
async def test_admin_prefix_and_journal_size_are_configurable():
    app = _app({"admin_prefix": "/_admin", "journal_size": 2})
    async with make_client(app) as client:
        for _ in range(3):
            await client.get("/todos")
        journal = (await client.get("/_admin/requests")).json()
        assert journal["count"] == 2
        # The old prefix is now an ordinary (unmatched) path.
        assert (await client.get("/__mock__")).status_code == 404
        assert (await client.post("/_admin/reset")).status_code == 200


@pytest.mark.asyncio
async def test_journal_can_be_disabled():
    async with make_client(_app({"journal_size": 0})) as client:
        await client.get("/todos")
        assert (await client.get("/__mock__/requests")).json()["count"] == 0


@pytest.mark.asyncio
async def test_journal_records_text_bodies_and_repeated_query_params():
    async with make_client(_app()) as client:
        await client.post("/orders?tag=a&tag=b", content=b"plain text", headers={"content-type": "text/plain"})
        entry = (await client.get("/__mock__/requests")).json()["requests"][0]
        assert entry["body"] == "plain text"
        assert entry["query"] == {"tag": ["a", "b"]}


@pytest.mark.asyncio
async def test_shop_example_polling_route():
    async with make_client(create_app(load_config(SHOP))) as client:
        codes = [(await client.get("/exports/7")).status_code for _ in range(4)]
        assert codes == [202, 202, 200, 200]
        done = await client.get("/exports/7")
        assert done.json()["url"].endswith("/exports/7.csv")


def test_sequence_and_admin_settings_are_validated():
    problems = validate_data({
        "config": {"admin_prefix": "/", "journal_size": -1},
        "routes": [
            {"path": "/a", "sequence": "loop", "responses": [{"status": 200}]},
            {"path": "/b", "response": {}, "responses": [{}]},
            {"path": "/c", "responses": []},
            {"path": "/d", "sequence": "cycle", "response": {}},
            {"path": "/e", "responses": [{"stauts": 200}]},
            {"path": "/__mock__/x", "response": {}},
        ],
    })
    text = [f"{p.level} {p.location}: {p.message}" for p in problems]
    assert any("config.admin_prefix" in t for t in text)
    assert any("config.journal_size" in t for t in text)
    assert any("routes[0].sequence" in t and "'loop'" in t for t in text)
    assert any("routes[1]" in t and "both 'response' and 'responses'" in t for t in text)
    assert any("routes[2].responses" in t and "non-empty" in t for t in text)
    assert any(t.startswith("warning routes[3].sequence") for t in text)
    assert any("routes[4].responses[0]" in t and "stauts" in t for t in text)
    assert any(t.startswith("warning routes[5].path") and "admin prefix" in t for t in text)
