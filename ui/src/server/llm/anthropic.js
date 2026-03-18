/**
 * Anthropic Messages API provider adapter.
 */

import { DeltaType, StopReason, mapEffort } from './types.js';

export class AnthropicProvider {
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
      const { default: Anthropic } = await import('@anthropic-ai/sdk');
      this._client = new Anthropic({
        apiKey: this.apiKey || undefined,
        baseURL: this.baseUrl || undefined,
      });
    }
    return this._client;
  }

  _formatMessages(messages) {
    const system = [];
    const formatted = [];

    for (const msg of messages) {
      if (msg.role === 'system') {
        system.push(msg.content);
        continue;
      }

      if (msg.role === 'tool') {
        // Convert tool results to Anthropic format
        formatted.push({
          role: 'user',
          content: [{
            type: 'tool_result',
            tool_use_id: msg.tool_call_id,
            content: msg.content,
          }],
        });
        continue;
      }

      if (msg.role === 'assistant' && msg.tool_calls) {
        // Convert assistant messages with tool_calls to Anthropic format
        const content = [];
        if (msg.content) {
          content.push({ type: 'text', text: msg.content });
        }
        for (const tc of msg.tool_calls) {
          content.push({
            type: 'tool_use',
            id: tc.id,
            name: tc.function.name,
            input: typeof tc.function.arguments === 'string'
              ? JSON.parse(tc.function.arguments)
              : tc.function.arguments,
          });
        }
        formatted.push({ role: 'assistant', content });
        continue;
      }

      // Regular user/assistant messages
      formatted.push({
        role: msg.role,
        content: msg.content,
      });
    }

    // Merge adjacent same-role messages (Anthropic requires strict alternation)
    const merged = [];
    for (const msg of formatted) {
      const prev = merged[merged.length - 1];
      if (prev && prev.role === msg.role) {
        // Merge content
        if (typeof prev.content === 'string' && typeof msg.content === 'string') {
          prev.content = prev.content + '\n\n' + msg.content;
        } else {
          const prevArr = Array.isArray(prev.content) ? prev.content : [{ type: 'text', text: prev.content }];
          const msgArr = Array.isArray(msg.content) ? msg.content : [{ type: 'text', text: msg.content }];
          prev.content = [...prevArr, ...msgArr];
        }
      } else {
        merged.push({ ...msg });
      }
    }

    return { system: system.join('\n\n'), messages: merged };
  }

  _formatTools(tools) {
    if (!tools || tools.length === 0) return undefined;
    return tools.map((t) => ({
      name: t.name,
      description: t.description || '',
      input_schema: t.inputSchema || { type: 'object', properties: {} },
    }));
  }

  async *chat(messages, tools, options = {}) {
    const client = await this._getClient();

    const { system, messages: formattedMessages } = this._formatMessages(messages);

    const params = {
      model: this.model,
      messages: formattedMessages,
      max_tokens: this.maxTokens,
      stream: true,
    };

    if (system) params.system = system;
    if (this.temperature > 0) params.temperature = this.temperature;

    const effortParams = mapEffort('anthropic', this.reasoningEffort);
    if (effortParams.thinking) params.thinking = effortParams.thinking;

    const formattedTools = this._formatTools(tools);
    if (formattedTools) params.tools = formattedTools;

    const stream = await client.messages.stream(params);

    // Track tool_use blocks being built
    const toolUseBlocks = {};
    let stopReason = null;

    for await (const event of stream) {
      switch (event.type) {
        case 'content_block_start': {
          const block = event.content_block;
          if (block?.type === 'tool_use') {
            toolUseBlocks[event.index] = {
              id: block.id,
              name: block.name,
              inputJson: '',
            };
          }
          break;
        }

        case 'content_block_delta': {
          const delta = event.delta;
          if (delta?.type === 'text_delta') {
            yield { type: DeltaType.TEXT, delta: delta.text };
          } else if (delta?.type === 'thinking_delta') {
            yield { type: DeltaType.THINKING, delta: delta.thinking };
          } else if (delta?.type === 'input_json_delta') {
            const block = toolUseBlocks[event.index];
            if (block) {
              block.inputJson += delta.partial_json;
            }
          }
          break;
        }

        case 'content_block_stop': {
          const block = toolUseBlocks[event.index];
          if (block) {
            let parsedArgs = {};
            try {
              parsedArgs = JSON.parse(block.inputJson);
            } catch {
              parsedArgs = block.inputJson;
            }
            yield {
              type: DeltaType.TOOL_CALL,
              id: block.id,
              name: block.name,
              arguments: parsedArgs,
            };
            delete toolUseBlocks[event.index];
          }
          break;
        }

        case 'message_stop':
        case 'message_delta': {
          if (event.delta?.stop_reason) {
            stopReason = event.delta.stop_reason;
          }
          break;
        }
      }
    }

    // Map stop reason
    let mappedReason;
    switch (stopReason) {
      case 'end_turn':
        mappedReason = StopReason.END_TURN;
        break;
      case 'tool_use':
        mappedReason = StopReason.TOOL_USE;
        break;
      case 'max_tokens':
        mappedReason = StopReason.MAX_TOKENS;
        break;
      default:
        mappedReason = StopReason.END_TURN;
    }

    yield { type: DeltaType.DONE, stopReason: mappedReason };
  }
}
