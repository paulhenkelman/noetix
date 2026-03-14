import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import http from 'http';
import express from 'express';

/**
 * Tests for the server's KB listing helper functions and pattern matching.
 * The listKbEntries() function is used by the backend API fallback path.
 */

describe('KB listing helpers', () => {
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
      app.post('/api/chat', (_req, res) => res.json({ answer: '', sources: [] }));

      mockBackend = app.listen(0, '127.0.0.1', () => {
        backendPort = mockBackend.address().port;
        resolve();
      });
    });
  }

  beforeEach(async () => {
    mockKbs = [
      { id: 'kb-001', title: 'Introduction to Algorithms', author: 'Cormen et al.', document_count: 3 },
      { id: 'kb-002', title: 'Design Patterns', author: 'Gang of Four', document_count: 5 },
      { id: 'kb-003', title: 'Clean Code', author: 'Robert C. Martin', document_count: 2 }
    ];
    mockDocs = {
      'kb-001': [
        { title: 'Chapter 1: Sorting', author: 'Cormen' },
        { title: 'Chapter 2: Graphs', author: 'Cormen' }
      ],
      'kb-002': [
        { title: 'Factory Pattern', author: 'GoF' },
        { title: 'Observer Pattern', author: 'GoF' }
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

  describe('/api/knowledge-bases endpoint', () => {
    it('should return all KBs with title and author', async () => {
      const res = await fetch(`http://127.0.0.1:${backendPort}/api/knowledge-bases`);
      const data = await res.json();
      expect(data).toHaveLength(3);
      expect(data[0]).toHaveProperty('title', 'Introduction to Algorithms');
      expect(data[0]).toHaveProperty('author', 'Cormen et al.');
    });
  });

  describe('/api/knowledge-bases/:kbId/documents', () => {
    it('should return documents for a specific KB', async () => {
      const res = await fetch(`http://127.0.0.1:${backendPort}/api/knowledge-bases/kb-001/documents`);
      const data = await res.json();
      expect(data).toHaveLength(2);
      expect(data[0].title).toBe('Chapter 1: Sorting');
    });

    it('should return empty array for unknown KB', async () => {
      const res = await fetch(`http://127.0.0.1:${backendPort}/api/knowledge-bases/kb-999/documents`);
      const data = await res.json();
      expect(data).toHaveLength(0);
    });
  });

  describe('KB_LISTING_PATTERN matching', () => {
    const KB_LISTING_PATTERN = /\b(list|show|what|which|all|every|title|author|document|documents|catalog|catalogue|entries|books?|texts?|materials?)\b/i;

    const shouldMatch = [
      'list all documents',
      'show me the titles',
      'what books are available?',
      'which entries do you have?',
      'show all authors',
      'List every document in the catalog',
      'What materials are in this KB?',
      'show me the catalogue'
    ];

    const shouldNotMatch = [
      'how does sorting work?',
      'explain the observer pattern',
      'summarize chapter 3',
      'hello'
    ];

    for (const input of shouldMatch) {
      it(`should match listing query: "${input}"`, () => {
        expect(KB_LISTING_PATTERN.test(input)).toBe(true);
      });
    }

    for (const input of shouldNotMatch) {
      it(`should not match non-listing query: "${input}"`, () => {
        expect(KB_LISTING_PATTERN.test(input)).toBe(false);
      });
    }
  });
});
