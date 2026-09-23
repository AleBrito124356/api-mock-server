# Changelog

## 0.2.0

### Added

- `mockserver validate FILE...`: checks keys at every level (with "did you
  mean" suggestions), methods, status codes, paths, latency/chaos/rate-limit
  ranges, resources, `response.file` existence and JSON validity, and every
  `{{ }}` expression (roots, request scopes, captured path params, faker
  helpers and their arguments, filters). `--strict` and `--json` options.
  `load_config(path, strict=True)` raises `ConfigError` for library users.
- `mockserver serve --watch`: hot reload of the mocks file and its body files.
  Resource data and sequences survive; invalid edits are rejected and the last
  good config keeps serving.
- Admin API under `/__mock__` (`config.admin_prefix`): `GET /routes` (with hit
  counts), `GET|DELETE /requests` (request journal with filters),
  `POST /reset` (resources, sequences, RNGs, rate limits, response scripts,
  journal). `config.journal_size` bounds the journal.
- `responses:` + `sequence: stick|cycle` on routes for scripted consecutive
  responses; `name:` on routes, returned as `X-Matched-Route`.
- Templated `status` (`"{{ request.query.code | default(200) }}"`),
  `faker.choice`, `faker.url`, `faker.ipv4`, literal expressions.
- `mockserver replay --fixtures DIR` and `serve --fixtures DIR` for offline
  replay; `MOCK_*` defaults from the environment or a `.env` file
  (`--env-file`); `--version`.
- `import-openapi`: Swagger 2.0 support, `--base-path` (default `auto`),
  `--dynamic` (request-aware templates) and `--resources` (stateful CRUD from
  collection/item paths); the output is validated right after import.
- `create_app(config, upstream_transport=...)` to plug an httpx transport into
  record mode (used by the offline tests).
- The bundled `examples/shop-api` and `examples/todo-api` configs, which were
  missing from 0.1.0.

### Fixed

- Template strings with two or more `{{ }}` tokens rendered as garbage.
- CORS preflight to stateful resources returned 405, so browsers could not
  send JSON POST/PUT/PATCH/DELETE to them.
- Boolean/number bodies were sent as `text/plain` Python repr (`True`).
- Template and body-file errors surfaced as an opaque 500; they are now a JSON
  `mock_error` naming the route and expression. An unreachable upstream is a
  502 `upstream_error`.
- Record/replay keys ignored the request body and repeated query params, so
  different POST payloads replayed the wrong fixture.
- `/__mock__` was unreachable in record mode.
- Stateful resources crashed on non-object JSON bodies and silently
  overwrote an item when a POST reused its id.
- `import-openapi` crashed on bare integer status keys (`200:`) and range keys
  (`2XX`), emitted `pattern` regexes as values, and ignored numeric bounds.
- `HEAD` routes could never match.
- Query matchers written as YAML booleans (`archived: true`) never matched.

### Changed (check these when upgrading)

- `serve`, `replay` and `record` refuse to start when the mocks file has
  validation errors. Pass `--no-validate` to start anyway.
- An unknown faker helper or filter is an error (JSON 500 `mock_error`)
  instead of silently rendering `null` / passing the value through.
- Booleans interpolated into a string render as `true`/`false`, not
  `True`/`False`.
- Bodies that are a number, boolean or a template rendering to `None` are
  sent as JSON (`application/json`). A string body on a route that declares a
  JSON `Content-Type` is JSON-encoded unless it already is a JSON object or
  array.
- With `cors: true` and an `Origin` header, `Access-Control-Allow-Origin`
  echoes the origin and `Access-Control-Allow-Credentials: true` is sent
  (instead of `*`), and non-safelisted response headers are exposed.
- Resource writes: a body that is not a JSON object is a 400; a POST with an
  existing id is a 409.
- Paths under `/__mock__` belong to the admin API; move it with
  `config.admin_prefix` if you had routes there.
- Rate-limit windows are per route (two routes sharing a method and path used
  to share one window).
- New fixtures are written in format version 2 (with `body_sha1`). Fixtures
  written by 0.1.x still load and replay.
- CLI `--fixtures` paths are relative to the working directory, not to the
  config file's directory.
- `faker.uuid` returns valid version-4 UUIDs, so seeded values differ from
  0.1.x.
- `mockserver import-openapi` prefixes paths with the server URL path by
  default (`/v1/pets` for the petstore example); use `--base-path /` for the
  old behaviour. The Python `import_openapi()` default is unchanged. Imported
  routes now carry `name` (from `operationId`) and omit `body`/`headers` when
  the response has no body.

## 0.1.0

- Initial release.
