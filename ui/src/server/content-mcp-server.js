#!/usr/bin/env node
/**
 * Noetix Content Services MCP Server
 *
 * Exposes the Noetix backend content pipeline as MCP tools over stdio transport.
 * Tools:
 *   - content_list_downloads   — List files in the Playwright download directory
 *   - content_upload           — Upload a local file for conversion and/or KB ingestion
 *   - content_status           — Check conversion job progress
 *   - content_voices           — List available TTS voices
 *   - content_kb_create        — Create a new knowledge base
 *   - content_library_list     — List processed content in the library
 *   - content_library_tag      — Update tags on a library item
 *   - content_library_metadata — Update title/author on a library item
 *   - content_library_ingest   — Re-ingest a library item into a different KB
 *
 * Configuration: reads ui.config via config.js; env vars override.
 */

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import http from 'node:http';
import https from 'node:https';
import os from 'node:os';
import { execSync } from 'node:child_process';
import { createInterface } from 'node:readline';
import config from './config.js';

const REMOTE_BASE = process.env.REMOTE_BASE || config.remoteBase;
const DOWNLOADS_DIR = process.env.DOWNLOADS_DIR || config.downloadsDir;

// Batch staging state
const stagedFiles = new Map(); // order (int) -> { path, name, order }
let nextStageOrder = 1;

// ---------------------------------------------------------------------------
// MIME type map
// ---------------------------------------------------------------------------

const MIME_MAP = {
  '.pdf': 'application/pdf',
  '.mp3': 'audio/mpeg',
  '.mp4': 'video/mp4',
  '.m4a': 'audio/mp4',
  '.m4b': 'audio/mp4',
  '.mkv': 'video/x-matroska',
  '.webm': 'video/webm',
  '.avi': 'video/x-msvideo',
  '.mov': 'video/quicktime',
  '.m4v': 'video/mp4',
  '.zip': 'application/zip',
  '.wav': 'audio/wav',
  '.aac': 'audio/aac',
};

// ---------------------------------------------------------------------------
// Source type auto-detection from file extension
// ---------------------------------------------------------------------------

const SOURCE_TYPE_MAP = {
  '.pdf': 'upload',
  '.mp3': 'audio_upload',
  '.m4b': 'audio_upload',
  '.m4a': 'audio_upload',
  '.aac': 'audio_upload',
  '.wav': 'audio_upload',
  '.mp4': 'video_upload',
  '.mkv': 'video_upload',
  '.webm': 'video_upload',
  '.avi': 'video_upload',
  '.mov': 'video_upload',
  '.m4v': 'video_upload',
  '.zip': 'archive_upload',
};

// ---------------------------------------------------------------------------
// Simple HTTP JSON client (same pattern as kb-mcp-server)
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
// Multipart upload client
// ---------------------------------------------------------------------------

