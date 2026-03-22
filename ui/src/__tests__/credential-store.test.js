import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import fs from 'fs';
import path from 'path';
import os from 'os';

// Mock fs to avoid touching real filesystem
vi.mock('fs');

// We need to dynamically import after mocking
let readCredentials, writeCredentials, isTokenExpired;
let getProviderCredentials, setProviderCredentials, clearProviderCredentials, getValidToken;

const NOETIX_DIR = path.join(os.homedir(), '.noetix');
const CREDENTIALS_PATH = path.join(NOETIX_DIR, 'credentials.json');

beforeEach(async () => {
  vi.resetModules();
  vi.restoreAllMocks();

  // Default: file does not exist
  fs.readFileSync = vi.fn(() => { throw new Error('ENOENT'); });
  fs.writeFileSync = vi.fn();
  fs.mkdirSync = vi.fn();
  fs.renameSync = vi.fn();

  const mod = await import('../server/auth/credential-store.js');
  readCredentials = mod.readCredentials;
  writeCredentials = mod.writeCredentials;
  isTokenExpired = mod.isTokenExpired;
  getProviderCredentials = mod.getProviderCredentials;
  setProviderCredentials = mod.setProviderCredentials;
  clearProviderCredentials = mod.clearProviderCredentials;
  getValidToken = mod.getValidToken;
});

describe('readCredentials', () => {
  it('should return empty object when file is missing', () => {
    expect(readCredentials()).toEqual({});
  });

  it('should parse valid JSON', () => {
    const data = { openai: { type: 'api_key', apiKey: 'sk-test' } };
    fs.readFileSync.mockReturnValue(JSON.stringify(data));
    expect(readCredentials()).toEqual(data);
  });

  it('should return empty object for invalid JSON', () => {
    fs.readFileSync.mockReturnValue('not json');
    expect(readCredentials()).toEqual({});
  });
});

describe('writeCredentials', () => {
  it('should create directory and write file with correct permissions', () => {
    const data = { openai: { type: 'api_key', apiKey: 'sk-test' } };
    writeCredentials(data);

    expect(fs.mkdirSync).toHaveBeenCalledWith(NOETIX_DIR, { recursive: true });
    expect(fs.writeFileSync).toHaveBeenCalledWith(
      CREDENTIALS_PATH + '.tmp',
      JSON.stringify(data, null, 2),
      { mode: 0o600 },
    );
    expect(fs.renameSync).toHaveBeenCalledWith(
      CREDENTIALS_PATH + '.tmp',
      CREDENTIALS_PATH,
    );
  });
});

describe('setProviderCredentials', () => {
  it('should merge without clobbering other providers', () => {
    const existing = {
      openai: { type: 'api_key', apiKey: 'sk-openai' },
      anthropic: { type: 'api_key', apiKey: 'sk-ant' },
    };
    fs.readFileSync.mockReturnValue(JSON.stringify(existing));

    setProviderCredentials('openai', { type: 'oauth', token: 'new-token' });

    const writeCall = fs.writeFileSync.mock.calls[0];
    const written = JSON.parse(writeCall[1]);
    // Anthropic should be untouched
    expect(written.anthropic).toEqual({ type: 'api_key', apiKey: 'sk-ant' });
    // OpenAI should be merged
    expect(written.openai.type).toBe('oauth');
    expect(written.openai.token).toBe('new-token');
    expect(written.openai.apiKey).toBe('sk-openai'); // merged, not clobbered
  });
});

describe('getProviderCredentials', () => {
  it('should return correct data for existing provider', () => {
    const data = { openai: { type: 'api_key', apiKey: 'sk-test' } };
    fs.readFileSync.mockReturnValue(JSON.stringify(data));
    expect(getProviderCredentials('openai')).toEqual({ type: 'api_key', apiKey: 'sk-test' });
  });

  it('should return null for missing provider', () => {
    fs.readFileSync.mockReturnValue(JSON.stringify({}));
    expect(getProviderCredentials('openai')).toBeNull();
  });
});

describe('clearProviderCredentials', () => {
  it('should remove only target provider', () => {
    const existing = {
      openai: { type: 'api_key', apiKey: 'sk-openai' },
      anthropic: { type: 'api_key', apiKey: 'sk-ant' },
    };
    fs.readFileSync.mockReturnValue(JSON.stringify(existing));

    clearProviderCredentials('openai');

    const writeCall = fs.writeFileSync.mock.calls[0];
    const written = JSON.parse(writeCall[1]);
    expect(written.openai).toBeUndefined();
    expect(written.anthropic).toEqual({ type: 'api_key', apiKey: 'sk-ant' });
  });
});

describe('isTokenExpired', () => {
  it('should return false when no expiresAt', () => {
    expect(isTokenExpired({ type: 'oauth', token: 'abc' })).toBe(false);
  });

  it('should return false for future timestamp', () => {
    expect(isTokenExpired({ expiresAt: Date.now() + 120000 })).toBe(false);
  });

  it('should return true for past timestamp', () => {
    expect(isTokenExpired({ expiresAt: Date.now() - 1000 })).toBe(true);
  });

  it('should return true within 60s buffer', () => {
    expect(isTokenExpired({ expiresAt: Date.now() + 30000 })).toBe(true);
  });
});

describe('getValidToken', () => {
  it('should return token when not expired', async () => {
    const data = {
      openai: { type: 'oauth', token: 'valid-token', expiresAt: Date.now() + 3600000 },
    };
    fs.readFileSync.mockReturnValue(JSON.stringify(data));

    const token = await getValidToken('openai');
    expect(token).toBe('valid-token');
  });

  it('should return null for non-oauth credentials', async () => {
    const data = {
      openai: { type: 'api_key', apiKey: 'sk-test' },
    };
    fs.readFileSync.mockReturnValue(JSON.stringify(data));

    const token = await getValidToken('openai');
    expect(token).toBeNull();
  });

  it('should return null for missing provider', async () => {
    fs.readFileSync.mockReturnValue(JSON.stringify({}));
    const token = await getValidToken('openai');
    expect(token).toBeNull();
  });

  it('should return existing token when expired but no refresh config', async () => {
    const data = {
      openai: { type: 'oauth', token: 'expired-token', expiresAt: Date.now() - 10000 },
    };
    fs.readFileSync.mockReturnValue(JSON.stringify(data));

    const token = await getValidToken('openai');
    expect(token).toBe('expired-token'); // fallback: returns existing token
  });

  it('should return token when no expiresAt set', async () => {
    const data = {
      openai: { type: 'oauth', token: 'no-expiry-token' },
    };
    fs.readFileSync.mockReturnValue(JSON.stringify(data));

    const token = await getValidToken('openai');
    expect(token).toBe('no-expiry-token');
  });
});
