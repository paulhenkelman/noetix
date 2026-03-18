/**
 * Ollama provider adapter.
 *
 * No SDK needed — raw fetch to the Ollama HTTP API.
 */

import { DeltaType, StopReason } from './types.js';

export class OllamaProvider {
  constructor(config) {
    this.model = config.llmModel;
    this.baseUrl = config.llmBaseUrl || 'http://localhost:11434';
    this.maxTokens = config.llmMaxTokens || 16384;
    this.temperature = config.llmTemperature ?? 0.0;
  }

  _formatMessages(messages) {
    // Ollama uses OpenAI-compatible message format
    return messages.map((msg) => {
      if (msg.role === 'tool') {
        return {
          role: 'tool',
          content: msg.content,
        };
      }
      if (msg.role === 'assistant' && msg.tool_calls) {
        return {
          role: 'assistant',
          content: msg.content || '',
          tool_calls: msg.tool_calls.map((tc) => ({
            function: {
              name: tc.function.name,
              arguments: typeof tc.function.arguments === 'string'
                ? JSON.parse(tc.function.arguments || '{}')
                : (tc.function.arguments || {}),
            },
          })),
        };
      }
      return { role: msg.role, content: msg.content };
    });
  }

  _formatTools(tools) {
    if (!tools || tools.length === 0) return undefined;
    return tools.map((t) => ({
      type: 'function',
      function: {
        name: t.name,
        description: t.description || '',
        parameters: t.inputSchema || {},
      },
    }));
  }

  async *chat(messages, tools, options = {}) {
    const url = `${this.baseUrl.replace(/\/$/, '')}/api/chat`;

    const body = {
      model: this.model,
      messages: this._formatMessages(messages),
      stream: true,
      options: {
        num_predict: this.maxTokens,
        temperature: this.temperature,
      },
    };

    const formattedTools = this._formatTools(tools);
    if (formattedTools) body.tools = formattedTools;

    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: options.signal,
    });

    if (!response.ok) {
      const text = await response.text();
      throw new Error(`Ollama API error ${response.status}: ${text}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;

        let chunk;
        try {
          chunk = JSON.parse(trimmed);
        } catch {
          continue;
        }

        if (chunk.message?.thinking) {
          yield { type: DeltaType.THINKING, delta: chunk.message.thinking };
        }

        if (chunk.message?.content) {
          yield { type: DeltaType.TEXT, delta: chunk.message.content };
        }

        if (chunk.message?.tool_calls) {
          for (const tc of chunk.message.tool_calls) {
            yield {
              type: DeltaType.TOOL_CALL,
              id: tc.id || `ollama-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
              name: tc.function?.name,
              arguments: tc.function?.arguments || {},
            };
          }
        }

        if (chunk.done) {
          const hasToolCalls = chunk.message?.tool_calls?.length > 0;
          yield {
            type: DeltaType.DONE,
            stopReason: hasToolCalls ? StopReason.TOOL_USE : StopReason.END_TURN,
          };
          return;
        }
      }
    }

    // If we reach here without a done signal, emit done anyway
    yield { type: DeltaType.DONE, stopReason: StopReason.END_TURN };
  }
}
