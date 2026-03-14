#!/usr/bin/env python3
"""
Web API Server for PDF to M4B Audiobook Pipeline
Provides REST API for uploading PDFs, tracking progress, and downloading audiobooks.
"""

import asyncio
import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Any
import threading
import subprocess
import logging

# Load config from knowledge.config (TOML), with .env fallback for secrets
from dotenv import load_dotenv
load_dotenv()
from config.settings import settings

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Form
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import aiofiles

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Knowledge Base imports (lazy loaded to avoid import errors if deps missing)
_kb_pipeline = None
_kb_registry = None

# Configuration — paths from knowledge.config
UPLOAD_DIR = Path(settings.uploads_dir)
LIBRARY_DIR = Path(settings.library_dir)
KB_DIR = Path(settings.knowledge_bases_dir)
UPLOAD_DIR.mkdir(exist_ok=True)
LIBRARY_DIR.mkdir(exist_ok=True)
KB_DIR.mkdir(exist_ok=True)

# In-memory job tracking
jobs: dict[str, dict] = {}

app = FastAPI(title="Noetix", description="Transform PDFs into audiobooks, searchable documents, and knowledge bases")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# V1 action-lifecycle & audit router
from v1_actions import router as v1_actions_router
app.include_router(v1_actions_router)


class JobStatus(BaseModel):
    job_id: str
    status: str  # pending, extracting, generating, building, completed, failed
    progress: float  # 0-100
    current_chapter: Optional[int] = None
    total_chapters: Optional[int] = None
    current_task: Optional[str] = None
    title: Optional[str] = None
    author: Optional[str] = None
    error: Optional[str] = None
    output_file: Optional[str] = None
    duration_minutes: Optional[float] = None
    file_size_mb: Optional[float] = None
    # Multi-output support
    requested_outputs: Optional[list[str]] = None  # ["m4b", "searchable_pdf", "combined_pdf"]
    output_files: Optional[dict[str, Any]] = None  # {"m4b": "path", "knowledge_base": {"kb_id": ..., "document_id": ...}, ...}


class LibraryItemTags(BaseModel):
    """Hierarchical tags for organizing library items."""
    organization: str = ""
    course_code: str = ""
    course_id: Optional[str] = None  # Link to Course in KB
    module_id: Optional[str] = None  # Link to Module in KB
    topic_ids: list[str] = Field(default_factory=list)  # Links to Topics in KB
    custom_tags: list[str] = Field(default_factory=list)


class LibraryItem(BaseModel):
    """Library item representing a conversion job with all its outputs."""
    id: str
    title: str
    author: str
    created_at: str
    output_files: dict = {}  # {"m4b": {...}, "searchable_pdf": {...}, "combined_pdf": {...}}
    # Hierarchical organization
    tags: LibraryItemTags = Field(default_factory=LibraryItemTags)
    # KB association
    kb_id: Optional[str] = None
    document_id: Optional[str] = None
    # Legacy fields for backward compatibility with M4B-only items
    filename: Optional[str] = None
    duration_minutes: Optional[float] = None
    file_size_mb: Optional[float] = None
    chapters: Optional[int] = None
    voice: Optional[str] = None
    path: Optional[str] = None


class LibraryItemTagsUpdate(BaseModel):
    """Request model for updating library item tags."""
    organization: Optional[str] = None
    course_code: Optional[str] = None
    course_id: Optional[str] = None
    module_id: Optional[str] = None
    topic_ids: Optional[list[str]] = None
    custom_tags: Optional[list[str]] = None
    sync_to_kb: bool = True  # Whether to sync changes to associated KB


class LibraryItemMetadataUpdate(BaseModel):
    """Request model for updating library item metadata."""
    title: Optional[str] = None
    author: Optional[str] = None


class LibraryFilterParams(BaseModel):
    """Filter parameters for library listing."""
    organization: Optional[str] = None
    course_code: Optional[str] = None
    course_id: Optional[str] = None
    module_id: Optional[str] = None
    topic_id: Optional[str] = None
    custom_tag: Optional[str] = None
    has_kb: Optional[bool] = None  # Filter by KB association


# Knowledge Base Models
class KBCreateRequest(BaseModel):
    name: str
    description: str = ""


class KBResponse(BaseModel):
    id: str
    name: str
    description: str
    created_at: str
    updated_at: str
    document_count: int = 0
    chunk_count: int = 0


class KBSearchRequest(BaseModel):
    query: str
    kb_ids: Optional[list[str]] = None
    top_k: int = 10


class KBSearchResult(BaseModel):
    text: str
    score: float
    document_id: str
    document_title: str
    document_author: str
    chapter_title: str = ""
    kb_id: str


class KBDocumentResponse(BaseModel):
    id: str
    kb_id: str
    title: str
    author: str
    source_file: str
    total_pages: int
    created_at: str
    ocr_required: bool
    chapter_count: int


class KBDocumentDetailResponse(KBDocumentResponse):
    chapters: list[dict]


class EntityResponse(BaseModel):
    """Response model for entity data."""
    id: str
    name: str
    type: str
    description: str = ""
    aliases: list[str] = []
    mention_count: int = 0


class RelatedDocumentResponse(BaseModel):
    """Response model for related document data."""
    id: str
    title: str
    author: str
    relationship_type: str
    score: float = 0.0


class FullTextSearchRequest(BaseModel):
    """Request model for full-text search."""
    query: str
    kb_ids: Optional[list[str]] = None
    top_k: int = 10


class FullTextSearchResult(BaseModel):
    """Response model for full-text search results."""
    text: str
    score: float
    document_id: str
    document_title: str
    chapter_title: str = ""
    kb_id: str


class GraphStatsResponse(BaseModel):
    """Response model for graph statistics."""
    documents: int = 0
    chapters: int = 0
    chunks: int = 0
    entities: int = 0
    relationships: int = 0


# Phase 5 Response Models
class SimilarDocumentResponse(BaseModel):
    """Response model for similar document."""
    document_id: str
    kb_id: str
    document_title: str = ""
    document_author: str = ""
    similarity_score: float
    matching_chunks: int = 0
    relationship_type: str = "similar_to"


class CitedDocumentResponse(BaseModel):
    """Response model for cited document."""
    document_id: str
    document_title: str = ""
    document_author: str = ""
    confidence_score: float
    relationship_type: str = "cites"


class InferredRelationshipResponse(BaseModel):
    """Response model for inferred relationship."""
    target_document_id: str
    target_document_title: str = ""
    relationship_type: str
    confidence: float
    reason: str = ""


class ExportSummaryResponse(BaseModel):
    """Response model for KB export."""
    kb_id: str
    output_path: str
    documents: int
    chunks: int
    entities: int
    relationships: int
    file_size_mb: float


class ImportSummaryResponse(BaseModel):
    """Response model for KB import."""
    kb_id: str
    documents_imported: int
    chunks_imported: int
    entities_imported: int
    relationships_imported: int


class AnalyticsResponse(BaseModel):
    """Response model for usage analytics."""
    period_days: int
    total_searches: int = 0
    unique_queries: int = 0
    top_queries: list = []
    kb_usage: list = []
    search_types: dict = {}
    avg_results: float = 0.0
    chapters: int = 0
    chunks: int = 0
    entities: int = 0
    relationships: int = 0


# =========================================================================
# Chat Models
# =========================================================================

class ChatMessage(BaseModel):
    """A single message in the conversation."""
    role: str  # "user" or "assistant"
    content: str


class ChatRequest(BaseModel):
    """Request model for chat endpoint."""
    query: str
    kb_id: str
    conversation_history: list[ChatMessage] = Field(default_factory=list)
    top_k: int = 5  # Number of context chunks to retrieve


class ChatSource(BaseModel):
    """Source reference for chat response."""
    ref: str
    document_id: str
    document_title: str
    chapter_title: Optional[str] = None
    page_number: Optional[int] = None
    text_preview: str
    score: float


class ChatResponse(BaseModel):
    """Response model for chat endpoint."""
    answer: str
    sources: list[ChatSource]
    kb_name: str


# =========================================================================
# Course Hierarchy Models
# =========================================================================

class CourseCreateRequest(BaseModel):
    """Request model for creating a course."""
    title: str
    code: str = ""
    description: str = ""
    instructor: str = ""


class CourseResponse(BaseModel):
    """Response model for course data."""
    id: str
    kb_id: str
    title: str
    code: str
    description: str
    instructor: str
    created_at: str


class ModuleCreateRequest(BaseModel):
    """Request model for creating a module."""
    number: int
    title: str
    description: str = ""


class TopicCreateRequest(BaseModel):
    """Request model for creating a topic."""
    title: str
    description: str = ""


class ConceptCreateRequest(BaseModel):
    """Request model for creating a concept."""
    name: str
    definition: str = ""
    chunk_ids: list[str] = Field(default_factory=list)


class LearningPathRequest(BaseModel):
    """Request model for learning path query."""
    from_concept: str
    to_concept: str


def get_kb_registry():
    """Get or create the KB registry."""
    global _kb_registry
    if _kb_registry is None:
        try:
            from knowledge_base import KBRegistry
            _kb_registry = KBRegistry(KB_DIR)
        except ImportError as e:
            logger.error(f"Knowledge base module not available: {e}")
            raise HTTPException(status_code=503, detail="Knowledge base feature not available")
    return _kb_registry


def get_kb_pipeline():
    """Get or create the KB ingestion pipeline."""
    global _kb_pipeline
    if _kb_pipeline is None:
        try:
            from knowledge_base import create_ingestion_pipeline
            # Pass the shared registry to ensure cache consistency
            registry = get_kb_registry()
            _kb_pipeline = create_ingestion_pipeline(KB_DIR, registry=registry)
        except ImportError as e:
            logger.error(f"Knowledge base module not available: {e}")
            raise HTTPException(status_code=503, detail="Knowledge base feature not available")
    return _kb_pipeline


# Graph store (lazy loaded)
_graph_store = None

# DEPRECATED: unified browser+chat agent removed in backend-first refactor.
# Browser traversal now handled via Playwright MCP.
_unified_agent = None  # kept to avoid NameError on any lingering references


def get_graph_store():
    """Get or create the Neo4j graph store."""
    global _graph_store
    if _graph_store is None:
        try:
            from knowledge_base.graph_store import GraphStore
            from config.settings import settings
            _graph_store = GraphStore(
                uri=settings.NEO4J_URI,
                user=settings.NEO4J_USER,
                password=settings.NEO4J_PASSWORD
            )
            _graph_store.ensure_schema()
            logger.info("Neo4j graph store initialized successfully")
        except Exception as e:
            logger.warning(f"Neo4j not available: {e}")
            _graph_store = None
    return _graph_store


def verify_neo4j() -> bool:
    """
    Verify Neo4j connection at startup.
    Returns True if Neo4j is available, False otherwise.
    """
    try:
        store = get_graph_store()
        if store:
            stats = store.get_stats("__test__")  # Quick connectivity test
            logger.info("Neo4j connection verified")
            return True
        return False
    except Exception as e:
        logger.warning(f"Neo4j verification failed: {e}")
        return False


# DEPRECATED: get_unified_agent() removed — browser+chat handled by noetix-ui + Codex.
# The /api/chat endpoint now uses _basic_rag_chat directly (RAG-only, no browser).


# Phase 5 helper functions to reduce code duplication
_vector_store = None
_embedder = None


def get_vector_store_and_embedder():
    """
    Get or create VectorStore and Embedder instances.
    Cached for efficiency across multiple endpoint calls.
    """
    global _vector_store, _embedder
    from pathlib import Path
    from config.settings import settings

    if _vector_store is None:
        from knowledge_base import VectorStore
        lancedb_path = Path(settings.KB_DIR) / "lancedb"
        _vector_store = VectorStore(lancedb_path)

    if _embedder is None:
        from knowledge_base import Embedder
        _embedder = Embedder()

    return _vector_store, _embedder


def get_document_text(kb_id: str, doc_id: str) -> Optional[str]:
    """
    Extract all text from a document's chunks.

    Args:
        kb_id: Knowledge base ID
        doc_id: Document ID

    Returns:
        Concatenated document text, or None if not found
    """
    vector_store, _ = get_vector_store_and_embedder()

    table_name = f"kb_{kb_id}"
    if table_name not in vector_store.db.table_names():
        return None

    table = vector_store.db.open_table(table_name)
    safe_doc_id = doc_id.replace("'", "''")
    results = table.search().where(f"document_id = '{safe_doc_id}'").limit(1000).to_list()

    if not results:
        return None

    # Sort by position and join text
    sorted_results = sorted(results, key=lambda x: x.get("position", 0))
    return " ".join(r["text"] for r in sorted_results)


# OpenAI Chat client (lazy loaded)
_openai_chat_client = None


def get_openai_chat_client():
    """Get or create OpenAI client for chat completions."""
    global _openai_chat_client
    if _openai_chat_client is None:
        from openai import OpenAI
        from config.settings import settings
        _openai_chat_client = OpenAI(api_key=settings.get_openai_key())
    return _openai_chat_client


def get_library_metadata_path():
    return LIBRARY_DIR / "metadata.json"


def load_library() -> list[dict]:
    """Load library metadata from disk."""
    meta_path = get_library_metadata_path()
    if meta_path.exists():
        with open(meta_path) as f:
            return json.load(f)
    return []


def save_library(items: list[dict]):
    """Save library metadata to disk."""
    with open(get_library_metadata_path(), 'w') as f:
        json.dump(items, f, indent=2)


def add_to_library(item: dict):
    """Add an item to the library."""
    items = load_library()
    # Check for duplicates by filename
    items = [i for i in items if i.get('filename') != item.get('filename')]
    items.insert(0, item)  # Add new item at the beginning
    save_library(items)


def update_library_item(job_id: str, title: str, author: str, output_type: str, output_info: dict):
    """
    Update or create a library item with an output file.

    Args:
        job_id: The job ID
        title: Document title
        author: Document author
        output_type: Type of output (m4b, searchable_pdf, combined_pdf)
        output_info: Dict with output details (path, filename, file_size_mb, etc.)
    """
    items = load_library()

    # Find existing item for this job
    existing_idx = None
    for idx, item in enumerate(items):
        if item.get('id') == job_id:
            existing_idx = idx
            break

    if existing_idx is not None:
        # Update existing item
        item = items[existing_idx]
        if 'output_files' not in item:
            item['output_files'] = {}
        item['output_files'][output_type] = output_info
        # Update to front of list
        items.pop(existing_idx)
        items.insert(0, item)
    else:
        # Create new item
        item = {
            'id': job_id,
            'title': title,
            'author': author,
            'created_at': datetime.now().isoformat(),
            'output_files': {
                output_type: output_info
            }
        }
        items.insert(0, item)

    save_library(items)


def combine_pdfs(pdf_paths: list[Path], output_path: Path) -> Path:
    """Combine multiple PDFs into a single PDF using PyMuPDF."""
    import fitz  # PyMuPDF

    # Create new PDF
    combined = fitz.open()

    for pdf_path in pdf_paths:
        doc = fitz.open(str(pdf_path))
        combined.insert_pdf(doc)
        doc.close()

    combined.save(str(output_path))
    combined.close()

    logger.info(f"Combined {len(pdf_paths)} PDFs into {output_path.name}")
    return output_path


