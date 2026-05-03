#!/usr/bin/env node
/**
 * host-control MCP server (currently: screenshot-only).
 *
 * Originally intended to expose Anthropic-style mouse/keyboard/screenshot tools
 * via xdotool + ydotool, but synthetic input does not work reliably on this
 * machine (GNOME on Wayland — Mutter silently drops uinput events from
 * non-physical devices). All input tools were removed to avoid misleading
 * the model into thinking it can drive the desktop.
 *
 * What works: silent, flashless screen capture via GNOME Shell's privileged
 * DBus method (see silent-screenshot.py). What's gone: cursor position,
 * mouse moves, clicks, drags, scroll, key, type. See CLAUDE.md for the full
 * diagnostic and the libei/RemoteDesktop path forward.
 *
 * Transport: stdio JSON-RPC (same shape as kb-mcp-server.js).
 *
 * Env:
 *   DISPLAY         — X display (default ":0", used only by fallback backends)
 *   SCREENSHOT_DIR  — where transient PNGs land (default /tmp)
 */

import { spawn, execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const DISPLAY = process.env.DISPLAY || ':0';
const SCREENSHOT_DIR = process.env.SCREENSHOT_DIR || '/tmp';

function runCmd(cmd, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { env: { ...process.env, DISPLAY } });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => (stdout += d.toString()));
    child.stderr.on('data', (d) => (stderr += d.toString()));
    child.on('error', reject);
    child.on('close', (code) => {
      if (code === 0) resolve({ stdout, stderr });
      else reject(new Error(`${cmd} exited ${code}: ${stderr || stdout}`));
    });
  });
}

const TOOLS = [
  {
    name: 'screenshot',
    description: 'Capture the current desktop as a PNG and return it as an inline image. Silent (no shutter sound) and flashless. This server is screenshot-only — synthetic mouse/keyboard input is not available on this host (Mutter+Wayland blocks it). For browser interaction, use the playwright MCP instead.',
    inputSchema: {
      type: 'object',
      properties: {
        delay_ms: { type: 'number', description: 'Optional wait before capture (ms, default 0)' }
      }
    }
  }
];

// Backend selection. silent-screenshot.py is preferred: it claims the
// org.gnome.Screenshot DBus name (allowlisted by Mutter) and calls the
// privileged Screenshot method with flash=false, suppressing both the visual
// flash AND the shutter sound. gnome-screenshot is loud; grim is wlroots-only;
// scrot returns black on Wayland because it goes through XWayland.
const SILENT_HELPER = path.join(path.dirname(new URL(import.meta.url).pathname), 'silent-screenshot.py');
let SCREENSHOT_TOOL = null;
function pickScreenshotTool() {
  if (SCREENSHOT_TOOL) return SCREENSHOT_TOOL;
  const candidates = [];
  if (fs.existsSync(SILENT_HELPER)) {
    candidates.push({ bin: 'python3', args: (file) => [SILENT_HELPER, file] });
  }
  candidates.push(
    { bin: 'gnome-screenshot', args: (file) => ['-f', file] },
    { bin: 'grim', args: (file) => [file] },
    { bin: 'scrot', args: (file) => ['-o', file] }
  );
  for (const candidate of candidates) {
    try {
      execFileSync('which', [candidate.bin], { stdio: 'ignore' });
      SCREENSHOT_TOOL = candidate;
      return candidate;
    } catch { /* not installed, try next */ }
  }
  throw new Error('No screenshot backend found');
}

async function takeScreenshot(delayMs = 0) {
  if (delayMs > 0) await new Promise((r) => setTimeout(r, delayMs));
  const tool = pickScreenshotTool();
  const file = path.join(SCREENSHOT_DIR, `cu_${Date.now()}_${process.pid}.png`);
  await runCmd(tool.bin, tool.args(file));
  const buf = fs.readFileSync(file);
  fs.unlinkSync(file);
  return {
    content: [
      { type: 'image', data: buf.toString('base64'), mimeType: 'image/png' }
    ]
  };
}

const HANDLERS = {
  screenshot: async ({ delay_ms = 0 } = {}) => takeScreenshot(delay_ms)
};

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + '\n');
}

async function handle(req) {
  const { id, method, params } = req;
  try {
    if (method === 'initialize') {
      return {
        jsonrpc: '2.0', id,
        result: {
          protocolVersion: '2024-11-05',
          capabilities: { tools: {} },
          serverInfo: { name: 'host-control', version: '2.0.0' }
        }
      };
    }
    if (method === 'notifications/initialized') return null;
    if (method === 'tools/list') {
      return { jsonrpc: '2.0', id, result: { tools: TOOLS } };
    }
    if (method === 'tools/call') {
      const { name, arguments: args = {} } = params;
      const handler = HANDLERS[name];
      if (!handler) {
        return { jsonrpc: '2.0', id, result: { isError: true, content: [{ type: 'text', text: `unknown tool: ${name}` }] } };
      }
      try {
        const result = await handler(args);
        return { jsonrpc: '2.0', id, result };
      } catch (e) {
        return { jsonrpc: '2.0', id, result: { isError: true, content: [{ type: 'text', text: `${name} failed: ${e.message}` }] } };
      }
    }
    return { jsonrpc: '2.0', id, error: { code: -32601, message: `method not found: ${method}` } };
  } catch (e) {
    return { jsonrpc: '2.0', id, error: { code: -32603, message: e.message } };
  }
}

let buf = '';
process.stdin.on('data', async (chunk) => {
  buf += chunk.toString();
  const lines = buf.split('\n');
  buf = lines.pop();
  for (const line of lines) {
    if (!line.trim()) continue;
    let req;
    try { req = JSON.parse(line); }
    catch { continue; }
    const resp = await handle(req);
    if (resp) send(resp);
  }
});

process.stderr.write(`[host-control-mcp] Started. screenshot-only.\n`);
