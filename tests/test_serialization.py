"""Response bodies: scalars are JSON, errors are explained, never opaque."""
import pytest

from mockserver import build_config, create_app
from tests.conftest import make_client


def _app(routes):
    return create_app(build_config({"config": {"seed": 1}, "routes": routes}))


@pytest.mark.asyncio
async def test_boolean_body_is_json_not_python_repr():
    app = _app([{"method": "GET", "path": "/flag", "response": {"body": "{{ faker.bool }}"}}])
    async with make_client(app) as client:
        r = await client.get("/flag")
        assert r.headers["content-type"] == "application/json"
        assert r.text in ("true", "false")
        assert isinstance(r.json(), bool)


@pytest.mark.asyncio
async def test_number_and_literal_false_bodies_are_json():
    app = _app([
        {"method": "GET", "path": "/n", "response": {"body": 42}},
        {"method": "GET", "path": "/f", "response": {"body": False}},
        {"method": "GET", "path": "/x", "response": {"body": 1.5}},
    ])
    async with make_client(app) as client:
        n = await client.get("/n")
        assert n.headers["content-type"] == "application/json" and n.json() == 42
        f = await client.get("/f")
        assert f.text == "false"
        x = await client.get("/x")
        assert x.json() == 1.5


@pytest.mark.asyncio
async def test_template_rendering_to_none_sends_json_null():
    app = _app([{"method": "GET", "path": "/maybe", "response": {"body": "{{ request.query.missing }}"}}])
    async with make_client(app) as client:
        r = await client.get("/maybe")
        assert r.text == "null"
        assert r.headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_no_body_declared_means_empty_body():
    app = _app([{"method": "DELETE", "path": "/thing", "response": {"status": 204}}])
    async with make_client(app) as client:
        r = await client.delete("/thing")
        assert r.status_code == 204 and r.content == b""


@pytest.mark.asyncio
async def test_plain_string_stays_text_unless_json_declared():
    app = _app([
        {"method": "GET", "path": "/text", "response": {"body": "hello"}},
        {
            "method": "GET",
            "path": "/json-string",
            "response": {"headers": {"Content-Type": "application/json"}, "body": "hello"},
        },
        {
            "method": "GET",
            "path": "/raw-json",
            "response": {"headers": {"Content-Type": "application/json"}, "body": '{"raw": true}'},
        },
    ])
    async with make_client(app) as client:
        text = await client.get("/text")
        assert text.headers["content-type"].startswith("text/plain") and text.text == "hello"
        js = await client.get("/json-string")
        assert js.json() == "hello"
        raw = await client.get("/raw-json")
        assert raw.json() == {"raw": True}


@pytest.mark.asyncio
async def test_templated_status_code():
    app = _app([{
        "method": "GET",
        "path": "/status",
        "response": {"status": "{{ request.query.code | default(200) | int }}", "body": {"ok": True}},
    }])
    async with make_client(app) as client:
        assert (await client.get("/status")).status_code == 200
        assert (await client.get("/status", params={"code": "503"})).status_code == 503
        bad = await client.get("/status", params={"code": "nope"})
        assert bad.status_code == 500 and bad.json()["error"] == "mock_error"


@pytest.mark.asyncio
async def test_bad_template_returns_json_500_naming_the_route():
    app = _app([{
        "method": "GET",
        "path": "/bad",
        "name": "bad-route",
        "response": {"body": {"n": "{{ faker.int(a, b) }}"}},
    }])
    async with make_client(app) as client:
        r = await client.get("/bad")
        assert r.status_code == 500
        body = r.json()
        assert body["error"] == "mock_error"
        assert body["route"] == "bad-route"
        assert body["expression"] == "faker.int(a, b)"
        assert "faker.int" in body["detail"]


@pytest.mark.asyncio
async def test_unnamed_route_error_uses_method_and_path():
    app = _app([{"method": "GET", "path": "/typo", "response": {"body": "{{ faker.nmae }}"}}])
    async with make_client(app) as client:
        r = await client.get("/typo")
        assert r.status_code == 500
        assert r.json()["route"] == "GET /typo"
        assert "nmae" in r.json()["detail"]


@pytest.mark.asyncio
async def test_missing_body_file_is_a_json_error(tmp_path):
    config = build_config(
        {"routes": [{"method": "GET", "path": "/f", "response": {"file": "nope.json"}}]},
        base_dir=str(tmp_path),
    )
    async with make_client(create_app(config)) as client:
        r = await client.get("/f")
        assert r.status_code == 500
        assert r.json()["type"] == "FileNotFoundError"


@pytest.mark.asyncio
async def test_query_match_on_yaml_boolean():
    app = _app([
        {"method": "GET", "path": "/items", "match": {"query": {"archived": True}}, "response": {"body": "archived"}},
        {"method": "GET", "path": "/items", "response": {"body": "live"}},
    ])
    async with make_client(app) as client:
        assert (await client.get("/items", params={"archived": "true"})).text == "archived"
        assert (await client.get("/items")).text == "live"
