import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { EventEmitter } from 'events';
import { Readable, Writable } from 'stream';

// Mock child_process.spawn before importing CodexClient
const mockProcs = [];

function createMockProc() {
  const stdin = new Writable({ write(_chunk, _enc, cb) { cb(); } });
  const stdout = new Readable({ read() {} });
  const stderr = new Readable({ read() {} });
  const proc = Object.assign(new EventEmitter(), {
    stdin,
    stdout,
    stderr,
    killed: false,
    kill(signal) { this.killed = true; this.emit('exit', null, signal); },
    pid: Math.floor(Math.random() * 100000)
  });
  mockProcs.push(proc);
  return proc;
}

vi.mock('child_process', () => ({
  spawn: vi.fn(() => createMockProc())
}));

const { CodexClient, CodexRpcError } = await import('../gateway/codex-client.js');

/** Send a JSONL line to the mock proc's stdout as if the app-server wrote it. */
function sendLine(proc, obj) {
  proc.stdout.push(JSON.stringify(obj) + '\n');
}

/** Intercept stdin writes and auto-respond to initialize */
function interceptWrites(proc) {
  const writes = [];
  proc.stdin.write = (chunk, enc, cb) => {
    writes.push(chunk);
    if (typeof enc === 'function') enc();
    else if (cb) cb();
    // Auto-respond to initialize
    const msg = JSON.parse(chunk);
    if (msg.method === 'initialize') {
      sendLine(proc, { id: msg.id, result: { userAgent: 'test/1.0' } });
    }
    return true;
  };
  return writes;
}

