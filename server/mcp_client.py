"""Async client to the Garmin MCP server (Taxuspt/garmin_mcp).

The dashboard talks to Garmin *exclusively through the MCP server* — this is the
"via garmin mcp" data path. We launch the server as a stdio subprocess and keep a
single long-lived MCP session.

To avoid anyio cross-task cancel-scope problems (the stdio session must be entered
and exited by the *same* task), the whole session lives inside one dedicated worker
task. Callers submit requests through an asyncio.Queue and await a future.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger("garmin_coach.mcp")

# Plain-text sentinels the server returns instead of JSON when there is no data
# or an error occurred (see garmin_mcp source: `return f"No ..."` / `f"Error ..."`).
_NON_JSON_PREFIXES = ("No ", "Error ", "Unknown ", "Unexpected ", "Failed ")


def _server_params() -> StdioServerParameters:
    """How to launch the Garmin MCP server.

    Override the command via GARMIN_MCP_CMD (space-separated) if you have it
    installed differently (e.g. a local checkout). Default pulls it with uvx.
    """
    override = os.environ.get("GARMIN_MCP_CMD")
    if override:
        parts = override.split()
        command, args = parts[0], parts[1:]
    else:
        command = "uvx"
        args = [
            "--python", "3.12",
            # garmin_mcp imports mcp.server.fastmcp, which the 2.x SDK dropped;
            # without this uvx resolves the latest and the server dies on import.
            "--with", "mcp<2",
            "--from", "git+https://github.com/Taxuspt/garmin_mcp",
            "garmin-mcp",
        ]
    # Pass through Garmin-related env (token dir, credentials files, region).
    env = {k: v for k, v in os.environ.items()}
    return StdioServerParameters(command=command, args=args, env=env)


@dataclass
class _Request:
    tool: str
    args: dict[str, Any]
    future: asyncio.Future


@dataclass
class GarminMCP:
    """Manages the lifecycle of the Garmin MCP subprocess + session."""

    _queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    _task: asyncio.Task | None = None
    _ready: asyncio.Event = field(default_factory=asyncio.Event)
    connected: bool = False
    error: str | None = None
    tool_names: list[str] = field(default_factory=list)

    async def start(self) -> None:
        """Launch the worker and wait until the session is ready (or failed)."""
        self._task = asyncio.create_task(self._run(), name="garmin-mcp-worker")
        await self._ready.wait()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        """Worker task: owns the stdio session for its whole lifetime."""
        try:
            async with stdio_client(_server_params()) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.tool_names = [t.name for t in tools.tools]
                    self.connected = True
                    self.error = None
                    log.info("Garmin MCP connected: %d tools", len(self.tool_names))
                    self._ready.set()
                    await self._serve(session)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # Most common cause: no saved token yet -> server exits at startup.
            self.connected = False
            self.error = _friendly_error(e)
            log.warning("Garmin MCP unavailable: %s", self.error)
            self._ready.set()

    async def _serve(self, session: ClientSession) -> None:
        while True:
            req: _Request = await self._queue.get()
            try:
                result = await session.call_tool(req.tool, req.args)
                if not req.future.done():
                    req.future.set_result(_parse_result(result))
            except Exception as e:  # noqa: BLE001
                if not req.future.done():
                    req.future.set_exception(e)

    async def call(self, tool: str, /, **args: Any) -> Any:
        """Call an MCP tool. Returns parsed JSON (dict/list) or None if no data."""
        if not self.connected:
            raise GarminMCPUnavailable(self.error or "Garmin MCP not connected")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        await self._queue.put(_Request(tool=tool, args=args, future=fut))
        return await fut


class GarminMCPUnavailable(RuntimeError):
    """Raised when the Garmin MCP session is not available (e.g. not authenticated)."""


def _parse_result(result: Any) -> Any:
    """Extract text content from a CallToolResult and parse JSON if present."""
    text = _result_text(result)
    if text is None:
        return None
    stripped = text.lstrip()
    if not stripped:
        return None
    if stripped.startswith(_NON_JSON_PREFIXES) and not stripped.startswith("{") and not stripped.startswith("["):
        # Human-readable "no data" / error message -> treat as empty.
        log.debug("tool returned message: %s", stripped[:120])
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text  # unstructured but not an obvious error; hand back raw


def _result_text(result: Any) -> str | None:
    content = getattr(result, "content", None)
    if not content:
        return None
    parts: list[str] = []
    for item in content:
        t = getattr(item, "text", None)
        if t is not None:
            parts.append(t)
    return "\n".join(parts) if parts else None


def _flatten(e: BaseException) -> list[str]:
    """Collect messages from an exception, unwrapping ExceptionGroups."""
    msgs = [str(e) or e.__class__.__name__]
    for sub in getattr(e, "exceptions", []) or []:
        msgs.extend(_flatten(sub))
    return msgs


def _friendly_error(e: Exception) -> str:
    joined = " | ".join(_flatten(e)).lower()
    if "connection closed" in joined or "closed" in joined or "exiting" in joined:
        return (
            "Garmin MCP server exited on startup. Either you are not authenticated "
            "yet — run once:  uvx --python 3.12 --with \"mcp<2\" --from "
            "git+https://github.com/Taxuspt/garmin_mcp garmin-mcp-auth   "
            "(enter your Garmin email/password + MFA code) — or it failed to import. "
            "Check the traceback above the warning: garmin_mcp needs the 1.x MCP SDK, "
            "hence the mcp<2 pin. Then restart the backend."
        )
    return str(e) or e.__class__.__name__
