import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import http from 'http';
import express from 'express';
import { spawn } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const KB_MCP_SERVER = path.join(__dirname, '..', 'server', 'kb-mcp-server.js');

/**
 * Tests for the Noetix KB MCP server (stdio JSON-RPC transport).
 */

function sendJsonRpc(proc, method, params = {}, id = 1) {
  return new Promise((resolve, reject) => {
    let data = '';
    const onData = (chunk) => {
      data += chunk.toString();
      // Each response is a newline-delimited JSON
      const lines = data.split('\n').filter((l) => l.trim());
      for (const line of lines) {
        try {
          const parsed = JSON.parse(line);
          if (parsed.id === id) {
            proc.stdout.removeListener('data', onData);
            resolve(parsed);
            return;
          }
        } catch {
          // incomplete JSON, keep buffering
        }
      }
    };
    proc.stdout.on('data', onData);
    proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    setTimeout(() => {
      proc.stdout.removeListener('data', onData);
      reject(new Error(`Timeout waiting for response to ${method}`));
    }, 10000);
  });
}

describe('KB MCP Server', () => {
  let mockBackend;
  let backendPort;
  let mockKbs;
  let mockDocs;

  function startMockBackend() {
    return new Promise((resolve) => {
      const app = express();
      app.use(express.json());

      app.get('/api/knowledge-bases', (_req, res) => res.json(mockKbs));
      app.get('/api/knowledge-bases/:kbId/documents', (req, res) => {
        res.json(mockDocs[req.params.kbId] || []);
      });
      app.post('/api/chat', (req, res) => {
        const { query, kb_id } = req.body;
        res.json({
          answer: `Mock answer for "${query}" in KB ${kb_id}`,
          sources: [{ document_title: 'Test Doc', page_number: 1, score: 0.95, text_preview: 'preview text' }]
        });
      });

      mockBackend = app.listen(0, '127.0.0.1', () => {
        backendPort = mockBackend.address().port;
        resolve();
      });
    });
  }

  beforeEach(async () => {
    mockKbs = [
      { id: 'kb-001', title: 'Introduction to Algorithms', author: 'Cormen et al.', document_count: 3 },
      { id: 'kb-002', title: 'Design Patterns', author: 'Gang of Four', document_count: 5 }
    ];
    mockDocs = {
      'kb-001': [
        { title: 'Chapter 1: Sorting', author: 'Cormen' },
        { title: 'Chapter 2: Graphs', author: 'Cormen' }
      ]
    };
    await startMockBackend();
  });

  afterEach(() => {
    return new Promise((resolve) => {
      if (mockBackend) {
        mockBackend.close(() => resolve());
        mockBackend = null;
      } else {
        resolve();
      }
    });
  });

  function spawnMcpServer() {
    return spawn('node', [KB_MCP_SERVER], {
      env: { ...process.env, REMOTE_BASE: `http://127.0.0.1:${backendPort}` },
      stdio: ['pipe', 'pipe', 'pipe']
    });
  }

  it('should respond to initialize', async () => {
    const proc = spawnMcpServer();
    try {
      const res = await sendJsonRpc(proc, 'initialize', {});
      expect(res.result.serverInfo.name).toBe('noetix-kb');
      expect(res.result.capabilities.tools).toBeDefined();
    } finally {
      proc.kill();
    }
  });

  it('should list available tools', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/list', {}, 1);
      const toolNames = res.result.tools.map((t) => t.name);
      expect(toolNames).toContain('kb_list');
      expect(toolNames).toContain('kb_search');
      expect(toolNames).toContain('kb_documents');
    } finally {
      proc.kill();
    }
  });

  it('should list knowledge bases via kb_list tool', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', { name: 'kb_list', arguments: {} }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('Introduction to Algorithms');
      expect(text).toContain('Cormen et al.');
      expect(text).toContain('Design Patterns');
      expect(text).toContain('Gang of Four');
      expect(text).toContain('2 knowledge base');
    } finally {
      proc.kill();
    }
  });

  it('should search a KB via kb_search tool', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'kb_search',
        arguments: { query: 'What is sorting?', kb_id: 'kb-001' }
      }, 3);
      const text = res.result.content[0].text;
      expect(text).toContain('Mock answer');
      expect(text).toContain('Sources');
      expect(text).toContain('Test Doc');
    } finally {
      proc.kill();
    }
  });

  it('should list documents via kb_documents tool', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'kb_documents',
        arguments: { kb_id: 'kb-001' }
      }, 4);
      const text = res.result.content[0].text;
      expect(text).toContain('Chapter 1: Sorting');
      expect(text).toContain('Cormen');
      expect(text).toContain('2 document');
    } finally {
      proc.kill();
    }
  });

  it('should return error for unknown tool', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'nonexistent_tool',
        arguments: {}
      }, 5);
      expect(res.result.isError).toBe(true);
      expect(res.result.content[0].text).toContain('Unknown tool');
    } finally {
      proc.kill();
    }
  });

  it('should require kb_id for kb_search', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'kb_search',
        arguments: { query: 'test' }
      }, 6);
      expect(res.result.isError).toBe(true);
      expect(res.result.content[0].text).toContain('required');
    } finally {
      proc.kill();
    }
  });
});
