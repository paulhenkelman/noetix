/**
 * Codex App Server Client (v0.106+ protocol)
 *
 * Manages a long-lived `codex app-server` child process and provides a
 * high-level API for thread/turn operations over the JSONL JSON-RPC 2.0
 * stdio transport.
 *
 * v0.106+ protocol:
 *   initialize → thread/start → turn/start
 *   Events auto-stream as item/agentMessage/delta, item/completed, turn/completed.
 */

import { spawn } from 'child_process';
import { createInterface } from 'readline';
import crypto from 'crypto';
import config from './config.js';

const REQUEST_TIMEOUT_MS = config.codexRequestTimeout;
const RESTART_DELAY_MS = config.codexRestartDelay;
const MAX_RESTART_ATTEMPTS = config.codexMaxRestartAttempts;

export class CodexClient {
  constructor(opts = {}) {
    this.codexBin = opts.codexBin || 'codex';
    this.model = opts.model || config.codexModel;
    this.cwd = opts.cwd || config.projectRoot;
    this.proc = null;
    this.rl = null;
    this._pending = new Map();       // id -> { resolve, reject, timer }
    this._turnWaiters = new Map();   // threadId -> { resolve, reject, timer, items, otherEvents }
    this._ready = false;
    this._initialized = false;
    this._restartCount = 0;
    this._shutdownRequested = false;
  }

  /**
   * Start the codex app-server process, send initialize handshake.
   */
  async init() {
    await this._spawn();
  }

