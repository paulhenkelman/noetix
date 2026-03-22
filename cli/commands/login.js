/**
 * noetix login / logout — credential management commands.
 */

import { select, password, input, confirm } from '@inquirer/prompts';
import chalk from 'chalk';
import fs from 'fs';
import path from 'path';
import { parse, stringify } from 'smol-toml';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const CLI_ROOT = path.resolve(path.dirname(__filename), '..', '..');

// Import credential store from ui/src/server/auth
// Resolve relative to CLI_ROOT which is the project root
const credStorePath = path.join(CLI_ROOT, 'ui', 'src', 'server', 'auth', 'credential-store.js');

async function loadCredentialStore() {
  return import(credStorePath);
}

function readNoetixConfig(dir) {
  const configPath = path.join(dir, 'noetix.config');
  try {
    return { path: configPath, data: parse(fs.readFileSync(configPath, 'utf-8')) };
  } catch {
    return null;
  }
}

function writeNoetixConfig(configPath, data) {
  fs.writeFileSync(configPath, stringify(data));
}

export async function login(options) {
  console.log('');
  console.log(chalk.bold('  Noetix Login'));
  console.log('');

  // Determine provider
  let provider = options.provider;

  if (!provider) {
    const config = readNoetixConfig(process.cwd());
    if (config?.data?.llm?.provider) {
      provider = config.data.llm.provider;
      console.log(chalk.dim(`  Provider from config: ${provider}`));
    } else {
      provider = await select({
        message: 'LLM provider',
        choices: [
          { name: 'OpenAI', value: 'openai' },
          { name: 'Anthropic', value: 'anthropic' },
          { name: 'Ollama (no auth needed)', value: 'ollama' },
          { name: 'vLLM (no auth needed)', value: 'vllm' },
        ],
      });
    }
  }

  if (provider === 'ollama' || provider === 'vllm') {
    console.log(chalk.dim(`  ${provider} does not require authentication.`));
    console.log('');
    return;
  }

  // Choose auth method
  const authMethod = await select({
    message: 'Authentication method',
    choices: [
      { name: 'API key', value: 'api_key' },
      { name: 'OAuth / Bearer token', value: 'oauth' },
    ],
  });

  const store = await loadCredentialStore();

  if (authMethod === 'api_key') {
    const apiKey = await password({
      message: `${provider === 'openai' ? 'OpenAI' : 'Anthropic'} API key`,
      mask: '*',
    });

    if (!apiKey) {
      console.log(chalk.yellow('  No key entered — cancelled.'));
      return;
    }

    store.setProviderCredentials(provider, { type: 'api_key', apiKey });
    console.log(chalk.green(`  API key saved for ${provider}.`));
  } else {
    // OAuth token
    const token = await password({
      message: 'Bearer token',
      mask: '*',
    });

    if (!token) {
      console.log(chalk.yellow('  No token entered — cancelled.'));
      return;
    }

    const creds = { type: 'oauth', token };

    const hasRefresh = await confirm({
      message: 'Do you have a refresh token?',
      default: false,
    });

    if (hasRefresh) {
      creds.refreshToken = await password({
        message: 'Refresh token',
        mask: '*',
      });

      creds.tokenEndpoint = await input({
        message: 'Token endpoint URL (for refresh)',
        default: '',
      });

      if (creds.tokenEndpoint) {
        creds.clientId = await input({
          message: 'Client ID (optional)',
          default: '',
        });
        if (!creds.clientId) delete creds.clientId;
      } else {
        delete creds.tokenEndpoint;
      }
    }

    store.setProviderCredentials(provider, creds);
    console.log(chalk.green(`  OAuth token saved for ${provider}.`));
  }

  // Update auth_method in noetix.config if it exists in cwd
  const config = readNoetixConfig(process.cwd());
  if (config) {
    if (!config.data.llm) config.data.llm = {};
    config.data.llm.auth_method = authMethod;
    writeNoetixConfig(config.path, config.data);
    console.log(chalk.dim(`  Updated auth_method in noetix.config`));
  }

  console.log('');
}

export async function logout(options) {
  console.log('');

  let provider = options.provider;

  if (!provider) {
    const config = readNoetixConfig(process.cwd());
    if (config?.data?.llm?.provider) {
      provider = config.data.llm.provider;
    } else {
      provider = await select({
        message: 'Provider to clear',
        choices: [
          { name: 'OpenAI', value: 'openai' },
          { name: 'Anthropic', value: 'anthropic' },
          { name: 'All providers', value: 'all' },
        ],
      });
    }
  }

  const store = await loadCredentialStore();

  if (provider === 'all') {
    store.clearProviderCredentials('openai');
    store.clearProviderCredentials('anthropic');
    console.log(chalk.green('  All credentials cleared.'));
  } else {
    store.clearProviderCredentials(provider);
    console.log(chalk.green(`  Credentials cleared for ${provider}.`));
  }

  console.log('');
}
