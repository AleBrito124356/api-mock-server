# Examples

Three self-contained mock configurations.

## shop-api

Everything in one file: query-based routing with priority, path params,
templated dynamic bodies (faker + sequences), a create endpoint that echoes
the request body, chaos (error injection), latency, rate limiting, a
file-backed response, and a stateful `/customers` collection.

```bash
mockserver serve --config examples/shop-api/mocks.yaml
```

## todo-api

The smallest useful fake backend: one `resources` block gives you full CRUD
for `/todos` with filtering, sorting and pagination. No handler code.

```bash
mockserver serve --config examples/todo-api/mocks.yaml
```

## openapi-import

Turn an OpenAPI 3 document into a runnable mock. The importer uses schema
examples for response bodies.

```bash
mockserver import-openapi examples/openapi-import/petstore.yaml -o petstore.mocks.yaml
mockserver serve --config petstore.mocks.yaml
curl localhost:8000/pets
```
