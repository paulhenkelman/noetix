import express from 'express';
import cors from 'cors';
import crypto from 'crypto';
import axios from 'axios';
import { SocksProxyAgent } from 'socks-proxy-agent';
import multer from 'multer';
import FormData from 'form-data';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { execSync, spawn as spawnChild } from 'child_process';
import { detectLiveEndpoint, formatDiagnostics } from './playwright-mcp.js';
import { AgentRunner } from './agent-runner.js';
import { MAX_VERIFY_ATTEMPTS, REQUIRES_TOOL_USE, needsVerification, buildCorrectionPrompt, selectBestResponse } from './verify.js';
import config from './config.js';
import { getProviderCredentials, setProviderCredentials, clearProviderCredentials } from './auth/credential-store.js';
import { buildAuthorizationUrl, exchangeCodeForTokens, exchangeIdTokenForApiKey, parseJwtClaims, fetchProviderModels, OAUTH_PROVIDERS } from './auth/oauth-pkce.js';
import { startCallbackServer } from './auth/callback-server.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const DATA_DIR = path.join(__dirname, '..', 'data');
const CHAT_SESSIONS_FILE = path.join(DATA_DIR, 'chat_sessions.json');
const ACTION_REQUESTS_FILE = path.join(DATA_DIR, 'action_requests.json');
const KB_DEPS_FILE = path.join(DATA_DIR, 'kb_dependencies.json');
const SUPPORTED_LIBRARY_BULK_OPS = new Set(['delete', 'reindex', 'retag']);

fs.mkdirSync(DATA_DIR, { recursive: true });

