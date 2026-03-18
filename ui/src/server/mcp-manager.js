/**
 * MCP Server Manager.
 *
 * Spawns MCP servers as child processes, manages their lifecycle,
 * and provides a unified tool-calling interface via JSON-RPC 2.0
 * over stdio.
 */

import { spawn } from 'child_process';
import { createInterface } from 'readline';
import crypto from 'crypto';

const LOG_PREFIX = '[mcp-manager]';

class MCPServerConnection {
  constructor(name, serverConfig) {
    this.name = name;
    this.command = serverConfig.command;
    this.args = serverConfig.args || [];
    this.env = serverConfig.env || {};
    this.startupTimeout = serverConfig.startupTimeout || 10000;
    this.toolTimeout = serverConfig.toolTimeout || 30000;
    this.proc = null;
    this.rl = null;
    this._pending = new Map();
    this._tools = [];
    this._initialized = false;
    this._restartCount = 0;
    this._maxRestarts = 3;
    this._shutdownRequested = false;
  }

  async start() {
    if (this._shutdownRequested) return;

    this.proc = spawn(this.command, this.args, {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, ...this.env },
    });

    this.proc.stderr.on('data', (chunk) => {
      const text = chunk.toString().trim();
      if (text) console.error(`${LOG_PREFIX} ${this.name} stderr: ${text}`);
    });

    this.proc.on('error', (err) => {
      console.error(`${LOG_PREFIX} ${this.name} process error: ${err.message}`);
      this._handleCrash();
    });

    this.proc.on('exit', (code, signal) => {
      if (!this._shutdownRequested) {
        console.warn(`${LOG_PREFIX} ${this.name} exited (code=${code}, signal=${signal})`);
        this._handleCrash();
      }
    });

    this.rl = createInterface({ input: this.proc.stdout });
    this.rl.on('line', (line) => this._onLine(line));

    // Initialize handshake
    try {
      const result = await this._request('initialize', {
        protocolVersion: '2024-11-05',
        capabilities: {},
        clientInfo: { name: 'noetix-agent', version: '1.0.0' },
      }, this.startupTimeout);

      // Send initialized notification (no response expected)
      this._notify('notifications/initialized', {});

      // List available tools
      const toolsResult = await this._request('tools/list', {}, this.startupTimeout);
      this._tools = toolsResult?.tools || [];
      this._initialized = true;
      this._restartCount = 0;

      console.log(`${LOG_PREFIX} ${this.name}: initialized (${this._tools.length} tools)`);
    } catch (err) {
      console.error(`${LOG_PREFIX} ${this.name}: initialization failed: ${err.message}`);
      throw err;
    }
  }

  _onLine(line) {
    const trimmed = line.trim();
    if (!trimmed) return;

    let msg;
    try {
      msg = JSON.parse(trimmed);
    } catch {
      return;
    }

    if (msg.id !== undefined && this._pending.has(msg.id)) {
      const pending = this._pending.get(msg.id);
      this._pending.delete(msg.id);
      clearTimeout(pending.timer);

      if (msg.error) {
        pending.reject(new Error(`MCP error (${this.name}): ${msg.error.message || JSON.stringify(msg.error)}`));
      } else {
        pending.resolve(msg.result);
      }
    }
  }

  _request(method, params = {}, timeout) {
    if (!this.proc || this.proc.killed) {
      return Promise.reject(new Error(`MCP server ${this.name} is not running`));
    }

    const id = crypto.randomUUID();
    const payload = JSON.stringify({ jsonrpc: '2.0', id, method, params });
    const timeoutMs = timeout || this.toolTimeout;

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._pending.delete(id);
        reject(new Error(`MCP request ${method} to ${this.name} timed out after ${timeoutMs}ms`));
      }, timeoutMs);

      this._pending.set(id, { resolve, reject, timer });

      try {
        this.proc.stdin.write(payload + '\n');
      } catch (err) {
        this._pending.delete(id);
        clearTimeout(timer);
        reject(new Error(`Failed to write to ${this.name} stdin: ${err.message}`));
      }
    });
  }

  _notify(method, params = {}) {
    if (!this.proc || this.proc.killed) return;
    const payload = JSON.stringify({ jsonrpc: '2.0', method, params });
    try {
      this.proc.stdin.write(payload + '\n');
    } catch {
      // Best-effort notification
    }
  }

  async callTool(name, args) {
    return this._request('tools/call', { name, arguments: args });
  }

  get tools() {
    return this._tools;
  }

  get isInitialized() {
    return this._initialized && this.proc && !this.proc.killed;
  }

  _handleCrash() {
    if (this._shutdownRequested) return;

    this._initialized = false;

    // Reject all pending requests
    for (const [id, pending] of this._pending) {
      clearTimeout(pending.timer);
      pending.reject(new Error(`MCP server ${this.name} crashed`));
    }
    this._pending.clear();

    this._restartCount++;
    if (this._restartCount <= this._maxRestarts) {
      const delay = 1000 * this._restartCount;
      console.log(`${LOG_PREFIX} ${this.name}: restarting in ${delay}ms (attempt ${this._restartCount}/${this._maxRestarts})`);
      setTimeout(() => this.start().catch((err) => {
        console.error(`${LOG_PREFIX} ${this.name}: restart failed: ${err.message}`);
      }), delay);
    } else {
      console.error(`${LOG_PREFIX} ${this.name}: max restart attempts reached`);
    }
  }

  async shutdown() {
    this._shutdownRequested = true;
    this._initialized = false;

    if (this.rl) {
      this.rl.close();
      this.rl = null;
    }

    if (this.proc && !this.proc.killed) {
      const exitPromise = new Promise((resolve) => {
        const timeout = setTimeout(() => {
          if (this.proc && !this.proc.killed) this.proc.kill('SIGKILL');
          resolve();
        }, 3000);
        this.proc.once('exit', () => { clearTimeout(timeout); resolve(); });
      });
      try { this.proc.stdin.end(); } catch { /* ignore */ }
      this.proc.kill('SIGTERM');
      await exitPromise;
    }

    this.proc = null;
  }
}

