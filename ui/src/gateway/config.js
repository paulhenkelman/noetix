/**
 * Noetix configuration loader.
 *
 * Reads ui.config and noetix.config from the project root and exports
 * a flat config object used by all gateway modules.
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

const config = {
  // Project
  projectRoot: PROJECT_ROOT,
  projectName: noetix.project?.name || 'noetix',

  // Gateway
  host: process.env.HOST || ui.gateway?.host || '0.0.0.0',
  port: Number(process.env.PORT || ui.gateway?.port || 8788),
  corsOrigins: ui.gateway?.cors_origins || ['http://localhost:5174', 'http://127.0.0.1:5174'],
  uploadTmpDir: ui.gateway?.upload_tmp_dir || '/tmp/noetix-upload',
  actionsMode: process.env.ACTIONS_MODE || ui.gateway?.actions_mode || 'remote',

  // Backend
  remoteBase: process.env.REMOTE_BASE || ui.backend?.url || 'http://10.0.0.50:8001',
  socksProxy: process.env.SOCKS_PROXY || ui.backend?.socks_proxy || '',
  backendTimeout: ui.backend?.request_timeout_ms || 60000,

  // Playwright
  playwrightMcpUrl: process.env.PLAYWRIGHT_MCP_URL || ui.playwright?.mcp_url || 'http://localhost:8931/mcp',
  playwrightProbeTimeout: ui.playwright?.probe_timeout_ms || 3000,
  playwrightFallbackPorts: ui.playwright?.fallback_ports || [8931, 8932, 3000],
  downloadsDir: expandHome(ui.playwright?.downloads_dir || '~/.cache/noetix-playwright'),

  // Codex client
  codexRequestTimeout: ui.codex_client?.request_timeout_ms || 7200000,
  codexRestartDelay: ui.codex_client?.restart_delay_ms || 1000,
  codexMaxRestartAttempts: ui.codex_client?.max_restart_attempts || 5,

  // Codex model
  codexModel: noetix.codex?.model || 'gpt-5.3-codex',
  codexReasoningEffort: noetix.codex?.reasoning_effort || 'xhigh',

  // Verify
  maxVerifyAttempts: ui.verify?.max_attempts || 2,

  // Frontend
  apiBase: ui.frontend?.api_base || 'http://127.0.0.1:8788',
  vitePort: ui.frontend?.vite_port || 5174,
};

export default config;