function readStoreFile(file) {
  try {
    if (!fs.existsSync(file)) return [];
    const raw = fs.readFileSync(file, 'utf-8');
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch (err) {
    console.warn(`[server] Failed to read ${file}: ${err.message}`);
    return [];
  }
}

function writeStoreFile(file, value) {
  try {
    fs.writeFileSync(file, JSON.stringify(value, null, 2));
  } catch (err) {
    console.error(`[server] Failed to write ${file}: ${err.message}`);
  }
}

function isIsoTimestamp(value) {
  return typeof value === 'string' && !Number.isNaN(Date.parse(value));
}

function normalizeKbScope(scope) {
  if (scope?.mode === 'none') return { mode: 'none', kb_ids: [] };
  const mode = scope?.mode === 'all' ? 'all' : 'selected';
  const kbIds =
    mode === 'all'
      ? []
      : Array.isArray(scope?.kb_ids)
          ? scope.kb_ids.filter((id) => typeof id === 'string' && id.trim())
          : [];
  return { mode, kb_ids: kbIds };
}

function getActionOperationKey(type, payload = {}) {
  if (type === 'library.bulk') {
    const op = typeof payload.operation === 'string' && payload.operation.trim() ? payload.operation.trim() : 'generic';
    return `${type}:${op}`;
  }
  return type || 'unknown';
}

function normalizePolicySnapshot(snapshot = {}, action = {}) {
  snapshot = snapshot && typeof snapshot === 'object' ? snapshot : {};
  const now = new Date().toISOString();
  const allowedOpsRaw = Array.isArray(snapshot.allowed_operations)
    ? snapshot.allowed_operations.filter((op) => typeof op === 'string' && op.trim())
    : [];
  const opKey = getActionOperationKey(action.type, action.payload);
  if (opKey && !allowedOpsRaw.includes(opKey)) allowedOpsRaw.push(opKey);

  return {
    created_by:
      typeof snapshot.created_by === 'string' && snapshot.created_by.trim()
        ? snapshot.created_by.trim()
        : 'noetix-ui-local-policy',
    approval_required: snapshot.approval_required === false ? false : true,
    allowed_operations: [...new Set(allowedOpsRaw)],
    risk_level:
      typeof snapshot.risk_level === 'string' && snapshot.risk_level.trim() ? snapshot.risk_level.trim() : 'moderate',
    created_at: isIsoTimestamp(snapshot.created_at) ? new Date(snapshot.created_at).toISOString() : now
  };
}

function mapRemoteStatusToLocal(status) {
  if (status === 'pending') return 'proposed';
  return status;
}

function normalizeRemoteActionRecord(remote) {
  const targets = remote?.payload?.targets || [];
  const first = targets[0] || {};
  const operations = [...new Set(targets.map((t) => t.operation).filter(Boolean))];
  const operation = operations.length === 1 ? operations[0] : operations[0] || 'unknown';
  const payload = {
    operation,
    item_ids: targets.map((t) => t.item_id).filter(Boolean)
  };
  if (operation === 'reindex') payload.kb_id = first?.params?.kb_id || null;
  if (operation === 'retag') payload.tags = first?.params?.tags || {};

  const cached = actionRequests.get(remote.id) || {};

  return {
    id: remote.id,
    type: remote?.payload?.type || 'library.bulk',
    title: remote.title || 'Action Request',
    payload,
    status: mapRemoteStatusToLocal(remote.status),
    chat_session_id: cached.chat_session_id || null,
    message_id: cached.message_id || null,
    approved_by: remote.reviewed_by || null,
    policy_snapshot: cached.policy_snapshot || null,
    result: remote.result || null,
    created_at: remote.created_at,
    updated_at: remote.updated_at
  };
}

function buildRemoteCreateRequestFromLocal(body) {
  const payload = body?.payload || {};
  const op = payload.operation;
  const itemIds = Array.isArray(payload.item_ids) ? payload.item_ids : [];
  const targets = itemIds.map((itemId) => {
    const params = {};
    if (op === 'reindex' && payload.kb_id) params.kb_id = payload.kb_id;
    if (op === 'retag' && payload.tags) params.tags = payload.tags;
    return { item_id: itemId, operation: op, params };
  });

  return {
    title: body?.title || `Library bulk ${op}`,
    description: '',
    requested_by: 'noetix-ui',
    payload: {
      type: body?.type || 'library.bulk',
      targets,
      params: {}
    }
  };
}

function hydrateSessions() {
  const map = new Map();
  for (const record of readStoreFile(CHAT_SESSIONS_FILE)) {
    if (!record?.id) continue;
    const created = isIsoTimestamp(record.created_at) ? new Date(record.created_at).toISOString() : new Date().toISOString();
    const updated = isIsoTimestamp(record.updated_at) ? new Date(record.updated_at).toISOString() : created;
    map.set(record.id, {
      id: record.id,
      title: typeof record.title === 'string' && record.title.trim() ? record.title.trim() : 'Untitled Session',
      default_kb_scope: normalizeKbScope(record.default_kb_scope),
      // Don't persist codex thread IDs across server restarts — they're ephemeral
      codexThreadId: null,
      created_at: created,
      updated_at: updated,
      messages: Array.isArray(record.messages) ? record.messages : []
    });
  }
  return map;
}

function hydrateActionRequests() {
  const map = new Map();
  for (const record of readStoreFile(ACTION_REQUESTS_FILE)) {
    if (!record?.id) continue;
    const created = isIsoTimestamp(record.created_at) ? new Date(record.created_at).toISOString() : new Date().toISOString();
    const updated = isIsoTimestamp(record.updated_at) ? new Date(record.updated_at).toISOString() : created;
    const payload = record && typeof record.payload === 'object' && record.payload ? record.payload : {};
    map.set(record.id, {
      id: record.id,
      type: record.type || 'library.bulk',
      title: typeof record.title === 'string' && record.title.trim() ? record.title.trim() : 'Action Request',
      payload,
      status: record.status || 'proposed',
      chat_session_id: record.chat_session_id || null,
      message_id: record.message_id || null,
      approved_by: record.approved_by || null,
      policy_snapshot: normalizePolicySnapshot(record.policy_snapshot, { type: record.type, payload }),
      result: record.result || null,
      created_at: created,
      updated_at: updated
    });
  }
  return map;
}

const sessions = hydrateSessions();
const actionRequests = hydrateActionRequests();

// KB dependencies: { [kb_id]: [dep_kb_id, ...] }
function hydrateKbDeps() {
  try {
    if (!fs.existsSync(KB_DEPS_FILE)) return {};
    const raw = JSON.parse(fs.readFileSync(KB_DEPS_FILE, 'utf-8'));
    return raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
  } catch { return {}; }
}
let kbDeps = hydrateKbDeps();
function persistKbDeps() { writeStoreFile(KB_DEPS_FILE, kbDeps); }

/** Expand KB IDs by recursively including dependencies. */
function expandKbDeps(kbIds) {
  const result = new Set(kbIds);
  const queue = [...kbIds];
  while (queue.length) {
    const id = queue.shift();
    const deps = kbDeps[id];
    if (!Array.isArray(deps)) continue;
    for (const dep of deps) {
      if (!result.has(dep)) { result.add(dep); queue.push(dep); }
    }
  }
  return [...result];
}

function persistSessions() {
  writeStoreFile(CHAT_SESSIONS_FILE, [...sessions.values()]);
}

function persistActionRequests() {
  writeStoreFile(ACTION_REQUESTS_FILE, [...actionRequests.values()]);
}

const app = express();
app.use(express.json({ limit: '10mb' }));
app.use(
  cors({
    origin: config.corsOrigins,
    credentials: true
  })
);

const upload = multer({ dest: config.uploadTmpDir });

const REMOTE_BASE = config.remoteBase;
const SOCKS_PROXY = config.socksProxy;
const ACTIONS_MODE = config.actionsMode;
const PLAYWRIGHT_MCP_URL = config.playwrightMcpUrl;
const port = config.port;
const host = config.host;

const socksAgent = SOCKS_PROXY ? new SocksProxyAgent(SOCKS_PROXY) : null;

// Initialize agent runner (LLM + MCP)
const codex = new AgentRunner();
codex.init().catch((err) => {
  console.error(`[server] Failed to start agent: ${err.message}`);
});

/**
 * Playwright MCP recovery helper.
 * With stdio transport (configured in config.toml), Playwright MCP is managed
 * by the codex app-server process. Restarting codex brings up a fresh Playwright
 * MCP automatically. This function just kills any orphaned HTTP-mode process on
 * port 8931 if one exists, to avoid port conflicts.
 */
async function cleanupPlaywrightMcp() {
  try {
    execSync('kill $(lsof -t -i :8931) 2>/dev/null || true', { timeout: 5000 });
    console.log('[server] Cleaned up orphaned Playwright MCP on port 8931');
  } catch { /* nothing on that port — normal for stdio mode */ }
}

function errorEnvelope(code, message, retryable = false, details = null, correlationId = null) {
  return { code, message, retryable, details, correlationId };
}

async function remoteRequest({ req, method = 'GET', path, body, headers = {}, responseType }) {
  const correlationId = req?.headers?.['x-correlation-id'] || crypto.randomUUID();
  const response = await axios({
    baseURL: REMOTE_BASE,
    url: path,
    method,
    data: body,
    timeout: config.backendTimeout,
    proxy: false,
    httpAgent: socksAgent || undefined,
    httpsAgent: socksAgent || undefined,
    headers: { 'x-correlation-id': correlationId, ...headers },
    responseType,
    validateStatus: () => true
  });

  return { correlationId, status: response.status, data: response.data, headers: response.headers };
}

async function forwardJson(req, res, { method = 'GET', path, body }) {
  const correlationId = req.headers['x-correlation-id'] || crypto.randomUUID();
  try {
    const result = await remoteRequest({
      req,
      method,
      path,
      body,
      headers: { 'Content-Type': 'application/json' }
    });
    res.setHeader('x-correlation-id', result.correlationId);
    return res.status(result.status).json(result.data);
  } catch (err) {
    return res.status(502).json(
      errorEnvelope(
        'UPSTREAM_UNREACHABLE',
        `Failed to reach remote backend at ${REMOTE_BASE}`,
        true,
        { reason: String(err?.message || err), socks_proxy: SOCKS_PROXY || null },
        correlationId
      )
    );
  }
}

function buildLibraryQueryString(query) {
  const allowed = ['organization', 'course_code', 'course_id', 'module_id', 'topic_id', 'custom_tag', 'has_kb'];
  const params = new URLSearchParams();
  for (const key of allowed) {
    const value = query[key];
    if (value !== undefined && value !== '') params.append(key, value);
  }
  const qs = params.toString();
  return qs ? `?${qs}` : '';
}

async function listAllKbIds(req) {
  const result = await remoteRequest({ req, path: '/api/knowledge-bases' });
  if (result.status < 200 || result.status >= 300 || !Array.isArray(result.data)) return [];
  return result.data.map((k) => k.id).filter(Boolean);
}

/**
 * Fetch all accessible KB entries with title + author metadata.
 * Tries /v1/knowledge-bases first (self-call avoided; hits backend directly),
 * then falls back to /api/knowledge-bases, and finally per-KB document listings.
 * Returns { entries: [{ kb_id, title, author, document_count }], source: string }.
 */
async function listKbEntries(req, kbIds) {
  // Strategy 1: Fetch KB-level metadata from backend
  try {
    const result = await remoteRequest({ req, path: '/api/knowledge-bases' });
    if (result.status >= 200 && result.status < 300 && Array.isArray(result.data) && result.data.length) {
      const filtered = kbIds.length
        ? result.data.filter((kb) => kbIds.includes(kb.id))
        : result.data;
      const entries = filtered.map((kb) => ({
        kb_id: kb.id,
        title: kb.title || kb.name || '(untitled)',
        author: kb.author || kb.created_by || 'Unknown',
        document_count: kb.document_count ?? kb.doc_count ?? null
      }));
      if (entries.length) return { entries, source: '/api/knowledge-bases' };
    }
  } catch (err) {
    console.warn(`[server] listKbEntries: /api/knowledge-bases failed: ${err.message}`);
  }

  // Strategy 2: For each resolved KB, fetch document-level entries
  const targetIds = kbIds.length ? kbIds : await listAllKbIds(req);
  const entries = [];
  for (const kbId of targetIds) {
    try {
      const docsRes = await remoteRequest({ req, path: `/api/knowledge-bases/${kbId}/documents` });
      if (docsRes.status >= 200 && docsRes.status < 300 && Array.isArray(docsRes.data)) {
        for (const doc of docsRes.data.slice(0, 200)) {
          entries.push({
            kb_id: kbId,
            title: doc.title || '(untitled)',
            author: doc.author || 'Unknown',
            document_count: null
          });
        }
      }
    } catch (err) {
      console.warn(`[server] listKbEntries: /api/knowledge-bases/${kbId}/documents failed: ${err.message}`);
    }
  }

  if (entries.length) return { entries, source: 'per-kb-documents' };
  return { entries: [], source: 'none' };
}

const KB_LISTING_PATTERN = /\b(list|show|what|which|all|every|title|author|document|documents|catalog|catalogue|entries|books?|texts?|materials?)\b/i;

/**
 * Detect whether an agent response is a "refusal" — the agent claims it cannot
 * fulfil the request rather than actually answering.  Used to trigger server-
 * level fallbacks when the data is available locally.
 */
const REFUSAL_PATTERN = /\b(I (can'?t|cannot|don'?t have|do not have|am unable to|lack|don'?t know how to)|not (available|accessible|possible|supported)|no (direct|way|access|ability)|outside (my|this)|beyond (my|this))\b/i;

async function resolveKbIds(scope, session, req) {
  let ids = [];
  const messageKbIds = scope?.kb_ids || [];
  if (Array.isArray(messageKbIds) && messageKbIds.length > 0) {
    ids = messageKbIds;
  } else {
    const sessionKbIds = session?.default_kb_scope?.kb_ids || [];
    if (Array.isArray(sessionKbIds) && sessionKbIds.length > 0) {
      ids = sessionKbIds;
    } else if (scope?.mode === 'all' || session?.default_kb_scope?.mode === 'all') {
      return await listAllKbIds(req);
    }
  }
  // Expand dependencies recursively
  return ids.length ? expandKbDeps(ids) : [];
}

app.get('/health', (_req, res) =>
  res.json({
    ok: true,
    remote_base: REMOTE_BASE,
    socks_proxy: SOCKS_PROXY || null,
    actions_mode: ACTIONS_MODE,
    chat_backend: config.llmProvider,
    agent_ready: codex.isReady,
    playwright_mcp_url: PLAYWRIGHT_MCP_URL
  })
);

// Auth status / login / logout
app.get('/v1/auth/status', (_req, res) => {
  const provider = config.llmProvider;
  const method = config.llmAuthMethod;
  // If using api_key mode with a key in config/env, auth is fine
  if (method === 'api_key' && config.llmApiKey) {
    return res.json({ authenticated: true, provider, method });
  }
  // If using oauth mode with env var token, auth is fine
  if (method === 'oauth' && config.llmAuthToken) {
    return res.json({ authenticated: true, provider, method });
  }
  // Check credential store
  const creds = getProviderCredentials(provider);
  if (method === 'oauth' && creds?.token) {
    return res.json({ authenticated: true, provider, method });
  }
  if (method === 'api_key' && creds?.apiKey) {
    return res.json({ authenticated: true, provider, method });
  }
  return res.json({ authenticated: false, provider, method });
});

app.post('/v1/auth/login', async (req, res) => {
  const { method, token, apiKey, provider: reqProvider } = req.body || {};
  const provider = reqProvider || config.llmProvider;

  if (method === 'oauth' || method === 'bearer') {
    if (!token) return res.status(400).json({ error: 'Token is required' });
    setProviderCredentials(provider, { type: 'oauth', token });
  } else {
    if (!apiKey) return res.status(400).json({ error: 'API key is required' });
    setProviderCredentials(provider, { type: 'api_key', apiKey });
  }

  // Fetch and cache available models
  const authToken = apiKey || token;
  if (provider === 'openai' && authToken) {
    try {
      const modelIds = await fetchProviderModels('openai', authToken);
      if (modelIds.length) setProviderCredentials(provider, { availableModels: modelIds });
    } catch {}
  }

  // Reinitialize agent runner with new credentials
  try {
    await codex.restart();
    res.json({ ok: true, provider, method: method || 'api_key' });
  } catch (err) {
    res.status(500).json({ ok: false, error: `Agent restart failed: ${err.message}` });
  }
});

app.post('/v1/auth/logout', async (_req, res) => {
  const provider = config.llmProvider;
  clearProviderCredentials(provider);
  try {
    await codex.shutdown();
  } catch {}
  res.json({ ok: true, provider });
});

// Model listing and selection
const ANTHROPIC_MODELS = [
  'claude-sonnet-4-6-20250514', 'claude-opus-4-6-20250514',
  'claude-haiku-4-5-20251001', 'claude-sonnet-4-5-20250514',
];

app.get('/v1/models', async (_req, res) => {
  const models = [];
  const activeProvider = config.llmProvider;
  const activeModel = codex.model;

  // OpenAI — use cached models from credential store (populated at login)
  const openaiCreds = getProviderCredentials('openai');
  if (openaiCreds) {
    let modelIds = openaiCreds.availableModels;

    // If no cached models, try fetching now
    if (!modelIds?.length) {
      const token = openaiCreds.apiKey || openaiCreds.token;
      if (token) {
        try {
          modelIds = await fetchProviderModels('openai', token);
          if (modelIds.length) {
            setProviderCredentials('openai', { availableModels: modelIds });
          }
        } catch {}
      }
    }

    if (modelIds?.length) {
      for (const id of modelIds) {
        models.push({ provider: 'openai', model: id, active: activeProvider === 'openai' && activeModel === id });
      }
    }
  }

  // Anthropic — static list (no public list API), show if we have credentials
  const anthropicCreds = getProviderCredentials('anthropic');
  if (anthropicCreds?.apiKey || anthropicCreds?.token || (activeProvider === 'anthropic' && config.llmApiKey)) {
    for (const id of ANTHROPIC_MODELS) {
      models.push({ provider: 'anthropic', model: id, active: activeProvider === 'anthropic' && activeModel === id });
    }
  }

  // Ollama — query local server (no auth needed)
  try {
    const ollamaBase = config.llmProvider === 'ollama' ? (config.llmBaseUrl || 'http://localhost:11434') : 'http://localhost:11434';
    const resp = await fetch(`${ollamaBase}/api/tags`, { signal: AbortSignal.timeout(2000) });
    if (resp.ok) {
      const data = await resp.json();
      for (const m of (data.models || [])) {
        models.push({ provider: 'ollama', model: m.name, active: activeProvider === 'ollama' && activeModel === m.name });
      }
    }
  } catch {}

  // vLLM — query if configured
  if (activeProvider === 'vllm' && config.llmBaseUrl) {
    try {
      const resp = await fetch(`${config.llmBaseUrl}/models`, { signal: AbortSignal.timeout(3000) });
      if (resp.ok) {
        const data = await resp.json();
        for (const m of (data.data || [])) {
          models.push({ provider: 'vllm', model: m.id, active: activeProvider === 'vllm' && activeModel === m.id });
        }
      }
    } catch {}
  }

  res.json({ models, active: { provider: activeProvider, model: activeModel } });
});

app.post('/v1/models/select', async (req, res) => {
  const { provider, model } = req.body || {};
  if (!provider || !model) return res.status(400).json({ error: 'provider and model are required' });

  // Update config in memory
  config.llmProvider = provider;
  config.llmModel = model;

  // Restart agent with new model
  try {
    await codex.restart();
    res.json({ ok: true, provider, model });
  } catch (err) {
    res.status(500).json({ ok: false, error: `Failed to switch model: ${err.message}` });
  }
});

// OAuth browser flow (PKCE)
const pendingOAuthFlows = new Map();

app.get('/v1/auth/oauth/start', (req, res) => {
  const provider = req.query.provider || config.llmProvider;
  if (!OAUTH_PROVIDERS[provider]) {
    return res.status(400).json({ error: `No OAuth config for provider: ${provider}` });
  }

  const { url, codeVerifier, state } = buildAuthorizationUrl(provider);

  // Start callback server to receive the redirect
  const callback = startCallbackServer(1455);
  pendingOAuthFlows.set(state, { provider, codeVerifier, callback, startedAt: Date.now() });

  // Auto-cleanup after 3 minutes
  setTimeout(() => {
    if (pendingOAuthFlows.has(state)) {
      pendingOAuthFlows.get(state).callback.close();
      pendingOAuthFlows.delete(state);
    }
  }, 180000);

  // Listen for the callback in the background
  callback.promise.then(async ({ code, state: returnedState }) => {
    const flow = pendingOAuthFlows.get(returnedState);
    if (!flow) return;

    try {
      const tokens = await exchangeCodeForTokens(flow.provider, code, flow.codeVerifier);
      const claims = parseJwtClaims(tokens.idToken || tokens.accessToken);

      // For OpenAI: exchange id_token for API key usable at api.openai.com
      let apiKey;
      if (flow.provider === 'openai' && tokens.idToken) {
        try {
          apiKey = await exchangeIdTokenForApiKey(tokens.idToken, config.llmOrganizationId);
        } catch (err) {
          console.error(`[server] API key exchange failed: ${err.message}`);
        }
      }

      const cfg = OAUTH_PROVIDERS[flow.provider];
      setProviderCredentials(flow.provider, {
        type: 'oauth',
        apiKey: apiKey || undefined,
        token: tokens.accessToken,
        idToken: tokens.idToken,
        refreshToken: tokens.refreshToken,
        expiresAt: tokens.expiresIn ? Date.now() + tokens.expiresIn * 1000 : undefined,
        tokenEndpoint: cfg.tokenEndpoint,
        clientId: cfg.clientId,
        accountId: claims?.['https://api.openai.com/auth']?.chatgpt_account_id || undefined,
      });

      // Fetch and cache available models
      const authToken = apiKey || tokens.accessToken;
      if (flow.provider === 'openai' && authToken) {
        try {
          const modelIds = await fetchProviderModels('openai', authToken);
          if (modelIds.length) {
            setProviderCredentials('openai', { availableModels: modelIds });
          }
        } catch (err) {
          console.error(`[server] Model fetch after OAuth failed: ${err.message}`);
        }
      }

      // Reinitialize agent with new credentials
      try {
        await codex.restart();
      } catch (reinitErr) {
        console.error(`[server] Agent restart after OAuth failed: ${reinitErr.message}`);
      }

      flow.completed = true;
      flow.email = claims.email;
    } catch (err) {
      flow.error = err.message;
    }
  }).catch((err) => {
    const flow = pendingOAuthFlows.get(state);
    if (flow) flow.error = err.message;
  });

  res.json({ url, state });
});

app.get('/v1/auth/oauth/poll', (req, res) => {
  const { state } = req.query;
  const flow = pendingOAuthFlows.get(state);
  if (!flow) return res.json({ completed: false, error: 'Unknown flow' });
  if (flow.error) {
    pendingOAuthFlows.delete(state);
    return res.json({ completed: false, error: flow.error });
  }
  if (flow.completed) {
    pendingOAuthFlows.delete(state);
    return res.json({ completed: true, email: flow.email });
  }
  return res.json({ completed: false });
});

// Playwright MCP diagnostics: detect live endpoint and bind status
app.get('/v1/playwright-mcp/status', async (_req, res) => {
  try {
    const detection = await detectLiveEndpoint(PLAYWRIGHT_MCP_URL);
    const message = formatDiagnostics(detection);
    return res.json({
      ok: !!detection.live,
      configured_url: PLAYWRIGHT_MCP_URL,
      live_endpoint: detection.live
        ? { url: detection.live.url, session_id: detection.live.sessionId, latency_ms: detection.live.latencyMs }
        : null,
      diagnostics: detection.diagnostics,
      message
    });
  } catch (err) {
    return res.status(500).json({
      ok: false,
      configured_url: PLAYWRIGHT_MCP_URL,
      live_endpoint: null,
      diagnostics: [],
      message: `Playwright MCP detection failed: ${err.message}`
    });
  }
});

// Analyze endpoint — single-turn LLM call via Codex for PDF structure extraction
app.post('/api/analyze', async (req, res) => {
  const { prompt } = req.body || {};
  if (!prompt || typeof prompt !== 'string') {
    return res.status(400).json({ error: 'Missing required field: prompt' });
  }

  try {
    await codex.ensureReady();
    const conv = await codex.createThread('You are a document structure analyzer. Return ONLY the requested JSON, no markdown, no explanation.');
    const result = await codex.sendTurn(conv.threadId, prompt);
    return res.json({ result: result.text || '' });
  } catch (err) {
    console.error(`[server] /api/analyze failed: ${err.message}`);
    return res.status(502).json({ error: `Analysis failed: ${err.message}` });
  }
});

// Back-compat passthroughs
app.get('/api/knowledge-bases', async (req, res) => forwardJson(req, res, { path: '/api/knowledge-bases' }));
app.post('/api/chat', async (req, res) => forwardJson(req, res, { method: 'POST', path: '/api/chat', body: req.body }));

// v1 KB
app.get('/v1/knowledge-bases', async (req, res) => forwardJson(req, res, { path: '/api/knowledge-bases' }));

// KB dependencies (must be before /:id routes)
app.get('/v1/knowledge-bases/dependencies', (_req, res) => res.json(kbDeps));

app.put('/v1/knowledge-bases/:id/dependencies', (req, res) => {
  const depIds = Array.isArray(req.body?.dependencies) ? req.body.dependencies.filter(id => typeof id === 'string' && id.trim()) : [];
  kbDeps[req.params.id] = depIds;
  persistKbDeps();
  return res.json({ kb_id: req.params.id, dependencies: depIds });
});

app.get('/v1/knowledge-bases/:id/documents', async (req, res) =>
  forwardJson(req, res, { path: `/api/knowledge-bases/${req.params.id}/documents` }));

app.delete('/v1/knowledge-bases/:id', async (req, res) =>
  forwardJson(req, res, { method: 'DELETE', path: `/api/knowledge-bases/${req.params.id}` }));

app.patch('/v1/knowledge-bases/:id', async (req, res) =>
  forwardJson(req, res, { method: 'PATCH', path: `/api/knowledge-bases/${req.params.id}`, body: req.body }));

app.delete('/v1/knowledge-bases/:kbId/documents/:docId', async (req, res) =>
  forwardJson(req, res, { method: 'DELETE', path: `/api/knowledge-bases/${req.params.kbId}/documents/${req.params.docId}` }));

// v1 chat sessions
app.post('/v1/chat/sessions', (req, res) => {
  let title = typeof req.body?.title === 'string' ? req.body.title.trim() : '';
  if (req.body?.title !== undefined && !title) {
    return res.status(400).json(errorEnvelope('VALIDATION_ERROR', 'title cannot be blank'));
  }
  if (!title) title = 'Untitled Session';
  const id = `s_${crypto.randomUUID()}`;
  const now = new Date().toISOString();
  const session = {
    id,
    title,
    default_kb_scope: normalizeKbScope(req.body?.default_kb_scope || {}),
    created_at: now,
    updated_at: now,
    messages: []
  };
  sessions.set(id, session);
  persistSessions();
  return res.status(201).json({
    id: session.id,
    title: session.title,
    default_kb_scope: session.default_kb_scope,
    created_at: now,
    updated_at: now
  });
});

app.get('/v1/chat/sessions', (_req, res) => {
  const items = [...sessions.values()]
    .sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1))
    .map((s) => ({
      id: s.id,
      title: s.title,
      default_kb_scope: s.default_kb_scope,
      created_at: s.created_at,
      updated_at: s.updated_at
    }));
  return res.json({ items });
});

app.get('/v1/chat/sessions/:sessionId', (req, res) => {
  const session = sessions.get(req.params.sessionId);
  if (!session) return res.status(404).json(errorEnvelope('NOT_FOUND', 'Chat session not found'));
  return res.json({
    id: session.id,
    title: session.title,
    default_kb_scope: session.default_kb_scope,
    created_at: session.created_at,
    updated_at: session.updated_at
  });
});

app.patch('/v1/chat/sessions/:sessionId', (req, res) => {
  const session = sessions.get(req.params.sessionId);
  if (!session) return res.status(404).json(errorEnvelope('NOT_FOUND', 'Chat session not found'));
  if (req.body?.title !== undefined) {
    const nextTitle = typeof req.body.title === 'string' ? req.body.title.trim() : '';
    if (!nextTitle) return res.status(400).json(errorEnvelope('VALIDATION_ERROR', 'title cannot be blank'));
    session.title = nextTitle;
  }
  if (req.body?.default_kb_scope) session.default_kb_scope = normalizeKbScope(req.body.default_kb_scope);
  session.updated_at = new Date().toISOString();
  persistSessions();
  return res.json({
    id: session.id,
    title: session.title,
    default_kb_scope: session.default_kb_scope,
    created_at: session.created_at,
    updated_at: session.updated_at
  });
});

app.get('/v1/chat/sessions/:sessionId/messages', (req, res) => {
  const session = sessions.get(req.params.sessionId);
  if (!session) return res.status(404).json(errorEnvelope('NOT_FOUND', 'Chat session not found'));
  return res.json({ items: session.messages });
});

app.delete('/v1/chat/sessions/:sessionId', (req, res) => {
  const exists = sessions.has(req.params.sessionId);
  if (!exists) return res.status(404).json(errorEnvelope('NOT_FOUND', 'Chat session not found'));
  sessions.delete(req.params.sessionId);
  persistSessions();

  // best-effort cleanup of cached action mappings for this session
  for (const [id, action] of actionRequests.entries()) {
    if (action?.chat_session_id === req.params.sessionId) actionRequests.delete(id);
  }
  persistActionRequests();

  return res.json({ ok: true, deleted_session_id: req.params.sessionId });
});

app.post('/v1/chat/sessions/:sessionId/messages', async (req, res) => {
  const session = sessions.get(req.params.sessionId);
  if (!session) return res.status(404).json(errorEnvelope('NOT_FOUND', 'Chat session not found'));

  const content = req.body?.content?.trim();
  if (!content) return res.status(400).json(errorEnvelope('VALIDATION_ERROR', 'content is required'));

  const normalizedScope = normalizeKbScope(req.body?.kb_scope);
  const kbIds = normalizedScope.mode === 'none' ? [] : await resolveKbIds(req.body?.kb_scope, session, req);

  const userMessage = { id: `m_${crypto.randomUUID()}`, role: 'user', content, created_at: new Date().toISOString() };
  session.messages.push(userMessage);
  session.updated_at = userMessage.created_at;
  persistSessions();

  const providedTopK = Number(req.body?.kb_scope?.top_k);
  const topK = Number.isFinite(providedTopK) && providedTopK > 0 ? Math.min(providedTopK, 20) : 8;

  // Reasoning effort: validate against protocol enum, default to null (use server config)
  // Floor at 'medium' — lower values ('none','minimal','low') cause API errors with tools like web_search
  const VALID_EFFORTS = ['medium', 'high', 'xhigh'];
  const effort = VALID_EFFORTS.includes(req.body?.effort) ? req.body.effort : null;

  // --- SSE streaming mode ---
  const wantsSSE = (req.headers.accept || '').includes('text/event-stream');
  let sseOpen = false;
  let sseWrite;
  if (wantsSSE) {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
      'X-Accel-Buffering': 'no',
      'Access-Control-Allow-Origin': req.headers.origin || '*'
    });
    res.flushHeaders();
    sseOpen = true;
    req.on('close', () => { sseOpen = false; });
    sseWrite = (event, data) => {
      if (!sseOpen) return;
      try {
        res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
      } catch (e) {
        sseOpen = false;
      }
    };
  }

  // onDelta callback — only active in SSE mode
  const onDelta = wantsSSE ? (delta) => {
    if (!sseOpen) return;
    sseWrite(delta.type, delta);
  } : undefined;

  try {
    // --- Codex app-server chat handler ---
    // Try to ensure codex is running (re-init if it crashed)
    const codexReady = codex.isReady || await codex.ensureReady();

    if (codexReady) {
      let systemInstructions = [
        `You are the Noetix assistant — a general-purpose AI with access to knowledge bases, a web browser, and content processing tools.`,
        ``,
        `CORE AGENT BEHAVIOR:`,
        `- Keep going until the user's request is completely resolved before ending your turn.`,
        `- Only finish when you are sure that the task is fully solved.`,
        `- Autonomously resolve the request to the best of your ability using the tools available to you.`,
        `- Do NOT guess or make up an answer. Only report what you actually observed or read.`,
        `- If you cannot complete a step, try alternative approaches — do not stop and report failure.`,
        ``,
        `TOOLS AVAILABLE:`,
        `- Memory tools: kb_list, kb_search, kb_documents — for querying the user's knowledge bases`,
        `- Browser tools: browser_navigate, browser_snapshot, browser_click, browser_type, browser_tab_list, browser_select_option, browser_take_screenshot — for browsing the web`,
        `- Content tools: content_list_downloads, content_upload, content_status, content_voices, content_kb_create, content_library_list, content_library_tag, content_library_metadata, content_library_ingest, content_library_delete, content_stage, content_staged_list, content_staged_clear, content_batch_convert — for converting files, managing the content library, and batch processing`,
        `- You ALWAYS have these tools. Never say "I don't have access" or "I can't browse" — just call the tool.`,
        ``,
        `EXECUTION RULES:`,
        `- ALWAYS complete the full task. Do NOT stop partway through.`,
        `- When a task requires multiple steps (navigate → click → read → extract), do ALL steps in one turn.`,
        `- NEVER say "Shall I continue?", "send a follow-up", "please try again", "if you want I can...", or "I can't access..." — just call the tools and finish.`,
        `- NEVER ask the user for a follow-up message. NEVER offer to do more — just DO it. You must complete the entire task in a single turn.`,
        `- If a PDF viewer is loading or content is incomplete, keep trying (re-snapshot, try download URL) until you have the actual content. Do NOT return partial results.`,
        `- If one approach fails, immediately try an alternative WITHOUT telling the user about the failure.`,
        `- After completing tool calls, provide a detailed text summary of what you found.`,
        ``,
        `ANTI-FABRICATION (CRITICAL):`,
        `- When the user asks about content in their knowledge bases or on specific web pages, you MUST use the appropriate tools to find the actual information.`,
        `- NEVER answer questions about knowledge base content from memory or training data — always use kb_search or kb_documents first.`,
        `- NEVER answer questions about web page content without using browser tools first.`,
        `- If you respond without calling the relevant tools, your response WILL be wrong.`,
        ``,
        `VERIFICATION — BEFORE YOU RESPOND, CHECK:`,
        `- Did I actually complete the task, or am I reporting a partial result?`,
        `- If the user asked for content from a document or web page, did I provide the ACTUAL content (not just the title/metadata)?`,
        `- Am I offering to "continue" or asking "would you like me to..." instead of just doing it?`,
        `- If any check fails, go back and fix it before responding.`,
        ``,
        `WEB BROWSING:`,
        `- If the user specifies a particular interface or website (e.g. "check my Canvas assignments", "look at my Gmail"), use the browser tools to navigate there and perform the task — the user's browser session has their logins.`,
        `- If the user asks for information that requires authentication (email, school portals, work tools, shopping history, bank, etc.) but doesn't specify where, use the browser tools — the user's browser has active sessions for these services.`,
        `- If the request does NOT require login and no specific site is given (e.g. "research topic X", "find information about Y"), prefer lightweight approaches first: use your own knowledge, query the user's knowledge bases, or use web search tools if available. Only open the browser when you need to read specific pages or interact with a site.`,
        `- When using the browser, you are operating the user's actual browser with their real sessions and cookies. You can access any site they are logged into.`,
        `- If a link leads to a PDF, take a browser_snapshot to read the rendered content.`,
        ``,
        `PDF AND DOCUMENT HANDLING:`,
        `- When you find a link to a PDF or document, CLICK IT or NAVIGATE to its URL.`,
        `- After clicking a PDF link, take a browser_snapshot to read its contents.`,
        `- If the first snapshot shows the PDF viewer is still loading (toolbar visible but no text), take a SECOND browser_snapshot — the text needs a moment to render.`,
        `- If the PDF text still doesn't appear after 2 snapshots, try browser_take_screenshot to capture the visual rendering.`,
        `- If a "Download" button appears instead of content, try browser_snapshot first (PDF may already be rendered).`,
        `- If the PDF opens in a viewer, take a snapshot to extract text from the visible page.`,
        `- Never report just the title/metadata of a document — always open it and read the actual content.`,
        `- If you cannot read the PDF in the viewer, try navigating directly to the PDF download URL (look for download links in the page).`,
        `- CRITICAL: If you cannot extract text from a PDF, say so honestly. NEVER fabricate or guess the document's contents.`,
        ``,
        `FAILURE RECOVERY:`,
        `- If a browser_click fails, take a fresh browser_snapshot and try a different element or approach.`,
        `- If a page doesn't load, try navigating directly to the URL.`,
        `- If a tool returns an error, retry it once, then try an alternative approach.`,
        `- NEVER give up after a single tool failure — always try at least 2-3 approaches.`,
        `- If a PDF viewer shows loading/empty content, try these in order: (1) take another snapshot after a pause, (2) try browser_take_screenshot, (3) look for a direct download link and navigate to it, (4) look for the file URL in the page source/links.`,
        ``,
        `BROWSER RECOVERY:`,
        `- Playwright MCP runs as a stdio subprocess — it restarts automatically if the server reconnects.`,
        `- If browser tools return errors, retry the tool call immediately. The connection is usually transient.`,
        `- If repeated retries fail, try alternative approaches (different URLs, direct download links, etc).`,
        ``,
        `CONTENT CONVERSION & INGESTION:`,
        `- The Noetix backend can convert between formats:`,
        `  - PDF → audiobook (M4B), searchable PDF, knowledge base`,
        `  - Audio (MP3/M4B/M4A) → transcription → PDF, knowledge base`,
        `  - Video (MP4/MKV/WebM) → transcription+visual analysis → audiobook, PDF, knowledge base`,
        `  - ZIP of videos → batch process each video`,
        `  - Multiple PDFs → combined PDF`,
        `- Workflow for downloading and processing content:`,
        `  1. Use browser tools to find and download the file (saved to ${config.downloadsDir})`,
        `  2. Call content_list_downloads to get the file path`,
        `  3. Call content_upload with the file path and desired outputs (e.g. outputs="m4b,knowledge_base")`,
        `     - source_type is auto-detected from the file extension`,
        `     - Provide kb_id or kb_name for KB ingestion`,
        `     - Provide voice for audiobook output (call content_voices to see options)`,
        `  4. Call content_status with the job_id to check progress — poll until completed`,
        `  5. Report results to the user`,
        `- For multi-video combined documents (e.g. all lesson videos → one combined PDF + KB):`,
        `  1. Download all videos via browser tools`,
        `  2. Call content_list_downloads to find the files`,
        `  3. Call content_stage for EACH file in order, naming them sequentially (e.g. "01_Introduction.mp4", "02_Topic.mp4")`,
        `  4. Call content_staged_list to verify order and completeness`,
        `  5. Call content_batch_convert with title, outputs, and optional kb_id/kb_name`,
        `     - This creates a ZIP and uploads as an archive — the backend processes all videos together into one combined document with global chapter numbering`,
        `  6. Poll content_status until completed`,
        `- ALWAYS use batch staging (content_stage + content_batch_convert) for multiple videos that belong to the same lesson/lecture — NEVER upload videos individually when a combined document is needed`,
        `- To organize content: content_library_list to browse, content_library_tag or content_library_metadata to update`,
        `- To reprocess existing content into a different KB: content_library_ingest`,
        `- To delete unwanted or duplicate content: content_library_delete`,
        `- NEVER fabricate job IDs, progress, or conversion results — always call content_status for real data`,
      ].join('\n');

      // Scope KB tools to selected KBs
      if (kbIds.length > 0) {
        const kbNames = [];
        try {
          const result = await listKbEntries(req, []);
          const allKbs = result?.entries || [];
          for (const id of kbIds) {
            const kb = allKbs.find(k => k.kb_id === id);
            if (kb) kbNames.push(`${kb.title} (${id})`);
            else kbNames.push(id);
          }
        } catch {}
        const nameList = kbNames.length ? kbNames.join(', ') : kbIds.join(', ');
        systemInstructions += `\n\nACTIVE KNOWLEDGE BASES (user-selected scope):\n`
          + `The user has selected these KBs: ${nameList}\n`
          + `- When using kb_search or kb_documents, ONLY query these KB IDs: ${kbIds.join(', ')}\n`
          + `- Do NOT list or search other KBs unless the user explicitly asks for all KBs.\n`
          + `- When the user says "the knowledge base" or "my KB", they mean these selected KBs.`;
      }

      // Helper: create a fresh Codex conversation for this session, with chat history for context
      async function createFreshConversation() {
        // Include recent chat history so the new conversation has context
        const history = session.messages.slice(-10); // last 10 messages
        let instructions = systemInstructions;
        if (history.length > 0) {
          const historyText = history.map(m =>
            `${m.role === 'user' ? 'User' : 'Assistant'}: ${m.content.slice(0, 1000)}`
          ).join('\n\n');
          instructions += `\n\nCONVERSATION HISTORY (for context):\n${historyText}`;
        }
        const conv = await codex.createThread(instructions);
        session.codexThreadId = conv.threadId;
        persistSessions();
      }

      // Always create a fresh conversation for every turn.
      // The codex model stops calling tools after 1-2 turns in the same conversation
      // because accumulated tool-call traces in the context suppress tool usage.
      // Fresh conversation = clean slate with full tool awareness every time.
      // Chat history is injected as context via createFreshConversation().
      try {
        await createFreshConversation();
      } catch (err) {
        const threadErrStr = typeof err.message === 'string' ? err.message : JSON.stringify(err.message ?? err);
        const errPayload = errorEnvelope('CODEX_THREAD_FAILED', `Failed to create Codex conversation: ${threadErrStr}`, true);
        if (wantsSSE && sseOpen) { sseWrite('error', errPayload); res.end(); return; }
        return res.status(502).json(errPayload);
      }

      // Per-turn capability reminder so the model never forgets its tools
      const turnReminder = '[SYSTEM: You have browser tools (browser_navigate, browser_snapshot, browser_click, browser_type, browser_tab_list, browser_select_option, browser_take_screenshot), KB tools (kb_list, kb_search, kb_documents), and content tools (content_list_downloads, content_upload, content_status, content_voices, content_kb_create, content_library_list, content_library_tag, content_library_metadata, content_library_ingest, content_library_delete, content_stage, content_staged_list, content_staged_clear, content_batch_convert) available RIGHT NOW. Use them — do not say they are unavailable. Complete the ENTIRE task in this turn — never ask the user to follow up or offer to continue later.]';
      const augmentedContent = `${turnReminder}\n\n${content}`;

      let assistantText;
      let toolsWereUsed = false;
      try {
        const result = await codex.sendTurn(session.codexThreadId, augmentedContent, onDelta, { effort });
        assistantText = result.text || '';
        toolsWereUsed = result.otherEvents && result.otherEvents.length > 0;
        if (result.otherEvents?.length) {
          console.log(`[server] Turn events (${result.otherEvents.length}): ${[...new Set(result.otherEvents)].join(', ')}`);
        }
      } catch (err) {
        const errStr = typeof err.message === 'string' ? err.message : JSON.stringify(err.message ?? err);
        console.error(`[server] sendTurn error:`, err.name, err.code, errStr, err.data ? JSON.stringify(err.data).slice(0, 500) : '');
        // Treat conversation-not-found, crashes, and server errors as recoverable
        const isRecoverable = errStr.includes('crashed')
          || errStr.includes('not running')
          || errStr.includes('conversation not found')
          || errStr.includes('unknown conversation')
          || err.code === -32001;
        if (isRecoverable) {
          try {
            // Try restarting Playwright MCP in case it caused the crash
            try { await cleanupPlaywrightMcp(); } catch (e) { console.warn(`[server] Playwright cleanup failed: ${e.message}`); }
            const restarted = await codex.ensureReady();
            if (!restarted) throw new Error('Failed to restart codex app-server');
            // Create new conversation (old one is gone), preserving chat history as context
            await createFreshConversation();
            const result = await codex.sendTurn(session.codexThreadId, augmentedContent, onDelta, { effort });
            assistantText = result.text || '';
            toolsWereUsed = result.otherEvents && result.otherEvents.length > 0;
          } catch (retryErr) {
            const retryErrStr = typeof retryErr.message === 'string' ? retryErr.message : JSON.stringify(retryErr.message ?? retryErr);
            const errPayload = errorEnvelope('CODEX_TURN_FAILED', `Codex turn failed after retry: ${retryErrStr}`, true);
            if (wantsSSE && sseOpen) { sseWrite('error', errPayload); res.end(); return; }
            return res.status(502).json(errPayload);
          }
        } else {
          const errPayload = errorEnvelope('CODEX_TURN_FAILED', `Codex turn failed: ${errStr}`, true);
          if (wantsSSE && sseOpen) { sseWrite('error', errPayload); res.end(); return; }
          return res.status(502).json(errPayload);
        }
      }

      // --- Tool-usage check: detect fabrication (answered without calling any tools) ---
      const requestNeedsTools = REQUIRES_TOOL_USE.test(content);
      if (!toolsWereUsed && requestNeedsTools && assistantText.trim()) {
        console.log(`[server] No tools used for request requiring tools — resetting conversation and retrying`);
        try {
          await createFreshConversation();
          const forceToolContent = `${turnReminder}\n\n[CRITICAL: You MUST call browser tools to answer this question. Do NOT answer from memory — you do not know the course content. Call browser_navigate to open the page, then browser_snapshot to read it. Any answer without tool use is fabricated and wrong.]\n\n${content}`;
          const retryResult = await codex.sendTurn(session.codexThreadId, forceToolContent, onDelta, { effort });
          const retryText = retryResult.text || '';
          const retryUsedTools = retryResult.otherEvents && retryResult.otherEvents.length > 0;
          if (retryText.trim()) {
            if (retryUsedTools) {
              console.log(`[server] Force-tool retry succeeded (${retryResult.otherEvents.length} events)`);
              assistantText = retryText;
              toolsWereUsed = true;
            } else {
              console.log(`[server] Force-tool retry also produced no tool events`);
              // Keep the retry text only if it's longer (might still be better)
              if (retryText.length > assistantText.length) assistantText = retryText;
            }
          }
        } catch (err) {
          console.error(`[server] Force-tool retry failed: ${err.message}`);
        }
      }

      // If blank, retry once asking for a summary
      if (!assistantText.trim()) {
        try {
          const retryResult = await codex.sendTurn(
            session.codexThreadId,
            `${turnReminder}\n\nProvide a text summary of what you found or did. If you have not completed the task, complete it now using your tools.`,
            onDelta,
            { effort }
          );
          assistantText = retryResult.text || '(The assistant completed tool operations but did not produce a text response. Please try rephrasing your question.)';
        } catch {
          assistantText = '(The assistant completed tool operations but did not produce a text response. Please try rephrasing your question.)';
        }
      }

      // --- Post-turn verification ---
      const allResponses = [assistantText];

      for (let attempt = 0; attempt < MAX_VERIFY_ATTEMPTS; attempt++) {
        const signals = needsVerification(allResponses[allResponses.length - 1], content);
        if (!signals) {
          console.log(`[server] Verification PASS (attempt ${attempt})`);
          break;
        }

        console.log(`[server] Verification FAIL (attempt ${attempt + 1}/${MAX_VERIFY_ATTEMPTS}): ${signals.join('; ') || 'high-risk request, short response'}`);

        try {
          const correctionResult = await codex.sendTurn(
            session.codexThreadId,
            `${turnReminder}\n\n${buildCorrectionPrompt(content, signals)}`,
            onDelta,
            { effort }
          );
          const correctedText = correctionResult.text || '';
          if (correctedText.trim()) {
            allResponses.push(correctedText);
          }
        } catch (err) {
          console.error(`[server] Verification turn failed: ${err.message}`);
          break;
        }
      }

      assistantText = selectBestResponse(allResponses);

      const assistantMessage = {
        id: `m_${crypto.randomUUID()}`, role: 'assistant', content: assistantText, created_at: new Date().toISOString()
      };
      session.messages.push(assistantMessage);
      session.updated_at = assistantMessage.created_at;
      persistSessions();

      const responsePayload = {
        assistant_message: assistantMessage,
        citations: [],
        retrieval_summary: { queried_kb_ids: kbIds, top_k: topK, backend: 'codex' },
        action_requests: []
      };

      if (wantsSSE && sseOpen) {
        sseWrite('done', responsePayload);
        res.end();
        return;
      }
      return res.json(responsePayload);
    }

    // --- Fallback: knowledge backend memory retrieval (when agent cannot start) ---
    // If no KBs selected, return a simple message — no retrieval without KBs
    if (!kbIds.length) {
      const assistantMessage = {
        id: `m_${crypto.randomUUID()}`,
        role: 'assistant',
        content: 'Agent is not available and no knowledge bases are selected for memory retrieval.',
        created_at: new Date().toISOString()
      };
      session.messages.push(assistantMessage);
      session.updated_at = assistantMessage.created_at;
      persistSessions();
      const payload = { assistant_message: assistantMessage, citations: [], retrieval_summary: { queried_kb_ids: [], top_k: topK }, action_requests: [] };
      if (wantsSSE && sseOpen) { sseWrite('done', payload); res.end(); return; }
      return res.json(payload);
    }

    const perKb = await Promise.all(
      kbIds.map(async (kbId) => {
        const result = await remoteRequest({
          req,
          method: 'POST',
          path: '/api/chat',
          body: { query: content, kb_id: kbId, conversation_history: [], top_k: topK },
          headers: { 'Content-Type': 'application/json' }
        });
        return { kbId, result };
      })
    );

    for (const row of perKb) {
      if (row.result.status < 200 || row.result.status >= 300) {
        return res.status(row.result.status).json(
          errorEnvelope(
            'UPSTREAM_CHAT_ERROR',
            row.result.data?.detail || `Upstream chat failed for kb ${row.kbId}`,
            false,
            row.result.data,
            row.result.correlationId
          )
        );
      }
    }

    let combinedAnswer =
      perKb.length === 1
        ? perKb[0].result.data?.answer || ''
        : perKb
            .map((row) => `KB ${row.kbId}:\n${row.result.data?.answer || '(no answer)'}`)
            .join('\n\n');

    // Fallback: some backend chat responses return empty answer with valid retrieval.
    // If user asks for title/author-style listing, fetch entries via listKbEntries (handles multi-KB).
    if (!combinedAnswer?.trim() && KB_LISTING_PATTERN.test(content || '')) {
      try {
        const kbEntriesMeta = await listKbEntries(req, kbIds);
        if (kbEntriesMeta.entries.length) {
          const lines = kbEntriesMeta.entries.map(
            (e, i) => `${i + 1}. "${e.title}" by ${e.author}`
          );
          combinedAnswer = `Here are the available entries:\n\n${lines.join('\n')}`;
        }
      } catch {
        // keep empty answer; UI will still show scoped KB and citations
      }
    }

    const citations = perKb.flatMap((row) =>
      (row.result.data?.sources || []).map((s) => ({
        ref: s.ref,
        kb_id: row.kbId,
        document_id: s.document_id,
        chunk_id: null,
        title: s.document_title,
        chapter_title: s.chapter_title,
        page_number: s.page_number,
        score: s.score,
        text_preview: s.text_preview
      }))
    );

    const assistantMessage = {
      id: `m_${crypto.randomUUID()}`,
      role: 'assistant',
      content: combinedAnswer,
      created_at: new Date().toISOString()
    };
    session.messages.push(assistantMessage);
    session.updated_at = assistantMessage.created_at;
    persistSessions();

    return res.json({
      assistant_message: assistantMessage,
      citations,
      retrieval_summary: { queried_kb_ids: kbIds, top_k: topK },
      action_requests: []
    });
  } catch (err) {
    console.error(`[server] Chat handler error: ${err.stack || err.message || err}`);
    persistSessions();
    const errPayload = errorEnvelope(
      'CHAT_ERROR',
      `Codex turn failed: ${err.message || err}`,
      true,
      { reason: String(err?.message || err) }
    );
    if (wantsSSE && sseOpen) { sseWrite('error', errPayload); res.end(); return; }
    return res.status(502).json(errPayload);
  }
});

