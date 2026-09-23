"""Build a mocks.yaml from an OpenAPI 3 (or Swagger 2) document.

For every operation the importer picks the most useful documented response
and turns it into a mock route. Status keys may be quoted or bare
(``200:``), ranges (``2XX``) or ``default``. The response body comes from, in
order of preference:

  1. a response ``example`` / ``examples``,
  2. a schema ``example`` (or 3.1 ``examples``),
  3. a value synthesized from the schema, respecting ``example``, ``default``,
     ``const``, ``enum``, ``format``, ``pattern`` (a matching string is
     generated), ``minimum``/``maximum`` (and the exclusive variants),
     ``multipleOf``, ``minLength``/``maxLength``, ``minItems`` and
     ``nullable`` / ``type: [x, "null"]``.

Options:

``base_path``
    Prefix every path. ``"auto"`` uses the path of ``servers[0].url`` (or
    Swagger ``basePath``), so ``https://api.example.com/v1`` + ``/pets``
    becomes ``/v1/pets``, which is what the frontend actually calls.
``dynamic``
    Emit ``{{ }}`` templates instead of frozen values: path params are echoed
    into the matching field (``GET /pets/123`` returns ``id: 123``), write
    operations echo the request body fields, formats and field names map to
    faker helpers (email, uuid, name, city, phone, url...), numbers get
    ``faker.int``/``faker.float`` within their bounds, enums become
    ``faker.choice``, and array responses contain several generated items.
``resources``
    Detect collection + item pairs (``/pets`` and ``/pets/{petId}``) and
    emit stateful ``resources:`` seeded from the spec's examples instead of
    static routes, so ``POST /pets`` then ``GET /pets/{id}`` round-trips.

``$ref`` pointers inside the document are resolved (``#/components/...``
and Swagger's ``#/definitions/...``). The output is a plain dict ready for
:func:`mockserver.config.build_config` or ``yaml.safe_dump``, and it passes
``mockserver validate``.
"""
from __future__ import annotations

import copy
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import yaml

try:  # Python 3.11+
    import re._parser as _sre_parse  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - Python < 3.11
    import sre_parse as _sre_parse  # type: ignore[no-redef]

_MAX_DEPTH = 8
_METHODS = ("get", "post", "put", "patch", "delete", "options", "head")
_WRITE_METHODS = ("post", "put", "patch")
_DYNAMIC_ARRAY_ITEMS = 3
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

AUTO = "auto"


class _RefResolver:
    def __init__(self, root: Dict[str, Any]) -> None:
        self.root = root

    def resolve(self, node: Any, _seen: Optional[set] = None) -> Any:
        seen = _seen or set()
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if ref in seen:
                return {}
            seen.add(ref)
            node = self._lookup(ref)
        return node

    def _lookup(self, ref: str) -> Any:
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return {}
        node: Any = self.root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return {}
        return node


# --------------------------------------------------------------------------- #
# Schema helpers
# --------------------------------------------------------------------------- #

