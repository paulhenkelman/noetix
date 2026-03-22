import { describe, it, expect, vi } from 'vitest';
import { DeltaType, StopReason, mapEffort } from '../server/llm/types.js';

describe('LLM types', () => {
  describe('mapEffort', () => {
    it('should map openai effort', () => {
      expect(mapEffort('openai', 'high')).toEqual({ reasoning_effort: 'high' });
      expect(mapEffort('openai', 'xhigh')).toEqual({ reasoning_effort: 'high' });
      expect(mapEffort('openai', 'medium')).toEqual({ reasoning_effort: 'medium' });
    });

    it('should map anthropic effort', () => {
      const result = mapEffort('anthropic', 'high');
      expect(result.thinking.type).toBe('enabled');
      expect(result.thinking.budget_tokens).toBe(10000);
    });

    it('should map anthropic xhigh effort', () => {
      const result = mapEffort('anthropic', 'xhigh');
      expect(result.thinking.budget_tokens).toBe(32000);
    });

    it('should return empty for ollama', () => {
      expect(mapEffort('ollama', 'high')).toEqual({});
    });

    it('should return empty for null effort', () => {
      expect(mapEffort('openai', null)).toEqual({});
    });

    it('should map vllm same as openai', () => {
      expect(mapEffort('vllm', 'medium')).toEqual({ reasoning_effort: 'medium' });
    });
  });
});

describe('OpenAI provider', () => {
  it('should format tools correctly', async () => {
    const { OpenAIProvider } = await import('../server/llm/openai.js');
    const provider = new OpenAIProvider({
      llmModel: 'gpt-5.3',
      llmApiKey: 'test',
      llmMaxTokens: 1024,
      llmTemperature: 0,
      llmReasoningEffort: 'high',
    });

    const tools = [
      { name: 'kb_search', description: 'Search KB', inputSchema: { type: 'object', properties: { query: { type: 'string' } } } },
    ];
    const formatted = provider._formatTools(tools);
    expect(formatted).toHaveLength(1);
    expect(formatted[0].type).toBe('function');
    expect(formatted[0].function.name).toBe('kb_search');
    expect(formatted[0].function.parameters).toEqual(tools[0].inputSchema);
  });

  it('should return undefined for empty tools', async () => {
    const { OpenAIProvider } = await import('../server/llm/openai.js');
    const provider = new OpenAIProvider({ llmModel: 'test', llmApiKey: 'test', llmMaxTokens: 1024, llmTemperature: 0, llmReasoningEffort: 'high' });
    expect(provider._formatTools([])).toBeUndefined();
    expect(provider._formatTools(null)).toBeUndefined();
  });

  it('should pass messages through unchanged', async () => {
    const { OpenAIProvider } = await import('../server/llm/openai.js');
    const provider = new OpenAIProvider({ llmModel: 'test', llmApiKey: 'test', llmMaxTokens: 1024, llmTemperature: 0, llmReasoningEffort: 'high' });
    const messages = [
      { role: 'system', content: 'You are helpful.' },
      { role: 'user', content: 'Hello' },
    ];
    expect(provider._formatMessages(messages)).toBe(messages);
  });

  it('should store authToken from config', async () => {
    const { OpenAIProvider } = await import('../server/llm/openai.js');
    const provider = new OpenAIProvider({
      llmModel: 'gpt-5.3',
      llmAuthToken: 'oauth-token-123',
      llmMaxTokens: 1024,
      llmTemperature: 0,
      llmReasoningEffort: 'high',
    });
    expect(provider.authToken).toBe('oauth-token-123');
    expect(provider.apiKey).toBeUndefined();
  });

  it('should store tokenGetter from config', async () => {
    const { OpenAIProvider } = await import('../server/llm/openai.js');
    const getter = async () => 'fresh-token';
    const provider = new OpenAIProvider({
      llmModel: 'gpt-5.3',
      llmTokenGetter: getter,
      llmMaxTokens: 1024,
      llmTemperature: 0,
      llmReasoningEffort: 'high',
    });
    expect(provider.tokenGetter).toBe(getter);
  });
});