// v1 library
app.get('/v1/library/items', async (req, res) => {
  const queryString = buildLibraryQueryString(req.query);
  return forwardJson(req, res, { path: `/api/library${queryString}` });
});

app.patch('/v1/library/items/:id', async (req, res) => {
  const id = req.params.id;
  const updates = req.body || {};

  const results = [];
  if (updates.title !== undefined || updates.author !== undefined) {
    const r = await remoteRequest({
      req,
      method: 'PATCH',
      path: `/api/library/${id}/metadata`,
      body: { title: updates.title, author: updates.author },
      headers: { 'Content-Type': 'application/json' }
    });
    results.push(r);
    if (r.status < 200 || r.status >= 300) return res.status(r.status).json(r.data);
  }

  if (updates.tags !== undefined) {
    const r = await remoteRequest({
      req,
      method: 'PATCH',
      path: `/api/library/${id}/tags`,
      body: updates.tags,
      headers: { 'Content-Type': 'application/json' }
    });
    results.push(r);
    if (r.status < 200 || r.status >= 300) return res.status(r.status).json(r.data);
  }

  if (!results.length) return res.status(400).json(errorEnvelope('VALIDATION_ERROR', 'No updates provided'));
  return res.json({ ok: true });
});

app.delete('/v1/library/items/:id', async (req, res) => {
  const id = req.params.id;
  // Best-effort KB cleanup before deleting item (ignore errors if no KB output)
  try { await remoteRequest({ req, method: 'DELETE', path: `/api/library/${id}/output/knowledge_base` }); } catch {}
  return forwardJson(req, res, { method: 'DELETE', path: `/api/library/${id}` });
});

