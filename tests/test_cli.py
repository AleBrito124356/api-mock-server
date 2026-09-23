"""CLI: .env defaults and their precedence, replay mode, record guards, --version."""
import json
import os

import pytest

from mockserver import cli
from mockserver.cli import EnvDefaults, build_parser, parse_dotenv, resolve_env
from mockserver.record import Recorder
from tests.conftest import make_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODO_EXAMPLE = os.path.join(ROOT, "examples", "todo-api", "mocks.yaml")


@pytest.fixture
def captured_run(monkeypatch):
    """Replace uvicorn with a recorder of what would have been served."""
    calls = []

    def fake_run(app, host, port):
        calls.append({"app": app, "host": host, "port": port})

    monkeypatch.setattr(cli, "_run", fake_run)
    return calls


def test_parse_dotenv_handles_common_syntax():
    text = """
# comment
MOCK_PORT=9001
export MOCK_HOST=0.0.0.0
MOCK_SEED = 42   # inline comment
MOCK_CONFIG="examples/a b.yaml"
MOCK_UPSTREAM='https://api.example.com/#frag'
MOCK_FIXTURES=
not a line
1BAD=x
"""
    values = parse_dotenv(text)
    assert values["MOCK_PORT"] == "9001"
    assert values["MOCK_HOST"] == "0.0.0.0"
    assert values["MOCK_SEED"] == "42"
    assert values["MOCK_CONFIG"] == "examples/a b.yaml"
    assert values["MOCK_UPSTREAM"] == "https://api.example.com/#frag"
    assert values["MOCK_FIXTURES"] == ""
    assert "1BAD" not in values


def test_precedence_flag_over_env_over_dotenv_over_default(tmp_path):
    (tmp_path / ".env").write_text("MOCK_PORT=9001\nMOCK_HOST=0.0.0.0\nMOCK_SEED=5\n", encoding="utf-8")

    # .env beats the built-in default.
    env = resolve_env(["serve"], environ={}, cwd=str(tmp_path))
    args = build_parser(env).parse_args(["serve"])
    assert (args.port, args.host, args.seed) == (9001, "0.0.0.0", 5)

    # A real environment variable beats .env.
    env = resolve_env(["serve"], environ={"MOCK_PORT": "9002"}, cwd=str(tmp_path))
    args = build_parser(env).parse_args(["serve"])
    assert args.port == 9002 and args.host == "0.0.0.0"

    # An explicit flag beats both.
    args = build_parser(env).parse_args(["serve", "--port", "9003"])
    assert args.port == 9003

    # No .env, no env: built-in defaults.
    empty = tmp_path / "empty"
    empty.mkdir()
    args = build_parser(resolve_env(["serve"], environ={}, cwd=str(empty))).parse_args(["serve"])
    assert (args.port, args.host, args.seed, args.config) == (8000, "127.0.0.1", None, "mocks.yaml")


def test_blank_values_fall_back_to_defaults():
    env = EnvDefaults({"MOCK_PORT": ""}, {"MOCK_HOST": "  "})
    args = build_parser(env).parse_args(["serve"])
    assert (args.port, args.host) == (8000, "127.0.0.1")


def test_env_file_flag_and_bad_integer(tmp_path, capsys):
    custom = tmp_path / "ci.env"
    custom.write_text("MOCK_PORT=7777\n", encoding="utf-8")
    env = resolve_env(["--env-file", str(custom), "serve"], environ={}, cwd=str(tmp_path))
    assert build_parser(env).parse_args(["serve"]).port == 7777

    with pytest.raises(SystemExit):
        resolve_env(["--env-file", str(tmp_path / "missing.env")], environ={}, cwd=str(tmp_path))

    with pytest.raises(SystemExit) as err:
        build_parser(EnvDefaults({"MOCK_PORT": "eighty"}, {}))
    assert "MOCK_PORT" in str(err.value)


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as err:
        cli.main(["--version"])
    assert err.value.code == 0
    assert "api-mock-server 0.2.0" in capsys.readouterr().out


