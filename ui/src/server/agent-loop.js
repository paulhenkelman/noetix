/**
 * Agent loop — orchestrates LLM ↔ tool iterations.
 *
 * Calls the LLM provider, collects tool calls, executes them via
 * MCPManager, appends results, and repeats until the model stops
 * or max iterations reached.
 */

import crypto from 'crypto';
import { DeltaType, StopReason } from './llm/types.js';

export class AgentLoop {
  constructor({ provider, mcpManager, maxIterations = 30 }) {
    this.provider = provider;
    this.mcpManager = mcpManager;
    this.maxIterations = maxIterations;
  }

  /**
   * Run the agent loop.
   *
   * @param {Array} messages - Conversation messages (canonical OpenAI format)
   * @param {Function} onDelta - Callback for streaming events
   * @param {Object} options - { signal } for AbortSignal
   * @returns {{ text: string, toolCalls: string[] }}
   */
  async run(messages, onDelta, options = {}) {
    const tools = this.mcpManager ? this.mcpManager.getTools() : [];
    const allToolCalls = [];
    let finalText = '';
    let iterations = 0;

    while (iterations < this.maxIterations) {
      iterations++;

      const toolCalls = [];
      let iterationText = '';

      // Call the LLM
      const stream = this.provider.chat(messages, tools, options);

      for await (const delta of stream) {
        switch (delta.type) {
          case DeltaType.TEXT:
            iterationText += delta.delta;
            if (onDelta) onDelta({ type: 'text', delta: delta.delta });
            break;

          case DeltaType.THINKING:
            if (onDelta) onDelta({ type: 'thinking', delta: delta.delta });
            break;

          case DeltaType.TOOL_CALL:
            toolCalls.push(delta);
            break;

          case DeltaType.DONE:
            break;
        }
      }

      // If no tool calls, we're done
      if (toolCalls.length === 0) {
        finalText = iterationText;
        break;
      }

      // Append assistant message with tool calls to conversation
      const assistantMsg = {
        role: 'assistant',
        content: iterationText || null,
        tool_calls: toolCalls.map((tc) => ({
          id: tc.id || crypto.randomUUID(),
          type: 'function',
          function: {
            name: tc.name,
            arguments: typeof tc.arguments === 'string'
              ? tc.arguments
              : JSON.stringify(tc.arguments),
          },
          // Preserve Responses API item ID (fc_...) for round-tripping
          ...(tc._responseItemId ? { _responseItemId: tc._responseItemId } : {}),
        })),
      };
      messages.push(assistantMsg);

      // Execute all tool calls
      const toolResults = await Promise.all(
        assistantMsg.tool_calls.map(async (tc) => {
          const toolName = tc.function.name;
          allToolCalls.push(toolName);

          if (onDelta) onDelta({ type: 'status', text: `Calling ${toolName}...` });

          try {
            let args;
            try {
              args = typeof tc.function.arguments === 'string'
                ? JSON.parse(tc.function.arguments)
                : tc.function.arguments;
            } catch {
              args = {};
            }

            const result = await this.mcpManager.callTool(toolName, args);

            // Extract text content from MCP result
            const content = this._extractContent(result);

            if (onDelta) onDelta({ type: 'status', text: `Completed ${toolName}` });

            return {
              role: 'tool',
              tool_call_id: tc.id,
              content,
            };
          } catch (err) {
            if (onDelta) onDelta({ type: 'status', text: `Error in ${toolName}: ${err.message}` });

            return {
              role: 'tool',
              tool_call_id: tc.id,
              content: `Error: ${err.message}`,
            };
          }
        })
      );

      // Append tool results to conversation
      messages.push(...toolResults);

      // Update running text (the final iteration's text will be the response)
      finalText = iterationText;
    }

    if (iterations >= this.maxIterations) {
      console.warn(`[agent-loop] Max iterations (${this.maxIterations}) reached`);
    }

    return {
      text: finalText,
      toolCalls: allToolCalls,
    };
  }

  _extractContent(result) {
    if (!result) return '';

    // MCP returns {content: [{type:'text', text:'...'}]}
    if (result.content && Array.isArray(result.content)) {
      return result.content
        .filter((c) => c.type === 'text')
        .map((c) => c.text)
        .join('\n');
    }

    // Fallback: if result is a string
    if (typeof result === 'string') return result;

    return JSON.stringify(result);
  }
}
