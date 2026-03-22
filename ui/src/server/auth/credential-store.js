/**
 * Credential store for LLM provider authentication.
 *
 * Stores and retrieves provider credentials from ~/.noetix/credentials.json.
 * File is written atomically (write .tmp → chmod 0600 → rename).
 */

import fs from 'fs';
import path from 'path';
import https from 'https';
import http from 'http';

const NOETIX_DIR = path.join(process.env.HOME || '/root', '.noetix');
const CREDENTIALS_PATH = path.join(NOETIX_DIR, 'credentials.json');

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function readCredentials() {
  try {
    return JSON.parse(fs.readFileSync(CREDENTIALS_PATH, 'utf-8'));
  } catch {
    return {};
  }
}

function writeCredentials(data) {
  fs.mkdirSync(NOETIX_DIR, { recursive: true });
  const tmp = CREDENTIALS_PATH + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2), { mode: 0o600 });
  fs.renameSync(tmp, CREDENTIALS_PATH);
}

function isTokenExpired(creds) {
  if (!creds.expiresAt) return false;          // no expiry set → treat as valid
  return Date.now() >= creds.expiresAt - 60000; // 60s buffer
}

function refreshTokenRequest(creds) {
  return new Promise((resolve, reject) => {
    if (!creds.refreshToken || !creds.tokenEndpoint) {
      return reject(new Error('No refresh token or token endpoint configured'));
    }

    const body = new URLSearchParams({
      grant_type: 'refresh_token',
      refresh_token: creds.refreshToken,
      ...(creds.clientId ? { client_id: creds.clientId } : {}),
    }).toString();

    const url = new URL(creds.tokenEndpoint);
    const transport = url.protocol === 'https:' ? https : http;

    const req = transport.request(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Content-Length': Buffer.byteLength(body),
      },
    }, (res) => {
      let data = '';
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => {
        if (res.statusCode < 200 || res.statusCode >= 300) {
          return reject(new Error(`Token refresh failed: ${res.statusCode} ${data}`));
        }
        try {
          const json = JSON.parse(data);
          resolve({
            token: json.access_token,
            refreshToken: json.refresh_token || creds.refreshToken,
            expiresAt: json.expires_in
              ? Date.now() + json.expires_in * 1000
              : undefined,
          });
        } catch (err) {
          reject(new Error(`Token refresh response parse error: ${err.message}`));
        }
      });
    });

    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Get stored credentials for a provider.
 * @param {string} provider - e.g. 'openai', 'anthropic'
 * @returns {{ type: string, apiKey?: string, token?: string, refreshToken?: string, expiresAt?: number, tokenEndpoint?: string, clientId?: string } | null}
 */
export function getProviderCredentials(provider) {
  const all = readCredentials();
  return all[provider] || null;
}

/**
 * Save credentials for a provider (merges with existing data).
 * @param {string} provider
 * @param {object} creds
 */
export function setProviderCredentials(provider, creds) {
  const all = readCredentials();
  all[provider] = { ...(all[provider] || {}), ...creds };
  writeCredentials(all);
}

/**
 * Remove all credentials for a provider.
 * @param {string} provider
 */
export function clearProviderCredentials(provider) {
  const all = readCredentials();
  delete all[provider];
  writeCredentials(all);
}

/**
 * Get a valid token for a provider, refreshing if expired.
 * Returns the token string or null if unavailable.
 * @param {string} provider
 * @returns {Promise<string|null>}
 */
export async function getValidToken(provider) {
  const creds = getProviderCredentials(provider);
  if (!creds || creds.type !== 'oauth') return null;

  if (!isTokenExpired(creds)) {
    return creds.token || null;
  }

  // Attempt refresh
  try {
    const refreshed = await refreshTokenRequest(creds);
    setProviderCredentials(provider, {
      token: refreshed.token,
      refreshToken: refreshed.refreshToken,
      expiresAt: refreshed.expiresAt,
    });
    return refreshed.token;
  } catch {
    // Refresh failed — return existing token (may still work)
    return creds.token || null;
  }
}

// Exported for testing
export { readCredentials, writeCredentials, isTokenExpired };
