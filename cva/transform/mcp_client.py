"""Thin client for the AWS Transform MCP server (stdio).  Requires ``pip install "mcp>=1.23,<2"`` (the 2.x SDK changed
its API and the AWS server itself needs 1.x).

Only an ALLOW-LIST of tools can be called.  ``complete_task`` (default action APPROVE), ``configure``, deletes,
collaborator and connector tools, and ``control_job`` are blocked here, in code, whatever the caller asks."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from contextlib import AsyncExitStack
from typing import Any

from .shape import unwrap

ALLOWED_TOOLS = {"get_status", "create_workspace", "create_job", "load_instructions", "get_job_status", "send_message",
                 "list_resources", "get_resource", "upload_artifact", "complete_task"}      # complete_task: only through the driver's guarded answer_task
BLOCKED_TOOLS = {"configure", "switch_profile", "delete_workspace", "delete_job", "manage_collaborator", "create_connector",
                 "accept_connector", "control_job", "adaptive_poll"}


class ToolBlocked(Exception):
    pass


class ToolError(Exception):
    pass


def _parse(result: Any) -> dict[str, Any]:
    text = "".join(getattr(c, "text", "") for c in (result.content or []))
    sc = getattr(result, "structuredContent", None)
    payload: Any = sc if sc else None
    if payload is None:
        try:
            payload = json.loads(text)
        except ValueError:
            payload = {"text": text}
    if getattr(result, "isError", False):
        raise ToolError(text[:600] or "tool reported an error")
    payload = unwrap(payload)
    if isinstance(payload, dict) and set(payload) == {"result"} and isinstance(payload["result"], dict):
        payload = payload["result"]                       # {"result": "<json>"} -> the decoded object itself
    return payload if isinstance(payload, dict) else {"result": payload}


class McpToolClient:
    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None, timeout: float = 240.0,
                 errlog_path: str | None = None) -> None:
        self.command, self.args, self.env, self.timeout = command, args, env or {}, timeout
        self.errlog_path = errlog_path                 # the server's own log (the AWS library logs every call): a file, not your terminal
        self._errlog: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stack: AsyncExitStack | None = None
        self._session: Any = None

    # -------------------------------------------------------------- lifecycle
    def start(self) -> "McpToolClient":
        try:
            import mcp  # noqa: F401
        except ImportError as e:
            raise RuntimeError('the "mcp" package is not installed: pip install "mcp>=1.23,<2"') from e
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._connect(), self._loop).result(self.timeout)
        return self

    async def _connect(self) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        self._stack = AsyncExitStack()
        params = StdioServerParameters(command=self.command, args=self.args, env=self.env or None)
        if self.errlog_path:
            self._errlog = open(self.errlog_path, "a", buffering=1, encoding="utf-8")   # noqa: SIM115 (closed in close())
        read, write = await self._stack.enter_async_context(
            stdio_client(params, errlog=self._errlog) if self._errlog else stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

    def close(self) -> None:
        if self._loop is None:
            return
        try:
            if self._stack is not None:
                asyncio.run_coroutine_threadsafe(self._stack.aclose(), self._loop).result(15)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop = None
        if self._errlog:
            self._errlog.close()
            self._errlog = None

    def __enter__(self) -> "McpToolClient":
        return self.start()

    def __exit__(self, *a: Any) -> None:
        self.close()

    # -------------------------------------------------------------- calls
    def tool_names(self) -> set[str]:
        assert self._loop and self._session
        res = asyncio.run_coroutine_threadsafe(self._session.list_tools(), self._loop).result(self.timeout)
        return {t.name for t in res.tools}

    accepts_timeout = True          # call() takes a per-call timeout: a listing that hangs must not freeze a check for minutes

    def call(self, name: str, args: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        if name in BLOCKED_TOOLS or name not in ALLOWED_TOOLS:
            raise ToolBlocked(f"tool '{name}' is not permitted for A01 (allowed: {sorted(ALLOWED_TOOLS)})")
        assert self._loop and self._session, "client not started"
        limit = timeout or self.timeout
        fut = asyncio.run_coroutine_threadsafe(self._session.call_tool(name, args), self._loop)
        try:
            res = fut.result(limit)
        except (concurrent.futures.TimeoutError, TimeoutError) as e:
            fut.cancel()
            raise TimeoutError(f"'{name}' did not answer within {limit:.0f} s") from e
        return _parse(res)
