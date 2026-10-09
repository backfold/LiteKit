"""SvelteKit-style file-based routing for Litestar."""

from litekit._layout import Layout, LayoutOptions
from litekit._router import file_router
from litekit.exceptions import LiteKitWarning, RouteConfigError

__all__ = ["Layout", "LayoutOptions", "LiteKitWarning", "RouteConfigError", "file_router"]
