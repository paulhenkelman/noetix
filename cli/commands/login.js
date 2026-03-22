/**
 * noetix login / logout — credential management commands.
 *
 * Supports browser-based OAuth PKCE, device code (headless), API key,
 * and manual token paste flows.
 */

import { select, password, input } from '@inquirer/prompts';
import chalk from 'chalk';
import ora from 'ora';
import fs from 'fs';
import path from 'path';
import { exec } from 'child_process';
import { parse, stringify } from 'smol-toml';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const CLI_ROOT = path.resolve(path.dirname(__filename), '..', '..');

// Lazy imports from ui/src/server/auth
const authDir = path.join(CLI_ROOT, 'ui', 'src', 'server', 'auth');

async function loadCredentialStore() {
  return import(path.join(authDir, 'credential-store.js'));
}

async function loadOAuthPKCE() {
  return import(path.join(authDir, 'oauth-pkce.js'));
}

async function loadCallbackServer() {
  return import(path.join(authDir, 'callback-server.js'));
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

function openBrowser(url) {
  const cmd = process.platform === 'darwin' ? 'open'
    : process.platform === 'win32' ? 'start'
    : 'xdg-open';
  exec(`${cmd} "${url}"`);
}

// ---------------------------------------------------------------------------
// Browser OAuth PKCE flow
// ---------------------------------------------------------------------------

async function loginWithBrowser(provider, store, oauth) {
  const { buildAuthorizationUrl, exchangeCodeForTokens, exchangeIdTokenForApiKey, parseJwtClaims, OAUTH_PROVIDERS } = oauth;
  const { startCallbackServer } = await loadCallbackServer();

  const cfg = OAUTH_PROVIDERS[provider];
  const { url, codeVerifier, state } = buildAuthorizationUrl(provider);

  // Start local callback server
  const callback = startCallbackServer(1455);

  console.log('');
  console.log(chalk.dim(`  Opening browser...`));
  console.log(chalk.dim(`  If it doesn't open, visit:`));
  console.log(chalk.cyan(`  ${url}`));
  console.log('');

  openBrowser(url);

  const spinner = ora('Waiting for sign-in to complete...').start();

  try {
    const { code, state: returnedState } = await callback.promise;

    if (returnedState !== state) {
      spinner.fail('OAuth state mismatch — possible CSRF. Try again.');
      return false;
    }

    spinner.text = 'Exchanging authorization code...';
    const tokens = await exchangeCodeForTokens(provider, code, codeVerifier);

    const claims = parseJwtClaims(tokens.idToken || tokens.accessToken);

    // For OpenAI: exchange id_token for an API key that works at api.openai.com
    let apiKey;
    if (provider === 'openai' && tokens.idToken) {
      spinner.text = 'Obtaining API key from subscription...';
      try {
        apiKey = await exchangeIdTokenForApiKey(tokens.idToken);
      } catch (err) {
        console.log(chalk.yellow(`  API key exchange failed: ${err.message}`));
        console.log(chalk.dim(`  Falling back to access token (may require chatgpt.com endpoint)`));
      }
    }

    // Store credentials
    store.setProviderCredentials(provider, {
      type: 'oauth',
      apiKey: apiKey || undefined,
      token: tokens.accessToken,
      idToken: tokens.idToken,
      refreshToken: tokens.refreshToken,
      expiresAt: tokens.expiresIn ? Date.now() + tokens.expiresIn * 1000 : undefined,
      tokenEndpoint: cfg.tokenEndpoint,
      clientId: cfg.clientId,
    });

    spinner.succeed(`Signed in to ${provider}${apiKey ? ' (API key obtained)' : ''}`);
    if (claims.email) console.log(chalk.dim(`  Account: ${claims.email}`));
    return true;
  } catch (err) {
    callback.close();
    spinner.fail(`Login failed: ${err.message}`);
    return false;
  }
}

// ---------------------------------------------------------------------------
// Device code flow (headless / SSH)
// ---------------------------------------------------------------------------

async function loginWithDeviceCode(provider, store, oauth) {
  const { requestDeviceCode, pollDeviceToken, exchangeIdTokenForApiKey, parseJwtClaims, OAUTH_PROVIDERS } = oauth;

  const cfg = OAUTH_PROVIDERS[provider];
  const spinner = ora('Requesting device code...').start();

  try {
    const device = await requestDeviceCode(provider);
    spinner.stop();

    console.log('');
    console.log(chalk.bold(`  Visit: ${chalk.cyan(device.verificationUri)}`));
    console.log(chalk.bold(`  Enter code: ${chalk.yellow(device.userCode)}`));
    console.log('');

    const pollSpinner = ora('Waiting for authorization...').start();
    const tokens = await pollDeviceToken(provider, device.deviceAuthId, device.userCode, device.interval);

    const claims = parseJwtClaims(tokens.idToken || tokens.accessToken);

    let apiKey;
    if (provider === 'openai' && tokens.idToken) {
      pollSpinner.text = 'Obtaining API key from subscription...';
      try {
        apiKey = await exchangeIdTokenForApiKey(tokens.idToken);
      } catch (err) {
        console.log(chalk.yellow(`  API key exchange failed: ${err.message}`));
      }
    }

    store.setProviderCredentials(provider, {
      type: 'oauth',
      apiKey: apiKey || undefined,
      token: tokens.accessToken,
      idToken: tokens.idToken,
      refreshToken: tokens.refreshToken,
      expiresAt: tokens.expiresIn ? Date.now() + tokens.expiresIn * 1000 : undefined,
      tokenEndpoint: cfg.tokenEndpoint,
      clientId: cfg.clientId,
    });

    pollSpinner.succeed(`Signed in to ${provider}${apiKey ? ' (API key obtained)' : ''}`);
    if (claims.email) console.log(chalk.dim(`  Account: ${claims.email}`));
    return true;
  } catch (err) {
    spinner.fail(`Device code login failed: ${err.message}`);
    return false;
  }
}

// ---------------------------------------------------------------------------
// Main login command
// ---------------------------------------------------------------------------

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

  const store = await loadCredentialStore();
  const oauth = await loadOAuthPKCE();
  const hasOAuth = !!oauth.OAUTH_PROVIDERS[provider];

  // If --device-auth flag, go straight to device code flow
  if (options.deviceAuth) {
    if (!oauth.OAUTH_PROVIDERS[provider]?.deviceAuthEndpoint) {
      console.log(chalk.red(`  Device code flow is not supported for ${provider}.`));
      return;
    }
    const ok = await loginWithDeviceCode(provider, store, oauth);
    if (ok) updateConfig(provider, 'oauth');
    console.log('');
    return;
  }

  // Choose auth method
  const choices = [];
  if (hasOAuth) {
    choices.push({ name: 'Sign in with browser (OAuth)', value: 'browser' });
  }
  if (oauth.OAUTH_PROVIDERS[provider]?.deviceAuthEndpoint) {
    choices.push({ name: 'Device code (headless / SSH)', value: 'device' });
  }
  choices.push(
    { name: 'API key', value: 'api_key' },
    { name: 'Paste token manually', value: 'paste' },
  );

  const authMethod = await select({
    message: 'Authentication method',
    choices,
  });

  let ok = false;

  if (authMethod === 'browser') {
    ok = await loginWithBrowser(provider, store, oauth);
    if (ok) updateConfig(provider, 'oauth');
  } else if (authMethod === 'device') {
    ok = await loginWithDeviceCode(provider, store, oauth);
    if (ok) updateConfig(provider, 'oauth');
  } else if (authMethod === 'api_key') {
    const providerName = provider === 'openai' ? 'OpenAI' : 'Anthropic';
    const apiKey = await password({
      message: `${providerName} API key`,
      mask: '*',
    });

    if (!apiKey) {
      console.log(chalk.yellow('  No key entered — cancelled.'));
    } else {
      store.setProviderCredentials(provider, { type: 'api_key', apiKey });
      console.log(chalk.green(`  API key saved for ${provider}.`));
      updateConfig(provider, 'api_key');
      ok = true;
    }
  } else if (authMethod === 'paste') {
    const token = await password({ message: 'Bearer token', mask: '*' });
    if (!token) {
      console.log(chalk.yellow('  No token entered — cancelled.'));
    } else {
      store.setProviderCredentials(provider, { type: 'oauth', token });
      console.log(chalk.green(`  Token saved for ${provider}.`));
      updateConfig(provider, 'oauth');
      ok = true;
    }
  }

  console.log('');
}

function updateConfig(provider, authMethod) {
  const config = readNoetixConfig(process.cwd());
  if (config) {
    if (!config.data.llm) config.data.llm = {};
    config.data.llm.auth_method = authMethod;
    writeNoetixConfig(config.path, config.data);
    console.log(chalk.dim(`  Updated auth_method in noetix.config`));
  }
}

// ---------------------------------------------------------------------------
// Logout
// ---------------------------------------------------------------------------

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
