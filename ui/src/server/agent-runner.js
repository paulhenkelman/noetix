/**
 * AgentRunner — drop-in replacement for CodexClient.
 *
 * Provides the identical public API: init(), isReady, ensureReady(),
 * createThread(), sendTurn(), shutdown().
 *
 * Internally uses LLM provider adapters, MCPManager, and AgentLoop
 * instead of the Codex app-server binary.
 */

import crypto from 'crypto';
import config from './config.js';
import { createProvider } from './llm/index.js';
import { MCPManager } from './mcp-manager.js';
import { AgentLoop } from './agent-loop.js';

const LOG_PREFIX = '[agent-runner]';

export class AgentRunner {
  constructor(opts = {}) {
    this.model = opts.model || config.llmModel;
    this.cwd = opts.cwd || config.projectRoot;
    this._provider = null;
    this._mcpManager = null;
    this._threads = new Map();
    this._ready = false;
    this._initialized = false;
    this._shutdownRequested = false;
    this._restartCount = 0;
  }

  async init() {
    if (this._shutdownRequested) return;

    try {
      // Create LLM provider
      this._provider = await createProvider(config);

      // Initialize MCP servers
      this._mcpManager = new MCPManager(config.mcpServers);
      await this._mcpManager.init();

      this._ready = true;
      this._initialized = true;
      this._restartCount = 0;

      console.log(`${LOG_PREFIX} Initialized with ${config.llmProvider}/${this.model}`);
    } catch (err) {
      console.error(`${LOG_PREFIX} Initialization failed: ${err.message}`);
      this._ready = false;
      this._initialized = false;
      throw err;
    }
  }

  get isReady() {
    return this._ready && this._initialized;
  }

  async ensureReady() {
    if (this.isReady) return true;
    if (this._shutdownRequested) return false;

    console.log(`${LOG_PREFIX} ensureReady: attempting re-init`);
    this._restartCount = 0;
    try {
      await this.init();
      return this.isReady;
    } catch (err) {
      console.error(`${LOG_PREFIX} ensureReady failed: ${err.message}`);
      return false;
    }
  }

  async createThread(baseInstructions) {
    const threadId = crypto.randomUUID();
    const messages = [];

    if (baseInstructions) {
      messages.push({ role: 'system', content: baseInstructions });
    }

    this._threads.set(threadId, { messages });
    return { threadId };
  }

  async sendTurn(threadId, userInput, onDelta, opts = {}) {
    const thread = this._threads.get(threadId);
    if (!thread) {
      throw new Error(`Thread ${threadId} not found`);
    }

    // Append user message
    thread.messages.push({ role: 'user', content: userInput });

    // Create agent loop and run
    const agentLoop = new AgentLoop({
      provider: this._provider,
      mcpManager: this._mcpManager,
      maxIterations: config.llmMaxToolIterations,
    });

    const result = await agentLoop.run(thread.messages, onDelta, {
      signal: opts.signal,
    });

    // Append assistant response to thread
    if (result.text) {
      thread.messages.push({ role: 'assistant', content: result.text });
    }

    // Return in the same format as CodexClient
    return {
      text: result.text,
      items: result.text ? [result.text] : [],
      otherEvents: result.toolCalls.map((name) => `mcpToolCall:${name}`),
      usage: null,
    };
  }

  async shutdown() {
    this._shutdownRequested = true;
    this._ready = false;
    this._initialized = false;

    if (this._mcpManager) {
      await this._mcpManager.shutdown();
      this._mcpManager = null;
    }

    this._provider = null;
    this._threads.clear();

    console.log(`${LOG_PREFIX} Shut down`);
  }

  async restart() {
    await this.shutdown();
    this._shutdownRequested = false;
    await this.init();
  }
}