def run_conversion(job_id: str, pdf_paths: list[Path], voice: str, title: str = None, author: str = None, outputs: list[str] = None, kb_id: str = None):
    """Run the conversion pipeline in a background thread."""
    # Convert to list if single path passed (backwards compat)
    if isinstance(pdf_paths, Path):
        pdf_paths = [pdf_paths]

    # Default to M4B only for backward compatibility
    if outputs is None:
        outputs = ["m4b"]

    jobs[job_id]["requested_outputs"] = outputs
    jobs[job_id]["output_files"] = {}
    jobs[job_id]["kb_id"] = kb_id

    try:
        # Step 0: Check if OCR is needed and pre-process
        ocr_cache_path = None
        ocr_cache_paths = {}  # Map from pdf_path to ocr_cache_path
        needs_ocr = False
        pdfs_needing_ocr = []
        jobs[job_id]["status"] = "checking"
        jobs[job_id]["current_task"] = "Checking if PDFs need OCR..."
        jobs[job_id]["progress"] = 1

        try:
            from pipeline.ocr_engine import OCREngine, preprocess_pdf_with_ocr

            engine = OCREngine(gpu=True)

            # Check ALL PDFs for OCR needs (fixes BUG-002)
            for i, pdf_path in enumerate(pdf_paths):
                jobs[job_id]["current_task"] = f"Checking PDF {i+1}/{len(pdf_paths)} for OCR..."
                if engine.needs_ocr(pdf_path):
                    pdfs_needing_ocr.append(pdf_path)
                    logger.info(f"PDF needs OCR: {pdf_path.name}")

            if pdfs_needing_ocr:
                needs_ocr = True
                jobs[job_id]["status"] = "ocr"

                # Pre-process each PDF that needs OCR
                for i, pdf_path in enumerate(pdfs_needing_ocr):
                    jobs[job_id]["current_task"] = f"Running OCR on PDF {i+1}/{len(pdfs_needing_ocr)} (this may take a while)..."
                    jobs[job_id]["progress"] = 2 + int((i / len(pdfs_needing_ocr)) * 15)

                    def ocr_progress(page, total, msg):
                        base_pct = 2 + int((i / len(pdfs_needing_ocr)) * 15)
                        page_pct = int((page / total) * (15 / len(pdfs_needing_ocr)))
                        jobs[job_id]["progress"] = base_pct + page_pct
                        jobs[job_id]["current_task"] = f"OCR ({i+1}/{len(pdfs_needing_ocr)}): {msg}"

                    cache_path = preprocess_pdf_with_ocr(
                        pdf_path,
                        progress_callback=ocr_progress
                    )
                    ocr_cache_paths[str(pdf_path)] = cache_path
                    logger.info(f"OCR pre-processing complete for {pdf_path.name}: {cache_path}")

                # Use the first OCR cache for backwards compatibility
                ocr_cache_path = ocr_cache_paths.get(str(pdf_paths[0]))

        except Exception as e:
            logger.warning(f"OCR check/pre-processing failed: {e}")
            # Continue without OCR - may fail later for scanned PDFs

        # Step 1: Analyze PDF(s) with Claude AI
        jobs[job_id]["status"] = "analyzing"
        if len(pdf_paths) > 1:
            jobs[job_id]["current_task"] = f"Analyzing {len(pdf_paths)} PDFs with AI..."
        else:
            jobs[job_id]["current_task"] = "Analyzing PDF with AI..."
        jobs[job_id]["progress"] = 18

        analysis = None
        combined_pdf_path = None
        analysis_path = None

        try:
            from pipeline.pdf_analyzer import PDFAnalyzer
            analyzer = PDFAnalyzer(ocr_cache_path=ocr_cache_path)

            if len(pdf_paths) > 1:
                # Multiple PDFs - analyze to determine order then combine
                jobs[job_id]["current_task"] = "AI determining PDF order..."
                analysis, ordered_paths = analyzer.analyze_multiple(pdf_paths)
                pdf_paths = ordered_paths  # Reorder based on AI analysis

                # Combine PDFs in the determined order
                jobs[job_id]["current_task"] = "Combining PDFs..."
                combined_pdf_path = combine_pdfs(pdf_paths, UPLOAD_DIR / f"{job_id}_combined.pdf")
                main_pdf_path = combined_pdf_path
            else:
                # Single PDF
                analysis = analyzer.analyze(pdf_paths[0])
                main_pdf_path = pdf_paths[0]

            # Use AI-detected values if not provided
            if not title:
                title = analysis.get("title", main_pdf_path.stem)
            if not author:
                author = analysis.get("author", "Unknown")

            # Update job with detected info
            jobs[job_id]["title"] = title
            jobs[job_id]["author"] = author
            jobs[job_id]["total_chapters"] = len(analysis.get("chapters", []))

            # Save analysis to file for pipeline to use
            analysis_path = main_pdf_path.with_suffix('.analysis.json')
            with open(analysis_path, 'w') as f:
                json.dump(analysis, f, indent=2)

            logger.info(f"AI Analysis: '{title}' by {author}, {len(analysis.get('chapters', []))} chapters")

        except Exception as e:
            logger.warning(f"AI analysis failed, using defaults: {e}")
            # Continue without AI analysis - pipeline will use fallback
            main_pdf_path = pdf_paths[0]

        # Create output directory
        output_dir = LIBRARY_DIR / job_id
        output_dir.mkdir(parents=True, exist_ok=True)

        # Calculate progress allocation based on outputs
        # Base: 20% for preprocessing (OCR + analysis)
        # Remaining 80% split among outputs
        output_progress = {}
        remaining_progress = 80
        if "combined_pdf" in outputs:
            output_progress["combined_pdf"] = 5
            remaining_progress -= 5
        if "searchable_pdf" in outputs:
            output_progress["searchable_pdf"] = 10
            remaining_progress -= 10
        if "m4b" in outputs:
            output_progress["m4b"] = remaining_progress

        current_progress = 20
        total_chapters = 0

        # === Output: Combined PDF ===
        if "combined_pdf" in outputs and len(pdf_paths) > 1:
            jobs[job_id]["status"] = "combining"
            jobs[job_id]["current_task"] = "Creating combined PDF..."
            jobs[job_id]["progress"] = current_progress

            safe_title = (title or main_pdf_path.stem).replace(' ', '_').replace(':', '-').replace('/', '-')[:50]
            combined_output_path = output_dir / f"{safe_title}_combined.pdf"
            combine_pdfs(pdf_paths, combined_output_path)
            jobs[job_id]["output_files"]["combined_pdf"] = str(combined_output_path)
            logger.info(f"Created combined PDF: {combined_output_path}")

            # Persist to library
            file_stat = combined_output_path.stat()
            update_library_item(job_id, title or main_pdf_path.stem, author or "Unknown", "combined_pdf", {
                "path": str(combined_output_path),
                "filename": combined_output_path.name,
                "file_size_mb": round(file_stat.st_size / (1024 * 1024), 1)
            })

            current_progress += output_progress.get("combined_pdf", 5)
        elif "combined_pdf" in outputs and len(pdf_paths) == 1:
            # Single PDF - just copy it
            safe_title = (title or main_pdf_path.stem).replace(' ', '_').replace(':', '-').replace('/', '-')[:50]
            combined_output_path = output_dir / f"{safe_title}_combined.pdf"
            shutil.copy(main_pdf_path, combined_output_path)
            jobs[job_id]["output_files"]["combined_pdf"] = str(combined_output_path)

            # Persist to library
            file_stat = combined_output_path.stat()
            update_library_item(job_id, title or main_pdf_path.stem, author or "Unknown", "combined_pdf", {
                "path": str(combined_output_path),
                "filename": combined_output_path.name,
                "file_size_mb": round(file_stat.st_size / (1024 * 1024), 1)
            })

            current_progress += output_progress.get("combined_pdf", 5)

        # === Output: Searchable PDF ===
        # Fixes BUG-001: Now processes the combined PDF when multiple files are uploaded
        if "searchable_pdf" in outputs:
            jobs[job_id]["status"] = "creating_searchable"
            safe_title = (title or main_pdf_path.stem).replace(' ', '_').replace(':', '-').replace('/', '-')[:50]
            searchable_output_path = output_dir / f"{safe_title}_searchable.pdf"

            if needs_ocr:
                # Run OCR on the main/combined PDF to add text layer
                jobs[job_id]["current_task"] = "Creating searchable PDF with OCR text layer..."
                jobs[job_id]["progress"] = current_progress

                try:
                    from pipeline.ocr_engine import OCREngine
                    ocr_engine = OCREngine(gpu=True)

                    # Use the combined PDF if multiple files were uploaded
                    source_pdf = main_pdf_path
                    if len(pdf_paths) > 1:
                        logger.info(f"Running OCR on combined PDF for searchable output")

                    ocr_engine.ocr_pdf_to_text_pdf(
                        source_pdf,
                        searchable_output_path
                    )
                    jobs[job_id]["output_files"]["searchable_pdf"] = str(searchable_output_path)
                    logger.info(f"Created searchable PDF: {searchable_output_path}")

                    # Persist to library
                    file_stat = searchable_output_path.stat()
                    update_library_item(job_id, title or main_pdf_path.stem, author or "Unknown", "searchable_pdf", {
                        "path": str(searchable_output_path),
                        "filename": searchable_output_path.name,
                        "file_size_mb": round(file_stat.st_size / (1024 * 1024), 1)
                    })

                except Exception as e:
                    logger.warning(f"Failed to create searchable PDF: {e}")
                    # Non-fatal - continue with other outputs

            else:
                # PDF(s) already have text - just copy the main/combined PDF
                jobs[job_id]["current_task"] = "PDF already has searchable text, copying..."
                shutil.copy(main_pdf_path, searchable_output_path)
                jobs[job_id]["output_files"]["searchable_pdf"] = str(searchable_output_path)

                # Persist to library
                file_stat = searchable_output_path.stat()
                update_library_item(job_id, title or main_pdf_path.stem, author or "Unknown", "searchable_pdf", {
                    "path": str(searchable_output_path),
                    "filename": searchable_output_path.name,
                    "file_size_mb": round(file_stat.st_size / (1024 * 1024), 1)
                })

            current_progress += output_progress.get("searchable_pdf", 10)

        # === Output: M4B Audiobook ===
        if "m4b" in outputs:
            jobs[job_id]["status"] = "extracting"
            jobs[job_id]["current_task"] = "Extracting chapters from PDF..."
            jobs[job_id]["progress"] = current_progress

            # Build command
            cmd = [
                "python", "pipeline.py",
                str(main_pdf_path),
                "--output-dir", str(output_dir),
                "--voice", voice
            ]
            if title:
                cmd.extend(["--title", title])
            if author:
                cmd.extend(["--author", author])
            if ocr_cache_path:
                cmd.extend(["--ocr-cache", str(ocr_cache_path)])

            # Run subprocess and capture output for progress parsing
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            m4b_base_progress = current_progress
            m4b_progress_range = output_progress.get("m4b", 65)

            for line in iter(process.stdout.readline, ''):
                line = line.strip()

                # Parse progress from output
                if "Found" in line and "chapters" in line:
                    try:
                        parts = line.split()
                        for i, p in enumerate(parts):
                            if p == "Found":
                                total_chapters = int(parts[i + 1])
                                jobs[job_id]["total_chapters"] = total_chapters
                    except Exception as e:
                        logger.debug(f"Failed to parse chapter count from line '{line}': {e}")

                elif "Generating audio for Chapter" in line:
                    jobs[job_id]["status"] = "generating"
                    try:
                        parts = line.split("Chapter")
                        if len(parts) > 1:
                            ch_part = parts[1].split(":")[0].strip()
                            current_chapter = int(ch_part)
                            jobs[job_id]["current_chapter"] = current_chapter
                            # Scale progress within M4B's allocation
                            chapter_pct = (current_chapter / max(total_chapters, 1)) * 0.85
                            jobs[job_id]["progress"] = m4b_base_progress + (chapter_pct * m4b_progress_range)
                            jobs[job_id]["current_task"] = f"Generating Chapter {current_chapter}..."
                    except Exception as e:
                        logger.debug(f"Failed to parse chapter number from line '{line}': {e}")

                elif "Building M4B" in line:
                    jobs[job_id]["status"] = "building"
                    jobs[job_id]["progress"] = m4b_base_progress + (0.90 * m4b_progress_range)
                    jobs[job_id]["current_task"] = "Building M4B file..."

                elif "Converting" in line and "to AAC" in line:
                    jobs[job_id]["current_task"] = "Converting audio to AAC..."

                elif "Concatenating" in line:
                    jobs[job_id]["current_task"] = "Concatenating chapters..."
                    jobs[job_id]["progress"] = m4b_base_progress + (0.95 * m4b_progress_range)

                elif "Output file:" in line:
                    try:
                        output_path = line.split("Output file:")[-1].strip()
                        jobs[job_id]["output_file"] = output_path
                        jobs[job_id]["output_files"]["m4b"] = output_path
                    except Exception as e:
                        logger.debug(f"Failed to parse output file path from line '{line}': {e}")

                elif "File size:" in line:
                    try:
                        size_str = line.split("File size:")[-1].strip()
                        size_mb = float(size_str.replace("MB", "").strip())
                        jobs[job_id]["file_size_mb"] = size_mb
                    except Exception as e:
                        logger.debug(f"Failed to parse file size from line '{line}': {e}")

                elif "Audio duration:" in line:
                    try:
                        dur_str = line.split("Audio duration:")[-1].strip()
                        dur_min = float(dur_str.replace("minutes", "").strip())
                        jobs[job_id]["duration_minutes"] = dur_min
                    except Exception as e:
                        logger.debug(f"Failed to parse audio duration from line '{line}': {e}")

            process.wait()

            if process.returncode == 0:
                # Find the M4B file
                m4b_files = list(output_dir.glob("*.m4b"))
                if m4b_files:
                    m4b_path = m4b_files[0]
                    jobs[job_id]["output_file"] = str(m4b_path)
                    jobs[job_id]["output_files"]["m4b"] = str(m4b_path)

                    # Persist to library
                    file_stat = m4b_path.stat()
                    update_library_item(job_id, title or main_pdf_path.stem, author or "Unknown", "m4b", {
                        "path": str(m4b_path),
                        "filename": m4b_path.name,
                        "file_size_mb": round(file_stat.st_size / (1024 * 1024), 1),
                        "duration_minutes": jobs[job_id].get("duration_minutes", 0),
                        "chapters": total_chapters,
                        "voice": voice
                    })
            else:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = "M4B conversion failed"
                raise Exception("M4B conversion failed")

        # === Output: Knowledge Base ===
        if "knowledge_base" in outputs:
            # Validate requirements
            if not kb_id:
                logger.error("Knowledge base output requested but no kb_id provided")
                jobs[job_id]["output_files"]["knowledge_base"] = "error: No knowledge base selected"
            else:
                jobs[job_id]["status"] = "ingesting"
                jobs[job_id]["current_task"] = "Ingesting into knowledge base..."
                jobs[job_id]["progress"] = 92

                try:
                    # Import KB pipeline
                    from knowledge_base import create_ingestion_pipeline

                    pipeline = create_ingestion_pipeline(KB_DIR)

                    # Prepare chapters for ingestion
                    # AI analyzer returns 1-indexed page numbers; convert to 0-indexed
                    chapters_data = []
                    if analysis:
                        chapters_data = [
                            {**ch, 'start_page': max(0, ch.get('start_page', 1) - 1)}
                            for ch in analysis.get("chapters", [])
                        ]

                    # Extract text using PDFExtractor (also captures page count)
                    from pipeline.pdf_extractor import PDFExtractor
                    extractor = PDFExtractor(main_pdf_path, ocr_cache_path=ocr_cache_path)
                    actual_page_count = extractor.get_page_count()

                    if not chapters_data:
                        # Fallback: extract text directly as single chapter
                        logger.info("No chapter analysis available, extracting full text as single chapter")
                        full_text = ""
                        for page_num in range(actual_page_count):
                            full_text += extractor.extract_page_text(page_num) + "\n"

                        chapters_data = [{
                            "number": 1,
                            "title": title or main_pdf_path.stem,
                            "text": full_text,
                            "start_page": 0
                        }]
                    else:
                        # Extract text for each chapter
                        for i, ch in enumerate(chapters_data):
                            start_page = ch.get("start_page", 0)
                            # Determine end page (next chapter's start - 1, or last page)
                            if i + 1 < len(chapters_data):
                                end_page = chapters_data[i + 1].get("start_page", actual_page_count) - 1
                            else:
                                end_page = actual_page_count - 1

                            # Extract text for pages
                            chapter_text = ""
                            for page_num in range(start_page, min(end_page + 1, actual_page_count)):
                                chapter_text += extractor.extract_page_text(page_num) + "\n"
                            ch["text"] = chapter_text.strip()

                    extractor.close()

                    def kb_progress(current, total, msg):
                        pct = 92 + int((current / max(total, 1)) * 6)  # 92-98%
                        jobs[job_id]["progress"] = pct
                        jobs[job_id]["current_task"] = f"KB: {msg}"

                    # Ingest document
                    doc = pipeline.ingest_document(
                        kb_id=kb_id,
                        title=title or main_pdf_path.stem,
                        author=author or "Unknown",
                        chapters=chapters_data,
                        source_file=main_pdf_path.name,
                        total_pages=actual_page_count,
                        ocr_required=needs_ocr,
                        progress_callback=kb_progress
                    )

                    jobs[job_id]["output_files"]["knowledge_base"] = {"kb_id": kb_id, "document_id": doc.id}
                    logger.info(f"Ingested document into KB {kb_id}, document {doc.id}")

                except Exception as e:
                    logger.error(f"Knowledge base ingestion failed: {e}")
                    # Non-fatal for KB - other outputs may have succeeded
                    jobs[job_id]["output_files"]["knowledge_base"] = f"error: {str(e)}"

        # All outputs complete
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["current_task"] = "Complete!"
        logger.info(f"Job {job_id} completed with outputs: {list(jobs[job_id]['output_files'].keys())}")

    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)

    finally:
        # Cleanup uploaded PDFs, analysis file, and OCR cache
        for pdf_path in pdf_paths:
            if pdf_path.exists():
                pdf_path.unlink()
        if combined_pdf_path and combined_pdf_path.exists():
            combined_pdf_path.unlink()
        if analysis_path and analysis_path.exists():
            analysis_path.unlink()
        if ocr_cache_path and Path(ocr_cache_path).exists():
            Path(ocr_cache_path).unlink()
            logger.info(f"Cleaned up OCR cache: {ocr_cache_path}")


