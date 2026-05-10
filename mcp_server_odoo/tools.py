"""MCP tool handlers for Odoo operations.

This module implements MCP tools for performing operations on Odoo data.
Tools are different from resources - they can have side effects and perform
actions like creating, updating, or deleting records.
"""

import base64
import json
import re
import xmlrpc.client
from ast import literal_eval as _parse_python_literal
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from .access_control import AccessControlError, AccessController
from .config import OdooConfig
from .error_handling import (
    NotFoundError,
    ValidationError,
)
from .error_sanitizer import ErrorSanitizer
from .logging_config import get_logger, perf_logger
from .odoo_connection import OdooConnection, OdooConnectionError
from .schemas import (
    AggregateResult,
    CallModelMethodResult,
    CreateResult,
    DeleteResult,
    FieldSelectionMetadata,
    ModelsResult,
    PostMessageResult,
    RecordResult,
    ResourceTemplatesResult,
    SearchResult,
    UpdateResult,
)

logger = get_logger(__name__)

# Public Odoo method = Python identifier not starting with "_".
_PUBLIC_METHOD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

# Refuse JSON strings larger than this on the parse path — bounds memory and
# guards against pathological inputs.
_MAX_JSON_PARAM_BYTES = 1_000_000


def _json_safe(value: Any) -> Any:
    """Coerce XML-RPC return types Pydantic can't serialize (Binary, DateTime)."""
    if isinstance(value, xmlrpc.client.Binary):
        return base64.b64encode(value.data).decode("ascii")
    if isinstance(value, xmlrpc.client.DateTime):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