app.delete('/v1/library/items/:id/output/:outputType', async (req, res) => {
  return forwardJson(req, res, {
    method: 'DELETE',
    path: `/api/library/${req.params.id}/output/${req.params.outputType}`
  });
});

app.get('/v1/library/items/:id/download', async (req, res) => {
  const outputType = req.query.output_type || 'searchable_pdf';
  try {
    const result = await remoteRequest({
      req,
      path: `/api/download/${req.params.id}?output_type=${encodeURIComponent(outputType)}`,
      responseType: 'stream'
    });
    if (result.status < 200 || result.status >= 300) {
      return res.status(result.status).json({ error: 'Download failed' });
    }
    if (result.headers['content-type']) res.setHeader('Content-Type', result.headers['content-type']);
    if (result.headers['content-disposition']) res.setHeader('Content-Disposition', result.headers['content-disposition']);
    result.data.pipe(res);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/v1/library/filters', async (req, res) => {
  return forwardJson(req, res, { path: '/api/library/filters' });
});

app.get('/v1/library/kb-orphans', async (req, res) => {
  try {
    // 1. Fetch all library items; collect linked document_ids and item IDs
    const libraryResult = await remoteRequest({ req, path: '/api/library' });
    const items = Array.isArray(libraryResult.data) ? libraryResult.data : (libraryResult.data?.items || []);
    const linkedDocIds = new Set();
    const itemIds = new Set();
    for (const item of items) {
      itemIds.add(item.id);
      const docId = item.document_id || item.output_files?.knowledge_base?.document_id;
      if (docId) linkedDocIds.add(docId);
    }

    // 2. Fetch all KBs, then each KB's documents
    const kbResult = await remoteRequest({ req, path: '/api/knowledge-bases' });
    const kbs = Array.isArray(kbResult.data) ? kbResult.data : (kbResult.data?.items || []);
    const kbMap = {};
    for (const kb of kbs) kbMap[kb.id] = kb.name || kb.id;

    const orphans = [];   // true orphans (no matching library item)
    const unlinked = [];  // have a matching library item but missing from output_files
    for (const kb of kbs) {
      try {
        const docsResult = await remoteRequest({ req, path: `/api/knowledge-bases/${kb.id}/documents` });
        const docs = Array.isArray(docsResult.data) ? docsResult.data : (docsResult.data?.items || docsResult.data?.documents || []);
        for (const doc of docs) {
          if (linkedDocIds.has(doc.id)) continue; // already linked via output_files.knowledge_base

          const docEntry = {
            document_id: doc.id,
            kb_id: kb.id,
            kb_name: kbMap[kb.id],
            title: doc.title || doc.source_file || '(untitled)',
            author: doc.author || '',
            source_file: doc.source_file || '',
            total_pages: doc.total_pages,
            chapter_count: doc.chapter_count,
            created_at: doc.created_at || ''
          };

          // Match by source_file prefix (e.g. "361bfcb8_0_..." starts with item id "361bfcb8")
          const sf = doc.source_file || '';
          let matchedItemId = null;
          for (const itemId of itemIds) {
            if (sf.startsWith(itemId)) { matchedItemId = itemId; break; }
          }

          if (matchedItemId) {
            unlinked.push({ ...docEntry, _orphan: false, _library_item_id: matchedItemId });
          } else {
            orphans.push({
              id: `orphan-${doc.id}`,
              _orphan: true,
              ...docEntry,
              output_files: {},
              tags: {}
            });
          }
        }
      } catch {}
    }

    return res.json({ orphans, unlinked });
  } catch (err) {
    return res.status(500).json(errorEnvelope('INTERNAL', err.message));
  }
});

app.post('/v1/library/bulk', async (req, res) => {
  const operation = req.body?.operation;
  const itemIds = req.body?.item_ids || [];
  if (!operation || !Array.isArray(itemIds) || !itemIds.length) {
    return res.status(400).json(errorEnvelope('VALIDATION_ERROR', 'operation and item_ids[] are required'));
  }

  const outcomes = [];

  for (const id of itemIds) {
    try {
      let r;
      if (operation === 'delete') {
        try { await remoteRequest({ req, method: 'DELETE', path: `/api/library/${id}/output/knowledge_base` }); } catch {}
        r = await remoteRequest({ req, method: 'DELETE', path: `/api/library/${id}` });
      } else if (operation === 'retag') {
        r = await remoteRequest({
          req,
          method: 'PATCH',
          path: `/api/library/${id}/tags`,
          body: req.body.tags || {},
          headers: { 'Content-Type': 'application/json' }
        });
      } else if (operation === 'reindex') {
        r = await remoteRequest({
          req,
          method: 'POST',
          path: `/api/library/${id}/ingest`,
          body: { kb_id: req.body?.kb_id },
          headers: { 'Content-Type': 'application/json' }
        });
      } else {
        return res.status(400).json(errorEnvelope('VALIDATION_ERROR', `Unsupported operation: ${operation}`));
      }

      outcomes.push({ item_id: id, status: r.status, ok: r.status >= 200 && r.status < 300, result: r.data });
    } catch (err) {
      outcomes.push({ item_id: id, status: 500, ok: false, result: { message: String(err?.message || err) } });
    }
  }

  return res.json({ operation, outcomes });
});

// ingest
app.post('/v1/ingest/jobs', upload.array('files'), async (req, res) => {
  const correlationId = req.headers['x-correlation-id'] || crypto.randomUUID();
  const form = new FormData();

  try {
    const fields = {
      voice: req.body.voice || 'af_heart',
      title: req.body.title || '',
      author: req.body.author || '',
      outputs: req.body.outputs || 'm4b',
      kb_id: req.body.kb_id || '',
      kb_name: req.body.kb_name || '',
      kb_description: req.body.kb_description || '',
      source_type: req.body.source_type || 'upload',
      library_id: req.body.library_id || ''
    };

    for (const [k, v] of Object.entries(fields)) if (v !== '') form.append(k, v);
    for (const f of req.files || []) form.append('files', fs.createReadStream(f.path), f.originalname || 'upload.bin');

    const result = await remoteRequest({
      req,
      method: 'POST',
      path: '/api/upload',
      body: form,
      headers: form.getHeaders(),
      responseType: 'json'
    });

    res.setHeader('x-correlation-id', result.correlationId);
    for (const f of req.files || []) {
      try {
        fs.unlinkSync(f.path);
      } catch {}
    }

    if (result.status >= 200 && result.status < 300) {
      const payload = result.data || {};
      return res.status(result.status).json({
        job_id: payload.id || payload.job_id || payload?.job?.id || null,
        status: payload.status || 'queued',
        raw: payload
      });
    }

    return res.status(result.status).json(
      errorEnvelope(
        'UPSTREAM_INGEST_ERROR',
        result.data?.detail || 'Upstream ingest request failed',
        false,
        result.data,
        result.correlationId
      )
    );
  } catch (err) {
    for (const f of req.files || []) {
      try {
        fs.unlinkSync(f.path);
      } catch {}
    }
    return res.status(502).json(
      errorEnvelope(
        'UPSTREAM_UNREACHABLE',
        `Failed to reach remote backend at ${REMOTE_BASE}`,
        true,
        { reason: String(err?.message || err), socks_proxy: SOCKS_PROXY || null },
        correlationId
      )
    );
  }
});

app.get('/v1/ingest/jobs', async (req, res) => forwardJson(req, res, { path: '/api/jobs' }));

app.get('/v1/ingest/jobs/:jobId', async (req, res) => forwardJson(req, res, { path: `/api/status/${req.params.jobId}` }));

app.get('/v1/ingest/jobs/:jobId/download', async (req, res) => {
  const outputType = req.query.output_type || 'm4b';
  try {
    const result = await remoteRequest({
      req,
      path: `/api/download/${req.params.jobId}?output_type=${encodeURIComponent(outputType)}`,
      responseType: 'stream'
    });
    if (result.status < 200 || result.status >= 300) {
      return res.status(result.status).json({ error: 'Download failed' });
    }
    if (result.headers['content-type']) res.setHeader('Content-Type', result.headers['content-type']);
    if (result.headers['content-disposition']) res.setHeader('Content-Disposition', result.headers['content-disposition']);
    result.data.pipe(res);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// actions bridge (remote backend-native by default)
app.get('/v1/actions/requests', async (req, res) => {
  if (ACTIONS_MODE === 'local') {
    const sessionId = req.query.chat_session_id;
    let items = [...actionRequests.values()];
    if (sessionId) items = items.filter((a) => a.chat_session_id === sessionId);
    items.sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1));
    return res.json({ items });
  }

  try {
    const remote = await remoteRequest({ req, path: '/v1/actions/requests?limit=500' });
    if (remote.status < 200 || remote.status >= 300) return res.status(remote.status).json(remote.data);
    const rawItems = Array.isArray(remote.data) ? remote.data : remote.data?.items || [];
    const mapped = rawItems.map((r) => {
      const local = normalizeRemoteActionRecord(r);
      actionRequests.set(local.id, local);
      return local;
    });
    persistActionRequests();

    const sessionId = req.query.chat_session_id;
    const filtered = sessionId ? mapped.filter((a) => a.chat_session_id === sessionId) : mapped;
    filtered.sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1));
    return res.json({ items: filtered });
  } catch (err) {
    return res.status(502).json(errorEnvelope('UPSTREAM_UNREACHABLE', String(err?.message || err), true));
  }
});

