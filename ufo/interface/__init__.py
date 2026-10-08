"""Interfaces: the CLI, the local HTTP API, and the dashboard."""
from __future__ import annotations

from .cli import build_parser, main
from .dashboard import DASHBOARD_HTML, render
from .server import AgentServer, serve

__all__ = [
    "AgentServer",
    "DASHBOARD_HTML",
    "build_parser",
    "main",
    "render",
    "serve",
]
