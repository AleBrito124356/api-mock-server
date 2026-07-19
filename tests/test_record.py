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
