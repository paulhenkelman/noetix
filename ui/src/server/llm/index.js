/**
 * LLM provider factory.
 *
 * Dynamic import based on config.llmProvider to avoid loading
 * unnecessary SDKs at startup.
 */

export async function createProvider(config) {
  const provider = config.llmProvider;

  switch (provider) {
    case 'openai': {
      const { OpenAIProvider } = await import('./openai.js');
      return new OpenAIProvider(config);
    }
    case 'anthropic': {
      const { AnthropicProvider } = await import('./anthropic.js');
      return new AnthropicProvider(config);
    }
    case 'ollama': {
      const { OllamaProvider } = await import('./ollama.js');
      return new OllamaProvider(config);
    }
    case 'vllm': {
      const { VLLMProvider } = await import('./vllm.js');
      return new VLLMProvider(config);
    }
    default:
      throw new Error(`Unknown LLM provider: ${provider}`);
  }
}
