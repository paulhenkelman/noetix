import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { DeltaType, StopReason } from '../server/llm/types.js';

// Mock the LLM provider factory
vi.mock('../server/llm/index.js', () => ({
  createProvider: vi.fn(),
}));

// Mock MCPManager as a class
let mockMCPInstance;
vi.mock('../server/mcp-manager.js', () => ({
  MCPManager: class MockMCPManager {
    constructor() {
      Object.assign(this, mockMCPInstance);
    }
  },
}));

const { createProvider } = await import('../server/llm/index.js');
const { AgentRunner } = await import('../server/agent-runner.js');

function setupMocks(chatSequences = []) {
  let callIndex = 0;
  const mockProvider = {
    async *chat(messages, tools, options) {
      const seq = chatSequences[callIndex++] || [
        { type: DeltaType.TEXT, delta: 'Default response' },
        { type: DeltaType.DONE, stopReason: StopReason.END_TURN },
      ];
      for (const delta of seq) {
        yield delta;
      }
    },
  };

  createProvider.mockResolvedValue(mockProvider);

  mockMCPInstance = {
    init: vi.fn().mockResolvedValue(undefined),
    getTools: vi.fn().mockReturnValue([
      { name: 'kb_search', description: 'Search', inputSchema: {} },
    ]),
    callTool: vi.fn().mockResolvedValue({
      content: [{ type: 'text', text: 'Tool result' }],
    }),
    isReady: true,
    shutdown: vi.fn().mockResolvedValue(undefined),
  };

  return { mockProvider, mockMCPInstance };
}

describe('AgentRunner', () => {
  let runner;

  beforeEach(() => {
    createProvider.mockReset();
  });

  afterEach(async () => {
    if (runner) await runner.shutdown();
  });

  describe('init', () => {
    it('should create provider and MCPManager', async () => {
      setupMocks();
      runner = new AgentRunner();
      await runner.init();

      expect(createProvider).toHaveBeenCalled();
      expect(runner.isReady).toBe(true);
    });
  });

  describe('isReady', () => {
    it('should be false before init', () => {
      setupMocks();
      runner = new AgentRunner();
      expect(runner.isReady).toBe(false);
    });

    it('should be true after init', async () => {
      setupMocks();
      runner = new AgentRunner();
      await runner.init();
      expect(runner.isReady).toBe(true);
    });

    it('should be false after shutdown', async () => {
      setupMocks();
      runner = new AgentRunner();
      await runner.init();
      await runner.shutdown();
      expect(runner.isReady).toBe(false);
    });
  });

  describe('createThread', () => {
    it('should return a threadId', async () => {
      setupMocks();
      runner = new AgentRunner();
      await runner.init();

      const result = await runner.createThread('You are a test assistant.');
      expect(result.threadId).toBeTruthy();
      expect(typeof result.threadId).toBe('string');
    });

    it('should store system instructions in thread messages', async () => {
      setupMocks();
      runner = new AgentRunner();
      await runner.init();

      const { threadId } = await runner.createThread('System instructions here');
      expect(runner._threads.get(threadId).messages[0]).toEqual({
        role: 'system',
        content: 'System instructions here',
      });
    });
  });

  describe('sendTurn', () => {
    it('should return text, items, otherEvents, usage', async () => {
      setupMocks([
        [
          { type: DeltaType.TEXT, delta: 'Here is your answer.' },
          { type: DeltaType.DONE, stopReason: StopReason.END_TURN },
        ],
      ]);
      runner = new AgentRunner();
      await runner.init();

      const { threadId } = await runner.createThread('Instructions');
      const result = await runner.sendTurn(threadId, 'What is 2+2?');

      expect(result.text).toBe('Here is your answer.');
      expect(result.items).toEqual(['Here is your answer.']);
      expect(result.otherEvents).toEqual([]);
      expect(result.usage).toBeNull();
    });

    it('should populate otherEvents from tool calls', async () => {
      setupMocks([
        [
          { type: DeltaType.TOOL_CALL, id: 'tc1', name: 'kb_search', arguments: { query: 'x' } },
          { type: DeltaType.DONE, stopReason: StopReason.TOOL_USE },
        ],
        [
          { type: DeltaType.TEXT, delta: 'Found it.' },
          { type: DeltaType.DONE, stopReason: StopReason.END_TURN },
        ],
      ]);
      runner = new AgentRunner();
      await runner.init();

      const { threadId } = await runner.createThread('Instructions');
      const result = await runner.sendTurn(threadId, 'Search for X');

      expect(result.otherEvents).toHaveLength(1);
      expect(result.otherEvents[0]).toBe('mcpToolCall:kb_search');
    });

    it('should call onDelta with text and thinking events', async () => {
      setupMocks([
        [
          { type: DeltaType.THINKING, delta: 'Let me think...' },
          { type: DeltaType.TEXT, delta: 'Answer: 4' },
          { type: DeltaType.DONE, stopReason: StopReason.END_TURN },
        ],
      ]);
      runner = new AgentRunner();
      await runner.init();

      const { threadId } = await runner.createThread('Instructions');
      const deltas = [];
      await runner.sendTurn(threadId, 'What is 2+2?', (d) => deltas.push(d));

      expect(deltas.find((d) => d.type === 'thinking')).toBeTruthy();
      expect(deltas.find((d) => d.type === 'text')).toBeTruthy();
    });

    it('should throw if thread not found', async () => {
      setupMocks();
      runner = new AgentRunner();
      await runner.init();

      await expect(runner.sendTurn('nonexistent', 'Hello')).rejects.toThrow('Thread nonexistent not found');
    });
  });

  describe('ensureReady', () => {
    it('should return true if already ready', async () => {
      setupMocks();
      runner = new AgentRunner();
      await runner.init();

      const result = await runner.ensureReady();
      expect(result).toBe(true);
    });

    it('should re-init if not ready', async () => {
      setupMocks();
      runner = new AgentRunner();
      expect(runner.isReady).toBe(false);

      const result = await runner.ensureReady();
      expect(result).toBe(true);
    });

    it('should return false after shutdown', async () => {
      setupMocks();
      runner = new AgentRunner();
      await runner.init();
      await runner.shutdown();

      const result = await runner.ensureReady();
      expect(result).toBe(false);
    });
  });

  describe('shutdown', () => {
    it('should shut down MCP manager and clear threads', async () => {
      const { mockMCPInstance: mcpInstance } = setupMocks();
      runner = new AgentRunner();
      await runner.init();

      await runner.createThread('test');
      expect(runner._threads.size).toBe(1);

      await runner.shutdown();
      expect(mcpInstance.shutdown).toHaveBeenCalled();
      expect(runner._threads.size).toBe(0);
      expect(runner.isReady).toBe(false);
    });
  });
});