def run_conversion_from_library(job_id: str, source_path: Path, source_type: str, voice: str, title: str, author: str, outputs: list[str], kb_id: str = None):
    """
    Run conversion from a library source (PDF or M4B).

    For PDF source: Extract text, run TTS for M4B, ingest to KB.
    For M4B source: Run STT for transcript, generate PDF, ingest to KB.
    """
    jobs[job_id]["requested_outputs"] = outputs
    jobs[job_id]["output_files"] = {}
    jobs[job_id]["kb_id"] = kb_id
    ocr_cache_path = None
    needs_ocr = False

    try:
        # Get output directory (use existing library directory)
        output_dir = LIBRARY_DIR / job_id
        output_dir.mkdir(parents=True, exist_ok=True)

        if source_type == "library_pdf":
            # Step 0: Check if OCR is needed and pre-process
            jobs[job_id]["status"] = "checking"
            jobs[job_id]["current_task"] = "Checking if PDF needs OCR..."
            jobs[job_id]["progress"] = 2

            try:
                from pipeline.ocr_engine import OCREngine, preprocess_pdf_with_ocr
                engine = OCREngine(gpu=True)

                if engine.needs_ocr(source_path):
                    needs_ocr = True
                    jobs[job_id]["status"] = "ocr"
                    jobs[job_id]["current_task"] = "Running OCR (this may take a while)..."

                    def ocr_progress(page, total, msg):
                        jobs[job_id]["progress"] = 2 + int((page / total) * 15)
                        jobs[job_id]["current_task"] = f"OCR: {msg}"

                    ocr_cache_path = preprocess_pdf_with_ocr(
                        source_path, progress_callback=ocr_progress
                    )
                    logger.info(f"OCR pre-processing complete for {source_path.name}: {ocr_cache_path}")
            except Exception as e:
                logger.warning(f"OCR check/pre-processing failed: {e}")

            # PDF source - can create M4B, searchable_pdf, and KB
            jobs[job_id]["status"] = "extracting"
            jobs[job_id]["current_task"] = "Extracting text from PDF..."
            jobs[job_id]["progress"] = 18

            # Analyze PDF with Claude AI for chapter detection
            analysis = None
            try:
                from pipeline.pdf_analyzer import PDFAnalyzer
                analyzer = PDFAnalyzer(ocr_cache_path=ocr_cache_path)
                analysis = analyzer.analyze(source_path)
                jobs[job_id]["total_chapters"] = len(analysis.get("chapters", []))
                logger.info(f"PDF analysis: {len(analysis.get('chapters', []))} chapters detected")
            except Exception as e:
                logger.warning(f"PDF analysis failed: {e}")

            # === Output: M4B Audiobook ===
            if "m4b" in outputs:
                jobs[job_id]["status"] = "generating"
                jobs[job_id]["current_task"] = "Generating audiobook..."
                jobs[job_id]["progress"] = 10

                # Build command for TTS pipeline
                cmd = [
                    "python", "pipeline.py",
                    str(source_path),
                    "--output-dir", str(output_dir),
                    "--voice", voice
                ]
                if title:
                    cmd.extend(["--title", title])
                if author:
                    cmd.extend(["--author", author])
                if ocr_cache_path:
                    cmd.extend(["--ocr-cache", str(ocr_cache_path)])

                # Run subprocess
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )

                total_chapters = 0
                for line in iter(process.stdout.readline, ''):
                    line = line.strip()

                    if "Found" in line and "chapters" in line:
                        try:
                            parts = line.split()
                            for i, p in enumerate(parts):
                                if p == "Found":
                                    total_chapters = int(parts[i + 1])
                                    jobs[job_id]["total_chapters"] = total_chapters
                        except:
                            pass

                    elif "Generating audio for Chapter" in line:
                        try:
                            parts = line.split("Chapter")
                            if len(parts) > 1:
                                ch_part = parts[1].split(":")[0].strip()
                                current_chapter = int(ch_part)
                                jobs[job_id]["current_chapter"] = current_chapter
                                chapter_pct = (current_chapter / max(total_chapters, 1)) * 0.7
                                jobs[job_id]["progress"] = 10 + int(chapter_pct * 70)
                                jobs[job_id]["current_task"] = f"Generating Chapter {current_chapter}..."
                        except:
                            pass

                    elif "Building M4B" in line:
                        jobs[job_id]["status"] = "building"
                        jobs[job_id]["progress"] = 80
                        jobs[job_id]["current_task"] = "Building M4B file..."

                    elif "File size:" in line:
                        try:
                            size_str = line.split("File size:")[-1].strip()
                            size_mb = float(size_str.replace("MB", "").strip())
                            jobs[job_id]["file_size_mb"] = size_mb
                        except:
                            pass

                    elif "Audio duration:" in line:
                        try:
                            dur_str = line.split("Audio duration:")[-1].strip()
                            dur_min = float(dur_str.replace("minutes", "").strip())
                            jobs[job_id]["duration_minutes"] = dur_min
                        except:
                            pass

                process.wait()

                if process.returncode == 0:
                    m4b_files = list(output_dir.glob("*.m4b"))
                    if m4b_files:
                        m4b_path = m4b_files[0]
                        jobs[job_id]["output_files"]["m4b"] = str(m4b_path)

                        # Update library
                        file_stat = m4b_path.stat()
                        update_library_item(job_id, title, author, "m4b", {
                            "path": str(m4b_path),
                            "filename": m4b_path.name,
                            "file_size_mb": round(file_stat.st_size / (1024 * 1024), 1),
                            "duration_minutes": jobs[job_id].get("duration_minutes", 0),
                            "chapters": total_chapters,
                            "voice": voice
                        })
                else:
                    jobs[job_id]["output_files"]["m4b"] = "error: M4B conversion failed"
                    logger.error("M4B conversion from library PDF failed")

            # === Output: Searchable PDF ===
            if "searchable_pdf" in outputs:
                jobs[job_id]["status"] = "creating_searchable"
                safe_title = (title or source_path.stem).replace(' ', '_').replace(':', '-').replace('/', '-')[:50]
                searchable_output_path = output_dir / f"{safe_title}_searchable.pdf"

                if needs_ocr:
                    jobs[job_id]["current_task"] = "Creating searchable PDF with OCR text layer..."
                    try:
                        from pipeline.ocr_engine import OCREngine
                        ocr_engine = OCREngine(gpu=True)
                        ocr_engine.ocr_pdf_to_text_pdf(source_path, searchable_output_path)
                        jobs[job_id]["output_files"]["searchable_pdf"] = str(searchable_output_path)
                        logger.info(f"Created searchable PDF: {searchable_output_path}")

                        file_stat = searchable_output_path.stat()
                        update_library_item(job_id, title, author, "searchable_pdf", {
                            "path": str(searchable_output_path),
                            "filename": searchable_output_path.name,
                            "file_size_mb": round(file_stat.st_size / (1024 * 1024), 1)
                        })
                    except Exception as e:
                        logger.warning(f"Failed to create searchable PDF: {e}")
                else:
                    jobs[job_id]["current_task"] = "PDF already has searchable text, copying..."
                    import shutil
                    shutil.copy(source_path, searchable_output_path)
                    jobs[job_id]["output_files"]["searchable_pdf"] = str(searchable_output_path)

                    file_stat = searchable_output_path.stat()
                    update_library_item(job_id, title, author, "searchable_pdf", {
                        "path": str(searchable_output_path),
                        "filename": searchable_output_path.name,
                        "file_size_mb": round(file_stat.st_size / (1024 * 1024), 1)
                    })

            # === Output: Knowledge Base ===
            if "knowledge_base" in outputs and kb_id:
                jobs[job_id]["status"] = "ingesting"
                jobs[job_id]["current_task"] = "Ingesting into knowledge base..."
                jobs[job_id]["progress"] = 90

                try:
                    from knowledge_base import create_ingestion_pipeline
                    from pipeline.pdf_extractor import PDFExtractor

                    pipeline = create_ingestion_pipeline(KB_DIR)
                    extractor = PDFExtractor(str(source_path), ocr_cache_path=ocr_cache_path)
                    page_count = extractor.get_page_count()

                    # Prepare chapters
                    # AI analyzer returns 1-indexed page numbers; convert to 0-indexed
                    chapters_data = []
                    if analysis and analysis.get("chapters"):
                        for i, ch in enumerate(analysis["chapters"]):
                            start_page = max(0, ch.get("start_page", 1) - 1)
                            if i + 1 < len(analysis["chapters"]):
                                end_page = analysis["chapters"][i + 1].get("start_page", page_count) - 1
                            else:
                                end_page = page_count - 1

                            chapter_text = ""
                            for page_num in range(start_page, min(end_page + 1, page_count)):
                                chapter_text += extractor.extract_page_text(page_num) + "\n"

                            chapters_data.append({
                                "number": i + 1,
                                "title": ch.get("title", f"Chapter {i + 1}"),
                                "text": chapter_text.strip(),
                                "start_page": start_page
                            })
                    else:
                        # Single chapter fallback
                        full_text = ""
                        for page_num in range(page_count):
                            full_text += extractor.extract_page_text(page_num) + "\n"

                        chapters_data = [{
                            "number": 1,
                            "title": title,
                            "text": full_text.strip(),
                            "start_page": 0
                        }]

                    extractor.close()

                    doc = pipeline.ingest_document(
                        kb_id=kb_id,
                        title=title,
                        author=author,
                        chapters=chapters_data,
                        source_file=source_path.name,
                        total_pages=page_count,
                        ocr_required=needs_ocr
                    )

                    jobs[job_id]["output_files"]["knowledge_base"] = {"kb_id": kb_id, "document_id": doc.id}
                    update_library_item(job_id, title, author, "knowledge_base", {"kb_id": kb_id, "document_id": doc.id})
                    logger.info(f"Ingested from library PDF into KB {kb_id}, document {doc.id}")

                except Exception as e:
                    logger.error(f"KB ingestion failed: {e}")
                    jobs[job_id]["output_files"]["knowledge_base"] = f"error: {str(e)}"

        elif source_type == "library_m4b":
            # M4B source - needs STT to create PDF and KB
            jobs[job_id]["status"] = "transcribing"
            jobs[job_id]["current_task"] = "Transcribing audio (this may take a while)..."
            jobs[job_id]["progress"] = 5

            try:
                from pipeline.stt_engine import STTEngine
                from pipeline.audio_metadata import extract_audio_metadata

                # Extract metadata from M4B
                metadata = extract_audio_metadata(source_path)
                chapters = metadata.get("chapters", [])

                # Transcribe
                stt = STTEngine()
                transcription = stt.transcribe(source_path)

                jobs[job_id]["progress"] = 60
                jobs[job_id]["current_task"] = "Processing transcript..."

                # If no chapters in metadata, detect from transcription
                if not chapters:
                    chapters = stt.detect_chapters(transcription["segments"])

                # Build chapter text from transcription segments
                chapters_data = []
                for i, ch in enumerate(chapters):
                    # Find segments within this chapter's time range
                    ch_segments = [
                        s for s in transcription["segments"]
                        if s["start"] >= ch.get("start", 0) and s["end"] <= ch.get("end", float('inf'))
                    ]
                    chapter_text = " ".join(s["text"] for s in ch_segments)

                    chapters_data.append({
                        "number": i + 1,
                        "title": ch.get("title", f"Chapter {i + 1}"),
                        "text": chapter_text.strip(),
                        "start_time": ch.get("start", 0),
                        "end_time": ch.get("end", 0)
                    })

                jobs[job_id]["total_chapters"] = len(chapters_data)

                # === Output: Searchable PDF ===
                if "searchable_pdf" in outputs:
                    jobs[job_id]["status"] = "creating_pdf"
                    jobs[job_id]["current_task"] = "Creating PDF from transcript..."
                    jobs[job_id]["progress"] = 70

                    from pipeline.transcript_to_pdf import create_transcript_pdf

                    safe_title = title.replace(' ', '_').replace(':', '-').replace('/', '-')[:50]
                    pdf_path = output_dir / f"{safe_title}_transcript.pdf"

                    create_transcript_pdf(
                        chapters=chapters_data,
                        title=title,
                        author=author,
                        output_path=pdf_path
                    )

                    jobs[job_id]["output_files"]["searchable_pdf"] = str(pdf_path)
                    file_stat = pdf_path.stat()
                    update_library_item(job_id, title, author, "searchable_pdf", {
                        "path": str(pdf_path),
                        "filename": pdf_path.name,
                        "file_size_mb": round(file_stat.st_size / (1024 * 1024), 1)
                    })
                    logger.info(f"Created transcript PDF: {pdf_path}")

                # === Output: Knowledge Base ===
                if "knowledge_base" in outputs and kb_id:
                    jobs[job_id]["status"] = "ingesting"
                    jobs[job_id]["current_task"] = "Ingesting into knowledge base..."
                    jobs[job_id]["progress"] = 85

                    try:
                        from knowledge_base import create_ingestion_pipeline

                        pipeline = create_ingestion_pipeline(KB_DIR)

                        doc = pipeline.ingest_document(
                            kb_id=kb_id,
                            title=title,
                            author=author,
                            chapters=chapters_data,
                            source_file=source_path.name,
                            total_pages=len(chapters_data)  # Use chapter count as proxy
                        )

                        jobs[job_id]["output_files"]["knowledge_base"] = {"kb_id": kb_id, "document_id": doc.id}
                        update_library_item(job_id, title, author, "knowledge_base", {"kb_id": kb_id, "document_id": doc.id})
                        logger.info(f"Ingested from M4B transcript into KB {kb_id}, document {doc.id}")

                    except Exception as e:
                        logger.error(f"KB ingestion failed: {e}")
                        jobs[job_id]["output_files"]["knowledge_base"] = f"error: {str(e)}"

            except ImportError as e:
                error_msg = f"STT not available: {e}. Install with: pip install faster-whisper"
                logger.error(error_msg)
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = error_msg
                return
            except Exception as e:
                logger.error(f"STT processing failed: {e}")
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = str(e)
                return

        # Complete
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["current_task"] = "Complete!"
        logger.info(f"Library conversion job {job_id} completed with outputs: {list(jobs[job_id]['output_files'].keys())}")

    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        logger.error(f"Library conversion failed: {e}")
    finally:
        if ocr_cache_path:
            try:
                if Path(ocr_cache_path).exists():
                    Path(ocr_cache_path).unlink()
                    logger.info(f"Cleaned up OCR cache: {ocr_cache_path}")
            except Exception:
                pass


def run_audio_conversion(job_id: str, audio_path: Path, title: str, author: str, outputs: list[str], kb_id: str = None, is_m4b_source: bool = False):
    """
    Run conversion from an audio file upload (MP3, AAC, M4B).

    Transcribes audio using STT, detects chapters, generates PDF and KB,
    and optionally converts to M4B (if source is not already M4B).
    """
    jobs[job_id]["requested_outputs"] = outputs
    jobs[job_id]["output_files"] = {}
    jobs[job_id]["kb_id"] = kb_id

    try:
        # Create output directory
        output_dir = LIBRARY_DIR / job_id
        output_dir.mkdir(parents=True, exist_ok=True)

        # Extract metadata from audio file
        jobs[job_id]["status"] = "analyzing"
        jobs[job_id]["current_task"] = "Extracting audio metadata..."
        jobs[job_id]["progress"] = 2

        try:
            from pipeline.audio_metadata import extract_audio_metadata, mine_metadata_from_text

            metadata = extract_audio_metadata(audio_path)

            # Use file metadata if not provided by user
            if not title:
                title = metadata.get("title") or audio_path.stem
            if not author:
                author = metadata.get("author") or "Unknown"

            jobs[job_id]["title"] = title
            jobs[job_id]["author"] = author

            # Get chapters from metadata (M4B usually has these)
            metadata_chapters = metadata.get("chapters", [])
            logger.info(f"Audio metadata: title='{title}', author='{author}', {len(metadata_chapters)} chapters from metadata")

        except Exception as e:
            logger.warning(f"Failed to extract audio metadata: {e}")
            if not title:
                title = audio_path.stem
            if not author:
                author = "Unknown"
            metadata_chapters = []

        # Transcribe audio
        jobs[job_id]["status"] = "transcribing"
        jobs[job_id]["current_task"] = "Transcribing audio (this may take a while)..."
        jobs[job_id]["progress"] = 5

        try:
            from pipeline.stt_engine import STTEngine

            stt = STTEngine()
            transcription = stt.transcribe(audio_path)

            jobs[job_id]["progress"] = 50
            jobs[job_id]["current_task"] = "Processing transcript..."

            # Use metadata chapters if available, otherwise detect from transcription
            if metadata_chapters:
                chapters = metadata_chapters
            else:
                chapters = stt.detect_chapters(transcription["segments"])

            # Build chapter text from transcription segments
            chapters_data = []
            for i, ch in enumerate(chapters):
                # Find segments within this chapter's time range
                ch_start = ch.get("start", 0)
                ch_end = ch.get("end", float('inf'))

                ch_segments = [
                    s for s in transcription["segments"]
                    if s["start"] >= ch_start and s["end"] <= ch_end
                ]
                chapter_text = " ".join(s["text"] for s in ch_segments)

                chapters_data.append({
                    "number": i + 1,
                    "title": ch.get("title", f"Chapter {i + 1}"),
                    "text": chapter_text.strip(),
                    "start_time": ch_start,
                    "end_time": ch_end
                })

            jobs[job_id]["total_chapters"] = len(chapters_data)
            logger.info(f"Transcription complete: {len(chapters_data)} chapters")

            # Try to mine metadata from transcript if still missing
            if title == audio_path.stem or author == "Unknown":
                mined = mine_metadata_from_text(transcription["text"], transcription["segments"])
                if mined.get("title") and title == audio_path.stem:
                    title = mined["title"]
                    jobs[job_id]["title"] = title
                if mined.get("author") and author == "Unknown":
                    author = mined["author"]
                    jobs[job_id]["author"] = author

        except ImportError as e:
            error_msg = f"STT not available: {e}. Install with: pip install faster-whisper"
            logger.error(error_msg)
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = error_msg
            return
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = f"Transcription failed: {str(e)}"
            return

        # === Output: M4B Audiobook (only if source is not already M4B) ===
        if "m4b" in outputs and not is_m4b_source:
            jobs[job_id]["status"] = "converting"
            jobs[job_id]["current_task"] = "Converting to M4B with chapters..."
            jobs[job_id]["progress"] = 55

            try:
                from pipeline.m4b_builder import M4BBuilder

                safe_title = title.replace(' ', '_').replace(':', '-').replace('/', '-')[:50]
                m4b_path = output_dir / f"{safe_title}.m4b"

                # Convert audio to M4B with chapter markers
                builder = M4BBuilder(output_dir)

                # Prepare chapter list for M4B
                m4b_chapters = []
                for ch in chapters_data:
                    m4b_chapters.append({
                        "title": ch["title"],
                        "start": ch.get("start_time", 0),
                        "end": ch.get("end_time", 0)
                    })

                builder.convert_audio_to_m4b(
                    audio_path,
                    m4b_path,
                    title=title,
                    author=author,
                    chapters=m4b_chapters
                )

                jobs[job_id]["output_files"]["m4b"] = str(m4b_path)

                # Update library
                file_stat = m4b_path.stat()
                update_library_item(job_id, title, author, "m4b", {
                    "path": str(m4b_path),
                    "filename": m4b_path.name,
                    "file_size_mb": round(file_stat.st_size / (1024 * 1024), 1),
                    "duration_minutes": transcription.get("duration", 0) / 60,
                    "chapters": len(chapters_data),
                    "voice": "original"  # Mark as original audio, not TTS
                })
                logger.info(f"Created M4B: {m4b_path}")

            except Exception as e:
                logger.error(f"M4B conversion failed: {e}")
                jobs[job_id]["output_files"]["m4b"] = f"error: {str(e)}"

        # If source is M4B, copy it to output and register in library
        elif is_m4b_source:
            safe_title = title.replace(' ', '_').replace(':', '-').replace('/', '-')[:50]
            m4b_dest = output_dir / f"{safe_title}.m4b"
            shutil.copy(audio_path, m4b_dest)

            file_stat = m4b_dest.stat()
            update_library_item(job_id, title, author, "m4b", {
                "path": str(m4b_dest),
                "filename": m4b_dest.name,
                "file_size_mb": round(file_stat.st_size / (1024 * 1024), 1),
                "duration_minutes": transcription.get("duration", 0) / 60,
                "chapters": len(chapters_data),
                "voice": "original"
            })
            jobs[job_id]["output_files"]["m4b"] = str(m4b_dest)

        # === Output: Searchable PDF ===
        if "searchable_pdf" in outputs:
            jobs[job_id]["status"] = "creating_pdf"
            jobs[job_id]["current_task"] = "Creating PDF from transcript..."
            jobs[job_id]["progress"] = 70

            try:
                from pipeline.transcript_to_pdf import create_transcript_pdf

                safe_title = title.replace(' ', '_').replace(':', '-').replace('/', '-')[:50]
                pdf_path = output_dir / f"{safe_title}_transcript.pdf"

                create_transcript_pdf(
                    chapters=chapters_data,
                    title=title,
                    author=author,
                    output_path=pdf_path
                )

                jobs[job_id]["output_files"]["searchable_pdf"] = str(pdf_path)

                # Update library
                file_stat = pdf_path.stat()
                update_library_item(job_id, title, author, "searchable_pdf", {
                    "path": str(pdf_path),
                    "filename": pdf_path.name,
                    "file_size_mb": round(file_stat.st_size / (1024 * 1024), 1)
                })
                logger.info(f"Created transcript PDF: {pdf_path}")

            except Exception as e:
                logger.error(f"PDF creation failed: {e}")
                jobs[job_id]["output_files"]["searchable_pdf"] = f"error: {str(e)}"

        # === Output: Knowledge Base ===
        if "knowledge_base" in outputs and kb_id:
            jobs[job_id]["status"] = "ingesting"
            jobs[job_id]["current_task"] = "Ingesting into knowledge base..."
            jobs[job_id]["progress"] = 85

            try:
                from knowledge_base import create_ingestion_pipeline

                pipeline = create_ingestion_pipeline(KB_DIR)

                doc = pipeline.ingest_document(
                    kb_id=kb_id,
                    title=title,
                    author=author,
                    chapters=chapters_data,
                    source_file=audio_path.name,
                    total_pages=len(chapters_data)  # Use chapter count as proxy
                )

                jobs[job_id]["output_files"]["knowledge_base"] = {"kb_id": kb_id, "document_id": doc.id}
                update_library_item(job_id, title, author, "knowledge_base", {"kb_id": kb_id, "document_id": doc.id})
                logger.info(f"Ingested audio transcript into KB {kb_id}, document {doc.id}")

            except Exception as e:
                logger.error(f"KB ingestion failed: {e}")
                jobs[job_id]["output_files"]["knowledge_base"] = f"error: {str(e)}"

        # Complete
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["current_task"] = "Complete!"
        logger.info(f"Audio conversion job {job_id} completed with outputs: {list(jobs[job_id]['output_files'].keys())}")

    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        logger.error(f"Audio conversion failed: {e}")

    finally:
        # Cleanup uploaded audio file
        if audio_path.exists():
            audio_path.unlink()
            logger.info(f"Cleaned up uploaded audio: {audio_path}")


