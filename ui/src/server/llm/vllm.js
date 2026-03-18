/**
 * vLLM provider adapter.
 *
 * Thin subclass of OpenAI — vLLM exposes an OpenAI-compatible API.
 */

import { OpenAIProvider } from './openai.js';

export class VLLMProvider extends OpenAIProvider {
  constructor(config) {
    super({
      ...config,
      llmBaseUrl: config.llmBaseUrl || 'http://localhost:8000/v1',
      llmApiKey: config.llmApiKey || 'dummy',
    });
  }
}
