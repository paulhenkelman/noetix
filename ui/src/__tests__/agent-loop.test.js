import { describe, it, expect, vi } from 'vitest';
import { AgentLoop } from '../server/agent-loop.js';
import { DeltaType, StopReason } from '../server/llm/types.js';

/**
 * Create a mock LLM provider that yields predetermined sequences.
 * Each call to chat() pops the next sequence from the array.
 */
function createMockProvider(sequences) {
  let callIndex = 0;
  return {
    async *chat(messages, tools, options) {
      const seq = sequences[callIndex++] || [];
      for (const delta of seq) {
        yield delta;
      }
    },
  };
}

/**
 * Create a mock MCPManager with canned tool results.
 */
function createMockMCPManager(toolResults = {}, tools = []) {
  return {
    getTools: () => tools,
    callTool: async (name, args) => {
      if (toolResults[name]) {
        return typeof toolResults[name] === 'function'
          ? toolResults[name](args)
          : toolResults[name];
      }
      return { content: [{ type: 'text', text: `Result for ${name}` }] };
    },
    isReady: true,
  };
}

describe('AgentLoop', () => {
  describe('single turn (no tools)', () => {
    it('should return text directly when no tool calls', async () => {
      const provider = createMockProvider([
        [
          { type: DeltaType.TEXT, delta: 'Hello, ' },
          { type: DeltaType.TEXT, delta: 'world!' },
          { type: DeltaType.DONE, stopReason: StopReason.END_TURN },
        ],
      ]);

      const mcpManager = createMockMCPManager();
      const loop = new AgentLoop({ provider, mcpManager });
      const result = await loop.run([{ role: 'user', content: 'Hi' }], null);

      expect(result.text).toBe('Hello, world!');
      expect(result.toolCalls).toEqual([]);
    });
  });

  describe('tool calling loop', () => {
    it('should call tools and loop back to LLM', async () => {
      const provider = createMockProvider([
        // First call: LLM requests a tool call
        [
          { type: DeltaType.TOOL_CALL, id: 'tc1', name: 'kb_search', arguments: { query: 'test' } },
          { type: DeltaType.DONE, stopReason: StopReason.TOOL_USE },
        ],
        // Second call: LLM returns final text
        [
          { type: DeltaType.TEXT, delta: 'Found 3 results.' },
          { type: DeltaType.DONE, stopReason: StopReason.END_TURN },
        ],
      ]);

      const mcpManager = createMockMCPManager({
        kb_search: { content: [{ type: 'text', text: 'Result 1, Result 2, Result 3' }] },
      }, [{ name: 'kb_search', description: 'Search', inputSchema: {} }]);

      const loop = new AgentLoop({ provider, mcpManager });
      const deltas = [];
      const result = await loop.run(
        [{ role: 'user', content: 'Search for test' }],
        (d) => deltas.push(d),
      );

      expect(result.text).toBe('Found 3 results.');
      expect(result.toolCalls).toContain('kb_search');
      expect(deltas.some((d) => d.type === 'status' && d.text.includes('kb_search'))).toBe(true);
    });
  });

  describe('parallel tool calls', () => {
    it('should execute multiple tool calls via Promise.all', async () => {
      const callOrder = [];

      const provider = createMockProvider([
        [
          { type: DeltaType.TOOL_CALL, id: 'tc1', name: 'tool_a', arguments: {} },
          { type: DeltaType.TOOL_CALL, id: 'tc2', name: 'tool_b', arguments: {} },
          { type: DeltaType.DONE, stopReason: StopReason.TOOL_USE },
        ],
        [
          { type: DeltaType.TEXT, delta: 'Both done.' },
          { type: DeltaType.DONE, stopReason: StopReason.END_TURN },
        ],
      ]);

      const mcpManager = createMockMCPManager({
        tool_a: async () => { callOrder.push('a'); return { content: [{ type: 'text', text: 'A result' }] }; },
        tool_b: async () => { callOrder.push('b'); return { content: [{ type: 'text', text: 'B result' }] }; },
      }, [
        { name: 'tool_a', description: 'A', inputSchema: {} },
        { name: 'tool_b', description: 'B', inputSchema: {} },
      ]);

      const loop = new AgentLoop({ provider, mcpManager });
      const result = await loop.run([{ role: 'user', content: 'Do both' }], null);

      expect(result.text).toBe('Both done.');
      expect(result.toolCalls).toEqual(['tool_a', 'tool_b']);
      expect(callOrder).toContain('a');
      expect(callOrder).toContain('b');
    });
  });

  describe('max iterations', () => {
    it('should stop after max iterations', async () => {
      // Provider always requests a tool call
      const sequences = [];
      for (let i = 0; i < 5; i++) {
        sequences.push([
          { type: DeltaType.TEXT, delta: `Iteration ${i}` },
          { type: DeltaType.TOOL_CALL, id: `tc${i}`, name: 'infinite_tool', arguments: {} },
          { type: DeltaType.DONE, stopReason: StopReason.TOOL_USE },
        ]);
      }
      const provider = createMockProvider(sequences);

      const mcpManager = createMockMCPManager({
        infinite_tool: { content: [{ type: 'text', text: 'keep going' }] },
      }, [{ name: 'infinite_tool', description: 'Loop', inputSchema: {} }]);

      const loop = new AgentLoop({ provider, mcpManager, maxIterations: 3 });
      const result = await loop.run([{ role: 'user', content: 'Loop forever' }], null);

      expect(result.toolCalls).toHaveLength(3);
    });
  });

  describe('onDelta callback', () => {
    it('should receive text, thinking, and status events', async () => {
      const provider = createMockProvider([
        [
          { type: DeltaType.THINKING, delta: 'Hmm, let me think...' },
          { type: DeltaType.TEXT, delta: 'Here is the answer.' },
          { type: DeltaType.DONE, stopReason: StopReason.END_TURN },
        ],
      ]);

      const mcpManager = createMockMCPManager();
      const loop = new AgentLoop({ provider, mcpManager });
      const deltas = [];
      await loop.run([{ role: 'user', content: 'Think' }], (d) => deltas.push(d));

      expect(deltas.find((d) => d.type === 'thinking')).toBeTruthy();
      expect(deltas.find((d) => d.type === 'thinking').delta).toBe('Hmm, let me think...');
      expect(deltas.find((d) => d.type === 'text')).toBeTruthy();
    });

    it('should emit status events for tool calls', async () => {
      const provider = createMockProvider([
        [
          { type: DeltaType.TOOL_CALL, id: 'tc1', name: 'browser_navigate', arguments: { url: 'http://example.com' } },
          { type: DeltaType.DONE, stopReason: StopReason.TOOL_USE },
        ],
        [
          { type: DeltaType.TEXT, delta: 'Done' },
          { type: DeltaType.DONE, stopReason: StopReason.END_TURN },
        ],
      ]);

      const mcpManager = createMockMCPManager({}, [{ name: 'browser_navigate', description: 'Nav', inputSchema: {} }]);
      const loop = new AgentLoop({ provider, mcpManager });
      const deltas = [];
      await loop.run([{ role: 'user', content: 'Navigate' }], (d) => deltas.push(d));

      const statusEvents = deltas.filter((d) => d.type === 'status');
      expect(statusEvents.some((d) => d.text.includes('Calling browser_navigate'))).toBe(true);
      expect(statusEvents.some((d) => d.text.includes('Completed browser_navigate'))).toBe(true);
    });
  });

  describe('tool error handling', () => {
    it('should pass tool errors as result messages', async () => {
      const provider = createMockProvider([
        [
          { type: DeltaType.TOOL_CALL, id: 'tc1', name: 'failing_tool', arguments: {} },
          { type: DeltaType.DONE, stopReason: StopReason.TOOL_USE },
        ],
        [
          { type: DeltaType.TEXT, delta: 'Tool failed, but I recovered.' },
          { type: DeltaType.DONE, stopReason: StopReason.END_TURN },
        ],
      ]);

      const mcpManager = {
        getTools: () => [{ name: 'failing_tool', description: 'Fails', inputSchema: {} }],
        callTool: async () => { throw new Error('Connection refused'); },
        isReady: true,
      };

      const loop = new AgentLoop({ provider, mcpManager });
      const result = await loop.run([{ role: 'user', content: 'Try tool' }], null);

      expect(result.text).toBe('Tool failed, but I recovered.');
      expect(result.toolCalls).toContain('failing_tool');
    });
  });
});