app.get('/v1/actions/requests/:id', async (req, res) => {
  if (ACTIONS_MODE === 'local') {
    const action = actionRequests.get(req.params.id);
    if (!action) return res.status(404).json(errorEnvelope('NOT_FOUND', 'Action request not found'));
    return res.json(action);
  }

  try {
    const remote = await remoteRequest({ req, path: `/v1/actions/requests/${req.params.id}` });
    if (remote.status < 200 || remote.status >= 300) return res.status(remote.status).json(remote.data);
    const mapped = normalizeRemoteActionRecord(remote.data);
    actionRequests.set(mapped.id, mapped);
    persistActionRequests();
    return res.json(mapped);
  } catch (err) {
    return res.status(502).json(errorEnvelope('UPSTREAM_UNREACHABLE', String(err?.message || err), true));
  }
});

app.post('/v1/actions/requests', async (req, res) => {
  const type = typeof req.body?.type === 'string' ? req.body.type : '';
  if (!type) return res.status(400).json(errorEnvelope('VALIDATION_ERROR', 'type is required'));
  if (type !== 'library.bulk') {
    return res.status(400).json(errorEnvelope('VALIDATION_ERROR', `Unsupported action type: ${type}`));
  }

  const chatSessionId = typeof req.body?.chat_session_id === 'string' ? req.body.chat_session_id.trim() : '';
  if (!chatSessionId) return res.status(400).json(errorEnvelope('VALIDATION_ERROR', 'chat_session_id is required'));

  const rawPayload = req.body?.payload || {};
  const operation = typeof rawPayload.operation === 'string' ? rawPayload.operation.trim().toLowerCase() : '';
  if (!SUPPORTED_LIBRARY_BULK_OPS.has(operation)) {
    return res.status(400).json(errorEnvelope('VALIDATION_ERROR', `Unsupported operation: ${operation}`));
  }

  const itemIds = Array.isArray(rawPayload.item_ids)
    ? [...new Set(rawPayload.item_ids.filter((id) => typeof id === 'string' && id.trim()))]
    : [];
  if (!itemIds.length) return res.status(400).json(errorEnvelope('VALIDATION_ERROR', 'payload.item_ids[] is required'));

  if (operation === 'reindex' && !(typeof rawPayload.kb_id === 'string' && rawPayload.kb_id.trim())) {
    return res.status(400).json(errorEnvelope('VALIDATION_ERROR', 'payload.kb_id is required for reindex'));
  }

  if (ACTIONS_MODE === 'local') {
    const now = new Date().toISOString();
    const payload = { operation, item_ids: itemIds };
    if (operation === 'reindex') payload.kb_id = rawPayload.kb_id;
    if (operation === 'retag') payload.tags = rawPayload.tags || {};
    const action = {
      id: `a_${crypto.randomUUID()}`,
      type,
      title: req.body?.title || `Library bulk ${operation}`,
      payload,
      status: 'proposed',
      chat_session_id: chatSessionId,
      message_id: req.body?.message_id || null,
      approved_by: null,
      policy_snapshot: normalizePolicySnapshot(req.body?.policy_snapshot || {}, { type, payload }),
      result: null,
      created_at: now,
      updated_at: now
    };
    actionRequests.set(action.id, action);
    persistActionRequests();
    return res.status(201).json(action);
  }

  try {
    const remoteBody = buildRemoteCreateRequestFromLocal(req.body);
    const remote = await remoteRequest({
      req,
      method: 'POST',
      path: '/v1/actions/requests',
      body: remoteBody,
      headers: { 'Content-Type': 'application/json' }
    });
    if (remote.status < 200 || remote.status >= 300) return res.status(remote.status).json(remote.data);

    const mapped = normalizeRemoteActionRecord(remote.data);
    mapped.chat_session_id = chatSessionId;
    mapped.message_id = req.body?.message_id || null;
    mapped.policy_snapshot = req.body?.policy_snapshot || null;
    actionRequests.set(mapped.id, mapped);
    persistActionRequests();
    return res.status(201).json(mapped);
  } catch (err) {
    return res.status(502).json(errorEnvelope('UPSTREAM_UNREACHABLE', String(err?.message || err), true));
  }
});

