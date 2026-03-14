"""
Knowledge Base Ingestion Pipeline

Orchestrates the process of adding documents to a knowledge base:
1. Extract text from PDF (via pdf_extractor)
2. Chunk text into segments
3. Generate embeddings
4. Store in vector database
5. Store document structure in graph database (Phase 3)
6. Extract and store entities (Phase 3)
"""

import logging
from pathlib import Path
from typing import Optional, Callable, TYPE_CHECKING

from .models import Document, Chapter, Chunk, KnowledgeBase, Entity, generate_id
from .registry import KBRegistry
from .embedder import Embedder
from .chunker import TextChunker
from .vector_store import VectorStore

if TYPE_CHECKING:
    from .graph_store import GraphStore
    from .entity_extractor import EntityExtractor

logger = logging.getLogger(__name__)


class KBIngestionPipeline:
    """
    Pipeline for ingesting documents into a knowledge base.
    """

    def __init__(
        self,
        registry: KBRegistry,
        vector_store: VectorStore,
        embedder: Embedder,
        chunker: Optional[TextChunker] = None,
        graph_store: Optional["GraphStore"] = None,
        entity_extractor: Optional["EntityExtractor"] = None
    ):
        """
        Initialize the ingestion pipeline.

        Args:
            registry: Knowledge base registry
            vector_store: Vector store for embeddings
            embedder: Embedding generator
            chunker: Text chunker (optional, uses defaults if not provided)
            graph_store: Neo4j graph store (optional, Phase 3)
            entity_extractor: Entity extractor (optional, Phase 3)
        """
        self.registry = registry
        self.vector_store = vector_store
        self.embedder = embedder
        self.chunker = chunker or TextChunker()
        self.graph_store = graph_store
        self.entity_extractor = entity_extractor

    def ingest_document(
        self,
        kb_id: str,
        title: str,
        author: str,
        chapters: list[dict],
        source_file: str,
        total_pages: int = 0,
        ocr_required: bool = False,
        progress_callback: Optional[Callable[[int, int, str], None]] = None
    ) -> Document:
        """
        Ingest a document into a knowledge base.

        Args:
            kb_id: Target knowledge base ID
            title: Document title
            author: Document author
            chapters: List of chapter dicts with 'number', 'title', 'text', and optionally 'start_page'
            source_file: Original filename
            total_pages: Total page count
            ocr_required: Whether OCR was needed
            progress_callback: Optional callback(current, total, message)

        Returns:
            Created Document object
        """
        # Verify KB exists
        kb = self.registry.get(kb_id)
        if not kb:
            raise ValueError(f"Knowledge base not found: {kb_id}")

        # Create document record
        doc = Document(
            id=generate_id(),
            kb_id=kb_id,
            title=title,
            author=author,
            source_file=source_file,
            total_pages=total_pages,
            ocr_required=ocr_required,
        )

        # Add document to graph store if available
        if self.graph_store:
            try:
                self.graph_store.add_document(kb_id, doc)
                logger.info(f"Added document '{title}' to graph store")
            except Exception as e:
                logger.error(f"Failed to add document to graph store: {e}")

        total_chunks = 0
        total_chapters = len(chapters)
        total_entities = 0
        all_entities: list[Entity] = []  # Track for deduplication across chapters

        # Process each chapter
        for idx, ch_data in enumerate(chapters):
            if progress_callback:
                progress_callback(idx, total_chapters, f"Processing chapter {idx + 1}: {ch_data.get('title', 'Untitled')}")

            chapter = Chapter(
                id=generate_id(),
                document_id=doc.id,
                number=ch_data.get('number', idx + 1),
                title=ch_data.get('title', f'Chapter {idx + 1}'),
                start_page=ch_data.get('start_page', 0),
                text=ch_data.get('text', ''),
            )
            doc.chapters.append(chapter)

            # Add chapter to graph store if available
            if self.graph_store:
                try:
                    self.graph_store.add_chapter(kb_id, chapter, doc.id)
                except Exception as e:
                    logger.error(f"Failed to add chapter to graph store: {e}")

            # Skip empty chapters
            if not chapter.text.strip():
                logger.warning(f"Skipping empty chapter: {chapter.title}")
                continue

            # Chunk the chapter text
            chunk_data = self.chunker.chunk_by_paragraphs(
                chapter.text,
                metadata={'page_number': chapter.start_page}
            )

            if not chunk_data:
                continue

            # Generate embeddings for chunks
            if progress_callback:
                progress_callback(idx, total_chapters, f"Embedding chapter {idx + 1}...")

            # Defensive filter: remove any empty/whitespace-only texts
            valid_chunks = [c for c in chunk_data if c.get('text', '').strip()]
            if not valid_chunks:
                logger.warning(f"No valid chunks after filtering for chapter: {chapter.title}")
                continue

            texts = [c['text'] for c in valid_chunks]
            embeddings = self.embedder.embed_batch(texts)
            chunk_data = valid_chunks  # Use filtered list for storage

            # Add chunk IDs
            for i, chunk in enumerate(chunk_data):
                chunk['id'] = generate_id()

            # Store in vector database
            self.vector_store.add_chunks(
                kb_id=kb_id,
                chunks=chunk_data,
                embeddings=embeddings,
                document_info={
                    'id': doc.id,
                    'title': doc.title,
                    'author': doc.author,
                },
                chapter_info={
                    'id': chapter.id,
                    'title': chapter.title,
                }
            )

            # Store chunks in graph database if available
            chunk_ids = [c['id'] for c in chunk_data]
            if self.graph_store:
                try:
                    self.graph_store.add_chunks(
                        kb_id=kb_id,
                        chunks=chunk_data,
                        document_id=doc.id,
                        chapter_id=chapter.id
                    )
                except Exception as e:
                    logger.error(f"Failed to add chunks to graph store: {e}")

            # Extract and store entities if extractor available
            if self.entity_extractor and self.graph_store:
                try:
                    if progress_callback:
                        progress_callback(idx, total_chapters, f"Extracting entities from chapter {idx + 1}...")

                    entities = self.entity_extractor.extract(
                        text=chapter.text,
                        existing_entities=all_entities,
                        kb_id=kb_id
                    )

                    if entities:
                        # Set first_seen_in for new entities
                        for entity in entities:
                            entity.first_seen_in = doc.id

                        # Add entities to graph with MENTIONS relationships
                        self.graph_store.add_entities(
                            kb_id=kb_id,
                            entities=entities,
                            document_id=doc.id,
                            chapter_id=chapter.id,
                            chunk_ids=chunk_ids
                        )

                        all_entities.extend(entities)
                        total_entities += len(entities)
                        logger.info(f"Extracted {len(entities)} entities from chapter '{chapter.title}'")

                except Exception as e:
                    logger.error(f"Failed to extract/store entities: {e}")

            total_chunks += len(chunk_data)
            logger.info(f"Ingested chapter '{chapter.title}': {len(chunk_data)} chunks")

        # Save document to registry
        self.registry.save_document(doc)

        # Update KB statistics
        self.registry.increment_stats(
            kb_id,
            documents=1,
            chapters=len(doc.chapters),
            chunks=total_chunks,
            entities=total_entities
        )

        if progress_callback:
            progress_callback(total_chapters, total_chapters, "Document ingestion complete")

        entity_msg = f", {total_entities} entities" if total_entities > 0 else ""
        logger.info(f"Ingested document '{title}' into KB '{kb_id}': {len(doc.chapters)} chapters, {total_chunks} chunks{entity_msg}")
        return doc

    def ingest_text(
        self,
        text: str,
        kb_id: str,
        title: str,
        author: str = "Unknown",
        source_file: str = "text_input",
        progress_callback: Optional[Callable[[int, int, str], None]] = None
    ) -> Document:
        """
        Ingest raw text into a knowledge base.

        Convenience method that wraps text into a single chapter and calls ingest_document.

        Args:
            text: The raw text content to ingest
            kb_id: Target knowledge base ID
            title: Document title
            author: Document author (default: "Unknown")
            source_file: Source identifier (default: "text_input")
            progress_callback: Optional callback(current, total, message)

        Returns:
            Created Document object
        """
        if not text or not text.strip():
            raise ValueError("Cannot ingest empty text")

        # Wrap text in a single chapter
        chapters = [{
            'number': 1,
            'title': title,
            'text': text.strip(),
            'start_page': 1
        }]

        return self.ingest_document(
            kb_id=kb_id,
            title=title,
            author=author,
            chapters=chapters,
            source_file=source_file,
            total_pages=1,
            ocr_required=False,
            progress_callback=progress_callback
        )

    def search(
        self,
        query: str,
        kb_ids: Optional[list[str]] = None,
        top_k: int = 10
    ) -> list[dict]:
        """
        Search across knowledge bases.

        Args:
            query: Search query text
            kb_ids: Optional list of KB IDs to search (None = all)
            top_k: Number of results

        Returns:
            List of search results with chunk data and scores
        """
        # Get query embedding
        query_embedding = self.embedder.embed(query)

        # Determine which KBs to search
        if kb_ids is None:
            kb_ids = [kb.id for kb in self.registry.list()]

        if not kb_ids:
            return []

        # Search
        results = self.vector_store.search_multiple_kbs(
            kb_ids=kb_ids,
            query_embedding=query_embedding,
            top_k=top_k
        )

        return results


