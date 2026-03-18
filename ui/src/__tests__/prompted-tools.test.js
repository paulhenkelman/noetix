import { describe, it, expect } from 'vitest';
import { injectToolPrompt, parseToolCalls, validateToolCall } from '../server/llm/prompted-tools.js';

describe('prompted-tools', () => {
  const sampleTools = [
    {
      name: 'kb_search',
      description: 'Search the knowledge base',
      inputSchema: {
        type: 'object',
        properties: { query: { type: 'string' } },
        required: ['query'],
      },
    },
    {
      name: 'browser_navigate',
      description: 'Navigate to a URL',
      inputSchema: {
        type: 'object',
        properties: { url: { type: 'string' } },
      },
    },
  ];

  describe('injectToolPrompt', () => {
    it('should append tool definitions to system content', () => {
      const result = injectToolPrompt('You are helpful.', sampleTools);
      expect(result).toContain('You are helpful.');
      expect(result).toContain('kb_search');
      expect(result).toContain('browser_navigate');
      expect(result).toContain('<tool_call>');
      expect(result).toContain('Search the knowledge base');
    });

    it('should return original content when no tools', () => {
      expect(injectToolPrompt('Hello', [])).toBe('Hello');
      expect(injectToolPrompt('Hello', null)).toBe('Hello');
    });
  });

  describe('parseToolCalls', () => {
    it('should extract valid tool calls', () => {
      const text = `Let me search for that.
<tool_call>{"name": "kb_search", "arguments": {"query": "photosynthesis"}}</tool_call>
I found the answer.`;

      const calls = parseToolCalls(text);
      expect(calls).toHaveLength(1);
      expect(calls[0].name).toBe('kb_search');
      expect(calls[0].arguments).toEqual({ query: 'photosynthesis' });
    });

    it('should extract multiple tool calls', () => {
      const text = `<tool_call>{"name": "tool_a", "arguments": {}}</tool_call>
Then also:
<tool_call>{"name": "tool_b", "arguments": {"x": 1}}</tool_call>`;

      const calls = parseToolCalls(text);
      expect(calls).toHaveLength(2);
      expect(calls[0].name).toBe('tool_a');
      expect(calls[1].name).toBe('tool_b');
    });

    it('should handle malformed JSON gracefully', () => {
      const text = `<tool_call>not json at all</tool_call>`;
      const calls = parseToolCalls(text);
      expect(calls).toHaveLength(0);
    });

    it('should handle missing tool_call blocks', () => {
      const text = 'No tool calls here.';
      const calls = parseToolCalls(text);
      expect(calls).toHaveLength(0);
    });

    it('should skip objects without name field', () => {
      const text = `<tool_call>{"arguments": {"x": 1}}</tool_call>`;
      const calls = parseToolCalls(text);
      expect(calls).toHaveLength(0);
    });

    it('should default arguments to empty object', () => {
      const text = `<tool_call>{"name": "simple_tool"}</tool_call>`;
      const calls = parseToolCalls(text);
      expect(calls).toHaveLength(1);
      expect(calls[0].arguments).toEqual({});
    });
  });

  describe('validateToolCall', () => {
    it('should return true for known tools', () => {
      expect(validateToolCall({ name: 'kb_search' }, sampleTools)).toBe(true);
      expect(validateToolCall({ name: 'browser_navigate' }, sampleTools)).toBe(true);
    });

    it('should return false for unknown tools', () => {
      expect(validateToolCall({ name: 'nonexistent' }, sampleTools)).toBe(false);
    });
  });
});
