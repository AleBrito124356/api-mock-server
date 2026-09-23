"""Stateful CRUD lifecycle for an in-memory resource."""
import pytest

from mockserver import build_config, create_app
from tests.conftest import make_client


def _app():
    config = build_config(
        {
            "config": {"seed": 1, "cors": True},
            "resources": [
                {
                    "name": "todos",
                    "path": "/todos",
                    "id_field": "id",
                    "id_type": "int",
                    "seed": [
                        {"id": 1, "title": "First", "done": False},
                        {"id": 2, "title": "Second", "done": True},
                    ],
                }
            ],
        }
    )
    return create_app(config)


@pytest.mark.asyncio
async def test_full_crud_lifecycle():
    async with make_client(_app()) as client:
        # LIST
        r = await client.get("/todos")
        assert r.status_code == 200
        assert r.headers["x-total-count"] == "2"
        assert len(r.json()) == 2

        # GET one
        one = await client.get("/todos/1")
        assert one.status_code == 200
        assert one.json()["title"] == "First"

        # GET missing
        missing = await client.get("/todos/999")
        assert missing.status_code == 404

        # CREATE - id auto-assigned, continues past seeded max.
        created = await client.post("/todos", json={"title": "Third", "done": False})
        assert created.status_code == 201
        new_id = created.json()["id"]
        assert new_id == 3
        assert created.headers["location"] == "/todos/3"

        # It is now retrievable.
        got = await client.get(f"/todos/{new_id}")
        assert got.json()["title"] == "Third"

        # REPLACE (PUT)
        replaced = await client.put(f"/todos/{new_id}", json={"title": "Replaced", "done": True})
        assert replaced.status_code == 200
        assert replaced.json() == {"id": 3, "title": "Replaced", "done": True}

        # UPDATE (PATCH) - partial, keeps other fields.
        patched = await client.patch(f"/todos/{new_id}", json={"done": False})
        assert patched.status_code == 200
        assert patched.json() == {"id": 3, "title": "Replaced", "done": False}

        # DELETE
        deleted = await client.delete(f"/todos/{new_id}")
        assert deleted.status_code == 204

        # Gone now.
        gone = await client.get(f"/todos/{new_id}")
        assert gone.status_code == 404

        # PUT/PATCH/DELETE on a missing id -> 404.
        assert (await client.put("/todos/999", json={"x": 1})).status_code == 404
        assert (await client.patch("/todos/999", json={"x": 1})).status_code == 404
        assert (await client.delete("/todos/999")).status_code == 404


@pytest.mark.asyncio
async def test_list_filter_sort_and_paginate():
    async with make_client(_app()) as client:
        # Filter by field.
        done = await client.get("/todos", params={"done": "true"})
        assert [t["id"] for t in done.json()] == [2]

        # Sort desc by id.
        srt = await client.get("/todos", params={"_sort": "id", "_order": "desc"})
        assert [t["id"] for t in srt.json()] == [2, 1]

        # Limit.
        limited = await client.get("/todos", params={"_limit": "1"})
        assert len(limited.json()) == 1
        # Total count header still reflects the full match set.
        assert limited.headers["x-total-count"] == "2"


@pytest.mark.asyncio
async def test_uuid_ids():
    config = build_config(
        {
            "resources": [
                {"name": "sessions", "path": "/sessions", "id_type": "uuid"}
            ]
        }
    )
    async with make_client(create_app(config)) as client:
        created = await client.post("/sessions", json={"user": "ada"})
        assert created.status_code == 201
        sid = created.json()["id"]
        assert isinstance(sid, str) and len(sid) == 36
        got = await client.get(f"/sessions/{sid}")
        assert got.status_code == 200
        assert got.json()["user"] == "ada"


# --------------------------------------------------------------------------- #
# Regressions: bad bodies are 400s, duplicate ids are 409s
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_array_body_is_400_not_500():
    async with make_client(_app()) as client:
        r = await client.post("/todos", json=[1, 2, 3])
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_body"
        assert "array" in r.json()["message"]
        # The store is untouched.
        assert (await client.get("/todos")).headers["x-total-count"] == "2"


@pytest.mark.asyncio
async def test_invalid_json_is_400_for_every_write_method():
    async with make_client(_app()) as client:
        headers = {"content-type": "application/json"}
        assert (await client.post("/todos", content=b"{nope", headers=headers)).status_code == 400
        assert (await client.put("/todos/1", content=b"{nope", headers=headers)).status_code == 400
        r = await client.patch("/todos/1", content=b'"just a string"', headers=headers)
        assert r.status_code == 400 and r.json()["error"] == "invalid_body"


@pytest.mark.asyncio
async def test_post_with_existing_id_is_409_and_does_not_overwrite():
    async with make_client(_app()) as client:
        r = await client.post("/todos", json={"id": 1, "title": "OVERWRITTEN"})
        assert r.status_code == 409
        assert r.json()["error"] == "conflict"
        assert (await client.get("/todos/1")).json()["title"] == "First"


@pytest.mark.asyncio
async def test_post_with_new_explicit_id_is_honoured():
    async with make_client(_app()) as client:
        r = await client.post("/todos", json={"id": 10, "title": "Ten"})
        assert r.status_code == 201 and r.headers["location"] == "/todos/10"
        nxt = await client.post("/todos", json={"title": "Eleven"})
        assert nxt.json()["id"] == 11


@pytest.mark.asyncio
async def test_unsupported_method_is_405_with_allow():
    async with make_client(_app()) as client:
        r = await client.delete("/todos")
        assert r.status_code == 405
        assert "POST" in r.headers["allow"]
