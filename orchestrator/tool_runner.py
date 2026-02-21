from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional, Set


@dataclass(frozen=True)
class ToolRequest:
    tool: str
    params: Dict[str, str]
    trusted: bool
    human_approved: bool = False


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    detail: str
    output: Optional[str] = None


class ToolRunner:
    def __init__(
        self,
        handlers: Optional[Dict[str, Callable[[ToolRequest], ToolResult]]] = None,
        safe_tools: Optional[Set[str]] = None,
        risky_tools: Optional[Set[str]] = None,
    ) -> None:
        self.handlers = handlers or {}
        self.safe_tools = safe_tools or {
            "repo.search",
            "repo.read",
            "test.run",
            "lint.run",
        }
        self.risky_tools = risky_tools or {
            "git.push",
            "deploy.run",
            "db.migrate",
            "filesystem.delete",
        }

    def run(self, request: ToolRequest) -> ToolResult:
        boundary = self._enforce_policy(request)
        if not boundary.ok:
            return boundary

        handler = self.handlers.get(request.tool)
        if handler is None:
            return ToolResult(
                ok=False,
                detail=f"tool has no registered handler: {request.tool}",
            )
        return handler(request)

    def _enforce_policy(self, request: ToolRequest) -> ToolResult:
        if request.tool not in self.safe_tools and request.tool not in self.risky_tools:
            return ToolResult(ok=False, detail=f"tool is not allow-listed: {request.tool}")

        if request.tool in self.risky_tools and not request.human_approved:
            return ToolResult(
                ok=False,
                detail=f"risky tool requires explicit human approval: {request.tool}",
            )

        if not request.trusted and request.tool not in {"repo.search", "repo.read"}:
            return ToolResult(
                ok=False,
                detail=f"untrusted context cannot run tool: {request.tool}",
            )

        return ToolResult(ok=True, detail=f"tool allowed: {request.tool}")
