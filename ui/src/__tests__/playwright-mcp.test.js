import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import http from 'http';
import { probeMcpEndpoint, detectLiveEndpoint, formatDiagnostics } from '../gateway/playwright-mcp.js';

describe('playwright-mcp', () => {
  let server;
  let serverPort;

  function startServer(handler) {
    return new Promise((resolve) => {
      server = http.createServer(handler);
      server.listen(0, '127.0.0.1', () => {
        serverPort = server.address().port;
        resolve();
      });
    });
  }

  afterEach(() => {
    return new Promise((resolve) => {
      if (server) {
        server.close(() => resolve());
        server = null;
      } else {
        resolve();
      }
    });
  });

  describe('probeMcpEndpoint', () => {
    it('should detect a live MCP endpoint', async () => {
      await startServer((_req, res) => {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ jsonrpc: '2.0', id: 1, result: { sessionId: 'test-session-1' } }));
      });

      const result = await probeMcpEndpoint(`http://127.0.0.1:${serverPort}/mcp`);
      expect(result.alive).toBe(true);
      expect(result.sessionId).toBe('test-session-1');
      expect(result.latencyMs).toBeGreaterThanOrEqual(0);
      expect(result.url).toContain(String(serverPort));
    });

    it('should detect a live endpoint even without sessionId in response', async () => {
      await startServer((_req, res) => {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ jsonrpc: '2.0', id: 1, result: {} }));
      });

      const result = await probeMcpEndpoint(`http://127.0.0.1:${serverPort}/mcp`);
      expect(result.alive).toBe(true);
      expect(result.sessionId).toBeNull();
    });

    it('should report dead endpoint when connection refused', async () => {
      const result = await probeMcpEndpoint('http://127.0.0.1:19999/mcp');
      expect(result.alive).toBe(false);
      expect(result.error).toMatch(/Connection failed/);
    });

    it('should report dead endpoint on HTTP 500', async () => {
      await startServer((_req, res) => {
        res.writeHead(500);
        res.end('Internal Server Error');
      });

      const result = await probeMcpEndpoint(`http://127.0.0.1:${serverPort}/mcp`);
      expect(result.alive).toBe(false);
      expect(result.error).toMatch(/HTTP 500/);
    });

    it('should handle non-JSON responses gracefully', async () => {
      await startServer((_req, res) => {
        res.writeHead(200, { 'Content-Type': 'text/plain' });
        res.end('OK');
      });

      const result = await probeMcpEndpoint(`http://127.0.0.1:${serverPort}/mcp`);
      expect(result.alive).toBe(true);
      expect(result.sessionId).toBeNull();
    });
  });

  describe('detectLiveEndpoint', () => {
    it('should find the live endpoint from configured URL', async () => {
      await startServer((_req, res) => {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ jsonrpc: '2.0', id: 1, result: {} }));
      });

      const result = await detectLiveEndpoint(`http://127.0.0.1:${serverPort}/mcp`);
      expect(result.live).not.toBeNull();
      expect(result.live.url).toContain(String(serverPort));
      expect(result.diagnostics.length).toBeGreaterThanOrEqual(1);
    });

    it('should report configured URL as dead when unreachable, even if fallback finds one', async () => {
      const result = await detectLiveEndpoint('http://127.0.0.1:19998/mcp');
      // The configured URL (port 19998) should be reported as dead in diagnostics
      const configuredDiag = result.diagnostics.find((d) => d.url.includes('19998'));
      expect(configuredDiag).toBeDefined();
      expect(configuredDiag.alive).toBe(false);
      // If a fallback port (e.g. 8931) happens to be live in the test environment,
      // detectLiveEndpoint correctly returns it — that's the intended fallback behavior
      expect(result.diagnostics.length).toBeGreaterThanOrEqual(1);
    });
  });

  describe('detectLiveEndpoint with session', () => {
    it('should extract sessionId from live endpoint response', async () => {
      await startServer((_req, res) => {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          jsonrpc: '2.0',
          id: 1,
          result: { sessionId: 'browser-sess-abc123' }
        }));
      });

      const result = await detectLiveEndpoint(`http://127.0.0.1:${serverPort}/mcp`);
      expect(result.live).not.toBeNull();
      expect(result.live.sessionId).toBe('browser-sess-abc123');
    });
  });

  describe('formatDiagnostics', () => {
    it('should format a live endpoint message', () => {
      const detection = {
        live: { url: 'http://127.0.0.1:8931/mcp', sessionId: 'sess-1', latencyMs: 12 },
        diagnostics: [{ url: 'http://127.0.0.1:8931/mcp', alive: true, latencyMs: 12, error: null }]
      };
      const msg = formatDiagnostics(detection);
      expect(msg).toContain('alive');
      expect(msg).toContain('8931');
      expect(msg).toContain('sess-1');
    });

    it('should format a no-endpoint-found message with instructions', () => {
      const detection = {
        live: null,
        diagnostics: [
          { url: 'http://127.0.0.1:8931/mcp', alive: false, latencyMs: 5, error: 'Connection refused' },
          { url: 'http://127.0.0.1:8932/mcp', alive: false, latencyMs: 5, error: 'Connection refused' }
        ]
      };
      const msg = formatDiagnostics(detection);
      expect(msg).toContain('No live Playwright MCP endpoint found');
      expect(msg).toContain('8931');
      expect(msg).toContain('Connection refused');
      expect(msg).toContain('npx');
    });
  });
});
