"""Record/replay: offline replay from fixtures and fixture serialization."""
import json
import os

import pytest

from mockserver import build_config, create_app
from mockserver.record import Recorder
from tests.conftest import make_client


def _write_fixture(directory, method, path, query, status, body):
    rec = Recorder(upstream=None, fixtures_dir=str(directory))
    key = Recorder._key(method, path, query)
    fixture = {
        "request": {"method": method, "path": path, "query": query},
        "response": {
            "status": status,
            "headers": {"content-type": "application/json"},
            "body": json.dumps(body),
            "body_encoding": "text",
        },
    }
    with open(os.path.join(str(directory), Recorder._filename(key)), "w", encoding="utf-8") as fh:
        json.dump(fixture, fh)


@pytest.mark.asyncio
async def test_replay_from_fixture_offline(tmp_path):
    _write_fixture(tmp_path, "GET", "/widgets", {}, 200, {"widgets": [1, 2, 3]})
    config = build_config(
        {"record": {"upstream": None, "fixtures_dir": str(tmp_path)}},
        base_dir=str(tmp_path),
    )
    async with make_client(create_app(config)) as client:
        r = await client.get("/widgets")
        assert r.status_code == 200
        assert r.json() == {"widgets": [1, 2, 3]}
        assert r.headers["x-mock-source"] == "replay"


@pytest.mark.asyncio
async def test_missing_fixture_offline_returns_404(tmp_path):
    config = build_config(
        {"record": {"upstream": None, "fixtures_dir": str(tmp_path)}},
        base_dir=str(tmp_path),
    )
    async with make_client(create_app(config)) as client:
        r = await client.get("/unknown")
        assert r.status_code == 404
        assert r.json()["error"] == "no_fixture"


def test_fixture_key_is_query_order_independent():
    a = Recorder._key("GET", "/x", {"a": "1", "b": "2"})
    b = Recorder._key("GET", "/x", {"b": "2", "a": "1"})
    assert a == b


def test_textual_body_stored_as_text_not_base64(tmp_path):
    rec = Recorder(upstream=None, fixtures_dir=str(tmp_path))
    fixture = rec._build_fixture(
        "GET", "/x", {}, 200, b'{"ok": true}', {"content-type": "application/json"}
    )
    assert fixture["response"]["body_encoding"] == "text"
    assert fixture["response"]["body"] == '{"ok": true}'


def test_binary_body_stored_as_base64(tmp_path):
    rec = Recorder(upstream=None, fixtures_dir=str(tmp_path))
    fixture = rec._build_fixture(
        "GET", "/x", {}, 200, b"\x89PNG\x0d\x0a", {"content-type": "image/png"}
    )
    assert fixture["response"]["body_encoding"] == "base64"


# --------------------------------------------------------------------------- #
# The real record -> upstream path, through an in-process fake upstream
# --------------------------------------------------------------------------- #
import httpx  # noqa: E402

from mockserver.record import body_hash  # noqa: E402


class FakeUpstream:
    """An httpx.MockTransport handler that echoes what it received."""

    def __init__(self):
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        payload = {
            "method": request.method,
            "path": request.url.path,
            "query": [list(pair) for pair in request.url.params.multi_items()],
            "body": json.loads(request.content) if request.content else None,
            "call": len(self.calls),
        }
        return httpx.Response(201 if request.method == "POST" else 200, json=payload)


def _record_app(tmp_path, upstream, record_block=None):
    block = {"upstream": "http://upstream.test", "fixtures_dir": str(tmp_path), "record": True}
    block.update(record_block or {})
    config = build_config({"record": block}, base_dir=str(tmp_path))
    return create_app(config, upstream_transport=httpx.MockTransport(upstream))


@pytest.mark.asyncio
async def test_record_proxies_saves_and_then_replays(tmp_path):
    upstream = FakeUpstream()
    async with make_client(_record_app(tmp_path, upstream)) as client:
        first = await client.get("/pets", params={"limit": "2"})
        assert first.status_code == 200
        assert first.headers["x-mock-source"] == "upstream"
        assert first.json()["path"] == "/pets"

        second = await client.get("/pets", params={"limit": "2"})
        assert second.headers["x-mock-source"] == "replay"
        assert second.json() == first.json()
        assert len(upstream.calls) == 1

    saved = [f for f in os.listdir(tmp_path) if f.endswith(".json")]
    assert len(saved) == 1
    with open(os.path.join(tmp_path, saved[0]), encoding="utf-8") as fh:
        fixture = json.load(fh)
    assert fixture["version"] == 2
    assert fixture["request"]["query"] == {"limit": "2"}


