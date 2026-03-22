/**
 * LLM provider factory.
 *
 * Dynamic import based on config.llmProvider to avoid loading
 * unnecessary SDKs at startup.
 */

import { getProviderCredentials, getValidToken } from '../auth/credential-store.js';

export async function createProvider(config) {
  const provider = config.llmProvider;
  let resolved = { ...config };

  // Resolve credentials from store (regardless of auth_method setting)
  if (!resolved.llmApiKey && !resolved.llmAuthToken) {
    const creds = getProviderCredentials(provider);
    if (creds) {
      if (creds.apiKey) {
        // API key — works at api.openai.com (standard endpoint)
        resolved.llmApiKey = creds.apiKey;
      } else if (creds.type === 'oauth' && creds.token) {
        // OAuth access token — route through subscription endpoint
        resolved.llmAuthToken = creds.token;
        resolved.llmTokenGetter = () => getValidToken(provider);
        if (creds.accountId) resolved.llmAccountId = creds.accountId;
        resolved._useResponsesApi = true;
      }
    }
  }

  switch (provider) {
    case 'openai': {
      // Subscription OAuth without API key → use Responses API at chatgpt.com
      if (resolved._useResponsesApi) {
        const { OpenAIResponsesProvider } = await import('./openai-responses.js');
        return new OpenAIResponsesProvider(resolved);
      }
      const { OpenAIProvider } = await import('./openai.js');
      return new OpenAIProvider(resolved);
    }
    case 'anthropic': {
      const { AnthropicProvider } = await import('./anthropic.js');
      return new AnthropicProvider(resolved);
    }
    case 'ollama': {
      const { OllamaProvider } = await import('./ollama.js');
      return new OllamaProvider(resolved);
    }
    case 'vllm': {
      const { VLLMProvider } = await import('./vllm.js');
      return new VLLMProvider(resolved);
    }
    default:
      throw new Error(`Unknown LLM provider: ${provider}`);
  }
}
