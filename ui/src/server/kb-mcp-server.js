#!/usr/bin/env node
/**
 * Noetix Knowledge Base MCP Server
 *
 * Exposes the Noetix backend KB API as MCP tools over stdio transport.
 * Tools:
 *   - kb_list          — List all accessible knowledge bases with title, author, doc count
 *   - kb_search        — Query a KB with a natural language question (memory retrieval)
 *   - kb_documents     — List all documents in a specific KB
 *
 * Configuration: reads ui.config via config.js; env vars override.
 */

import http from 'node:http';
import https from 'node:https';
import { createInterface } from 'node:readline';
import config from './config.js';

const REMOTE_BASE = process.env.REMOTE_BASE || config.remoteBase;
const SOCKS_PROXY = process.env.SOCKS_PROXY || config.socksProxy;

// ---------------------------------------------------------------------------
// Simple HTTP client (no dependency on axios)
// ---------------------------------------------------------------------------

function request(method, urlPath, body = null) {
  const base = new URL(REMOTE_BASE);
  const mod = base.protocol === 'https:' ? https : http;
  const payload = body ? JSON.stringify(body) : null;

  return new Promise((resolve, reject) => {
    const req = mod.request(
      {
        hostname: base.hostname,
        port: base.port,
        path: urlPath,
        method,
        headers: {
          'Content-Type': 'application/json',
          ...(payload ? { 'Content-Length': Buffer.byteLength(payload) } : {})
        },
        timeout: 30000
      },
      (res) => {
        let data = '';
        res.on('data', (chunk) => (data += chunk));
        res.on('end', () => {
          try {
            resolve({ status: res.statusCode, data: JSON.parse(data) });
          } catch {
            resolve({ status: res.statusCode, data });
          }
        });
      }
    );
    req.on('error', (err) => reject(err));
    req.on('timeout', () => { req.destroy(); reject(new Error('Request timed out')); });
    if (payload) req.write(payload);
    req.end();
  });
}

// ---------------------------------------------------------------------------
// Tool definitions
// ---------------------------------------------------------------------------

const TOOLS = [
  {
    name: 'kb_list',
    description: 'List all accessible knowledge bases with title, author, and document count. Use this when the user asks what titles, books, documents, or materials are available.',
    inputSchema: {
      type: 'object',
      properties: {},
      required: []
    }
  },
  {
    name: 'kb_search',
    description: 'Search a knowledge base with a natural language query. Returns an answer and source citations. Use this for any factual question about KB content.',
    inputSchema: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'The natural language question to search for' },
        kb_id: { type: 'string', description: 'The knowledge base ID to search in' },
        top_k: { type: 'number', description: 'Number of top results to return (default: 8)' }
      },
      required: ['query', 'kb_id']
    }
  },
  {
    name: 'kb_documents',
    description: 'List all documents in a specific knowledge base. Returns title and author for each document.',
    inputSchema: {
      type: 'object',
      properties: {
        kb_id: { type: 'string', description: 'The knowledge base ID to list documents from' }
      },
      required: ['kb_id']
    }
  }
];

// ---------------------------------------------------------------------------
// Tool implementations
// ---------------------------------------------------------------------------