app.post('/v1/actions/requests/:id/approve', async (req, res) => {
  if (ACTIONS_MODE === 'local') {
    const action = actionRequests.get(req.params.id);
    if (!action) return res.status(404).json(errorEnvelope('NOT_FOUND', 'Action request not found'));
    if (action.status !== 'proposed') {
      return res.status(409).json(errorEnvelope('INVALID_STATE', `Action is ${action.status}, expected proposed`));
    }
    action.status = 'approved';
    action.approved_by = req.body?.approved_by || 'local-user';
    action.updated_at = new Date().toISOString();
    actionRequests.set(action.id, action);
    persistActionRequests();
    return res.json(action);
  }

  try {
    const remote = await remoteRequest({
      req,
      method: 'POST',
      path: `/v1/actions/requests/${req.params.id}/approve`,
      body: { reviewed_by: req.body?.approved_by || 'local-user', note: req.body?.note || '' },
      headers: { 'Content-Type': 'application/json' }
    });
    if (remote.status < 200 || remote.status >= 300) return res.status(remote.status).json(remote.data);
    const mapped = normalizeRemoteActionRecord(remote.data);
    const cached = actionRequests.get(mapped.id);
    if (cached?.chat_session_id) mapped.chat_session_id = cached.chat_session_id;
    actionRequests.set(mapped.id, mapped);
    persistActionRequests();
    return res.json(mapped);
  } catch (err) {
    return res.status(502).json(errorEnvelope('UPSTREAM_UNREACHABLE', String(err?.message || err), true));
  }
});