export class MCPManager {
  constructor(serverConfigs) {
    this._configs = serverConfigs;
    this._connections = new Map();
    this._toolIndex = new Map(); // toolName -> MCPServerConnection
    this._ready = false;
  }

  async init() {
    const entries = Object.entries(this._configs);

    for (const [name, cfg] of entries) {
      const conn = new MCPServerConnection(name, cfg);
      this._connections.set(name, conn);
    }

    // Start all servers (sequentially to avoid stdout interleaving issues)
    for (const [name, conn] of this._connections) {
      try {
        await conn.start();
      } catch (err) {
        console.error(`${LOG_PREFIX} Failed to start ${name}: ${err.message}`);
      }
    }

    // Build tool index
    this._toolIndex.clear();
    for (const [, conn] of this._connections) {
      for (const tool of conn.tools) {
        this._toolIndex.set(tool.name, conn);
      }
    }

    this._ready = true;
    console.log(`${LOG_PREFIX} All servers initialized. Total tools: ${this._toolIndex.size}`);
  }

  getTools() {
    const tools = [];
    for (const [, conn] of this._connections) {
      tools.push(...conn.tools);
    }
    return tools;
  }

  async callTool(name, args) {
    const conn = this._toolIndex.get(name);
    if (!conn) {
      throw new Error(`Unknown tool: ${name}`);
    }
    if (!conn.isInitialized) {
      throw new Error(`MCP server for tool ${name} is not ready`);
    }
    return conn.callTool(name, args);
  }

  get isReady() {
    if (!this._ready) return false;
    for (const [, conn] of this._connections) {
      if (!conn.isInitialized) return false;
    }
    return true;
  }

  async shutdown() {
    const promises = [];
    for (const [, conn] of this._connections) {
      promises.push(conn.shutdown());
    }
    await Promise.all(promises);
    this._connections.clear();
    this._toolIndex.clear();
    this._ready = false;
    console.log(`${LOG_PREFIX} All servers shut down`);
  }
}
