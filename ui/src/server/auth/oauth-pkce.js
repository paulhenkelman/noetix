/**
 * OAuth 2.0 PKCE helpers and provider-specific configuration.
 *
 * Supports OpenAI (browser + device code) and Anthropic (browser) flows.
 * Zero external dependencies — uses Node.js crypto, http, https.
 */

import crypto from 'crypto';
import https from 'https';
import http from 'http';

// ---------------------------------------------------------------------------
// Provider OAuth configurations
// ---------------------------------------------------------------------------

export const OAUTH_PROVIDERS = {
  openai: {
    authEndpoint: 'https://auth.openai.com/oauth/authorize',
    tokenEndpoint: 'https://auth.openai.com/oauth/token',
    clientId: 'app_EMoamEEZ73f0CkXaXp7hrann',
    redirectUri: 'http://localhost:1455/auth/callback',
    scopes: 'openid profile email offline_access',
    extraParams: {
      id_token_add_organizations: 'true',
      codex_cli_simplified_flow: 'true',
      originator: 'codex_cli_rs',
    },
    deviceAuthEndpoint: 'https://auth.openai.com/api/accounts/deviceauth/usercode',
    deviceTokenEndpoint: 'https://auth.openai.com/api/accounts/deviceauth/token',
    deviceVerificationUri: 'https://auth.openai.com/codex/device',
    subscriptionBaseUrl: 'https://chatgpt.com/backend-api/codex',
  },
  anthropic: {
    authEndpoint: 'https://claude.ai/oauth/authorize',
    tokenEndpoint: 'https://console.anthropic.com/v1/oauth/token',
    clientId: '9d1c250a-e61b-44d9-88ed-5944d1962f5e',
    redirectUri: 'https://console.anthropic.com/oauth/code/callback',
    scopes: 'user:inference user:profile',
    extraParams: { code: 'true' },
    subscriptionBaseUrl: null, // uses standard api.anthropic.com
  },
};

// ---------------------------------------------------------------------------
// PKCE helpers
// ---------------------------------------------------------------------------

function base64url(buffer) {
  return buffer.toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function generatePKCE() {
  const codeVerifier = base64url(crypto.randomBytes(64));
  const codeChallenge = base64url(crypto.createHash('sha256').update(codeVerifier).digest());
  return { codeVerifier, codeChallenge };
}

export function buildAuthorizationUrl(provider, opts = {}) {
  const cfg = OAUTH_PROVIDERS[provider];
  if (!cfg) throw new Error(`No OAuth config for provider: ${provider}`);

  const { codeVerifier, codeChallenge } = generatePKCE();
  const state = base64url(crypto.randomBytes(32));

  const params = new URLSearchParams({
    response_type: 'code',
    client_id: cfg.clientId,
    redirect_uri: cfg.redirectUri,
    scope: cfg.scopes,
    code_challenge: codeChallenge,
    code_challenge_method: 'S256',
    state,
    ...cfg.extraParams,
  });

  // Include organization/workspace ID so it's embedded in the id_token
  if (opts.organizationId) {
    params.set('allowed_workspace_id', opts.organizationId);
  }

  return {
    url: `${cfg.authEndpoint}?${params.toString()}`,
    codeVerifier,
    state,
  };
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

function httpsGet(url, headers = {}) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(url);
    const transport = parsed.protocol === 'https:' ? https : http;

    const req = transport.request(parsed, {
      method: 'GET',
      headers,
    }, (res) => {
      let data = '';
      res.on('data', (c) => { data += c; });
      res.on('end', () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          try { resolve(JSON.parse(data)); } catch { resolve(data); }
        } else {
          reject(new Error(`HTTP ${res.statusCode}: ${data}`));
        }
      });
    });

    req.on('error', reject);
    req.end();
  });
}

function httpsPost(url, body, contentType = 'application/x-www-form-urlencoded') {
  return new Promise((resolve, reject) => {
    const parsed = new URL(url);
    const transport = parsed.protocol === 'https:' ? https : http;

    const payload = typeof body === 'string' ? body : JSON.stringify(body);
    if (typeof body !== 'string') contentType = 'application/json';

    const req = transport.request(parsed, {
      method: 'POST',
      headers: {
        'Content-Type': contentType,
        'Content-Length': Buffer.byteLength(payload),
      },
    }, (res) => {
      let data = '';
      res.on('data', (c) => { data += c; });
      res.on('end', () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          try { resolve(JSON.parse(data)); } catch { resolve(data); }
        } else {
          reject(new Error(`HTTP ${res.statusCode}: ${data}`));
        }
      });
    });

    req.on('error', reject);
    req.write(payload);
    req.end();
  });
}

// ---------------------------------------------------------------------------
// Token exchange
// ---------------------------------------------------------------------------

export async function exchangeCodeForTokens(provider, code, codeVerifier) {
  const cfg = OAUTH_PROVIDERS[provider];

  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    code,
    redirect_uri: cfg.redirectUri,
    client_id: cfg.clientId,
    code_verifier: codeVerifier,
  }).toString();

  const resp = await httpsPost(cfg.tokenEndpoint, body);

  return {
    accessToken: resp.access_token,
    refreshToken: resp.refresh_token,
    expiresIn: resp.expires_in,
    idToken: resp.id_token,
    rawResponse: resp,
  };
}

