# Noetix

Noetix is a self-hosted knowledge platform that turns your documents into a memory system any MCP-aware AI client (currently Claude Code) can search, retrieve from, and operate on. Feed it textbooks, papers, lecture videos, manuals, or any content you want available to your agent. It builds searchable knowledge bases, converts content between formats (PDF↔audiobook, video→transcript, …), and exposes everything through MCP tools.

There is no built-in chat layer or LLM. **Claude Code is the agent**, talking to Noetix through stdio MCP servers that bridge into the backend. The backend stays content-focused; the agent stays conversational.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│ Claude Code  (the agent)                                 │
│  ├── noetix-kb MCP (stdio)         ─┐                    │
│  ├── noetix-content MCP (stdio)    ─┤                    │
│  └── (optional) playwright, etc.    │                    │
└─────────────────────────────────────┼────────────────────┘
                                      │
┌─────────────────────────────────────▼────────────────────┐
│ Knowledge Backend  (FastAPI :8001)                       │
│  ├── KB CRUD & retrieval (vector + keyword + graph)      │
│  ├── Content pipeline (PDF/audio/video conversion)       │
│  └── Library management                                  │
└────────────────────┬─────────────────────────────────────┘
                     │
┌────────────────────▼─────────────────────────────────────┐
│ Repository Dashboard  (Express :8788, optional)          │
│  ├── Web UI for browsing KBs, library, jobs              │
│  └── /v1/* proxy → backend /api/*                        │
└──────────────────────────────────────────────────────────┘
```

The MCP servers run as Claude Code stdio children — they're not network services. The backend is a Python FastAPI app. The web UI is a separate static SPA plus a thin Express proxy that gives you a browser view of the same data. Useful for management work; not required for agent operation.

## Install

```bash
git clone https://github.com/paulhenkelman/noetix.git
cd noetix
./install.sh
```

The installer detects an existing deployment in the current directory and offers **update**, **alternate location**, or **cancel**. On a fresh install it walks you through three modes:

| Mode | Installs | Use when |
|------|----------|----------|
| **Full** | Backend + UI + agent MCPs | Single-machine deployment — this host serves data and runs Claude Code. |
| **Back-end** | Backend + UI only | Data-side server. Agents connect to it from elsewhere. |
| **Agent** | MCP servers + Claude Code config | Agent-side client. Stores no data locally; talks to a remote backend. |

Non-interactive (CI / re-deploy):

```bash
./install.sh -m full -y                                   # accept all defaults
./install.sh -m backend -y --backend-port 8001            # data-side, fixed port
./install.sh -m agent -y --backend-url http://server:8001 # agent pointed at remote
```

The same script handles updates: pull the repo on a deployed host, run `./install.sh -y`, and the installer preserves existing ports, refreshes deps, rebuilds the frontend, and re-registers MCPs idempotently.

### Prerequisites

- **Node.js 18+** (all modes)
- **Python 3.11+, FFmpeg** (back-end / full)
- **NVIDIA GPU** (recommended for back-end — TTS, STT, OCR are CPU-fallback otherwise)
- **Claude Code** (agent / full) — install from https://claude.com/claude-code
- **Docker** (optional — containerised backend, Neo4j graph store)

## Knowledge System

The backend converts documents into structured, searchable memory. Upload a PDF and the pipeline:

1. Extracts text (OCR for scanned pages)
2. Detects hierarchical structure (parts, chapters, sections)
3. Chunks and embeds for vector search (LanceDB)
4. Optionally builds a Neo4j graph of entities and relationships

Retrieval combines three signals — vector similarity, keyword full-text, and graph traversal — and answers cite their source with a breadcrumb path back to the originating document section.

### Supported Formats

- **PDF** — extraction, OCR, structure detection
- **Audio** (MP3, M4A, M4B) — transcription via faster-whisper
- **Video** (MP4, MKV, WebM) — transcription plus visual analysis
- **Archives** (ZIP) — batch process bundled files

### Format Conversion

Beyond building memory, Noetix transforms content between formats. The flagship pipeline turns PDFs into M4B audiobooks with chapter markers, using Kokoro TTS for natural-sounding speech with GPU acceleration. Inverse paths exist for video and audio.

## MCP Tools

`agent` and `full` installs register two MCP servers with Claude Code (project scope, written into `.mcp.json` at the install root).

### noetix-kb — 3 tools

| Tool | Purpose |
|------|---------|
| `kb_list` | List all KBs (name, description, doc count, chunk count) |
| `kb_search` | Natural-language query against a KB; returns matched chunks with sources |
| `kb_documents` | Enumerate documents in a specific KB |

### noetix-content — 14 tools

| Tool | Purpose |
|------|---------|
| `content_list_downloads` | List downloaded files awaiting processing |
| `content_upload` | Upload a file for conversion or KB ingest |
| `content_status` | Poll conversion job |
| `content_voices` | List available TTS voices |
| `content_kb_create` | Create a new knowledge base |
| `content_library_list` | Browse processed content |
| `content_library_tag` | Update tags on a library item |
| `content_library_metadata` | Update title / author |
| `content_library_ingest` | Re-ingest into a different KB |
| `content_library_delete` | Delete a library item |
| `content_stage` / `content_staged_list` / `content_staged_clear` | Staging workflow for batch jobs |
| `content_batch_convert` | Batch-convert staged files into a combined document |

After install, run `claude mcp list` from the install directory to confirm both servers show `✓ Connected`.

## Repository Dashboard

The Express UI at `:8788` is for non-agent workflows: browsing the library, retagging items, watching ingest job progress. It's a vanilla-JS SPA fetching from its own origin (`location.origin`), so the same bundle works whether you load it locally or over the LAN at the deployment host.

The dashboard is optional. If your only use case is "Claude Code → MCP → backend", you can skip the UI install and just run `agent` mode.

## Configuration

Generated by `noetix init` from templates and gitignored (per-host, may contain secrets):

| File | Controls |
|------|----------|
| `noetix.config` | Project metadata, MCP server settings |
| `ui.config` | UI port, backend URL, CORS, downloads dir, frontend |
| `knowledge.config` | Backend port, OpenAI keys, storage paths, Neo4j |

Also per-host: `.mcp.json` (Claude Code MCP registrations) and `.claude/settings.local.json` (per-machine MCP overrides like `host-control` or `playwright`).

## Service Management

```bash
noetix start              # Start all services for this install
noetix start frontend     # UI only
noetix start backend      # Backend only
noetix stop
noetix status             # Health check
```

On Linux the installer creates systemd user units and runs `loginctl enable-linger` so services persist across logout (without linger, user services stop the moment your last session closes — a subtle source of "service flap" in fresh deployments).

## Deployment Patterns

| Pattern | Mode on each machine |
|---------|---------------------|
| Single host | `full` on one machine |
| Split server / client | `backend` on a GPU/data server, `agent` on each agent-side client (laptop, workstation) |
| Multi-tenant agents | `backend` on the server, `agent` on each user's machine, all pointing at the same backend URL |

`agent` mode is intentionally lightweight: it copies only the MCP server source files needed by Claude Code, doesn't run Python or build the SPA, and stores no data locally. Multiple agent clients share one back-end without contention.

## Project Structure

```
noetix/
├── cli/                       Installer + service management
│   ├── commands/init.js       Three-mode installer
│   ├── lib/checks.js          Prereq detection (node, python, docker, GPU, ffmpeg)
│   └── templates/             Config templates
├── ui/
│   ├── src/server/            Express gateway, kb-mcp-server, content-mcp-server
│   ├── src/frontend/          Vanilla-JS SPA (Vite-built)
│   └── __tests__/             Vitest suite
├── knowledge/                 Python FastAPI backend
│   ├── pipeline/              PDF/OCR/TTS/STT/video processing
│   ├── knowledge_base/        Ingestion, vector store (LanceDB), graph (Neo4j)
│   └── server.py              FastAPI entry point
├── install.sh                 Bootstrap → noetix init
└── CLAUDE.md                  Operational notes for AI sessions
```

## License

MIT. See [LICENSE](LICENSE).