def run_video_conversion(job_id: str, video_path: Path, title: str, author: str, outputs: list[str], kb_id: str = None):
    """
    Run conversion from a video file upload (MP4, MKV, WebM, AVI).

    Extracts audio for STT, extracts frames for GPT-5.1 Vision analysis,
    merges content, and generates M4B (with visuals narrated), PDF, and KB.
    """
    jobs[job_id]["requested_outputs"] = outputs
    jobs[job_id]["output_files"] = {}
    jobs[job_id]["kb_id"] = kb_id

    temp_dir = None

    try:
        # Create output directory
        output_dir = LIBRARY_DIR / job_id
        output_dir.mkdir(parents=True, exist_ok=True)

        # Initialize video processor
        jobs[job_id]["status"] = "initializing"
        jobs[job_id]["current_task"] = "Initializing video processor..."
        jobs[job_id]["progress"] = 2

        try:
            from pipeline.video_processor import VideoProcessor
            from pipeline.stt_engine import STTEngine

            stt = STTEngine()
            openai_client = get_openai_chat_client()

            temp_dir = output_dir / "temp"
            processor = VideoProcessor(
                openai_client=openai_client,
                stt_engine=stt,
                temp_dir=temp_dir
            )

        except ImportError as e:
            error_msg = f"Video processing dependencies missing: {e}"
            logger.error(error_msg)
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = error_msg
            return

        # Define progress callback
        def progress_callback(stage, current, total, detail):
            stage_weights = {
                "audio_extraction": (0, 5),
                "transcription": (5, 40),
                "frame_extraction": (40, 50),
                "vision_analysis": (50, 75),
                "merging": (75, 80),
            }

            base, span = stage_weights.get(stage, (0, 100))
            progress = base + int((current / max(total, 1)) * (span - base))

            jobs[job_id]["status"] = stage
            jobs[job_id]["current_task"] = detail
            jobs[job_id]["progress"] = progress

        # Process video
        jobs[job_id]["status"] = "processing"
        jobs[job_id]["current_task"] = "Processing video..."
        jobs[job_id]["progress"] = 5

        try:
            result = processor.process_video(video_path, progress_callback=progress_callback)

            chapters_data = result["chapters"]
            merged_text = result["merged_text"]
            transcription = result["transcription"]
            frame_analyses = result["frame_analyses"]

            # Update title/author if not provided
            if not title:
                title = video_path.stem
            if not author:
                author = "Unknown"

            jobs[job_id]["title"] = title
            jobs[job_id]["author"] = author
            jobs[job_id]["total_chapters"] = len(chapters_data)

            logger.info(f"Video processed: {len(chapters_data)} chapters, {len(frame_analyses)} frames analyzed")

        except Exception as e:
            logger.error(f"Video processing failed: {e}")
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = f"Video processing failed: {str(e)}"
            return

        # === Output: M4B Audiobook (TTS with visual narration) ===
        if "m4b" in outputs:
            jobs[job_id]["status"] = "creating_m4b"
            jobs[job_id]["current_task"] = "Creating M4B audiobook with TTS..."
            jobs[job_id]["progress"] = 80

            try:
                # Prepare chapters for TTS with visual descriptions integrated
                tts_chapters = []
                for ch in chapters_data:
                    # Build text with visual descriptions interspersed
                    chapter_text = ch.get("text", "")

                    # Add visual descriptions at end of chapter
                    visuals = ch.get("visuals", [])
                    if visuals:
                        chapter_text += "\n\nVisual content in this section:\n"
                        for v in visuals:
                            timestamp = v.get("timestamp", 0)
                            mins = int(timestamp // 60)
                            secs = int(timestamp % 60)
                            desc = v.get("description", "")
                            chapter_text += f"\nAt {mins}:{secs:02d}: {desc}\n"

                    tts_chapters.append({
                        "number": len(tts_chapters) + 1,
                        "title": ch.get("title", f"Chapter {len(tts_chapters) + 1}"),
                        "text": chapter_text.strip()
                    })

                # Use existing TTS pipeline
                from pipeline.orchestrator import AudiobookPipeline
                from pipeline.m4b_builder import M4BBuilder, AudiobookMetadata, AACConverter

                safe_title = title.replace(' ', '_').replace(':', '-').replace('/', '-')[:50]
                m4b_path = output_dir / f"{safe_title}.m4b"

                # Initialize pipeline with default voice
                pipeline = AudiobookPipeline(
                    output_dir=output_dir,
                    voice="af_heart"  # Default voice for video narration
                )

                # Initialize parallel AAC converter
                aac_converter = AACConverter(temp_dir=output_dir / "aac_temp")

                # Generate chapter audio
                chapter_audio_files = []
                for i, ch in enumerate(tts_chapters):
                    jobs[job_id]["current_task"] = f"Generating audio for chapter {i + 1}/{len(tts_chapters)}..."
                    jobs[job_id]["current_chapter"] = i + 1

                    if not ch["text"].strip():
                        continue

                    wav_path = pipeline.process_chapter(ch["number"], ch["title"], ch["text"])
                    if wav_path and wav_path.exists():
                        chapter_audio_files.append(wav_path)
                        # Submit for parallel AAC conversion
                        aac_converter.submit_conversion(wav_path)

                # Wait for all conversions and build M4B
                aac_converter.wait_all()

                if chapter_audio_files:
                    builder = M4BBuilder(output_dir)
                    metadata = AudiobookMetadata(
                        title=title,
                        author=author,
                        narrator="AI Narrator"
                    )
                    builder.build_m4b(chapter_audio_files, metadata, aac_converter=aac_converter)

                    # Find the created M4B file
                    m4b_files = list(output_dir.glob("*.m4b"))
                    if m4b_files:
                        m4b_path = m4b_files[0]
                        jobs[job_id]["output_files"]["m4b"] = str(m4b_path)

                        file_stat = m4b_path.stat()
                        update_library_item(job_id, title, author, "m4b", {
                            "path": str(m4b_path),
                            "filename": m4b_path.name,
                            "file_size_mb": round(file_stat.st_size / (1024 * 1024), 1),
                            "duration_minutes": result.get("duration", 0) / 60,
                            "chapters": len(tts_chapters),
                            "voice": "af_heart",
                            "source": "video"
                        })
                        logger.info(f"Created M4B from video: {m4b_path}")

                aac_converter.shutdown()

            except Exception as e:
                logger.error(f"M4B creation failed: {e}")
                jobs[job_id]["output_files"]["m4b"] = f"error: {str(e)}"

        # === Output: Searchable PDF with Screenshots ===
        if "searchable_pdf" in outputs:
            jobs[job_id]["status"] = "creating_pdf"
            jobs[job_id]["current_task"] = "Creating PDF with screenshots..."
            jobs[job_id]["progress"] = 90

            try:
                from pipeline.video_to_pdf import create_video_pdf

                safe_title = title.replace(' ', '_').replace(':', '-').replace('/', '-')[:50]
                pdf_path = output_dir / f"{safe_title}_video_transcript.pdf"

                create_video_pdf(
                    chapters=chapters_data,
                    title=title,
                    author=author,
                    output_path=pdf_path,
                    include_screenshots=True
                )

                jobs[job_id]["output_files"]["searchable_pdf"] = str(pdf_path)

                file_stat = pdf_path.stat()
                update_library_item(job_id, title, author, "searchable_pdf", {
                    "path": str(pdf_path),
                    "filename": pdf_path.name,
                    "file_size_mb": round(file_stat.st_size / (1024 * 1024), 1),
                    "source": "video"
                })
                logger.info(f"Created video PDF: {pdf_path}")

            except Exception as e:
                logger.error(f"PDF creation failed: {e}")
                jobs[job_id]["output_files"]["searchable_pdf"] = f"error: {str(e)}"

        # === Output: Knowledge Base ===
        if "knowledge_base" in outputs and kb_id:
            jobs[job_id]["status"] = "ingesting"
            jobs[job_id]["current_task"] = "Ingesting into knowledge base..."
            jobs[job_id]["progress"] = 95

            try:
                from knowledge_base import create_ingestion_pipeline

                pipeline = create_ingestion_pipeline(KB_DIR)

                # Prepare chapters with visual content for KB
                kb_chapters = []
                for ch in chapters_data:
                    # Combine transcript and visual descriptions
                    combined_text = ch.get("text", "")

                    visuals = ch.get("visuals", [])
                    if visuals:
                        combined_text += "\n\n[Visual Content]\n"
                        for v in visuals:
                            combined_text += f"- {v.get('description', '')}\n"

                    kb_chapters.append({
                        "number": len(kb_chapters) + 1,
                        "title": ch.get("title", f"Chapter {len(kb_chapters) + 1}"),
                        "text": combined_text.strip()
                    })

                doc = pipeline.ingest_document(
                    kb_id=kb_id,
                    title=title,
                    author=author,
                    chapters=kb_chapters,
                    source_file=video_path.name,
                    total_pages=len(kb_chapters)
                )

                jobs[job_id]["output_files"]["knowledge_base"] = {"kb_id": kb_id, "document_id": doc.id}
                update_library_item(job_id, title, author, "knowledge_base", {"kb_id": kb_id, "document_id": doc.id})
                logger.info(f"Ingested video content into KB {kb_id}, document {doc.id}")

            except Exception as e:
                logger.error(f"KB ingestion failed: {e}")
                jobs[job_id]["output_files"]["knowledge_base"] = f"error: {str(e)}"

        # Complete
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["current_task"] = "Complete!"
        logger.info(f"Video conversion job {job_id} completed with outputs: {list(jobs[job_id]['output_files'].keys())}")

    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        logger.error(f"Video conversion failed: {e}")

    finally:
        # Cleanup
        if video_path.exists():
            video_path.unlink()
            logger.info(f"Cleaned up uploaded video: {video_path}")

        if temp_dir and temp_dir.exists():
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
            logger.info(f"Cleaned up temp dir: {temp_dir}")


def run_archive_conversion(job_id: str, archive_path: Path, title: str, author: str, outputs: list[str], kb_id: str = None):
    """
    Run conversion from a ZIP archive containing video files.

    Extracts archive, processes all videos with STT + Vision analysis,
    combines content, and generates M4B (with visuals narrated), PDF, and KB.
    """
    jobs[job_id]["requested_outputs"] = outputs
    jobs[job_id]["output_files"] = {}
    jobs[job_id]["kb_id"] = kb_id

    archive_processor = None
    contents = None

    try:
        # Create output directory
        output_dir = LIBRARY_DIR / job_id
        output_dir.mkdir(parents=True, exist_ok=True)

        # Phase 1: Extract archive (5%)
        jobs[job_id]["status"] = "extracting"
        jobs[job_id]["current_task"] = "Extracting archive..."
        jobs[job_id]["progress"] = 2

        try:
            from pipeline.archive_processor import ArchiveProcessor

            archive_processor = ArchiveProcessor(temp_base_dir=UPLOAD_DIR / "temp")

            def extract_progress(current, total):
                jobs[job_id]["progress"] = int((current / max(total, 1)) * 5)
                jobs[job_id]["current_task"] = f"Extracting file {current}/{total}..."

            contents = archive_processor.extract(archive_path, progress_callback=extract_progress)

            if not contents.video_files:
                raise ValueError("No video files found in archive")

            jobs[job_id]["video_count"] = len(contents.video_files)
            logger.info(f"Extracted {len(contents.video_files)} videos from archive")

        except Exception as e:
            logger.error(f"Archive extraction failed: {e}")
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = f"Archive extraction failed: {str(e)}"
            return

        # Phase 2: Process videos (5-80%)
        jobs[job_id]["status"] = "processing_videos"
        jobs[job_id]["current_task"] = "Initializing video processor..."
        jobs[job_id]["progress"] = 6

        try:
            from pipeline.batch_video_processor import BatchVideoProcessor
            from pipeline.stt_engine import STTEngine

            stt = STTEngine()
            openai_client = get_openai_chat_client()

            batch_processor = BatchVideoProcessor(
                openai_client=openai_client,
                stt_engine=stt,
                temp_dir=contents.extract_dir / "processing"
            )

            def video_progress(video_idx, video_total, stage, current, total, detail):
                base_progress = 6 + int((video_idx / max(video_total, 1)) * 74)
                stage_progress = int((current / max(total, 1)) * (74 / max(video_total, 1)))
                jobs[job_id]["progress"] = min(80, base_progress + stage_progress)
                jobs[job_id]["current_video"] = video_idx + 1
                jobs[job_id]["current_task"] = f"Video {video_idx + 1}/{video_total}: {detail}"

            result = batch_processor.process_archive(contents, progress_callback=video_progress)

            # Update metadata from processing result
            if not title:
                title = result.course_title
            if not author:
                author = result.course_author

            jobs[job_id]["title"] = title
            jobs[job_id]["author"] = author
            jobs[job_id]["total_chapters"] = len(result.combined_chapters)

            logger.info(f"Processed {result.success_count}/{result.video_count} videos, {len(result.combined_chapters)} chapters")

        except ImportError as e:
            error_msg = f"Video processing dependencies missing: {e}"
            logger.error(error_msg)
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = error_msg
            return

        except Exception as e:
            logger.error(f"Video processing failed: {e}")
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = f"Video processing failed: {str(e)}"
            return

        # Phase 3: Generate outputs (80-100%)

        # === Output: M4B Audiobook (TTS with visual narration) ===
        if "m4b" in outputs:
            jobs[job_id]["status"] = "creating_m4b"
            jobs[job_id]["current_task"] = "Creating M4B audiobook with TTS..."
            jobs[job_id]["progress"] = 82

            try:
                # Prepare chapters for TTS with visual descriptions
                tts_chapters = []
                for ch in result.combined_chapters:
                    chapter_text = ch.get_text_for_m4b()
                    tts_chapters.append({
                        "number": ch.number,
                        "title": ch.title,
                        "text": chapter_text.strip()
                    })

                # Use existing TTS pipeline
                from pipeline.orchestrator import AudiobookPipeline
                from pipeline.m4b_builder import M4BBuilder, AudiobookMetadata, AACConverter

                safe_title = title.replace(' ', '_').replace(':', '-').replace('/', '-')[:50]

                # Initialize pipeline with default voice
                pipeline = AudiobookPipeline(
                    output_dir=output_dir,
                    voice="af_heart"
                )

                # Initialize parallel AAC converter
                aac_converter = AACConverter(temp_dir=output_dir / "aac_temp")

                # Generate chapter audio
                chapter_audio_files = []
                for i, ch in enumerate(tts_chapters):
                    jobs[job_id]["current_task"] = f"Generating audio for chapter {i + 1}/{len(tts_chapters)}..."
                    jobs[job_id]["current_chapter"] = i + 1

                    if not ch["text"].strip():
                        continue

                    wav_path = pipeline.process_chapter(ch["number"], ch["title"], ch["text"])
                    if wav_path and wav_path.exists():
                        chapter_audio_files.append(wav_path)
                        aac_converter.submit_conversion(wav_path)

                # Wait for all conversions and build M4B
                aac_converter.wait_all()

                if chapter_audio_files:
                    builder = M4BBuilder(output_dir)
                    metadata = AudiobookMetadata(
                        title=title,
                        author=author,
                        narrator="AI Narrator"
                    )
                    builder.build_m4b(chapter_audio_files, metadata, aac_converter=aac_converter)

                    # Find the created M4B file
                    m4b_files = list(output_dir.glob("*.m4b"))
                    if m4b_files:
                        m4b_path = m4b_files[0]
                        jobs[job_id]["output_files"]["m4b"] = str(m4b_path)

                        file_stat = m4b_path.stat()
                        update_library_item(job_id, title, author, "m4b", {
                            "path": str(m4b_path),
                            "filename": m4b_path.name,
                            "file_size_mb": round(file_stat.st_size / (1024 * 1024), 1),
                            "duration_minutes": result.total_duration / 60,
                            "chapters": len(tts_chapters),
                            "voice": "af_heart",
                            "source": "archive",
                            "video_count": result.video_count
                        })
                        logger.info(f"Created M4B from archive: {m4b_path}")

                aac_converter.shutdown()

            except Exception as e:
                logger.error(f"M4B creation failed: {e}")
                jobs[job_id]["output_files"]["m4b"] = f"error: {str(e)}"

        # === Output: Searchable PDF with Screenshots ===
        if "searchable_pdf" in outputs:
            jobs[job_id]["status"] = "creating_pdf"
            jobs[job_id]["current_task"] = "Creating PDF with screenshots..."
            jobs[job_id]["progress"] = 90

            try:
                from pipeline.video_to_pdf import create_video_pdf

                safe_title = title.replace(' ', '_').replace(':', '-').replace('/', '-')[:50]
                pdf_path = output_dir / f"{safe_title}_course_transcript.pdf"

                # Convert EnhancedChapters to format expected by create_video_pdf
                pdf_chapters = []
                for ch in result.combined_chapters:
                    text, visuals = ch.get_content_for_pdf()
                    pdf_chapters.append({
                        "title": f"{ch.title} (from {ch.video_title})",
                        "start": ch.local_start,
                        "end": ch.local_end,
                        "text": text,
                        "visuals": [
                            {
                                "timestamp": v.timestamp,
                                "description": v.description,
                                "frame_path": str(v.frame_path)
                            }
                            for v in visuals
                        ]
                    })

                create_video_pdf(
                    chapters=pdf_chapters,
                    title=title,
                    author=author,
                    output_path=pdf_path,
                    include_screenshots=True,
                    skip_descriptions_with_images=True  # Images speak for themselves
                )

                jobs[job_id]["output_files"]["searchable_pdf"] = str(pdf_path)

                file_stat = pdf_path.stat()
                update_library_item(job_id, title, author, "searchable_pdf", {
                    "path": str(pdf_path),
                    "filename": pdf_path.name,
                    "file_size_mb": round(file_stat.st_size / (1024 * 1024), 1),
                    "source": "archive",
                    "video_count": result.video_count
                })
                logger.info(f"Created course PDF: {pdf_path}")

            except Exception as e:
                logger.error(f"PDF creation failed: {e}")
                jobs[job_id]["output_files"]["searchable_pdf"] = f"error: {str(e)}"

        # === Output: Knowledge Base ===
        if "knowledge_base" in outputs and kb_id:
            jobs[job_id]["status"] = "ingesting"
            jobs[job_id]["current_task"] = "Adding to knowledge base..."
            jobs[job_id]["progress"] = 96

            try:
                from knowledge_base.ingest import create_ingestion_pipeline

                pipeline = create_ingestion_pipeline()

                # Convert chapters to KB format
                kb_chapters = []
                for ch in result.combined_chapters:
                    kb_data = ch.get_content_for_kb()
                    kb_chapters.append({
                        "number": kb_data["number"],
                        "title": kb_data["title"],
                        "text": kb_data["text"],
                        "start_time": kb_data["start_time"],
                        "end_time": kb_data["end_time"],
                        "video_source": kb_data["video_source"],
                        "visuals": kb_data["visuals"]
                    })

                doc = pipeline.ingest_document(
                    kb_id=kb_id,
                    title=title,
                    author=author,
                    chapters=kb_chapters,
                    source_file=archive_path.name,
                    total_pages=len(kb_chapters)
                )

                jobs[job_id]["output_files"]["knowledge_base"] = {"kb_id": kb_id, "document_id": doc.id}
                update_library_item(job_id, title, author, "knowledge_base", {
                    "kb_id": kb_id,
                    "document_id": doc.id,
                    "source": "archive",
                    "video_count": result.video_count
                })
                logger.info(f"Ingested archive content into KB {kb_id}, document {doc.id}")

            except Exception as e:
                logger.error(f"KB ingestion failed: {e}")
                jobs[job_id]["output_files"]["knowledge_base"] = f"error: {str(e)}"

        # Complete
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["current_task"] = "Complete!"
        logger.info(f"Archive conversion job {job_id} completed with outputs: {list(jobs[job_id]['output_files'].keys())}")

    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        logger.error(f"Archive conversion failed: {e}")

    finally:
        # Cleanup
        if archive_path.exists():
            archive_path.unlink()
            logger.info(f"Cleaned up uploaded archive: {archive_path}")

        if archive_processor and contents:
            archive_processor.cleanup(contents.extract_dir)
            logger.info(f"Cleaned up extraction directory")


@app.post("/api/upload")
async def upload_pdf(
    files: list[UploadFile] = File(default=None),  # Optional for library sources
    voice: str = Form("af_heart"),
    title: str = Form(None),
    author: str = Form(None),
    outputs: str = Form("m4b"),  # Comma-separated: "m4b,searchable_pdf,combined_pdf,knowledge_base"
    kb_id: str = Form(None),  # Target knowledge base ID
    kb_name: str = Form(None),  # Create new KB with this name
    kb_description: str = Form(""),  # Description for new KB
    source_type: str = Form("upload"),  # "upload", "library_pdf", "library_m4b"
    library_id: str = Form(None)  # Library item ID for library sources
):
    """Upload one or more PDFs, or use a library item as source, and start conversion."""
    # Parse outputs parameter
    output_list = [o.strip() for o in outputs.split(",") if o.strip()]
    valid_outputs = {"m4b", "searchable_pdf", "combined_pdf", "knowledge_base"}
    output_list = [o for o in output_list if o in valid_outputs]
    if not output_list:
        output_list = ["m4b"]  # Default to M4B

    # Handle knowledge base creation if needed
    target_kb_id = kb_id
    if "knowledge_base" in output_list:
        if kb_name and not kb_id:
            # Create new KB
            try:
                registry = get_kb_registry()
                new_kb = registry.create(name=kb_name, description=kb_description)
                target_kb_id = new_kb.id
                logger.info(f"Created new knowledge base: {new_kb.id} ({kb_name})")
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to create knowledge base: {e}")
        elif not kb_id:
            raise HTTPException(status_code=400, detail="Knowledge base output requires kb_id or kb_name")

    # Handle library sources
    if source_type in ("library_pdf", "library_m4b"):
        if not library_id:
            raise HTTPException(status_code=400, detail="Library source requires library_id")

        # Find library item
        library_items = load_library()
        source_item = None
        for item in library_items:
            if item["id"] == library_id:
                source_item = item
                break

        if not source_item:
            raise HTTPException(status_code=404, detail=f"Library item not found: {library_id}")

        # Use library metadata for title/author if not provided
        if not title:
            title = source_item.get("title")
        if not author:
            author = source_item.get("author")

        # Get source file path
        output_files = source_item.get("output_files", {})
        if source_type == "library_pdf":
            if "searchable_pdf" not in output_files:
                raise HTTPException(status_code=400, detail="Library item has no searchable PDF")
            source_path = Path(output_files["searchable_pdf"].get("path", ""))
            if not source_path.exists():
                raise HTTPException(status_code=404, detail="Source PDF file not found")

            # For PDF source, disable PDF outputs since we already have it
            output_list = [o for o in output_list if o not in ("searchable_pdf", "combined_pdf")]

        elif source_type == "library_m4b":
            # M4B source needs STT - will be handled in run_conversion
            if "m4b" in output_files:
                source_path = Path(output_files["m4b"].get("path", ""))
            elif "path" in source_item:  # Legacy format
                source_path = Path(source_item["path"])
            else:
                raise HTTPException(status_code=400, detail="Library item has no M4B audio")

            if not source_path.exists():
                raise HTTPException(status_code=404, detail="Source M4B file not found")

            # For M4B source, disable M4B output since we already have it
            output_list = [o for o in output_list if o != "m4b"]

        if not output_list:
            raise HTTPException(status_code=400, detail="No valid outputs selected for this source type")

        # Generate job ID - use library_id to update the same library entry
        job_id = library_id

        # Initialize job
        jobs[job_id] = {
            "job_id": job_id,
            "status": "pending",
            "progress": 0,
            "current_task": "Queued for processing...",
            "title": title,
            "author": author,
            "total_chapters": None,
            "current_chapter": None,
            "output_file": None,
            "error": None,
            "file_count": 1,
            "requested_outputs": output_list,
            "output_files": {},
            "kb_id": target_kb_id,
            "source_type": source_type,
            "source_path": str(source_path),
        }

        # Start conversion in background thread
        thread = threading.Thread(
            target=run_conversion_from_library,
            args=(job_id, source_path, source_type, voice, title, author, output_list, target_kb_id)
        )
        thread.start()

        return {"job_id": job_id, "message": "Conversion started from library", "outputs": output_list, "kb_id": target_kb_id}

    # Handle audio file upload
    if source_type == "audio_upload":
        if not files or len(files) == 0:
            raise HTTPException(status_code=400, detail="No audio file provided")

        audio_file = files[0]
        audio_extensions = {'.m4b', '.mp3', '.aac', '.m4a'}
        file_ext = Path(audio_file.filename).suffix.lower()

        if file_ext not in audio_extensions:
            raise HTTPException(status_code=400, detail=f"Unsupported audio format: {file_ext}")

        # Check if source is already M4B
        is_m4b_source = file_ext == '.m4b'
        if is_m4b_source:
            # Remove M4B from outputs if present (already have it)
            output_list = [o for o in output_list if o != "m4b"]

        if not output_list:
            raise HTTPException(status_code=400, detail="No valid outputs selected for audio source")

        # Generate job ID and save audio file
        job_id = str(uuid.uuid4())[:8]
        audio_path = UPLOAD_DIR / f"{job_id}_{audio_file.filename}"

        async with aiofiles.open(audio_path, 'wb') as out_file:
            content = await audio_file.read()
            await out_file.write(content)

        # Initialize job
        jobs[job_id] = {
            "job_id": job_id,
            "status": "pending",
            "progress": 0,
            "current_task": "Queued for processing...",
            "title": title or Path(audio_file.filename).stem,
            "author": author,
            "total_chapters": None,
            "current_chapter": None,
            "output_file": None,
            "error": None,
            "file_count": 1,
            "requested_outputs": output_list,
            "output_files": {},
            "kb_id": target_kb_id,
            "source_type": "audio_upload",
        }

        # Start conversion in background thread
        thread = threading.Thread(
            target=run_audio_conversion,
            args=(job_id, audio_path, title, author, output_list, target_kb_id, is_m4b_source)
        )
        thread.start()

        return {"job_id": job_id, "message": "Audio conversion started", "outputs": output_list, "kb_id": target_kb_id}

    # Handle video file upload
    if source_type == "video_upload":
        if not files or len(files) == 0:
            raise HTTPException(status_code=400, detail="No video file provided")

        video_file = files[0]
        video_extensions = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.m4v'}
        file_ext = Path(video_file.filename).suffix.lower()

        if file_ext not in video_extensions:
            raise HTTPException(status_code=400, detail=f"Unsupported video format: {file_ext}. Supported: {', '.join(video_extensions)}")

        if not output_list:
            raise HTTPException(status_code=400, detail="No outputs selected for video source")

        # Generate job ID and save video file
        job_id = str(uuid.uuid4())[:8]
        video_path = UPLOAD_DIR / f"{job_id}_{video_file.filename}"

        async with aiofiles.open(video_path, 'wb') as out_file:
            content = await video_file.read()
            await out_file.write(content)

        # Initialize job
        jobs[job_id] = {
            "job_id": job_id,
            "status": "pending",
            "progress": 0,
            "current_task": "Queued for video processing...",
            "title": title or Path(video_file.filename).stem,
            "author": author,
            "total_chapters": None,
            "current_chapter": None,
            "output_file": None,
            "error": None,
            "file_count": 1,
            "requested_outputs": output_list,
            "output_files": {},
            "kb_id": target_kb_id,
            "source_type": "video_upload",
        }

        # Start conversion in background thread
        thread = threading.Thread(
            target=run_video_conversion,
            args=(job_id, video_path, title, author, output_list, target_kb_id)
        )
        thread.start()

        return {"job_id": job_id, "message": "Video conversion started", "outputs": output_list, "kb_id": target_kb_id}

    # Handle archive (ZIP) file upload
    if source_type == "archive_upload":
        if not files or len(files) == 0:
            raise HTTPException(status_code=400, detail="No archive file provided")

        archive_file = files[0]
        archive_extensions = {'.zip'}
        file_ext = Path(archive_file.filename).suffix.lower()

        if file_ext not in archive_extensions:
            raise HTTPException(status_code=400, detail=f"Unsupported archive format: {file_ext}. Supported: {', '.join(archive_extensions)}")

        if not output_list:
            raise HTTPException(status_code=400, detail="No outputs selected for archive source")

        # Generate job ID and save archive file
        job_id = str(uuid.uuid4())[:8]
        archive_path = UPLOAD_DIR / f"{job_id}_{archive_file.filename}"

        async with aiofiles.open(archive_path, 'wb') as out_file:
            content = await archive_file.read()
            await out_file.write(content)

        # Initialize job
        jobs[job_id] = {
            "job_id": job_id,
            "status": "pending",
            "progress": 0,
            "current_task": "Queued for archive processing...",
            "title": title or Path(archive_file.filename).stem,
            "author": author,
            "total_chapters": None,
            "current_chapter": None,
            "error": None,
            "file_count": 1,
            "requested_outputs": output_list,
            "output_files": {},
            "kb_id": target_kb_id,
            "source_type": "archive_upload",
            "video_count": None,
            "current_video": None,
        }

        # Start conversion in background thread
        thread = threading.Thread(
            target=run_archive_conversion,
            args=(job_id, archive_path, title, author, output_list, target_kb_id)
        )
        thread.start()

        return {"job_id": job_id, "message": "Archive processing started", "outputs": output_list, "kb_id": target_kb_id}

    # Standard PDF file upload handling
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    # Validate all files are PDFs
    for file in files:
        if not file.filename.lower().endswith('.pdf'):
            raise HTTPException(status_code=400, detail=f"Only PDF files are allowed: {file.filename}")

    # Generate job ID
    job_id = str(uuid.uuid4())[:8]

    # Save uploaded files
    pdf_paths = []
    for idx, file in enumerate(files):
        pdf_path = UPLOAD_DIR / f"{job_id}_{idx}_{file.filename}"
        async with aiofiles.open(pdf_path, 'wb') as out_file:
            content = await file.read()
            await out_file.write(content)
        pdf_paths.append(pdf_path)

    # Initialize job
    first_filename = files[0].filename if files else "Unknown"
    jobs[job_id] = {
        "job_id": job_id,
        "status": "pending",
        "progress": 0,
        "current_task": "Queued for processing...",
        "title": title or Path(first_filename).stem,
        "author": author,
        "total_chapters": None,
        "current_chapter": None,
        "output_file": None,
        "error": None,
        "file_count": len(files),
        "requested_outputs": output_list,
        "output_files": {},
        "kb_id": target_kb_id,  # Target KB for ingestion
    }

    # Start conversion in background thread
    thread = threading.Thread(
        target=run_conversion,
        args=(job_id, pdf_paths, voice, title, author, output_list, target_kb_id)
    )
    thread.start()

    return {"job_id": job_id, "message": "Conversion started", "file_count": len(files), "outputs": output_list, "kb_id": target_kb_id}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str) -> JobStatus:
    """Get the status of a conversion job."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    return JobStatus(**job)


@app.get("/api/jobs")
async def list_jobs():
    """List all conversion jobs with their statuses."""
    result = []
    for jid, job in jobs.items():
        result.append({
            "job_id": job.get("job_id", jid),
            "status": job.get("status", "unknown"),
            "progress": job.get("progress", 0),
            "current_task": job.get("current_task"),
            "title": job.get("title"),
            "author": job.get("author"),
            "error": job.get("error"),
            "requested_outputs": job.get("requested_outputs"),
            "output_files": job.get("output_files"),
            "source_type": job.get("source_type"),
        })
    # Most recent first (dict insertion order = creation order, reverse it)
    result.reverse()
    return result


@app.get("/api/download/{job_id}")
async def download_audiobook(job_id: str, output_type: str = "m4b"):
    """Download a completed output file.

    Args:
        job_id: The job identifier
        output_type: Type of output to download (m4b, searchable_pdf, combined_pdf)
    """
    # Determine media type based on output type
    media_types = {
        "m4b": "audio/mp4",
        "searchable_pdf": "application/pdf",
        "combined_pdf": "application/pdf"
    }
    media_type = media_types.get(output_type, "application/octet-stream")

    # Check in active jobs
    if job_id in jobs:
        job = jobs[job_id]
        if job["status"] != "completed":
            raise HTTPException(status_code=400, detail="Conversion not complete")

        output_files = job.get("output_files", {})
        output_file = output_files.get(output_type)

        # Fallback to output_file for backward compatibility
        if not output_file and output_type == "m4b":
            output_file = job.get("output_file")

        if output_file and Path(output_file).exists():
            return FileResponse(
                output_file,
                media_type=media_type,
                filename=Path(output_file).name
            )

    # Check in library for any output type
    library = load_library()
    for item in library:
        if item["id"] == job_id:
            # Check new output_files structure first
            output_files = item.get("output_files", {})
            if output_type in output_files:
                output_info = output_files[output_type]
                file_path = Path(output_info.get("path", ""))
                if file_path.exists():
                    return FileResponse(
                        str(file_path),
                        media_type=media_type,
                        filename=output_info.get("filename", file_path.name)
                    )

            # Fallback to legacy path field for backward compatibility with M4B
            if output_type == "m4b" and "path" in item:
                file_path = Path(item.get("path", ""))
                if file_path.exists():
                    return FileResponse(
                        str(file_path),
                        media_type=media_type,
                        filename=item.get("filename", file_path.name)
                    )

    raise HTTPException(status_code=404, detail=f"Output file not found: {output_type}")


@app.get("/api/library")
async def get_library(
    organization: str = None,
    course_code: str = None,
    course_id: str = None,
    module_id: str = None,
    topic_id: str = None,
    custom_tag: str = None,
    has_kb: bool = None
) -> list[LibraryItem]:
    """
    Get the library of completed conversions with optional filtering.

    Filter parameters:
    - organization: Filter by organization name
    - course_code: Filter by course code (e.g., "CS7637")
    - course_id: Filter by linked course ID in KB
    - module_id: Filter by linked module ID in KB
    - topic_id: Filter by linked topic ID in KB
    - custom_tag: Filter by custom tag
    - has_kb: Filter by KB association (true = has KB, false = no KB)
    """
    items = load_library()
    result = []

    for item in items:
        # Must have at least id, title, author, created_at
        if not all(k in item for k in ['id', 'title', 'author', 'created_at']):
            continue

        # Ensure tags exist (for legacy items)
        if 'tags' not in item:
            item['tags'] = {}

        tags = item.get('tags', {})

        # Apply filters
        if organization and tags.get('organization', '').lower() != organization.lower():
            continue
        if course_code and tags.get('course_code', '').lower() != course_code.lower():
            continue
        if course_id and tags.get('course_id') != course_id:
            continue
        if module_id and tags.get('module_id') != module_id:
            continue
        if topic_id and topic_id not in tags.get('topic_ids', []):
            continue
        if custom_tag and custom_tag not in tags.get('custom_tags', []):
            continue
        if has_kb is not None:
            item_has_kb = bool(item.get('kb_id')) or bool(
                isinstance(item.get('output_files', {}).get('knowledge_base'), dict)
                and item['output_files']['knowledge_base'].get('kb_id')
            )
            if has_kb != item_has_kb:
                continue

        # For legacy items, check for M4B-specific fields
        if 'filename' in item and 'output_files' not in item:
            # Legacy M4B-only item - needs all legacy fields
            if all(k in item for k in ['filename', 'duration_minutes', 'file_size_mb', 'chapters', 'voice']):
                result.append(LibraryItem(**item))
        else:
            # New format with output_files
            result.append(LibraryItem(**item))

    return result


@app.get("/api/library/filters")
async def get_library_filters():
    """
    Get available filter values from existing library items.

    Returns distinct values for organizations, course codes, and custom tags.
    """
    items = load_library()

    organizations = set()
    course_codes = set()
    custom_tags = set()

    for item in items:
        tags = item.get('tags', {})
        if tags.get('organization'):
            organizations.add(tags['organization'])
        if tags.get('course_code'):
            course_codes.add(tags['course_code'])
        for tag in tags.get('custom_tags', []):
            custom_tags.add(tag)

    return {
        "organizations": sorted(organizations),
        "course_codes": sorted(course_codes),
        "custom_tags": sorted(custom_tags)
    }


@app.get("/api/library/{item_id}")
async def get_library_item(item_id: str):
    """Get a single library item by ID."""
    items = load_library()
    for item in items:
        if item.get('id') == item_id:
            # Ensure tags exists
            if 'tags' not in item:
                item['tags'] = {}
            # Return the raw dict - more flexible than strict Pydantic validation
            return item
    raise HTTPException(status_code=404, detail="Item not found")


@app.patch("/api/library/{item_id}/tags")
async def update_library_item_tags(item_id: str, request: LibraryItemTagsUpdate):
    """
    Update tags for a library item.

    If sync_to_kb is True and the item has an associated KB document,
    the hierarchical structure will be synced to the KB.
    """
    items = load_library()
    item_index = None
    item = None

    for i, it in enumerate(items):
        if it.get('id') == item_id:
            item_index = i
            item = it
            break

    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")

    # Initialize tags if not present
    if 'tags' not in item:
        item['tags'] = {}

    # Update only provided fields
    if request.organization is not None:
        item['tags']['organization'] = request.organization
    if request.course_code is not None:
        item['tags']['course_code'] = request.course_code
    if request.course_id is not None:
        item['tags']['course_id'] = request.course_id
    if request.module_id is not None:
        item['tags']['module_id'] = request.module_id
    if request.topic_ids is not None:
        item['tags']['topic_ids'] = request.topic_ids
    if request.custom_tags is not None:
        item['tags']['custom_tags'] = request.custom_tags

    # Sync to KB if requested and item has KB association
    kb_sync_result = None
    if request.sync_to_kb and item.get('kb_id') and item.get('document_id'):
        kb_sync_result = await _sync_tags_to_kb(item)

    # Save updated library
    items[item_index] = item
    save_library(items)

    return {
        "success": True,
        "tags": item['tags'],
        "kb_sync": kb_sync_result
    }


@app.patch("/api/library/{item_id}/metadata")
async def update_library_item_metadata(item_id: str, request: LibraryItemMetadataUpdate):
    """
    Update metadata (title, author) for a library item.

    If the item has an associated KB document, the document metadata
    will also be updated.
    """
    items = load_library()
    item_index = None
    item = None

    for i, it in enumerate(items):
        if it.get('id') == item_id:
            item_index = i
            item = it
            break

    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")

    # Update only provided fields
    if request.title is not None:
        item['title'] = request.title
    if request.author is not None:
        item['author'] = request.author

    # Update KB document if associated
    kb_update_result = None
    if item.get('kb_id') and item.get('document_id'):
        try:
            registry = get_kb_registry()
            docs = registry.list_documents(item['kb_id'])
            for doc in docs:
                if doc.id == item['document_id']:
                    # Update document metadata
                    if request.title is not None:
                        doc.title = request.title
                    if request.author is not None:
                        doc.author = request.author
                    registry.save_document(item['kb_id'], doc)
                    kb_update_result = {"document_updated": True}
                    break
        except Exception as e:
            logger.warning(f"Failed to update KB document metadata: {e}")
            kb_update_result = {"document_updated": False, "error": str(e)}

    # Save updated library
    items[item_index] = item
    save_library(items)

    return {
        "success": True,
        "title": item['title'],
        "author": item['author'],
        "kb_update": kb_update_result
    }


async def _sync_tags_to_kb(item: dict) -> dict:
    """
    Sync library item tags to the associated KB's course structure.

    Creates or links courses/modules/topics as needed.
    """
    kb_id = item.get('kb_id')
    document_id = item.get('document_id')
    tags = item.get('tags', {})

    if not kb_id or not document_id:
        return {"synced": False, "reason": "No KB association"}

    graph_store = get_graph_store()
    if not graph_store:
        return {"synced": False, "reason": "Graph database not available"}

    result = {"synced": True, "actions": []}

    try:
        # If course_code provided but no course_id, try to find or create course
        if tags.get('course_code') and not tags.get('course_id'):
            # Search for existing course with this code
            courses = graph_store.search_courses(kb_id, tags['course_code'], top_k=1)
            if courses:
                tags['course_id'] = courses[0].get('id')
                result['actions'].append(f"Linked to existing course: {courses[0].get('title')}")
            else:
                # Create new course
                from knowledge_base.models import Course, generate_id
                course = Course(
                    id=generate_id(),
                    kb_id=kb_id,
                    title=tags.get('organization', '') + ' ' + tags['course_code'],
                    code=tags['course_code'],
                    description=""
                )
                graph_store.add_course(kb_id, course)
                tags['course_id'] = course.id
                result['actions'].append(f"Created new course: {course.title}")

        # Link document to course in graph if we have both
        if tags.get('course_id') and document_id:
            with graph_store._session() as session:
                session.run(
                    """
                    MATCH (d:Document {id: $doc_id})
                    MATCH (c:Course {id: $course_id})
                    MERGE (c)-[:CONTAINS_DOCUMENT]->(d)
                    """,
                    doc_id=document_id,
                    course_id=tags['course_id']
                )
                result['actions'].append("Linked document to course")

        # Link document to module if specified
        if tags.get('module_id') and document_id:
            with graph_store._session() as session:
                session.run(
                    """
                    MATCH (d:Document {id: $doc_id})
                    MATCH (m:Module {id: $module_id})
                    MERGE (m)-[:CONTAINS_DOCUMENT]->(d)
                    """,
                    doc_id=document_id,
                    module_id=tags['module_id']
                )
                result['actions'].append("Linked document to module")

        # Link document to topics if specified
        if tags.get('topic_ids') and document_id:
            for topic_id in tags['topic_ids']:
                with graph_store._session() as session:
                    session.run(
                        """
                        MATCH (d:Document {id: $doc_id})
                        MATCH (t:Topic {id: $topic_id})
                        MERGE (t)-[:CONTAINS_DOCUMENT]->(d)
                        """,
                        doc_id=document_id,
                        topic_id=topic_id
                    )
            result['actions'].append(f"Linked document to {len(tags['topic_ids'])} topics")

    except Exception as e:
        logger.error(f"Failed to sync tags to KB: {e}")
        result['synced'] = False
        result['error'] = str(e)

    return result


@app.delete("/api/library/{item_id}")
async def delete_from_library(item_id: str):
    """Delete an item and all its outputs from the library."""
    items = load_library()
    item_to_delete = None

    for item in items:
        if item["id"] == item_id:
            item_to_delete = item
            break

    if not item_to_delete:
        raise HTTPException(status_code=404, detail="Item not found")

    # Find job directory from any output file path
    job_dir = None

    # Check new output_files structure
    output_files = item_to_delete.get("output_files", {})
    for output_info in output_files.values():
        if isinstance(output_info, dict) and "path" in output_info:
            item_path = Path(output_info["path"])
            if item_path.exists():
                job_dir = item_path.parent
                break

    # Fallback to legacy path field
    if not job_dir and "path" in item_to_delete:
        item_path = Path(item_to_delete["path"])
        if item_path.exists():
            job_dir = item_path.parent

    # Remove the entire job directory
    if job_dir and job_dir.exists() and job_dir != LIBRARY_DIR:
        shutil.rmtree(job_dir)

    # Update library
    items = [i for i in items if i["id"] != item_id]
    save_library(items)

    return {"message": "Deleted successfully"}


@app.delete("/api/library/{item_id}/output/{output_type}")
async def delete_library_output(item_id: str, output_type: str):
    """Delete a specific output from a library item (m4b, searchable_pdf, knowledge_base)."""
    valid_output_types = {"m4b", "searchable_pdf", "combined_pdf", "knowledge_base"}
    if output_type not in valid_output_types:
        raise HTTPException(status_code=400, detail=f"Invalid output type: {output_type}")

    items = load_library()
    item = None
    item_idx = None

    for idx, i in enumerate(items):
        if i["id"] == item_id:
            item = i
            item_idx = idx
            break

    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    output_files = item.get("output_files", {})

    # Check if output exists
    if output_type not in output_files:
        # Check legacy path field for M4B
        if output_type == "m4b" and "path" in item:
            # Handle legacy M4B format
            legacy_path = Path(item["path"])
            if legacy_path.exists():
                legacy_path.unlink()
            # Remove legacy fields
            for field in ["path", "filename", "duration_minutes", "file_size_mb", "chapters", "voice"]:
                item.pop(field, None)
        else:
            raise HTTPException(status_code=404, detail=f"Output not found: {output_type}")
    else:
        # Handle new output_files format
        output_info = output_files[output_type]

        if output_type == "knowledge_base":
            # Delete from KB (vector store and registry)
            kb_id = output_info.get("kb_id") if isinstance(output_info, dict) else output_info
            if kb_id:
                try:
                    # Get all documents for this library item in the KB and delete them
                    registry = get_kb_registry()
                    pipeline = get_kb_pipeline()

                    # Find and delete documents associated with this item
                    # First check if we have a stored document_id (preferred)
                    # Otherwise fall back to title/author matching
                    stored_doc_id = output_info.get("document_id") if isinstance(output_info, dict) else None
                    docs = registry.list_documents(kb_id)

                    for doc in docs:
                        # Match by stored document_id if available, otherwise by title+author
                        if stored_doc_id:
                            matches = (doc.id == stored_doc_id)
                        else:
                            matches = (doc.title == item.get("title") and doc.author == item.get("author"))

                        if matches:
                            # Track stats for update
                            deleted_chunks = 0
                            deleted_entities = 0

                            # Delete from vector store
                            try:
                                deleted_chunks = pipeline.vector_store.delete_document(kb_id, doc.id)
                            except Exception as e:
                                logger.warning(f"Failed to delete from vector store: {e}")

                            # Delete from graph store if available
                            if pipeline.graph_store:
                                try:
                                    result = pipeline.graph_store.delete_document(kb_id, doc.id)
                                    deleted_entities = result.get("entities_deleted", 0)
                                except Exception as e:
                                    logger.warning(f"Failed to delete from graph store: {e}")

                            # Delete from registry
                            registry.delete_document(kb_id, doc.id)

                            # Update KB stats (decrement counts)
                            registry.increment_stats(
                                kb_id,
                                documents=-1,
                                chapters=-len(doc.chapters) if doc.chapters else 0,
                                chunks=-deleted_chunks,
                                entities=-deleted_entities
                            )

                            logger.info(f"Deleted document {doc.id} from KB {kb_id} (chunks: {deleted_chunks}, entities: {deleted_entities})")

                            # If we matched by document_id, we're done (only one match expected)
                            if stored_doc_id:
                                break
                except Exception as e:
                    logger.error(f"Failed to delete from KB: {e}")
        else:
            # Delete the file
            if isinstance(output_info, dict) and "path" in output_info:
                file_path = Path(output_info["path"])
                if file_path.exists():
                    file_path.unlink()
                    logger.info(f"Deleted file: {file_path}")

        # Remove from output_files
        del output_files[output_type]
        item["output_files"] = output_files

    # Check if all outputs are gone
    remaining_outputs = item.get("output_files", {})
    has_legacy_path = "path" in item

    if not remaining_outputs and not has_legacy_path:
        # No outputs left - remove the entire item and its directory
        job_dir = LIBRARY_DIR / item_id
        if job_dir.exists() and job_dir != LIBRARY_DIR:
            shutil.rmtree(job_dir)
        items = [i for i in items if i["id"] != item_id]
        logger.info(f"Removed library item {item_id} (no outputs remaining)")
    else:
        # Update the item in place
        items[item_idx] = item

    save_library(items)

    return {"message": f"Deleted {output_type} successfully", "remaining_outputs": list(item.get("output_files", {}).keys()) if item_idx is not None and item_idx < len(items) else []}


class ReingestRequest(BaseModel):
    kb_id: str


@app.post("/api/library/{item_id}/ingest")
async def reingest_to_kb(item_id: str, request: ReingestRequest):
    """Re-ingest a library item into a knowledge base.

    Useful if initial ingestion failed or to add to a different KB.
    """
    # Find the library item
    items = load_library()
    item = None
    for i in items:
        if i["id"] == item_id:
            item = i
            break

    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    # Find a PDF to extract from (prefer searchable PDF)
    pdf_path = None
    output_files = item.get("output_files", {})

    if "searchable_pdf" in output_files:
        pdf_path = Path(output_files["searchable_pdf"].get("path", ""))

    if not pdf_path or not pdf_path.exists():
        raise HTTPException(
            status_code=400,
            detail="No PDF available for re-ingestion. Searchable PDF not found."
        )

    # Verify KB exists
    try:
        registry = get_kb_registry()
        kb = registry.get(request.kb_id)
        if not kb:
            raise HTTPException(status_code=404, detail=f"Knowledge base not found: {request.kb_id}")
    except Exception as e:
        logger.error(f"Failed to get KB registry: {e}")
        raise HTTPException(status_code=503, detail="Knowledge base feature not available")

    # Extract chapters from PDF
    try:
        from pipeline.pdf_extractor import PDFExtractor

        extractor = PDFExtractor(str(pdf_path))
        page_count = extractor.get_page_count()

        # First try to detect chapters from TOC
        toc_chapters = extractor.detect_chapters_from_toc()

        # Then extract chapter text
        chapters = extractor.detect_chapters_from_text(
            {"chapters": toc_chapters} if toc_chapters else None
        )

        # Prepare chapters in expected format
        chapters_data = []

        if chapters:
            for i, ch in enumerate(chapters):
                chapters_data.append({
                    "number": i + 1,
                    "title": ch.title,
                    "text": ch.content,
                    "start_page": ch.page_start
                })
        else:
            # Fallback: extract full text as single chapter
            logger.info("No chapters detected, extracting full text as single chapter")
            full_text = ""
            for page_num in range(page_count):
                full_text += extractor.extract_page_text(page_num) + "\n"

            chapters_data = [{
                "number": 1,
                "title": item.get("title", "Full Document"),
                "text": full_text.strip(),
                "start_page": 0
            }]

        extractor.close()

        if not chapters_data:
            raise HTTPException(status_code=400, detail="No content extracted from PDF")

        # Get pipeline and ingest
        pipeline = get_kb_pipeline()

        doc = pipeline.ingest_document(
            kb_id=request.kb_id,
            title=item.get("title", "Unknown"),
            author=item.get("author", "Unknown"),
            chapters=chapters_data,
            source_file=pdf_path.name,
            total_pages=page_count
        )

        # Update library metadata to indicate KB ingestion
        output_files["knowledge_base"] = {"kb_id": request.kb_id, "document_id": doc.id}
        item["output_files"] = output_files
        save_library(items)

        logger.info(f"Re-ingested document {item_id} into KB {request.kb_id}, document {doc.id}")

        return {
            "message": "Document ingested successfully",
            "document_id": doc.id,
            "kb_id": request.kb_id,
            "chapter_count": len(chapters)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Re-ingestion failed: {e}")
        raise HTTPException(status_code=500, detail=f"Re-ingestion failed: {str(e)}")


@app.get("/api/voices")
async def get_voices():
    """Get available TTS voices."""
    return {
        "voices": [
            {"id": "af_heart", "name": "Heart", "gender": "female", "description": "Warm, expressive", "recommended": True},
            {"id": "af_bella", "name": "Bella", "gender": "female", "description": "Clear, professional"},
            {"id": "af_nicole", "name": "Nicole", "gender": "female", "description": "Soft, gentle"},
            {"id": "af_sarah", "name": "Sarah", "gender": "female", "description": "Bright, energetic"},
            {"id": "af_sky", "name": "Sky", "gender": "female", "description": "Youthful"},
            {"id": "am_adam", "name": "Adam", "gender": "male", "description": "Deep, authoritative"},
            {"id": "am_michael", "name": "Michael", "gender": "male", "description": "Warm, friendly"},
        ]
    }


# ============ Knowledge Base API Endpoints ============

@app.get("/api/knowledge-bases")
async def list_knowledge_bases() -> list[KBResponse]:
    """List all knowledge bases."""
    registry = get_kb_registry()
    kbs = registry.list()
    return [
        KBResponse(
            id=kb.id,
            name=kb.name,
            description=kb.description,
            created_at=kb.created_at.isoformat(),
            updated_at=kb.updated_at.isoformat(),
            document_count=kb.stats.document_count,
            chunk_count=kb.stats.chunk_count,
        )
        for kb in kbs
    ]


@app.post("/api/knowledge-bases")
async def create_knowledge_base(request: KBCreateRequest) -> KBResponse:
    """Create a new knowledge base."""
    registry = get_kb_registry()
    kb = registry.create(name=request.name, description=request.description)
    return KBResponse(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        created_at=kb.created_at.isoformat(),
        updated_at=kb.updated_at.isoformat(),
        document_count=0,
        chunk_count=0,
    )


@app.get("/api/knowledge-bases/{kb_id}")
async def get_knowledge_base(kb_id: str) -> KBResponse:
    """Get a knowledge base by ID."""
    registry = get_kb_registry()
    kb = registry.get(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return KBResponse(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        created_at=kb.created_at.isoformat(),
        updated_at=kb.updated_at.isoformat(),
        document_count=kb.stats.document_count,
        chunk_count=kb.stats.chunk_count,
    )


@app.delete("/api/knowledge-bases/{kb_id}")
async def delete_knowledge_base(kb_id: str):
    """Delete a knowledge base and all its data."""
    registry = get_kb_registry()

    if not registry.exists(kb_id):
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    pipeline = get_kb_pipeline()

    # Delete vector data
    try:
        pipeline.vector_store.delete_kb(kb_id)
    except Exception as e:
        logger.warning(f"Failed to delete vector data for KB {kb_id}: {e}")

    # Delete graph data if graph store is available
    if pipeline.graph_store:
        try:
            pipeline.graph_store.delete_kb(kb_id)
        except Exception as e:
            logger.warning(f"Failed to delete graph data for KB {kb_id}: {e}")

    # Delete registry entry
    registry.delete(kb_id)
    return {"message": "Knowledge base deleted", "id": kb_id}


@app.patch("/api/knowledge-bases/{kb_id}")
async def update_knowledge_base(kb_id: str, request: KBCreateRequest):
    """Update a knowledge base (name/description)."""
    registry = get_kb_registry()
    kwargs = {}
    if request.name:
        kwargs["name"] = request.name
    if request.description is not None:
        kwargs["description"] = request.description
    kb = registry.update(kb_id, **kwargs)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return KBResponse(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        created_at=kb.created_at.isoformat(),
        updated_at=kb.updated_at.isoformat(),
        document_count=kb.stats.document_count,
        chunk_count=kb.stats.chunk_count,
    )



@app.get("/api/knowledge-bases/{kb_id}/documents")
async def list_kb_documents(kb_id: str) -> list[KBDocumentResponse]:
    """List all documents in a knowledge base."""
    registry = get_kb_registry()

    if not registry.exists(kb_id):
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    docs = registry.list_documents(kb_id)
    return [
        KBDocumentResponse(
            id=doc.id,
            kb_id=doc.kb_id,
            title=doc.title,
            author=doc.author,
            source_file=doc.source_file,
            total_pages=doc.total_pages,
            created_at=doc.created_at.isoformat(),
            ocr_required=doc.ocr_required,
            chapter_count=len(doc.chapters),
        )
        for doc in docs
    ]


@app.get("/api/knowledge-bases/{kb_id}/documents/{doc_id}")
async def get_kb_document(kb_id: str, doc_id: str) -> KBDocumentDetailResponse:
    """Get a specific document with its chapters."""
    registry = get_kb_registry()

    if not registry.exists(kb_id):
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    doc = registry.get_document(kb_id, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    return KBDocumentDetailResponse(
        id=doc.id,
        kb_id=doc.kb_id,
        title=doc.title,
        author=doc.author,
        source_file=doc.source_file,
        total_pages=doc.total_pages,
        created_at=doc.created_at.isoformat(),
        ocr_required=doc.ocr_required,
        chapter_count=len(doc.chapters),
        chapters=[
            {
                "id": ch.id,
                "number": ch.number,
                "title": ch.title,
                "start_page": ch.start_page,
            }
            for ch in doc.chapters
        ],
    )


@app.delete("/api/knowledge-bases/{kb_id}/documents/{doc_id}")
async def delete_kb_document(kb_id: str, doc_id: str):
    """Delete a document from a knowledge base."""
    registry = get_kb_registry()

    if not registry.exists(kb_id):
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    doc = registry.get_document(kb_id, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    pipeline = get_kb_pipeline()

    # Delete from vector store
    deleted_chunks = 0
    try:
        deleted_chunks = pipeline.vector_store.delete_document(kb_id, doc_id)
        logger.info(f"Deleted {deleted_chunks} chunks for document {doc_id}")
    except Exception as e:
        logger.warning(f"Failed to delete vector data for document {doc_id}: {e}")

    # Delete from graph store and get deleted entity count
    deleted_entities = 0
    if pipeline.graph_store:
        try:
            result = pipeline.graph_store.delete_document(kb_id, doc_id)
            deleted_entities = result.get("entities_deleted", 0)
            logger.info(f"Deleted graph data for document {doc_id}: {result}")
        except Exception as e:
            logger.warning(f"Failed to delete graph data for document {doc_id}: {e}")

    # Delete from registry
    registry.delete_document(kb_id, doc_id)

    # Update KB stats (decrement documents, chapters, chunks, and entities)
    registry.increment_stats(
        kb_id,
        documents=-1,
        chapters=-len(doc.chapters),
        chunks=-deleted_chunks,
        entities=-deleted_entities
    )

    return {"message": "Document deleted", "id": doc_id}


@app.post("/api/knowledge-bases/search")
async def search_knowledge_bases(request: KBSearchRequest) -> list[KBSearchResult]:
    """Search across knowledge bases."""
    pipeline = get_kb_pipeline()

    results = pipeline.search(
        query=request.query,
        kb_ids=request.kb_ids,
        top_k=request.top_k
    )

    return [
        KBSearchResult(
            text=r["text"],
            score=r["score"],
            document_id=r["document_id"],
            document_title=r["document_title"],
            document_author=r["document_author"],
            chapter_title=r.get("chapter_title", ""),
            kb_id=r["kb_id"],
        )
        for r in results
    ]


# Graph API Endpoints

@app.get("/api/knowledge-bases/{kb_id}/entities")
async def list_kb_entities(
    kb_id: str,
    entity_type: Optional[str] = None,
    limit: int = 100
) -> list[EntityResponse]:
    """List entities in a knowledge base."""
    registry = get_kb_registry()
    pipeline = get_kb_pipeline()

    if not registry.exists(kb_id):
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    if not pipeline.graph_store:
        raise HTTPException(status_code=503, detail="Graph store not available")

    try:
        entities = pipeline.graph_store.search_entities(
            kb_id=kb_id,
            query="",  # Empty query returns all
            entity_type=entity_type,
            top_k=limit
        )
        return [
            EntityResponse(
                id=e.get("id", ""),
                name=e.get("name", ""),
                type=e.get("type", ""),
                description=e.get("description", ""),
                aliases=e.get("aliases", []),
                mention_count=e.get("mention_count", 0),
            )
            for e in entities
        ]
    except Exception as e:
        logger.error(f"Failed to list entities: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/knowledge-bases/{kb_id}/documents/{doc_id}/entities")
async def get_document_entities(
    kb_id: str,
    doc_id: str,
    entity_type: Optional[str] = None
) -> list[EntityResponse]:
    """Get entities for a specific document."""
    registry = get_kb_registry()
    pipeline = get_kb_pipeline()

    if not registry.exists(kb_id):
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    doc = registry.get_document(kb_id, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    if not pipeline.graph_store:
        raise HTTPException(status_code=503, detail="Graph store not available")

    try:
        entities = pipeline.graph_store.get_document_entities(
            kb_id=kb_id,
            document_id=doc_id,
            entity_type=entity_type
        )
        return [
            EntityResponse(
                id=e.get("id", ""),
                name=e.get("name", ""),
                type=e.get("type", ""),
                description=e.get("description", ""),
                aliases=e.get("aliases", []),
                mention_count=e.get("mention_count", 0),
            )
            for e in entities
        ]
    except Exception as e:
        logger.error(f"Failed to get document entities: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/knowledge-bases/{kb_id}/documents/{doc_id}/related")
async def get_related_documents(
    kb_id: str,
    doc_id: str,
    max_hops: int = 2
) -> list[RelatedDocumentResponse]:
    """Get documents related to a specific document via shared entities."""
    registry = get_kb_registry()
    pipeline = get_kb_pipeline()

    if not registry.exists(kb_id):
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    doc = registry.get_document(kb_id, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    if not pipeline.graph_store:
        raise HTTPException(status_code=503, detail="Graph store not available")

    try:
        related = pipeline.graph_store.find_related_documents(
            kb_id=kb_id,
            document_id=doc_id,
            max_hops=max_hops
        )
        return [
            RelatedDocumentResponse(
                id=r.get("id", ""),
                title=r.get("title", ""),
                author=r.get("author", ""),
                relationship_type=r.get("relationship_type", "shared_entity"),
                score=r.get("score", 0.0),
            )
            for r in related
        ]
    except Exception as e:
        logger.error(f"Failed to find related documents: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/knowledge-bases/search-fulltext")
async def fulltext_search(request: FullTextSearchRequest) -> list[FullTextSearchResult]:
    """Full-text search using Neo4j (keyword-based)."""
    pipeline = get_kb_pipeline()

    if not pipeline.graph_store:
        raise HTTPException(status_code=503, detail="Graph store not available")

    # Determine which KBs to search
    registry = get_kb_registry()
    kb_ids = request.kb_ids
    if kb_ids is None:
        kb_ids = [kb.id for kb in registry.list()]

    if not kb_ids:
        return []

    try:
        results = []
        for kb_id in kb_ids:
            kb_results = pipeline.graph_store.full_text_search(
                kb_id=kb_id,
                query=request.query,
                top_k=request.top_k
            )
            for r in kb_results:
                results.append(FullTextSearchResult(
                    text=r.get("text", ""),
                    score=r.get("score", 0.0),
                    document_id=r.get("document_id", ""),
                    document_title=r.get("document_title", ""),
                    chapter_title=r.get("chapter_title", ""),
                    kb_id=kb_id,
                ))

        # Sort by score and limit
        results.sort(key=lambda x: x.score, reverse=True)
        return results[:request.top_k]
    except Exception as e:
        logger.error(f"Full-text search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/knowledge-bases/{kb_id}/stats/graph")
async def get_graph_stats(kb_id: str) -> GraphStatsResponse:
    """Get graph statistics for a knowledge base."""
    registry = get_kb_registry()
    pipeline = get_kb_pipeline()

    if not registry.exists(kb_id):
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    if not pipeline.graph_store:
        raise HTTPException(status_code=503, detail="Graph store not available")

    try:
        stats = pipeline.graph_store.get_stats(kb_id)
        return GraphStatsResponse(
            documents=stats.get("documents", 0),
            chapters=stats.get("chapters", 0),
            chunks=stats.get("chunks", 0),
            entities=stats.get("entities", 0),
            relationships=stats.get("relationships", 0),
        )
    except Exception as e:
        logger.error(f"Failed to get graph stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================================
# Phase 5: Advanced Knowledge Base Endpoints
# =========================================================================

@app.get("/api/knowledge-bases/{kb_id}/documents/{doc_id}/similar")
async def find_similar_documents(
    kb_id: str,
    doc_id: str,
    target_kb_ids: Optional[str] = None,
    top_k: int = 10,
    similarity_threshold: float = 0.7
) -> list[SimilarDocumentResponse]:
    """Find documents similar to the given document."""
    registry = get_kb_registry()
    pipeline = get_kb_pipeline()

    if not registry.exists(kb_id):
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    # Check document exists
    doc = registry.get_document(kb_id, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Parse target_kb_ids from comma-separated string
    target_kbs = None
    if target_kb_ids:
        target_kbs = [kb.strip() for kb in target_kb_ids.split(",")]

    try:
        from knowledge_base.advanced import DocumentSimilarityDetector

        vector_store, embedder = get_vector_store_and_embedder()
        detector = DocumentSimilarityDetector(
            vector_store, embedder, pipeline.graph_store
        )

        results = detector.find_similar_documents(
            document_id=doc_id,
            source_kb_id=kb_id,
            target_kb_ids=target_kbs,
            top_k=top_k,
            similarity_threshold=similarity_threshold
        )

        return [SimilarDocumentResponse(**r) for r in results]

    except Exception as e:
        logger.error(f"Failed to find similar documents: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/knowledge-bases/{kb_id}/documents/{doc_id}/citations")
async def detect_citations(kb_id: str, doc_id: str) -> list[CitedDocumentResponse]:
    """Detect citations in the document and match to KB documents."""
    registry = get_kb_registry()

    if not registry.exists(kb_id):
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    # Check document exists
    doc = registry.get_document(kb_id, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    try:
        from knowledge_base.advanced import CitationDetector

        # Get document text using helper
        document_text = get_document_text(kb_id, doc_id)
        if not document_text:
            return []

        detector = CitationDetector(registry)
        cited = detector.find_cited_documents(doc_id, kb_id, document_text)

        return [CitedDocumentResponse(**c) for c in cited]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to detect citations: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/knowledge-bases/{kb_id}/documents/{doc_id}/relationships")
async def infer_relationships(kb_id: str, doc_id: str) -> list[InferredRelationshipResponse]:
    """Infer potential relationships for a document."""
    registry = get_kb_registry()
    pipeline = get_kb_pipeline()

    if not registry.exists(kb_id):
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    # Check document exists
    doc = registry.get_document(kb_id, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    if not pipeline.graph_store:
        raise HTTPException(status_code=503, detail="Graph store not available")

    try:
        from knowledge_base.advanced import RelationshipInferencer

        vector_store, embedder = get_vector_store_and_embedder()
        document_text = get_document_text(kb_id, doc_id)

        inferencer = RelationshipInferencer(
            registry, pipeline.graph_store, vector_store, embedder
        )

        relationships = inferencer.infer_relationships(doc_id, kb_id, document_text)

        return [InferredRelationshipResponse(**r) for r in relationships]

    except Exception as e:
        logger.error(f"Failed to infer relationships: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class ExportRequest(BaseModel):
    include_embeddings: bool = False


@app.post("/api/knowledge-bases/{kb_id}/export")
async def export_kb(kb_id: str, request: ExportRequest = None) -> ExportSummaryResponse:
    """Export a knowledge base to a JSON file."""
    registry = get_kb_registry()
    pipeline = get_kb_pipeline()

    if not registry.exists(kb_id):
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    try:
        from knowledge_base.advanced import KBExporter
        from pathlib import Path
        from config.settings import settings

        vector_store, _ = get_vector_store_and_embedder()
        exporter = KBExporter(registry, vector_store, pipeline.graph_store)

        # Create exports directory
        exports_dir = Path(settings.KB_DIR) / "exports"
        exports_dir.mkdir(exist_ok=True)
        output_path = exports_dir / f"{kb_id}.json"

        include_embeddings = request.include_embeddings if request else False
        summary = exporter.export_kb(
            kb_id=kb_id,
            output_path=output_path,
            include_embeddings=include_embeddings,
            include_graph=True
        )

        return ExportSummaryResponse(**summary)

    except Exception as e:
        logger.error(f"Failed to export KB: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class ImportRequest(BaseModel):
    input_path: str
    new_name: Optional[str] = None


@app.post("/api/knowledge-bases/import")
async def import_kb(request: ImportRequest) -> ImportSummaryResponse:
    """Import a knowledge base from an exported JSON file."""
    from pathlib import Path
    from config.settings import settings

    input_path = Path(request.input_path).resolve()

    # Security: Only allow imports from the exports directory
    exports_dir = (Path(settings.KB_DIR) / "exports").resolve()
    exports_dir.mkdir(exist_ok=True)

    try:
        if not input_path.is_relative_to(exports_dir):
            raise HTTPException(
                status_code=403,
                detail=f"Import path must be within the exports directory: {exports_dir}"
            )
    except ValueError:
        # is_relative_to raises ValueError on Python < 3.9 for non-relative paths
        raise HTTPException(
            status_code=403,
            detail=f"Import path must be within the exports directory: {exports_dir}"
        )

    if not input_path.exists():
        raise HTTPException(status_code=404, detail=f"Import file not found: {input_path}")

    if not input_path.suffix == ".json":
        raise HTTPException(status_code=400, detail="Import file must be a .json file")

    try:
        from knowledge_base.advanced import KBImporter

        registry = get_kb_registry()
        pipeline = get_kb_pipeline()
        vector_store, embedder = get_vector_store_and_embedder()

        importer = KBImporter(registry, vector_store, embedder, pipeline.graph_store)

        summary = importer.import_kb(
            input_path=input_path,
            new_kb_name=request.new_name,
            regenerate_embeddings=False
        )

        return ImportSummaryResponse(**summary)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to import KB: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/analytics")
async def get_analytics(analytics_type: str = "search", days: int = 7) -> dict:
    """Get usage analytics for the knowledge base system."""
    try:
        from knowledge_base.advanced import UsageAnalytics
        from config.settings import settings

        analytics = UsageAnalytics(settings.KB_DIR)

        if analytics_type == "search":
            return analytics.get_search_analytics(days)
        elif analytics_type == "documents":
            return analytics.get_document_analytics(days)
        elif analytics_type == "all":
            return {
                "search": analytics.get_search_analytics(days),
                "documents": analytics.get_document_analytics(days)
            }
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid analytics_type: {analytics_type}. Use 'search', 'documents', or 'all'."
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get analytics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================================
# Course Hierarchy API Endpoints
# =========================================================================

@app.post("/api/knowledge-bases/{kb_id}/courses", response_model=CourseResponse)
async def create_course(kb_id: str, request: CourseCreateRequest):
    """Create a new course within a knowledge base."""
    registry = get_kb_registry()
    kb = registry.get(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    graph_store = get_graph_store()
    if not graph_store:
        raise HTTPException(status_code=503, detail="Graph database required for course hierarchy")

    try:
        from knowledge_base.models import Course, generate_id
        from datetime import datetime

        course = Course(
            id=generate_id(),
            kb_id=kb_id,
            title=request.title,
            code=request.code,
            description=request.description,
            instructor=request.instructor,
            created_at=datetime.now()
        )

        graph_store.add_course(kb_id, course)

        return CourseResponse(
            id=course.id,
            kb_id=kb_id,
            title=course.title,
            code=course.code,
            description=course.description,
            instructor=course.instructor,
            created_at=course.created_at.isoformat()
        )

    except Exception as e:
        logger.error(f"Failed to create course: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/courses/{course_id}")
async def get_course(course_id: str):
    """Get course details."""
    graph_store = get_graph_store()
    if not graph_store:
        raise HTTPException(status_code=503, detail="Graph database required")

    try:
        structure = graph_store.get_course_structure(course_id)
        if not structure:
            raise HTTPException(status_code=404, detail="Course not found")
        return structure

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get course: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/courses/{course_id}/structure")
async def get_course_structure(course_id: str):
    """Get full hierarchical structure of a course including modules, topics, and concepts."""
    graph_store = get_graph_store()
    if not graph_store:
        raise HTTPException(status_code=503, detail="Graph database required")

    try:
        structure = graph_store.get_course_structure(course_id)
        if not structure:
            raise HTTPException(status_code=404, detail="Course not found")
        return structure

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get course structure: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/courses/{course_id}/modules")
async def create_module(course_id: str, request: ModuleCreateRequest):
    """Add a module to a course."""
    graph_store = get_graph_store()
    if not graph_store:
        raise HTTPException(status_code=503, detail="Graph database required")

    try:
        from knowledge_base.models import Module, generate_id

        # Get course to verify it exists and get kb_id
        structure = graph_store.get_course_structure(course_id)
        if not structure:
            raise HTTPException(status_code=404, detail="Course not found")

        kb_id = structure.get("kb_id", "")

        # Calculate order based on existing modules
        modules = structure.get("modules", [])
        order = len([m for m in modules if m.get("module")])

        module = Module(
            id=generate_id(),
            course_id=course_id,
            number=request.number,
            title=request.title,
            description=request.description,
            order=order
        )

        graph_store.add_module(course_id, module, kb_id)

        return {
            "id": module.id,
            "course_id": course_id,
            "number": module.number,
            "title": module.title,
            "description": module.description,
            "order": module.order
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create module: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/modules/{module_id}/topics")
async def create_topic(module_id: str, request: TopicCreateRequest):
    """Add a topic to a module."""
    graph_store = get_graph_store()
    if not graph_store:
        raise HTTPException(status_code=503, detail="Graph database required")

    try:
        from knowledge_base.models import Topic, generate_id

        # Get module info (need kb_id)
        # For simplicity, we'll query for it
        with graph_store._session() as session:
            result = session.run(
                "MATCH (m:Module {id: $module_id}) RETURN m.kb_id as kb_id, m.course_id as course_id",
                module_id=module_id
            ).single()

            if not result:
                raise HTTPException(status_code=404, detail="Module not found")

            kb_id = result["kb_id"]

            # Count existing topics for order
            topic_count = session.run(
                "MATCH (m:Module {id: $module_id})-[:HAS_TOPIC]->(t:Topic) RETURN count(t) as cnt",
                module_id=module_id
            ).single()["cnt"]

        topic = Topic(
            id=generate_id(),
            module_id=module_id,
            title=request.title,
            description=request.description,
            order=topic_count
        )

        graph_store.add_topic(module_id, topic, kb_id)

        return {
            "id": topic.id,
            "module_id": module_id,
            "title": topic.title,
            "description": topic.description,
            "order": topic.order
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create topic: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/topics/{topic_id}/concepts")
async def create_concept(topic_id: str, request: ConceptCreateRequest):
    """Add a concept to a topic."""
    graph_store = get_graph_store()
    if not graph_store:
        raise HTTPException(status_code=503, detail="Graph database required")

    try:
        from knowledge_base.models import Concept, generate_id

        # Get topic info (need kb_id)
        with graph_store._session() as session:
            result = session.run(
                "MATCH (t:Topic {id: $topic_id}) RETURN t.kb_id as kb_id",
                topic_id=topic_id
            ).single()

            if not result:
                raise HTTPException(status_code=404, detail="Topic not found")

            kb_id = result["kb_id"]

        concept = Concept(
            id=generate_id(),
            topic_id=topic_id,
            name=request.name,
            definition=request.definition,
            chunk_ids=request.chunk_ids
        )

        graph_store.add_concept(topic_id, concept, kb_id)

        return {
            "id": concept.id,
            "topic_id": topic_id,
            "name": concept.name,
            "definition": concept.definition,
            "chunk_ids": concept.chunk_ids
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create concept: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/courses/{course_id}/learning-path")
async def get_learning_path(
    course_id: str,
    from_concept: str = None,
    to_concept: str = None
):
    """
    Get learning path between two concepts within a course.

    If concepts not specified, returns the sequential module/topic order.
    """
    graph_store = get_graph_store()
    if not graph_store:
        raise HTTPException(status_code=503, detail="Graph database required")

    try:
        # Get course info for kb_id
        structure = graph_store.get_course_structure(course_id)
        if not structure:
            raise HTTPException(status_code=404, detail="Course not found")

        kb_id = structure.get("kb_id", "")

        if from_concept and to_concept:
            # Find path between specific concepts
            path = graph_store.get_learning_path(from_concept, to_concept, kb_id)
            return {"path": path, "from_concept": from_concept, "to_concept": to_concept}
        else:
            # Return sequential structure
            return {
                "path": _extract_sequential_path(structure),
                "sequential": True
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get learning path: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _extract_sequential_path(structure: dict) -> list[dict]:
    """Extract sequential learning path from course structure."""
    path = []
    for module_data in structure.get("modules", []):
        module = module_data.get("module", {})
        if module:
            path.append({
                "type": "module",
                "id": module.get("id"),
                "title": module.get("title"),
                "number": module.get("number")
            })
            for topic_data in module_data.get("topics", []):
                topic = topic_data.get("topic", {})
                if topic:
                    path.append({
                        "type": "topic",
                        "id": topic.get("id"),
                        "title": topic.get("title")
                    })
                    for concept in topic_data.get("concepts", []):
                        if concept:
                            path.append({
                                "type": "concept",
                                "id": concept.get("id"),
                                "name": concept.get("name")
                            })
    return path


@app.get("/api/topics/{topic_id}/prerequisites")
async def get_topic_prerequisites(topic_id: str):
    """Get prerequisite topics for a given topic."""
    graph_store = get_graph_store()
    if not graph_store:
        raise HTTPException(status_code=503, detail="Graph database required")

    try:
        prereqs = graph_store.get_prerequisites(topic_id)
        return {"topic_id": topic_id, "prerequisites": prereqs}

    except Exception as e:
        logger.error(f"Failed to get prerequisites: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/topics/{topic_id}/prerequisites/{prereq_topic_id}")
async def add_topic_prerequisite(topic_id: str, prereq_topic_id: str):
    """Mark a topic as prerequisite of another."""
    graph_store = get_graph_store()
    if not graph_store:
        raise HTTPException(status_code=503, detail="Graph database required")

    try:
        graph_store.add_prerequisite(topic_id, prereq_topic_id)
        return {"success": True, "topic_id": topic_id, "prerequisite_id": prereq_topic_id}

    except Exception as e:
        logger.error(f"Failed to add prerequisite: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/knowledge-bases/{kb_id}/infer-relationships")
async def infer_relationships(kb_id: str):
    """Auto-infer relationships between concepts and topics based on content."""
    graph_store = get_graph_store()
    if not graph_store:
        raise HTTPException(status_code=503, detail="Graph database required")

    try:
        counts = graph_store.infer_relationships(kb_id)
        return {"kb_id": kb_id, "inferred": counts}

    except Exception as e:
        logger.error(f"Failed to infer relationships: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/courses/{course_id}")
async def delete_course(course_id: str):
    """Delete a course and all its modules, topics, and concepts."""
    graph_store = get_graph_store()
    if not graph_store:
        raise HTTPException(status_code=503, detail="Graph database required")

    try:
        counts = graph_store.delete_course(course_id)
        return {"success": True, "deleted": counts}

    except Exception as e:
        logger.error(f"Failed to delete course: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/knowledge-bases/{kb_id}/courses")
async def list_courses(kb_id: str):
    """List all courses in a knowledge base."""
    graph_store = get_graph_store()
    if not graph_store:
        raise HTTPException(status_code=503, detail="Graph database required")

    try:
        with graph_store._session() as session:
            result = session.run(
                """
                MATCH (c:Course {kb_id: $kb_id})
                OPTIONAL MATCH (c)-[:HAS_MODULE]->(m:Module)
                WITH c, count(DISTINCT m) as module_count
                RETURN c {.*, module_count: module_count}
                ORDER BY c.created_at DESC
                """,
                kb_id=kb_id
            )
            courses = [dict(record["c"]) for record in result]
            return {"kb_id": kb_id, "courses": courses}

    except Exception as e:
        logger.error(f"Failed to list courses: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/knowledge-bases/{kb_id}/concepts/search")
async def search_concepts(kb_id: str, q: str, top_k: int = 20):
    """Search for concepts by name or definition."""
    graph_store = get_graph_store()
    if not graph_store:
        raise HTTPException(status_code=503, detail="Graph database required")

    try:
        concepts = graph_store.search_concepts(kb_id, q, top_k)
        return {"kb_id": kb_id, "query": q, "concepts": concepts}

    except Exception as e:
        logger.error(f"Failed to search concepts: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =========================================================================
# Chat API Endpoint
# =========================================================================

@app.post("/api/chat")
async def chat_with_kb(request: ChatRequest) -> ChatResponse:
    """
    RAG chat over a knowledge base.

    Returns retrieved context chunks and a synthesized answer.
    Browser automation has been removed from this endpoint; it is now
    handled by the noetix-ui layer via Codex + Playwright MCP.
    """
    registry = get_kb_registry()
    kb = registry.get(request.kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    return await _basic_rag_chat(request, kb)


async def _basic_rag_chat(request: ChatRequest, kb) -> ChatResponse:
    """
    RAG chat: hybrid KB retrieval → LLM answer synthesis.

    This is now the sole implementation of /api/chat (no browser path).
    """
    pipeline = get_kb_pipeline()

    # Search for relevant context using hybrid search
    try:
        context_parts = []
        sources = []

        if hasattr(pipeline, 'hybrid_retriever') and pipeline.hybrid_retriever:
            results = pipeline.hybrid_retriever.search(
                query=request.query,
                kb_ids=[request.kb_id],
                top_k=request.top_k,
                search_type="hybrid"
            )

            for i, r in enumerate(results, 1):
                source_ref = f"[{i}]"
                context_parts.append(f"{source_ref} {r.chunk.text}")

                sources.append(ChatSource(
                    ref=source_ref,
                    document_id=r.chunk.document_id,
                    document_title=r.document_title,
                    chapter_title=r.chapter_title,
                    page_number=r.chunk.page_number,
                    text_preview=r.chunk.text[:200] + "..." if len(r.chunk.text) > 200 else r.chunk.text,
                    score=r.score
                ))
        else:
            # Fallback to basic vector search
            vector_store, embedder = get_vector_store_and_embedder()
            query_embedding = embedder.embed(request.query)
            raw_results = vector_store.search(request.kb_id, query_embedding, top_k=request.top_k)

            for i, r in enumerate(raw_results, 1):
                source_ref = f"[{i}]"
                context_parts.append(f"{source_ref} {r['text']}")

                sources.append(ChatSource(
                    ref=source_ref,
                    document_id=r["document_id"],
                    document_title=r.get("document_title", "Unknown"),
                    chapter_title=r.get("chapter_title"),
                    page_number=r.get("page_number"),
                    text_preview=r["text"][:200] + "..." if len(r["text"]) > 200 else r["text"],
                    score=1.0 / (1.0 + r.get("score", 0))
                ))

        context = "\n\n".join(context_parts)

    except Exception as e:
        logger.error(f"Context retrieval failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve context: {e}")

    # Build system prompt establishing expertise
    system_prompt = f"""You are an expert assistant specializing in "{kb.name}".
{f'Description: {kb.description}' if kb.description else ''}

