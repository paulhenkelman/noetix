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
Configured separately (not in this project). Provides browser_navigate, browser_snapshot, browser_click, browser_type, browser_tabs, browser_evaluate, etc. Configured against an existing Chrome instance via `--cdp-endpoint http://localhost:9222`.

### host-control (1 tool) — Silent Screenshots
Custom MCP at `ui/src/server/computer-use-mcp-server.js`. Exposes a single `screenshot` tool — silent (no shutter sound) and flashless via `silent-screenshot.py`, which claims the `org.gnome.Screenshot` D-Bus name and calls GNOME Shell's privileged Screenshot method with `flash=false`.

Synthetic mouse/keyboard input was attempted but does not work on GNOME Wayland: Mutter silently drops uinput events from virtual devices (verified with evtest). All input tools were stripped to avoid misleading the model. For browser interaction use playwright; for native-app input on Wayland the only realistic path is libei + xdg-desktop-portal RemoteDesktop. See "Operational caveats" below for the full diagnostic. **Only useful on machines with a display and GNOME** — don't enable on pacgpu1.

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

### MCP server config (`.mcp.json`, gitignored)
The installer (`noetix init` in `agent` or `full` mode) registers `noetix-kb` and `noetix-content` with Claude Code via `claude mcp add -s project`, which writes absolute paths into `.mcp.json` at the install root. That file is **per-machine** and gitignored — never commit it. To re-register from scratch: re-run `./install.sh -y` and the installer removes any prior registration before adding a fresh one.

`.claude/settings.local.json` is also gitignored and may carry per-machine MCP overrides (e.g., `host-control`, `playwright`) that don't make sense to commit.

### Runtime data directories
`knowledge/{data, knowledge_bases, library, uploads}` are gitignored and populated by the backend at runtime. **They may also be symlinks** to a shared store outside the repo — pacgpu1 has them pointing at `~/src/projects/tts/{...}` so multiple projects share one corpus.

Caveat: `git stash --include-untracked` (without further filters) **captures symlinks** at those paths and on `apply` may fail to restore them cleanly, leaving the install with empty real directories where the data used to be visible. If you must stash before pulling, target only the files you intend to: e.g., `git stash push .claude/settings.json ui/whatever.js`. The installer's `mkdirSync(recursive:true)` is symlink-aware and will not overwrite an existing symlink, but it can't recover one that was already moved into a stash.

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

## Operational caveats

### host-control: why input doesn't work on GNOME Wayland

The host-control MCP only exposes `screenshot` because synthetic input is blocked at the compositor level on this host (`XDG_SESSION_TYPE=wayland`, compositor: gnome-shell/Mutter). Every available path was tried and verified failing:

- **xdotool**: emits X11 events through XWayland. Cursor *positioning* works, but synthetic button/key events get delivered to the currently focused X11 window, not the window under the cursor. `xdotool getactivewindow` and `xdotool search --name` both fail because Mutter doesn't expose EWMH `_NET_ACTIVE_WINDOW`.
- **ydotool 1.x** (built from source, daemon installed): emits events at the kernel uinput level. Verified via `evtest` that `EV_REL`/`EV_KEY` events are produced correctly. Mutter silently drops them — no pointer movement, no clicks land. Deliberate GNOME security posture against virtual input devices.
- **GNOME Shell `org.gnome.Shell.Eval`**: locked down for non-elevated callers.
- **`org.gnome.Shell.Screenshot.Screenshot`** direct DBus call: returns `Screenshot is not allowed` unless the caller holds the `org.gnome.Screenshot` well-known bus name. `silent-screenshot.py` exploits this allowlist for capture.

The one thing that does work fully is screen capture via the silent helper.

### How to drive UI when you need to

- **Browser (Chrome)** — use `playwright` MCP. CDP-attached, reliable click/type/snapshot/navigate, no compositor involvement. Covers ~95% of agent tasks.
- **Native apps** — currently impossible on this machine. Tell the user, do not pretend.

### Future fix paths (do not pursue without explicit user request)

1. **libei + xdg-desktop-portal RemoteDesktop** — the official Wayland-blessed input injection API. GNOME 45+ supports it; the user authorises a portal session once and the client gets an FD for an event stream that Mutter trusts. ~200-300 lines of Python or C. Right answer if input becomes a hard requirement.
2. **Switch to "GNOME on Xorg" session at login** — ydotool/xdotool both work fully under X11. Major UX change; requires explicit user consent.
3. **Switch compositors** (Sway, Hyprland, KDE Plasma) — these accept ydotool. Even bigger UX change.

Do not suggest reinstalling ydotool 0.1.8, fiddling with udev rules, or "just trying it again" — that path was fully exhausted with evtest verification proving Mutter is the wall.

### playwright-mcp tab management — the correct usage

`playwright.browser_tabs` works fully in CDP-attached mode despite a now-superseded note that claimed otherwise. Specifically:

- `browser_tabs action=list` enumerates tabs with their playwright-internal index. The output marks `(current)` next to whichever tab Playwright is currently bound to.
- `browser_tabs action=select index=N` is a **single atomic operation that updates BOTH** Chrome's tab strip (so the user sees the tab change) AND Playwright's internal "current page" pointer (so subsequent `browser_navigate`, `browser_snapshot`, `browser_evaluate`, etc. target the newly-selected tab). No second call required, no `Target.activateTarget` HTTP shorthand needed, no side tools.
- For selection by URL/title substring: list first, find the index, then select. Two calls but trivial.

Do not build side tools like `cdp-tabs` or `Target.activateTarget` wrappers for tab activation. That path was tried; it's redundant and creates a sync-state problem that doesn't otherwise exist. The original misdiagnosis was likely from selecting an already-current tab (no observable effect).

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
