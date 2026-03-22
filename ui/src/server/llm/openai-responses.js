/**
 * OpenAI Responses API provider adapter (subscription mode).
 *
 * Uses chatgpt.com/backend-api/codex/responses endpoint with OAuth
 * access tokens. Required for ChatGPT Plus/Pro/Team subscriptions
 * that don't have Platform API keys.
 *
 * Different from the Chat Completions adapter:
 * - Endpoint: chatgpt.com, not api.openai.com
 * - API format: Responses API (input[], instructions) not Chat Completions (messages[])
 * - Auth: Bearer access_token + ChatGPT-Account-Id header
 * - Must set store:false, stream:true
 */

import https from 'https';
import { DeltaType, StopReason } from './types.js';

const RESPONSES_URL = 'https://chatgpt.com/backend-api/codex/responses';

export class OpenAIResponsesProvider {
  constructor(config) {
    this.model = config.llmModel;
    this.accessToken = config.llmAuthToken;
    this.tokenGetter = config.llmTokenGetter;
    this.accountId = config.llmAccountId;
    this.maxTokens = config.llmMaxTokens || 16384;
    this.temperature = config.llmTemperature ?? 1.0;
    this.reasoningEffort = config.llmReasoningEffort || 'high';
  }

  _formatTools(tools) {
    if (!tools || tools.length === 0) return undefined;
    return tools.map((t) => ({
      type: 'function',
      name: t.name,
      description: t.description || '',
      parameters: t.inputSchema || {},
    }));
  }

  async *chat(messages, tools, options = {}) {
    const token = this.tokenGetter ? await this.tokenGetter() : this.accessToken;

    // Separate system message as instructions, rest as input
    let instructions = '';
    const input = [];
    for (const msg of messages) {
      if (msg.role === 'system') {
        instructions += (instructions ? '\n\n' : '') + msg.content;
      } else if (msg.role === 'tool') {
        // Tool results in Responses API format
        input.push({
          type: 'function_call_output',
          call_id: msg.tool_call_id,
          output: msg.content,
        });
      } else if (msg.role === 'assistant' && msg.tool_calls) {
        // Assistant tool call results — emit as function_call items
        for (const tc of msg.tool_calls) {
          // Responses API requires id starting with 'fc' and separate call_id
          const fcId = tc._responseItemId || tc.id;
          input.push({
            type: 'function_call',
            id: fcId,
            call_id: tc.id,
            name: tc.function.name,
            arguments: typeof tc.function.arguments === 'string'
              ? tc.function.arguments
              : JSON.stringify(tc.function.arguments),
          });
        }
        if (msg.content) {
          input.push({ role: 'assistant', content: msg.content });
        }
      } else {
        input.push({ role: msg.role, content: msg.content });
      }
    }

    const body = {
      model: this.model,
      input,
      instructions: instructions || 'You are a helpful assistant.',
      store: false,
      stream: true,
    };

    if (this.temperature > 0) body.temperature = this.temperature;

    const effortMap = { xhigh: 'high', high: 'high', medium: 'medium', low: 'low' };
    if (this.reasoningEffort && effortMap[this.reasoningEffort]) {
      body.reasoning = { effort: effortMap[this.reasoningEffort] };
    }

    const formattedTools = this._formatTools(tools);
    if (formattedTools) body.tools = formattedTools;

    // Make streaming request
    const events = await this._streamRequest(body, token, options.signal);

    // Track tool calls
    const toolCalls = {};
    let stopReason = StopReason.END_TURN;

    for await (const event of events) {
      switch (event.type) {
        case 'response.output_text.delta':
          yield { type: DeltaType.TEXT, delta: event.delta };
          break;

        case 'response.reasoning_summary_text.delta':
          yield { type: DeltaType.THINKING, delta: event.delta };
          break;

        case 'response.output_item.added':
          if (event.item?.type === 'function_call') {
            toolCalls[event.output_index] = {
              id: event.item.call_id || event.item.id,
              responseItemId: event.item.id,  // fc_... ID for Responses API
              name: event.item.name,
              arguments: '',
            };
          }
          break;

        case 'response.function_call_arguments.delta':
          if (toolCalls[event.output_index]) {
            toolCalls[event.output_index].arguments += event.delta;
          }
          break;

        case 'response.output_item.done':
          if (event.item?.type === 'function_call') {
            const tc = toolCalls[event.output_index];
            if (tc) {
              let parsedArgs = {};
              try { parsedArgs = JSON.parse(tc.arguments); } catch { parsedArgs = tc.arguments; }
              yield {
                type: DeltaType.TOOL_CALL,
                id: tc.id,
                name: tc.name,
                arguments: parsedArgs,
                _responseItemId: tc.responseItemId,
              };
              stopReason = StopReason.TOOL_USE;
            }
          }
          break;

        case 'response.completed': {
          const status = event.response?.status;
          if (status === 'failed') stopReason = StopReason.END_TURN;
          break;
        }
      }
    }

    yield { type: DeltaType.DONE, stopReason };
  }

  _streamRequest(body, token, signal) {
    return new Promise((resolve, reject) => {
      const url = new URL(RESPONSES_URL);
      const payload = JSON.stringify(body);

      const headers = {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(payload),
        'Authorization': `Bearer ${token}`,
        'User-Agent': 'noetix/0.1.0',
      };
      if (this.accountId) {
        headers['ChatGPT-Account-Id'] = this.accountId;
      }

      const req = https.request(url, { method: 'POST', headers }, (res) => {
        if (res.statusCode !== 200) {
          let data = '';
          res.on('data', (c) => { data += c; });
          res.on('end', () => reject(new Error(`${res.statusCode} ${data}`)));
          return;
        }

        // Return an async iterator that parses SSE events
        resolve(parseSSEStream(res));
      });

      if (signal) {
        signal.addEventListener('abort', () => req.destroy());
      }

      req.on('error', reject);
      req.write(payload);
      req.end();
    });
  }
}

async function* parseSSEStream(stream) {
  let buffer = '';

  for await (const chunk of stream) {
    buffer += chunk.toString();
    const lines = buffer.split('\n');
    buffer = lines.pop();

    let eventType = '';
    let eventData = '';

    for (const line of lines) {
      if (line.startsWith('event: ')) {
        eventType = line.slice(7);
      } else if (line.startsWith('data: ')) {
        eventData = line.slice(6);
      } else if (line === '' && eventType && eventData) {
        try {
          yield JSON.parse(eventData);
        } catch {}
        eventType = '';
        eventData = '';
      }
    }
  }
}
