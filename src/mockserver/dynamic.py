"""Response templating.

Responses in a mocks file can contain ``{{ ... }}`` expressions that are
evaluated per request so mock data looks real and varies call to call.

Supported roots:

    request.query.<key>     value from the query string
    request.path.<key>      value of a {param} captured from the path
    request.header.<name>   request header (case-insensitive)
    request.body.<a.b.c>    nested field from the parsed JSON body
    request.method          the HTTP method

    faker.<helper>(args)    fake data: name, email, uuid, int, float, city ...
    seq.<name>(start, step) an incrementing counter, persistent per server
    now / now.<part>        current time (iso, date, time, timestamp, year ...)
    uuid                    a random uuid4 string
    env.<VAR>               an environment variable

Literals are allowed too: ``{{ 'text' }}``, ``{{ 42 }}``, ``{{ true }}``,
``{{ null }}``.

Filters are chained with ``|``: ``{{ request.query.page | default(1) | int }}``.

If a value is *exactly* one ``{{ expr }}`` its native type is preserved
(so ``{{ faker.int(1, 5) }}`` yields a JSON number, not a string). A string
with text around the token, or with several tokens, is interpolated into a
string: ``"{{ faker.first_name }} {{ faker.last_name }}"``.

A broken expression (an unknown faker helper or filter, or arguments of the
wrong type) raises :class:`TemplateError`, which the server turns into a JSON
500 that names the expression, instead of silently rendering ``null``.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import time
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from random import Random
from typing import Any, Dict, List, Optional, Tuple

_TOKEN = re.compile(r"\{\{\s*(.*?)\s*\}\}", re.S)
_ATOM = re.compile(r"^([A-Za-z_][\w]*(?:\.[A-Za-z_][\w-]*)*)\s*(?:\((.*)\))?\s*$", re.S)
_FILTER = re.compile(r"^([A-Za-z_]\w*)\s*(?:\((.*)\))?$", re.S)

#: Roots an expression may start with (besides literals).
KNOWN_ROOTS = ("request", "faker", "seq", "now", "uuid", "env")
#: Identifiers that are parsed as literals rather than roots.
LITERAL_WORDS = ("true", "false", "null", "none")
#: ``request.<scope>`` names.
REQUEST_SCOPES = ("method", "query", "path", "header", "headers", "body", "json")
#: ``now.<part>`` names.
NOW_PARTS = ("iso", "timestamp", "date", "time", "year", "month", "day")
#: Filters usable after ``|``.
KNOWN_FILTERS = ("default", "upper", "lower", "title", "int", "float", "round", "json")


class TemplateError(ValueError):
    """A ``{{ ... }}`` expression could not be evaluated."""

    def __init__(self, message: str, expression: Optional[str] = None) -> None:
        self.expression = expression
        detail = f"{message} (in '{{{{ {expression} }}}}')" if expression else message
        super().__init__(detail)


class MockFaker:
    """A tiny, seedable fake-data generator with no external dependencies."""

    _FIRST = [
        "Ava", "Liam", "Sofia", "Noah", "Maya", "Diego", "Emma", "Mateo",
        "Olivia", "Lucas", "Isabella", "Ethan", "Camila", "Leo", "Zoe", "Hugo",
    ]
    _LAST = [
        "Brito", "Nguyen", "Garcia", "Smith", "Okafor", "Rossi", "Haddad",
        "Kim", "Silva", "Muller", "Torres", "Ivanova", "Chen", "Ali", "Novak",
    ]
    _DOMAINS = ["example.com", "example.org", "test.dev", "mail.example"]
    _CITIES = [
        "Panama City", "Lisbon", "Austin", "Nairobi", "Osaka", "Bogota",
        "Berlin", "Toronto", "Manila", "Porto",
    ]
    _COUNTRIES = ["Panama", "Portugal", "Kenya", "Japan", "Colombia", "Canada"]
    _COMPANIES = [
        "Norden", "Acme", "Globex", "Umbrella", "Initech", "Hooli",
        "Stark Labs", "Wayne Co", "Wonka", "Cyberdyne",
    ]
    _WORDS = [
        "alpha", "signal", "orbit", "delta", "harbor", "quartz", "meadow",
        "cascade", "vector", "ember", "lattice", "pixel", "nimbus", "cobalt",
    ]
    _COLORS = ["#2563EB", "#16A34A", "#DC2626", "#9333EA", "#EA580C", "#0891B2"]
    _STATUSES = ["active", "pending", "archived", "draft"]

    def __init__(self, rng: Random) -> None:
        self.rng = rng

    def first_name(self) -> str:
        return self.rng.choice(self._FIRST)

    def last_name(self) -> str:
        return self.rng.choice(self._LAST)

    def name(self) -> str:
        return f"{self.first_name()} {self.last_name()}"

    def username(self) -> str:
        return f"{self.first_name().lower()}.{self.last_name().lower()}{self.rng.randint(1, 99)}"

    def email(self) -> str:
        return f"{self.first_name().lower()}.{self.last_name().lower()}@{self.rng.choice(self._DOMAINS)}"

    def uuid(self) -> str:
        # version=4 sets the version/variant bits so the value is a valid uuid4.
        return str(_uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def int(self, lo: int = 0, hi: int = 100) -> int:
        return self.rng.randint(int(lo), int(hi))

    def float(self, lo: float = 0.0, hi: float = 1.0, ndigits: int = 2) -> float:
        return round(self.rng.uniform(float(lo), float(hi)), int(ndigits))

    def price(self, lo: float = 1.0, hi: float = 999.0) -> float:
        return round(self.rng.uniform(float(lo), float(hi)), 2)

    def bool(self) -> bool:
        return self.rng.random() < 0.5

    def choice(self, *options: Any) -> Any:
        if not options:
            raise ValueError("faker.choice needs at least one option")
        return self.rng.choice(list(options))

    def word(self) -> str:
        return self.rng.choice(self._WORDS)

    def words(self, n: int = 3) -> str:
        return " ".join(self.rng.choice(self._WORDS) for _ in range(int(n)))

    def sentence(self, words: int = 6) -> str:
        s = " ".join(self.rng.choice(self._WORDS) for _ in range(int(words)))
        return s.capitalize() + "."

    def paragraph(self, sentences: int = 3) -> str:
        return " ".join(self.sentence() for _ in range(int(sentences)))

    def city(self) -> str:
        return self.rng.choice(self._CITIES)

    def country(self) -> str:
        return self.rng.choice(self._COUNTRIES)

    def company(self) -> str:
        return self.rng.choice(self._COMPANIES)

    def color(self) -> str:
        return self.rng.choice(self._COLORS)

    def status(self) -> str:
        return self.rng.choice(self._STATUSES)

    def phone(self) -> str:
        return f"+507 {self.rng.randint(6000, 6999)}-{self.rng.randint(1000, 9999)}"

    def slug(self) -> str:
        return f"{self.word()}-{self.word()}-{self.rng.randint(100, 999)}"

    def url(self) -> str:
        return f"https://{self.rng.choice(self._DOMAINS)}/{self.slug()}"

    def ipv4(self) -> str:
        return "192.0.2.%d" % self.rng.randint(1, 254)

    def _rand_date(self, lo_days: int, hi_days: int) -> date:
        offset = self.rng.randint(int(lo_days), int(hi_days))
        return date.today() + timedelta(days=offset)

    def date(self, past_days: int = 365, future_days: int = 0) -> str:
        return self._rand_date(-int(past_days), int(future_days)).isoformat()

    def past_date(self, days: int = 365) -> str:
        return self._rand_date(-int(days), -1).isoformat()

    def future_date(self, days: int = 365) -> str:
        return self._rand_date(1, int(days)).isoformat()

    def datetime(self, past_days: int = 365) -> str:
        d = self._rand_date(-int(past_days), 0)
        t = timedelta(seconds=self.rng.randint(0, 86399))
        return (datetime(d.year, d.month, d.day) + t).isoformat() + "Z"


def faker_helpers() -> List[str]:
    """Public helper names callable as ``faker.<name>``."""
    return sorted(
        name for name, member in inspect.getmembers(MockFaker, inspect.isfunction)
        if not name.startswith("_")
    )


class TemplateEngine:
    """Holds faker state and named sequences for the life of the server."""

    def __init__(self, seed: Optional[int] = None) -> None:
        self.seed = seed
        self.faker = MockFaker(Random(seed))
        self.sequences: Dict[str, int] = {}

    def reset(self) -> None:
        """Rewind the RNG to its seed and clear every sequence counter."""
        self.faker = MockFaker(Random(self.seed))
        self.sequences = {}

    def next_seq(self, name: str, start: int = 1, step: int = 1) -> int:
        if name not in self.sequences:
            self.sequences[name] = int(start)
        else:
            self.sequences[name] += int(step)
        return self.sequences[name]

    def render(self, value: Any, request_ctx: Optional[Dict[str, Any]] = None) -> Any:
        return render(value, request_ctx or {}, self)


# --------------------------------------------------------------------------- #
# Expression parsing
# --------------------------------------------------------------------------- #

def _split_top(text: str, sep: str) -> List[str]:
    """Split on ``sep`` ignoring separators inside quotes or parentheses."""
    out: List[str] = []
    buf: List[str] = []
    depth = 0
    quote: Optional[str] = None
    for ch in text:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
            continue
        if ch == ")":
            depth -= 1
            buf.append(ch)
            continue
        if ch == sep and depth == 0:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf))
    return out


def _parse_literal(token: str) -> Any:
    token = token.strip()
    if len(token) >= 2 and token[0] in "'\"" and token[-1] == token[0]:
        return token[1:-1]
    low = token.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none", ""):
        return None
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return token


def _parse_args(arg_str: Optional[str]) -> List[Any]:
    if arg_str is None or arg_str.strip() == "":
        return []
    return [_parse_literal(part) for part in _split_top(arg_str, ",")]


@dataclass
class ParsedExpression:
    """The structure of one ``{{ ... }}`` expression.

    ``kind`` is ``"ref"`` for ``root.a.b(args)`` references and ``"literal"``
    for quoted strings, numbers and ``true``/``false``/``null``. Unknown bare
    identifiers still evaluate to their own text (backwards compatible), but
    ``mockserver validate`` reports them because they are nearly always typos.
    """

    source: str
    kind: str
    root: Optional[str] = None
    rest: List[str] = field(default_factory=list)
    args: List[Any] = field(default_factory=list)
    has_call: bool = False
    literal: Any = None
    filters: List[Tuple[str, List[Any]]] = field(default_factory=list)
    bad_filters: List[str] = field(default_factory=list)


def parse_expression(expr: str) -> ParsedExpression:
    """Parse an expression (the text between ``{{`` and ``}}``)."""
    parts = _split_top(expr, "|")
    head = parts[0].strip()
    parsed: ParsedExpression
    match = _ATOM.match(head)
    if match and match.group(1).split(".")[0].lower() not in LITERAL_WORDS:
        segs = match.group(1).split(".")
        arg_str = match.group(2)
        parsed = ParsedExpression(
            source=expr,
            kind="ref",
            root=segs[0],
            rest=segs[1:],
            args=_parse_args(arg_str) if arg_str is not None else [],
            has_call=arg_str is not None,
        )
    else:
        parsed = ParsedExpression(source=expr, kind="literal", literal=_parse_literal(head))
    for raw in parts[1:]:
        fm = _FILTER.match(raw.strip())
        if not fm:
            parsed.bad_filters.append(raw.strip())
            continue
        f_args = _parse_args(fm.group(2)) if fm.group(2) is not None else []
        parsed.filters.append((fm.group(1), f_args))
    return parsed


def find_expressions(text: str) -> List[str]:
    """Return the inner text of every ``{{ ... }}`` token in ``text``."""
    return [m.group(1).strip() for m in _TOKEN.finditer(text)]


def single_expression(text: str) -> Optional[str]:
    """If ``text`` is exactly one token (ignoring outer whitespace), return it."""
    stripped = text.strip()
    tokens = list(_TOKEN.finditer(stripped))
    if len(tokens) == 1 and tokens[0].span() == (0, len(stripped)):
        return tokens[0].group(1).strip()
    return None


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #

def _resolve_request(rest: List[str], req: Dict[str, Any]) -> Any:
    if not rest:
        return None
    scope = rest[0]
    keys = rest[1:]
    if scope == "method":
        return req.get("method")
    if scope in ("header", "headers"):
        headers = req.get("headers") or {}
        return headers.get(keys[0].lower()) if keys else None
    if scope == "query":
        return (req.get("query") or {}).get(keys[0]) if keys else None
    if scope == "path":
        return (req.get("path") or {}).get(keys[0]) if keys else None
    if scope in ("body", "json"):
        node: Any = req.get("json") if req.get("json") is not None else req.get("body")
        for k in keys:
            if isinstance(node, dict):
                node = node.get(k)
            elif isinstance(node, list):
                try:
                    node = node[int(k)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return node
    return None


def _resolve_faker(parsed: ParsedExpression, engine: TemplateEngine) -> Any:
    if not parsed.rest:
        raise TemplateError("'faker' needs a helper name, e.g. faker.name", parsed.source)
    name = parsed.rest[0]
    method = getattr(engine.faker, name, None) if not name.startswith("_") else None
    if method is None or not callable(method):
        raise TemplateError(f"unknown faker helper '{name}'", parsed.source)
    try:
        return method(*parsed.args)
    except (TypeError, ValueError) as exc:
        raise TemplateError(f"faker.{name}{tuple(parsed.args)!r} failed: {exc}", parsed.source) from exc


def _resolve_seq(parsed: ParsedExpression, engine: TemplateEngine) -> Any:
    if not parsed.rest:
        raise TemplateError("'seq' needs a counter name, e.g. seq.order", parsed.source)
    args = parsed.args
    try:
        start = int(args[0]) if len(args) >= 1 else 1
        step = int(args[1]) if len(args) >= 2 else 1
    except (TypeError, ValueError) as exc:
        raise TemplateError(f"seq arguments must be integers, got {tuple(args)!r}", parsed.source) from exc
    return engine.next_seq(parsed.rest[0], start, step)


def _resolve_now(rest: List[str], args: List[Any]) -> Any:
    now = datetime.now()
    if args:
        return now.strftime(str(args[0]))
    if not rest:
        return now.isoformat(timespec="seconds")
    part = rest[0]
    if part == "iso":
        return now.isoformat(timespec="seconds")
    if part == "timestamp":
        return int(time.time())
    if part == "date":
        return now.date().isoformat()
    if part == "time":
        return now.time().strftime("%H:%M:%S")
    if part == "year":
        return now.year
    if part == "month":
        return now.month
    if part == "day":
        return now.day
    return now.isoformat(timespec="seconds")


def _eval_parsed(parsed: ParsedExpression, req: Dict[str, Any], engine: TemplateEngine) -> Any:
    if parsed.kind == "literal":
        return parsed.literal
    root, rest = parsed.root, parsed.rest
    if root == "request":
        return _resolve_request(rest, req)
    if root == "faker":
        return _resolve_faker(parsed, engine)
    if root == "seq":
        return _resolve_seq(parsed, engine)
    if root == "now":
        return _resolve_now(rest, parsed.args)
    if root == "uuid":
        return engine.faker.uuid()
    if root == "env":
        key = rest[0] if rest else (str(parsed.args[0]) if parsed.args else "")
        return os.environ.get(key)
    # Not a known root: keep the historical behaviour of treating the bare
    # identifier as a literal string. `mockserver validate` flags these.
    return ".".join([root or ""] + list(rest))


def _apply_filter(value: Any, name: str, args: List[Any], source: str) -> Any:
    if name == "default":
        return value if value not in (None, "") else (args[0] if args else "")
    if name == "upper":
        return str(value).upper()
    if name == "lower":
        return str(value).lower()
    if name == "title":
        return str(value).title()
    if name == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return value
    if name == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if name == "round":
        try:
            return round(float(value), int(args[0]) if args else 0)
        except (TypeError, ValueError):
            return value
    if name == "json":
        return json.dumps(value)
    raise TemplateError(f"unknown filter '{name}' (known: {', '.join(KNOWN_FILTERS)})", source)


def evaluate(expr: str, req: Dict[str, Any], engine: TemplateEngine) -> Any:
    parsed = parse_expression(expr)
    if parsed.bad_filters:
        raise TemplateError(f"cannot parse filter '{parsed.bad_filters[0]}'", expr)
    value = _eval_parsed(parsed, req, engine)
    for name, args in parsed.filters:
        value = _apply_filter(value, name, args, expr)
    return value


def render_string(text: str, req: Dict[str, Any], engine: TemplateEngine) -> Any:
    single = single_expression(text)
    if single is not None:
        # Whole string is a single expression: keep the native type.
        return evaluate(single, req, engine)

    def repl(match: "re.Match[str]") -> str:
        value = evaluate(match.group(1).strip(), req, engine)
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return str(value)

    return _TOKEN.sub(repl, text)


def render(value: Any, req: Dict[str, Any], engine: TemplateEngine) -> Any:
    if isinstance(value, str):
        return render_string(value, req, engine)
    if isinstance(value, dict):
        return {k: render(v, req, engine) for k, v in value.items()}
    if isinstance(value, list):
        return [render(item, req, engine) for item in value]
    return value
