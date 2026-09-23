"""Config validation: typos are reported with locations and suggestions."""
import glob
import os

import pytest
import yaml

from mockserver import cli, load_config
from mockserver.openapi_import import import_openapi
from mockserver.validate import ConfigError, has_errors, validate_data, validate_file, validate_text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = sorted(glob.glob(os.path.join(ROOT, "examples", "**", "mocks.yaml"), recursive=True))
PETSTORE = os.path.join(ROOT, "examples", "openapi-import", "petstore.yaml")


def _messages(problems, level=None):
    return [f"{p.location}: {p.message}" for p in problems if level is None or p.level == level]


def _find(problems, location, text):
    return any(p.location == location and text in p.message for p in problems)


def test_typod_keys_get_did_you_mean_at_the_right_location():
    problems = validate_data({
        "routes": [{"methd": "POST", "path": "/x", "respnse": {"status": 201, "body": {"a": 1}}}],
    })
    assert _find(problems, "routes[0]", "unknown key 'respnse' (did you mean 'response'?)")
    assert _find(problems, "routes[0]", "unknown key 'methd' (did you mean 'method'?)")
    assert has_errors(problems)


def test_nested_unknown_keys():
    problems = validate_data({
        "config": {"sed": 1},
        "routes": [{
            "path": "/x",
            "match": {"qurey": {"a": 1}},
            "chaos": {"eror_rate": 0.5, "rate_limit": {"limit": 1, "windw_ms": 5}},
            "latency": {"fixed": 3},
            "response": {"stauts": 200},
        }],
        "resources": [{"name": "a", "id_feild": "x"}],
        "record": {"upstrem": None},
    })
    for loc, key in [
        ("config", "sed"), ("routes[0].match", "qurey"), ("routes[0].chaos", "eror_rate"),
        ("routes[0].chaos.rate_limit", "windw_ms"), ("routes[0].latency", "fixed"),
        ("routes[0].response", "stauts"), ("resources[0]", "id_feild"), ("record", "upstrem"),
    ]:
        assert _find(problems, loc, f"unknown key '{key}'"), (loc, key, _messages(problems))


def test_extension_keys_are_allowed():
    problems = validate_data({
        "config": {"_source": "imported", "x-owner": "qa"},
        "routes": [{"path": "/x", "x-note": "hi", "description": "ok", "response": {"body": 1}}],
    })
    assert not has_errors(problems), _messages(problems)


@pytest.mark.parametrize(
    "route,location,text",
    [
        ({"method": "FETCH", "path": "/x", "response": {}}, "routes[0].method", "unknown HTTP method 'FETCH'"),
        ({"path": "/x", "response": {"status": 700}}, "routes[0].response.status", "not a valid HTTP status"),
        ({"path": "/x", "response": {"status": "ok"}}, "routes[0].response.status", "not a valid HTTP status"),
        ({"path": "x", "response": {}}, "routes[0].path", "must start with '/'"),
        ({"path": "/a/{id", "response": {}}, "routes[0].path", "unbalanced braces"),
        ({"path": "/a/{id}/{id}", "response": {}}, "routes[0].path", "appears twice"),
        ({"path": "/x", "priority": "high", "response": {}}, "routes[0].priority", "must be an integer"),
        ({"path": "/x", "chaos": {"error_rate": 2}, "response": {}}, "routes[0].chaos.error_rate", "from 0 to 1"),
        ({"path": "/x", "chaos": {"error_status": 42}, "response": {}}, "routes[0].chaos.error_status", "not a valid"),
        ({"path": "/x", "chaos": {"rate_limit": {"limit": 0}}, "response": {}}, "routes[0].chaos.rate_limit.limit", ">= 1"),
        ({"path": "/x", "chaos": {"rate_limit": {}}, "response": {}}, "routes[0].chaos.rate_limit.limit", "is required"),
        ({"path": "/x", "latency": {"random_ms": [9, 1]}, "response": {}}, "routes[0].latency.random_ms", "greater than"),
        ({"path": "/x", "latency": {"random_ms": 5}, "response": {}}, "routes[0].latency.random_ms", "[low, high]"),
        ({"path": "/x", "latency": {"fixed_ms": -1}, "response": {}}, "routes[0].latency.fixed_ms", ">= 0"),
        ({"path": "/x", "response": {"headers": {"X": [1]}}}, "routes[0].response.headers.X", "single value"),
    ],
)
def test_bad_values_are_errors(route, location, text):
    problems = validate_data({"routes": [route]})
    assert _find(problems, location, text), _messages(problems)


