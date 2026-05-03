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
import { detectLiveEndpoint, formatDiagnostics } from './playwright-mcp.js';
import config from './config.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const DATA_DIR = path.join(__dirname, '..', 'data');
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


app.get('/health', (_req, res) =>
  res.json({
    ok: true,
    remote_base: REMOTE_BASE,
    socks_proxy: SOCKS_PROXY || null,
    actions_mode: ACTIONS_MODE,
    playwright_mcp_url: PLAYWRIGHT_MCP_URL
  })
);

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
