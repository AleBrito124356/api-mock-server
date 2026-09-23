"""In-memory CRUD collections.

Declare a resource in the mocks file:

    resources:
      - name: todos
        path: /todos
        id_field: id
        id_type: int          # or "uuid"
        seed:
          - {id: 1, title: "Ship the mock server", done: false}

and you get the full REST surface for free, backed by a dict:

    GET    /todos            list  (supports ?field=value, _limit, _offset,
                                    _sort, _order, _page, _per_page)
    GET    /todos/{id}       one   (404 if missing)
    POST   /todos            create (auto-assigns the id, returns 201 +
                                    Location; 409 if the body reuses an id)
    PUT    /todos/{id}       replace (404 if missing)
    PATCH  /todos/{id}       partial update (404 if missing)
    DELETE /todos/{id}       delete (204, or 404 if missing)
    OPTIONS                  204 with an Allow header

Write bodies must be JSON objects; anything else is a 400, never a crash.

State lives in the process and resets on restart. Perfect for prototyping a
frontend against a backend that does not exist yet.
"""
from __future__ import annotations

import copy
import re
import uuid as _uuid
from typing import Any, Dict, List, Optional, Tuple

from .config import ResourceSpec, compile_path

# Query params that control listing rather than filter fields.
_CONTROL = {"_limit", "_offset", "_sort", "_order", "_page", "_per_page", "_q"}

_COLLECTION_ALLOW = "GET, HEAD, POST, OPTIONS"
_ITEM_ALLOW = "GET, HEAD, PUT, PATCH, DELETE, OPTIONS"


class ResourceConflict(Exception):
    """A create tried to reuse an id that already exists."""

    def __init__(self, item_id: Any) -> None:
        super().__init__(f"id {item_id!r} already exists")
        self.item_id = item_id


