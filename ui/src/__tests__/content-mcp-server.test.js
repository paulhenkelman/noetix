import { describe, it, expect, beforeEach, afterEach, beforeAll, afterAll } from 'vitest';
import http from 'http';
import express from 'express';
import { spawn } from 'child_process';
import path from 'path';
import fs from 'fs';
import os from 'os';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CONTENT_MCP_SERVER = path.join(__dirname, '..', 'gateway', 'content-mcp-server.js');

/**
 * Tests for the Noetix Content Services MCP server (stdio JSON-RPC transport).
 */

function sendJsonRpc(proc, method, params = {}, id = 1) {
  return new Promise((resolve, reject) => {
    let data = '';
    const onData = (chunk) => {
      data += chunk.toString();
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

describe('Content MCP Server', () => {
  let mockBackend;
  let backendPort;
  let tmpDir;

  // Track requests received by mock backend for assertions
  let lastUploadHeaders;
  let lastUploadBody;
  let lastTagBody;
  let lastMetadataBody;
  let lastIngestBody;
  let lastDeletedItemId;

  function startMockBackend() {
    return new Promise((resolve) => {
      const app = express();
      app.use(express.json());

      // Upload endpoint — validates multipart Content-Type
      app.post('/api/upload', (req, res) => {
        lastUploadHeaders = req.headers;
        // Collect raw body for multipart inspection
        let rawBody = Buffer.alloc(0);
        req.on('data', (chunk) => { rawBody = Buffer.concat([rawBody, chunk]); });
        req.on('end', () => {
          lastUploadBody = rawBody;
          res.json({ job_id: 'job-abc-123', message: 'Upload accepted' });
        });
      });

      // Status endpoint
      app.get('/api/status/:jobId', (req, res) => {
        const jobId = req.params.jobId;
        if (jobId === 'job-completed') {
          res.json({
            status: 'completed',
            progress: 100,
            current_task: 'done',
            title: 'Test Document',
            output_files: {
              searchable_pdf: { filename: 'test.pdf', file_size_mb: 1.5 },
              m4b: { filename: 'test.m4b', file_size_mb: 45.2 }
            }
          });
        } else if (jobId === 'job-failed') {
          res.json({
            status: 'failed',
            progress: 0,
            error: 'Video processing failed: audio too short'
          });
        } else if (jobId === 'job-completed-array') {
          res.json({
            status: 'completed',
            progress: 100,
            title: 'Legacy Document',
            output_files: ['/output/legacy.m4b', '/output/legacy.pdf']
          });
        } else {
          res.json({
            status: 'processing',
            progress: 42,
            current_task: 'Converting chapter 3 of 7',
            title: 'Test Document'
          });
        }
      });

      // Voices endpoint
      app.get('/api/voices', (_req, res) => {
        res.json([
          { name: 'af_heart', description: 'Female, warm tone' },
          { name: 'am_bold', description: 'Male, confident' },
          { name: 'bf_gentle', description: 'British female, gentle' }
        ]);
      });

      // KB create endpoint
      app.post('/api/knowledge-bases', (req, res) => {
        if (!req.body.name) {
          return res.status(400).json({ error: 'name is required' });
        }
        res.json({ id: 'kb-new-001', name: req.body.name });
      });

      // Library list endpoint
      app.get('/api/library', (req, res) => {
        let items = [
          { id: 'lib-001', title: 'Algorithms Lecture', author: 'Dr. Smith', outputs: 'm4b,knowledge_base', tags: { course_code: 'CS6795' }, kb_id: 'kb-001' },
          { id: 'lib-002', title: 'Data Structures Video', author: 'Dr. Jones', outputs: 'searchable_pdf', tags: { course_code: 'CS6515' }, kb_id: null },
          { id: 'lib-003', title: 'ML Primer', author: 'Dr. Lee', outputs: 'knowledge_base', tags: { course_code: 'CS6795' }, kb_id: 'kb-002' }
        ];
        if (req.query.course_code) {
          items = items.filter(i => i.tags?.course_code === req.query.course_code);
        }
        if (req.query.organization) {
          items = items.filter(i => i.tags?.organization === req.query.organization);
        }
        if (req.query.has_kb !== undefined) {
          const wantKb = req.query.has_kb === 'true';
          items = items.filter(i => wantKb ? !!i.kb_id : !i.kb_id);
        }
        res.json(items);
      });

      // Library tag endpoint
      app.patch('/api/library/:itemId/tags', (req, res) => {
        lastTagBody = req.body;
        res.json({ ok: true });
      });

      // Library metadata endpoint
      app.patch('/api/library/:itemId/metadata', (req, res) => {
        lastMetadataBody = req.body;
        res.json({ ok: true });
      });

      // Library ingest endpoint
      app.post('/api/library/:itemId/ingest', (req, res) => {
        lastIngestBody = req.body;
        res.json({ job_id: 'job-reingest-456' });
      });

      // Library delete endpoint
      app.delete('/api/library/:itemId', (req, res) => {
        lastDeletedItemId = req.params.itemId;
        res.json({ ok: true, deleted: req.params.itemId });
      });

      mockBackend = app.listen(0, '127.0.0.1', () => {
        backendPort = mockBackend.address().port;
        resolve();
      });
    });
  }

  beforeEach(async () => {
    lastUploadHeaders = null;
    lastUploadBody = null;
    lastTagBody = null;
    lastMetadataBody = null;
    lastIngestBody = null;
    lastDeletedItemId = null;

    // Create temp downloads directory with test files
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'content-mcp-test-'));
    fs.writeFileSync(path.join(tmpDir, 'lecture.pdf'), 'fake-pdf-content');
    fs.writeFileSync(path.join(tmpDir, 'recording.mp3'), 'fake-mp3-content');
    fs.writeFileSync(path.join(tmpDir, 'video.mp4'), 'fake-mp4-content');
    fs.writeFileSync(path.join(tmpDir, 'console-log.txt'), 'should be excluded');
    fs.writeFileSync(path.join(tmpDir, 'screenshot-123.png'), 'should be excluded');

    await startMockBackend();
  });

  afterEach(() => {
    return new Promise((resolve) => {
      // Clean up temp dir
      try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch {}
      if (mockBackend) {
        mockBackend.close(() => resolve());
        mockBackend = null;
      } else {
        resolve();
      }
    });
  });

  function spawnMcpServer(extraEnv = {}) {
    return spawn('node', [CONTENT_MCP_SERVER], {
      env: {
        ...process.env,
        REMOTE_BASE: `http://127.0.0.1:${backendPort}`,
        DOWNLOADS_DIR: tmpDir,
        ...extraEnv
      },
      stdio: ['pipe', 'pipe', 'pipe']
    });
  }

  // ---------------------------------------------------------------------------
  // Protocol tests
  // ---------------------------------------------------------------------------

  it('should respond to initialize with noetix-content name', async () => {
    const proc = spawnMcpServer();
    try {
      const res = await sendJsonRpc(proc, 'initialize', {});
      expect(res.result.serverInfo.name).toBe('noetix-content');
      expect(res.result.serverInfo.version).toBe('1.0.0');
      expect(res.result.capabilities.tools).toBeDefined();
    } finally {
      proc.kill();
    }
  });

  it('should list 14 tools', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/list', {}, 1);
      const toolNames = res.result.tools.map((t) => t.name);
      expect(toolNames).toHaveLength(14);
      expect(toolNames).toContain('content_list_downloads');
      expect(toolNames).toContain('content_upload');
      expect(toolNames).toContain('content_status');
      expect(toolNames).toContain('content_voices');
      expect(toolNames).toContain('content_kb_create');
      expect(toolNames).toContain('content_library_list');
      expect(toolNames).toContain('content_library_tag');
      expect(toolNames).toContain('content_library_metadata');
      expect(toolNames).toContain('content_library_ingest');
      expect(toolNames).toContain('content_library_delete');
      expect(toolNames).toContain('content_stage');
      expect(toolNames).toContain('content_staged_list');
      expect(toolNames).toContain('content_staged_clear');
      expect(toolNames).toContain('content_batch_convert');
    } finally {
      proc.kill();
    }
  });

  it('should return error for unknown tool', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', { name: 'nonexistent', arguments: {} }, 2);
      expect(res.result.isError).toBe(true);
      expect(res.result.content[0].text).toContain('Unknown tool');
    } finally {
      proc.kill();
    }
  });

  // ---------------------------------------------------------------------------
  // content_list_downloads
  // ---------------------------------------------------------------------------

  it('should list files with absolute paths and sizes', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_list_downloads',
        arguments: {}
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('lecture.pdf');
      expect(text).toContain('recording.mp3');
      expect(text).toContain('video.mp4');
      expect(text).toContain(tmpDir); // absolute paths
      expect(text).not.toContain('console-');
      expect(text).not.toContain('screenshot-');
      expect(text).toContain('3 file');
    } finally {
      proc.kill();
    }
  });

  it('should filter files by pattern', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_list_downloads',
        arguments: { pattern: '*.pdf' }
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('lecture.pdf');
      expect(text).not.toContain('recording.mp3');
      expect(text).not.toContain('video.mp4');
      expect(text).toContain('1 file');
    } finally {
      proc.kill();
    }
  });

  it('should handle empty downloads directory', async () => {
    const emptyDir = fs.mkdtempSync(path.join(os.tmpdir(), 'content-mcp-empty-'));
    const proc = spawnMcpServer({ DOWNLOADS_DIR: emptyDir });
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_list_downloads',
        arguments: {}
      }, 2);
      expect(res.result.content[0].text).toContain('No files found');
    } finally {
      proc.kill();
      try { fs.rmSync(emptyDir, { recursive: true, force: true }); } catch {}
    }
  });

  // ---------------------------------------------------------------------------
  // content_upload
  // ---------------------------------------------------------------------------

  it('should upload a PDF and return job_id', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_upload',
        arguments: { file_path: path.join(tmpDir, 'lecture.pdf') }
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('job-abc-123');
      expect(res.result.isError).toBeUndefined();
    } finally {
      proc.kill();
    }
  });

  it('should auto-detect source_type from extension', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);

      // Upload MP3 — should auto-detect as audio_upload
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_upload',
        arguments: { file_path: path.join(tmpDir, 'recording.mp3') }
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('job-abc-123');

      // Verify multipart Content-Type header was sent
      expect(lastUploadHeaders['content-type']).toContain('multipart/form-data');

      // Verify the body contains audio_upload source_type
      const bodyStr = lastUploadBody.toString();
      expect(bodyStr).toContain('audio_upload');
    } finally {
      proc.kill();
    }
  });

  it('should include metadata fields in multipart upload', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_upload',
        arguments: {
          file_path: path.join(tmpDir, 'lecture.pdf'),
          title: 'My Lecture',
          author: 'Prof. Test',
          outputs: 'm4b,knowledge_base',
          kb_name: 'Test KB',
          voice: 'af_heart'
        }
      }, 2);
      expect(res.result.isError).toBeUndefined();

      const bodyStr = lastUploadBody.toString();
      expect(bodyStr).toContain('My Lecture');
      expect(bodyStr).toContain('Prof. Test');
      expect(bodyStr).toContain('m4b,knowledge_base');
      expect(bodyStr).toContain('Test KB');
      expect(bodyStr).toContain('af_heart');
    } finally {
      proc.kill();
    }
  });

  it('should error on missing file', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_upload',
        arguments: { file_path: '/nonexistent/file.pdf' }
      }, 2);
      expect(res.result.isError).toBe(true);
      expect(res.result.content[0].text).toContain('File not found');
    } finally {
      proc.kill();
    }
  });

  // ---------------------------------------------------------------------------
  // content_status
  // ---------------------------------------------------------------------------

  it('should return in-progress status with percentage', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_status',
        arguments: { job_id: 'job-in-progress' }
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('processing');
      expect(text).toContain('42%');
      expect(text).toContain('Converting chapter 3 of 7');
    } finally {
      proc.kill();
    }
  });

  it('should return completed status with output files (dict format)', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_status',
        arguments: { job_id: 'job-completed' }
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('completed');
      expect(text).toContain('100%');
      expect(text).toContain('searchable_pdf:');
      expect(text).toContain('test.pdf');
      expect(text).toContain('1.5 MB');
      expect(text).toContain('m4b:');
      expect(text).toContain('test.m4b');
    } finally {
      proc.kill();
    }
  });

  // ---------------------------------------------------------------------------
  // content_voices
  // ---------------------------------------------------------------------------

  it('should return voice list', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_voices',
        arguments: {}
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('af_heart');
      expect(text).toContain('am_bold');
      expect(text).toContain('bf_gentle');
      expect(text).toContain('Female, warm tone');
    } finally {
      proc.kill();
    }
  });

  // ---------------------------------------------------------------------------
  // content_kb_create
  // ---------------------------------------------------------------------------

  it('should create a knowledge base', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_kb_create',
        arguments: { name: 'CS6795 Spring 2026' }
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('kb-new-001');
      expect(text).toContain('CS6795 Spring 2026');
    } finally {
      proc.kill();
    }
  });

  it('should require name for kb_create', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_kb_create',
        arguments: {}
      }, 2);
      expect(res.result.isError).toBe(true);
      expect(res.result.content[0].text).toContain('name is required');
    } finally {
      proc.kill();
    }
  });

  // ---------------------------------------------------------------------------
  // content_library_list
  // ---------------------------------------------------------------------------

  it('should list library items', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_library_list',
        arguments: {}
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('Algorithms Lecture');
      expect(text).toContain('Data Structures Video');
      expect(text).toContain('ML Primer');
      expect(text).toContain('3 library item');
    } finally {
      proc.kill();
    }
  });

  it('should filter library items by course_code', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_library_list',
        arguments: { course_code: 'CS6795' }
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('Algorithms Lecture');
      expect(text).toContain('ML Primer');
      expect(text).not.toContain('Data Structures Video');
      expect(text).toContain('2 library item');
    } finally {
      proc.kill();
    }
  });

  // ---------------------------------------------------------------------------
  // content_library_tag
  // ---------------------------------------------------------------------------

  it('should update tags on a library item', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_library_tag',
        arguments: {
          item_id: 'lib-001',
          organization: 'Georgia Tech',
          course_code: 'CS6795',
          custom_tags: 'midterm,review'
        }
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('Tags updated');
      expect(text).toContain('lib-001');
    } finally {
      proc.kill();
    }
  });

  it('should require item_id for library_tag', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_library_tag',
        arguments: { course_code: 'CS6795' }
      }, 2);
      expect(res.result.isError).toBe(true);
      expect(res.result.content[0].text).toContain('item_id is required');
    } finally {
      proc.kill();
    }
  });

  // ---------------------------------------------------------------------------
  // content_library_metadata
  // ---------------------------------------------------------------------------

  it('should update title and author on a library item', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_library_metadata',
        arguments: { item_id: 'lib-002', title: 'Updated Title', author: 'New Author' }
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('Metadata updated');
      expect(text).toContain('lib-002');
    } finally {
      proc.kill();
    }
  });

  // ---------------------------------------------------------------------------
  // content_library_ingest
  // ---------------------------------------------------------------------------

  it('should re-ingest a library item into a KB', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_library_ingest',
        arguments: { item_id: 'lib-001', kb_id: 'kb-002' }
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('Re-ingest started');
      expect(text).toContain('lib-001');
      expect(text).toContain('job-abc-123');
      expect(text).toContain('kb-002');

      // Verify it uses /api/upload with library_pdf source_type
      const bodyStr = lastUploadBody.toString();
      expect(bodyStr).toContain('source_type');
      expect(bodyStr).toContain('library_pdf');
      expect(bodyStr).toContain('library_id');
      expect(bodyStr).toContain('lib-001');
      expect(bodyStr).toContain('kb_id');
      expect(bodyStr).toContain('kb-002');
    } finally {
      proc.kill();
    }
  });

  it('should require item_id for library_ingest', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_library_ingest',
        arguments: { kb_id: 'kb-002' }
      }, 2);
      expect(res.result.isError).toBe(true);
      expect(res.result.content[0].text).toContain('item_id is required');
    } finally {
      proc.kill();
    }
  });

  // ---------------------------------------------------------------------------
  // content_library_delete
  // ---------------------------------------------------------------------------

  it('should delete a library item', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_library_delete',
        arguments: { item_id: 'lib-001' }
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('lib-001');
      expect(text).toContain('deleted');
      expect(lastDeletedItemId).toBe('lib-001');
    } finally {
      proc.kill();
    }
  });

  it('should require item_id for library_delete', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_library_delete',
        arguments: {}
      }, 2);
      expect(res.result.isError).toBe(true);
      expect(res.result.content[0].text).toContain('item_id is required');
    } finally {
      proc.kill();
    }
  });

  // ---------------------------------------------------------------------------
  // content_status fixes
  // ---------------------------------------------------------------------------

  it('should show error field in failed status', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_status',
        arguments: { job_id: 'job-failed' }
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('failed');
      expect(text).toContain('Error:');
      expect(text).toContain('Video processing failed: audio too short');
    } finally {
      proc.kill();
    }
  });

  it('should show output_files as array (legacy format)', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_status',
        arguments: { job_id: 'job-completed-array' }
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('completed');
      expect(text).toContain('/output/legacy.m4b');
      expect(text).toContain('/output/legacy.pdf');
    } finally {
      proc.kill();
    }
  });

  // ---------------------------------------------------------------------------
  // content_stage
  // ---------------------------------------------------------------------------

  it('should stage a file with auto-order', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const filePath = path.join(tmpDir, 'video.mp4');
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_stage',
        arguments: { file_path: filePath }
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('Staged: #1');
      expect(text).toContain('video.mp4');
      expect(text).toContain('1 file(s) staged');
    } finally {
      proc.kill();
    }
  });

  it('should stage with explicit order and name', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const filePath = path.join(tmpDir, 'video.mp4');
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_stage',
        arguments: { file_path: filePath, name: '05_Conclusion.mp4', order: 5 }
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('Staged: #5');
      expect(text).toContain('05_Conclusion.mp4');
      expect(text).toContain('1 file(s) staged');
    } finally {
      proc.kill();
    }
  });

  it('should reject staging a missing file', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_stage',
        arguments: { file_path: '/nonexistent/missing.mp4' }
      }, 2);
      expect(res.result.isError).toBe(true);
      expect(res.result.content[0].text).toContain('File not found');
    } finally {
      proc.kill();
    }
  });

  // ---------------------------------------------------------------------------
  // content_staged_list
  // ---------------------------------------------------------------------------

  it('should list staged files in order with sizes', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      // Stage two files
      await sendJsonRpc(proc, 'tools/call', {
        name: 'content_stage',
        arguments: { file_path: path.join(tmpDir, 'video.mp4'), name: '01_Intro.mp4' }
      }, 2);
      await sendJsonRpc(proc, 'tools/call', {
        name: 'content_stage',
        arguments: { file_path: path.join(tmpDir, 'recording.mp3'), name: '02_Audio.mp3' }
      }, 3);

      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_staged_list',
        arguments: {}
      }, 4);
      const text = res.result.content[0].text;
      expect(text).toContain('Staged files (2)');
      expect(text).toContain('01_Intro.mp4');
      expect(text).toContain('02_Audio.mp3');
      expect(text).toContain('Total:');
    } finally {
      proc.kill();
    }
  });

  it('should return empty message when nothing staged', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_staged_list',
        arguments: {}
      }, 2);
      const text = res.result.content[0].text;
      expect(text).toContain('No files staged');
    } finally {
      proc.kill();
    }
  });

  // ---------------------------------------------------------------------------
  // content_staged_clear
  // ---------------------------------------------------------------------------

  it('should clear staging area and reset', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      // Stage a file
      await sendJsonRpc(proc, 'tools/call', {
        name: 'content_stage',
        arguments: { file_path: path.join(tmpDir, 'video.mp4') }
      }, 2);

      // Clear
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_staged_clear',
        arguments: {}
      }, 3);
      expect(res.result.content[0].text).toContain('Cleared 1 staged file(s)');

      // Verify empty
      const listRes = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_staged_list',
        arguments: {}
      }, 4);
      expect(listRes.result.content[0].text).toContain('No files staged');
    } finally {
      proc.kill();
    }
  });

  // ---------------------------------------------------------------------------
  // content_batch_convert
  // ---------------------------------------------------------------------------

  it('should reject batch convert when nothing staged', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_batch_convert',
        arguments: { title: 'Test Batch' }
      }, 2);
      expect(res.result.isError).toBe(true);
      expect(res.result.content[0].text).toContain('No files staged');
    } finally {
      proc.kill();
    }
  });

  it('should create ZIP from staged files and upload as archive_upload', async () => {
    const proc = spawnMcpServer();
    try {
      await sendJsonRpc(proc, 'initialize', {}, 0);
      // Stage two files
      await sendJsonRpc(proc, 'tools/call', {
        name: 'content_stage',
        arguments: { file_path: path.join(tmpDir, 'video.mp4'), name: '01_Intro.mp4' }
      }, 2);
      await sendJsonRpc(proc, 'tools/call', {
        name: 'content_stage',
        arguments: { file_path: path.join(tmpDir, 'recording.mp3'), name: '02_Audio.mp3' }
      }, 3);

      // Batch convert
      const res = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_batch_convert',
        arguments: { title: 'Lesson 25', outputs: 'searchable_pdf', kb_name: 'CS7637' }
      }, 4);
      const text = res.result.content[0].text;
      expect(res.result.isError).toBeUndefined();
      expect(text).toContain('Batch upload started: 2 files');
      expect(text).toContain('job-abc-123');

      // Verify upload was archive_upload
      const bodyStr = lastUploadBody.toString();
      expect(bodyStr).toContain('archive_upload');
      expect(bodyStr).toContain('.zip');
      expect(bodyStr).toContain('Lesson 25');
      expect(bodyStr).toContain('searchable_pdf');
      expect(bodyStr).toContain('CS7637');

      // Verify staging was cleared after success
      const listRes = await sendJsonRpc(proc, 'tools/call', {
        name: 'content_staged_list',
        arguments: {}
      }, 5);
      expect(listRes.result.content[0].text).toContain('No files staged');
    } finally {
      proc.kill();
    }
  });
});
