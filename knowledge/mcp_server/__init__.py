"""
Noetix MCP Server

Exposes knowledge base functionality to AI agents via the
Model Context Protocol (MCP).
"""

from .server import create_server, run_server

__all__ = ['create_server', 'run_server']
