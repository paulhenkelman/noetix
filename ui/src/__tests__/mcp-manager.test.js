import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { EventEmitter } from 'events';
import { Readable, Writable } from 'stream';

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
    pid: Math.floor(Math.random() * 100000),
  });
  mockProcs.push(proc);
  return proc;
}

const mockSpawn = vi.fn(() => createMockProc());

vi.mock('child_process', () => ({
  spawn: (...args) => mockSpawn(...args),
  createInterface: vi.fn(),
}));

const { MCPManager } = await import('../server/mcp-manager.js');

function sendLine(proc, obj) {
  proc.stdout.push(JSON.stringify(obj) + '\n');
}

function interceptWrites(proc, tools = []) {
  const writes = [];
  proc.stdin.write = (chunk, enc, cb) => {
    writes.push(chunk);
    if (typeof enc === 'function') enc();
    else if (cb) cb();

    let msg;
    try { msg = JSON.parse(chunk); } catch { return true; }

    if (msg.method === 'initialize') {
      sendLine(proc, {
        id: msg.id,
        result: {
          protocolVersion: '2024-11-05',
          capabilities: { tools: { listChanged: false } },
          serverInfo: { name: 'test-server', version: '1.0.0' },
        },
      });
    } else if (msg.method === 'tools/list') {
      sendLine(proc, {
        id: msg.id,
        result: { tools },
      });
    }
    return true;
  };
  return writes;
}

describe('MCPManager', () => {
  let manager;

  beforeEach(() => {
    mockProcs.length = 0;
    mockSpawn.mockClear();
  });

  afterEach(async () => {
    if (manager) await manager.shutdown();
    for (const proc of mockProcs) {
      if (!proc.killed) proc.kill();
    }
  });

  describe('init', () => {
    it('should spawn processes, initialize, and list tools', async () => {
      const kbTools = [
        { name: 'kb_search', description: 'Search KB', inputSchema: { type: 'object' } },
        { name: 'kb_list', description: 'List KBs', inputSchema: { type: 'object' } },
      ];
      const contentTools = [
        { name: 'content_upload', description: 'Upload content', inputSchema: { type: 'object' } },
      ];

      const proc1 = createMockProc();
      const proc2 = createMockProc();
      mockSpawn.mockReturnValueOnce(proc1).mockReturnValueOnce(proc2);
      interceptWrites(proc1, kbTools);
      interceptWrites(proc2, contentTools);

      manager = new MCPManager({
        kb: { command: 'node', args: ['kb.js'], env: {}, startupTimeout: 5000, toolTimeout: 10000 },
        content: { command: 'node', args: ['content.js'], env: {}, startupTimeout: 5000, toolTimeout: 10000 },
      });

      await manager.init();

      expect(manager.isReady).toBe(true);
      const tools = manager.getTools();
      expect(tools).toHaveLength(3);
      expect(tools.map((t) => t.name)).toContain('kb_search');
      expect(tools.map((t) => t.name)).toContain('kb_list');
      expect(tools.map((t) => t.name)).toContain('content_upload');
    });
  });

  describe('getTools', () => {
    it('should return aggregated tools from all servers', async () => {
      const proc1 = createMockProc();
      const proc2 = createMockProc();
      mockSpawn.mockReturnValueOnce(proc1).mockReturnValueOnce(proc2);
      interceptWrites(proc1, [{ name: 'tool_a', description: 'A', inputSchema: {} }]);
      interceptWrites(proc2, [{ name: 'tool_b', description: 'B', inputSchema: {} }]);

      manager = new MCPManager({
        server1: { command: 'cmd1', args: [], env: {}, startupTimeout: 5000, toolTimeout: 10000 },
        server2: { command: 'cmd2', args: [], env: {}, startupTimeout: 5000, toolTimeout: 10000 },
      });

      await manager.init();

      const tools = manager.getTools();
      expect(tools).toHaveLength(2);
      expect(tools[0].name).toBe('tool_a');
      expect(tools[1].name).toBe('tool_b');
    });
  });

  describe('callTool', () => {
    it('should route to correct server and return result', async () => {
      const proc1 = createMockProc();
      mockSpawn.mockReturnValueOnce(proc1);
      interceptWrites(proc1, [{ name: 'kb_search', description: 'Search', inputSchema: {} }]);

      manager = new MCPManager({
        kb: { command: 'node', args: [], env: {}, startupTimeout: 5000, toolTimeout: 10000 },
      });

      await manager.init();

      // Override stdin.write to capture and respond to tool call
      const writes = [];
      proc1.stdin.write = (chunk, enc, cb) => {
        writes.push(chunk);
        if (typeof enc === 'function') enc();
        else if (cb) cb();

        const msg = JSON.parse(chunk);
        if (msg.method === 'tools/call') {
          sendLine(proc1, {
            id: msg.id,
            result: {
              content: [{ type: 'text', text: 'Found 3 results' }],
            },
          });
        }
        return true;
      };

      const result = await manager.callTool('kb_search', { query: 'test' });
      expect(result.content[0].text).toBe('Found 3 results');

      const sent = JSON.parse(writes[0]);
      expect(sent.method).toBe('tools/call');
      expect(sent.params.name).toBe('kb_search');
      expect(sent.params.arguments).toEqual({ query: 'test' });
    });

    it('should throw on unknown tool name', async () => {
      const proc1 = createMockProc();
      mockSpawn.mockReturnValueOnce(proc1);
      interceptWrites(proc1, [{ name: 'kb_search', description: 'Search', inputSchema: {} }]);

      manager = new MCPManager({
        kb: { command: 'node', args: [], env: {}, startupTimeout: 5000, toolTimeout: 10000 },
      });

      await manager.init();

      await expect(manager.callTool('nonexistent_tool', {})).rejects.toThrow('Unknown tool');
    });
  });

  describe('shutdown', () => {
    it('should kill all processes', async () => {
      const proc1 = createMockProc();
      const proc2 = createMockProc();
      mockSpawn.mockReturnValueOnce(proc1).mockReturnValueOnce(proc2);
      interceptWrites(proc1, []);
      interceptWrites(proc2, []);

      manager = new MCPManager({
        s1: { command: 'cmd1', args: [], env: {}, startupTimeout: 5000, toolTimeout: 10000 },
        s2: { command: 'cmd2', args: [], env: {}, startupTimeout: 5000, toolTimeout: 10000 },
      });

      await manager.init();
      expect(manager.isReady).toBe(true);

      await manager.shutdown();
      expect(manager.isReady).toBe(false);
      expect(proc1.killed).toBe(true);
      expect(proc2.killed).toBe(true);
    });
  });
});
