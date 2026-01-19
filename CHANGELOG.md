# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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