def test_serve_uses_mock_config_from_dotenv(tmp_path, monkeypatch, captured_run):
    (tmp_path / ".env").write_text(f"MOCK_CONFIG={TODO_EXAMPLE}\nMOCK_PORT=8123\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for var in cli.ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["serve"]) == 0
    served = captured_run[0]
    assert served["port"] == 8123
    assert [r.name for r in served["app"].state.mock_server.config.resources] == ["todos"]


def test_serve_missing_config_fails(tmp_path, monkeypatch, capsys, captured_run):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["serve", "--config", "nope.yaml"]) == 1
    assert "Config file not found" in capsys.readouterr().err
    assert captured_run == []


def test_record_without_upstream_points_to_replay(tmp_path, monkeypatch, capsys, captured_run):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MOCK_UPSTREAM", raising=False)
    assert cli.main(["record", "--fixtures", "fx"]) == 2
    err = capsys.readouterr().err
    assert "MOCK_UPSTREAM" in err and "mockserver replay" in err
    assert captured_run == []


def test_record_upstream_from_env(tmp_path, monkeypatch, captured_run):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MOCK_UPSTREAM", "http://upstream.test")
    assert cli.main(["record", "--fixtures", "fx"]) == 0
    recorder = captured_run[0]["app"].state.mock_server.recorder
    assert recorder.upstream == "http://upstream.test"
    # CLI paths are relative to the working directory, not the config file.
    assert recorder.fixtures_dir == str(tmp_path / "fx")


def _save_fixture(directory, method, path, body):
    rec = Recorder(upstream=None, fixtures_dir=str(directory))
    fixture = rec._build_fixture(method, path, {}, 200, json.dumps(body).encode(), {"content-type": "application/json"})
    rec._save(Recorder._key(method, path, {}), fixture)


@pytest.mark.asyncio
async def test_replay_command_serves_fixtures_offline(tmp_path, monkeypatch, captured_run):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    _save_fixture(fixtures, "GET", "/users", [{"id": 1}])
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MOCK_CONFIG", raising=False)
    assert cli.main(["replay", "--fixtures", "fixtures", "--port", "8124"]) == 0
    app = captured_run[0]["app"]
    assert app.state.mock_server.recorder.upstream is None
    async with make_client(app) as client:
        hit = await client.get("/users")
        assert hit.json() == [{"id": 1}] and hit.headers["x-mock-source"] == "replay"
        miss = await client.get("/other")
        assert miss.status_code == 404 and miss.json()["error"] == "no_fixture"


@pytest.mark.asyncio
async def test_replay_combines_with_a_config(tmp_path, monkeypatch, captured_run):
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    _save_fixture(fixtures, "GET", "/legacy", {"from": "fixture"})
    monkeypatch.chdir(tmp_path)
    assert cli.main(["replay", "--fixtures", "fx", "--config", TODO_EXAMPLE]) == 0
    async with make_client(captured_run[0]["app"]) as client:
        assert (await client.get("/todos")).status_code == 200
        assert (await client.get("/legacy")).json() == {"from": "fixture"}


def test_replay_errors(tmp_path, monkeypatch, capsys, captured_run):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["replay", "--fixtures", "missing"]) == 1
    assert "Fixtures directory not found" in capsys.readouterr().err
    (tmp_path / "fx").mkdir()
    assert cli.main(["replay", "--fixtures", "fx", "--config", "nope.yaml"]) == 1
    assert "Config file not found" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_serve_with_fixtures_adds_offline_replay(tmp_path, monkeypatch, captured_run):
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    _save_fixture(fixtures, "GET", "/recorded", {"ok": True})
    monkeypatch.chdir(tmp_path)
    assert cli.main(["serve", "--config", TODO_EXAMPLE, "--fixtures", "fx"]) == 0
    async with make_client(captured_run[0]["app"]) as client:
        assert (await client.get("/todos/1")).json()["id"] == 1
        assert (await client.get("/recorded")).json() == {"ok": True}