def create_ingestion_pipeline(
    kb_dir: Path,
    enable_graph: bool = True,
    enable_entities: bool = True,
    registry: KBRegistry = None
) -> KBIngestionPipeline:
    """
    Create a configured ingestion pipeline.

    Args:
        kb_dir: Base directory for knowledge bases
        enable_graph: Whether to enable Neo4j graph storage
        enable_entities: Whether to enable entity extraction (requires graph)
        registry: Optional existing registry to use (for cache consistency)

    Returns:
        Configured KBIngestionPipeline
    """
    from config.settings import settings

    if registry is None:
        registry = KBRegistry(kb_dir)
    vector_store = VectorStore(settings.LANCEDB_PATH)
    embedder = Embedder()
    chunker = TextChunker(
        chunk_size=settings.CHUNK_SIZE,
        overlap=settings.CHUNK_OVERLAP
    )

    # Optional graph store
    graph_store = None
    if enable_graph:
        try:
            from .graph_store import GraphStore
            graph_store = GraphStore(
                uri=settings.NEO4J_URI,
                user=settings.NEO4J_USER,
                password=settings.NEO4J_PASSWORD
            )
            graph_store.ensure_schema()
            logger.info("Graph store initialized successfully")
        except Exception as e:
            logger.warning(f"Could not initialize graph store: {e}")
            graph_store = None

    # Optional entity extractor (requires graph store)
    entity_extractor = None
    if enable_entities and graph_store:
        try:
            from .entity_extractor import EntityExtractor
            entity_extractor = EntityExtractor()
            logger.info("Entity extractor initialized successfully")
        except Exception as e:
            logger.warning(f"Could not initialize entity extractor: {e}")
            entity_extractor = None

    return KBIngestionPipeline(
        registry=registry,
        vector_store=vector_store,
        embedder=embedder,
        chunker=chunker,
        graph_store=graph_store,
        entity_extractor=entity_extractor
    )
