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

Filters are chained with ``|``: ``{{ request.query.page | default(1) | int }}``.

If a value is *exactly* one ``{{ expr }}`` its native type is preserved
(so ``{{ faker.int(1, 5) }}`` yields a JSON number, not a string). Embedded
expressions are stringified.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid as _uuid
from datetime import date, datetime, timedelta
from random import Random
from typing import Any, Dict, List, Optional

_TOKEN = re.compile(r"\{\{\s*(.*?)\s*\}\}", re.S)
_FULL = re.compile(r"^\{\{\s*(.*?)\s*\}\}$", re.S)
_ATOM = re.compile(r"^([A-Za-z_][\w]*(?:\.[A-Za-z_][\w-]*)*)\s*(?:\((.*)\))?\s*$", re.S)


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
        return str(_uuid.UUID(int=self.rng.getrandbits(128)))

    def int(self, lo: int = 0, hi: int = 100) -> int:
        return self.rng.randint(int(lo), int(hi))

    def float(self, lo: float = 0.0, hi: float = 1.0, ndigits: int = 2) -> float:
        return round(self.rng.uniform(float(lo), float(hi)), int(ndigits))

    def price(self, lo: float = 1.0, hi: float = 999.0) -> float:
        return round(self.rng.uniform(float(lo), float(hi)), 2)

    def bool(self) -> bool:
        return self.rng.random() < 0.5

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


class TemplateEngine:
    """Holds faker state and named sequences for the life of the server."""

    def __init__(self, seed: Optional[int] = None) -> None:
        self.faker = MockFaker(Random(seed))
        self.sequences: Dict[str, int] = {}

    def next_seq(self, name: str, start: int = 1, step: int = 1) -> int:
        if name not in self.sequences:
            self.sequences[name] = int(start)
        else:
            self.sequences[name] += int(step)
        return self.sequences[name]

    def render(self, value: Any, request_ctx: Optional[Dict[str, Any]] = None) -> Any:
        return render(value, request_ctx or {}, self)


# --------------------------------------------------------------------------- #
# Expression parsing / evaluation
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


def _resolve_faker(rest: List[str], args: List[Any], engine: TemplateEngine) -> Any:
    if not rest:
        return None
    method = getattr(engine.faker, rest[0], None)
    if method is None or not callable(method):
        return None
    return method(*args)


def _resolve_seq(rest: List[str], args: List[Any], engine: TemplateEngine) -> Any:
    if not rest:
        return None
    name = rest[0]
    start = args[0] if len(args) >= 1 else 1
    step = args[1] if len(args) >= 2 else 1
    return engine.next_seq(name, int(start), int(step))


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


def _eval_atom(expr: str, req: Dict[str, Any], engine: TemplateEngine) -> Any:
    match = _ATOM.match(expr)
    if not match:
        return _parse_literal(expr)
    path = match.group(1)
    arg_str = match.group(2)
    args = _parse_args(arg_str) if arg_str is not None else []
    segs = path.split(".")
    root, rest = segs[0], segs[1:]

    if root == "request":
        return _resolve_request(rest, req)
    if root == "faker":
        return _resolve_faker(rest, args, engine)
    if root == "seq":
        return _resolve_seq(rest, args, engine)
    if root == "now":
        return _resolve_now(rest, args)
    if root == "uuid":
        return engine.faker.uuid()
    if root == "env":
        key = rest[0] if rest else (str(args[0]) if args else "")
        return os.environ.get(key)
    # Not a known root: treat the whole thing as a literal.
    return _parse_literal(path)


def _apply_filter(value: Any, filter_expr: str) -> Any:
    match = re.match(r"^([A-Za-z_]\w*)\s*(?:\((.*)\))?$", filter_expr.strip(), re.S)
    if not match:
        return value
    name = match.group(1)
    args = _parse_args(match.group(2)) if match.group(2) is not None else []

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
    return value


def evaluate(expr: str, req: Dict[str, Any], engine: TemplateEngine) -> Any:
    parts = _split_top(expr, "|")
    value = _eval_atom(parts[0].strip(), req, engine)
    for f in parts[1:]:
        value = _apply_filter(value, f)
    return value


def render_string(text: str, req: Dict[str, Any], engine: TemplateEngine) -> Any:
    stripped = text.strip()
    full = _FULL.match(stripped)
    if full:
        # Whole string is a single expression: keep the native type.
        return evaluate(full.group(1).strip(), req, engine)

    def repl(match: "re.Match[str]") -> str:
        value = evaluate(match.group(1).strip(), req, engine)
        if value is None:
            return ""
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