  async _spawn() {
    if (this._shutdownRequested) return;

    this._initialized = false;

    this.proc = spawn(this.codexBin, ['app-server'], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env }
    });

    this.proc.stderr.on('data', (chunk) => {
      const text = chunk.toString().trim();
      if (text) console.error(`[codex-app-server stderr] ${text}`);
    });

    this.proc.on('error', (err) => {
      console.error(`[codex-client] Process error: ${err.message}`);
      this._handleCrash();
    });

    this.proc.on('exit', (code, signal) => {
      console.warn(`[codex-client] Process exited (code=${code}, signal=${signal})`);
      this._handleCrash();
    });

    // Line-buffered JSONL reader on stdout
    this.rl = createInterface({ input: this.proc.stdout });
    this.rl.on('line', (line) => this._onLine(line));

    this._ready = true;
    this._restartCount = 0;

    // Send initialize handshake
    try {
      await this._request('initialize', {
        clientInfo: { name: 'noetix-ui', title: 'Noetix UI', version: '1.0.0' }
      });
      this._initialized = true;
      console.log('[codex-client] app-server initialized');
    } catch (err) {
      console.error(`[codex-client] initialize handshake failed: ${err.message}`);
    }
  }

  _onLine(line) {
    const trimmed = line.trim();
    if (!trimmed) return;

    let msg;
    try {
      msg = JSON.parse(trimmed);
    } catch (err) {
      console.warn(`[codex-client] Non-JSON line: ${trimmed.slice(0, 200)}`);
      return;
    }

    // JSON-RPC response (has id matching a pending request)
    if (msg.id !== undefined && this._pending.has(msg.id)) {
      const pending = this._pending.get(msg.id);
      this._pending.delete(msg.id);
      clearTimeout(pending.timer);

      if (msg.error) {
        pending.reject(new CodexRpcError(msg.error.code, msg.error.message, msg.error.data));
      } else {
        pending.resolve(msg.result);
      }
      return;
    }

    // JSON-RPC notification (no id, has method)
    if (msg.method) {
      this._handleNotification(msg.method, msg.params);
      return;
    }
  }

  _handleNotification(method, params) {
    // v0.106 emits both new-style (threadId) and legacy (conversationId) events
    const threadId = params?.threadId || params?.conversationId;
    const waiter = threadId && this._turnWaiters.get(threadId);

    switch (method) {
      case 'item/completed': {
        // Completed item — collect agent messages and track tool usage
        const item = params?.item;
        if (!item) break;

        if (item.type === 'agentMessage' && waiter) {
          const text = item.text;
          if (text) {
            waiter.items.push(text);
          }
        } else if (item.type === 'userMessage' || item.type === 'reasoning') {
          // Internal bookkeeping items — not tool events, not agent text
          break;
        } else if (waiter) {
          // Non-message items are tool usage: mcpToolCall, commandExecution, dynamicToolCall, fileChange, webSearch
          waiter.otherEvents.push(item.type || method);
        }
        break;
      }

      case 'item/agentMessage/delta': {
        // Streaming text delta (canonical new-style event — 1 per logical delta)
        if (waiter?.onDelta && params?.delta) {
          waiter.onDelta({ type: 'text', delta: params.delta });
        }
        break;
      }

      case 'item/reasoning/summaryTextDelta': {
        // Reasoning delta (canonical new-style event — 1 per logical delta)
        if (waiter?.onDelta && params?.delta) {
          waiter.onDelta({ type: 'thinking', delta: params.delta });
        }
        break;
      }

      case 'codex/event/mcp_tool_call_begin': {
        // MCP tool call starting — forward as status
        if (waiter?.onDelta) {
          const tool = params?.msg?.invocation?.tool || params?.tool || 'tool';
          waiter.onDelta({ type: 'status', text: `Calling ${tool}...` });
        }
        break;
      }

      case 'codex/event/mcp_tool_call_end': {
        // MCP tool call completed — forward as status
        if (waiter?.onDelta) {
          const tool = params?.msg?.invocation?.tool || params?.tool || 'tool';
          waiter.onDelta({ type: 'status', text: `Completed ${tool}` });
        }
        break;
      }

      case 'codex/event/task_started': {
        if (waiter?.onDelta) {
          waiter.onDelta({ type: 'status', text: 'Agent working...' });
        }
        break;
      }

      case 'codex/event/task_complete': {
        if (waiter?.onDelta) {
          waiter.onDelta({ type: 'status', text: 'Agent finished' });
        }
        break;
      }

      // Legacy duplicate events — suppress to avoid tripling SSE events
      // The app-server emits each delta as 3 method calls: item/*, codex/event/*, codex/event/*_content_*
      // We only handle the canonical item/* events above.
      case 'codex/event/agent_message_delta':
      case 'codex/event/agent_message_content_delta':
      case 'codex/event/reasoning_content_delta':
      case 'codex/event/agent_reasoning_delta':
      case 'codex/event/agent_reasoning_section_break':
      case 'codex/event/agent_reasoning':
      case 'codex/event/agent_message':
      case 'item/reasoning/summaryPartAdded':
      case 'turn/started':
      case 'item/started':
      case 'thread/status/changed':
      case 'thread/tokenUsage/updated':
      case 'codex/event/token_count':
      case 'codex/event/mcp_startup_update':
      case 'codex/event/mcp_startup_complete':
      case 'codex/event/item_started':
      case 'codex/event/item_completed':
      case 'codex/event/user_message': {
        // Known events — no action needed (legacy duplicates or informational)
        break;
      }

      case 'turn/completed': {
        // Turn finished — resolve or reject based on status
        const turn = params?.turn;
        if (waiter) {
          this._turnWaiters.delete(threadId);
          clearTimeout(waiter.timer);

          if (turn?.status === 'failed') {
            const raw = turn.error?.message ?? 'Turn failed';
            const errMsg = typeof raw === 'string' ? raw : JSON.stringify(raw);
            waiter.reject(new Error(errMsg));
          } else {
            waiter.resolve({
              text: waiter.items.join('\n\n'),
              items: waiter.items,
              otherEvents: waiter.otherEvents,
              usage: null
            });
          }
        }
        break;
      }

      case 'error': {
        // General error — reject all waiters (process-level error)
        const rawErr = params?.message || params?.error || 'Unknown error';
        const errMsg = typeof rawErr === 'string' ? rawErr : JSON.stringify(rawErr);
        if (waiter) {
          this._turnWaiters.delete(threadId);
          clearTimeout(waiter.timer);
          waiter.reject(new Error(errMsg));
        }
        break;
      }

      // Track any other events for tool-usage detection
      default:
        if (waiter) {
          waiter.otherEvents.push(method);
        }
        break;
    }
  }

  /**
   * Send a JSON-RPC request and return a promise for the response.
   */
  _request(method, params = {}) {
    if (!this._ready || !this.proc || this.proc.killed) {
      return Promise.reject(new Error('Codex app-server is not running'));
    }

    const id = crypto.randomUUID();
    const payload = JSON.stringify({ jsonrpc: '2.0', id, method, params });

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._pending.delete(id);
        reject(new Error(`Request ${method} timed out after ${REQUEST_TIMEOUT_MS}ms`));
      }, REQUEST_TIMEOUT_MS);

      this._pending.set(id, { resolve, reject, timer });

      try {
        this.proc.stdin.write(payload + '\n');
      } catch (err) {
        this._pending.delete(id);
        clearTimeout(timer);
        reject(new Error(`Failed to write to app-server stdin: ${err.message}`));
      }
    });
  }

  /**
   * Create a new thread with system instructions.
   * Returns { threadId }.
   */
  async createThread(baseInstructions) {
    const params = {
      cwd: this.cwd,
      approvalPolicy: 'never',
      sandbox: 'danger-full-access'
    };
    if (baseInstructions) {
      params.baseInstructions = baseInstructions;
    }
    const result = await this._request('thread/start', params);
    const threadId = result?.thread?.id;

    // No addConversationListener needed — events auto-stream in v0.106+
    return { threadId };
  }

  /**
   * Send a user message as a new turn in an existing thread.
   * Collects events until turn/completed.
   * Returns { text, items, otherEvents, usage }.
   */
  async sendTurn(threadId, userInput, onDelta, opts = {}) {
    // Set up the notification waiter BEFORE sending the request
    const turnPromise = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._turnWaiters.delete(threadId);
        reject(new Error(`Turn timed out after ${REQUEST_TIMEOUT_MS}ms`));
      }, REQUEST_TIMEOUT_MS);

      this._turnWaiters.set(threadId, { resolve, reject, timer, items: [], otherEvents: [], onDelta: onDelta || null });
    });

    // Send the user turn (v0.106+ — only threadId + input; effort overrides reasoning depth)
    const params = {
      threadId,
      input: [{ type: 'text', text: userInput }]
    };
    if (opts.effort) params.effort = opts.effort;

    await this._request('turn/start', params);

    return turnPromise;
  }

  /**
   * Handle process crash — reject all pending requests and auto-restart.
   */
  _handleCrash() {
    if (this._shutdownRequested) return;

    this._ready = false;
    this._initialized = false;

    for (const [id, pending] of this._pending) {
      clearTimeout(pending.timer);
      pending.reject(new Error('Codex app-server process crashed'));
    }
    this._pending.clear();

    for (const [threadId, waiter] of this._turnWaiters) {
      clearTimeout(waiter.timer);
      waiter.reject(new Error('Codex app-server process crashed'));
    }
    this._turnWaiters.clear();

    this._restartCount++;
    if (this._restartCount <= MAX_RESTART_ATTEMPTS) {
      const delay = RESTART_DELAY_MS * this._restartCount;
      console.log(`[codex-client] Restarting in ${delay}ms (attempt ${this._restartCount}/${MAX_RESTART_ATTEMPTS})`);
      setTimeout(() => this._spawn(), delay);
    } else {
      console.error(`[codex-client] Max restart attempts (${MAX_RESTART_ATTEMPTS}) reached.`);
    }
  }

  async shutdown() {
    this._shutdownRequested = true;
    this._ready = false;
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
        }, 5000);
        this.proc.once('exit', () => { clearTimeout(timeout); resolve(); });
      });
      this.proc.stdin.end();
      this.proc.kill('SIGTERM');
      await exitPromise;
    }

    this.proc = null;
    console.log('[codex-client] Shut down');
  }

  get isReady() {
    return this._ready && this._initialized && this.proc && !this.proc.killed;
  }

  async ensureReady() {
    if (this.isReady) return true;
    if (this._shutdownRequested) return false;

    console.log('[codex-client] ensureReady: attempting re-init');
    this._restartCount = 0;
    try {
      await this._spawn();
      return this.isReady;
    } catch (err) {
      console.error(`[codex-client] ensureReady failed: ${err.message}`);
      return false;
    }
  }
}

export class CodexRpcError extends Error {
  constructor(code, message, data) {
    super(typeof message === 'string' ? message : JSON.stringify(message));
    this.name = 'CodexRpcError';
    this.code = code;
    this.data = data;
  }
}
