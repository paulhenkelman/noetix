/**
 * OpenAI Chat Completions provider adapter.
 */

import { DeltaType, StopReason, mapEffort } from './types.js';

export class OpenAIProvider {
  constructor(config) {
    this.model = config.llmModel;
    this.apiKey = config.llmApiKey;
    this.baseUrl = config.llmBaseUrl || undefined;
    this.maxTokens = config.llmMaxTokens || 16384;
    this.temperature = config.llmTemperature ?? 0.0;
    this.reasoningEffort = config.llmReasoningEffort || 'high';
    this._client = null;
  }

  async _getClient() {
    if (!this._client) {
      const { default: OpenAI } = await import('openai');
      this._client = new OpenAI({
        apiKey: this.apiKey || undefined,
        baseURL: this.baseUrl || undefined,
      });
    }
    return this._client;
  }

  _formatMessages(messages) {
    // Canonical format is OpenAI-native, pass through
    return messages;
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
    const client = await this._getClient();

    const params = {
      model: this.model,
      messages: this._formatMessages(messages),
      stream: true,
      max_tokens: this.maxTokens,
      temperature: this.temperature,
      ...mapEffort('openai', this.reasoningEffort),
    };

    const formattedTools = this._formatTools(tools);
    if (formattedTools) params.tools = formattedTools;
    if (options.signal) params.signal = options.signal;

    const stream = await client.chat.completions.create(params);

    // Accumulate tool calls by index
    const toolCallAccumulators = {};
    let finishReason = null;

    for await (const chunk of stream) {
      const choice = chunk.choices?.[0];
      if (!choice) continue;

      const delta = choice.delta;
      if (!delta) continue;

      // Text content
      if (delta.content) {
        yield { type: DeltaType.TEXT, delta: delta.content };
      }

      // Reasoning/thinking content (OpenAI reasoning models)
      if (delta.reasoning_content) {
        yield { type: DeltaType.THINKING, delta: delta.reasoning_content };
      }

      // Tool calls (streamed incrementally)
      if (delta.tool_calls) {
        for (const tc of delta.tool_calls) {
          const idx = tc.index;
          if (!toolCallAccumulators[idx]) {
            toolCallAccumulators[idx] = {
              id: tc.id || '',
              name: tc.function?.name || '',
              arguments: '',
            };
          }
          const acc = toolCallAccumulators[idx];
          if (tc.id) acc.id = tc.id;
          if (tc.function?.name) acc.name = tc.function.name;
          if (tc.function?.arguments) acc.arguments += tc.function.arguments;
        }
      }

      if (choice.finish_reason) {
        finishReason = choice.finish_reason;
      }
    }

    // Emit accumulated tool calls
    for (const idx of Object.keys(toolCallAccumulators).sort((a, b) => a - b)) {
      const acc = toolCallAccumulators[idx];
      let parsedArgs = {};
      try {
        parsedArgs = JSON.parse(acc.arguments);
      } catch {
        parsedArgs = acc.arguments;
      }
      yield {
        type: DeltaType.TOOL_CALL,
        id: acc.id,
        name: acc.name,
        arguments: parsedArgs,
      };
    }

    // Map finish reason
    let stopReason;
    switch (finishReason) {
      case 'stop':
        stopReason = StopReason.END_TURN;
        break;
      case 'tool_calls':
        stopReason = StopReason.TOOL_USE;
        break;
      case 'length':
        stopReason = StopReason.MAX_TOKENS;
        break;
      default:
        stopReason = StopReason.END_TURN;
    }

    yield { type: DeltaType.DONE, stopReason };
  }
}
