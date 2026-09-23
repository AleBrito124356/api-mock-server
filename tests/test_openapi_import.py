"""OpenAPI import turns a spec into servable routes."""
import os

import pytest

from mockserver import build_config, create_app
from mockserver.openapi_import import import_openapi
from tests.conftest import make_client

_SPEC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples",
    "openapi-import",
    "petstore.yaml",
)


def test_import_produces_routes_for_each_operation():
    config = import_openapi(_SPEC)
    routes = config["routes"]
    signatures = {(r["method"], r["path"]) for r in routes}
    assert ("GET", "/pets") in signatures
    assert ("POST", "/pets") in signatures
    assert ("GET", "/pets/{petId}") in signatures


def test_response_example_is_used_for_body():
    config = import_openapi(_SPEC)
    get_pets = next(r for r in config["routes"] if r["method"] == "GET" and r["path"] == "/pets")
    body = get_pets["response"]["body"]
    assert isinstance(body, list)
    assert body[0]["name"] == "Rex"


def test_schema_example_synthesized_when_no_response_example():
    config = import_openapi(_SPEC)
    post_pets = next(r for r in config["routes"] if r["method"] == "POST" and r["path"] == "/pets")
    body = post_pets["response"]["body"]
    # Built from the Pet schema examples.
    assert body["id"] == 7
    assert body["name"] == "Fido"
    # enum -> first value.
    assert body["status"] == "available"


@pytest.mark.asyncio
async def test_imported_config_is_servable():
    config = build_config(import_openapi(_SPEC), base_dir=os.path.dirname(_SPEC))
    async with make_client(create_app(config)) as client:
        r = await client.get("/pets")
        assert r.status_code == 200
        assert r.json()[0]["name"] == "Rex"

        one = await client.get("/pets/7")
        assert one.status_code == 200
        assert one.json()["name"] == "Fido"


# --------------------------------------------------------------------------- #
# Hardening: real-world spec shapes that used to crash or produce junk
# --------------------------------------------------------------------------- #
import re  # noqa: E402
import uuid  # noqa: E402

import yaml  # noqa: E402

from mockserver import cli  # noqa: E402
from mockserver.openapi_import import detect_base_path, string_from_pattern  # noqa: E402
from mockserver.validate import validate_data, validate_file  # noqa: E402


def _spec(tmp_path, paths, **extra):
    doc = {"openapi": "3.0.3", "info": {"title": "T", "version": "1"}, "paths": paths}
    doc.update(extra)
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return str(path)


def _schema_route(schema, code="200"):
    return {"/x": {"get": {"responses": {code: {
        "description": "ok", "content": {"application/json": {"schema": schema}},
    }}}}}


def _route(config, method="GET", path="/x"):
    return next(r for r in config["routes"] if r["method"] == method and r["path"] == path)


def test_unquoted_integer_status_keys(tmp_path):
    # YAML turns a bare 200: key into an int; this used to raise AttributeError.
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        "openapi: 3.0.0\ninfo: {title: t, version: '1'}\npaths:\n  /a:\n    get:\n      responses:\n"
        "        404: {description: missing}\n        200:\n          description: ok\n"
        "          content:\n            application/json:\n              example: {ok: true}\n",
        encoding="utf-8",
    )
    route = _route(import_openapi(str(spec)), path="/a")
    assert route["response"]["status"] == 200
    assert route["response"]["body"] == {"ok": True}


@pytest.mark.parametrize(
    "responses,method,status",
    [
        ({"2XX": {"description": "ok"}}, "get", 200),
        ({"2XX": {"description": "ok"}}, "post", 201),
        ({"default": {"description": "whatever"}}, "get", 200),
        ({"4XX": {"description": "bad"}, "204": {"description": "gone"}}, "delete", 204),
        ({"200": {"description": "a"}, "201": {"description": "b"}}, "post", 201),
        ({"200": {"description": "a"}, "201": {"description": "b"}}, "get", 200),
        ({"x-extension": {}, "500": {"description": "err"}}, "get", 500),
    ],
)
def test_status_key_normalization(tmp_path, responses, method, status):
    spec = _spec(tmp_path, {"/a": {method: {"responses": responses}}})
    route = _route(import_openapi(spec), method=method.upper(), path="/a")
    assert route["response"]["status"] == status


def test_schema_constraints_are_respected(tmp_path):
    schema = {"type": "object", "properties": {
        "code": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "qty": {"type": "integer", "minimum": 5},
        "exclusive": {"type": "integer", "minimum": 5, "exclusiveMinimum": True},
        "v31": {"type": "number", "exclusiveMinimum": 10},
        "neg": {"type": "integer", "maximum": -3},
        "even": {"type": "integer", "minimum": 3, "multipleOf": 2},
        "short": {"type": "string", "maxLength": 3},
        "long": {"type": "string", "minLength": 10},
        "maybe": {"type": ["string", "null"], "format": "email"},
        "tags": {"type": "array", "minItems": 2, "items": {"type": "string", "enum": ["a", "b"]}},
    }}
    body = _route(import_openapi(_spec(tmp_path, _schema_route(schema))))["response"]["body"]
    assert re.fullmatch(r"[A-Z]{3}", body["code"])
    assert body["qty"] == 5
    assert body["exclusive"] == 6
    assert body["v31"] > 10
    assert body["neg"] <= -3
    assert body["even"] >= 3 and body["even"] % 2 == 0
    assert len(body["short"]) <= 3
    assert len(body["long"]) >= 10
    assert "@" in body["maybe"]
    assert body["tags"] == ["a", "a"]


