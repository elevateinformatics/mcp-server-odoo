"""MCP Server for Odoo - Model Context Protocol server for Odoo ERP systems."""

from importlib.metadata import PackageNotFoundError, version

__version__: str
try:
    __version__ = version("mcp-server-odoo-ei")
except PackageNotFoundError:
    # Package not installed (development mode)
    __version__ = "0.0.0-dev"

__author__ = "Luciano Bustos"
__license__ = "MPL-2.0"

from .access_control import AccessControlError, AccessController, ModelPermissions
from .config import OdooConfig, load_config
from .odoo_connection import OdooConnection, OdooConnectionError, create_connection
from .server import OdooMCPServer

__all__ = [
    "OdooMCPServer",
    "OdooConfig",
    "load_config",
    "OdooConnection",
    "OdooConnectionError",
    "create_connection",
    "AccessController",
    "AccessControlError",
    "ModelPermissions",
    "__version__",
]