describe('CodexClient', () => {
  let client;

  beforeEach(() => {
    mockProcs.length = 0;
    client = new CodexClient();
  });

  afterEach(async () => {
    if (client) await client.shutdown();
    for (const proc of mockProcs) {
      if (!proc.killed) proc.kill();
    }
  });

  describe('init / process management', () => {
    it('should spawn app-server and send initialize', async () => {
      const proc = createMockProc();
      const origSpawn = (await import('child_process')).spawn;
      origSpawn.mockReturnValueOnce(proc);
      interceptWrites(proc);
      await client.init();
      expect(client.isReady).toBe(true);
      expect(mockProcs.length).toBeGreaterThanOrEqual(1);
    });

    it('should report not ready before init', () => {
      expect(client.isReady).toBe(false);
    });

    it('should report not ready after shutdown', async () => {
      const proc = createMockProc();
      const origSpawn = (await import('child_process')).spawn;
      origSpawn.mockReturnValueOnce(proc);
      interceptWrites(proc);
      await client.init();
      expect(client.isReady).toBe(true);
      await client.shutdown();
      expect(client.isReady).toBe(false);
    });
  });

  describe('JSONL message parsing', () => {
    async function initClient() {
      const proc = createMockProc();
      const origSpawn = (await import('child_process')).spawn;
      origSpawn.mockReturnValueOnce(proc);
      interceptWrites(proc);
      await client.init();
      return proc;
    }

    it('should parse JSON-RPC response and resolve pending request', async () => {
      const proc = await initClient();
      const writes = [];
      proc.stdin.write = (chunk, enc, cb) => {
        writes.push(chunk);
        if (typeof enc === 'function') enc();
        else if (cb) cb();
        return true;
      };

      const promise = client._request('test/method', { foo: 'bar' });
      await new Promise((r) => setTimeout(r, 10));

      const sent = JSON.parse(writes[0]);
      expect(sent.method).toBe('test/method');
      sendLine(proc, { id: sent.id, result: { success: true } });

      const result = await promise;
      expect(result).toEqual({ success: true });
    });

    it('should reject on JSON-RPC error response', async () => {
      const proc = await initClient();
      const writes = [];
      proc.stdin.write = (chunk, enc, cb) => {
        writes.push(chunk);
        if (typeof enc === 'function') enc();
        else if (cb) cb();
        return true;
      };

      const promise = client._request('bad/method', {});
      await new Promise((r) => setTimeout(r, 10));
      const sent = JSON.parse(writes[0]);

      sendLine(proc, { id: sent.id, error: { code: -32601, message: 'Method not found' } });

      await expect(promise).rejects.toThrow('Method not found');
    });

    it('should ignore non-JSON lines', async () => {
      const proc = await initClient();
      proc.stdout.push('this is not json\n');
      proc.stdout.push('\n');
    });
  });

  describe('createThread', () => {
    it('should send thread/start and return threadId', async () => {
      const proc = createMockProc();
      const origSpawn = (await import('child_process')).spawn;
      origSpawn.mockReturnValueOnce(proc);
      interceptWrites(proc);
      await client.init();

      const writes = [];
      proc.stdin.write = (chunk, enc, cb) => {
        writes.push(chunk);
        if (typeof enc === 'function') enc();
        else if (cb) cb();
        // Auto-respond to thread/start
        const msg = JSON.parse(chunk);
        if (msg.method === 'thread/start') {
          sendLine(proc, { id: msg.id, result: { thread: { id: 'thread-abc' }, model: 'gpt-5.3-codex' } });
        }
        return true;
      };

      const result = await client.createThread('You are a test assistant.');
      expect(result.threadId).toBe('thread-abc');

      // Verify thread/start was sent with baseInstructions
      const threadStart = JSON.parse(writes[0]);
      expect(threadStart.method).toBe('thread/start');
      expect(threadStart.params.baseInstructions).toBe('You are a test assistant.');
      expect(threadStart.params.approvalPolicy).toBe('never');

      // No addConversationListener — only one request sent
      expect(writes).toHaveLength(1);
    });
  });

  describe('sendTurn', () => {
    async function initWithThread() {
      const proc = createMockProc();
      const origSpawn = (await import('child_process')).spawn;
      origSpawn.mockReturnValueOnce(proc);

      // Auto-respond to everything during setup
      proc.stdin.write = (chunk, enc, cb) => {
        const msg = JSON.parse(chunk);
        if (msg.method === 'initialize') {
          sendLine(proc, { id: msg.id, result: { userAgent: 'test' } });
        } else if (msg.method === 'thread/start') {
          sendLine(proc, { id: msg.id, result: { thread: { id: 'thread-turn' }, model: 'test' } });
        }
        if (typeof enc === 'function') enc();
        else if (cb) cb();
        return true;
      };

      await client.init();
      await client.createThread('test instructions');
      return proc;
    }

    it('should send turn/start and collect item/completed until turn/completed', async () => {
      const proc = await initWithThread();

      const writes = [];
      proc.stdin.write = (chunk, enc, cb) => {
        writes.push(chunk);
        if (typeof enc === 'function') enc();
        else if (cb) cb();
        const msg = JSON.parse(chunk);
        if (msg.method === 'turn/start') {
          sendLine(proc, { id: msg.id, result: {} });
        }
        return true;
      };

      const promise = client.sendTurn('thread-turn', 'List knowledge bases');
      await new Promise((r) => setTimeout(r, 10));

      const sent = JSON.parse(writes[0]);
      expect(sent.method).toBe('turn/start');
      expect(sent.params.threadId).toBe('thread-turn');
      expect(sent.params.input).toEqual([{ type: 'text', text: 'List knowledge bases' }]);

      // Simulate v0.106+ event stream
      sendLine(proc, { method: 'turn/started', params: { threadId: 'thread-turn' } });
      sendLine(proc, { method: 'item/completed', params: {
        threadId: 'thread-turn',
        item: { type: 'agentMessage', text: 'Found 4 knowledge bases:\n1. CS 6795\n2. English 1002' }
      }});
      sendLine(proc, { method: 'turn/completed', params: {
        threadId: 'thread-turn',
        turn: { status: 'completed', items: [] }
      }});

      const result = await promise;
      expect(result.text).toBe('Found 4 knowledge bases:\n1. CS 6795\n2. English 1002');
      expect(result.items).toHaveLength(1);
      // turn/started is not handled by any specific case, so it doesn't go to otherEvents
      expect(result.otherEvents).toEqual([]);
    });

    it('should track tool call events in otherEvents', async () => {
      const proc = await initWithThread();

      proc.stdin.write = (chunk, enc, cb) => {
        const msg = JSON.parse(chunk);
        if (msg.method === 'turn/start') {
          sendLine(proc, { id: msg.id, result: {} });
        }
        if (typeof enc === 'function') enc();
        else if (cb) cb();
        return true;
      };

      const promise = client.sendTurn('thread-turn', 'Open the syllabus');
      await new Promise((r) => setTimeout(r, 10));

      // Simulate a turn with tool calls (v0.106+ uses item/completed with typed items)
      sendLine(proc, { method: 'turn/started', params: { threadId: 'thread-turn' } });
      sendLine(proc, { method: 'item/completed', params: {
        threadId: 'thread-turn',
        item: { type: 'mcpToolCall', tool: 'browser_navigate' }
      }});
      sendLine(proc, { method: 'item/completed', params: {
        threadId: 'thread-turn',
        item: { type: 'mcpToolCall', tool: 'browser_snapshot' }
      }});
      sendLine(proc, { method: 'item/completed', params: {
        threadId: 'thread-turn',
        item: { type: 'agentMessage', text: 'I found the syllabus content.' }
      }});
      sendLine(proc, { method: 'turn/completed', params: {
        threadId: 'thread-turn',
        turn: { status: 'completed', items: [] }
      }});

      const result = await promise;
      expect(result.text).toBe('I found the syllabus content.');
      expect(result.otherEvents).toHaveLength(2);
      expect(result.otherEvents).toContain('mcpToolCall');
    });

    it('should return empty otherEvents when no tools are used', async () => {
      const proc = await initWithThread();

      proc.stdin.write = (chunk, enc, cb) => {
        const msg = JSON.parse(chunk);
        if (msg.method === 'turn/start') {
          sendLine(proc, { id: msg.id, result: {} });
        }
        if (typeof enc === 'function') enc();
        else if (cb) cb();
        return true;
      };

      const promise = client.sendTurn('thread-turn', 'Hello');
      await new Promise((r) => setTimeout(r, 10));

      // Only agent message + turn completed — no tool events
      sendLine(proc, { method: 'item/completed', params: {
        threadId: 'thread-turn',
        item: { type: 'agentMessage', text: 'Hello! How can I help?' }
      }});
      sendLine(proc, { method: 'turn/completed', params: {
        threadId: 'thread-turn',
        turn: { status: 'completed', items: [] }
      }});

      const result = await promise;
      expect(result.text).toBe('Hello! How can I help?');
      expect(result.otherEvents).toEqual([]);
    });

    it('should handle multiple agent messages by joining them', async () => {
      const proc = await initWithThread();

      proc.stdin.write = (chunk, enc, cb) => {
        const msg = JSON.parse(chunk);
        if (msg.method === 'turn/start') {
          sendLine(proc, { id: msg.id, result: {} });
        }
        if (typeof enc === 'function') enc();
        else if (cb) cb();
        return true;
      };

      const promise = client.sendTurn('thread-turn', 'test');
      await new Promise((r) => setTimeout(r, 10));

      sendLine(proc, { method: 'item/completed', params: {
        threadId: 'thread-turn',
        item: { type: 'agentMessage', text: 'First part' }
      }});
      sendLine(proc, { method: 'item/completed', params: {
        threadId: 'thread-turn',
        item: { type: 'agentMessage', text: 'Second part' }
      }});
      sendLine(proc, { method: 'turn/completed', params: {
        threadId: 'thread-turn',
        turn: { status: 'completed', items: [] }
      }});

      const result = await promise;
      expect(result.text).toBe('First part\n\nSecond part');
      expect(result.items).toHaveLength(2);
    });

    it('should reject on turn/completed with failed status', async () => {
      const proc = await initWithThread();

      proc.stdin.write = (chunk, enc, cb) => {
        const msg = JSON.parse(chunk);
        if (msg.method === 'turn/start') {
          sendLine(proc, { id: msg.id, result: {} });
        }
        if (typeof enc === 'function') enc();
        else if (cb) cb();
        return true;
      };

      const promise = client.sendTurn('thread-turn', 'fail');
      await new Promise((r) => setTimeout(r, 10));

      sendLine(proc, { method: 'turn/completed', params: {
        threadId: 'thread-turn',
        turn: { status: 'failed', error: { message: 'Model error occurred' } }
      }});

      await expect(promise).rejects.toThrow('Model error occurred');
    });

    it('should reject on general error notification', async () => {
      const proc = await initWithThread();

      proc.stdin.write = (chunk, enc, cb) => {
        const msg = JSON.parse(chunk);
        if (msg.method === 'turn/start') {
          sendLine(proc, { id: msg.id, result: {} });
        }
        if (typeof enc === 'function') enc();
        else if (cb) cb();
        return true;
      };

      const promise = client.sendTurn('thread-turn', 'fail');
      await new Promise((r) => setTimeout(r, 10));

      sendLine(proc, { method: 'error', params: {
        threadId: 'thread-turn',
        message: 'Stream error occurred'
      }});

      await expect(promise).rejects.toThrow('Stream error occurred');
    });

    it('should call onDelta callback with thinking, text, and status events', async () => {
      const proc = await initWithThread();

      proc.stdin.write = (chunk, enc, cb) => {
        const msg = JSON.parse(chunk);
        if (msg.method === 'turn/start') {
          sendLine(proc, { id: msg.id, result: {} });
        }
        if (typeof enc === 'function') enc();
        else if (cb) cb();
        return true;
      };

      const deltas = [];
      const onDelta = (d) => deltas.push(d);
      const promise = client.sendTurn('thread-turn', 'Open the syllabus', onDelta);
      await new Promise((r) => setTimeout(r, 10));

      // Simulate reasoning delta
      sendLine(proc, { method: 'item/reasoning/summaryTextDelta', params: {
        threadId: 'thread-turn', delta: 'Thinking about the syllabus...'
      }});
      // Simulate tool call begin
      sendLine(proc, { method: 'codex/event/mcp_tool_call_begin', params: {
        threadId: 'thread-turn', msg: { invocation: { tool: 'browser_navigate' } }
      }});
      // Simulate text delta
      sendLine(proc, { method: 'item/agentMessage/delta', params: {
        threadId: 'thread-turn', delta: 'Here is the syllabus'
      }});
      // Simulate tool completed
      sendLine(proc, { method: 'item/completed', params: {
        threadId: 'thread-turn',
        item: { type: 'mcpToolCall', tool: 'browser_navigate' }
      }});
      // Simulate agent message + turn completed
      sendLine(proc, { method: 'item/completed', params: {
        threadId: 'thread-turn',
        item: { type: 'agentMessage', text: 'Found it.' }
      }});
      sendLine(proc, { method: 'turn/completed', params: {
        threadId: 'thread-turn',
        turn: { status: 'completed', items: [] }
      }});

      const result = await promise;
      expect(result.text).toBe('Found it.');

      // Verify delta events were received
      // 3 events: thinking (reasoning), status (tool begin), text (message delta)
      expect(deltas.length).toBeGreaterThanOrEqual(3);
      expect(deltas.find(d => d.type === 'thinking')).toBeTruthy();
      expect(deltas.find(d => d.type === 'thinking').delta).toBe('Thinking about the syllabus...');
      expect(deltas.find(d => d.type === 'text')).toBeTruthy();
      expect(deltas.find(d => d.type === 'status')).toBeTruthy();
      expect(deltas.find(d => d.type === 'status').text).toContain('browser_navigate');
    });

    it('should track commandExecution items in otherEvents', async () => {
      const proc = await initWithThread();

      proc.stdin.write = (chunk, enc, cb) => {
        const msg = JSON.parse(chunk);
        if (msg.method === 'turn/start') {
          sendLine(proc, { id: msg.id, result: {} });
        }
        if (typeof enc === 'function') enc();
        else if (cb) cb();
        return true;
      };

      const promise = client.sendTurn('thread-turn', 'Run a command');
      await new Promise((r) => setTimeout(r, 10));

      sendLine(proc, { method: 'item/completed', params: {
        threadId: 'thread-turn',
        item: { type: 'commandExecution', command: 'ls -la' }
      }});
      sendLine(proc, { method: 'item/completed', params: {
        threadId: 'thread-turn',
        item: { type: 'agentMessage', text: 'Done running.' }
      }});
      sendLine(proc, { method: 'turn/completed', params: {
        threadId: 'thread-turn',
        turn: { status: 'completed', items: [] }
      }});

      const result = await promise;
      expect(result.text).toBe('Done running.');
      expect(result.otherEvents).toEqual(['commandExecution']);
    });
  });

  describe('crash and restart', () => {
    it('should reject pending requests on process crash', async () => {
      const proc = createMockProc();
      const origSpawn = (await import('child_process')).spawn;
      origSpawn.mockReturnValueOnce(proc);
      interceptWrites(proc);
      await client.init();

      const writes = [];
      proc.stdin.write = (chunk, enc, cb) => {
        writes.push(chunk);
        if (typeof enc === 'function') enc();
        else if (cb) cb();
        return true;
      };

      const promise = client._request('test/method', {});
      await new Promise((r) => setTimeout(r, 10));

      proc.killed = true;
      proc.emit('exit', 1, null);

      await expect(promise).rejects.toThrow('crashed');
    });

    it('should reject turn waiters on process crash', async () => {
      const proc = createMockProc();
      const origSpawn = (await import('child_process')).spawn;
      origSpawn.mockReturnValueOnce(proc);
      interceptWrites(proc);
      await client.init();

      // Set up a waiter manually
      const promise = new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('timeout')), 5000);
        client._turnWaiters.set('thread-crash', { resolve, reject, timer, items: [], otherEvents: [] });
      });

      proc.killed = true;
      proc.emit('exit', 1, null);

      await expect(promise).rejects.toThrow('crashed');
    });
  });

  describe('ensureReady', () => {
    it('should return true if already ready', async () => {
      const proc = createMockProc();
      const origSpawn = (await import('child_process')).spawn;
      origSpawn.mockReturnValueOnce(proc);
      interceptWrites(proc);
      await client.init();

      const result = await client.ensureReady();
      expect(result).toBe(true);
    });

    it('should re-spawn if process is not running', async () => {
      const proc = createMockProc();
      const origSpawn = (await import('child_process')).spawn;
      origSpawn.mockReturnValueOnce(proc);
      interceptWrites(proc);

      expect(client.isReady).toBe(false);
      const result = await client.ensureReady();
      expect(result).toBe(true);
    });

    it('should return false if shutdown was requested', async () => {
      const proc = createMockProc();
      const origSpawn = (await import('child_process')).spawn;
      origSpawn.mockReturnValueOnce(proc);
      interceptWrites(proc);
      await client.init();
      await client.shutdown();

      const result = await client.ensureReady();
      expect(result).toBe(false);
    });
  });
});
