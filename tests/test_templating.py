"""Templating: typed single-token values, faker, sequences, request access."""
from mockserver.dynamic import TemplateEngine


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