/**
 * Exchange an OAuth id_token for an OpenAI API key.
 * This is the critical second step after OAuth login — the resulting
 * API key works at api.openai.com/v1 (no Cloudflare issues).
 */
export async function exchangeIdTokenForApiKey(idToken, organizationId) {
  const params = {
    grant_type: 'urn:ietf:params:oauth:grant-type:token-exchange',
    subject_token_type: 'urn:ietf:params:oauth:token-type:id_token',
    subject_token: idToken,
    requested_token: 'openai-api-key',
    client_id: OAUTH_PROVIDERS.openai.clientId,
  };
  if (organizationId) params.organization_id = organizationId;

  const body = new URLSearchParams(params).toString();
  const resp = await httpsPost(OAUTH_PROVIDERS.openai.tokenEndpoint, body);
  return resp.api_key || resp.access_token || resp.token;
}

export async function refreshAccessToken(provider, refreshToken) {
  const cfg = OAUTH_PROVIDERS[provider];

  const body = new URLSearchParams({
    grant_type: 'refresh_token',
    refresh_token: refreshToken,
    client_id: cfg.clientId,
  }).toString();

  const resp = await httpsPost(cfg.tokenEndpoint, body);

  return {
    accessToken: resp.access_token,
    refreshToken: resp.refresh_token || refreshToken,
    expiresIn: resp.expires_in,
  };
}

// ---------------------------------------------------------------------------
// JWT claim extraction (no verification — just payload decode)
// ---------------------------------------------------------------------------

export function parseJwtClaims(token) {
  if (!token) return {};
  try {
    const parts = token.split('.');
    if (parts.length < 2) return {};
    const payload = Buffer.from(parts[1], 'base64url').toString('utf-8');
    return JSON.parse(payload);
  } catch {
    return {};
  }
}

// ---------------------------------------------------------------------------
// Device code flow (OpenAI only)
// ---------------------------------------------------------------------------

export async function requestDeviceCode(provider = 'openai') {
  const cfg = OAUTH_PROVIDERS[provider];
  if (!cfg.deviceAuthEndpoint) throw new Error(`Device code flow not supported for ${provider}`);

  const resp = await httpsPost(cfg.deviceAuthEndpoint, { client_id: cfg.clientId });

  return {
    deviceAuthId: resp.device_auth_id,
    userCode: resp.user_code,
    verificationUri: cfg.deviceVerificationUri || resp.verification_uri,
    interval: resp.interval || 5,
  };
}

export async function pollDeviceToken(provider, deviceAuthId, userCode, interval = 5) {
  const cfg = OAUTH_PROVIDERS[provider];
  if (!cfg.deviceTokenEndpoint) throw new Error(`Device code flow not supported for ${provider}`);

  const maxAttempts = Math.ceil(900 / interval); // 15 minute timeout

  for (let i = 0; i < maxAttempts; i++) {
    await new Promise(r => setTimeout(r, interval * 1000));

    try {
      const resp = await httpsPost(cfg.deviceTokenEndpoint, {
        device_auth_id: deviceAuthId,
        user_code: userCode,
      });

      // Success — we get authorization_code + code_verifier back
      if (resp.authorization_code && resp.code_verifier) {
        // Exchange for actual tokens
        return exchangeCodeForTokens(provider, resp.authorization_code, resp.code_verifier);
      }

      // Direct token response (some implementations)
      if (resp.access_token) {
        return {
          accessToken: resp.access_token,
          refreshToken: resp.refresh_token,
          expiresIn: resp.expires_in,
          idToken: resp.id_token,
          rawResponse: resp,
        };
      }
    } catch (err) {
      // 403/404 = authorization pending, keep polling
      if (err.message.includes('403') || err.message.includes('404')) continue;
      throw err;
    }
  }

  throw new Error('Device authorization timed out');
}

// ---------------------------------------------------------------------------
// Model discovery
// ---------------------------------------------------------------------------

/**
 * Fetch available models from a provider using the given access token.
 * Returns an array of model ID strings.
 */
export async function fetchProviderModels(provider, accessToken) {
  if (provider === 'openai') {
    const resp = await httpsGet('https://api.openai.com/v1/models', {
      Authorization: `Bearer ${accessToken}`,
    });
    if (resp?.data) {
      const EXCLUDE = /audio|transcribe|tts|realtime|image|search|diarize|deep-research|instruct|gpt-3/;
      const DATED = /-20\d\d-\d\d-\d\d$/;
      const LEGACY_GPT4 = /^gpt-4($|-0|-turbo|-1106)/;

      return resp.data
        .map(m => m.id)
        .filter(id =>
          /^(gpt-[45]|o[1-9])/.test(id)
          && !EXCLUDE.test(id)
          && !DATED.test(id)
          && !LEGACY_GPT4.test(id)
        )
        .sort();
    }
  }
  return [];
}