@pytest.mark.parametrize(
    "template,text",
    [
        ("{{ faker.nmae }}", "unknown faker helper 'nmae' (did you mean 'name'?)"),
        ("{{ faker.int(1, 2, 3, 4) }}", "faker.int"),
        ("{{ faker.int(a, b) }}", "fails"),
        ("{{ reqest.query.q }}", "unknown template root 'reqest' (did you mean 'request'?)"),
        ("{{ request.qurey.q }}", "unknown request scope 'qurey' (did you mean 'query'?)"),
        ("{{ request.query }}", "needs a name"),
        ("{{ request.path.pid }}", "path param 'pid' (did you mean 'id'?) is not captured"),
        ("{{ request.query.q | uper }}", "unknown filter 'uper' (did you mean 'upper'?)"),
        ("{{ seq }}", "needs a counter name"),
        ("{{ seq.n(x) }}", "up to two integers"),
        ("{{ now.yesterday }}", "unknown now part"),
        ("{{ env }}", "needs a variable name"),
        ("{{ }}", "empty"),
    ],
)
def test_template_expressions_are_checked(template, text):
    problems = validate_data({"routes": [{"path": "/items/{id}", "response": {"body": {"v": template}}}]})
    assert any(text in p.message for p in problems if p.level == "error"), _messages(problems)


def test_valid_templates_pass():
    problems = validate_data({"routes": [{
        "path": "/items/{id}",
        "response": {
            "status": "{{ request.query.code | default(200) | int }}",
            "headers": {"X-Id": "{{ request.path.id }}"},
            "body": {
                "id": "{{ request.path.id | int }}",
                "name": "{{ faker.first_name }} {{ faker.last_name }}",
                "n": "{{ faker.int(1, 5) }}",
                "pick": "{{ faker.choice('a', 'b') }}",
                "lit": "{{ 'text' }}",
                "flag": "{{ true }}",
                "at": "{{ now.iso }}",
                "fmt": "{{ now('%Y') }}",
                "order": "{{ seq.order(1000, 5) }}",
                "env": "{{ env.HOME | default('x') }}",
                "u": "{{ uuid }}",
                "h": "{{ request.header.X-Api-Key }}",
                "b": "{{ request.body.user.name }}",
            },
        },
    }]})
    assert problems == [], _messages(problems)


def test_unclosed_template_is_a_warning():
    problems = validate_data({"routes": [{"path": "/x", "response": {"body": "Hello {{ faker.name"}}]})
    assert [p.level for p in problems] == ["warning"]


def test_body_file_checks(tmp_path):
    (tmp_path / "ok.json").write_text('{"n": "{{ faker.int(1, 3) }}"}', encoding="utf-8")
    (tmp_path / "bad.json").write_text("{nope", encoding="utf-8")
    (tmp_path / "typo.txt").write_text("Hi {{ faker.nmae }}", encoding="utf-8")
    problems = validate_data(
        {"routes": [
            {"path": "/ok", "response": {"file": "ok.json"}},
            {"path": "/missing", "response": {"file": "missing.json"}},
            {"path": "/bad", "response": {"file": "bad.json"}},
            {"path": "/typo", "response": {"file": "typo.txt"}},
            {"path": "/both", "response": {"file": "ok.json", "body": {"x": 1}}},
        ]},
        base_dir=str(tmp_path),
    )
    assert _find(problems, "routes[1].response.file", "file not found: missing.json")
    assert _find(problems, "routes[2].response.file", "not valid JSON")
    assert any(p.location.startswith("routes[3].response.file<typo.txt>") and "nmae" in p.message for p in problems)
    assert any(p.level == "warning" and "file wins" in p.message for p in problems)
    assert not any(p.location.startswith("routes[0]") for p in problems)


