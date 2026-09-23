"""The bundled examples load, and every README flow works against them."""
import glob
import os
import time
import uuid

import pytest

from mockserver import create_app, load_config
from tests.conftest import make_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = sorted(glob.glob(os.path.join(ROOT, "examples", "**", "mocks.yaml"), recursive=True))
SHOP = os.path.join(ROOT, "examples", "shop-api", "mocks.yaml")
TODO = os.path.join(ROOT, "examples", "todo-api", "mocks.yaml")


def shop_app(seed=None):
    config = load_config(SHOP)
    if seed is not None:
        config.settings["seed"] = seed
    return create_app(config)


def test_examples_exist_and_load():
    names = {os.path.basename(os.path.dirname(p)) for p in EXAMPLES}
    assert {"shop-api", "todo-api"} <= names
    for path in EXAMPLES:
        config = load_config(path)
        assert config.routes or config.resources, path
        create_app(config)


@pytest.mark.asyncio
async def test_products_list_is_templated_and_typed():
    async with make_client(shop_app()) as client:
        r = await client.get("/products")
        assert r.status_code == 200
        body = r.json()
        assert body["page"] == 1 and len(body["results"]) == 3
        first = body["results"][0]
        assert set(first) == {"id", "name", "price", "in_stock", "sku"}
        assert isinstance(first["id"], int) and isinstance(first["price"], float)
        assert isinstance(first["in_stock"], bool)
        assert r.headers["x-matched-route"] == "products-list"
        assert (await client.get("/products", params={"page": "3"})).json()["page"] == 3


@pytest.mark.asyncio
async def test_books_route_wins_by_priority():
    async with make_client(shop_app()) as client:
        r = await client.get("/products", params={"category": "books"})
        assert r.headers["x-matched-route"] == "products-books"
        assert r.json()["category"] == "books"


@pytest.mark.asyncio
async def test_product_detail_id_is_an_int():
    async with make_client(shop_app()) as client:
        r = await client.get("/products/42")
        assert r.json()["id"] == 42


@pytest.mark.asyncio
async def test_orders_echo_body_and_mint_ids():
    async with make_client(shop_app()) as client:
        a = await client.post("/orders", json={"item": "Keyboard", "qty": 2})
        assert a.status_code == 201
        assert {k: a.json()[k] for k in ("id", "status", "item", "qty", "customer")} == {
            "id": 1000, "status": "pending", "item": "Keyboard", "qty": 2, "customer": "guest",
        }
        b = await client.post("/orders", json={"item": "Mouse"})
        assert b.json()["id"] == 1001 and b.json()["qty"] == 1

        invalid = await client.post("/orders", json={"item": "Desk", "qty": 0})
        assert invalid.status_code == 422
        assert invalid.headers["x-matched-route"] == "create-order-invalid"


@pytest.mark.asyncio
async def test_me_requires_an_authorization_header():
    async with make_client(shop_app()) as client:
        assert (await client.get("/me")).status_code == 401
        ok = await client.get("/me", headers={"Authorization": "Bearer x"})
        assert ok.status_code == 200 and "@" in ok.json()["email"]


@pytest.mark.asyncio
async def test_flaky_is_a_seeded_mix_of_503_and_200():
    async def statuses():
        async with make_client(shop_app()) as client:
            return [(await client.get("/flaky")).status_code for _ in range(20)]

    first, second = await statuses(), await statuses()
    assert first == second
    assert set(first) == {200, 503}


@pytest.mark.asyncio
async def test_search_is_rate_limited():
    async with make_client(shop_app()) as client:
        codes = [(await client.get("/search", params={"q": "lamp"})).status_code for _ in range(6)]
        assert codes == [200] * 5 + [429]
        blocked = await client.get("/search")
        assert blocked.headers["retry-after"] == "10"


@pytest.mark.asyncio
async def test_health_body_comes_from_file():
    async with make_client(shop_app()) as client:
        r = await client.get("/health")
        body = r.json()
        assert body["status"] == "ok" and body["service"] == "shop-api-mock"
        uuid.UUID(body["uptime_check_id"])


@pytest.mark.asyncio
async def test_recommendations_are_slow_on_purpose():
    async with make_client(shop_app()) as client:
        start = time.perf_counter()
        r = await client.get("/recommendations")
        assert time.perf_counter() - start >= 0.14
        assert len(r.json()) == 2


@pytest.mark.asyncio
async def test_shop_customers_resource():
    async with make_client(shop_app()) as client:
        gold = await client.get("/customers", params={"tier": "gold"})
        assert [c["id"] for c in gold.json()] == [1, 3]
        created = await client.post("/customers", json={"name": "New", "tier": "bronze"})
        assert created.json()["id"] == 4


@pytest.mark.asyncio
async def test_todo_readme_flow():
    async with make_client(create_app(load_config(TODO))) as client:
        created = await client.post("/todos", json={"title": "Wire up the API", "done": False})
        assert created.status_code == 201
        assert created.json() == {"title": "Wire up the API", "done": False, "id": 4}
        assert created.headers["location"] == "/todos/4"

        page = await client.get("/todos", params={"done": "false", "_sort": "id", "_limit": "2"})
        assert [t["id"] for t in page.json()] == [2, 3]
        assert page.headers["x-total-count"] == "3"

        patched = await client.patch("/todos/4", json={"done": True})
        assert patched.json()["done"] is True

        deleted = await client.delete("/todos/4")
        assert deleted.status_code == 204
        assert (await client.get("/todos/4")).status_code == 404
