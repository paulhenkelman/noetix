/**
 * Local OAuth callback server.
 *
 * Spins up a temporary HTTP server on a given port to receive the OAuth
 * authorization code redirect. Auto-closes after receiving the callback
 * or on timeout.
 */

import http from 'http';

const SUCCESS_HTML = `<!doctype html>
<html><head><title>Noetix</title></head>
<body style="background:#0b1020;color:#e9eefc;font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center">
<h2>Authentication successful</h2>
<p style="color:#5bc48a">You can close this tab and return to Noetix.</p>
</div></body></html>`;

const ERROR_HTML = `<!doctype html>
<html><head><title>Noetix</title></head>
<body style="background:#0b1020;color:#e9eefc;font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center">
<h2>Authentication failed</h2>
<p style="color:#e05050">Missing authorization code. Please try again.</p>
</div></body></html>`;

/**
 * Start a temporary HTTP server to receive the OAuth callback.
 *
 * @param {number} port - Port to listen on (default 1455)
 * @param {number} timeout - Timeout in ms (default 120000 = 2 minutes)
 * @returns {{ promise: Promise<{ code: string, state: string }>, close: () => void }}
 */
export function startCallbackServer(port = 1455, timeout = 120000) {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, `http://localhost:${port}`);

    if (url.pathname === '/auth/callback') {
      const code = url.searchParams.get('code');
      const state = url.searchParams.get('state');
      const error = url.searchParams.get('error');
      const errorDesc = url.searchParams.get('error_description');

      if (code) {
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end(SUCCESS_HTML);
        resolve({ code, state });
      } else if (error) {
        const msg = errorDesc || error;
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end(ERROR_HTML.replace('Missing authorization code. Please try again.', msg));
        reject(new Error(`OAuth error: ${msg}`));
      } else {
        res.writeHead(400, { 'Content-Type': 'text/html' });
        res.end(ERROR_HTML);
        reject(new Error('OAuth callback missing authorization code'));
      }
      setTimeout(() => server.close(), 500);
    } else {
      res.writeHead(404);
      res.end('Not found');
    }
  });

  const timer = setTimeout(() => {
    server.close();
    reject(new Error('OAuth callback timed out — no response within 2 minutes'));
  }, timeout);

  server.on('close', () => clearTimeout(timer));

  server.listen(port, '127.0.0.1');

  return {
    promise,
    close: () => {
      clearTimeout(timer);
      server.close();
    },
  };
}