@pytest.mark.parametrize(
    "pattern",
    [r"^[A-Z]{3}$", r"^\d{4}-\d{2}-\d{2}$", r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$",
     r"^(ORD|INV)-[0-9]{6}$", r"^\+?[1-9]\d{7,14}$", r"[^0-9]+", r"^[a-f0-9]{8}(-[a-f0-9]{4}){3}$"],
)
def test_generated_strings_match_their_pattern(pattern):
    value = string_from_pattern(pattern)
    assert value is not None and re.search(pattern, value), value


def test_swagger2_basics(tmp_path):
    doc = {
        "swagger": "2.0",
        "info": {"title": "Legacy", "version": "1"},
        "basePath": "/api",
        "paths": {"/users/{id}": {"get": {
            "parameters": [{"name": "id", "in": "path", "type": "integer", "required": True}],
            "responses": {200: {"description": "ok", "schema": {"$ref": "#/definitions/User"}}},
        }}, "/users": {"get": {"responses": {"200": {
            "description": "ok",
            "examples": {"application/json": [{"id": 1, "email": "a@example.com"}]},
        }}}}},
        "definitions": {"User": {"type": "object", "properties": {
            "id": {"type": "integer", "example": 3}, "email": {"type": "string", "format": "email"},
        }}},
    }
    path = tmp_path / "swagger.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    config = import_openapi(str(path))
    assert _route(config, path="/users/{id}")["response"]["body"] == {"id": 3, "email": "user@example.com"}
    assert _route(config, path="/users")["response"]["body"] == [{"id": 1, "email": "a@example.com"}]
    assert detect_base_path(doc) == "/api"
    dynamic = import_openapi(str(path), base_path="auto", dynamic=True)
    assert _route(dynamic, path="/api/users/{id}")["response"]["body"]["id"] == "{{ request.path.id | int }}"


def test_non_openapi_documents_are_rejected(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text("hello: world\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Not an OpenAPI document"):
        import_openapi(str(path))


def test_base_path_options(tmp_path):
    spec = _spec(tmp_path, {"/a": {"get": {"responses": {"200": {"description": "ok"}}}}},
                 servers=[{"url": "https://{env}.example.com/{version}/", "variables": {
                     "env": {"default": "api"}, "version": {"default": "v2"}}}])
    assert _route(import_openapi(spec), path="/a")
    assert _route(import_openapi(spec, base_path="auto"), path="/v2/a")
    assert _route(import_openapi(spec, base_path="/custom/"), path="/custom/a")
    assert _route(import_openapi(spec, base_path="/"), path="/a")


def test_path_params_with_dashes_are_sanitized(tmp_path):
    spec = _spec(tmp_path, {"/pets/{pet-id}": {"get": {
        "parameters": [{"name": "pet-id", "in": "path", "required": True, "schema": {"type": "integer"}}],
        "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {
            "type": "object", "properties": {"id": {"type": "integer"}}}}}}},
    }}})
    config = import_openapi(spec, dynamic=True)
    route = config["routes"][0]
    assert route["path"] == "/pets/{pet_id}"
    assert route["response"]["body"]["id"] == "{{ request.path.pet_id | int }}"
    assert not [p for p in validate_data(config) if p.level == "error"]


def test_duplicate_operation_ids_get_unique_names(tmp_path):
    ok = {"200": {"description": "ok"}}
    spec = _spec(tmp_path, {
        "/a": {"get": {"operationId": "fetch", "responses": ok}},
        "/b": {"get": {"operationId": "fetch", "responses": ok}},
    })
    names = [r["name"] for r in import_openapi(spec)["routes"]]
    assert names == ["fetch", "fetch-2"]


# --------------------------------------------------------------------------- #
# --dynamic and --resources
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_dynamic_petstore_echoes_the_path_param():
    config = build_config(import_openapi(_SPEC, base_path="auto", dynamic=True))
    async with make_client(create_app(config)) as client:
        one = await client.get("/v1/pets/123")
        assert one.status_code == 200
        pet = one.json()
        assert pet["id"] == 123
        assert pet["status"] in ("available", "pending", "sold")
        assert isinstance(pet["name"], str) and pet["name"]

        listing = (await client.get("/v1/pets")).json()
        assert len(listing) == 3
        assert all(isinstance(p["id"], int) and 1 <= p["id"] <= 1000 for p in listing)

        created = await client.post("/v1/pets", json={"name": "Nemo", "tag": "fish"})
        assert created.status_code == 201
        assert created.json()["name"] == "Nemo" and created.json()["tag"] == "fish"
        again = await client.post("/v1/pets", json={"name": "Dory"})
        assert again.json()["id"] == created.json()["id"] + 1


