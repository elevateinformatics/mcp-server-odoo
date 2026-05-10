# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **CI lint job**: pre-commit was running `black` while CI runs `ruff format` —
  the two tools disagree on `assert x, msg` formatting, so commits that passed
  pre-commit could fail CI. Removed black from pre-commit and dev deps; both
  pre-commit and CI now use `ruff format`. Repo reformatted accordingly.

## [0.7.0] - 2026-05-10

### Added

- **`tail_logs` and `list_log_files` tools** — first-class MCP tools wrapping the
  companion Odoo module
  [`elevate_informatics_log_reader`](https://github.com/elevateinformatics/elevate_informatics_log_reader).
  Runtime detection of the model surfaces a clear install hint when the module
  is missing — no need to call `call_model_method` manually, no YOLO required if
  the model is whitelisted in `mcp.enabled.model`.

### Merged from upstream `ivnvxd/mcp-server-odoo` v0.6.0

Upstream features adopted while preserving the `mcp-server-odoo-ei` fork:
- **`aggregate_records` tool** — server-side grouping (`formatted_read_group` / `read_group` fallback).
- **`call_model_method` tool** — opt-in (`ODOO_MCP_ENABLE_METHOD_CALLS`) generic XML-RPC escape hatch (YOLO-only).
- **`post_message` tool** — chatter via `mail.thread.message_post()`.
- **Per-proxy XML-RPC transport** — fixes wire-state races under concurrent tool calls (#44).
- **Drop progress notifications under stdio** — strict MCP clients no longer tear down the transport.
- **`ODOO_MCP_DEFAULT_LIMIT` honored** when search_records omits limit; over-limit caps to `max_limit`.
- **Lifespan hooks + Context injection** in tools/resources for client-visible logging/progress.
- **Health endpoint** (`GET /health`) and **completion handler** for model autocomplete.

### Preserved from this fork

- Mutable session locale + `get_locale`/`set_locale` tools.
- Per-call `lang` override on every CRUD tool.
- jsonb translation tools (`get_field_translations`, `update_field_translations`).
- Batch tools `read_records`, `create_records`, `update_records` (with `lang`).
- gzip XML-RPC response decompression.
- Auto-reconnect on stale connection / `[Errno 10053]`.
- Nuitka build profile (`scripts/build_nuitka.py`).
- Package `mcp-server-odoo-ei`.

---

## Upstream history (ivnvxd)

## [0.6.0] - 2026-05-02

### Added
- **`aggregate_records` tool**: Server-side aggregation via Odoo's grouping methods (#64). Dispatches to `formatted_read_group` on Odoo 19+ and falls back to `read_group` with response normalization on older versions.
- **`call_model_method` tool** (YOLO-only, opt-in via `ODOO_MCP_ENABLE_METHOD_CALLS`): generic XML-RPC `execute_kw` escape hatch for workflow actions not covered by CRUD.

### Fixed
- **Concurrent tool calls**: Per-proxy XML-RPC transport prevents wire-state corruption when multiple tools run in parallel (#44).
- **CI**: Disable MCP rate limiting in integration tests — the 300/min ceiling tripped mid-suite under the shared admin API key. Production keeps the default.

## [0.5.2] - 2026-04-30

### Added
- **`post_message` tool**: Post to an Odoo record's chatter via `mail.thread.message_post()` (#50).

### Fixed
- **Stdio transport**: Drop terminal `notifications/progress` from `search_records` and `list_models` — strict MCP clients treated the post-response notification as a fatal protocol error and tore down the transport.

## [0.5.1] - 2026-04-23

### Fixed
- **CI**: Skip MCP integration tests for fork PRs where repository secrets are unavailable
- **Search**: Cap over-limit to `max_limit` instead of resetting to `default_limit` (#57, @litnimax)
- **Search**: `ODOO_MCP_DEFAULT_LIMIT` now applies when clients omit `limit` — previously hardcoded, bypassing the env var in the common case (#61)

## [0.5.0] - 2026-02-28

### Added
- **Lifespan hooks**: Consolidated duplicated setup/teardown from `run_stdio()` and `run_http()` into a single `_odoo_lifespan()` async context manager passed to FastMCP — transport methods are now thin wrappers
- **Context injection**: All 7 tool functions and 4 resource functions accept `ctx: Context` for client-visible observability — sends info/warning/progress notifications to MCP clients in real time
- **Health endpoint**: `GET /health` HTTP route via FastMCP `custom_route` — returns connection status and version as JSON for load balancers and monitoring
- **Model autocomplete**: FastMCP completion handler returns matching model names when clients request autocomplete for `model` parameters in resource URIs

### Changed
- **Health endpoint**: Simplified response to `status`, `version`, and `connected` only — removed `url`, `database`, `error_metrics`, `recent_errors`, and `performance` fields
- **Tests**: Overhaul test suite — replace fake MCP protocol simulators with real handler calls, mock only at the XML-RPC network boundary, add CRUD/tool/lifespan/completion coverage, remove example-only and mock-asserting tests

### Fixed
- **Boolean formatting**: `False` values displayed as "Not set" instead of "No" in formatted output due to branch precedence bug in `_format_simple_value()`
- **Completion handler**: Returned raw list instead of `Completion(values=...)`, breaking MCP protocol contract
- **Lifespan cleanup**: Connection cleanup didn't run when setup failed because setup was outside the `try` block

## [0.4.5] - 2026-02-27

### Changed
- **Documentation**: Synchronized `--help`, `.env.example`, and README with all current environment variables and test commands
- **CI**: Run lint and unit tests in parallel; gate integration tests to PRs and main/dev branches; add concurrency groups and uv caching
- **Project config**: Clean up `.gitignore`, trim redundant dependencies, consolidate dev tooling config

## [0.4.4] - 2026-02-25

### Changed
- **Connection startup**: `OdooConnection.connect()` resolves the target database *before* creating MCP proxies in standard mode, setting the `X-Odoo-Database` header for `_test_connection()` and all subsequent calls
- **`AccessController` auth**: Supports session-cookie authentication as fallback when no API key is configured — authenticates via `/web/session/authenticate` and retries once on session expiry

### Fixed
- **All standard-mode auth combinations now work**: API-key-only (S1/S2), API-key + user/pass (S3/S4), and user/pass-only (S5/S6) — with or without explicit `ODOO_DB` — all connect, authenticate, and pass access control. Previously S1–S6 could fail with 404 on multi-DB servers
- **Multi-database routing**: Standard mode MCP endpoints (`/mcp/xmlrpc/*`, `/mcp/models`, `/mcp/health`, etc.) returned 404 when multiple databases existed in PostgreSQL because Odoo couldn't determine which DB to route to. Now sends `X-Odoo-Database` header on all MCP requests (XML-RPC and REST)
- **DB endpoint routing**: Database listing now always uses the server-wide `/xmlrpc/db` endpoint instead of `/mcp/xmlrpc/db`, since the latter requires a DB context that isn't available yet

## [0.4.3] - 2026-02-24

### Changed
- **CI**: Reduce Python matrix to 3.10 + 3.13; add YOLO and MCP integration test jobs with Odoo 18 + PostgreSQL 17 Docker containers
- **Test markers**: Rename `integration` → `mcp`, `e2e` → `yolo`, drop `odoo_required`; add markers to 30 previously-invisible tests; remove `RUN_MCP_TESTS` gate
- **CI coverage**: Collect coverage from unit, YOLO and MCP jobs; merge and upload to Codecov

### Fixed
- **Standard mode auth**: `AccessController` no longer crashes at startup when only username/password is configured (without `ODOO_API_KEY`). Now logs a warning and defers auth failure to REST call time with actionable error messages suggesting `ODOO_API_KEY` or YOLO mode

## [0.4.2] - 2026-02-23

### Added
- **Docker support**: Multi-stage Dockerfile with `uv` for fast builds, and GitHub Actions workflow for multi-platform images (amd64 + arm64) published to both GHCR and Docker Hub

## [0.4.1] - 2026-02-22

### Added
- **Multi-language support**: New `ODOO_LOCALE` environment variable to get Odoo responses in any installed language (e.g. `es_ES`, `fr_FR`, `de_DE`). Validates locale at startup and falls back to default language if not installed. (#23)

### Fixed
- **Models without `name` field**: `create_record` and `update_record` failed on models like `mail.activity` that lack a `name` field due to hardcoded field list in post-operation read-back
- **Version-aware record URLs**: `create_record` and `update_record` now generate `/odoo/{model}/{id}` URLs for Odoo 18+ instead of the legacy `/web#id=...` format (which is still used for Odoo ≤ 17)

## [0.4.0] - 2026-02-22

### Added
- **Structured output**: All tools return typed Pydantic models with auto-generated JSON schemas for MCP clients (`SearchResult`, `RecordResult`, `ModelsResult`, `CreateResult`, `UpdateResult`, `DeleteResult`)
- **Tool annotations**: All tools declare `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint` via MCP `ToolAnnotations`
- **Resource annotations**: All resources declare `audience` and `priority` via MCP `Annotations`
- **Human-readable titles**: All tools and resources include `title` for better display in MCP clients

### Changed
- **MCP SDK**: Upgraded from `>=1.9.4` to `>=1.26.0,<2`
- **`get_record` structured output**: Returns `RecordResult` with separate `record` and `metadata` fields instead of injecting `_metadata` into record data
- **Tooling**: Replace black/mypy with ruff format/ty for formatting and type checking

### Fixed
- **VertexAI compatibility**: Simplified `search_records` `domain`/`fields` type hints from `Union` to `Optional[Any]` to avoid `anyOf` JSON schemas rejected by VertexAI/Google ADK (#27)
- **Stale record data**: Removed record-level caching from `read()` to prevent returning stale field values (e.g. `active`) when records change in Odoo between calls (#28)
- **Tests**: Integration tests now use `ODOO_URL` for server detection, deduplicated server checks, fixed async test handling, updated assertions for structured output types, halved suite runtime

### Removed
- Legacy error type aliases (`ToolError`, `ResourceError`, `ResourceNotFoundError`, `ResourcePermissionError`) — use `ValidationError`, `NotFoundError`, `PermissionError` directly
- Unused `_setup_handlers()` method from `OdooMCPServer`

## [0.3.1] - 2026-02-21

### Fixed
- **Authentication bypass**: Add missing `@property` on `is_authenticated` — was always truthy as a method reference, bypassing auth guards

### Changed
- Update CI dependencies (black 26.1.0, GitHub Actions v6/v7)
- Server version test validates semver format instead of hardcoded value

## [0.3.0] - 2025-09-14

(Pre-fork upstream baseline.)

---

## Fork history (elevateinformatics)

## [0.5.0] - 2026-05-08

### Added

- **Mutable session locale**: The default `context.lang` injected on every Odoo
  call is now a runtime-mutable session value (initialized from `ODOO_LOCALE`).
  Two new tools manage it:
  - `get_locale` — returns the current session locale and the languages
    installed in the database (`res.lang`).
  - `set_locale(lang)` — changes the default for subsequent calls; pass
    `null`/empty to clear it. Validated against installed languages.
- **Per-call language override**: `search_records`, `get_record`,
  `read_records`, `create_record`, `create_records`, `update_record`, and
  `update_records` accept an optional `lang` argument that overrides the
  session locale for that single call (cache-bypassed to avoid mixing
  translations).
- **Multi-language read/write tools** for translatable fields, including
  `ir.ui.view.arch_db` and qweb HTML:
  - `get_field_translations(model, record_ids, field_names, langs?)` returns
    every language at once via Odoo's `get_field_translations` API.
  - `update_field_translations(model, record_ids, translations)` writes
    `{field: {lang: value}}` (or `{field: {lang: {term_src: term_value}}}`
    for term-based fields) directly into the jsonb storage via Odoo's
    `update_field_translations` API. Other languages are left untouched.

### Fixed

- **`context.lang` no longer overwrites caller-provided values**: previously,
  the configured locale was injected unconditionally on every `execute_kw`
  call, masking any per-call language passed via `kwargs.context.lang`. The
  injection now only fills in the default when the caller did not specify one.

## [0.4.7] - 2026-01-19

### Fixed

- **list_models Validation Error**: Fixed Pydantic type mismatch in `list_models` tool return type (#6)
  - Return type was `Dict[str, List[Dict[str, Any]]]` but YOLO mode returns `yolo_mode` (dict) and `total` (int)
  - Changed return type to `Dict[str, Any]` to correctly represent the actual response structure
  - Affects both YOLO read and YOLO full modes

## [0.4.6] - 2026-01-16

### Fixed

- **Connection Aborted Error (Windows)**: Added detection of `[Errno 10053]` "An established connection was aborted by the software" error to reconnectable errors list
  - This error commonly occurs on Windows when connection is dropped after idle period
  - Now triggers automatic reconnection with retry logic like other connection errors

## [0.4.5] - 2026-01-15

### Fixed

- **Connection Timeout After Idle**: Fixed issue where the first API call after extended idle period would fail with "Remote end closed connection without response" (#5)
  - Implemented automatic reconnection with retry logic for stale connections
  - Added detection of reconnectable connection errors (connection reset, broken pipe, etc.)
  - Operations now automatically retry up to 2 times with fresh connections
  - XML-RPC business logic errors are not retried (only connection issues)

## [0.4.4] - 2026-01-07

### Added
- **Batch Create Tool**: Added `create_records` tool for creating multiple records in a single API call
  - More efficient than calling `create_record` multiple times
  - Validates all records before creation
  - Returns all created records with id and display_name

## [0.4.3] - 2026-01-07

### Added
- **Batch Read Tool**: Added `read_records` tool for reading multiple records by ID in a single API call
  - More efficient than calling `get_record` multiple times
  - Supports smart field selection or explicit field list
  - Returns found records and reports any missing IDs (non-blocking)

## [0.4.2] - 2026-01-07

### Added
- **Batch Update Tool**: Added `update_records` tool for updating multiple records in a single API call (#2)
  - More efficient than calling `update_record` multiple times
  - Validates all record IDs exist before updating
  - Atomic operation - if one fails, all fail
  - Returns count of updated records and their IDs

## [0.3.2] - 2026-01-06

### Changed
- **Metadata**: Updated package author and maintainer information

## [0.3.1] - 2026-01-06

### Fixed
- **Model Compatibility**: Fixed `create_record` and `update_record` to support models without `name` field (e.g., `survey.survey` which uses `title`)
  - Removed `name` from essential fields, now only returns `id` and `display_name`
  - `display_name` is a computed field that exists in all Odoo models

### Changed
- **CI/CD**: Updated PyPI publish workflow to use API token authentication

## [0.3.0] - 2025-11-18

### Added
- **Multi-Language Support**: Added `ODOO_LOCALE` environment variable to request Odoo responses in different languages
  - Automatically injects language context into all Odoo API calls
  - Supports any locale installed in your Odoo instance (es_ES, es_AR, fr_FR, etc.)
  - Returns translated field labels, selection values, and error messages
  - Preserves existing context values when locale is added
- **YOLO Mode**: Development mode for testing without MCP module installation
  - Read-Only: Safe demo mode with read-only access to all models
  - Full Access: Unrestricted access for development (never use in production)
  - Works with any standard Odoo instance via native XML-RPC endpoints

## [0.2.2] - 2025-08-04

### Added
- **Direct Record URLs**: Added `url` field to `create_record` and `update_record` responses for direct access to records in Odoo

### Changed
- **Minimal Response Fields**: Reduced `create_record` and `update_record` tool responses to return only essential fields (id, name, display_name) to minimize LLM context usage
- **Smart Field Optimization**: Implemented dynamic field importance scoring to reduce smart default fields to most essential across all models, with configurable limit via `ODOO_MCP_MAX_SMART_FIELDS`

## [0.2.1] - 2025-06-28

### Changed
- **Resource Templates**: Updated `list_resource_templates` tool to clarify that query parameters are not supported in FastMCP resources

## [0.2.0] - 2025-06-19

### Added
- **Write Operations**: Enabled full CRUD functionality with `create_record`, `update_record`, and `delete_record` tools (#5)

### Changed
- **Resource Simplification**: Removed query parameters from resource URIs due to FastMCP limitations - use tools for advanced queries (#4)

### Fixed
- **Domain Parameter Parsing**: Fixed `search_records` tool to accept both JSON strings and Python-style domain strings, supporting various format variations

## [0.1.2] - 2025-06-19

### Added
- **Resource Discovery**: Added `list_resource_templates` tool to provide resource URI template information
- **HTTP Transport**: Added streamable-http transport support for web and remote access

## [0.1.1] - 2025-06-16

### Fixed
- **HTTPS Connection**: Fixed SSL/TLS support by using `SafeTransport` for HTTPS URLs instead of regular `Transport`
- **Database Validation**: Skip database existence check when database is explicitly configured, as listing may be restricted for security

## [0.1.0] - 2025-06-08

### Added

#### Core Features
- **MCP Server**: Full Model Context Protocol implementation using FastMCP with stdio transport
- **Dual Authentication**: API key and username/password authentication
- **Resource System**: Complete `odoo://` URI schema with 5 operations (record, search, browse, count, fields)
- **Tools**: `search_records`, `get_record`, `list_models` with smart field selection
- **Auto-Discovery**: Automatic database detection and connection management

#### Data & Performance
- **LLM-Optimized Output**: Hierarchical text formatting for AI consumption
- **Connection Pooling**: Efficient connection reuse with health checks
- **Pagination**: Smart handling of large datasets
- **Caching**: Performance optimization for frequently accessed data
- **Error Handling**: Comprehensive error sanitization and user-friendly messages

#### Security & Access Control
- **Multi-layered Security**: Odoo permissions + MCP-specific access controls
- **Session Management**: Automatic credential injection and session handling
- **Audit Logging**: Complete operation logging for security

## Limitations
- **No Prompts**: Guided workflows not available
- **Alpha Status**: API may change before 1.0.0

**Note**: This alpha release provides production-ready data access for Odoo via AI assistants.