@pytest.mark.asyncio
async def test_different_post_bodies_get_different_fixtures(tmp_path):
    upstream = FakeUpstream()
    async with make_client(_record_app(tmp_path, upstream)) as client:
        a = await client.post("/pets", json={"name": "a"})
        b = await client.post("/pets", json={"name": "DIFFERENT"})
        assert a.headers["x-mock-source"] == "upstream"
        assert b.headers["x-mock-source"] == "upstream"
        assert b.json()["body"] == {"name": "DIFFERENT"}

        # Same payload with a different key order / whitespace replays.
        again = await client.post(
            "/pets", content=b'{ "name" : "a" }', headers={"content-type": "application/json"}
        )
        assert again.headers["x-mock-source"] == "replay"
        assert again.json()["body"] == {"name": "a"}
    assert len(upstream.calls) == 2
    assert len([f for f in os.listdir(tmp_path) if f.endswith(".json")]) == 2


@pytest.mark.asyncio
async def test_repeated_query_params_are_kept_and_forwarded(tmp_path):
    upstream = FakeUpstream()
    async with make_client(_record_app(tmp_path, upstream)) as client:
        r = await client.get("/search?tag=a&tag=b")
        assert r.json()["query"] == [["tag", "a"], ["tag", "b"]]
        only_b = await client.get("/search?tag=b")
        assert only_b.headers["x-mock-source"] == "upstream"
        # Query order does not matter for replay.
        swapped = await client.get("/search?tag=b&tag=a")
        assert swapped.headers["x-mock-source"] == "replay"


def test_body_hash_is_canonical_for_json():
    assert body_hash(b'{"a":1,"b":2}') == body_hash(b'{ "b": 2, "a": 1 }')
    assert body_hash(b'{"a":1}') != body_hash(b'{"a":2}')
    assert body_hash(b"") is None
    assert Recorder._key("POST", "/search", {}, body_hash(b'{"q":1}')) != Recorder._key("POST", "/search", {})


@pytest.mark.asyncio
async def test_legacy_fixture_without_body_hash_still_replays(tmp_path):
    # A 0.1.x fixture for a POST: no body_sha1 recorded.
    _write_fixture(tmp_path, "POST", "/login", {}, 200, {"token": "legacy"})
    config = build_config({"record": {"upstream": None, "fixtures_dir": str(tmp_path)}}, base_dir=str(tmp_path))
    async with make_client(create_app(config)) as client:
        r = await client.post("/login", json={"user": "ada"})
        assert r.status_code == 200
        assert r.json() == {"token": "legacy"}
        assert r.headers["x-mock-source"] == "replay"


@pytest.mark.asyncio
async def test_index_reachable_in_record_mode(tmp_path):
    upstream = FakeUpstream()
    async with make_client(_record_app(tmp_path, upstream)) as client:
        r = await client.get("/__mock__")
        assert r.status_code == 200
        assert r.json()["record"] is True
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_unreachable_upstream_is_502_json(tmp_path):
    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)

    config = build_config(
        {"record": {"upstream": "http://down.test", "fixtures_dir": str(tmp_path)}}, base_dir=str(tmp_path)
    )
    async with make_client(create_app(config, upstream_transport=httpx.MockTransport(boom))) as client:
        r = await client.get("/anything")
        assert r.status_code == 502
        assert r.json()["error"] == "upstream_error"
        assert "ConnectError" in r.json()["detail"]


@pytest.mark.asyncio
async def test_record_false_proxies_without_saving(tmp_path):
    upstream = FakeUpstream()
    async with make_client(_record_app(tmp_path, upstream, {"record": False})) as client:
        await client.get("/x")
        await client.get("/x")
    assert len(upstream.calls) == 2
    assert [f for f in os.listdir(tmp_path) if f.endswith(".json")] == []
