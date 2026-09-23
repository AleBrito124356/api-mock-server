# Examples

Three self-contained mock configurations. `mockserver validate examples/*/mocks.yaml`
checks them, and `tests/test_examples.py` runs every flow described here.

## shop-api

Everything in one file: query-based routing with priority and named routes,
path params, templated dynamic bodies (faker + sequences), a create endpoint
that echoes the request body, body and header matching, chaos (error
injection), latency, rate limiting, a scripted polling endpoint
(`/exports/{id}`: 202, 202, then 200), a file-backed response, and a stateful
`/customers` collection.

```bash
mockserver serve --config examples/shop-api/mocks.yaml
curl localhost:8000/__mock__/routes        # what is configured, with hit counts
```

## todo-api

The smallest useful fake backend: one `resources` block gives you full CRUD
for `/todos` with filtering, sorting and pagination. No handler code.

```bash
mockserver serve --config examples/todo-api/mocks.yaml
curl -X POST localhost:8000/__mock__/reset  # back to the three seeded todos
```

## openapi-import

Turn an OpenAPI 3 document into a runnable mock. By default paths get the
server URL prefix (`/v1`) and bodies come from the spec's examples.
`--dynamic` makes them request-aware (`GET /v1/pets/123` returns id 123) and
`--resources` turns `/pets` + `/pets/{petId}` into real CRUD.

```bash
mockserver import-openapi examples/openapi-import/petstore.yaml -o petstore.mocks.yaml --dynamic
mockserver serve --config petstore.mocks.yaml
curl localhost:8000/v1/pets/123
```
