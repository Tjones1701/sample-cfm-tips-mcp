"""
AWS Lambda handler for the CFM Tips MCP server.

Uses Mangum as the ASGI-to-Lambda adapter with a minimal Starlette app that
routes API Gateway requests to the MCP SDK's StreamableHTTPServerTransport.

Architecture:
    - Starlette app with Route("/mcp") delegates to the MCP transport's
      handle_request ASGI callable.
    - On first invocation, an asyncio background task starts transport.connect()
      + server.run() in the SAME event loop that Mangum uses.  This avoids the
      cross-event-loop threading issues that cause Lambda timeouts.
    - Mangum translates API Gateway v2 events into ASGI calls.

CDK Packaging Notes:
    The CDK PythonFunction bundles the contents of sample-cfm-tips-mcp/ as the
    Lambda deployment root (/var/task/).  All modules live at the package root,
    so standard imports work without extra sys.path manipulation.
"""

import asyncio
import os
import sys

from mangum import Mangum
from mcp.server.streamable_http import StreamableHTTPServerTransport
from starlette.applications import Starlette
from starlette.routing import Route

# Ensure the Lambda task root is on sys.path
_task_root = os.path.dirname(os.path.abspath(__file__))
if _task_root not in sys.path:
    sys.path.insert(0, _task_root)

# Importing this module registers all MCP tools on the shared `server` instance
import mcp_server_with_runbooks  # noqa: F401

server = mcp_server_with_runbooks.server

# JSON response mode — SSE streaming is incompatible with Lambda.
# mcp_session_id=None disables session-ID validation, which is appropriate
# for stateless Lambda where each cold start creates a new instance and
# callers can't reliably maintain a session across invocations.
transport = StreamableHTTPServerTransport(
    mcp_session_id=None,
    is_json_response_enabled=True,
)

_server_task: asyncio.Task | None = None


async def _run_server():
    """Connect the transport to the MCP server (runs until shutdown)."""
    async with transport.connect() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


async def _ensure_server_running():
    """Start the MCP server as a background asyncio task if not already running.

    Must be called from within the event loop that Mangum uses so that the
    anyio memory streams are shared correctly with handle_request.
    """
    global _server_task
    if _server_task is None or _server_task.done():
        _server_task = asyncio.create_task(_run_server())
        # Yield control so connect() can set up _read_stream_writer
        await asyncio.sleep(0.05)


class McpAsgiApp:
    """Thin ASGI wrapper that ensures the server task is running, then
    delegates to transport.handle_request.

    Starlette's Route treats a class endpoint as a raw ASGI app, calling
    ``await endpoint(scope, receive, send)`` — exactly what we need.
    """

    async def __call__(self, scope, receive, send):
        await _ensure_server_running()
        await transport.handle_request(scope, receive, send)


_mcp_app = McpAsgiApp()

app = Starlette(
    routes=[
        Route("/mcp", endpoint=_mcp_app, methods=["GET", "POST", "DELETE"]),
        Route("/mcp/", endpoint=_mcp_app, methods=["GET", "POST", "DELETE"]),
    ],
)

# Lambda entry point
handler = Mangum(app, lifespan="off")