def _schema_type(schema: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    """(type, nullable), understanding 3.0 ``nullable`` and 3.1 type lists."""
    raw = schema.get("type")
    nullable = bool(schema.get("nullable"))
    if isinstance(raw, list):
        nullable = nullable or "null" in raw
        types = [t for t in raw if t != "null"]
        raw = types[0] if types else "null"
    if raw is None:
        if "properties" in schema:
            raw = "object"
        elif "items" in schema:
            raw = "array"
    return raw, nullable


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _bounds(schema: Dict[str, Any]) -> Tuple[Optional[float], bool, Optional[float], bool]:
    """(low, low_exclusive, high, high_exclusive) for 3.0 and 3.1 styles."""
    lo, hi = _num(schema.get("minimum")), _num(schema.get("maximum"))
    lo_ex = hi_ex = False
    ex_min, ex_max = schema.get("exclusiveMinimum"), schema.get("exclusiveMaximum")
    if ex_min is True:
        lo_ex = True
    elif _num(ex_min) is not None:
        lo, lo_ex = _num(ex_min), True
    if ex_max is True:
        hi_ex = True
    elif _num(ex_max) is not None:
        hi, hi_ex = _num(ex_max), True
    return lo, lo_ex, hi, hi_ex


def _int_range(schema: Dict[str, Any], default: Tuple[int, int] = (0, 100)) -> Tuple[int, int]:
    import math

    lo, lo_ex, hi, hi_ex = _bounds(schema)
    ilo = None if lo is None else (math.floor(lo) + 1 if lo_ex else math.ceil(lo))
    ihi = None if hi is None else (math.ceil(hi) - 1 if hi_ex else math.floor(hi))
    if ilo is None and ihi is None:
        ilo, ihi = default
    elif ilo is None:
        ilo = min(default[0], ihi) if ihi >= default[0] else ihi - (default[1] - default[0])
    elif ihi is None:
        ihi = max(ilo + (default[1] - default[0]), ilo)
    if ihi < ilo:
        ihi = ilo
    step = schema.get("multipleOf")
    if isinstance(step, int) and not isinstance(step, bool) and step > 0:
        first = -(-ilo // step) * step
        if first <= ihi:
            ilo = first
            ihi = first + ((ihi - first) // step) * step
    return int(ilo), int(ihi)


def _static_int(schema: Dict[str, Any]) -> int:
    lo, hi = _int_range(schema, default=(0, 100))
    value = lo
    step = schema.get("multipleOf")
    if isinstance(step, int) and not isinstance(step, bool) and step > 0 and value % step:
        value = min(-(-value // step) * step, hi)
    return value


def _float_range(schema: Dict[str, Any], default: Tuple[float, float] = (0.0, 100.0)) -> Tuple[float, float]:
    lo, lo_ex, hi, hi_ex = _bounds(schema)
    flo = default[0] if lo is None else float(lo)
    fhi = (max(flo + (default[1] - default[0]), flo) if hi is None else float(hi))
    if lo is None and hi is not None:
        flo = min(default[0], fhi)
    span = fhi - flo
    if lo_ex:
        flo += span / 100 if span > 0 else 0.01
    if hi_ex:
        fhi -= span / 100 if span > 0 else 0.01
    if fhi < flo:
        fhi = flo
    return round(flo, 2), round(fhi, 2)


def _static_number(schema: Dict[str, Any]) -> float:
    lo, lo_ex, hi, hi_ex = _bounds(schema)
    if lo is None and hi is None:
        return 0.0
    flo, fhi = _float_range(schema)
    if lo_ex or hi_ex:
        return round((flo + fhi) / 2, 2)
    return flo


_FORMAT_STRINGS = {
    "date-time": "2026-01-01T00:00:00Z",
    "date": "2026-01-01",
    "time": "00:00:00",
    "email": "user@example.com",
    "uuid": "00000000-0000-4000-8000-000000000000",
    "uri": "https://example.com",
    "url": "https://example.com",
    "hostname": "example.com",
    "ipv4": "192.0.2.1",
    "ipv6": "2001:db8::1",
    "byte": "c3RyaW5n",
    "password": "string",
}


class _PatternUnsupported(Exception):
    pass


def _op(op: Any) -> str:
    return str(getattr(op, "name", op))


def _pick_from_class(items: List[Tuple[Any, Any]], counter: List[int]) -> str:
    negate = any(_op(op) == "NEGATE" for op, _ in items)
    pool: List[str] = []
    for op, av in items:
        name = _op(op)
        if name == "LITERAL":
            pool.append(chr(av))
        elif name == "RANGE":
            lo, hi = av
            pool.extend(chr(c) for c in range(lo, min(hi, lo + 25) + 1))
        elif name == "CATEGORY":
            pool.extend(_category_chars(_op(av)))
    if negate:
        for candidate in "axZ0_-":
            if candidate not in pool:
                return candidate
        raise _PatternUnsupported("negated class")
    if not pool:
        raise _PatternUnsupported("empty class")
    char = pool[counter[0] % len(pool)]
    counter[0] += 1
    return char


def _category_chars(name: str) -> str:
    if name.endswith("NOT_DIGIT"):
        return "abc"
    if name.endswith("DIGIT"):
        return "0123456789"
    if name.endswith("NOT_WORD"):
        return "-"
    if name.endswith("WORD"):
        return "abcdefgh"
    if name.endswith("NOT_SPACE"):
        return "abc"
    if name.endswith("SPACE"):
        return " "
    raise _PatternUnsupported(name)


def _gen_pattern(parsed: Any, out: List[str], counter: List[int]) -> None:
    for op, av in parsed:
        name = _op(op)
        if name == "LITERAL":
            out.append(chr(av))
        elif name == "NOT_LITERAL":
            out.append("a" if chr(av) != "a" else "b")
        elif name == "ANY":
            out.append("x")
        elif name == "IN":
            out.append(_pick_from_class(av, counter))
        elif name == "CATEGORY":
            out.append(_category_chars(_op(av))[0])
        elif name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            lo, hi, sub = av
            n = lo if lo > 0 else 1
            if lo != hi and n < 3 <= hi:
                n = 3
            n = min(n, hi)
            for _ in range(n):
                _gen_pattern(sub, out, counter)
        elif name == "SUBPATTERN":
            _gen_pattern(av[-1], out, counter)
        elif name == "ATOMIC_GROUP":
            _gen_pattern(av, out, counter)
        elif name == "BRANCH":
            _gen_pattern(av[1][0], out, counter)
        elif name in ("AT", "ASSERT", "ASSERT_NOT"):
            continue
        else:
            raise _PatternUnsupported(name)


def string_from_pattern(pattern: str) -> Optional[str]:
    """A deterministic string that matches ``pattern``, or None if unsure."""
    try:
        parsed = _sre_parse.parse(pattern)
        out: List[str] = []
        _gen_pattern(parsed, out, [0])
        value = "".join(out)
        if re.search(pattern, value):
            return value
    except (_PatternUnsupported, re.error, TypeError, ValueError, IndexError, OverflowError):
        return None
    return None


def _fit_length(value: str, schema: Dict[str, Any]) -> str:
    """Pad or trim ``value`` into [minLength, maxLength]."""
    min_len, max_len = schema.get("minLength"), schema.get("maxLength")
    if isinstance(min_len, int) and len(value) < min_len:
        value = value + "x" * (min_len - len(value))
    if isinstance(max_len, int) and len(value) > max_len:
        value = value[:max_len]
    return value


def _static_string(schema: Dict[str, Any]) -> str:
    fmt = schema.get("format")
    if fmt in _FORMAT_STRINGS:
        return _FORMAT_STRINGS[fmt]
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        generated = string_from_pattern(pattern)
        if generated is not None:
            fitted = _fit_length(generated, schema)
            if re.search(pattern, fitted):
                return fitted
            return generated
    return _fit_length("string", schema)


def _schema_examples(schema: Dict[str, Any]) -> Tuple[bool, Any]:
    if "example" in schema:
        return True, schema["example"]
    examples = schema.get("examples")
    if isinstance(examples, list) and examples:  # JSON Schema / OpenAPI 3.1
        return True, examples[0]
    return False, None


def example_from_schema(schema: Any, resolver: _RefResolver, depth: int = 0) -> Any:
    """A static example value for ``schema``."""
    schema = resolver.resolve(schema)
    if not isinstance(schema, dict) or depth > _MAX_DEPTH:
        return None

    found, example = _schema_examples(schema)
    if found:
        return example
    if "default" in schema:
        return schema["default"]
    if "const" in schema:
        return schema["const"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]

    for combiner in ("allOf", "oneOf", "anyOf"):
        if combiner in schema and schema[combiner]:
            if combiner == "allOf":
                merged: Dict[str, Any] = {}
                for sub in schema[combiner]:
                    part = example_from_schema(sub, resolver, depth + 1)
                    if isinstance(part, dict):
                        merged.update(part)
                if merged:
                    return merged
            return example_from_schema(schema[combiner][0], resolver, depth + 1)

    schema_type, _nullable = _schema_type(schema)
    if schema_type == "object":
        props = schema.get("properties", {}) or {}
        return {name: example_from_schema(sub, resolver, depth + 1) for name, sub in props.items()}
    if schema_type == "array":
        item = example_from_schema(schema.get("items", {}), resolver, depth + 1)
        if item is None:
            return []
        count = schema.get("minItems") if isinstance(schema.get("minItems"), int) else 1
        return [copy.deepcopy(item) for _ in range(max(1, min(count, 5)))]
    if schema_type == "integer":
        return _static_int(schema)
    if schema_type == "number":
        return _static_number(schema)
    if schema_type == "boolean":
        return True
    if schema_type == "string":
        return _static_string(schema)
    return None


# --------------------------------------------------------------------------- #
# Dynamic (templated) generation
# --------------------------------------------------------------------------- #

_NAME_HINTS: List[Tuple[Tuple[str, ...], str]] = [
    (("email", "emailaddress", "mail"), "{{ faker.email }}"),
    (("firstname", "givenname", "forename"), "{{ faker.first_name }}"),
    (("lastname", "surname", "familyname"), "{{ faker.last_name }}"),
    (("username", "login", "handle", "nickname"), "{{ faker.username }}"),
    (("name", "fullname", "displayname", "customername", "ownername", "authorname"), "{{ faker.name }}"),
    (("city", "town"), "{{ faker.city }}"),
    (("country",), "{{ faker.country }}"),
    (("company", "organization", "organisation", "companyname", "employer"), "{{ faker.company }}"),
    (("phone", "phonenumber", "mobile", "telephone", "tel"), "{{ faker.phone }}"),
    (("color", "colour"), "{{ faker.color }}"),
    (("slug",), "{{ faker.slug }}"),
    (("url", "website", "homepage", "link", "avatar", "avatarurl", "imageurl", "image", "photourl"), "{{ faker.url }}"),
    (("title", "headline", "subject"), "{{ faker.words(3) | title }}"),
    (("description", "summary", "bio", "body", "content", "text", "comment", "message", "notes", "note"),
     "{{ faker.sentence }}"),
    (("status", "state"), "{{ faker.status }}"),
    (("tag", "category", "label", "kind"), "{{ faker.word }}"),
]
_FORMAT_TEMPLATES = {
    "email": "{{ faker.email }}",
    "uuid": "{{ faker.uuid }}",
    "date-time": "{{ now.iso }}",
    "date": "{{ faker.date }}",
    "time": "{{ now.time }}",
    "uri": "{{ faker.url }}",
    "url": "{{ faker.url }}",
    "ipv4": "{{ faker.ipv4 }}",
}
_PRICE_WORDS = ("price", "amount", "cost", "total", "subtotal", "balance", "fee")


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _quote_literal(value: Any) -> Optional[str]:
    """Render a literal for a template argument list, or None if unsafe."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str) and "}}" not in value and "{{" not in value:
        if "'" not in value:
            return f"'{value}'"
        if '"' not in value:
            return f'"{value}"'
    return None


class _DynamicContext:
    """What a template may refer to for one operation."""

    def __init__(
        self,
        path_params: Dict[str, Dict[str, Any]],
        request_fields: Dict[str, Any],
        collection: str,
        method: str,
    ) -> None:
        self.path_params = path_params          # sanitized name -> schema
        self.request_fields = request_fields    # top-level request body props
        self.collection = collection
        self.method = method

    def id_param(self) -> Optional[str]:
        """The path param that identifies the item (petId, id, pet_id...)."""
        names = list(self.path_params)
        for name in reversed(names):
            if _norm(name) == "id" or _norm(name).endswith("id"):
                return name
        return names[-1] if names else None


def _param_echo(prop: str, schema: Dict[str, Any], ctx: _DynamicContext) -> Optional[str]:
    target: Optional[str] = None
    if prop in ctx.path_params:
        target = prop
    elif _norm(prop) == "id":
        target = ctx.id_param()
    if target is None:
        return None
    param_schema = ctx.path_params.get(target) or {}
    as_int = _schema_type(schema)[0] == "integer" or _schema_type(param_schema)[0] == "integer"
    return "{{ request.path.%s%s }}" % (target, " | int" if as_int else "")


def template_from_schema(
    schema: Any,
    resolver: _RefResolver,
    ctx: _DynamicContext,
    depth: int = 0,
    prop: Optional[str] = None,
    level: str = "root",
) -> Any:
    """Like :func:`example_from_schema` but returns ``{{ }}`` templates.

    ``level`` is ``"root"`` for the response body itself, ``"field"`` for a
    property of the root object (only those echo path params and request
    fields) and ``"nested"`` for everything deeper.
    """
    schema = resolver.resolve(schema)
    if not isinstance(schema, dict) or depth > _MAX_DEPTH:
        return None

    if prop is not None and level == "field":
        echo = _param_echo(prop, schema, ctx)
        if echo is not None:
            return echo
        if ctx.method in _WRITE_METHODS and prop in ctx.request_fields and _norm(prop) != "id":
            return "{{ request.body.%s }}" % prop if _IDENT.match(prop) else example_from_schema(schema, resolver)
        if ctx.method == "post" and _norm(prop) == "id" and _schema_type(schema)[0] == "integer":
            return "{{ seq.%s_id(1) }}" % (re.sub(r"\W", "_", ctx.collection) or "item")

    if "const" in schema:
        return schema["const"]
    if "enum" in schema and schema["enum"]:
        literals = [_quote_literal(v) for v in schema["enum"] if v is not None]
        if literals and all(lit is not None for lit in literals):
            return "{{ faker.choice(%s) }}" % ", ".join(literals)  # type: ignore[arg-type]
        return schema["enum"][0]

    for combiner in ("allOf", "oneOf", "anyOf"):
        if combiner in schema and schema[combiner]:
            if combiner == "allOf":
                merged: Dict[str, Any] = {}
                for sub in schema[combiner]:
                    part = template_from_schema(sub, resolver, ctx, depth + 1, None, level)
                    if isinstance(part, dict):
                        merged.update(part)
                if merged:
                    return merged
            return template_from_schema(schema[combiner][0], resolver, ctx, depth + 1, prop, level)

    schema_type, _nullable = _schema_type(schema)
    if schema_type == "object":
        props = schema.get("properties", {}) or {}
        child_level = "field" if level == "root" else "nested"
        return {
            name: template_from_schema(sub, resolver, ctx, depth + 1, name, child_level)
            for name, sub in props.items()
        }
    if schema_type == "array":
        item = template_from_schema(schema.get("items", {}), resolver, ctx, depth + 1, None, "nested")
        if item is None:
            return []
        count = max(_DYNAMIC_ARRAY_ITEMS, schema.get("minItems") or 0)
        if isinstance(schema.get("maxItems"), int):
            count = min(count, schema["maxItems"])
        # Separate copies: each item renders independently and the YAML
        # output stays free of &anchor/*alias noise.
        return [copy.deepcopy(item) for _ in range(max(count, 0))]
    if schema_type == "integer":
        default = (1, 1000) if prop and _norm(prop).endswith("id") else (0, 100)
        lo, hi = _int_range(schema, default=default)
        return "{{ faker.int(%d, %d) }}" % (lo, hi)
    if schema_type == "number":
        default = (1.0, 500.0) if prop and any(w in _norm(prop) for w in _PRICE_WORDS) else (0.0, 100.0)
        lo_f, hi_f = _float_range(schema, default=default)
        helper = "price" if prop and any(w in _norm(prop) for w in _PRICE_WORDS) else "float"
        return "{{ faker.%s(%s, %s) }}" % (helper, lo_f, hi_f)
    if schema_type == "boolean":
        return "{{ faker.bool }}"
    if schema_type == "string":
        return _string_template(schema, prop)
    return example_from_schema(schema, resolver, depth)


def _string_template(schema: Dict[str, Any], prop: Optional[str]) -> Any:
    fmt = schema.get("format")
    if fmt in _FORMAT_TEMPLATES:
        return _FORMAT_TEMPLATES[fmt]
    if "pattern" in schema or "minLength" in schema or "maxLength" in schema:
        found, example = _schema_examples(schema)
        return example if found else _static_string(schema)
    if prop:
        key = _norm(prop)
        for names, template in _NAME_HINTS:
            if key in names:
                return template
    found, example = _schema_examples(schema)
    if found:
        return example
    if fmt in _FORMAT_STRINGS:
        return _FORMAT_STRINGS[fmt]
    return "{{ faker.words(2) }}"


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #

def _normalize_status(code: Any) -> Optional[Tuple[str, Optional[int]]]:
    """Map a responses key to (kind, status). Returns None for junk keys."""
    key = str(code).strip().upper()
    if key == "DEFAULT":
        return "default", None
    if re.fullmatch(r"[1-5]XX", key):
        return "range", int(key[0]) * 100
    if key.isdigit() and 100 <= int(key) <= 599:
        return "exact", int(key)
    return None


def _pick_response(responses: Dict[Any, Any], method: str = "get") -> Optional[Tuple[Any, int, Any]]:
    """Choose the most useful documented response: (original key, status, response)."""
    if not isinstance(responses, dict) or not responses:
        return None
    candidates = []
    for key, value in responses.items():
        norm = _normalize_status(key)
        if norm is None:
            continue
        kind, status = norm
        if kind == "exact":
            assert status is not None
            if 200 <= status < 300:
                preferred = 201 if method == "post" else 200
                rank = (0, 0 if status == preferred else 1, status)
            elif status < 400:
                rank = (3, 0, status)
            else:
                rank = (4, 0, status)
        elif kind == "range":
            assert status is not None
            rank = (1, 0, status) if status == 200 else (3 if status < 400 else 4, 1, status)
            if status == 200:
                status = 201 if method == "post" else 200
        else:
            rank = (2, 0, 0)
            status = 201 if method == "post" else 200
        candidates.append((rank, key, status, value))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    _rank, key, status, value = candidates[0]
    return key, int(status), value


def _response_media(response: Dict[str, Any], resolver: _RefResolver, swagger2: bool) -> Tuple[Optional[Dict[str, Any]], Any, bool]:
    """(schema, example, has_example) for a response object."""
    response = resolver.resolve(response)
    if not isinstance(response, dict):
        return None, None, False
    if swagger2:
        examples = response.get("examples")
        if isinstance(examples, dict) and examples:
            value = examples.get("application/json", next(iter(examples.values())))
            return response.get("schema"), value, True
        return response.get("schema"), None, False
    content = response.get("content", {}) or {}
    media = content.get("application/json")
    if media is None:
        media = next((v for k, v in content.items() if "json" in str(k)), None)
    if media is None and content:
        media = next(iter(content.values()))
    media = resolver.resolve(media)
    if not isinstance(media, dict):
        return None, None, False
    if "example" in media:
        return media.get("schema"), media["example"], True
    examples = media.get("examples")
    if isinstance(examples, dict) and examples:
        first = resolver.resolve(next(iter(examples.values())))
        if isinstance(first, dict) and "value" in first:
            return media.get("schema"), first["value"], True
    return media.get("schema"), None, False


def _body_from_response(response: Dict[str, Any], resolver: _RefResolver, swagger2: bool = False) -> Any:
    schema, example, has_example = _response_media(response, resolver, swagger2)
    if has_example:
        return example
    if schema is not None:
        return example_from_schema(schema, resolver)
    return None


def _request_fields(operation: Dict[str, Any], resolver: _RefResolver, swagger2: bool) -> Dict[str, Any]:
    schema: Any = None
    if swagger2:
        for param in operation.get("parameters") or []:
            param = resolver.resolve(param)
            if isinstance(param, dict) and param.get("in") == "body":
                schema = param.get("schema")
    else:
        body = resolver.resolve(operation.get("requestBody") or {})
        content = (body or {}).get("content") or {} if isinstance(body, dict) else {}
        media = content.get("application/json")
        if media is None and content:
            media = next(iter(content.values()))
        media = resolver.resolve(media)
        if isinstance(media, dict):
            schema = media.get("schema")
    schema = resolver.resolve(schema)
    if isinstance(schema, dict):
        props: Dict[str, Any] = dict(schema.get("properties") or {})
        for part in schema.get("allOf") or []:
            part = resolver.resolve(part)
            if isinstance(part, dict):
                props.update(part.get("properties") or {})
        return props
    return {}


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def _sanitize_path(path: str) -> Tuple[str, Dict[str, str]]:
    """Make ``{param}`` names identifiers (``{pet-id}`` -> ``{pet_id}``)."""
    mapping: Dict[str, str] = {}

    def repl(m: "re.Match[str]") -> str:
        original = m.group(1)
        clean = re.sub(r"\W", "_", original) or "param"
        if clean[0].isdigit():
            clean = "_" + clean
        mapping[original] = clean
        return "{%s}" % clean

    return re.sub(r"\{([^{}]+)\}", repl, path), mapping


def _path_params(item: Dict[str, Any], operation: Dict[str, Any], resolver: _RefResolver,
                 mapping: Dict[str, str], swagger2: bool) -> Dict[str, Dict[str, Any]]:
    params: Dict[str, Dict[str, Any]] = {original: {} for original in mapping}
    for source in (item.get("parameters") or [], operation.get("parameters") or []):
        for param in source:
            param = resolver.resolve(param)
            if not isinstance(param, dict) or param.get("in") != "path":
                continue
            name = param.get("name")
            if name in params:
                schema = param if swagger2 else resolver.resolve(param.get("schema") or {})
                params[name] = schema if isinstance(schema, dict) else {}
    return {mapping[name]: schema for name, schema in params.items()}


def detect_base_path(spec: Dict[str, Any]) -> str:
    """The path part of ``servers[0].url`` (OpenAPI 3) or ``basePath`` (Swagger 2)."""
    if spec.get("swagger"):
        base = str(spec.get("basePath") or "")
    else:
        servers = spec.get("servers") or []
        url = servers[0].get("url", "") if servers and isinstance(servers[0], dict) else ""
        # Server variables like {version} cannot be resolved offline: use defaults.
        if servers and isinstance(servers[0], dict):
            for var, meta in (servers[0].get("variables") or {}).items():
                if isinstance(meta, dict) and "default" in meta:
                    url = url.replace("{%s}" % var, str(meta["default"]))
        base = urlparse(url).path if "://" in url else url
    base = "/" + base.strip("/") if base.strip("/") else ""
    return base


def _normalize_base(base_path: Optional[str], spec: Dict[str, Any]) -> str:
    if base_path is None:
        return ""
    if base_path == AUTO:
        return detect_base_path(spec)
    stripped = base_path.strip().strip("/")
    return "/" + stripped if stripped else ""


def _collection_name(path: str) -> str:
    segments = [s for s in path.split("/") if s and not s.startswith("{")]
    return segments[-1] if segments else "items"


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #

def load_spec(spec_path: str) -> Dict[str, Any]:
    with open(spec_path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    try:
        spec = yaml.safe_load(raw)
    except yaml.YAMLError:
        spec = json.loads(raw)
    if not isinstance(spec, dict):
        raise ValueError("The OpenAPI document did not parse to an object.")
    if not (spec.get("openapi") or spec.get("swagger")):
        raise ValueError(
            "Not an OpenAPI document: expected a top-level 'openapi: 3.x' or 'swagger: \"2.0\"' key."
        )
    if spec.get("swagger") and not str(spec["swagger"]).startswith("2"):
        raise ValueError(f"Unsupported Swagger version {spec['swagger']!r}; only 2.0 and OpenAPI 3.x are supported.")
    return spec


def import_openapi(
    spec_path: str,
    base_path: Optional[str] = None,
    dynamic: bool = False,
    resources: bool = False,
) -> Dict[str, Any]:
    """Parse an OpenAPI 3 / Swagger 2 file and return a mocks-config dict.

    ``base_path=None`` keeps the spec's paths as they are; ``"auto"`` prefixes
    them with the server URL path (the CLI default); any other string is used
    as the prefix. See the module docstring for ``dynamic`` and ``resources``.
    """
    spec = load_spec(spec_path)
    swagger2 = bool(spec.get("swagger"))
    resolver = _RefResolver(spec)
    prefix = _normalize_base(base_path, spec)

    operations: List[Dict[str, Any]] = []
    for raw_path, item in (spec.get("paths") or {}).items():
        item = resolver.resolve(item)
        if not isinstance(item, dict):
            continue
        path, mapping = _sanitize_path(str(raw_path))
        for method, operation in item.items():
            if str(method).lower() not in _METHODS or not isinstance(operation, dict):
                continue
            operations.append({
                "method": str(method).lower(),
                "path": path,
                "item": item,
                "operation": operation,
                "mapping": mapping,
            })

    resource_blocks: List[Dict[str, Any]] = []
    claimed: set = set()
    if resources:
        resource_blocks, claimed = _detect_resources(operations, resolver, swagger2, prefix)

    routes: List[Dict[str, Any]] = []
    names: set = set()
    for op in operations:
        if (op["method"], op["path"]) in claimed:
            continue
        route = _build_route(op, resolver, swagger2, prefix, dynamic)
        if "name" in route:  # operationIds should be unique, but real specs repeat them
            base, n = route["name"], 2
            while route["name"] in names:
                route["name"], n = f"{base}-{n}", n + 1
            names.add(route["name"])
        routes.append(route)

    info = spec.get("info", {}) or {}
    config: Dict[str, Any] = {
        "config": {
            "cors": True,
            "seed": 42,
            "_source": f"imported from {os.path.basename(spec_path)}: "
                       f"{info.get('title', 'API')} {info.get('version', '')}".strip(),
        },
        "routes": routes,
    }
    if resource_blocks:
        config["resources"] = resource_blocks
    return config


def _build_route(op: Dict[str, Any], resolver: _RefResolver, swagger2: bool, prefix: str,
                 dynamic: bool) -> Dict[str, Any]:
    method, operation = op["method"], op["operation"]
    picked = _pick_response(operation.get("responses") or {}, method)
    status = 201 if method == "post" else 200
    body: Any = None
    if picked:
        _key, status, response = picked
        if dynamic:
            schema, example, has_example = _response_media(response, resolver, swagger2)
            if schema is not None:
                ctx = _DynamicContext(
                    _path_params(op["item"], operation, resolver, op["mapping"], swagger2),
                    _request_fields(operation, resolver, swagger2),
                    _collection_name(op["path"]),
                    method,
                )
                body = template_from_schema(schema, resolver, ctx)
            elif has_example:
                body = example
        else:
            body = _body_from_response(response, resolver, swagger2)
    if status == 204:
        body = None
    response_block: Dict[str, Any] = {"status": status}
    if body is not None:
        response_block["headers"] = {"Content-Type": "application/json"}
        response_block["body"] = body
    route: Dict[str, Any] = {
        "method": method.upper(),
        "path": prefix + op["path"],
        "response": response_block,
    }
    op_id = operation.get("operationId")
    if isinstance(op_id, str) and op_id.strip():
        route["name"] = op_id.strip()
    summary = operation.get("summary") or operation.get("description")
    if summary:
        route["description"] = str(summary).strip().splitlines()[0]
    return route


def _item_schema(op: Optional[Dict[str, Any]], resolver: _RefResolver, swagger2: bool) -> Dict[str, Any]:
    if op is None:
        return {}
    picked = _pick_response(op["operation"].get("responses") or {}, op["method"])
    if not picked:
        return {}
    schema, _example, _has = _response_media(picked[2], resolver, swagger2)
    schema = resolver.resolve(schema)
    if isinstance(schema, dict) and _schema_type(schema)[0] == "array":
        schema = resolver.resolve(schema.get("items") or {})
    return schema if isinstance(schema, dict) else {}


def _detect_resources(operations: List[Dict[str, Any]], resolver: _RefResolver, swagger2: bool,
                      prefix: str) -> Tuple[List[Dict[str, Any]], set]:
    by_path: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for op in operations:
        by_path.setdefault(op["path"], {})[op["method"]] = op

    blocks: List[Dict[str, Any]] = []
    claimed: set = set()
    used_names: set = set()
    for path, methods in by_path.items():
        match = re.fullmatch(r"(.*)/\{([^{}/]+)\}", path)
        if not match:
            continue
        collection, param = match.group(1), match.group(2)
        if "{" in collection.rsplit("/", 1)[-1] or collection not in by_path:
            continue
        coll_ops = by_path[collection]
        if "get" not in methods or not ({"get", "post"} & set(coll_ops)):
            continue

        item_schema = _item_schema(methods.get("get"), resolver, swagger2) or _item_schema(
            coll_ops.get("get"), resolver, swagger2
        )
        props = item_schema.get("properties") or {}
        id_field = param if param in props else ("id" if "id" in props else param)
        id_schema = resolver.resolve(props.get(id_field) or {})
        param_schema = _path_params(methods["get"]["item"], methods["get"]["operation"], resolver,
                                    methods["get"]["mapping"], swagger2).get(param, {})
        id_kind = _schema_type(id_schema)[0] or _schema_type(param_schema)[0]
        id_type = "int" if id_kind == "integer" else "uuid"

        seed = _resource_seed(coll_ops.get("get"), item_schema, id_field, id_type, resolver, swagger2)
        name = _collection_name(collection)
        base_name, n = name, 2
        while name in used_names:
            name, n = f"{base_name}{n}", n + 1
        used_names.add(name)
        block: Dict[str, Any] = {
            "name": name,
            "path": prefix + collection,
            "id_field": id_field,
            "id_type": id_type,
            "seed": seed,
        }
        blocks.append(block)
        for method in ("get", "post"):
            if method in coll_ops:
                claimed.add((method, collection))
        for method in ("get", "put", "patch", "delete"):
            if method in methods:
                claimed.add((method, path))
    return blocks, claimed


def _resource_seed(list_op: Optional[Dict[str, Any]], item_schema: Dict[str, Any], id_field: str,
                   id_type: str, resolver: _RefResolver, swagger2: bool) -> List[Dict[str, Any]]:
    if list_op is not None:
        picked = _pick_response(list_op["operation"].get("responses") or {}, "get")
        if picked:
            _schema, example, has_example = _response_media(picked[2], resolver, swagger2)
            if has_example and isinstance(example, list) and example and all(isinstance(i, dict) for i in example):
                seen: set = set()
                unique = []
                for item in example:
                    key = str(item.get(id_field))
                    if id_field in item and key in seen:
                        continue
                    seen.add(key)
                    unique.append(item)
                return unique
    template = example_from_schema(item_schema, resolver) if item_schema else None
    if not isinstance(template, dict):
        return []
    seed = []
    for i in range(1, 4):
        item = copy.deepcopy(template)
        if id_type == "int":
            item[id_field] = i
        else:
            item.pop(id_field, None)  # the store mints a uuid
        seed.append(item)
    return seed


def write_mocks_yaml(
    config: Dict[str, Any],
    spec_path: str,
    out_path: str,
    dynamic: bool = False,
    resources: bool = False,
) -> None:
    """Write an imported config as commented, readable YAML."""
    modes = [m for m, on in (("dynamic", dynamic), ("resources", resources)) if on]
    header = [
        "# Generated by: mockserver import-openapi",
        f"# Source: {os.path.basename(spec_path)}",
    ]
    if modes:
        header.append(f"# Modes: {', '.join(modes)}")
    header.append("# Review the bodies below, then check the file with: mockserver validate")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(header) + "\n\n")
        yaml.safe_dump(config, fh, sort_keys=False, allow_unicode=True, default_flow_style=False)


def import_openapi_to_yaml(
    spec_path: str,
    out_path: str,
    base_path: Optional[str] = None,
    dynamic: bool = False,
    resources: bool = False,
) -> int:
    """Import a spec and write mocks YAML. Returns the number of routes + resources."""
    config = import_openapi(spec_path, base_path=base_path, dynamic=dynamic, resources=resources)
    write_mocks_yaml(config, spec_path, out_path, dynamic=dynamic, resources=resources)
    return len(config["routes"]) + len(config.get("resources") or [])
