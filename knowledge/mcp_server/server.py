"""
Noetix MCP Server

Model Context Protocol server exposing knowledge base functionality
to AI agents.
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def create_server(kb_dir=None):
    """
    Create and configure the MCP server.

    Args:
        kb_dir: Knowledge base directory (uses default if not provided)

    Returns:
        Configured MCP Server instance
    """
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent

    from .tools import create_kb_tools

    # Initialize tools
    tools = create_kb_tools(kb_dir)

    # Create MCP server
    server = Server("noetix-kb")

    @server.list_tools()
    async def list_tools():
        """Return list of available tools."""
        return [
            Tool(
                name="list_knowledge_bases",
                description="List all available knowledge bases with statistics including document count, chunk count, and last updated time.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            ),
            Tool(
                name="search",
                description="Search across knowledge bases using hybrid retrieval (vector + keyword search). Returns matching text chunks with document context and relevance scores.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query text"
                        },
                        "kb_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of knowledge base IDs to search (searches all if not specified)"
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Number of results to return (default: 10)",
                            "default": 10
                        },
                        "search_type": {
                            "type": "string",
                            "enum": ["hybrid", "semantic", "keyword"],
                            "description": "Type of search to perform (default: hybrid)",
                            "default": "hybrid"
                        }
                    },
                    "required": ["query"]
                }
            ),
            Tool(
                name="get_document",
                description="Get full document details including all chapters, metadata, and extracted entities.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "doc_id": {
                            "type": "string",
                            "description": "Document UUID"
                        },
                        "kb_id": {
                            "type": "string",
                            "description": "Knowledge base ID (optional, searches all if not provided)"
                        }
                    },
                    "required": ["doc_id"]
                }
            ),
            Tool(
                name="get_chunk",
                description="Get a specific text chunk with surrounding context chunks for better understanding.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "chunk_id": {
                            "type": "string",
                            "description": "Chunk UUID"
                        },
                        "context_chunks": {
                            "type": "integer",
                            "description": "Number of chunks before/after to include (default: 2)",
                            "default": 2
                        }
                    },
                    "required": ["chunk_id"]
                }
            ),
            Tool(
                name="find_related",
                description="Find documents related to a given document via shared entities and graph connections.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "doc_id": {
                            "type": "string",
                            "description": "Source document UUID"
                        },
                        "kb_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Knowledge base IDs to search (searches all if not specified)"
                        },
                        "relationship_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Filter by relationship type (e.g., shared_entity, cites)"
                        },
                        "max_hops": {
                            "type": "integer",
                            "description": "Maximum graph traversal depth (default: 2)",
                            "default": 2
                        }
                    },
                    "required": ["doc_id"]
                }
            ),
            Tool(
                name="get_entities",
                description="Get named entities (people, organizations, concepts, locations) from documents.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "doc_id": {
                            "type": "string",
                            "description": "Limit to specific document (optional)"
                        },
                        "kb_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Knowledge base IDs to search (searches all if not specified)"
                        },
                        "entity_type": {
                            "type": "string",
                            "enum": ["PERSON", "ORG", "CONCEPT", "LOCATION", "PRODUCT", "EVENT", "DATE", "WORK_OF_ART"],
                            "description": "Filter by entity type"
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Maximum number of entities to return (default: 100)",
                            "default": 100
                        }
                    },
                    "required": []
                }
            ),
            Tool(
                name="ask",
                description="Ask a question using RAG over the knowledge base. Returns retrieved context and source references that can be used to answer the question.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "Natural language question"
                        },
                        "kb_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Limit context to specific knowledge bases"
                        },
                        "doc_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Limit context to specific documents"
                        },
                        "include_sources": {
                            "type": "boolean",
                            "description": "Include source chunk references in response (default: true)",
                            "default": True
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Number of context chunks to retrieve (default: 5)",
                            "default": 5
                        }
                    },
                    "required": ["question"]
                }
            ),
            Tool(
                name="get_kb_stats",
                description="Get detailed statistics for a specific knowledge base including document count, chunk count, entity breakdown, and storage usage.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "kb_id": {
                            "type": "string",
                            "description": "Knowledge base identifier"
                        }
                    },
                    "required": ["kb_id"]
                }
            ),
            # Phase 5: Advanced Features
            Tool(
                name="find_similar_documents",
                description="Find documents similar to a given document using vector similarity. Can search within the same KB or across multiple KBs.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "document_id": {
                            "type": "string",
                            "description": "Source document UUID"
                        },
                        "kb_id": {
                            "type": "string",
                            "description": "Knowledge base ID of the source document"
                        },
                        "target_kb_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "KBs to search for similar documents (default: same KB)"
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Maximum number of similar documents to return (default: 10)",
                            "default": 10
                        },
                        "similarity_threshold": {
                            "type": "number",
                            "description": "Minimum similarity score 0-1 (default: 0.7)",
                            "default": 0.7
                        }
                    },
                    "required": ["document_id", "kb_id"]
                }
            ),
            Tool(
                name="detect_citations",
                description="Detect citations in a document and match them against other documents in the knowledge base. Returns potentially cited documents with confidence scores.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "document_id": {
                            "type": "string",
                            "description": "Document UUID to analyze for citations"
                        },
                        "kb_id": {
                            "type": "string",
                            "description": "Knowledge base ID"
                        }
                    },
                    "required": ["document_id", "kb_id"]
                }
            ),
            Tool(
                name="infer_relationships",
                description="Infer potential relationships for a document by combining signals from shared entities, content similarity, and citation patterns.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "document_id": {
                            "type": "string",
                            "description": "Document UUID to analyze"
                        },
                        "kb_id": {
                            "type": "string",
                            "description": "Knowledge base ID"
                        }
                    },
                    "required": ["document_id", "kb_id"]
                }
            ),
            Tool(
                name="export_kb",
                description="Export a knowledge base to a JSON file for backup or transfer. Includes documents, chunks, entities, and relationships.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "kb_id": {
                            "type": "string",
                            "description": "Knowledge base ID to export"
                        },
                        "output_path": {
                            "type": "string",
                            "description": "Output file path (optional, uses default exports directory)"
                        },
                        "include_embeddings": {
                            "type": "boolean",
                            "description": "Include vector embeddings in export (warning: large file size)",
                            "default": False
                        }
                    },
                    "required": ["kb_id"]
                }
            ),
            Tool(
                name="import_kb",
                description="Import a knowledge base from an exported JSON file.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "input_path": {
                            "type": "string",
                            "description": "Path to the exported JSON file"
                        },
                        "new_name": {
                            "type": "string",
                            "description": "Optional new name for the KB (creates a new KB instead of merging)"
                        }
                    },
                    "required": ["input_path"]
                }
            ),
            Tool(
                name="get_analytics",
                description="Get usage analytics for the knowledge base system including search patterns and document access.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "analytics_type": {
                            "type": "string",
                            "enum": ["search", "documents", "all"],
                            "description": "Type of analytics to retrieve (default: search)",
                            "default": "search"
                        },
                        "days": {
                            "type": "integer",
                            "description": "Number of days to analyze (default: 7)",
                            "default": 7
                        }
                    },
                    "required": []
                }
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        """Handle tool calls."""
        import json

        def validate_required(params: list[str]) -> Optional[str]:
            """Return error message if required params missing, None if valid."""
            for p in params:
                if p not in arguments or arguments[p] is None or arguments[p] == "":
                    return f"Missing required parameter: {p}"
            return None

        async def run_tool(func, *args, **kwargs):
            """Run synchronous tool in thread executor to avoid blocking."""
            return await asyncio.to_thread(func, *args, **kwargs)

        try:
            if name == "list_knowledge_bases":
                result = await run_tool(tools.list_knowledge_bases)

            elif name == "search":
                error = validate_required(["query"])
                if error:
                    result = {"error": error}
                else:
                    result = await run_tool(
                        tools.search,
                        query=arguments["query"],
                        kb_ids=arguments.get("kb_ids"),
                        top_k=arguments.get("top_k", 10),
                        search_type=arguments.get("search_type", "hybrid")
                    )

            elif name == "get_document":
                error = validate_required(["doc_id"])
                if error:
                    result = {"error": error}
                else:
                    result = await run_tool(
                        tools.get_document,
                        doc_id=arguments["doc_id"],
                        kb_id=arguments.get("kb_id")
                    )
                    if result is None:
                        result = {"error": "Document not found"}

            elif name == "get_chunk":
                error = validate_required(["chunk_id"])
                if error:
                    result = {"error": error}
                else:
                    result = await run_tool(
                        tools.get_chunk,
                        chunk_id=arguments["chunk_id"],
                        context_chunks=arguments.get("context_chunks", 2)
                    )
                    if result is None:
                        result = {"error": "Chunk not found"}

            elif name == "find_related":
                error = validate_required(["doc_id"])
                if error:
                    result = {"error": error}
                else:
                    result = await run_tool(
                        tools.find_related,
                        doc_id=arguments["doc_id"],
                        kb_ids=arguments.get("kb_ids"),
                        relationship_types=arguments.get("relationship_types"),
                        max_hops=arguments.get("max_hops", 2)
                    )

            elif name == "get_entities":
                result = await run_tool(
                    tools.get_entities,
                    doc_id=arguments.get("doc_id"),
                    kb_ids=arguments.get("kb_ids"),
                    entity_type=arguments.get("entity_type"),
                    top_k=arguments.get("top_k", 100)
                )

            elif name == "ask":
                error = validate_required(["question"])
                if error:
                    result = {"error": error}
                else:
                    result = await run_tool(
                        tools.ask,
                        question=arguments["question"],
                        kb_ids=arguments.get("kb_ids"),
                        doc_ids=arguments.get("doc_ids"),
                        include_sources=arguments.get("include_sources", True),
                        top_k=arguments.get("top_k", 5)
                    )

            elif name == "get_kb_stats":
                error = validate_required(["kb_id"])
                if error:
                    result = {"error": error}
                else:
                    result = await run_tool(
                        tools.get_kb_stats,
                        kb_id=arguments["kb_id"]
                    )
                    if result is None:
                        result = {"error": "Knowledge base not found"}

            # Phase 5: Advanced Features
            elif name == "find_similar_documents":
                error = validate_required(["document_id", "kb_id"])
                if error:
                    result = {"error": error}
                else:
                    result = await run_tool(
                        tools.find_similar_documents,
                        document_id=arguments["document_id"],
                        kb_id=arguments["kb_id"],
                        target_kb_ids=arguments.get("target_kb_ids"),
                        top_k=arguments.get("top_k", 10),
                        similarity_threshold=arguments.get("similarity_threshold", 0.7)
                    )

            elif name == "detect_citations":
                error = validate_required(["document_id", "kb_id"])
                if error:
                    result = {"error": error}
                else:
                    result = await run_tool(
                        tools.detect_citations,
                        document_id=arguments["document_id"],
                        kb_id=arguments["kb_id"]
                    )

            elif name == "infer_relationships":
                error = validate_required(["document_id", "kb_id"])
                if error:
                    result = {"error": error}
                else:
                    result = await run_tool(
                        tools.infer_relationships,
                        document_id=arguments["document_id"],
                        kb_id=arguments["kb_id"]
                    )

            elif name == "export_kb":
                error = validate_required(["kb_id"])
                if error:
                    result = {"error": error}
                else:
                    result = await run_tool(
                        tools.export_kb,
                        kb_id=arguments["kb_id"],
                        output_path=arguments.get("output_path"),
                        include_embeddings=arguments.get("include_embeddings", False)
                    )

            elif name == "import_kb":
                error = validate_required(["input_path"])
                if error:
                    result = {"error": error}
                else:
                    result = await run_tool(
                        tools.import_kb,
                        input_path=arguments["input_path"],
                        new_name=arguments.get("new_name")
                    )

            elif name == "get_analytics":
                result = await run_tool(
                    tools.get_analytics,
                    analytics_type=arguments.get("analytics_type", "search"),
                    days=arguments.get("days", 7)
                )

            else:
                result = {"error": f"Unknown tool: {name}"}

            # Format result as JSON text
            return [TextContent(
                type="text",
                text=json.dumps(result, indent=2, default=str)
            )]

        except Exception as e:
            logger.error(f"Tool {name} failed: {e}")
            return [TextContent(
                type="text",
                text=json.dumps({"error": str(e)})
            )]

    return server


async def run_server_async(kb_dir=None):
    """
    Run the MCP server asynchronously via stdio.

    Args:
        kb_dir: Knowledge base directory
    """
    from mcp.server.stdio import stdio_server

    server = create_server(kb_dir)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


def run_server(kb_dir=None):
    """
    Run the MCP server (blocking).

    This is the main entry point for running the server.

    Args:
        kb_dir: Knowledge base directory
    """
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    logger.info("Starting Noetix MCP Server")
    asyncio.run(run_server_async(kb_dir))


if __name__ == "__main__":
    run_server()
