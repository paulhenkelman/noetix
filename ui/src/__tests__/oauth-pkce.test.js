import { describe, it, expect } from 'vitest';
import { generatePKCE, buildAuthorizationUrl, parseJwtClaims, OAUTH_PROVIDERS } from '../server/auth/oauth-pkce.js';

describe('PKCE helpers', () => {
  it('generatePKCE returns base64url verifier and challenge without padding', () => {
    const { codeVerifier, codeChallenge } = generatePKCE();

    // Base64url: no +, /, or = characters
    expect(codeVerifier).not.toMatch(/[+/=]/);
    expect(codeChallenge).not.toMatch(/[+/=]/);

    // Verifier from 64 bytes base64url = 86 chars
    expect(codeVerifier.length).toBeGreaterThanOrEqual(80);
    // Challenge from 32-byte SHA-256 base64url = 43 chars
    expect(codeChallenge).toHaveLength(43);
  });

  it('generatePKCE produces different values each call', () => {
    const a = generatePKCE();
    const b = generatePKCE();
    expect(a.codeVerifier).not.toBe(b.codeVerifier);
    expect(a.codeChallenge).not.toBe(b.codeChallenge);
  });
});

describe('buildAuthorizationUrl', () => {
  it('builds correct OpenAI authorization URL', () => {
    const { url, codeVerifier, state } = buildAuthorizationUrl('openai');

    expect(url).toContain('https://auth.openai.com/oauth/authorize');
    expect(url).toContain('client_id=app_EMoamEEZ73f0CkXaXp7hrann');
    expect(url).toContain('redirect_uri=');
    expect(url).toContain('localhost%3A1455');
    expect(url).toContain('code_challenge_method=S256');
    expect(url).toContain('code_challenge=');
    expect(url).toContain('response_type=code');
    expect(url).toContain('scope=openid');
    expect(url).toContain('state=');

    expect(codeVerifier).toBeTruthy();
    expect(state).toBeTruthy();
  });

  it('builds correct Anthropic authorization URL', () => {
    const { url } = buildAuthorizationUrl('anthropic');

    expect(url).toContain('https://claude.ai/oauth/authorize');
    expect(url).toContain('client_id=9d1c250a');
    expect(url).toContain('scope=user%3Ainference');
  });

  it('throws for unknown provider', () => {
    expect(() => buildAuthorizationUrl('unknown')).toThrow('No OAuth config for provider');
  });
});

describe('parseJwtClaims', () => {
  it('decodes a JWT payload', () => {
    // Build a minimal JWT: header.payload.signature
    const payload = { email: 'test@example.com', sub: '12345' };
    const encoded = Buffer.from(JSON.stringify(payload)).toString('base64url');
    const jwt = `eyJhbGciOiJSUzI1NiJ9.${encoded}.fake-signature`;

    const claims = parseJwtClaims(jwt);
    expect(claims.email).toBe('test@example.com');
    expect(claims.sub).toBe('12345');
  });

  it('returns empty object for null/undefined', () => {
    expect(parseJwtClaims(null)).toEqual({});
    expect(parseJwtClaims(undefined)).toEqual({});
  });

  it('returns empty object for invalid token', () => {
    expect(parseJwtClaims('not-a-jwt')).toEqual({});
  });

  it('extracts OpenAI account info', () => {
    const payload = {
      email: 'user@example.com',
      'https://api.openai.com/auth': {
        chatgpt_account_id: 'acct_123',
        organization_id: 'org_456',
      },
    };
    const encoded = Buffer.from(JSON.stringify(payload)).toString('base64url');
    const jwt = `header.${encoded}.sig`;

    const claims = parseJwtClaims(jwt);
    expect(claims['https://api.openai.com/auth'].chatgpt_account_id).toBe('acct_123');
  });
});

describe('OAUTH_PROVIDERS config', () => {
  it('has OpenAI config with all required fields', () => {
    const cfg = OAUTH_PROVIDERS.openai;
    expect(cfg.authEndpoint).toBeTruthy();
    expect(cfg.tokenEndpoint).toBeTruthy();
    expect(cfg.clientId).toBeTruthy();
    expect(cfg.redirectUri).toContain('localhost:1455');
    expect(cfg.scopes).toContain('openid');
    expect(cfg.deviceAuthEndpoint).toBeTruthy();
    expect(cfg.subscriptionBaseUrl).toContain('chatgpt.com');
  });

  it('has Anthropic config with all required fields', () => {
    const cfg = OAUTH_PROVIDERS.anthropic;
    expect(cfg.authEndpoint).toContain('claude.ai');
    expect(cfg.tokenEndpoint).toContain('anthropic.com');
    expect(cfg.clientId).toBeTruthy();
    expect(cfg.scopes).toContain('user:inference');
    expect(cfg.subscriptionBaseUrl).toBeNull();
  });

  it('OpenAI has device code endpoints', () => {
    expect(OAUTH_PROVIDERS.openai.deviceAuthEndpoint).toBeTruthy();
    expect(OAUTH_PROVIDERS.openai.deviceTokenEndpoint).toBeTruthy();
    expect(OAUTH_PROVIDERS.openai.deviceVerificationUri).toBeTruthy();
  });

  it('Anthropic does not have device code endpoints', () => {
    expect(OAUTH_PROVIDERS.anthropic.deviceAuthEndpoint).toBeUndefined();
  });
});
