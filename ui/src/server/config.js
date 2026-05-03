/**
 * Noetix configuration loader.
 *
 * Reads ui.config and noetix.config from the project root and exports
 * a flat config object used by all server modules.
 *
 * Uses smol-toml for TOML parsing.
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { parse } from 'smol-toml';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PROJECT_ROOT = path.resolve(__dirname, '..', '..', '..');

function loadToml(filename) {
  const filePath = path.join(PROJECT_ROOT, filename);
  try {
    return parse(fs.readFileSync(filePath, 'utf-8'));
  } catch (err) {
    console.warn(`[config] Failed to load ${filePath}: ${err.message}`);
    return {};
  }
}

function expandHome(p) {
  if (typeof p === 'string' && p.startsWith('~/')) {
    return path.join(process.env.HOME || '/root', p.slice(2));
  }
  return p;
}

const ui = loadToml('ui.config');
const noetix = loadToml('noetix.config');

// Resolve MCP server configs (replaces scripts/generate-codex-config.js)
const mcpRaw = noetix.mcp_servers || {};
const remoteBase = process.env.REMOTE_BASE || ui.backend?.url || 'http://10.0.0.50:8001';
const socksProxyVal = process.env.SOCKS_PROXY || ui.backend?.socks_proxy || '';
const downloadsDirVal = expandHome(ui.playwright?.downloads_dir || '~/.cache/noetix-playwright');

const kbServerPath = path.join(PROJECT_ROOT, 'ui', 'src', 'server', 'kb-mcp-server.js');
const contentServerPath = path.join(PROJECT_ROOT, 'ui', 'src', 'server', 'content-mcp-server.js');
const playwrightCfg = mcpRaw.playwright || {};
const playwrightOutputDir = expandHome(playwrightCfg.output_dir || '~/.cache/noetix-playwright');
const cdpEndpoint = playwrightCfg.cdp_endpoint || 'http://localhost:9222';

const mcpServers = {
  kb: {
    command: mcpRaw.kb?.command || 'node',
    args: [kbServerPath],
    env: { REMOTE_BASE: remoteBase, SOCKS_PROXY: socksProxyVal },
    startupTimeout: (mcpRaw.kb?.startup_timeout_sec || 10) * 1000,
    toolTimeout: (mcpRaw.kb?.tool_timeout_sec || 30) * 1000,
  },
  content: {
    command: mcpRaw.content?.command || 'node',
    args: [contentServerPath],
    env: { REMOTE_BASE: remoteBase, DOWNLOADS_DIR: downloadsDirVal },
    startupTimeout: (mcpRaw.content?.startup_timeout_sec || 10) * 1000,
    toolTimeout: (mcpRaw.content?.tool_timeout_sec || 600) * 1000,
  },
  playwright: {
    command: playwrightCfg.command || 'playwright-mcp',
    args: ['--cdp-endpoint', cdpEndpoint],
    env: { OUTPUT_DIR: playwrightOutputDir },
    startupTimeout: (playwrightCfg.startup_timeout_sec || 15) * 1000,
    toolTimeout: (playwrightCfg.tool_timeout_sec || 60) * 1000,
  },
};

const config = {
  // Project
  projectRoot: PROJECT_ROOT,
  projectName: noetix.project?.name || 'noetix',

  // Server
  host: process.env.HOST || ui.server?.host || '0.0.0.0',
  port: Number(process.env.PORT || ui.server?.port || 8788),
  corsOrigins: ui.server?.cors_origins || ['http://localhost:5174', 'http://127.0.0.1:5174'],
  uploadTmpDir: ui.server?.upload_tmp_dir || '/tmp/noetix-upload',
  actionsMode: process.env.ACTIONS_MODE || ui.server?.actions_mode || 'remote',

  // Backend
  remoteBase,
  socksProxy: socksProxyVal,
  backendTimeout: ui.backend?.request_timeout_ms || 60000,

  // Playwright
  playwrightMcpUrl: process.env.PLAYWRIGHT_MCP_URL || ui.playwright?.mcp_url || 'http://localhost:8931/mcp',
  playwrightProbeTimeout: ui.playwright?.probe_timeout_ms || 3000,
  playwrightFallbackPorts: ui.playwright?.fallback_ports || [8931, 8932, 3000],
  downloadsDir: downloadsDirVal,

  // MCP servers (structured)
  mcpServers,

  // Frontend
  apiBase: ui.frontend?.api_base || 'http://127.0.0.1:8788',
  vitePort: ui.frontend?.vite_port || 5174,
};

export default config;
