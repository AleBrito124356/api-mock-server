# api-mock-server

**A config-driven mock REST API for frontend dev and testing.** Define endpoints in YAML, get templated dynamic responses, latency and error injection, stateful CRUD resources, OpenAPI import, and record-and-replay — no handler code.

![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Starlette](https://img.shields.io/badge/ASGI-Starlette-ff69b4)
![Tests](https://img.shields.io/badge/tests-38%20passing-brightgreen)

---

## Why

You are building a frontend and the backend does not exist yet. Or it exists but is slow, flaky, or hard to seed into the exact state your UI needs. So you hand-roll an Express server full of `res.json(...)` stubs, and three weeks later it is a second codebase nobody maintains.

`api-mock-server` replaces that with a single YAML file. You declare methods, paths, and responses. You get:

- **Dynamic bodies** — `{{ faker.name }}`, `{{ seq.order }}`, `{{ request.path.id }}` so mock data looks real and varies per call instead of being one frozen fixture.
- **Chaos on purpose** — inject latency and errors to test how your UI behaves on a bad network, seeded so failures are reproducible.
- **Stateful CRUD** — declare a resource and get `GET/POST/PUT/PATCH/DELETE` backed by an in-memory store, so a prototype can actually create and edit records.
- **OpenAPI import** — turn a contract into a runnable mock in one command.
- **Record and replay** — proxy a real upstream once, then work offline against saved fixtures.

It is a small, dependency-light Python package on Starlette. The whole thing is driven by config, so a designer or a QA engineer can change a response without touching code.

## How it works

Every request goes through one match pipeline. Explicit routes are tried first, in priority order; then stateful resources; then record/replay; then a helpful 404.

```mermaid
flowchart TD
    A[Incoming request] --> B{Explicit route match}
    B -- "method + path + query/header/body" --> C[Behavior gate]
    B -- no match --> D{Stateful resource match}
    C --> C1[Rate limit check]
    C1 --> C2[Error injection]
    C2 --> C3[Latency delay]
    C3 --> C4[Render template body]
    C4 --> Z[Response]
    D -- collection or item --> E[CRUD on in-memory store]
    E --> Z
    D -- no match --> F{Record mode configured}
    F -- fixture exists --> G[Replay saved fixture]
    F -- miss + upstream --> H[Proxy upstream and save fixture]
    F -- no --> I[404 with hint]
    G --> Z
    H --> Z
    I --> Z
```

Routes are ordered by `priority` descending, then by declaration order, and the **first** route whose method, path, and match conditions all satisfy wins. That is how a specific `/products?category=books` route can shadow the generic `/products` list.

## Quickstart

```bash
git clone https://github.com/AleBrito124356/api-mock-server
cd api-mock-server
pip install -r requirements.txt        # or: pip install -e .

# Optional: defaults so you can skip repeating flags.
cp .env.example .env

# Serve one of the bundled examples.
python cli.py serve --config examples/shop-api/mocks.yaml
# -> api-mock-server serving ... on http://127.0.0.1:8000
```

Installing the package (`pip install -e .`) also gives you a `mockserver` command, so `mockserver serve ...` works from anywhere.

## Usage

### Serve mocks

```bash
mockserver serve --config examples/shop-api/mocks.yaml --port 8000 --seed 7
```

```bash
$ curl -s localhost:8000/products | jq '.results[0]'
{
  "id": 1,
  "name": "Quartz Orbit",
  "price": 200.44,
  "in_stock": true,
  "sku": "cobalt-vector-196"
}

# A query-specific route wins over the generic list via priority:
$ curl -s 'localhost:8000/products?category=books' -D - | grep -i x-matched
x-matched-route: products-books

# Path params flow into the body; single-token templates keep their JSON type:
$ curl -s localhost:8000/products/42 | jq '.id'
42

# Create endpoints can echo the request body and mint ids:
$ curl -s -X POST localhost:8000/orders \
       -H 'content-type: application/json' -d '{"item":"Keyboard","qty":2}' | jq
{ "id": 1000, "status": "pending", "item": "Keyboard", "qty": 2, ... }

# Chaos: ~40% of these fail with a 503, seeded so runs are reproducible:
$ for i in 1 2 3 4 5; do curl -s -o /dev/null -w "%{http_code} " localhost:8000/flaky; done
503 200 200 503 503
```

### Stateful CRUD

```bash
mockserver serve --config examples/todo-api/mocks.yaml
```

```bash
$ curl -s -X POST localhost:8000/todos \
       -H 'content-type: application/json' -d '{"title":"Wire up the API","done":false}'
{ "title": "Wire up the API", "done": false, "id": 4 }     # 201 Created, Location: /todos/4

$ curl -s 'localhost:8000/todos?done=false&_sort=id&_limit=2'   # filter, sort, paginate
$ curl -s -X PATCH localhost:8000/todos/4 -d '{"done":true}' -H 'content-type: application/json'
$ curl -s -X DELETE localhost:8000/todos/4 -i                    # 204 No Content
```

### Import an OpenAPI 3 spec

```bash
mockserver import-openapi examples/openapi-import/petstore.yaml -o petstore.mocks.yaml
mockserver serve --config petstore.mocks.yaml
```

Response bodies are taken from the spec's response examples, then schema examples, then synthesized from the schema (respecting `example`, `default`, `enum`, and `format`).

### Record and replay

```bash
# Proxy everything unmatched to the real API and save responses as fixtures.
mockserver record --upstream https://api.example.com --fixtures ./fixtures --port 8000

# Later, work offline: point at the same fixtures dir with no upstream and it replays.
```

Fixtures are plain JSON on disk, one file per request, so they are easy to inspect, edit, and commit.

## mocks.yaml reference

```yaml
config:                     # global settings (all optional)
  seed: 7                   # deterministic faker data + chaos
  cors: true                # add permissive CORS headers + handle preflight
  latency:                  # default delay for every route
    fixed_ms: 0
  chaos:                    # default error/rate-limit behavior
    error_rate: 0.0

routes:                     # explicit endpoints, tried first
  - method: GET             # GET | POST | PUT | PATCH | DELETE | ANY
    path: /products/{id}    # {param} segments are captured
    priority: 10            # higher wins ties; default 0
    match:                  # optional extra conditions
      query: { category: books }   # value, or "*" for "present with any value"
      headers: { X-Api-Version: "2" }
      body: { user: ada }          # subset match against the JSON body
    latency:                # per-route, overrides the global default
      random_ms: [50, 400]
    chaos:
      error_rate: 0.4       # fraction of calls that fail
      error_status: 503
      error_body: { error: upstream_unavailable }
      rate_limit: { limit: 60, window_ms: 60000, status: 429 }
    response:
      status: 200
      headers: { Content-Type: application/json }
      body:                 # inline, templated ...
        id: "{{ request.path.id | int }}"
        name: "{{ faker.name }}"
      # file: ./bodies/thing.json   # ... or loaded from a file (also templated)

resources:                  # in-memory CRUD collections, tried after routes
  - name: todos
    path: /todos
    id_field: id
    id_type: int            # int (auto-increment) or uuid
    seed:
      - { id: 1, title: "First", done: false }

record:                     # optional proxy-and-record block
  upstream: https://api.example.com   # null for offline replay only
  fixtures_dir: fixtures
  record: true
```

### Template helpers

| Expression | Result |
|---|---|
| `{{ request.query.page }}` | query-string value |
| `{{ request.path.id }}` | captured `{id}` path param |
| `{{ request.header.X-Api-Key }}` | request header, case-insensitive |
| `{{ request.body.user.name }}` | nested field from the JSON body |
| `{{ faker.name }}` `faker.email` `faker.uuid` `faker.city` | fake data |
| `{{ faker.int(1, 100) }}` `faker.price(5, 500)` `faker.bool` | typed fakes |
| `{{ seq.order }}` `{{ seq.invoice(1000, 5) }}` | incrementing counter |
| `{{ now.iso }}` `now.date` `now.timestamp` | current time |
| `{{ uuid }}` | random uuid4 |
| `\| default(1) \| int \| upper \| round(2) \| json` | chained filters |

When a value is exactly one `{{ expr }}`, its native type is preserved — `{{ faker.int(1, 5) }}` yields a JSON number, not a string. Embedded expressions like `"id-{{ seq.n }}"` are stringified.

## Project structure

```
api-mock-server/
├── cli.py                         # run straight from a checkout
├── src/mockserver/
│   ├── config.py                  # YAML -> typed model, path compilation
│   ├── server.py                  # ASGI app, match pipeline, dispatch
│   ├── dynamic.py                 # {{ ... }} templating + seedable faker
│   ├── behavior.py                # latency, error injection, rate limiting
│   ├── stateful.py                # in-memory CRUD resources
│   ├── openapi_import.py          # OpenAPI 3 -> mocks skeleton
│   ├── record.py                  # proxy-and-record / offline replay
│   └── cli.py                     # serve / import-openapi / record
├── examples/                      # shop API, todo API, OpenAPI import
└── tests/                         # matching, templating, CRUD, chaos, import, record
```

## How it compares

| | api-mock-server | Prism | WireMock | json-server |
|---|---|---|---|---|
| Config format | one YAML file | OpenAPI spec | JSON stub files | JSON db file |
| Works without an OpenAPI spec | yes | no | yes | yes |
| Dynamic templated bodies | faker, sequences, request echo | schema examples only | response templating | limited |
| Match on query/header/**body** | yes | limited | yes | no |
| Latency + error injection | seeded, per-route | via CLI flags | yes | no |
| Stateful CRUD out of the box | yes | no | via scenarios | yes |
| Record and replay | yes | proxy mode | yes | no |
| Runtime | Python / Starlette | Node | Java / JVM | Node |

**Honest take:** if you already have a maintained OpenAPI contract and only need spec-conformant responses, [Prism](https://github.com/stoplightio/prism) validates against the spec and is the better fit. If you live in the JVM or need enterprise matching features, [WireMock](https://github.com/wiremock/wiremock) is more battle-tested. [json-server](https://github.com/typicode/json-server) is the fastest path to plain REST-over-JSON. `api-mock-server` is for when you want **one readable file** that mixes stateful CRUD, hand-crafted dynamic responses, and deliberate chaos — in a Python stack, without a spec as a prerequisite.

## Related projects

- **[fastapi-production-template](https://github.com/AleBrito124356/fastapi-production-template)** — when the mock has served its purpose, build the real async FastAPI backend from this starter.
- **[webhook-toolkit](https://github.com/AleBrito124356/webhook-toolkit)** — the same record-and-replay idea for inbound webhooks: verify, inspect, and replay locally.
- **[nextjs-ai-chat-template](https://github.com/AleBrito124356/nextjs-ai-chat-template)** — a clean Next.js 15 frontend to point at this mock while the backend is still being built.
- **[python-cli-template](https://github.com/AleBrito124356/python-cli-template)** — the batteries-included pattern for the kind of Typer + Rich CLI this project ships.

## License

MIT — Copyright (c) 2026 Alejandro Brito. See [LICENSE](LICENSE).