function multipartUpload(urlPath, filePath, fields = {}) {
  const boundary = crypto.randomBytes(16).toString('hex');
  const base = new URL(REMOTE_BASE);
  const mod = base.protocol === 'https:' ? https : http;

  // Build parts as buffers
  const parts = [];

  // Text field parts
  for (const [key, value] of Object.entries(fields)) {
    if (value === undefined || value === null) continue;
    parts.push(Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="${key}"\r\n\r\n${value}\r\n`
    ));
  }

  // File part (optional — library_pdf re-ingest sends no file)
  if (filePath) {
    const fileName = path.basename(filePath);
    const ext = path.extname(filePath).toLowerCase();
    const mimeType = MIME_MAP[ext] || 'application/octet-stream';
    const fileHeader = Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="files"; filename="${fileName}"\r\nContent-Type: ${mimeType}\r\n\r\n`
    );
    const fileData = fs.readFileSync(filePath);
    const fileTrailer = Buffer.from('\r\n');

    parts.push(fileHeader);
    parts.push(fileData);
    parts.push(fileTrailer);
  }

  // Closing boundary
  parts.push(Buffer.from(`--${boundary}--\r\n`));

  const bodyBuffer = Buffer.concat(parts);

  return new Promise((resolve, reject) => {
    const req = mod.request(
      {
        hostname: base.hostname,
        port: base.port,
        path: urlPath,
        method: 'POST',
        headers: {
          'Content-Type': `multipart/form-data; boundary=${boundary}`,
          'Content-Length': bodyBuffer.length
        },
        timeout: 120000
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
    req.on('timeout', () => { req.destroy(); reject(new Error('Upload timed out')); });
    req.write(bodyBuffer);
    req.end();
  });
}

// ---------------------------------------------------------------------------
// Tool definitions
// ---------------------------------------------------------------------------

const TOOLS = [
  // Group A — File Discovery
  {
    name: 'content_list_downloads',
    description: 'List files in the Playwright download directory available for processing. Use after downloading files via browser tools to find their paths.',
    inputSchema: {
      type: 'object',
      properties: {
        pattern: { type: 'string', description: 'Substring or extension filter (e.g. "*.pdf" or "lecture")' }
      },
      required: []
    }
  },

  // Group B — Conversion Pipeline
  {
    name: 'content_upload',
    description: 'Upload a local file for conversion (PDF→audiobook, video→transcription, etc.) and/or KB ingestion. Returns a job_id to track progress.',
    inputSchema: {
      type: 'object',
      properties: {
        file_path: { type: 'string', description: 'Absolute path to the local file' },
        outputs: { type: 'string', description: 'Comma-separated outputs: m4b, searchable_pdf, combined_pdf, knowledge_base (default: knowledge_base)' },
        source_type: { type: 'string', description: 'upload, audio_upload, video_upload, or archive_upload (auto-detected from extension if omitted)' },
        title: { type: 'string', description: 'Title metadata' },
        author: { type: 'string', description: 'Author metadata' },
        kb_id: { type: 'string', description: 'Existing KB to ingest into' },
        kb_name: { type: 'string', description: 'Create new KB with this name (if kb_id not provided)' },
        voice: { type: 'string', description: 'TTS voice for audiobook output (default: af_heart)' }
      },
      required: ['file_path']
    }
  },
  {
    name: 'content_status',
    description: 'Check conversion job progress. Returns status, percentage, current task, and output files when complete.',
    inputSchema: {
      type: 'object',
      properties: {
        job_id: { type: 'string', description: 'The job ID returned by content_upload' }
      },
      required: ['job_id']
    }
  },
  {
    name: 'content_voices',
    description: 'List available TTS voices for audiobook conversion.',
    inputSchema: {
      type: 'object',
      properties: {},
      required: []
    }
  },

  // Group C — Knowledge Base Management
  {
    name: 'content_kb_create',
    description: 'Create a new knowledge base for content ingestion.',
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string', description: 'Name for the new knowledge base' },
        description: { type: 'string', description: 'Optional description' }
      },
      required: ['name']
    }
  },

  // Group D — Library Management
  {
    name: 'content_library_list',
    description: 'List processed content in the library with optional filters.',
    inputSchema: {
      type: 'object',
      properties: {
        organization: { type: 'string', description: 'Filter by organization' },
        course_code: { type: 'string', description: 'Filter by course code' },
        has_kb: { type: 'boolean', description: 'Filter to items with/without a knowledge base' }
      },
      required: []
    }
  },
  {
    name: 'content_library_tag',
    description: 'Update tags on a library item (for organizing by course/module/topic).',
    inputSchema: {
      type: 'object',
      properties: {
        item_id: { type: 'string', description: 'The library item ID' },
        organization: { type: 'string', description: 'Organization tag' },
        course_code: { type: 'string', description: 'Course code tag' },
        course_id: { type: 'string', description: 'Course ID tag' },
        module_id: { type: 'string', description: 'Module ID tag' },
        custom_tags: { type: 'string', description: 'Comma-separated custom tags' }
      },
      required: ['item_id']
    }
  },
  {
    name: 'content_library_metadata',
    description: 'Update title and author on a library item.',
    inputSchema: {
      type: 'object',
      properties: {
        item_id: { type: 'string', description: 'The library item ID' },
        title: { type: 'string', description: 'New title' },
        author: { type: 'string', description: 'New author' }
      },
      required: ['item_id']
    }
  },
  {
    name: 'content_library_ingest',
    description: 'Re-ingest an existing library item into a knowledge base. Provide kb_id for an existing KB, or kb_name to create a new one.',
    inputSchema: {
      type: 'object',
      properties: {
        item_id: { type: 'string', description: 'The library item ID' },
        kb_id: { type: 'string', description: 'Existing KB ID to ingest into' },
        kb_name: { type: 'string', description: 'Create a new KB with this name (used only if kb_id is not provided)' }
      },
      required: ['item_id']
    }
  },
  {
    name: 'content_library_delete',
    description: 'Delete a library item and its associated files/KB vectors. Use to clean up unwanted or duplicate content.',
    inputSchema: {
      type: 'object',
      properties: {
        item_id: { type: 'string', description: 'The library item ID to delete' }
      },
      required: ['item_id']
    }
  },

  // Group E — Batch Staging
  {
    name: 'content_stage',
    description: 'Stage a local file for batch conversion. Use to collect multiple files before uploading them as a single combined archive.',
    inputSchema: {
      type: 'object',
      properties: {
        file_path: { type: 'string', description: 'Absolute path to the local file' },
        name: { type: 'string', description: 'Filename in archive (defaults to basename of file_path)' },
        order: { type: 'integer', description: 'Ordering number (defaults to auto-increment)' }
      },
      required: ['file_path']
    }
  },
  {
    name: 'content_staged_list',
    description: 'List all currently staged files with order, name, path, and size.',
    inputSchema: {
      type: 'object',
      properties: {},
      required: []
    }
  },
  {
    name: 'content_staged_clear',
    description: 'Clear the staging area, removing all staged files from the batch.',
    inputSchema: {
      type: 'object',
      properties: {},
      required: []
    }
  },
  {
    name: 'content_batch_convert',
    description: 'ZIP all staged files and upload as an archive for combined processing. Creates one combined document from all staged files (e.g. multiple videos → single PDF with global chapter numbering).',
    inputSchema: {
      type: 'object',
      properties: {
        title: { type: 'string', description: 'Title for the combined document' },
        author: { type: 'string', description: 'Author metadata' },
        outputs: { type: 'string', description: 'Comma-separated outputs: searchable_pdf, m4b, knowledge_base (default: searchable_pdf)' },
        kb_id: { type: 'string', description: 'Existing KB to ingest into' },
        kb_name: { type: 'string', description: 'Create new KB with this name' },
        voice: { type: 'string', description: 'TTS voice for audiobook output' }
      },
      required: []
    }
  }
];

