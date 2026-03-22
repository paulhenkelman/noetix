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

  if (config.llmAuthMethod === 'oauth') {
    // OAuth mode: env var takes precedence, then credential store
    if (resolved.llmAuthToken) {
      // Already set from LLM_AUTH_TOKEN env var — use as-is
    } else {
      const creds = getProviderCredentials(provider);
      if (creds?.type === 'oauth') {
        // Prefer exchanged API key (works at api.openai.com, no Cloudflare)
        if (creds.apiKey) {
          resolved.llmApiKey = creds.apiKey;
        } else if (creds.token) {
          resolved.llmAuthToken = creds.token;
          resolved.llmTokenGetter = () => getValidToken(provider);
        }
      }
    }
  } else if (!resolved.llmApiKey) {
    // API key mode: fallback to credential store if no key in config/env
    const creds = getProviderCredentials(provider);
    if (creds?.apiKey) resolved.llmApiKey = creds.apiKey;
  }

  switch (provider) {
    case 'openai': {
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
