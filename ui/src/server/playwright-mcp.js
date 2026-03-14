/**
 * Playwright MCP session detection and binding.
 *
 * Detects a running Playwright MCP endpoint (typically on 127.0.0.1:8931/mcp)
 * and provides diagnostics/health checks. When the endpoint is live, consumers
 * can bind to the existing browser session instead of forcing a managed/relay spawn.
 */

import http from 'http';
import config from './config.js';

const DEFAULT_MCP_URL = config.playwrightMcpUrl;
const PROBE_TIMEOUT_MS = config.playwrightProbeTimeout;

/**
 * Probe a Playwright MCP endpoint to check if it's alive and responsive.
 * Returns { alive, url, sessionId?, error?, latencyMs }.
 */
export async function probeMcpEndpoint(url = DEFAULT_MCP_URL) {
  const start = Date.now();
  const parsed = new URL(url);
  return new Promise((resolve) => {
    const req = http.request(
      {
        hostname: parsed.hostname,
        port: parsed.port || 80,
        path: parsed.pathname,
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        timeout: PROBE_TIMEOUT_MS
      },
      (res) => {
        let body = '';
        res.on('data', (chunk) => (body += chunk));
        res.on('end', () => {
          const latencyMs = Date.now() - start;
          // MCP endpoints respond to JSON-RPC; any 2xx or recognizable body means alive
          if (res.statusCode >= 200 && res.statusCode < 500) {
            let sessionId = null;
            try {
              const data = JSON.parse(body);
              sessionId = data?.result?.sessionId || data?.sessionId || null;
            } catch {
              // non-JSON response is fine; endpoint is still alive
            }
            resolve({ alive: true, url, sessionId, latencyMs, statusCode: res.statusCode });
          } else {
            resolve({
              alive: false,
              url,
              error: `HTTP ${res.statusCode}: ${body.slice(0, 200)}`,
              latencyMs
            });
          }
        });
      }
    );

    req.on('error', (err) => {
      resolve({
        alive: false,
        url,
        error: `Connection failed: ${err.message}`,
        latencyMs: Date.now() - start
      });
    });

    req.on('timeout', () => {
      req.destroy();
      resolve({
        alive: false,
        url,
        error: `Probe timed out after ${PROBE_TIMEOUT_MS}ms`,
        latencyMs: Date.now() - start
      });
    });

    // Send a minimal JSON-RPC initialize probe
    req.write(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} }));
    req.end();
  });
}

/**
 * Detect all candidate Playwright MCP endpoints and return the first live one.
 * Checks the configured URL first, then common fallback ports.
 */
export async function detectLiveEndpoint(configuredUrl = DEFAULT_MCP_URL) {
  const candidates = [configuredUrl];
  // Fallback ports from config
  const fallbackPorts = config.playwrightFallbackPorts;
  for (const port of fallbackPorts) {
    const fallback = `http://127.0.0.1:${port}/mcp`;
    if (!candidates.includes(fallback)) candidates.push(fallback);
  }

  const results = await Promise.all(candidates.map((url) => probeMcpEndpoint(url)));
  const live = results.find((r) => r.alive);
  return {
    live: live || null,
    candidates: results,
    diagnostics: results.map((r) => ({
      url: r.url,
      alive: r.alive,
      latencyMs: r.latencyMs,
      error: r.error || null
    }))
  };
}

/**
 * Build a diagnostic message string for logging or API responses.
 */
export function formatDiagnostics(detection) {
  if (detection.live) {
    return `Playwright MCP endpoint alive at ${detection.live.url} (${detection.live.latencyMs}ms)${
      detection.live.sessionId ? `, session: ${detection.live.sessionId}` : ''
    }`;
  }
  const tried = detection.diagnostics
    .map((d) => `  ${d.url}: ${d.error}`)
    .join('\n');
  return `No live Playwright MCP endpoint found. Tried:\n${tried}\n\nEnsure Playwright MCP is running: npx @anthropic-ai/playwright-mcp --port 8931`;
}

// ---------------------------------------------------------------------------
// MCP JSON-RPC client — allows the server to call Playwright tools directly
// ---------------------------------------------------------------------------

const MCP_CALL_TIMEOUT_MS = 30000;
let mcpRequestId = 100;

/**
 * Send a JSON-RPC request to an MCP endpoint and return the parsed response.
 */