// ---------------------------------------------------------------------------
// Tool implementations
// ---------------------------------------------------------------------------

async function handleToolCall(name, args) {
  switch (name) {

    // --- Group A: File Discovery ---

    case 'content_list_downloads': {
      const { pattern } = args;
      let dir;
      try {
        dir = fs.readdirSync(DOWNLOADS_DIR);
      } catch (err) {
        return { isError: true, content: [{ type: 'text', text: `Cannot read downloads directory: ${err.message}` }] };
      }

      // Exclude Playwright artifacts (console logs, screenshots)
      let files = dir.filter(f => !f.startsWith('console-') && !f.startsWith('screenshot-'));

      // Apply pattern filter
      if (pattern) {
        const p = pattern.replace(/^\*/, ''); // "*.pdf" → ".pdf"
        files = files.filter(f => f.toLowerCase().includes(p.toLowerCase()));
      }

      // Build file info sorted by modification time (newest first)
      const fileInfos = files.map(f => {
        const fullPath = path.join(DOWNLOADS_DIR, f);
        try {
          const stat = fs.statSync(fullPath);
          return {
            name: f,
            path: fullPath,
            size_mb: (stat.size / (1024 * 1024)).toFixed(2),
            modified: stat.mtime.toISOString()
          };
        } catch {
          return { name: f, path: fullPath, size_mb: '?', modified: '?' };
        }
      }).sort((a, b) => (b.modified > a.modified ? 1 : -1));

      if (!fileInfos.length) {
        return { content: [{ type: 'text', text: 'No files found in downloads directory.' }] };
      }

      const lines = fileInfos.map((f, i) =>
        `${i + 1}. ${f.name} (${f.size_mb} MB) — ${f.path}`
      );
      return { content: [{ type: 'text', text: `Found ${fileInfos.length} file(s):\n\n${lines.join('\n')}` }] };
    }

    // --- Group B: Conversion Pipeline ---

    case 'content_upload': {
      const { file_path, outputs, source_type, title, author, kb_id, kb_name, voice } = args;
      if (!file_path) {
        return { isError: true, content: [{ type: 'text', text: 'file_path is required' }] };
      }

      // Verify file exists
      if (!fs.existsSync(file_path)) {
        return { isError: true, content: [{ type: 'text', text: `File not found: ${file_path}` }] };
      }

      // Auto-detect source_type from extension
      const ext = path.extname(file_path).toLowerCase();
      const detectedType = source_type || SOURCE_TYPE_MAP[ext] || 'upload';

      const fields = {
        source_type: detectedType,
        outputs: outputs || 'knowledge_base',
      };
      if (title) fields.title = title;
      if (author) fields.author = author;
      if (kb_id) fields.kb_id = kb_id;
      if (kb_name) fields.kb_name = kb_name;
      if (voice) fields.voice = voice;

      const res = await multipartUpload('/api/upload', file_path, fields);
      if (res.status < 200 || res.status >= 300) {
        return { isError: true, content: [{ type: 'text', text: `Upload failed (HTTP ${res.status}): ${JSON.stringify(res.data)}` }] };
      }

      const jobId = res.data?.job_id || res.data?.id || '(unknown)';
      const msg = res.data?.message || `File uploaded successfully. Source type: ${detectedType}`;
      return { content: [{ type: 'text', text: `Job ID: ${jobId}\n${msg}` }] };
    }

    case 'content_status': {
      const { job_id } = args;
      if (!job_id) {
        return { isError: true, content: [{ type: 'text', text: 'job_id is required' }] };
      }

      const res = await request('GET', `/api/status/${job_id}`);
      if (res.status < 200 || res.status >= 300) {
        return { isError: true, content: [{ type: 'text', text: `Status check failed (HTTP ${res.status}): ${JSON.stringify(res.data)}` }] };
      }

      const d = res.data;
      const progress = d.progress ?? 0;
      const barLen = 20;
      const filled = Math.round((progress / 100) * barLen);
      const bar = '█'.repeat(filled) + '░'.repeat(barLen - filled);

      let text = `Status: ${d.status || 'unknown'}\nProgress: [${bar}] ${progress}%`;
      if (d.current_task) text += `\nCurrent task: ${d.current_task}`;
      if (d.title) text += `\nTitle: ${d.title}`;
      if (d.error) text += `\nError: ${d.error}`;
      if (d.output_files && typeof d.output_files === 'object' && !Array.isArray(d.output_files)) {
        const entries = Object.entries(d.output_files);
        if (entries.length) {
          text += `\nOutput files:`;
          for (const [type, info] of entries) {
            if (typeof info === 'string' && info.startsWith('error:')) {
              text += `\n  - ${type}: FAILED — ${info}`;
            } else if (typeof info === 'object') {
              text += `\n  - ${type}: ${info.filename || info.path || type} (${info.file_size_mb || '?'} MB)`;
            } else {
              text += `\n  - ${type}: ${info}`;
            }
          }
        }
      } else if (Array.isArray(d.output_files) && d.output_files.length) {
        text += `\nOutput files:\n${d.output_files.map(f => `  - ${f}`).join('\n')}`;
      }
      return { content: [{ type: 'text', text }] };
    }

    case 'content_voices': {
      const res = await request('GET', '/api/voices');
      if (res.status < 200 || res.status >= 300) {
        return { isError: true, content: [{ type: 'text', text: `Failed to fetch voices (HTTP ${res.status}): ${JSON.stringify(res.data)}` }] };
      }

      const voices = Array.isArray(res.data) ? res.data : (res.data?.voices || []);
      if (!voices.length) {
        return { content: [{ type: 'text', text: 'No voices available.' }] };
      }

      const lines = voices.map((v, i) =>
        `${i + 1}. **${v.name || v.id}**${v.description ? ` — ${v.description}` : ''}`
      );
      return { content: [{ type: 'text', text: `Available voices:\n\n${lines.join('\n')}` }] };
    }

    // --- Group C: Knowledge Base Management ---

    case 'content_kb_create': {
      const { name, description } = args;
      if (!name) {
        return { isError: true, content: [{ type: 'text', text: 'name is required' }] };
      }

      const body = { name };
      if (description) body.description = description;

      const res = await request('POST', '/api/knowledge-bases', body);
      if (res.status < 200 || res.status >= 300) {
        return { isError: true, content: [{ type: 'text', text: `Failed to create KB (HTTP ${res.status}): ${JSON.stringify(res.data)}` }] };
      }

      return { content: [{ type: 'text', text: `Knowledge base created:\n  ID: ${res.data.id}\n  Name: ${res.data.name}` }] };
    }

    // --- Group D: Library Management ---

    case 'content_library_list': {
      const { organization, course_code, has_kb } = args;
      const params = new URLSearchParams();
      if (organization) params.set('organization', organization);
      if (course_code) params.set('course_code', course_code);
      if (has_kb !== undefined) params.set('has_kb', String(has_kb));

      const qs = params.toString();
      const urlPath = `/api/library${qs ? '?' + qs : ''}`;
      const res = await request('GET', urlPath);
      if (res.status < 200 || res.status >= 300) {
        return { isError: true, content: [{ type: 'text', text: `Library list failed (HTTP ${res.status}): ${JSON.stringify(res.data)}` }] };
      }

      const items = Array.isArray(res.data) ? res.data : [];
      if (!items.length) {
        return { content: [{ type: 'text', text: 'No items found in the library.' }] };
      }

      const lines = items.map((item, i) => {
        let line = `${i + 1}. **${item.title || '(untitled)'}** by ${item.author || 'Unknown'} [id: ${item.id}]`;
        if (item.outputs) line += ` | outputs: ${item.outputs}`;
        if (item.tags) line += ` | tags: ${JSON.stringify(item.tags)}`;
        if (item.kb_id) line += ` | kb: ${item.kb_id}`;
        return line;
      });
      return { content: [{ type: 'text', text: `Found ${items.length} library item(s):\n\n${lines.join('\n')}` }] };
    }

    case 'content_library_tag': {
      const { item_id, organization, course_code, course_id, module_id, custom_tags } = args;
      if (!item_id) {
        return { isError: true, content: [{ type: 'text', text: 'item_id is required' }] };
      }

      const body = {};
      if (organization) body.organization = organization;
      if (course_code) body.course_code = course_code;
      if (course_id) body.course_id = course_id;
      if (module_id) body.module_id = module_id;
      if (custom_tags) body.custom_tags = custom_tags.split(',').map(t => t.trim());

      const res = await request('PATCH', `/api/library/${item_id}/tags`, body);
      if (res.status < 200 || res.status >= 300) {
        return { isError: true, content: [{ type: 'text', text: `Tag update failed (HTTP ${res.status}): ${JSON.stringify(res.data)}` }] };
      }

      return { content: [{ type: 'text', text: `Tags updated for item ${item_id}.` }] };
    }

    case 'content_library_metadata': {
      const { item_id, title, author } = args;
      if (!item_id) {
        return { isError: true, content: [{ type: 'text', text: 'item_id is required' }] };
      }

      const body = {};
      if (title) body.title = title;
      if (author) body.author = author;

      const res = await request('PATCH', `/api/library/${item_id}/metadata`, body);
      if (res.status < 200 || res.status >= 300) {
        return { isError: true, content: [{ type: 'text', text: `Metadata update failed (HTTP ${res.status}): ${JSON.stringify(res.data)}` }] };
      }

      return { content: [{ type: 'text', text: `Metadata updated for item ${item_id}.` }] };
    }

    case 'content_library_ingest': {
      const { item_id, kb_id, kb_name } = args;
      if (!item_id) {
        return { isError: true, content: [{ type: 'text', text: 'item_id is required' }] };
      }
      if (!kb_id && !kb_name) {
        return { isError: true, content: [{ type: 'text', text: 'kb_id or kb_name is required' }] };
      }

      // Use /api/upload with source_type=library_pdf instead of /api/library/{id}/ingest
      // because the latter has a "table already exists" bug on KBs with existing documents.
      const fields = {
        source_type: 'library_pdf',
        library_id: item_id,
        outputs: 'knowledge_base',
      };
      if (kb_id) fields.kb_id = kb_id;
      if (kb_name) fields.kb_name = kb_name;

      const res = await multipartUpload('/api/upload', null, fields);
      if (res.status < 200 || res.status >= 300) {
        return { isError: true, content: [{ type: 'text', text: `Ingest failed (HTTP ${res.status}): ${JSON.stringify(res.data)}` }] };
      }

      const jobId = res.data?.job_id || res.data?.id || '(unknown)';
      return { content: [{ type: 'text', text: `Re-ingest started for item ${item_id}.\nJob ID: ${jobId}\nKB: ${kb_id || kb_name}` }] };
    }

    case 'content_library_delete': {
      const { item_id } = args;
      if (!item_id) {
        return { isError: true, content: [{ type: 'text', text: 'item_id is required' }] };
      }

      const res = await request('DELETE', `/api/library/${item_id}`);
      if (res.status < 200 || res.status >= 300) {
        return { isError: true, content: [{ type: 'text', text: `Delete failed (HTTP ${res.status}): ${JSON.stringify(res.data)}` }] };
      }

      return { content: [{ type: 'text', text: `Library item ${item_id} deleted.` }] };
    }

    // --- Group E: Batch Staging ---

    case 'content_stage': {
      const { file_path, name, order } = args;
      if (!file_path) return { isError: true, content: [{ type: 'text', text: 'file_path is required' }] };
      if (!fs.existsSync(file_path)) return { isError: true, content: [{ type: 'text', text: `File not found: ${file_path}` }] };

      const assignedOrder = order != null ? Number(order) : nextStageOrder++;
      const assignedName = name || path.basename(file_path);
      stagedFiles.set(assignedOrder, { path: path.resolve(file_path), name: assignedName, order: assignedOrder });

      // Advance nextStageOrder past any manually-set orders
      if (assignedOrder >= nextStageOrder) nextStageOrder = assignedOrder + 1;

      return { content: [{ type: 'text', text: `Staged: #${assignedOrder} "${assignedName}"\n${stagedFiles.size} file(s) staged total.` }] };
    }

    case 'content_staged_list': {
      if (!stagedFiles.size) return { content: [{ type: 'text', text: 'No files staged. Use content_stage to add files.' }] };

      const sorted = [...stagedFiles.values()].sort((a, b) => a.order - b.order);
      let totalMb = 0;
      const lines = sorted.map(e => {
        let sizeMb = '?';
        try { const s = fs.statSync(e.path); sizeMb = (s.size / (1024 * 1024)).toFixed(2); totalMb += s.size / (1024 * 1024); } catch {}
        return `  ${e.order}. ${e.name} (${sizeMb} MB) — ${e.path}`;
      });
      return { content: [{ type: 'text', text: `Staged files (${sorted.length}):\n${lines.join('\n')}\n\nTotal: ${totalMb.toFixed(1)} MB` }] };
    }

    case 'content_staged_clear': {
      const count = stagedFiles.size;
      stagedFiles.clear();
      nextStageOrder = 1;
      return { content: [{ type: 'text', text: count ? `Cleared ${count} staged file(s).` : 'Staging area was already empty.' }] };
    }

    case 'content_batch_convert': {
      if (!stagedFiles.size) return { isError: true, content: [{ type: 'text', text: 'No files staged. Use content_stage first.' }] };

      const { title, author, outputs, kb_id, kb_name, voice } = args;
      const sorted = [...stagedFiles.values()].sort((a, b) => a.order - b.order);

      // Create temp dir and symlink staged files with ordered names
      const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ccbatch-'));
      try {
        for (const entry of sorted) {
          const prefix = String(entry.order).padStart(3, '0');
          const ext = path.extname(entry.name);
          const base = path.basename(entry.name, ext);
          const linkName = `${prefix}_${base}${ext}`;
          fs.symlinkSync(entry.path, path.join(tmpDir, linkName));
        }

        // Create ZIP
        const zipPath = `${tmpDir}.zip`;
        execSync(`cd "${tmpDir}" && zip -j "${zipPath}" *`, { timeout: 120000 });

        // Upload as archive
        const fields = { source_type: 'archive_upload', outputs: outputs || 'searchable_pdf' };
        if (title) fields.title = title;
        if (author) fields.author = author;
        if (kb_id) fields.kb_id = kb_id;
        if (kb_name) fields.kb_name = kb_name;
        if (voice) fields.voice = voice;

        const res = await multipartUpload('/api/upload', zipPath, fields);

        // Cleanup
        fs.rmSync(tmpDir, { recursive: true, force: true });
        try { fs.unlinkSync(zipPath); } catch {}

        if (res.status < 200 || res.status >= 300) {
          return { isError: true, content: [{ type: 'text', text: `Batch upload failed (HTTP ${res.status}): ${JSON.stringify(res.data)}` }] };
        }

        // Clear staging on success
        const fileCount = stagedFiles.size;
        stagedFiles.clear();
        nextStageOrder = 1;

        const jobId = res.data?.job_id || '(unknown)';
        return { content: [{ type: 'text', text: `Batch upload started: ${fileCount} files\nJob ID: ${jobId}\n${res.data?.message || 'Processing...'}` }] };

      } catch (err) {
        // Cleanup on error
        try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch {}
        try { fs.unlinkSync(`${tmpDir}.zip`); } catch {}
        return { isError: true, content: [{ type: 'text', text: `Batch convert failed: ${err.message}` }] };
      }
    }

    default:
      return { isError: true, content: [{ type: 'text', text: `Unknown tool: ${name}` }] };
  }
}

// ---------------------------------------------------------------------------
// JSON-RPC stdio transport (MCP protocol)
// ---------------------------------------------------------------------------

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
        serverInfo: { name: 'noetix-content', version: '1.0.0' }
      });

    case 'notifications/initialized':
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

process.stderr.write(`[noetix-content-mcp] Started. Backend: ${REMOTE_BASE}, Downloads: ${DOWNLOADS_DIR}\n`);