@pytest.mark.asyncio
async def test_dynamic_formats_and_name_hints(tmp_path):
    schema = {"type": "object", "properties": {
        "user_id": {"type": "string", "format": "uuid"},
        "contact": {"type": "string", "format": "email"},
        "first_name": {"type": "string"},
        "city": {"type": "string"},
        "homepage": {"type": "string", "format": "uri"},
        "age": {"type": "integer", "minimum": 18, "maximum": 30},
        "balance": {"type": "number", "minimum": 0, "maximum": 50},
        "active": {"type": "boolean"},
        "role": {"type": "string", "enum": ["admin", "viewer"]},
        "sku": {"type": "string", "pattern": "^SKU-[0-9]{4}$"},
    }}
    config = import_openapi(_spec(tmp_path, _schema_route(schema)), dynamic=True)
    assert validate_data(config) == []
    async with make_client(create_app(build_config(config))) as client:
        for _ in range(5):
            body = (await client.get("/x")).json()
            assert uuid.UUID(body["user_id"]).version == 4
            assert re.fullmatch(r"[a-z]+\.[a-z]+@[a-z.]+", body["contact"])
            assert body["first_name"][0].isupper()
            assert body["homepage"].startswith("https://")
            assert 18 <= body["age"] <= 30
            assert 0 <= body["balance"] <= 50
            assert isinstance(body["active"], bool)
            assert body["role"] in ("admin", "viewer")
            assert re.fullmatch(r"SKU-[0-9]{4}", body["sku"])


@pytest.mark.asyncio
async def test_resources_mode_round_trips_crud():
    config = import_openapi(_SPEC, base_path="auto", resources=True)
    assert config["routes"] == []
    assert config["resources"][0]["name"] == "pets" and config["resources"][0]["path"] == "/v1/pets"
    async with make_client(create_app(build_config(config))) as client:
        seeded = (await client.get("/v1/pets")).json()
        assert [p["name"] for p in seeded] == ["Rex", "Whiskers"]
        created = await client.post("/v1/pets", json={"name": "Nemo", "tag": "fish"})
        assert created.status_code == 201
        pet_id = created.json()["id"]
        assert (await client.get(f"/v1/pets/{pet_id}")).json()["name"] == "Nemo"
        assert (await client.patch(f"/v1/pets/{pet_id}", json={"tag": "clownfish"})).json()["tag"] == "clownfish"
        assert (await client.delete(f"/v1/pets/{pet_id}")).status_code == 204


def test_resources_mode_keeps_unrelated_operations_and_generates_seed(tmp_path):
    item = {"type": "object", "properties": {"sku": {"type": "string", "format": "uuid"}, "name": {"type": "string"}}}
    ok = {"200": {"description": "ok", "content": {"application/json": {"schema": item}}}}
    spec = _spec(tmp_path, {
        "/items": {"get": {"responses": {"200": {"description": "ok", "content": {"application/json": {
            "schema": {"type": "array", "items": item}}}}}}},
        "/items/{sku}": {"get": {"responses": ok}, "delete": {"responses": {"204": {"description": "gone"}}}},
        "/items/{sku}/publish": {"post": {"responses": ok}},
        "/health": {"get": {"responses": ok}},
    })
    config = import_openapi(spec, resources=True)
    resource = config["resources"][0]
    assert (resource["id_field"], resource["id_type"]) == ("sku", "uuid")
    assert len(resource["seed"]) == 3 and all("sku" not in s for s in resource["seed"])
    assert {(r["method"], r["path"]) for r in config["routes"]} == {
        ("POST", "/items/{sku}/publish"), ("GET", "/health"),
    }
    assert [p for p in validate_data(config) if p.level == "error"] == []


def test_cli_import_with_all_flags_validates_the_output(tmp_path, capsys):
    out = tmp_path / "petstore.mocks.yaml"
    code = cli.main(["import-openapi", _SPEC, "-o", str(out), "--dynamic", "--resources"])
    assert code == 0
    text = capsys.readouterr().out
    assert "Imported 0 route(s) and 1 resource(s)" in text
    assert "prefixed with /v1" in text
    assert ": OK" in text
    assert validate_file(str(out)) == []
    assert out.read_text(encoding="utf-8").startswith("# Generated by: mockserver import-openapi")
    assert "&id" not in out.read_text(encoding="utf-8")


def test_cli_import_reports_bad_documents(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("just: yaml\n", encoding="utf-8")
    assert cli.main(["import-openapi", str(bad), "-o", str(tmp_path / "o.yaml")]) == 1
    assert "Not an OpenAPI document" in capsys.readouterr().err