class OdooToolHandler:
    """Handles MCP tool requests for Odoo operations."""

    def __init__(
        self,
        app: FastMCP,
        connection: OdooConnection,
        access_controller: AccessController,
        config: OdooConfig,
    ):
        """Initialize tool handler.

        Args:
            app: FastMCP application instance
            connection: Odoo connection instance
            access_controller: Access control instance
            config: Odoo configuration instance
        """
        self.app = app
        self.connection = connection
        self.access_controller = access_controller
        self.config = config

        # Register tools
        self._register_tools()

    def _format_datetime(self, value: str) -> str:
        """Format datetime values to ISO 8601 with timezone."""
        if not value or not isinstance(value, str):
            return value

        # Handle Odoo's compact datetime format (YYYYMMDDTHH:MM:SS)
        if len(value) == 17 and "T" in value and "-" not in value:
            try:
                dt = datetime.strptime(value, "%Y%m%dT%H:%M:%S")
                return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            except ValueError:
                pass

        # Handle standard Odoo datetime format (YYYY-MM-DD HH:MM:SS)
        if " " in value and len(value) == 19:
            try:
                dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
                return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            except ValueError:
                pass

        return value

    def _process_record_dates(self, record: Dict[str, Any], model: str) -> Dict[str, Any]:
        """Process datetime fields in a record to ensure proper formatting."""
        # Common datetime field names in Odoo
        known_datetime_fields = {
            "create_date",
            "write_date",
            "date",
            "datetime",
            "date_start",
            "date_end",
            "date_from",
            "date_to",
            "date_order",
            "date_invoice",
            "date_due",
            "last_update",
            "last_activity",
            "activity_date_deadline",
        }

        # First try to get field metadata
        fields_info = None
        try:
            fields_info = self.connection.fields_get(model)
        except Exception:
            # Field metadata unavailable, will use fallback detection
            pass

        # Process each field in the record
        for field_name, field_value in record.items():
            if not isinstance(field_value, str):
                continue

            should_format = False

            # Check if field is identified as datetime from metadata
            if fields_info and isinstance(fields_info, dict) and field_name in fields_info:
                field_type = fields_info[field_name].get("type")
                if field_type == "datetime":
                    should_format = True

            # Check if field name suggests it's a datetime field
            if not should_format and field_name in known_datetime_fields:
                should_format = True

            # Check if field name ends with common datetime suffixes
            if not should_format and any(
                field_name.endswith(suffix) for suffix in ["_date", "_datetime", "_time"]
            ):
                should_format = True

            # Pattern-based detection for datetime-like strings
            if not should_format and (
                (
                    len(field_value) == 17 and "T" in field_value and "-" not in field_value
                )  # 20250607T21:55:52
                or (
                    len(field_value) == 19 and " " in field_value and field_value.count("-") == 2
                )  # 2025-06-07 21:55:52
            ):
                should_format = True

            # Apply formatting if needed
            if should_format:
                formatted = self._format_datetime(field_value)
                if formatted != field_value:
                    record[field_name] = formatted

        return record

    def _score_field_importance(self, field_name: str, field_info: Dict[str, Any]) -> int:
        """Score field importance for smart default selection.

        Args:
            field_name: Name of the field
            field_info: Field metadata from fields_get()

        Returns:
            Importance score (higher = more important)
        """
        # Tier 1: Essential fields (always included)
        if field_name in {"id", "name", "display_name", "active"}:
            return 1000

        # Exclude system/technical fields by prefix
        exclude_prefixes = ("_", "message_", "activity_", "website_message_")
        if field_name.startswith(exclude_prefixes):
            return 0

        # Exclude specific technical fields
        exclude_fields = {
            "write_date",
            "create_date",
            "write_uid",
            "create_uid",
            "__last_update",
            "access_token",
            "access_warning",
            "access_url",
        }
        if field_name in exclude_fields:
            return 0

        score = 0

        # Tier 2: Required fields are very important
        if field_info.get("required"):
            score += 500

        # Tier 3: Field type importance
        field_type = field_info.get("type", "")
        type_scores = {
            "char": 200,
            "boolean": 180,
            "selection": 170,
            "integer": 160,
            "float": 160,
            "monetary": 140,
            "date": 150,
            "datetime": 150,
            "many2one": 120,  # Relations useful but not primary
            "text": 80,
            "one2many": 40,
            "many2many": 40,  # Heavy relations
            "binary": 10,
            "html": 10,
            "image": 10,  # Heavy content
        }
        score += type_scores.get(field_type, 50)

        # Tier 4: Storage and searchability bonuses
        if field_info.get("store", True):
            score += 80
        if field_info.get("searchable", True):
            score += 40

        # Tier 5: Business-relevant field patterns (bonus)
        business_patterns = [
            "state",
            "status",
            "stage",
            "priority",
            "company",
            "currency",
            "amount",
            "total",
            "date",
            "user",
            "partner",
            "email",
            "phone",
            "address",
            "street",
            "city",
            "country",
            "code",
            "ref",
            "number",
        ]
        if any(pattern in field_name.lower() for pattern in business_patterns):
            score += 60

        # Exclude expensive computed fields (non-stored)
        if field_info.get("compute") and not field_info.get("store", True):
            score = min(score, 30)  # Cap computed fields at low score

        # Exclude large field types completely
        if field_type in ("binary", "image", "html"):
            return 0

        # Exclude one2many and many2many fields (can be large)
        if field_type in ("one2many", "many2many"):
            return 0

        return max(score, 0)

    def _get_smart_default_fields(self, model: str) -> Optional[List[str]]:
        """Get smart default fields for a model using field importance scoring.

        Args:
            model: The Odoo model name

        Returns:
            List of field names to include by default, or None if unable to determine
        """
        try:
            # Get all field definitions
            fields_info = self.connection.fields_get(model)

            # Score all fields by importance
            field_scores = []
            for field_name, field_info in fields_info.items():
                score = self._score_field_importance(field_name, field_info)
                if score > 0:  # Only include fields with positive scores
                    field_scores.append((field_name, score))

            # Sort by score (highest first)
            field_scores.sort(key=lambda x: x[1], reverse=True)

            # Select top N fields based on configuration
            max_fields = self.config.max_smart_fields
            selected_fields = [field_name for field_name, _ in field_scores[:max_fields]]

            # Ensure essential fields are always included
            essential_fields = ["id", "name", "display_name", "active"]
            for field in essential_fields:
                if field in fields_info and field not in selected_fields:
                    selected_fields.append(field)

            # Remove duplicates while preserving order
            final_fields = []
            seen = set()
            for field in selected_fields:
                if field not in seen:
                    final_fields.append(field)
                    seen.add(field)

            # Ensure we have at least essential fields
            if not final_fields:
                final_fields = [f for f in essential_fields if f in fields_info]

            logger.debug(
                f"Smart default fields for {model}: {len(final_fields)} of {len(fields_info)} fields "
                f"(max configured: {max_fields})"
            )
            return final_fields

        except Exception as e:
            logger.warning(f"Could not determine default fields for {model}: {e}")
            # Return None to indicate we should get all fields
            return None

    def _parse_domain_input(self, domain: Optional[Any]) -> List[Any]:
        """Coerce a domain parameter into an Odoo domain list.

        Accepts a list (passed through), a JSON string, a Python-literal
        string with single quotes / ``True``/``False`` capitalization, or
        ``None`` (returns ``[]``). Raises ``ValidationError`` on anything
        that doesn't yield a list.
        """
        if domain is None:
            return []
        if not isinstance(domain, str):
            return domain

        try:
            parsed = json.loads(domain)
        except json.JSONDecodeError:
            try:
                json_domain = domain.replace("'", '"')
                json_domain = json_domain.replace("True", "true").replace("False", "false")
                parsed = json.loads(json_domain)
            except json.JSONDecodeError as e:
                try:
                    parsed = _parse_python_literal(domain)
                except (ValueError, SyntaxError):
                    raise ValidationError(
                        f"Invalid domain parameter. Expected JSON array or Python list, "
                        f"got: {domain[:100]}..."
                    ) from e

        if not isinstance(parsed, list):
            raise ValidationError(f"Domain must be a list, got {type(parsed).__name__}")

        logger.debug(f"Parsed domain from string: {parsed}")
        return parsed

    async def _ctx_info(self, ctx, message: str):
        """Send info to MCP client context if available."""
        if ctx:
            try:
                await ctx.info(message)
            except Exception:
                logger.debug(f"Failed to send ctx info: {message}")

    async def _ctx_warning(self, ctx, message: str):
        """Send warning to MCP client context if available."""
        if ctx:
            try:
                await ctx.warning(message)
            except Exception:
                logger.debug(f"Failed to send ctx warning: {message}")

    async def _ctx_progress(self, ctx, progress: float, total: float, message: str = ""):
        """Report progress to MCP client context if available."""
        if ctx:
            try:
                await ctx.report_progress(progress, total, message)
            except Exception:
                logger.debug(f"Failed to report progress: {progress}/{total}")

    def _register_tools(self):
        """Register all tool handlers with FastMCP."""

        @self.app.tool(
            title="Search Records",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            ),
        )
        async def search_records(
            model: str,
            domain: Optional[Any] = None,
            fields: Optional[Any] = None,
            limit: Optional[int] = None,
            offset: int = 0,
            order: Optional[str] = None,
            lang: Optional[str] = None,
            ctx: Optional[Context] = None,
        ) -> SearchResult:
            """Search for records in an Odoo model.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                domain: Odoo domain filter - can be:
                    - A list: [['is_company', '=', True]]
                    - A JSON string: "[['is_company', '=', true]]"
                    - None: returns all records (default)
                fields: Field selection options - can be:
                    - None (default): Returns smart selection of common fields
                    - A list: ["field1", "field2", ...] - Returns only specified fields
                    - A JSON string: '["field1", "field2"]' - Parsed to list
                    - ["__all__"] or '["__all__"]': Returns ALL fields (warning: may cause serialization errors)
                limit: Maximum number of records to return. Omit to use the
                    server-configured default (ODOO_MCP_DEFAULT_LIMIT). Capped
                    at ODOO_MCP_MAX_LIMIT.
                offset: Number of records to skip
                order: Sort order (e.g., 'name asc')
                lang: Optional one-shot language override (Odoo lang code,
                    e.g. ``"es_AR"``). Translatable fields are returned in this
                    language for this call only; the session locale is unchanged.

            Returns:
                Search results with records, total count, and pagination info
            """
            result = await self._handle_search_tool(
                model, domain, fields, limit, offset, order, lang, ctx
            )
            return SearchResult(**result)

        @self.app.tool(
            title="Get Record",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        async def get_record(
            model: str,
            record_id: int,
            fields: Optional[List[str]] = None,
            lang: Optional[str] = None,
            ctx: Optional[Context] = None,
        ) -> RecordResult:
            """Get a specific record by ID with smart field selection.

            This tool supports selective field retrieval to optimize performance and response size.
            By default, returns a smart selection of commonly-used fields based on the model's field metadata.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                record_id: The record ID
                fields: Field selection options:
                    - None (default): Returns smart selection of common fields
                    - ["field1", "field2", ...]: Returns only specified fields
                    - ["__all__"]: Returns ALL fields (warning: can be very large)
                lang: Optional one-shot language override (Odoo lang code,
                    e.g. ``"es_AR"``). Translatable fields are returned in this
                    language for this call only; the session locale is unchanged.

            Workflow for field discovery:
            1. To see all available fields for a model, use the resource:
               read("odoo://res.partner/fields")
            2. Then request specific fields:
               get_record("res.partner", 1, fields=["name", "email", "phone"])

            Examples:
                # Get smart defaults (recommended)
                get_record("res.partner", 1)

                # Get specific fields only
                get_record("res.partner", 1, fields=["name", "email", "phone"])

                # Get ALL fields (use with caution)
                get_record("res.partner", 1, fields=["__all__"])

            Returns:
                Record data with requested fields. When using smart defaults,
                includes metadata with field statistics.
            """
            return await self._handle_get_record_tool(model, record_id, fields, lang, ctx)

        @self.app.tool(
            title="Read Records (batch)",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        async def read_records(
            model: str,
            record_ids: List[int],
            fields: Optional[List[str]] = None,
            lang: Optional[str] = None,
            ctx: Optional[Context] = None,
        ) -> Dict[str, Any]:
            """Read multiple records by their IDs in a single API call (batch operation).

            More efficient than calling get_record multiple times.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                record_ids: List of record IDs to read
                fields: Optional list of fields. None → smart defaults; ["__all__"] → all fields.
                lang: Optional one-shot language override (Odoo lang code).

            Returns:
                Dictionary with `records`, `count`, `missing_ids`, `model`.
            """
            return await self._handle_read_records_tool(model, record_ids, fields, lang, ctx)

        @self.app.tool(
            title="List Models",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        async def list_models(ctx: Optional[Context] = None) -> ModelsResult:
            """List all models enabled for MCP access with their allowed operations.

            Returns:
                List of models with their technical names, display names,
                and allowed operations (read, write, create, unlink).
            """
            result = await self._handle_list_models_tool(ctx)
            return ModelsResult(**result)

        @self.app.tool(
            title="List Resource Templates",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        async def list_resource_templates(ctx: Optional[Context] = None) -> ResourceTemplatesResult:
            """List available resource URI templates.

            Since MCP resources with parameters are registered as templates,
            they don't appear in the standard resource list. This tool provides
            information about available resource patterns you can use.

            Returns:
                Resource template definitions with examples and enabled models.
            """
            result = await self._handle_list_resource_templates_tool(ctx)
            return ResourceTemplatesResult(**result)

        @self.app.tool(
            title="Create Record",
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=True,
            ),
        )
        async def create_record(
            model: str,
            values: Dict[str, Any],
            lang: Optional[str] = None,
            ctx: Optional[Context] = None,
        ) -> CreateResult:
            """Create a new record in an Odoo model.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                values: Field values for the new record
                lang: Optional one-shot language override (Odoo lang code).
                    Translatable values in ``values`` are stored under this
                    language; other languages are filled with the same value
                    by Odoo. Use ``update_field_translations`` afterwards if
                    you need different values per language.

            Returns:
                Created record details with ID, URL, and confirmation.
            """
            result = await self._handle_create_record_tool(model, values, lang, ctx)
            return CreateResult(**result)

        @self.app.tool(
            title="Create Records (batch)",
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=True,
            ),
        )
        async def create_records(
            model: str,
            records: List[Dict[str, Any]],
            lang: Optional[str] = None,
            ctx: Optional[Context] = None,
        ) -> Dict[str, Any]:
            """Create multiple records in a single API call (batch operation).

            More efficient than calling create_record multiple times.

            Args:
                model: The Odoo model name (e.g., 'project.task')
                records: List of dictionaries, each containing field values for a record
                lang: Optional one-shot language override (Odoo lang code).

            Returns:
                Dictionary with `success`, `created_count`, `records`.
            """
            return await self._handle_create_records_tool(model, records, lang, ctx)

        @self.app.tool(
            title="Update Record",
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            ),
        )
        async def update_record(
            model: str,
            record_id: int,
            values: Dict[str, Any],
            lang: Optional[str] = None,
            ctx: Optional[Context] = None,
        ) -> UpdateResult:
            """Update an existing record.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                record_id: The record ID to update
                values: Field values to update
                lang: Optional one-shot language override (Odoo lang code).
                    Translatable values in ``values`` are stored under this
                    language only — other languages remain unchanged. Use
                    ``update_field_translations`` to write multiple languages
                    in a single call.

            Returns:
                Updated record details with confirmation.
            """
            result = await self._handle_update_record_tool(model, record_id, values, lang, ctx)
            return UpdateResult(**result)

        @self.app.tool(
            title="Update Records (batch)",
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            ),
        )
        async def update_records(
            model: str,
            record_ids: List[int],
            values: Dict[str, Any],
            lang: Optional[str] = None,
            ctx: Optional[Context] = None,
        ) -> Dict[str, Any]:
            """Update multiple records with the same values (batch operation).

            More efficient than calling update_record multiple times.

            Args:
                model: The Odoo model name (e.g., 'project.task')
                record_ids: List of record IDs to update
                values: Field values to apply to all records
                lang: Optional one-shot language override (Odoo lang code).

            Returns:
                Dictionary with `success`, `updated_count`, `record_ids`.
            """
            return await self._handle_update_records_tool(model, record_ids, values, lang, ctx)

        @self.app.tool(
            title="Delete Record",
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
                openWorldHint=False,
            ),
        )
        async def delete_record(
            model: str,
            record_id: int,
            ctx: Optional[Context] = None,
        ) -> DeleteResult:
            """Delete a record.

            Args:
                model: The Odoo model name (e.g., 'res.partner')
                record_id: The record ID to delete

            Returns:
                Deletion confirmation with the deleted record's name and ID.
            """
            result = await self._handle_delete_record_tool(model, record_id, ctx)
            return DeleteResult(**result)

        @self.app.tool(
            title="Post Message",
            annotations=ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=True,
            ),
        )
        async def post_message(
            model: str,
            record_id: int,
            body: str,
            subtype: Literal["note", "comment"] = "note",
            message_type: Literal["comment", "notification"] = "comment",
            partner_ids: Optional[List[int]] = None,
            attachment_ids: Optional[List[int]] = None,
            body_is_html: bool = False,
            ctx: Optional[Context] = None,
        ) -> PostMessageResult:
            """Post a message to an Odoo record's chatter (mail.thread).

            ``subtype="note"`` (default) is an internal log; ``subtype="comment"``
            notifies followers. Set ``body_is_html=True`` for HTML markup
            (Odoo 17+ escapes str bodies otherwise).

            Args:
                model: Odoo model name (e.g., 'res.partner')
                record_id: Record ID to post to
                body: Message body (plain text by default; HTML if body_is_html=True)
                subtype: 'note' (internal, default) or 'comment' (notifies followers)
                message_type: 'comment' (default) or 'notification'
                partner_ids: Optional list of res.partner IDs to additionally notify
                attachment_ids: Optional list of existing ir.attachment IDs to link
                body_is_html: Treat body as HTML rather than plain text (Odoo 17+)

            Returns:
                Confirmation with the new mail.message ID.
            """
            result = await self._handle_post_message_tool(
                model,
                record_id,
                body,
                subtype,
                message_type,
                partner_ids,
                attachment_ids,
                body_is_html,
                ctx,
            )
            return PostMessageResult(**result)

        @self.app.tool(
            title="Aggregate Records",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            ),
        )
        async def aggregate_records(
            model: str,
            groupby: List[str],
            aggregates: Optional[List[str]] = None,
            domain: Optional[Any] = None,
            order: Optional[str] = None,
            limit: Optional[int] = None,
            offset: int = 0,
            ctx: Optional[Context] = None,
        ) -> AggregateResult:
            """Aggregate records server-side via Odoo's grouping methods.

            Use this tool whenever the question is "totals/counts/groupings",
            not "list of records". It pushes the aggregation down to Odoo
            instead of pulling raw records and reducing client-side.

            Dispatches by Odoo version: ``formatted_read_group`` on 19+
            (the new dedicated method), falls back to ``read_group`` on
            older versions with response-shape normalization. Callers see
            the same response shape on every supported version.

            Args:
                model: Odoo model name (e.g. 'sale.order')
                groupby: One or more group expressions. Field names, optionally
                    with a granularity suffix for date/datetime fields:
                    ``["date_order:month"]``, ``["partner_id"]``,
                    ``["partner_id", "date_order:year"]``.
                aggregates: Aggregate expressions of the form ``"field:operator"``
                    (sum, avg, min, max, count, count_distinct, array_agg, ...).
                    Examples: ``["amount_total:sum"]``, ``["id:count"]``.
                    If omitted or empty, defaults to ``["__count"]`` so each
                    group carries a count. Pass ``["__count", "amount_total:sum"]``
                    to get both.
                domain: Odoo domain filter — list, JSON string, or None.
                order: Sort expression over groupby keys / aggregates,
                    e.g. ``"date_order:month"`` or ``"amount_total:sum desc"``.
                limit: Maximum number of groups. Defaults to
                    ``ODOO_MCP_DEFAULT_LIMIT``; capped at ``ODOO_MCP_MAX_LIMIT``.
                offset: Number of groups to skip.

            Returns:
                ``AggregateResult`` with ``groups`` (list of dicts; each contains
                the groupby keys, ``__count``, and any requested aggregates),
                plus the echoed ``model``, ``groupby``, and ``aggregates``.

            Examples:
                # Sales by month
                aggregate_records(
                    "sale.order",
                    groupby=["date_order:month"],
                    aggregates=["amount_total:sum"],
                    domain=[["state", "in", ["sale", "done"]]],
                )

                # Partner count by country
                aggregate_records("res.partner", groupby=["country_id"])
            """
            result = await self._handle_aggregate_records_tool(
                model, groupby, aggregates, domain, order, limit, offset, ctx
            )
            return AggregateResult(**result)

        # Two-key opt-in: invisible to the client unless both flags are set.
        if self.config.is_write_allowed and self.config.enable_method_calls:
            logger.info("call_model_method tool ENABLED (full YOLO + ODOO_MCP_ENABLE_METHOD_CALLS)")

            @self.app.tool(
                title="Call Model Method",
                annotations=ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=True,
                    idempotentHint=False,
                    openWorldHint=True,
                ),
            )
            async def call_model_method(
                model: str,
                method: str,
                arguments: Optional[Union[List[Any], str]] = None,
                keyword_arguments: Optional[Union[Dict[str, Any], str]] = None,
                ctx: Optional[Context] = None,
            ) -> CallModelMethodResult:
                """Call a public Odoo model method via XML-RPC execute_kw.

                Workflow escape hatch for actions not covered by CRUD: posting an
                invoice (``account.move.action_post``), confirming a sale order
                (``sale.order.action_confirm``), validating a picking, etc.

                Available ONLY when the server runs with full YOLO and
                ``ODOO_MCP_ENABLE_METHOD_CALLS=true``. Odoo still enforces record
                rules and model ACLs for the authenticated user.

                Args:
                    model: Technical model name (e.g. ``account.move``).
                    method: Public Python identifier. Dotted, dashed, whitespace,
                        and ``_``-prefixed names are rejected.
                    arguments: Positional argument list for ``execute_kw``, as a
                        list or JSON-string. For recordset methods, the first
                        element is typically the list of ids: ``[[42]]`` runs on
                        id 42. Defaults to ``[]``.
                    keyword_arguments: Optional dict (or JSON-object string) of
                        keyword arguments for ``execute_kw`` (e.g. ``{"context": {...}}``).

                Returns:
                    ``CallModelMethodResult`` with the raw method return value in
                    ``result`` (bool/dict/list/None depending on the method).

                Prefer ``create_record`` / ``update_record`` / ``delete_record``
                when sufficient.
                """
                result = await self._handle_call_model_method_tool(
                    model, method, arguments, keyword_arguments, ctx
                )
                return CallModelMethodResult(**result)

        @self.app.tool(
            title="List Log Files",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        async def list_log_files(ctx: Optional[Context] = None) -> Dict[str, Any]:
            """List Odoo / system log files visible to the server.

            Requires the ``elevate_informatics_log_reader`` Odoo module to be
            installed (https://github.com/elevateinformatics/elevate_informatics_log_reader).
            Returns a clear error pointing to the install instructions when the
            module is missing.

            Returns:
                Dict with ``files`` (each ``{path, size_bytes, mtime, readable,
                is_configured_logfile}``), ``configured_logfile``, ``dirs_scanned``.
            """
            return await self._handle_list_log_files_tool(ctx)

        @self.app.tool(
            title="Tail Log",
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        async def tail_logs(
            path: Optional[str] = None,
            lines: int = 200,
            grep: Optional[str] = None,
            ctx: Optional[Context] = None,
        ) -> Dict[str, Any]:
            """Return the last ``lines`` lines of an Odoo log file.

            Requires the ``elevate_informatics_log_reader`` Odoo module
            (https://github.com/elevateinformatics/elevate_informatics_log_reader).

            Args:
                path: Absolute path. ``None`` (default) → the active Odoo
                    log file (``tools.config['logfile']``, typically
                    ``~/logs/odoo.log`` on Odoo.sh).
                lines: Number of trailing lines (1..5000).
                grep: Optional substring filter applied after tailing.

            Returns:
                Dict with ``path``, ``lines``, ``count``, ``truncated``,
                ``size_bytes``.
            """
            return await self._handle_tail_logs_tool(path, lines, grep, ctx)

        @self.app.tool()
        async def get_locale() -> Dict[str, Any]:
            """Return the current session locale and the languages installed in Odoo.

            The session locale is the default language injected as ``context.lang``
            on every Odoo call made by this MCP server. It is initialized from the
            ``ODOO_LOCALE`` env var (if set) and can be changed at runtime with
            ``set_locale``. Per-call ``lang`` parameters on other tools take
            precedence and do not modify the session locale.

            Returns:
                Dictionary with:
                - session_locale: Current session lang code (or None if Odoo
                  should fall back to the authenticated user's language).
                - installed_languages: List of active languages in the database
                  (each with ``code``, ``name``, ``iso_code``, ``active``).
            """
            return await self._handle_get_locale_tool()

        @self.app.tool()
        async def set_locale(lang: Optional[str] = None) -> Dict[str, Any]:
            """Set or clear the session locale used as default ``context.lang``.

            Args:
                lang: Odoo lang code to use as the new default (e.g. ``"es_AR"``,
                    ``"en_US"``, ``"fr_FR"``). Pass an empty string or ``null``
                    to clear it; Odoo will then fall back to the authenticated
                    user's language for subsequent calls.

            Returns:
                Dictionary with:
                - previous_locale: Previous session locale (or None).
                - session_locale: New session locale (or None).
                - validated: True if the lang code matches an installed language
                  in Odoo. False (with a warning) if it could not be verified.
            """
            return await self._handle_set_locale_tool(lang)

        @self.app.tool()
        async def get_field_translations(
            model: str,
            record_ids: List[int],
            field_names: List[str],
            langs: Optional[List[str]] = None,
        ) -> Dict[str, Any]:
            """Read all translations of one or more translatable fields.

            Returns the per-language values stored in Odoo's jsonb translation
            storage, regardless of the current session locale. Use this when you
            need to inspect or export multilingual content (product names,
            mail templates, view ``arch_db``, etc.).

            Args:
                model: The Odoo model name (e.g. ``"product.template"``).
                record_ids: List of record IDs to inspect.
                field_names: Translatable fields to read.
                langs: Optional list of Odoo lang codes to restrict the result.
                    ``None`` returns every installed language.

            Returns:
                Dictionary shaped as::

                    {
                      "model": "product.template",
                      "translations": {
                        "<record_id>": {
                          "<field>": [
                            {"lang": "en_US", "source": "...", "value": "..."},
                            {"lang": "es_AR", "source": "...", "value": "..."}
                          ]
                        }
                      }
                    }

                For term-based fields (``arch_db``, qweb HTML), each language
                yields one entry per source-term pair instead of a single
                ``source/value`` pair.
            """
            return await self._handle_get_field_translations_tool(
                model, record_ids, field_names, langs
            )

        @self.app.tool()
        async def update_field_translations(
            model: str,
            record_ids: List[int],
            translations: Dict[str, Dict[str, Any]],
        ) -> Dict[str, Any]:
            """Write translations for multiple languages at once.

            Bypasses ``context.lang`` and writes directly into the jsonb storage
            via Odoo's ``update_field_translations`` API. Works for any
            ``translate=True`` field, including ``ir.ui.view.arch_db`` and qweb
            templates. Languages not present in the payload are left untouched.

            Args:
                model: The Odoo model name.
                record_ids: List of record IDs to update.
                translations: Mapping of field -> {lang_code: value}. For plain
                    translatable fields, ``value`` is the translated string. For
                    term-based fields like ``arch_db`` it is a dict mapping
                    source term -> translated term.

            Example:
                update_field_translations(
                    "product.template",
                    [42],
                    {
                        "name": {"en_US": "Sofa", "es_AR": "Sofá", "fr_FR": "Canapé"},
                        "description_sale": {"en_US": "...", "es_AR": "..."}
                    }
                )

            Returns:
                Dictionary with success flag and the list of updated field/lang
                pairs.
            """
            return await self._handle_update_field_translations_tool(
                model, record_ids, translations
            )

    async def _handle_search_tool(
        self,
        model: str,
        domain: Optional[Any],
        fields: Optional[Any],
        limit: Optional[int],
        offset: int,
        order: Optional[str],
        lang: Optional[str] = None,
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle search tool request."""
        try:
            with perf_logger.track_operation("tool_search", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "read")
                await self._ctx_info(ctx, f"Searching {model}...")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                parsed_domain = self._parse_domain_input(domain)

                # Handle fields parameter - can be string or list
                parsed_fields = fields
                if fields is not None and isinstance(fields, str):
                    # Parse string to list
                    try:
                        parsed_fields = json.loads(fields)
                        if not isinstance(parsed_fields, list):
                            raise ValidationError(
                                f"Fields must be a list, got {type(parsed_fields).__name__}"
                            )
                    except json.JSONDecodeError:
                        # Try Python literal eval as fallback
                        try:
                            import ast

                            parsed_fields = ast.literal_eval(fields)
                            if not isinstance(parsed_fields, list):
                                raise ValidationError(
                                    f"Fields must be a list, got {type(parsed_fields).__name__}"
                                )
                        except (ValueError, SyntaxError) as e:
                            raise ValidationError(
                                f"Invalid fields parameter. Expected JSON array or Python list, got: {fields[:100]}..."
                            ) from e

                # Set defaults
                if limit is None or limit <= 0:
                    limit = self.config.default_limit
                elif limit > self.config.max_limit:
                    limit = self.config.max_limit

                # Get total count (only thread `lang` through when set so mocks
                # asserting on plain (model, domain) still match).
                if lang:
                    total_count = self.connection.search_count(model, parsed_domain, lang=lang)
                else:
                    total_count = self.connection.search_count(model, parsed_domain)
                await self._ctx_progress(ctx, 1, 3, f"Found {total_count} records")

                if lang:
                    record_ids = self.connection.search(
                        model,
                        parsed_domain,
                        lang=lang,
                        limit=limit,
                        offset=offset,
                        order=order,
                    )
                else:
                    record_ids = self.connection.search(
                        model, parsed_domain, limit=limit, offset=offset, order=order
                    )

                # Determine which fields to fetch
                fields_to_fetch = parsed_fields
                if parsed_fields is None:
                    # Use smart field selection to avoid serialization issues
                    fields_to_fetch = self._get_smart_default_fields(model)
                    await self._ctx_info(ctx, f"Using smart field defaults for {model}")
                    logger.debug(
                        f"Using smart defaults for {model} search: {len(fields_to_fetch) if fields_to_fetch else 'all'} fields"
                    )
                elif parsed_fields == ["__all__"]:
                    # Explicit request for all fields
                    fields_to_fetch = None  # Odoo interprets None as all fields
                    await self._ctx_warning(
                        ctx,
                        f"Fetching ALL fields for {model} — may be slow or cause serialization errors",
                    )
                    logger.debug(f"Fetching all fields for {model} search")

                # Read records
                records = []
                if record_ids:
                    if lang:
                        records = self.connection.read(
                            model, record_ids, fields_to_fetch, lang=lang
                        )
                    else:
                        records = self.connection.read(model, record_ids, fields_to_fetch)
                    # Process datetime fields in each record
                    records = [self._process_record_dates(record, model) for record in records]
                await self._ctx_info(ctx, f"Returning {len(records)} records")

                return {
                    "records": records,
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "model": model,
                }

        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in search_records tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Search failed: {sanitized_msg}") from e

    async def _handle_get_record_tool(
        self,
        model: str,
        record_id: int,
        fields: Optional[List[str]],
        lang: Optional[str] = None,
        ctx=None,
    ) -> RecordResult:
        """Handle get record tool request."""
        try:
            with perf_logger.track_operation("tool_get_record", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "read")
                await self._ctx_info(ctx, f"Getting {model}/{record_id}...")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Determine which fields to fetch
                fields_to_fetch = fields
                use_smart_defaults = False
                total_fields = None
                field_selection_method = "explicit"

                if fields is None:
                    # Use smart field selection
                    fields_to_fetch = self._get_smart_default_fields(model)
                    use_smart_defaults = True
                    field_selection_method = "smart_defaults"
                    logger.debug(
                        f"Using smart defaults for {model}: {len(fields_to_fetch) if fields_to_fetch else 'all'} fields"
                    )
                elif fields == ["__all__"]:
                    # Explicit request for all fields
                    fields_to_fetch = None  # Odoo interprets None as all fields
                    field_selection_method = "all"
                    logger.debug(f"Fetching all fields for {model}")
                else:
                    # Specific fields requested
                    logger.debug(f"Fetching specific fields for {model}: {fields}")

                # Read the record
                if lang:
                    records = self.connection.read(model, [record_id], fields_to_fetch, lang=lang)
                else:
                    records = self.connection.read(model, [record_id], fields_to_fetch)

                if not records:
                    raise ValidationError(f"Record not found: {model} with ID {record_id}")

                # Process datetime fields in the record
                record = self._process_record_dates(records[0], model)

                # Build metadata when using smart defaults
                metadata = None
                if use_smart_defaults:
                    try:
                        all_fields_info = self.connection.fields_get(model)
                        total_fields = len(all_fields_info)
                    except Exception:
                        pass

                    metadata = FieldSelectionMetadata(
                        fields_returned=len(record),
                        field_selection_method=field_selection_method,
                        total_fields_available=total_fields,
                        note=f"Limited fields returned for performance. Use fields=['__all__'] for all fields or see odoo://{model}/fields for available fields.",
                    )

                return RecordResult(record=record, metadata=metadata)

        except ValidationError:
            raise
        except NotFoundError as e:
            raise ValidationError(str(e)) from e
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in get_record tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to get record: {sanitized_msg}") from e

    async def _handle_read_records_tool(
        self,
        model: str,
        record_ids: List[int],
        fields: Optional[List[str]],
        lang: Optional[str] = None,
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle read records (batch) tool request."""
        try:
            with perf_logger.track_operation("tool_read_records", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "read")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate input
                if not record_ids:
                    raise ValidationError("No record IDs provided for batch read")

                # Determine which fields to fetch
                fields_to_fetch = fields
                if fields is None:
                    # Use smart field selection
                    fields_to_fetch = self._get_smart_default_fields(model)
                    logger.debug(
                        f"Using smart defaults for {model} batch read: "
                        f"{len(fields_to_fetch) if fields_to_fetch else 'all'} fields"
                    )
                elif fields == ["__all__"]:
                    # Explicit request for all fields
                    fields_to_fetch = None
                    logger.debug(f"Fetching all fields for {model} batch read")

                # Read records (single API call)
                if lang:
                    records = self.connection.read(model, record_ids, fields_to_fetch, lang=lang)
                else:
                    records = self.connection.read(model, record_ids, fields_to_fetch)

                # Process datetime fields in each record
                records = [self._process_record_dates(record, model) for record in records]

                # Identify missing IDs
                returned_ids = {record["id"] for record in records}
                missing_ids = sorted(set(record_ids) - returned_ids)

                return {
                    "records": records,
                    "count": len(records),
                    "missing_ids": missing_ids,
                    "model": model,
                }

        except ValidationError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in read_records tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to read records: {sanitized_msg}") from e

    async def _handle_list_models_tool(self, ctx=None) -> Dict[str, Any]:
        """Handle list models tool request with permissions."""
        try:
            with perf_logger.track_operation("tool_list_models"):
                await self._ctx_info(ctx, "Listing available models...")
                # Check if YOLO mode is enabled
                if self.config.is_yolo_enabled:
                    # Query actual models from ir.model in YOLO mode
                    try:
                        # Exclude transient models and less useful system models
                        domain = [
                            "&",
                            ("transient", "=", False),
                            "|",
                            "|",
                            ("model", "not like", "ir.%"),
                            ("model", "not like", "base.%"),
                            (
                                "model",
                                "in",
                                [
                                    "ir.attachment",
                                    "ir.model",
                                    "ir.model.fields",
                                    "ir.config_parameter",
                                ],
                            ),
                        ]

                        # Query models from database
                        model_records = self.connection.search_read(
                            "ir.model",
                            domain,
                            ["model", "name"],
                            order="name ASC",
                            limit=200,  # Reasonable limit for practical use
                        )

                        # Prepare response with YOLO mode metadata
                        mode_desc = (
                            "READ-ONLY" if self.config.yolo_mode == "read" else "FULL ACCESS"
                        )
                        await self._ctx_info(
                            ctx,
                            f"YOLO mode ({mode_desc}): found {len(model_records)} models",
                        )

                        # Create metadata about YOLO mode
                        yolo_metadata = {
                            "enabled": True,
                            "level": self.config.yolo_mode,  # "read" or "true"
                            "description": mode_desc,
                            "warning": "🚨 All models accessible without MCP security!",
                            "operations": {
                                "read": True,
                                "write": self.config.yolo_mode == "true",
                                "create": self.config.yolo_mode == "true",
                                "unlink": self.config.yolo_mode == "true",
                            },
                        }

                        # Process actual models (clean data without permissions)
                        models_list = []
                        for record in model_records:
                            model_entry = {
                                "model": record["model"],
                                "name": record["name"] or record["model"],
                            }
                            models_list.append(model_entry)

                        logger.info(
                            f"YOLO mode ({mode_desc}): Listed {len(model_records)} models from database"
                        )

                        return {
                            "yolo_mode": yolo_metadata,
                            "models": models_list,
                            "total": len(models_list),
                        }

                    except Exception as e:
                        logger.error(f"Failed to query models in YOLO mode: {e}")
                        # Return error in consistent structure
                        mode_desc = (
                            "READ-ONLY" if self.config.yolo_mode == "read" else "FULL ACCESS"
                        )
                        return {
                            "yolo_mode": {
                                "enabled": True,
                                "level": self.config.yolo_mode,
                                "description": mode_desc,
                                "warning": f"⚠️ Error querying models: {str(e)}",
                                "operations": {
                                    "read": False,
                                    "write": False,
                                    "create": False,
                                    "unlink": False,
                                },
                            },
                            "models": [],
                            "total": 0,
                            "error": str(e),
                        }

                # Standard mode: Get models from MCP access controller
                models = self.access_controller.get_enabled_models()

                # Enrich with permissions for each model
                if models:
                    await self._ctx_info(ctx, f"Enriching {len(models)} models...")
                enriched_models = []
                for model_info in models:
                    model_name = model_info["model"]
                    try:
                        # Get permissions for this model
                        permissions = self.access_controller.get_model_permissions(model_name)
                        enriched_model = {
                            "model": model_name,
                            "name": model_info["name"],
                            "operations": {
                                "read": permissions.can_read,
                                "write": permissions.can_write,
                                "create": permissions.can_create,
                                "unlink": permissions.can_unlink,
                            },
                        }
                        enriched_models.append(enriched_model)
                    except Exception as e:
                        # If we can't get permissions for a model, include it with all operations false
                        logger.warning(f"Failed to get permissions for {model_name}: {e}")
                        enriched_model = {
                            "model": model_name,
                            "name": model_info["name"],
                            "operations": {
                                "read": False,
                                "write": False,
                                "create": False,
                                "unlink": False,
                            },
                        }
                        enriched_models.append(enriched_model)

                # Return proper JSON structure with enriched models array
                return {"models": enriched_models}
        except ValidationError:
            raise
        except Exception as e:
            logger.error(f"Error in list_models tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to list models: {sanitized_msg}") from e

    async def _handle_list_resource_templates_tool(self, ctx=None) -> Dict[str, Any]:
        """Handle list resource templates tool request."""
        try:
            await self._ctx_info(ctx, "Listing resource templates...")
            # Get list of enabled models that can be used with resources
            enabled_models = self.access_controller.get_enabled_models()
            model_names = [m["model"] for m in enabled_models if m.get("read", True)]

            # Define the resource templates
            templates = [
                {
                    "uri_template": "odoo://{model}/record/{record_id}",
                    "description": "Get a specific record by ID",
                    "parameters": {
                        "model": "Odoo model name (e.g., res.partner)",
                        "record_id": "Record ID (e.g., 10)",
                    },
                    "example": "odoo://res.partner/record/10",
                },
                {
                    "uri_template": "odoo://{model}/search",
                    "description": "Basic search returning first 10 records",
                    "parameters": {
                        "model": "Odoo model name",
                    },
                    "example": "odoo://res.partner/search",
                    "note": "Query parameters are not supported. Use search_records tool for advanced queries.",
                },
                {
                    "uri_template": "odoo://{model}/count",
                    "description": "Count all records in a model",
                    "parameters": {
                        "model": "Odoo model name",
                    },
                    "example": "odoo://res.partner/count",
                    "note": "Query parameters are not supported. Use search_records tool for filtered counts.",
                },
                {
                    "uri_template": "odoo://{model}/fields",
                    "description": "Get field definitions for a model",
                    "parameters": {"model": "Odoo model name"},
                    "example": "odoo://res.partner/fields",
                },
            ]

            # Return the resource template information
            return {
                "templates": templates,
                "enabled_models": model_names[:10],  # Show first 10 as examples
                "total_models": len(model_names),
                "note": "Resource URIs do not support query parameters. Use tools (search_records, get_record) for advanced operations with filtering, pagination, and field selection.",
            }

        except Exception as e:
            logger.error(f"Error in list_resource_templates tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to list resource templates: {sanitized_msg}") from e

    async def _handle_create_record_tool(
        self,
        model: str,
        values: Dict[str, Any],
        lang: Optional[str] = None,
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle create record tool request."""
        try:
            with perf_logger.track_operation("tool_create_record", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "create")
                await self._ctx_info(ctx, f"Creating record in {model}...")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate required fields
                if not values:
                    raise ValidationError("No values provided for record creation")

                # Create the record
                if lang:
                    record_id = self.connection.create(model, values, lang=lang)
                else:
                    record_id = self.connection.create(model, values)

                # Return only essential fields to minimize context usage
                # Users can use get_record if they need more fields
                # Only use universally available fields — 'name' is missing from
                # some models (e.g. survey.survey uses 'title').
                essential_fields = ["id", "display_name"]

                # Read only the essential fields
                records = self.connection.read(model, [record_id], essential_fields)
                if not records:
                    raise ValidationError(
                        f"Failed to read created record: {model} with ID {record_id}"
                    )

                # Process dates in the minimal record
                record = self._process_record_dates(records[0], model)

                record_url = self.connection.build_record_url(model, record_id)

                return {
                    "success": True,
                    "record": record,
                    "url": record_url,
                    "message": f"Successfully created {model} record with ID {record_id}",
                }

        except ValidationError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in create_record tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to create record: {sanitized_msg}") from e

    async def _handle_create_records_tool(
        self,
        model: str,
        records: List[Dict[str, Any]],
        lang: Optional[str] = None,
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle create records (batch) tool request."""
        try:
            with perf_logger.track_operation("tool_create_records", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "create")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate input
                if not records:
                    raise ValidationError("No records provided for batch creation")

                if not isinstance(records, list):
                    raise ValidationError("Records must be a list of dictionaries")

                for i, record in enumerate(records):
                    if not isinstance(record, dict):
                        raise ValidationError(f"Record at index {i} must be a dictionary")
                    if not record:
                        raise ValidationError(f"Record at index {i} is empty")

                # Create all records in a single API call
                # Odoo's create method accepts a list of dicts and returns a list of IDs
                if lang:
                    record_ids = self.connection.create(model, records, lang=lang)
                else:
                    record_ids = self.connection.create(model, records)

                # Handle single ID return (when only one record was created)
                if isinstance(record_ids, int):
                    record_ids = [record_ids]

                # Read essential fields for all created records
                essential_fields = ["id", "display_name"]
                created_records = self.connection.read(model, record_ids, essential_fields)

                # Process dates in the records
                created_records = [
                    self._process_record_dates(record, model) for record in created_records
                ]

                return {
                    "success": True,
                    "created_count": len(record_ids),
                    "records": created_records,
                    "message": f"Successfully created {len(record_ids)} {model} records",
                }

        except ValidationError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in create_records tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to create records: {sanitized_msg}") from e

    async def _handle_update_record_tool(
        self,
        model: str,
        record_id: int,
        values: Dict[str, Any],
        lang: Optional[str] = None,
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle update record tool request."""
        try:
            with perf_logger.track_operation("tool_update_record", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "write")
                await self._ctx_info(ctx, f"Updating {model}/{record_id}...")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate input
                if not values:
                    raise ValidationError("No values provided for record update")

                # Check if record exists (only fetch ID to verify existence)
                existing = self.connection.read(model, [record_id], ["id"])
                if not existing:
                    raise NotFoundError(f"Record not found: {model} with ID {record_id}")

                # Update the record
                if lang:
                    success = self.connection.write(model, [record_id], values, lang=lang)
                else:
                    success = self.connection.write(model, [record_id], values)

                # Return only essential fields to minimize context usage
                # Users can use get_record if they need more fields
                # Only use universally available fields — 'name' is missing from
                # some models (e.g. survey.survey uses 'title').
                essential_fields = ["id", "display_name"]

                # Read only the essential fields
                records = self.connection.read(model, [record_id], essential_fields)
                if not records:
                    raise ValidationError(
                        f"Failed to read updated record: {model} with ID {record_id}"
                    )

                # Process dates in the minimal record
                record = self._process_record_dates(records[0], model)

                record_url = self.connection.build_record_url(model, record_id)

                return {
                    "success": success,
                    "record": record,
                    "url": record_url,
                    "message": f"Successfully updated {model} record with ID {record_id}",
                }

        except ValidationError:
            raise
        except NotFoundError as e:
            raise ValidationError(str(e)) from e
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in update_record tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to update record: {sanitized_msg}") from e

    async def _handle_update_records_tool(
        self,
        model: str,
        record_ids: List[int],
        values: Dict[str, Any],
        lang: Optional[str] = None,
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle update records (batch) tool request."""
        try:
            with perf_logger.track_operation("tool_update_records", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "write")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate input
                if not record_ids:
                    raise ValidationError("No record IDs provided for batch update")

                if not values:
                    raise ValidationError("No values provided for batch update")

                # Verify all records exist (fetch only IDs to minimize overhead)
                existing = self.connection.read(model, record_ids, ["id"])
                existing_ids = {record["id"] for record in existing}
                missing_ids = set(record_ids) - existing_ids

                if missing_ids:
                    raise ValidationError(
                        f"Records not found: {model} with IDs {sorted(missing_ids)}"
                    )

                # Perform batch update (single API call)
                if lang:
                    success = self.connection.write(model, record_ids, values, lang=lang)
                else:
                    success = self.connection.write(model, record_ids, values)

                return {
                    "success": success,
                    "updated_count": len(record_ids),
                    "record_ids": record_ids,
                    "message": f"Successfully updated {len(record_ids)} {model} records",
                }

        except ValidationError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in update_records tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to update records: {sanitized_msg}") from e

    # ------------------------------------------------------------------
    # Log reader (companion module: elevate_informatics_log_reader)
    # ------------------------------------------------------------------

    LOG_READER_MODEL = "elevate.log.reader"
    LOG_READER_INSTALL_HINT = (
        "Install the Odoo module 'elevate_informatics_log_reader' "
        "(https://github.com/elevateinformatics/elevate_informatics_log_reader) "
        "and ensure the calling user has the 'Settings / Administration' group."
    )

    def _ensure_log_reader_available(self) -> None:
        """Raise ValidationError with a helpful hint if the log reader model is missing."""
        if not self.connection.is_authenticated:
            raise ValidationError("Not authenticated with Odoo")
        try:
            existing = self.connection.search(
                "ir.model", [("model", "=", self.LOG_READER_MODEL)], limit=1
            )
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error while probing log reader: {e}") from e
        if not existing:
            raise ValidationError(
                f"Model '{self.LOG_READER_MODEL}' not found on this Odoo. "
                f"{self.LOG_READER_INSTALL_HINT}"
            )

    async def _handle_list_log_files_tool(self, ctx=None) -> Dict[str, Any]:
        """Delegate to elevate.log.reader.list_files()."""
        try:
            with perf_logger.track_operation("tool_list_log_files"):
                self._ensure_log_reader_available()
                await self._ctx_info(ctx, "Listing Odoo log files...")
                return self.connection.execute_kw(self.LOG_READER_MODEL, "list_files", [], {})
        except ValidationError:
            raise
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in list_log_files tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to list log files: {sanitized_msg}") from e

    async def _handle_tail_logs_tool(
        self,
        path: Optional[str],
        lines: int,
        grep: Optional[str],
        ctx=None,
    ) -> Dict[str, Any]:
        """Delegate to elevate.log.reader.tail()."""
        try:
            with perf_logger.track_operation("tool_tail_logs"):
                self._ensure_log_reader_available()
                kwargs: Dict[str, Any] = {"lines": lines}
                if path is not None:
                    kwargs["path"] = path
                if grep is not None:
                    kwargs["grep"] = grep
                await self._ctx_info(ctx, f"Tailing log{f' {path}' if path else ' (default)'}...")
                return self.connection.execute_kw(self.LOG_READER_MODEL, "tail", [], kwargs)
        except ValidationError:
            raise
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in tail_logs tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to tail log: {sanitized_msg}") from e

    async def _handle_get_locale_tool(self) -> Dict[str, Any]:
        """Handle get_locale tool request."""
        try:
            with perf_logger.track_operation("tool_get_locale"):
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                installed: List[Dict[str, Any]] = []
                try:
                    installed = self.connection.list_installed_languages()
                except Exception as e:
                    logger.warning(f"Could not list installed languages: {e}")

                return {
                    "session_locale": self.connection.get_session_locale(),
                    "installed_languages": installed,
                }
        except ValidationError:
            raise
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in get_locale tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to get locale: {sanitized_msg}") from e

    async def _handle_set_locale_tool(self, lang: Optional[str]) -> Dict[str, Any]:
        """Handle set_locale tool request."""
        try:
            with perf_logger.track_operation("tool_set_locale"):
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                previous = self.connection.get_session_locale()
                normalized = lang.strip() if isinstance(lang, str) and lang.strip() else None

                # Validate against installed languages when a lang is requested.
                # Failure to validate (e.g. perms on res.lang) is non-fatal — we
                # still set the locale but flag validated=False.
                validated = False
                installed_codes: List[str] = []
                if normalized is not None:
                    try:
                        languages = self.connection.list_installed_languages()
                        installed_codes = [lng["code"] for lng in languages if lng.get("code")]
                        validated = normalized in installed_codes
                        if not validated:
                            raise ValidationError(
                                f"Language code '{normalized}' is not installed in Odoo. "
                                f"Installed: {', '.join(installed_codes) or '(none queryable)'}"
                            )
                    except ValidationError:
                        raise
                    except Exception as e:
                        logger.warning(
                            f"Could not validate lang '{normalized}' against res.lang: {e}"
                        )

                new_locale = self.connection.set_session_locale(normalized)

                return {
                    "previous_locale": previous,
                    "session_locale": new_locale,
                    "validated": validated,
                    "message": (
                        f"Session locale cleared (was {previous!r})"
                        if new_locale is None
                        else f"Session locale set to {new_locale!r} (was {previous!r})"
                    ),
                }
        except ValidationError:
            raise
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in set_locale tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to set locale: {sanitized_msg}") from e

    async def _handle_get_field_translations_tool(
        self,
        model: str,
        record_ids: List[int],
        field_names: List[str],
        langs: Optional[List[str]],
    ) -> Dict[str, Any]:
        """Handle get_field_translations tool request."""
        try:
            with perf_logger.track_operation("tool_get_field_translations", model=model):
                self.access_controller.validate_model_access(model, "read")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")
                if not record_ids:
                    raise ValidationError("No record IDs provided")
                if not field_names:
                    raise ValidationError("No field names provided")

                translations: Dict[int, Dict[str, Any]] = {rid: {} for rid in record_ids}
                for field_name in field_names:
                    per_record = self.connection.get_field_translations(
                        model, record_ids, field_name, langs
                    )
                    for rid, payload in per_record.items():
                        translations.setdefault(rid, {})[field_name] = payload

                return {
                    "model": model,
                    "record_ids": record_ids,
                    "field_names": field_names,
                    "langs": langs,
                    # JSON-friendly: stringify int keys
                    "translations": {str(rid): data for rid, data in translations.items()},
                }
        except ValidationError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in get_field_translations tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to get field translations: {sanitized_msg}") from e

    async def _handle_update_field_translations_tool(
        self,
        model: str,
        record_ids: List[int],
        translations: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Handle update_field_translations tool request."""
        try:
            with perf_logger.track_operation("tool_update_field_translations", model=model):
                self.access_controller.validate_model_access(model, "write")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")
                if not record_ids:
                    raise ValidationError("No record IDs provided")
                if not translations or not isinstance(translations, dict):
                    raise ValidationError(
                        "translations must be a non-empty dict of field -> {lang: value}"
                    )

                # Verify records exist
                existing = self.connection.read(model, record_ids, ["id"])
                existing_ids = {record["id"] for record in existing}
                missing = sorted(set(record_ids) - existing_ids)
                if missing:
                    raise NotFoundError(f"Records not found: {model} with IDs {missing}")

                applied: List[Dict[str, Any]] = []
                for field_name, per_lang in translations.items():
                    if not isinstance(per_lang, dict) or not per_lang:
                        raise ValidationError(
                            f"Translations for '{field_name}' must be a non-empty "
                            "{lang_code: value} dict"
                        )
                    applied.append({"field": field_name, "langs": sorted(per_lang.keys())})

                success = self.connection.update_field_translations(model, record_ids, translations)

                return {
                    "success": bool(success),
                    "model": model,
                    "record_ids": record_ids,
                    "applied": applied,
                    "message": (
                        f"Updated translations for {len(applied)} field(s) on "
                        f"{len(record_ids)} {model} record(s)"
                    ),
                }
        except ValidationError:
            raise
        except NotFoundError as e:
            raise ValidationError(str(e)) from e
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in update_field_translations tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to update field translations: {sanitized_msg}") from e

    async def _handle_delete_record_tool(
        self,
        model: str,
        record_id: int,
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle delete record tool request."""
        try:
            with perf_logger.track_operation("tool_delete_record", model=model):
                # Check model access
                self.access_controller.validate_model_access(model, "unlink")
                await self._ctx_info(ctx, f"Deleting {model}/{record_id}...")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Check if record exists and get display info
                existing = self.connection.read(model, [record_id], ["id", "display_name"])
                if not existing:
                    raise NotFoundError(f"Record not found: {model} with ID {record_id}")

                # Store some info about the record before deletion
                record_name = existing[0].get("display_name", f"ID {record_id}")

                # Delete the record
                success = self.connection.unlink(model, [record_id])

                return {
                    "success": success,
                    "deleted_id": record_id,
                    "deleted_name": record_name,
                    "message": f"Successfully deleted {model} record '{record_name}' (ID: {record_id})",
                }

        except ValidationError:
            raise
        except NotFoundError as e:
            raise ValidationError(str(e)) from e
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in delete_record tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to delete record: {sanitized_msg}") from e

    async def _handle_post_message_tool(
        self,
        model: str,
        record_id: int,
        body: str,
        subtype: str,
        message_type: str,
        partner_ids: Optional[List[int]],
        attachment_ids: Optional[List[int]],
        body_is_html: bool,
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle post message tool request."""
        subtype_xmlid_map = {
            "note": "mail.mt_note",
            "comment": "mail.mt_comment",
        }
        try:
            with perf_logger.track_operation("tool_post_message", model=model):
                # Check model access — message_post mutates the record
                self.access_controller.validate_model_access(model, "write")
                await self._ctx_info(ctx, f"Posting message to {model}/{record_id}...")

                # Ensure we're connected
                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate body before any XML-RPC call
                if not body or not body.strip():
                    raise ValidationError("body must not be empty")

                # Build kwargs — omit partner_ids/attachment_ids when None
                # (empty list means "clear all" in some Odoo contexts)
                kwargs: Dict[str, Any] = {
                    "body": body,
                    "message_type": message_type,
                    "subtype_xmlid": subtype_xmlid_map[subtype],
                }
                if partner_ids is not None:
                    kwargs["partner_ids"] = partner_ids
                if attachment_ids is not None:
                    kwargs["attachment_ids"] = attachment_ids
                if body_is_html:
                    # Odoo 19 escapes any plain str body — opt-in flag preserves HTML
                    kwargs["body_is_html"] = True

                # Call message_post; translate the "no mail.thread" error before
                # the outer ladder turns it into a generic "Connection error".
                try:
                    raw = self.connection.execute_kw(model, "message_post", [record_id], kwargs)
                except OdooConnectionError as e:
                    err_msg = str(e)
                    if "message_post" in err_msg and (
                        "has no attribute" in err_msg
                        or "AttributeError" in err_msg
                        or "does not exist" in err_msg
                    ):
                        raise ValidationError(
                            f"Model '{model}' does not support chatter "
                            "(no mail.thread inheritance)."
                        ) from e
                    raise

                # Coerce return value to int message_id
                if isinstance(raw, bool) or raw is None:
                    raise ValidationError(f"Unexpected return from message_post: {raw!r}")
                if isinstance(raw, int):
                    message_id = raw
                elif isinstance(raw, list) and raw and isinstance(raw[0], int):
                    message_id = raw[0]
                else:
                    raise ValidationError(f"Unexpected return from message_post: {raw!r}")

                return {
                    "success": True,
                    "message_id": message_id,
                }

        except ValidationError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in post_message tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to post message: {sanitized_msg}") from e

    # Metadata keys we always preserve in normalized read_group output.
    # Anything else not in the requested groupby/aggregates is filtered
    # out — read_group with empty ``fields=`` defaults to ALL aggregator
    # fields on the model, which leaks unrelated numeric fields.
    _READ_GROUP_META_KEYS = frozenset({"__count", "__extra_domain", "__range", "__fold"})

    def _call_read_group_normalized(
        self,
        model: str,
        domain: List[Any],
        groupby: List[str],
        aggregates: List[str],
        order: Optional[str],
        limit: int,
        offset: int,
    ) -> List[Dict[str, Any]]:
        """Call legacy ``read_group`` and normalize its response shape.

        Odoo < 19 doesn't have ``formatted_read_group``. ``read_group`` is
        the long-standing alternative; with ``lazy=False`` its response is
        already close to the v19 shape. Three normalizations:

        * ``__domain`` → ``__extra_domain`` (key rename, per v19 convention).
        * Aggregate keys: read_group emits aggregate values keyed by the
          bare field name (e.g. ``"id:count"`` is returned as ``"id"``);
          rename back to ``"field:op"`` to match v19.
        * Bucket key whitelist: drop fields the caller didn't request.
          read_group with empty ``fields=`` returns all aggregator fields
          on the model (e.g. ``message_bounce``, ``partner_latitude``);
          formatted_read_group never does that. Filter to keep only what
          the caller asked for plus metadata keys (``__count``, etc.).

        Translates kwargs:
            * ``aggregates`` → ``fields`` (drop ``__count``; read_group emits
              it implicitly when ``lazy=False``).
            * ``order`` → ``orderby`` (omit entirely when ``None`` so
              read_group uses its default).
        """
        # __count is implicit in read_group; passing it as a field raises a fault.
        fields_kwarg = [a for a in aggregates if a != "__count"]

        kwargs: Dict[str, Any] = {
            "fields": fields_kwarg,
            "groupby": groupby,
            "limit": limit,
            "offset": offset,
            "lazy": False,
        }
        if order is not None:
            kwargs["orderby"] = order

        groups = self.connection.execute_kw(model, "read_group", [domain], kwargs)

        # Aggregate key rename: build a list of (bare_field, full_expr)
        # pairs to restore after read_group strips the operator suffix.
        # Skip aggregates whose bare field collides with a groupby key —
        # the groupby value already lives under that key.
        groupby_field_names = {g.split(":", 1)[0] for g in groupby}
        agg_renames = [
            (a.split(":", 1)[0], a)
            for a in fields_kwarg
            if ":" in a and a.split(":", 1)[0] not in groupby_field_names
        ]

        # Whitelist of keys allowed in the final bucket: groupby specs +
        # requested aggregates (post-rename) + known metadata keys.
        allowed_keys = self._READ_GROUP_META_KEYS | set(groupby) | set(fields_kwarg)

        normalized: List[Dict[str, Any]] = []
        for bucket in groups:
            if "__domain" in bucket:
                bucket["__extra_domain"] = bucket.pop("__domain")
            for bare, full in agg_renames:
                if bare in bucket and full != bare:
                    bucket[full] = bucket.pop(bare)
            normalized.append({k: v for k, v in bucket.items() if k in allowed_keys})
        return normalized

    async def _handle_aggregate_records_tool(
        self,
        model: str,
        groupby: List[str],
        aggregates: Optional[List[str]],
        domain: Optional[Any],
        order: Optional[str],
        limit: Optional[int],
        offset: int,
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle aggregate_records tool request."""
        try:
            with perf_logger.track_operation("tool_aggregate_records", model=model):
                # Access check (read permission — same as search_records)
                self.access_controller.validate_model_access(model, "read")
                await self._ctx_info(ctx, f"Aggregating {model}...")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                # Validate groupby — empty groupby collapses to a single
                # bucket, which search_count already covers.
                if not groupby:
                    raise ValidationError(
                        "groupby must not be empty (use search_count for an unfiltered total)."
                    )

                parsed_domain = self._parse_domain_input(domain)

                # Limit defaults & capping (mirror search_records)
                if limit is None or limit <= 0:
                    limit = self.config.default_limit
                elif limit > self.config.max_limit:
                    limit = self.config.max_limit

                # Default to ['__count'] when caller omits aggregates —
                # otherwise formatted_read_group returns only the groupby
                # keys with no quantitative data, which defeats the tool.
                effective_aggregates = aggregates if aggregates else ["__count"]

                # Version dispatch: formatted_read_group is Odoo 19+ only;
                # fall back to read_group with response normalization on
                # older versions. When the version is unknown (None), assume
                # newer and let the XML-RPC fault surface — the caller can
                # set ODOO_DB or check the connection log.
                major = self.connection.get_major_version()
                if major is not None and major < 19:
                    groups = self._call_read_group_normalized(
                        model, parsed_domain, groupby, effective_aggregates, order, limit, offset
                    )
                else:
                    kwargs: Dict[str, Any] = {
                        "groupby": groupby,
                        "aggregates": effective_aggregates,
                        "limit": limit,
                        "offset": offset,
                    }
                    if order is not None:
                        kwargs["order"] = order
                    groups = self.connection.execute_kw(
                        model, "formatted_read_group", [parsed_domain], kwargs
                    )

                await self._ctx_info(ctx, f"Returning {len(groups)} groups")

                return {
                    "groups": groups,
                    "model": model,
                    "groupby": groupby,
                    "aggregates": effective_aggregates,
                }

        except ValidationError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in aggregate_records tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Aggregation failed: {sanitized_msg}") from e

    @staticmethod
    def _parse_execute_kw_arguments(value: Optional[Any]) -> List[Any]:
        """Coerce the ``arguments`` parameter to a list (JSON-only)."""
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            if len(value) > _MAX_JSON_PARAM_BYTES:
                raise ValidationError(
                    f"arguments JSON-string exceeds {_MAX_JSON_PARAM_BYTES} bytes"
                )
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as e:
                raise ValidationError(
                    f"Invalid arguments parameter. Expected JSON array, got: {value[:100]}"
                ) from e
            if not isinstance(parsed, list):
                raise ValidationError(f"arguments must be a list, got {type(parsed).__name__}")
            return parsed
        raise ValidationError(
            f"arguments must be a list or JSON-string, got {type(value).__name__}"
        )

    @staticmethod
    def _parse_execute_kw_kwargs(value: Optional[Any]) -> Dict[str, Any]:
        """Coerce the ``keyword_arguments`` parameter to a dict (JSON-only)."""
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            if len(value) > _MAX_JSON_PARAM_BYTES:
                raise ValidationError(
                    f"keyword_arguments JSON-string exceeds {_MAX_JSON_PARAM_BYTES} bytes"
                )
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as e:
                raise ValidationError(
                    f"Invalid keyword_arguments parameter. Expected JSON object, got: {value[:100]}"
                ) from e
            if not isinstance(parsed, dict):
                raise ValidationError(
                    f"keyword_arguments must be a dict, got {type(parsed).__name__}"
                )
            return parsed
        raise ValidationError(
            f"keyword_arguments must be a dict or JSON-string, got {type(value).__name__}"
        )

    async def _handle_call_model_method_tool(
        self,
        model: str,
        method: str,
        arguments: Optional[Any],
        keyword_arguments: Optional[Any],
        ctx=None,
    ) -> Dict[str, Any]:
        """Handle call_model_method tool request."""
        try:
            with perf_logger.track_operation("tool_call_model_method", model=model):
                model = (model or "").strip()
                method = (method or "").strip()
                if not model:
                    raise ValidationError("model must not be empty")
                if not method:
                    raise ValidationError("method must not be empty")
                if not _PUBLIC_METHOD_RE.fullmatch(method):
                    raise ValidationError(
                        f"Refusing to call '{method}': only public ASCII Python "
                        "identifiers are accepted; dotted, dashed, whitespace, "
                        "non-ASCII, and _-prefixed names are rejected."
                    )

                # No-op under full YOLO; placeholder if the gate ever loosens.
                self.access_controller.validate_model_access(model, "write")
                await self._ctx_info(ctx, f"Calling {model}.{method}(...)")

                if not self.connection.is_authenticated:
                    raise ValidationError("Not authenticated with Odoo")

                args_list = self._parse_execute_kw_arguments(arguments)
                kwargs_dict = self._parse_execute_kw_kwargs(keyword_arguments)

                # Audit only what was called, not the values — kwargs may carry PII.
                logger.info(
                    "call_model_method invoked: model=%s method=%s args_len=%d kwargs_keys=%s",
                    model,
                    method,
                    len(args_list),
                    sorted(kwargs_dict.keys()),
                )

                rpc_result = self.connection.execute_kw(model, method, args_list, kwargs_dict)

                # Workflow methods mutate state; flush the model's cache.
                self.connection.performance_manager.invalidate_record_cache(model)

                return {
                    "success": True,
                    "result": _json_safe(rpc_result),
                    "message": f"Successfully called {model}.{method}",
                }

        except ValidationError:
            raise
        except AccessControlError as e:
            raise ValidationError(f"Access denied: {e}") from e
        except OdooConnectionError as e:
            raise ValidationError(f"Connection error: {e}") from e
        except Exception as e:
            logger.error(f"Error in call_model_method tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to call model method: {sanitized_msg}") from e


def register_tools(
    app: FastMCP,
    connection: OdooConnection,
    access_controller: AccessController,
    config: OdooConfig,
) -> OdooToolHandler:
    """Register all Odoo tools with the FastMCP app.

    Args:
        app: FastMCP application instance
        connection: Odoo connection instance
        access_controller: Access control instance
        config: Odoo configuration instance

    Returns:
        The tool handler instance
    """
    handler = OdooToolHandler(app, connection, access_controller, config)
    logger.info("Registered Odoo MCP tools")
    return handler