app.post('/v1/actions/requests/:id/reject', async (req, res) => {
  if (ACTIONS_MODE === 'local') {
    const action = actionRequests.get(req.params.id);
    if (!action) return res.status(404).json(errorEnvelope('NOT_FOUND', 'Action request not found'));
    if (action.status !== 'proposed') {
      return res.status(409).json(errorEnvelope('INVALID_STATE', `Action is ${action.status}, expected proposed`));
    }
    action.status = 'rejected';
    action.result = { reason: req.body?.reason || 'Rejected by user' };
    action.updated_at = new Date().toISOString();
    actionRequests.set(action.id, action);
    persistActionRequests();
    return res.json(action);
  }

  try {
    const remote = await remoteRequest({
      req,
      method: 'POST',
      path: `/v1/actions/requests/${req.params.id}/reject`,
      body: { reviewed_by: req.body?.approved_by || 'local-user', note: req.body?.reason || 'Rejected by user' },
      headers: { 'Content-Type': 'application/json' }
    });
    if (remote.status < 200 || remote.status >= 300) return res.status(remote.status).json(remote.data);
    const mapped = normalizeRemoteActionRecord(remote.data);
    const cached = actionRequests.get(mapped.id);
    if (cached?.chat_session_id) mapped.chat_session_id = cached.chat_session_id;
    actionRequests.set(mapped.id, mapped);
    persistActionRequests();
    return res.json(mapped);
  } catch (err) {
    return res.status(502).json(errorEnvelope('UPSTREAM_UNREACHABLE', String(err?.message || err), true));
  }
});

