# Noetix

Knowledge platform with MCP tool services, content processing pipeline, and repository management dashboard.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│ Claude Code (AI layer)                                  │
│  ├── noetix-kb MCP server (stdio) ─────┐               │
│  ├── noetix-content MCP server (stdio) ─┤               │
│  ├── Playwright MCP (browser control)   │               │
│  └── computer-use (GUI control)         │               │
└─────────────────────────────────────────│───────────────┘
                                          │
┌─────────────────────────────────────────│───────────────┐
│ Knowledge Backend (pacgpu1:8001)        │               │
│  ├── Knowledge base CRUD & search ◄─────┘               │
│  ├── Content pipeline (PDF/audio/video conversion)      │
│  ├── Library management                                 │
│  └── Ingest jobs                                        │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ Repository Dashboard (Express :8788)                    │
│  ├── Web UI: Library, KB, Jobs, Input tabs              │
│  └── API proxy to backend                               │
└─────────────────────────────────────────────────────────┘
```

**Claude Code IS the AI.** The MCP servers extend Claude Code with knowledge base access, content processing, and browser automation. There is no built-in LLM or chat — all AI interaction happens through Claude Code.

## Project Structure

```
noetix/
├── ui/
│   ├── src/
│   │   ├── server/
│   │   │   ├── server.js              # Express gateway (~950 lines)
│   │   │   ├── config.js              # TOML config loader (ui.config)
│   │   │   ├── kb-mcp-server.js       # KB MCP server (3 tools, stdio)
│   │   │   ├── content-mcp-server.js  # Content MCP server (14 tools, stdio)
│   │   │   ├── playwright-mcp.js      # Playwright status/diagnostics
│   │   │   └── mcp-manager.js         # MCP lifecycle management
│   │   ├── frontend/
│   │   │   ├── main.js                # SPA (~930 lines, vanilla JS)
│   │   │   └── index.html
│   │   └── __tests__/                 # 5 test files, 71 tests
│   ├── package.json
│   └── vite.config.js
├── cli/                               # CLI: init, start, stop, status
│   ├── index.js
│   ├── commands/
│   └── templates/                     # Config file templates
├── knowledge/                         # Python backend (runs on pacgpu1)
├── .claude/
│   └── settings.json                  # MCP server configuration
├── noetix.config                      # MCP server settings (gitignored)
├── ui.config                          # Server/frontend settings (gitignored)
└── CLAUDE.md                          # This file
```

## MCP Tools Available

### noetix-kb (3 tools) — Memory System
| Tool | Description |
|------|-------------|
| `kb_list` | List all knowledge bases with name, description, document count |
| `kb_search` | Query a KB with natural language; returns matched chunks with sources |
| `kb_documents` | List all documents in a specific KB |

### noetix-content (14 tools) — Content Pipeline
| Tool | Description |
|------|-------------|
| `content_list_downloads` | List files in the Playwright download directory |
| `content_upload` | Upload a file for conversion (PDF→audiobook, video→transcript, etc.) |
| `content_status` | Check conversion job progress |
| `content_voices` | List available TTS voices for audiobook generation |
| `content_kb_create` | Create a new knowledge base |
| `content_library_list` | Browse processed content in the library |
| `content_library_tag` | Update tags on a library item |
| `content_library_metadata` | Update title/author metadata |
| `content_library_ingest` | Re-ingest a library item into a different KB |
| `content_library_delete` | Delete a library item |
| `content_stage` | Stage a file for batch processing |
| `content_staged_list` | List staged files |
| `content_staged_clear` | Clear the staging area |
| `content_batch_convert` | Batch convert staged files into a combined document |

### Playwright MCP — Browser Automation
Configured separately (not in this project). Provides browser_navigate, browser_snapshot, browser_click, browser_type, browser_tab_list, etc.

### computer-use — GUI Control
Claude Code built-in. Screenshot-based desktop/VM control via coordinate clicks.

## Backend (pacgpu1)

The knowledge backend runs on `10.0.0.50:8001` (ssh alias: `paccpu1`). It provides:
- Knowledge base CRUD (`/api/knowledge-bases`)
- Semantic search (`/api/chat`)
- Content conversion pipeline (PDF, audio, video → various output formats)
- Library management (`/api/library`)
- Ingest job tracking (`/api/jobs`, `/api/status/:id`)

The backend is a Python FastAPI app (uvicorn) with embedding models, chunking, and graph store support.

## Express Gateway (server.js)

Proxies all API requests to the backend. Serves the built frontend SPA. Key endpoint groups:

- `/health` — Server health check
- `/v1/knowledge-bases/*` — KB CRUD, documents, dependencies
- `/v1/library/*` — Library items, tags, metadata, bulk ops, orphan detection
- `/v1/ingest/jobs` — File upload, job status, output download
- `/v1/actions/*` — Action request workflow (propose, approve, reject, execute)
- `/v1/audit/*` — Audit export
- `/v1/playwright-mcp/status` — Playwright diagnostics

## Configuration

### Instance configs (gitignored, generated by `noetix init`)
- `noetix.config` — Project name, MCP server settings
- `ui.config` — Server host/port, backend URL, SOCKS proxy, Playwright, frontend

### MCP server config (`.claude/settings.json`)
Configures noetix-kb and noetix-content as stdio MCP servers for Claude Code with `REMOTE_BASE` pointing to pacgpu1.

## Content Processing Workflows

### Single file conversion
1. Download file via browser tools → saved to `~/.cache/noetix-playwright`
2. `content_list_downloads` → find file path
3. `content_upload` with file path, desired outputs (e.g. `"m4b,knowledge_base"`), and target KB
4. `content_status` → poll until completed

### Batch processing (multiple files → combined document)
1. Download all files
2. `content_stage` for each file in order
3. `content_staged_list` → verify order
4. `content_batch_convert` with title, outputs, target KB
5. `content_status` → poll until completed

### Supported conversions
- PDF → audiobook (M4B), searchable PDF, knowledge base
- Audio (MP3/M4B/M4A) → transcription → PDF, knowledge base
- Video (MP4/MKV/WebM) → transcription+visual analysis → audiobook, PDF, knowledge base
- ZIP of videos → batch process all videos
- Multiple PDFs → combined PDF

## Development

### Run tests
```bash
cd ui && npm test
```

### Build frontend
```bash
cd ui && npm run build
```

### Start dev server
```bash
cd ui && node src/server/server.js
```

### Managed by systemd
```bash
systemctl --user restart noetix-ui
systemctl --user status noetix-ui
journalctl --user -u noetix-ui -f
```

The systemd unit needs `PATH` to include `~/.npm-global/bin` for Playwright MCP.

## Deployment Target

Production deployment is on **pacgpu1** (`10.0.0.50`, ssh alias `paccpu1`):
- Ubuntu 24.04, Node 22, Python 3.12
- 250GB RAM, 915GB NVMe
- Knowledge backend already running on :8001
- No noetix UI deployment yet (clean slate)
- Existing projects: `cs7637`, `StreamingConcierge`

## Terminology

- **Memory system** / **memory construct** — the knowledge base retrieval system. Never use "RAG".
- **Repository dashboard** — the web UI for browsing KBs, library, jobs.
- **MCP tools** — the stdio servers that extend Claude Code with Noetix capabilities.

## Git

- Branch `revamp` — current active development (stripped AI layer)
- Branch `dev-models` — previous version with built-in LLM chat (archived)
- Branch `master` / `main` — stable releases
- Remote: `github.com/paulhenkelman/noetix`
- Instance configs (noetix.config, ui.config) are gitignored; templates in `cli/templates/`
