"""serve --watch: edits apply on the next request without losing state."""
import os
import textwrap

import pytest

from mockserver import create_app, load_config
from tests.conftest import make_client

BASE = """
config:
  seed: 1
routes:
  - name: hello
    method: GET
    path: /hello
    response:
      body: {msg: "%s"}
  - method: GET
    path: /count
    response:
      body: {n: "{{ seq.calls }}"}
resources:
  - name: notes
    path: /notes
    seed:
      - {id: 1, text: first}
"""


def _write(path, text):
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    # Guarantee a visible mtime change even on coarse filesystem clocks.
    stat = os.stat(path)
    bump = getattr(_write, "bump", 0) + 1
    _write.bump = bump
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + bump * 1_000_000_000))


def _app(path):
    return create_app(load_config(str(path)), watch=True, watch_interval=0)


@pytest.mark.asyncio
async def test_edit_is_served_on_next_request(tmp_path):
    cfg = tmp_path / "mocks.yaml"
    _write(cfg, BASE % "v1")
    async with make_client(_app(cfg)) as client:
        assert (await client.get("/hello")).json() == {"msg": "v1"}
        _write(cfg, BASE % "v2")
        assert (await client.get("/hello")).json() == {"msg": "v2"}
        index = (await client.get("/__mock__")).json()
        assert index["watch"] == {"enabled": True, "reloads": 1, "errors": []}


@pytest.mark.asyncio
async def test_invalid_edit_keeps_the_last_good_config(tmp_path):
    cfg = tmp_path / "mocks.yaml"
    _write(cfg, BASE % "good")
    async with make_client(_app(cfg)) as client:
        assert (await client.get("/hello")).json() == {"msg": "good"}
        _write(cfg, (BASE % "broken").replace("response:", "respnse:", 1))
        r = await client.get("/hello")
        assert r.json() == {"msg": "good"}
        errors = (await client.get("/__mock__")).json()["watch"]["errors"]
        assert any("respnse" in e for e in errors)

        # YAML that does not even parse is also rejected.
        _write(cfg, "routes: [ {path: /x\n")
        assert (await client.get("/hello")).json() == {"msg": "good"}

        # Fixing the file recovers.
        _write(cfg, BASE % "fixed")
        assert (await client.get("/hello")).json() == {"msg": "fixed"}
        assert (await client.get("/__mock__")).json()["watch"]["errors"] == []


@pytest.mark.asyncio
async def test_resource_data_and_sequences_survive_a_route_edit(tmp_path):
    cfg = tmp_path / "mocks.yaml"
    _write(cfg, BASE % "v1")
    async with make_client(_app(cfg)) as client:
        await client.post("/notes", json={"text": "added before reload"})
        assert (await client.get("/count")).json() == {"n": 1}
        _write(cfg, BASE % "v2")
        assert (await client.get("/hello")).json() == {"msg": "v2"}
        notes = (await client.get("/notes")).json()
        assert [n["text"] for n in notes] == ["first", "added before reload"]
        assert (await client.get("/count")).json() == {"n": 2}


@pytest.mark.asyncio
async def test_changed_resource_definition_resets_that_resource(tmp_path):
    cfg = tmp_path / "mocks.yaml"
    _write(cfg, BASE % "v1")
    async with make_client(_app(cfg)) as client:
        await client.post("/notes", json={"text": "temp"})
        _write(cfg, (BASE % "v1").replace("{id: 1, text: first}", "{id: 1, text: reseeded}"))
        notes = (await client.get("/notes")).json()
        assert [n["text"] for n in notes] == ["reseeded"]


@pytest.mark.asyncio
async def test_body_file_edits_are_watched(tmp_path):
    body = tmp_path / "body.json"
    body.write_text('{"v": 1}', encoding="utf-8")
    cfg = tmp_path / "mocks.yaml"
    _write(cfg, "routes:\n  - path: /f\n    response: {file: body.json}\n")
    async with make_client(_app(cfg)) as client:
        assert (await client.get("/f")).json() == {"v": 1}
        # A broken body file is rejected by validation on reload...
        _write(body, "{nope")
        r = await client.get("/__mock__")
        assert any("not valid JSON" in e for e in r.json()["watch"]["errors"])
        _write(body, '{"v": 2}')
        assert (await client.get("/f")).json() == {"v": 2}


@pytest.mark.asyncio
async def test_cli_seed_override_survives_reload(tmp_path):
    cfg = tmp_path / "mocks.yaml"
    _write(cfg, BASE % "v1")
    config = load_config(str(cfg))
    config.set_seed(99)
    app = create_app(config, watch=True, watch_interval=0)
    async with make_client(app) as client:
        _write(cfg, BASE % "v2")
        assert (await client.get("/hello")).json() == {"msg": "v2"}
        assert app.state.mock_server.config.seed == 99


@pytest.mark.asyncio
async def test_throttle_skips_checks_between_intervals(tmp_path):
    cfg = tmp_path / "mocks.yaml"
    _write(cfg, BASE % "v1")
    app = create_app(load_config(str(cfg)), watch=True, watch_interval=3600)
    async with make_client(app) as client:
        _write(cfg, BASE % "v2")
        assert (await client.get("/hello")).json() == {"msg": "v1"}


def test_watch_requires_a_file_backed_config():
    from mockserver import build_config

    with pytest.raises(ValueError):
        create_app(build_config({}), watch=True)
