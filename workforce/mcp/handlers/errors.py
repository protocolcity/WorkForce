"""MCP tool errors."""
from __future__ import annotations


class ToolError(Exception):
    def __init__(self, message: str, code: int = -32000):
        super().__init__(message)
        self.code = code

