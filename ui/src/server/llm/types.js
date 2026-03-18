/**
 * Shared LLM delta types and helpers.
 */

export const DeltaType = {
  TEXT: 'text',
  THINKING: 'thinking',
  TOOL_CALL: 'tool_call',
  TOOL_CALL_DELTA: 'tool_call_delta',
  DONE: 'done',
};

export const StopReason = {
  END_TURN: 'end_turn',
  TOOL_USE: 'tool_use',
  MAX_TOKENS: 'max_tokens',
};

/**
 * Map a generic effort string to provider-specific parameters.
 */
export function mapEffort(provider, effort) {
  if (!effort) return {};

  switch (provider) {
    case 'openai':
    case 'vllm': {
      // OpenAI supports low/medium/high
      const mapped = effort === 'xhigh' ? 'high' : effort;
      return { reasoning_effort: mapped };
    }
    case 'anthropic': {
      const budgets = { low: 2048, medium: 4096, high: 10000, xhigh: 32000 };
      const budget = budgets[effort] || budgets.high;
      return { thinking: { type: 'enabled', budget_tokens: budget } };
    }
    case 'ollama':
    default:
      return {};
  }
}
