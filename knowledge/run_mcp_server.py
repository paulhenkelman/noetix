#!/usr/bin/env python3
"""
Noetix MCP Server Runner

Run this script to start the MCP server for integration with
AI agents and other MCP clients.

Usage:
    python run_mcp_server.py

Or as a module:
    python -m mcp_server.server
"""

import sys
import os

# Ensure project root is in path
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)


def main():
    """Main entry point."""
    from mcp_server import run_server
    run_server()


if __name__ == "__main__":
    main()
