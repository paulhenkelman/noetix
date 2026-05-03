"""
Noetix Knowledge Base Module

Provides hybrid RAG capabilities with vector search (LanceDB) and
graph-based retrieval (Neo4j).
"""

from .models import (
    KnowledgeBase, Document, Chapter, Section, Chunk, SearchResult, Entity,
    ENTITY_TYPES, Course, Module, Topic, Concept, LibraryItemTags
)
from .registry import KBRegistry
from .embedder import Embedder
from .chunker import TextChunker
from .vector_store import VectorStore
from .ingest import KBIngestionPipeline, create_ingestion_pipeline
from .hybrid_retriever import HybridRetriever, create_hybrid_retriever
from .advanced import (
    DocumentSimilarityDetector,
    CitationDetector,
    RelationshipInferencer,
    KBExporter,
    KBImporter,
    UsageAnalytics
)

# Optional imports for graph layer (may not be available)
try:
    from .graph_store import GraphStore
    _graph_available = True
except ImportError:
    GraphStore = None
    _graph_available = False

try:
    from .entity_extractor import EntityExtractor
    _entity_extractor_available = True
except ImportError:
    EntityExtractor = None
    _entity_extractor_available = False

__all__ = [
    # Core Models
    'KnowledgeBase',
    'Document',
    'Chapter',
    'Section',
    'Chunk',
    'SearchResult',
    'Entity',
    'ENTITY_TYPES',
    # Course Hierarchy
    'Course',
    'Module',
    'Topic',
    'Concept',
    'LibraryItemTags',
    # Components
    'KBRegistry',
    'Embedder',
    'TextChunker',
    'VectorStore',
    'KBIngestionPipeline',
    'create_ingestion_pipeline',
    'GraphStore',
    'EntityExtractor',
    'HybridRetriever',
    'create_hybrid_retriever',
]