async function handleToolCall(name, args) {
  switch (name) {
    case 'kb_list': {
      const res = await request('GET', '/api/knowledge-bases');
      if (res.status < 200 || res.status >= 300) {
        return { isError: true, content: [{ type: 'text', text: `Backend returned HTTP ${res.status}: ${JSON.stringify(res.data)}` }] };
      }
      const kbs = Array.isArray(res.data) ? res.data : [];
      if (!kbs.length) {
        return { content: [{ type: 'text', text: 'No knowledge bases found.' }] };
      }
      const lines = kbs.map((kb, i) =>
        `${i + 1}. **${kb.title || kb.name || '(untitled)'}** by ${kb.author || kb.created_by || 'Unknown'} [id: ${kb.id}] (${kb.document_count ?? kb.doc_count ?? '?'} documents)`
      );
      return { content: [{ type: 'text', text: `Found ${kbs.length} knowledge base(s):\n\n${lines.join('\n')}` }] };
    }

    case 'kb_search': {
      const { query, kb_id, top_k = 8 } = args;
      if (!query || !kb_id) {
        return { isError: true, content: [{ type: 'text', text: 'query and kb_id are required' }] };
      }
      const res = await request('POST', '/api/chat', {
        query,
        kb_id,
        conversation_history: [],
        top_k: Math.min(Number(top_k) || 8, 20)
      });
      if (res.status < 200 || res.status >= 300) {
        return { isError: true, content: [{ type: 'text', text: `Backend returned HTTP ${res.status}: ${JSON.stringify(res.data)}` }] };
      }
      const answer = res.data?.answer || '(no answer)';
      const sources = (res.data?.sources || []).map((s) =>
        `- ${s.document_title || 'Untitled'} (p.${s.page_number || '?'}, score: ${s.score?.toFixed(2) || '?'}): ${(s.text_preview || '').slice(0, 200)}`
      );
      const sourceBlock = sources.length ? `\n\nSources:\n${sources.join('\n')}` : '';
      return { content: [{ type: 'text', text: `${answer}${sourceBlock}` }] };
    }

    case 'kb_documents': {
      const { kb_id } = args;
      if (!kb_id) {
        return { isError: true, content: [{ type: 'text', text: 'kb_id is required' }] };
      }
      const res = await request('GET', `/api/knowledge-bases/${kb_id}/documents`);
      if (res.status < 200 || res.status >= 300) {
        return { isError: true, content: [{ type: 'text', text: `Backend returned HTTP ${res.status}: ${JSON.stringify(res.data)}` }] };
      }
      const docs = Array.isArray(res.data) ? res.data : [];
      if (!docs.length) {
        return { content: [{ type: 'text', text: `No documents found in KB ${kb_id}.` }] };
      }
      const lines = docs.map((d, i) =>
        `${i + 1}. **${d.title || '(untitled)'}** by ${d.author || 'Unknown'}`
      );
      return { content: [{ type: 'text', text: `Found ${docs.length} document(s) in KB ${kb_id}:\n\n${lines.join('\n')}` }] };
    }

    default:
      return { isError: true, content: [{ type: 'text', text: `Unknown tool: ${name}` }] };
  }
}

// ---------------------------------------------------------------------------
// JSON-RPC stdio transport (MCP protocol)
// ---------------------------------------------------------------------------

let jsonRpcId = 0;

function sendResponse(id, result) {
  const msg = JSON.stringify({ jsonrpc: '2.0', id, result });
  process.stdout.write(msg + '\n');
}

function sendError(id, code, message) {
  const msg = JSON.stringify({ jsonrpc: '2.0', id, error: { code, message } });
  process.stdout.write(msg + '\n');
}

async function handleMessage(msg) {
  const { id, method, params } = msg;

  switch (method) {
    case 'initialize':
      return sendResponse(id, {
        protocolVersion: '2024-11-05',
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: 'noetix-kb', version: '1.0.0' }
      });

    case 'notifications/initialized':
      // No response needed for notifications
      return;

    case 'tools/list':
      return sendResponse(id, { tools: TOOLS });

    case 'tools/call': {
      const toolName = params?.name;
      const toolArgs = params?.arguments || {};
      try {
        const result = await handleToolCall(toolName, toolArgs);
        return sendResponse(id, result);
      } catch (err) {
        return sendResponse(id, {
          isError: true,
          content: [{ type: 'text', text: `Tool execution error: ${err.message}` }]
        });
      }
    }

    default:
      if (id !== undefined) {
        return sendError(id, -32601, `Method not found: ${method}`);
      }
  }
}

// ---------------------------------------------------------------------------
// Main: read JSON-RPC messages from stdin
// ---------------------------------------------------------------------------

const rl = createInterface({ input: process.stdin });

rl.on('line', async (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  try {
    const msg = JSON.parse(trimmed);
    await handleMessage(msg);
  } catch (err) {
    sendError(null, -32700, `Parse error: ${err.message}`);
  }
});

rl.on('close', () => process.exit(0));

process.stderr.write(`[noetix-kb-mcp] Started. Backend: ${REMOTE_BASE}\n`);