class ResourceStore:
    """A single collection backed by an ordered dict of items."""

    def __init__(self, spec: ResourceSpec) -> None:
        self.spec = spec
        self.id_field = spec.id_field
        self.id_type = spec.id_type
        self._items: Dict[str, Dict[str, Any]] = {}
        self._auto = 0
        for item in spec.seed:
            self._seed_item(copy.deepcopy(item))

    # -- id handling ------------------------------------------------------ #
    def _key(self, raw_id: Any) -> str:
        return str(raw_id)

    def _next_id(self) -> Any:
        if self.id_type == "uuid":
            return str(_uuid.uuid4())
        self._auto += 1
        return self._auto

    def _seed_item(self, item: Dict[str, Any]) -> None:
        if self.id_field not in item:
            item[self.id_field] = self._next_id()
        raw = item[self.id_field]
        if self.id_type == "int":
            try:
                self._auto = max(self._auto, int(raw))
            except (TypeError, ValueError):
                pass
        self._items[self._key(raw)] = item

    def coerce_id(self, raw: str) -> Any:
        if self.id_type == "int":
            try:
                return int(raw)
            except (TypeError, ValueError):
                return raw
        return raw

    # -- CRUD ------------------------------------------------------------- #
    def list(self, query: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int]:
        items = list(self._items.values())

        # Free-text search across stringified values.
        term = query.get("_q")
        if term:
            needle = str(term).lower()
            items = [it for it in items if needle in " ".join(str(v) for v in it.values()).lower()]

        # Field equality filters.
        for key, value in query.items():
            if key in _CONTROL:
                continue
            items = [it for it in items if _field_equals(it.get(key), value)]

        total = len(items)

        # Sorting.
        sort_field = query.get("_sort")
        if sort_field:
            reverse = str(query.get("_order", "asc")).lower() == "desc"
            items = sorted(items, key=lambda it: _sort_key(it.get(sort_field)), reverse=reverse)

        # Pagination: either _offset/_limit or _page/_per_page.
        offset = _to_int(query.get("_offset"), 0)
        limit = _to_int(query.get("_limit"), None)
        if "_page" in query or "_per_page" in query:
            per = _to_int(query.get("_per_page"), 10) or 10
            page = max(1, _to_int(query.get("_page"), 1) or 1)
            offset = (page - 1) * per
            limit = per
        if offset:
            items = items[offset:]
        if limit is not None:
            items = items[:limit]

        return items, total

    def get(self, raw_id: Any) -> Optional[Dict[str, Any]]:
        return self._items.get(self._key(raw_id))

    def create(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Insert a new item. Raises :class:`ResourceConflict` on a duplicate id."""
        item = dict(data or {})
        if self.id_field not in item or item[self.id_field] in (None, ""):
            item[self.id_field] = self._next_id()
        else:
            if self._key(item[self.id_field]) in self._items:
                raise ResourceConflict(item[self.id_field])
            # Honor a client-supplied numeric id but keep the counter ahead.
            if self.id_type == "int":
                try:
                    self._auto = max(self._auto, int(item[self.id_field]))
                except (TypeError, ValueError):
                    pass
        self._items[self._key(item[self.id_field])] = item
        return item

    def replace(self, raw_id: Any, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        key = self._key(raw_id)
        if key not in self._items:
            return None
        item = dict(data or {})
        item[self.id_field] = self.coerce_id(str(raw_id)) if self.id_type == "int" else raw_id
        self._items[key] = item
        return item

    def update(self, raw_id: Any, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        key = self._key(raw_id)
        existing = self._items.get(key)
        if existing is None:
            return None
        merged = dict(existing)
        merged.update(data or {})
        merged[self.id_field] = existing[self.id_field]
        self._items[key] = merged
        return merged

    def delete(self, raw_id: Any) -> bool:
        return self._items.pop(self._key(raw_id), None) is not None


def _field_equals(field_value: Any, query_value: Any) -> bool:
    """Compare a stored field against a query-string value.

    Booleans need special handling because JSON serializes them lowercase
    (``true``/``false``) while ``str(True)`` is ``"True"``.
    """
    q = str(query_value)
    if isinstance(field_value, bool):
        return (field_value and q.lower() in ("true", "1")) or (
            not field_value and q.lower() in ("false", "0")
        )
    return str(field_value) == q


def _to_int(value: Any, default: Optional[int]) -> Optional[int]:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sort_key(value: Any) -> Tuple[int, Any]:
    """Sort None last and keep numbers/strings comparable across types."""
    if value is None:
        return (2, 0)
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (0, value)
    return (1, str(value))


def _data_identity(spec: ResourceSpec) -> Tuple[Any, ...]:
    """What defines a store's data (latency/chaos changes keep the data)."""
    return (spec.name, spec.path, spec.id_field, spec.id_type, repr(spec.seed))


class ResourceRouter:
    """Maps an incoming method+path to a resource operation.

    Pass ``previous`` (the router being replaced on a hot reload) to keep the
    in-memory data of every resource whose definition did not change.
    """

    def __init__(self, specs: List[ResourceSpec], previous: Optional["ResourceRouter"] = None) -> None:
        self.stores: Dict[str, ResourceStore] = {}
        self._entries: List[Tuple[ResourceSpec, "re.Pattern[str]", "re.Pattern[str]"]] = []
        self.kept: List[str] = []
        for spec in specs:
            old = previous.stores.get(spec.name) if previous is not None else None
            if old is not None and _data_identity(old.spec) == _data_identity(spec):
                old.spec = spec
                self.stores[spec.name] = old
                self.kept.append(spec.name)
            else:
                self.stores[spec.name] = ResourceStore(spec)
            coll_re, _ = compile_path(spec.path)
            item_re, _ = compile_path(spec.path.rstrip("/") + "/{__rid__}")
            self._entries.append((spec, coll_re, item_re))

    def match(self, method: str, path: str) -> Optional[Dict[str, Any]]:
        """Return a dict describing the operation, or None if no resource matches."""
        for spec, coll_re, item_re in self._entries:
            if coll_re.match(path):
                if method in ("GET", "HEAD"):
                    return {"spec": spec, "kind": "list"}
                if method == "POST":
                    return {"spec": spec, "kind": "create"}
                if method == "OPTIONS":
                    return {"spec": spec, "kind": "options", "allow": _COLLECTION_ALLOW}
                return {"spec": spec, "kind": "method_not_allowed", "allow": _COLLECTION_ALLOW}
            m = item_re.match(path)
            if m:
                raw_id = m.group("__rid__")
                store = self.stores[spec.name]
                item_id = store.coerce_id(raw_id)
                if method in ("GET", "HEAD"):
                    return {"spec": spec, "kind": "get", "id": item_id}
                if method == "PUT":
                    return {"spec": spec, "kind": "replace", "id": item_id}
                if method == "PATCH":
                    return {"spec": spec, "kind": "update", "id": item_id}
                if method == "DELETE":
                    return {"spec": spec, "kind": "delete", "id": item_id}
                if method == "OPTIONS":
                    return {"spec": spec, "kind": "options", "allow": _ITEM_ALLOW}
                return {"spec": spec, "kind": "method_not_allowed", "allow": _ITEM_ALLOW}
        return None

    def store_for(self, spec: ResourceSpec) -> ResourceStore:
        return self.stores[spec.name]
