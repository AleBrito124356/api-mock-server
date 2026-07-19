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