describe('Anthropic provider', () => {
  it('should extract system messages', async () => {
    const { AnthropicProvider } = await import('../server/llm/anthropic.js');
    const provider = new AnthropicProvider({ llmModel: 'claude-3', llmApiKey: 'test', llmMaxTokens: 1024, llmTemperature: 0, llmReasoningEffort: 'high' });

    const messages = [
      { role: 'system', content: 'You are helpful.' },
      { role: 'user', content: 'Hello' },
      { role: 'assistant', content: 'Hi there' },
    ];

    const { system, messages: formatted } = provider._formatMessages(messages);
    expect(system).toBe('You are helpful.');
    expect(formatted).toHaveLength(2);
    expect(formatted[0].role).toBe('user');
    expect(formatted[1].role).toBe('assistant');
  });

  it('should convert tool results to user tool_result blocks', async () => {
    const { AnthropicProvider } = await import('../server/llm/anthropic.js');
    const provider = new AnthropicProvider({ llmModel: 'claude-3', llmApiKey: 'test', llmMaxTokens: 1024, llmTemperature: 0, llmReasoningEffort: 'high' });

    const messages = [
      { role: 'user', content: 'Search for X' },
      {
        role: 'assistant',
        content: '',
        tool_calls: [{ id: 'tc1', type: 'function', function: { name: 'kb_search', arguments: '{"query":"X"}' } }],
      },
      { role: 'tool', tool_call_id: 'tc1', content: 'Found results' },
    ];

    const { messages: formatted } = provider._formatMessages(messages);
    expect(formatted).toHaveLength(3);

    // Assistant with tool_use
    expect(formatted[1].role).toBe('assistant');
    expect(formatted[1].content[0].type).toBe('tool_use');
    expect(formatted[1].content[0].name).toBe('kb_search');

    // Tool result as user message
    expect(formatted[2].role).toBe('user');
    expect(formatted[2].content[0].type).toBe('tool_result');
    expect(formatted[2].content[0].tool_use_id).toBe('tc1');
  });

  it('should merge adjacent same-role messages', async () => {
    const { AnthropicProvider } = await import('../server/llm/anthropic.js');
    const provider = new AnthropicProvider({ llmModel: 'claude-3', llmApiKey: 'test', llmMaxTokens: 1024, llmTemperature: 0, llmReasoningEffort: 'high' });

    const messages = [
      { role: 'user', content: 'Hello' },
      { role: 'user', content: 'World' },
      { role: 'assistant', content: 'Hi' },
    ];

    const { messages: formatted } = provider._formatMessages(messages);
    expect(formatted).toHaveLength(2);
    expect(formatted[0].content).toBe('Hello\n\nWorld');
    expect(formatted[1].content).toBe('Hi');
  });

  it('should format tools with input_schema', async () => {
    const { AnthropicProvider } = await import('../server/llm/anthropic.js');
    const provider = new AnthropicProvider({ llmModel: 'claude-3', llmApiKey: 'test', llmMaxTokens: 1024, llmTemperature: 0, llmReasoningEffort: 'high' });

    const tools = [
      { name: 'kb_search', description: 'Search', inputSchema: { type: 'object', properties: { q: { type: 'string' } } } },
    ];
    const formatted = provider._formatTools(tools);
    expect(formatted[0].input_schema).toEqual(tools[0].inputSchema);
    expect(formatted[0].name).toBe('kb_search');
  });

  it('should store authToken from config', async () => {
    const { AnthropicProvider } = await import('../server/llm/anthropic.js');
    const provider = new AnthropicProvider({
      llmModel: 'claude-sonnet-4-6-20250514',
      llmAuthToken: 'sk-ant-oat01-test',
      llmMaxTokens: 1024,
      llmTemperature: 0,
      llmReasoningEffort: 'high',
    });
    expect(provider.authToken).toBe('sk-ant-oat01-test');
    expect(provider.apiKey).toBeUndefined();
  });

  it('should store tokenGetter from config', async () => {
    const { AnthropicProvider } = await import('../server/llm/anthropic.js');
    const getter = async () => 'refreshed-token';
    const provider = new AnthropicProvider({
      llmModel: 'claude-sonnet-4-6-20250514',
      llmTokenGetter: getter,
      llmMaxTokens: 1024,
      llmTemperature: 0,
      llmReasoningEffort: 'high',
    });
    expect(provider.tokenGetter).toBe(getter);
  });
});

describe('Ollama provider', () => {
  it('should format messages in OpenAI-compatible format', async () => {
    const { OllamaProvider } = await import('../server/llm/ollama.js');
    const provider = new OllamaProvider({ llmModel: 'llama3', llmBaseUrl: 'http://localhost:11434', llmMaxTokens: 1024, llmTemperature: 0 });

    const messages = [
      { role: 'system', content: 'You are helpful.' },
      { role: 'user', content: 'Hello' },
    ];

    const formatted = provider._formatMessages(messages);
    expect(formatted).toHaveLength(2);
    expect(formatted[0]).toEqual({ role: 'system', content: 'You are helpful.' });
    expect(formatted[1]).toEqual({ role: 'user', content: 'Hello' });
  });

  it('should format tools same as OpenAI', async () => {
    const { OllamaProvider } = await import('../server/llm/ollama.js');
    const provider = new OllamaProvider({ llmModel: 'llama3', llmBaseUrl: 'http://localhost:11434', llmMaxTokens: 1024, llmTemperature: 0 });

    const tools = [
      { name: 'test_tool', description: 'Test', inputSchema: { type: 'object' } },
    ];
    const formatted = provider._formatTools(tools);
    expect(formatted[0].type).toBe('function');
    expect(formatted[0].function.name).toBe('test_tool');
  });
});

describe('vLLM provider', () => {
  it('should extend OpenAI with custom baseUrl', async () => {
    const { VLLMProvider } = await import('../server/llm/vllm.js');
    const provider = new VLLMProvider({ llmModel: 'llama-70b', llmMaxTokens: 1024, llmTemperature: 0, llmReasoningEffort: 'high' });
    expect(provider.baseUrl).toBe('http://localhost:8000/v1');
    expect(provider.apiKey).toBe('dummy');
    expect(provider.model).toBe('llama-70b');
  });
});

describe('LLM factory', () => {
  it('should throw on unknown provider', async () => {
    const { createProvider } = await import('../server/llm/index.js');
    await expect(createProvider({ llmProvider: 'unknown' })).rejects.toThrow('Unknown LLM provider');
  });
});
