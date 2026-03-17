# Noetix

Noetix is a self-hosted AI platform that learns from your documents and acts on
your behalf. Feed it textbooks, papers, manuals, or any content you want it to
absorb. It builds a searchable knowledge base, then uses that knowledge to
answer questions, navigate websites, run commands, and carry out multi-step
tasks through browser control and a CLI agent.

The core idea: teach it what you know, then let it work for you.

## Quick Start

Install the CLI globally:

```bash
npm install -g noetix
```

Run the setup wizard:

```bash
noetix init
```

The wizard offers three installation modes:

- **Full** - UI and knowledge backend on a single machine
- **Frontend only** - UI that connects to a remote backend
- **Backend only** - Knowledge backend and MCP server (no UI)

After setup, start all services:

```bash
noetix start
```

Open the UI at `http://localhost:8788`.

### Prerequisites

- Node.js 18+
- Python 3.11+ (for the knowledge backend)
- FFmpeg (for audio and video processing)
- Codex CLI (`npm install -g @openai/codex`)
- Docker (optional, for Neo4j graph database)

## How It Works

Noetix has three layers: a browser-based UI, an Express gateway that manages
the AI agent, and a Python knowledge backend that handles document processing
and retrieval.

```
Browser UI  -->  Express Gateway (:8788)  -->  Knowledge Backend (:8001)
                      |
                 Codex Agent
                   |  |  |
                  MCP Servers
            (KB, Content, Browser)
```

The gateway sits between you and the Codex agent. When you send a message, the
gateway creates a conversation, injects relevant knowledge from your documents,
and streams the response back with real-time thinking indicators. A post-turn
verification system detects when the agent fabricates answers or fails to use
its tools, automatically retrying with a fresh conversation.

## Knowledge System

The knowledge backend converts your documents into searchable, structured
memory. Upload a PDF and the system will:

1. Extract text (with OCR for scanned pages)
2. Detect the document's hierarchical structure (parts, chapters, sections)
3. Split content into chunks and generate embeddings
4. Store vectors in LanceDB for semantic search
5. Optionally build a Neo4j graph of entities, concepts, and relationships

Retrieval combines three signals: semantic similarity (vector search), keyword
matching (full-text index), and graph traversal (entity relationships). Results
include breadcrumb paths showing exactly where in a document each answer comes
from.

### Supported Formats

- **PDF** - text extraction, OCR, structure detection
- **Audio** (MP3, M4A, M4B) - transcription via faster-whisper
- **Video** (MP4, MKV, WebM) - transcription and conversion
- **Archives** (ZIP) - batch processing of bundled files

### Document Conversion

Beyond building knowledge bases, Noetix converts content between formats. The
flagship pipeline turns PDFs into M4B audiobooks with chapter markers, using
Kokoro TTS for natural-sounding speech with GPU acceleration.

## Browser Integration

The agent controls a real browser through Playwright MCP. It can navigate
pages, fill forms, click buttons, read content, take screenshots, and handle
multi-step workflows across tabs. This is how Noetix acts on your behalf: it
reads your course materials from the knowledge base, then applies that
knowledge by interacting with websites, LMS platforms, or any web application
you point it at.

## MCP Extensibility

Noetix uses the Model Context Protocol for all tool access. Three MCP servers
ship by default:

- **noetix-kb** - search knowledge bases, list documents, browse structure,
  explore concepts, find cross-references
- **noetix-content** - upload files, track conversions, manage the content
  library, tag and organize materials
- **noetix-playwright** - browser automation through a CDP endpoint

Additional MCP servers can be added through the Codex configuration file. Any
tool that speaks MCP becomes available to the agent.

## Configuration

Three TOML files control the system:

| File | Controls |
|------|----------|
| `noetix.config` | AI model, reasoning effort, MCP server definitions |
| `ui.config` | Gateway port, backend URL, CORS, frontend settings |
| `knowledge.config` | Backend port, OpenAI keys, storage paths, Neo4j |

The `noetix init` wizard generates these from templates. Edit them directly to
adjust ports, swap models, or add MCP servers.

## Service Management

```bash
noetix start              # Start all services
noetix start frontend     # Start UI only
noetix start backend      # Start backend only
noetix stop               # Stop all services
noetix status             # Check service health
```

Services run as systemd user units and start automatically on boot after the
initial setup.

## Deployment

Noetix supports split deployment across machines. A common setup runs the
knowledge backend on a GPU server (for embeddings, TTS, and transcription) and
the UI on a lightweight machine closer to the user. The frontend-only install
mode connects to a remote backend URL, with optional SOCKS proxy support for
tunneled connections.

## Project Structure

```
noetix/
  cli/                  CLI package (noetix init/start/stop/status)
  ui/
    src/server/         Express gateway, Codex client, MCP servers
    src/frontend/       Browser UI (Vite)
  knowledge/
    pipeline/           PDF extraction, OCR, TTS, STT, video processing
    knowledge_base/     Ingestion, retrieval, vector store, graph store
    mcp_server/         MCP tool definitions and server
    server.py           FastAPI backend
  scripts/              Setup and build utilities
```

## License

All rights reserved.