export function mcpCall(url, method, params = {}) {
  const id = ++mcpRequestId;
  const parsed = new URL(url);
  const payload = JSON.stringify({ jsonrpc: '2.0', id, method, params });

  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        hostname: parsed.hostname,
        port: parsed.port || 80,
        path: parsed.pathname,
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) },
        timeout: MCP_CALL_TIMEOUT_MS
      },
      (res) => {
        let body = '';
        res.on('data', (chunk) => (body += chunk));
        res.on('end', () => {
          try {
            const data = JSON.parse(body);
            if (data.error) {
              reject(new Error(`MCP error ${data.error.code}: ${data.error.message}`));
            } else {
              resolve(data.result);
            }
          } catch {
            reject(new Error(`MCP non-JSON response (HTTP ${res.statusCode}): ${body.slice(0, 300)}`));
          }
        });
      }
    );
    req.on('error', (err) => reject(new Error(`MCP connection failed: ${err.message}`)));
    req.on('timeout', () => { req.destroy(); reject(new Error(`MCP call timed out after ${MCP_CALL_TIMEOUT_MS}ms`)); });
    req.write(payload);
    req.end();
  });
}

/**
 * Call a Playwright MCP tool by name.  Wraps mcpCall with tools/call convention.
 */
export async function callPlaywrightTool(url, toolName, args = {}) {
  return mcpCall(url, 'tools/call', { name: toolName, arguments: args });
}

/**
 * Execute a browser automation sequence:
 *   1. browser_snapshot — get current page state / tab list
 *   2. (optional) browser_tab_select — switch to named tab
 *   3. browser_snapshot — read content of target tab
 * Returns { tabs, snapshot, targetTab, error }.
 */
export async function browserReadTab(url, tabQuery) {
  const result = { tabs: null, snapshot: null, targetTab: null, error: null };

  try {
    // Step 1: initial snapshot to discover tabs
    const snap1 = await callPlaywrightTool(url, 'browser_snapshot', {});
    result.snapshot = extractTextContent(snap1);

    // Parse tab list from snapshot (Playwright MCP returns accessibility tree with tab info)
    result.tabs = parseTabsFromSnapshot(result.snapshot);

    // Step 2: if a tab query was given, try to switch to it
    if (tabQuery && result.tabs.length) {
      const match = result.tabs.find((t) =>
        t.title.toLowerCase().includes(tabQuery.toLowerCase())
      );
      if (match) {
        result.targetTab = match;
        try {
          await callPlaywrightTool(url, 'browser_tab_select', { ref: match.ref });
        } catch {
          // Some MCP versions use index instead of ref
          try {
            await callPlaywrightTool(url, 'browser_tab_select', { index: match.index });
          } catch (e2) {
            result.error = `Tab switch failed: ${e2.message}`;
            return result;
          }
        }
        // Step 3: snapshot the target tab
        const snap2 = await callPlaywrightTool(url, 'browser_snapshot', {});
        result.snapshot = extractTextContent(snap2);
      } else {
        result.error = `No tab matching "${tabQuery}" found. Open tabs: ${result.tabs.map((t) => t.title).join(', ')}`;
      }
    }
  } catch (err) {
    result.error = `Browser automation failed: ${err.message}`;
  }

  return result;
}

/** Extract text content from an MCP tool result (handles various response shapes). */
function extractTextContent(mcpResult) {
  if (!mcpResult) return '';
  if (typeof mcpResult === 'string') return mcpResult;
  // MCP tools/call returns { content: [{ type: 'text', text: '...' }] }
  if (Array.isArray(mcpResult.content)) {
    return mcpResult.content
      .filter((c) => c.type === 'text')
      .map((c) => c.text)
      .join('\n');
  }
  if (mcpResult.text) return mcpResult.text;
  return JSON.stringify(mcpResult);
}

/** Parse tab information from a Playwright MCP snapshot's accessibility tree text. */
function parseTabsFromSnapshot(snapshotText) {
  const tabs = [];
  // Playwright MCP snapshots include tab entries like:
  //   [tab ref="T1"] Title of tab   or   tab "Title" [ref=T1]
  // We match several common formats.
  const patterns = [
    /\[tab(?:\s+ref="?([^"\]]+)"?)?\]\s*(.+)/gi,
    /tab\s+"([^"]+)"(?:\s*\[ref=([^\]]+)\])?/gi,
    /- tab:\s*(.+?)(?:\s*\(ref:\s*([^)]+)\))?$/gim
  ];
  for (const pat of patterns) {
    let m;
    while ((m = pat.exec(snapshotText)) !== null) {
      const title = (m[2] || m[1] || '').trim();
      const ref = (m[1] || m[2] || '').trim();
      if (title) tabs.push({ title, ref, index: tabs.length });
    }
    if (tabs.length) break;
  }
  return tabs;
}

/** Detect if a user message is a browser automation request. */
export const BROWSER_ACTION_PATTERN = /\b(playwright|browser|tab|outlook|gmail|inbox|email|webpage?|click|navigate|open\s+(the\s+)?page|read\s+(the\s+)?(first|latest|recent|last)\s+(message|email|mail))\b/i;