Your role:
- Answer questions accurately based on the provided context from the knowledge base
- When citing information, reference the source numbers in square brackets (e.g., [1], [2])
- If the context doesn't contain enough information to fully answer the question, acknowledge this and provide what you can
- Be conversational but informative
- Stay focused on the topic of this knowledge base

Context from the knowledge base:
{context if context else "No relevant context found."}

Remember: Base your answers primarily on the provided context. If you need to add general knowledge, clearly distinguish it from the sourced information."""

    # Build messages array for OpenAI
    messages = [{"role": "system", "content": system_prompt}]

    # Add conversation history
    for msg in request.conversation_history:
        messages.append({"role": msg.role, "content": msg.content})

    # Add current query
    messages.append({"role": "user", "content": request.query})

    # Call OpenAI
    try:
        client = get_openai_chat_client()
        response = client.chat.completions.create(
            model=settings.extraction_model,
            messages=messages,
            temperature=0.7,
            max_completion_tokens=2048
        )

        answer = response.choices[0].message.content

    except Exception as e:
        logger.error(f"OpenAI API call failed: {e}")
        raise HTTPException(status_code=500, detail=f"LLM call failed: {e}")

    return ChatResponse(
        answer=answer,
        sources=sources,
        kb_name=kb.name,
    )


# =========================================================================
# Browser Session / Web Extraction Endpoints  [DEPRECATED]
# =========================================================================
# These endpoints have been removed.
# Browser traversal and extraction-agent orchestration are now handled
# by the noetix-ui layer via Codex + Playwright MCP.
# All endpoints below return HTTP 410 Gone.
# =========================================================================

_BROWSER_DEPRECATED_MSG = (
    "Browser automation endpoints have been removed. "
    "This functionality is now handled via Playwright MCP."
)


@app.post("/api/browser/start")
async def start_browser_session():
    raise HTTPException(status_code=410, detail=_BROWSER_DEPRECATED_MSG)


@app.post("/api/browser/stop")
async def stop_browser_session():
    raise HTTPException(status_code=410, detail=_BROWSER_DEPRECATED_MSG)


@app.get("/api/browser/status")
async def get_browser_status():
    raise HTTPException(status_code=410, detail=_BROWSER_DEPRECATED_MSG)


@app.post("/api/browser/agent-mode")
async def enter_agent_mode():
    raise HTTPException(status_code=410, detail=_BROWSER_DEPRECATED_MSG)


@app.post("/api/browser/extract")
async def extract_content():
    raise HTTPException(status_code=410, detail=_BROWSER_DEPRECATED_MSG)


@app.post("/api/browser/extract-videos")
async def extract_videos():
    raise HTTPException(status_code=410, detail=_BROWSER_DEPRECATED_MSG)


@app.post("/api/browser/process-videos")
async def process_extracted_videos():
    raise HTTPException(status_code=410, detail=_BROWSER_DEPRECATED_MSG)


@app.get("/api/browser/downloaded-videos")
async def list_browser_downloaded_videos():
    raise HTTPException(status_code=410, detail=_BROWSER_DEPRECATED_MSG)


@app.post("/api/browser/chat")
async def extraction_chat():
    raise HTTPException(status_code=410, detail=_BROWSER_DEPRECATED_MSG)


@app.post("/api/browser/process-extraction")
async def process_extraction():
    raise HTTPException(status_code=410, detail=_BROWSER_DEPRECATED_MSG)


# Serve the SPA
@app.get("/", response_class=HTMLResponse)
async def serve_spa():
    """Serve the main SPA."""
    index_path = Path("static/index.html")
    if index_path.exists():
        async with aiofiles.open(index_path) as f:
            return await f.read()
    return HTMLResponse("<h1>Static files not found. Run from the correct directory.</h1>")


# Mount static files
static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