app.post('/v1/actions/requests/:id/execute', async (req, res) => {
  if (ACTIONS_MODE === 'local') {
    const action = actionRequests.get(req.params.id);
    if (!action) return res.status(404).json(errorEnvelope('NOT_FOUND', 'Action request not found'));
    if (action.status !== 'approved') {
      return res.status(409).json(errorEnvelope('INVALID_STATE', `Action is ${action.status}, expected approved`));
    }
    action.status = 'executing';
    action.updated_at = new Date().toISOString();
    actionRequests.set(action.id, action);
    persistActionRequests();
    return res.json(action);
  }

  try {
    const remote = await remoteRequest({ req, method: 'POST', path: `/v1/actions/requests/${req.params.id}/execute` });
    if (remote.status < 200 || remote.status >= 300) return res.status(remote.status).json(remote.data);
    const mapped = normalizeRemoteActionRecord(remote.data);
    const cached = actionRequests.get(mapped.id);
    if (cached?.chat_session_id) mapped.chat_session_id = cached.chat_session_id;
    actionRequests.set(mapped.id, mapped);
    persistActionRequests();
    return res.json(mapped);
  } catch (err) {
    return res.status(502).json(errorEnvelope('UPSTREAM_UNREACHABLE', String(err?.message || err), true));
  }
});

app.get('/v1/audit/actions/export', async (req, res) => {
  if (ACTIONS_MODE === 'local') {
    const filterSessionId =
      typeof req.query.chat_session_id === 'string' && req.query.chat_session_id.trim()
        ? req.query.chat_session_id.trim()
        : '';
    let items = [...actionRequests.values()];
    if (filterSessionId) items = items.filter((a) => a.chat_session_id === filterSessionId);
    items.sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
    return res.json({
      generated_at: new Date().toISOString(),
      filters: { chat_session_id: filterSessionId || null },
      total: items.length,
      actions: items.map((a) => ({ ...a }))
    });
  }

  const params = new URLSearchParams();
  if (req.query.status) params.set('status', req.query.status);
  if (req.query.since) params.set('since', req.query.since);
  if (req.query.until) params.set('until', req.query.until);

  try {
    const remote = await remoteRequest({ req, path: `/v1/audit/actions/export${params.toString() ? `?${params}` : ''}` });
    return res.status(remote.status).json(remote.data);
  } catch (err) {
    return res.status(502).json(errorEnvelope('UPSTREAM_UNREACHABLE', String(err?.message || err), true));
  }
});

// Serve built frontend (production)
const DIST_DIR = path.resolve(__dirname, '..', '..', 'dist');
if (fs.existsSync(path.join(DIST_DIR, 'index.html'))) {
  app.use(express.static(DIST_DIR));
  app.get('*', (req, res) => {
    res.sendFile(path.join(DIST_DIR, 'index.html'));
  });
}

app.listen(port, host, () =>
  console.log(
    `noetix-ui listening on ${host}:${port} (remote: ${REMOTE_BASE}, socks: ${SOCKS_PROXY || 'disabled'})`
  )
);