def test_resource_checks():
    problems = validate_data({"resources": [
        {"name": "todos", "id_type": "integer", "seed": [{"id": 1}, {"id": 1}, "x"]},
        {"name": "todos"},
        {"path": "/nameless"},
    ]})
    assert _find(problems, "resources[0].id_type", "(did you mean 'int'?)")
    assert _find(problems, "resources[0].seed[1].id", "duplicate id 1")
    assert _find(problems, "resources[0].seed[2]", "must be a mapping")
    assert _find(problems, "resources[1].name", "already used")
    assert _find(problems, "resources[1].path", "already served")
    assert _find(problems, "resources[2].name", "is required")


def test_duplicate_route_names_and_shadowed_routes():
    problems = validate_data({"routes": [
        {"name": "a", "path": "/x", "response": {}},
        {"name": "a", "path": "/y", "response": {}},
        {"path": "/x", "response": {"body": "never"}},
    ]})
    assert _find(problems, "routes[1].name", "already used by routes[0]")
    assert any(p.level == "warning" and p.location == "routes[2]" and "can never match" in p.message for p in problems)


def test_structure_errors():
    assert _find(validate_data({"routes": {"a": 1}}), "routes", "must be a list")
    assert _find(validate_data(["x"]), "", "must be a mapping")
    assert _find(validate_data({"record": {"upstream": "ftp://x"}}), "record.upstream", "http(s) URL")
    empty = validate_data(None)
    assert [p.level for p in empty] == ["warning"]


def test_yaml_syntax_error_has_a_line_number():
    problems = validate_text("routes:\n  - path: /x\n    response: {body: [1, 2}\n")
    assert has_errors(problems)
    assert problems[0].location.startswith("line 3")


def test_shipped_examples_validate_clean():
    assert EXAMPLES
    for path in EXAMPLES:
        assert validate_file(path) == [], (path, _messages(validate_file(path)))


def test_imported_petstore_validates_clean(tmp_path):
    out = tmp_path / "petstore.mocks.yaml"
    out.write_text(yaml.safe_dump(import_openapi(PETSTORE)), encoding="utf-8")
    assert validate_file(str(out)) == []


def test_load_config_strict_raises(tmp_path):
    path = tmp_path / "m.yaml"
    path.write_text("routes:\n  - path: /x\n    respnse: {}\n", encoding="utf-8")
    load_config(str(path))  # lenient by default, as before
    with pytest.raises(ConfigError) as err:
        load_config(str(path), strict=True)
    assert "respnse" in str(err.value)


def test_validate_command_exit_codes(tmp_path, capsys):
    good = os.path.join(ROOT, "examples", "shop-api", "mocks.yaml")
    assert cli.main(["validate", good]) == 0
    assert "OK (12 routes, 1 resource)" in capsys.readouterr().out

    broken = tmp_path / "broken.yaml"
    broken.write_text("routes:\n  - methd: GET\n    path: /x\n    response: {status: 999}\n", encoding="utf-8")
    assert cli.main(["validate", good, str(broken)]) == 1
    out = capsys.readouterr().out
    assert "did you mean 'method'" in out and "routes[0].response.status" in out

    warn_only = tmp_path / "warn.yaml"
    warn_only.write_text("routes:\n  - path: /x\n", encoding="utf-8")
    assert cli.main(["validate", str(warn_only)]) == 0
    assert cli.main(["validate", "--strict", str(warn_only)]) == 1

    assert cli.main(["validate", "--json", str(broken)]) == 1
    assert '"level": "error"' in capsys.readouterr().out


def test_serve_refuses_invalid_config_unless_told(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "_run", lambda app, host, port: calls.append(app))
    broken = tmp_path / "broken.yaml"
    broken.write_text("routes:\n  - path: /x\n    respnse: {status: 201}\n", encoding="utf-8")
    assert cli.main(["serve", "--config", str(broken)]) == 1
    assert "Refusing to start" in capsys.readouterr().err
    assert calls == []
    assert cli.main(["serve", "--config", str(broken), "--no-validate"]) == 0
    assert len(calls) == 1
