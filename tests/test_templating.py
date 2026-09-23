"""Templating: typed single-token values, faker, sequences, request access."""
import uuid

import pytest

from mockserver.dynamic import MockFaker, TemplateEngine, TemplateError, parse_expression, single_expression


def test_single_token_preserves_native_type():
    engine = TemplateEngine(seed=1)
    ctx = {"query": {"page": "3"}}
    # Whole-string token -> real int, not "3".
    out = engine.render({"page": "{{ request.query.page | int }}"}, ctx)
    assert out == {"page": 3}
    assert isinstance(out["page"], int)


def test_embedded_token_is_stringified():
    engine = TemplateEngine(seed=1)
    out = engine.render("id-{{ seq.n }}", {})
    assert out == "id-1"


def test_sequence_increments_and_is_named():
    engine = TemplateEngine(seed=1)
    a = engine.render("{{ seq.order }}", {})
    b = engine.render("{{ seq.order }}", {})
    c = engine.render("{{ seq.other }}", {})
    assert (a, b, c) == (1, 2, 1)


def test_sequence_with_start_and_step():
    engine = TemplateEngine(seed=1)
    a = engine.render("{{ seq.inv(1000, 5) }}", {})
    b = engine.render("{{ seq.inv(1000, 5) }}", {})
    assert (a, b) == (1000, 1005)


def test_faker_is_deterministic_for_a_seed():
    a = TemplateEngine(seed=42).render("{{ faker.name }}", {})
    b = TemplateEngine(seed=42).render("{{ faker.name }}", {})
    assert a == b
    assert " " in a  # "First Last"


def test_faker_int_returns_int_in_range():
    engine = TemplateEngine(seed=5)
    for _ in range(50):
        v = engine.render("{{ faker.int(1, 10) }}", {})
        assert isinstance(v, int) and 1 <= v <= 10


def test_default_filter_fills_missing_request_value():
    engine = TemplateEngine(seed=1)
    out = engine.render("{{ request.query.page | default(1) | int }}", {"query": {}})
    assert out == 1


def test_request_body_nested_access():
    engine = TemplateEngine(seed=1)
    ctx = {"json": {"user": {"name": "Ada"}}}
    out = engine.render("{{ request.body.user.name }}", ctx)
    assert out == "Ada"


def test_uuid_and_now_render_to_strings():
    engine = TemplateEngine(seed=1)
    u = engine.render("{{ uuid }}", {})
    assert isinstance(u, str) and len(u) == 36
    ts = engine.render("{{ now.timestamp }}", {})
    assert isinstance(ts, int)


def test_nested_structures_are_walked():
    engine = TemplateEngine(seed=1)
    tpl = {"items": [{"id": "{{ seq.i }}"}, {"id": "{{ seq.i }}"}]}
    out = engine.render(tpl, {})
    assert out == {"items": [{"id": 1}, {"id": 2}]}


# --------------------------------------------------------------------------- #
# Regressions: several tokens in one string, errors that name the expression
# --------------------------------------------------------------------------- #

def test_two_tokens_interpolate_instead_of_swallowing_the_string():
    engine = TemplateEngine(seed=1)
    out = engine.render("{{ faker.first_name }} {{ faker.last_name }}", {})
    assert "{{" not in out and "}}" not in out and "faker" not in out
    first, last = out.split(" ")
    assert first in MockFaker._FIRST and last in MockFaker._LAST


def test_two_request_tokens_with_separator():
    engine = TemplateEngine(seed=1)
    out = engine.render("{{ request.query.a }}-{{ request.query.b }}", {"query": {"a": "x", "b": "y"}})
    assert out == "x-y"


def test_single_token_with_outer_whitespace_keeps_type():
    engine = TemplateEngine(seed=1)
    assert engine.render("  {{ 41 }}  ", {}) == 41


def test_single_expression_detection():
    assert single_expression("{{ a }}") == "a"
    assert single_expression("{{ a }} {{ b }}") is None
    assert single_expression("x {{ a }}") is None


def test_interpolated_booleans_are_lowercase_json_style():
    engine = TemplateEngine(seed=1)
    assert engine.render("flag={{ true }}", {}) == "flag=true"


def test_unknown_faker_helper_raises_template_error():
    engine = TemplateEngine(seed=1)
    with pytest.raises(TemplateError) as err:
        engine.render("{{ faker.nmae }}", {})
    assert "nmae" in str(err.value)
    assert err.value.expression == "faker.nmae"


def test_bad_faker_arguments_raise_template_error():
    engine = TemplateEngine(seed=1)
    with pytest.raises(TemplateError) as err:
        engine.render({"n": "{{ faker.int(a, b) }}"}, {})
    assert "faker.int" in str(err.value)


def test_unknown_filter_raises_template_error():
    engine = TemplateEngine(seed=1)
    with pytest.raises(TemplateError):
        engine.render("{{ request.query.x | shout }}", {"query": {"x": "hi"}})


def test_faker_choice_and_valid_uuid4():
    engine = TemplateEngine(seed=3)
    assert engine.render("{{ faker.choice('a', 'b') }}", {}) in ("a", "b")
    value = engine.render("{{ faker.uuid }}", {})
    assert uuid.UUID(value).version == 4


def test_reset_rewinds_rng_and_sequences():
    engine = TemplateEngine(seed=9)
    first = [engine.render("{{ faker.name }}", {}), engine.render("{{ seq.n }}", {})]
    engine.render("{{ seq.n }}", {})
    engine.reset()
    again = [engine.render("{{ faker.name }}", {}), engine.render("{{ seq.n }}", {})]
    assert first == again


def test_parse_expression_structure():
    parsed = parse_expression("faker.int(1, 5) | default(3) | int")
    assert parsed.kind == "ref"
    assert (parsed.root, parsed.rest, parsed.args) == ("faker", ["int"], [1, 5])
    assert [f[0] for f in parsed.filters] == ["default", "int"]
    assert parse_expression("'quoted'").kind == "literal"
    assert parse_expression("true").literal is True
