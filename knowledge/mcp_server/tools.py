"""
MCP Tool Implementations

Business logic for all MCP-exposed tools.
"""

import logging
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class KBTools:
    """
    Knowledge Base tools for MCP server.

    Provides all KB operations that can be exposed via MCP.
    """

    def __init__(
        self,
        kb_dir: Path,
        registry,
        vector_store,
        embedder,
        graph_store=None,
        hybrid_retriever=None
    ):
        """
        Initialize KB tools.

        Args:
            kb_dir: Knowledge base directory
            registry: KBRegistry instance
            vector_store: VectorStore instance
            embedder: Embedder instance
            graph_store: Optional GraphStore instance
            hybrid_retriever: Optional HybridRetriever instance
        """
        self.kb_dir = kb_dir
        self.registry = registry
        self.vector_store = vector_store
        self.embedder = embedder
        self.graph_store = graph_store
        self.hybrid_retriever = hybrid_retriever

        # Initialize Phase 5 components (lazy loaded)
        self._similarity_detector = None
        self._citation_detector = None
        self._relationship_inferencer = None
        self._exporter = None
        self._importer = None
        self._analytics = None

    @property
    def similarity_detector(self):
        """Lazy-load document similarity detector."""
        if self._similarity_detector is None:
            from knowledge_base.advanced import DocumentSimilarityDetector
            self._similarity_detector = DocumentSimilarityDetector(
                self.vector_store, self.embedder, self.graph_store
            )
        return self._similarity_detector

    @property
    def citation_detector(self):
        """Lazy-load citation detector."""
        if self._citation_detector is None:
            from knowledge_base.advanced import CitationDetector
            self._citation_detector = CitationDetector(
                self.registry, self.graph_store
            )
        return self._citation_detector

    @property
    def relationship_inferencer(self):
        """Lazy-load relationship inferencer."""
        if self._relationship_inferencer is None and self.graph_store:
            from knowledge_base.advanced import RelationshipInferencer
            self._relationship_inferencer = RelationshipInferencer(
                self.registry, self.graph_store, self.vector_store, self.embedder
            )
        return self._relationship_inferencer

    @property
    def exporter(self):
        """Lazy-load KB exporter."""
        if self._exporter is None:
            from knowledge_base.advanced import KBExporter
            self._exporter = KBExporter(
                self.registry, self.vector_store, self.graph_store
            )
        return self._exporter

    @property
    def importer(self):
        """Lazy-load KB importer."""
        if self._importer is None:
            from knowledge_base.advanced import KBImporter
            self._importer = KBImporter(
                self.registry, self.vector_store, self.embedder, self.graph_store
            )
        return self._importer

    @property
    def analytics(self):
        """Lazy-load usage analytics."""
        if self._analytics is None:
            from knowledge_base.advanced import UsageAnalytics
            self._analytics = UsageAnalytics(self.kb_dir)
        return self._analytics

    def list_knowledge_bases(self) -> list[dict]:
        """
        List all available knowledge bases with statistics.

        Returns:
            List of knowledge bases with id, name, description,
            document_count, chunk_count, and last_updated.
        """
        kbs = self.registry.list()
        results = []

        for kb in kbs:
            # Defensive handling of potentially missing stats
            stats = kb.stats if kb.stats else None
            results.append({
                "id": kb.id,
                "name": kb.name,
                "description": kb.description,
                "document_count": getattr(stats, 'document_count', 0) if stats else 0,
                "chapter_count": getattr(stats, 'chapter_count', 0) if stats else 0,
                "chunk_count": getattr(stats, 'chunk_count', 0) if stats else 0,
                "entity_count": getattr(stats, 'entity_count', 0) if stats else 0,
                "created_at": kb.created_at.isoformat(),
                "updated_at": kb.updated_at.isoformat()
            })

        return results

    def search(
        self,
        query: str,
        kb_ids: Optional[list[str]] = None,
        top_k: int = 10,
        search_type: str = "hybrid"
    ) -> list[dict]:
        """
        Search across knowledge bases using hybrid retrieval.

        Args:
            query: Search query text
            kb_ids: Limit to specific KBs (None = all)
            top_k: Number of results to return
            search_type: Type of search ("hybrid", "semantic", "keyword")

        Returns:
            List of matching chunks with document context and relevance scores.
        """
        if not query or not query.strip():
            return []

        # Default to all KBs if none specified
        if not kb_ids:
            all_kbs = self.registry.list()
            kb_ids = [kb.id for kb in all_kbs]

        if not kb_ids:
            return []

        if self.hybrid_retriever:
            results = self.hybrid_retriever.search(
                query=query,
                kb_ids=kb_ids,
                top_k=top_k,
                search_type=search_type
            )
            formatted_results = []
            for r in results:
                entry = {
                    "chunk_id": r.chunk.id,
                    "text": r.chunk.text,
                    "kb_id": r.chunk.kb_id,
                    "document_id": r.chunk.document_id,
                    "document_title": r.document_title,
                    "document_author": r.document_author,
                    "chapter_id": r.chunk.chapter_id,
                    "chapter_title": r.chapter_title,
                    "page_number": r.chunk.page_number,
                    "score": r.score,
                }
                if r.section_path:
                    entry["section_path"] = r.section_path
                if r.related_concepts:
                    entry["related_concepts"] = r.related_concepts
                formatted_results.append(entry)

            # Log search for analytics
            self.log_search(query, kb_ids, len(formatted_results), search_type)

            return formatted_results

        # Fallback to basic vector search
        try:
            query_embedding = self.embedder.embed(query)
        except Exception as e:
            logger.error(f"Failed to generate query embedding: {e}")
            return []

        results = self.vector_store.search_multiple_kbs(kb_ids, query_embedding, top_k)

        fallback_results = [
            {
                "chunk_id": r["id"],
                "text": r["text"],
                "kb_id": r["kb_id"],
                "document_id": r["document_id"],
                "document_title": r.get("document_title", ""),
                "document_author": r.get("document_author", ""),
                "chapter_id": r.get("chapter_id"),
                "chapter_title": r.get("chapter_title"),
                "page_number": r.get("page_number"),
                "score": 1.0 / (1.0 + max(0, r.get("score", 0)))  # Convert distance to similarity, clamp negative
            }
            for r in results
        ]

        # Log search for analytics
        self.log_search(query, kb_ids, len(fallback_results), search_type)

        return fallback_results

    def get_document(self, doc_id: str, kb_id: Optional[str] = None) -> Optional[dict]:
        """
        Get full document details including all chapters and metadata.

        Args:
            doc_id: Document UUID
            kb_id: Knowledge base ID (optional, searches all if not provided)

        Returns:
            Document with title, author, chapters, entities, and metadata.
        """
        # Try to find document in registry
        if kb_id:
            doc = self.registry.get_document(kb_id, doc_id)
            if doc:
                result = doc.model_dump()
                # Add entities if graph store available
                if self.graph_store:
                    try:
                        entities = self.graph_store.get_document_entities(kb_id, doc_id)
                        result["entities"] = entities
                    except Exception as e:
                        logger.debug(f"Could not get entities for doc {doc_id}: {e}")
                # Log document view for analytics
                self.log_document_view(doc_id, kb_id)
                return result

        # Search all KBs for the document
        for kb in self.registry.list():
            doc = self.registry.get_document(kb.id, doc_id)
            if doc:
                result = doc.model_dump()
                if self.graph_store:
                    try:
                        entities = self.graph_store.get_document_entities(kb.id, doc_id)
                        result["entities"] = entities
                    except Exception as e:
                        logger.debug(f"Could not get entities: {e}")
                # Log document view for analytics
                self.log_document_view(doc_id, kb.id)
                return result

        # Try graph store directly if available
        if self.graph_store:
            for kb in self.registry.list():
                doc = self.graph_store.get_document(kb.id, doc_id)
                if doc:
                    entities = self.graph_store.get_document_entities(kb.id, doc_id)
                    doc["entities"] = entities
                    # Log document view for analytics
                    self.log_document_view(doc_id, kb.id)
                    return doc

        return None

    def get_chunk(
        self,
        chunk_id: str,
        context_chunks: int = 2
    ) -> Optional[dict]:
        """
        Get a specific chunk with surrounding context.

        Args:
            chunk_id: Chunk UUID
            context_chunks: Number of chunks before/after to include

        Returns:
            Chunk with text, metadata, and surrounding chunks.
        """
        if not chunk_id:
            return None

        # Search all KB tables for the chunk by ID
        for kb in self.registry.list():
            try:
                table_name = f"kb_{kb.id}"
                if table_name not in self.vector_store.db.table_names():
                    continue

                table = self.vector_store.db.open_table(table_name)

                # Query for the specific chunk ID
                safe_id = chunk_id.replace("'", "''")
                results = table.search().where(f"id = '{safe_id}'").limit(1).to_list()

                if not results:
                    continue

                r = results[0]
                chunk_data = {
                    "id": r["id"],
                    "kb_id": r["kb_id"],
                    "document_id": r["document_id"],
                    "chapter_id": r.get("chapter_id", ""),
                    "text": r["text"],
                    "page_number": r.get("page_number"),
                    "position": r.get("position", 0),
                    "token_count": r.get("token_count", 0),
                    "document_title": r.get("document_title", ""),
                    "document_author": r.get("document_author", ""),
                    "chapter_title": r.get("chapter_title", ""),
                    "context_before": [],
                    "context_after": []
                }

                # Get surrounding context chunks if requested
                if context_chunks > 0:
                    position = r.get("position", 0)
                    doc_id = r["document_id"]
                    chapter_id = r.get("chapter_id", "")

                    # Build filter for same document/chapter
                    safe_doc_id = doc_id.replace("'", "''")
                    filter_expr = f"document_id = '{safe_doc_id}'"
                    if chapter_id:
                        safe_chapter_id = chapter_id.replace("'", "''")
                        filter_expr += f" AND chapter_id = '{safe_chapter_id}'"

                    # Get all chunks from same doc/chapter
                    context_results = table.search().where(filter_expr).limit(100).to_list()

                    # Sort by position and extract context
                    sorted_chunks = sorted(context_results, key=lambda x: x.get("position", 0))

                    for c in sorted_chunks:
                        c_pos = c.get("position", 0)
                        if position - context_chunks <= c_pos < position:
                            chunk_data["context_before"].append(c["text"])
                        elif position < c_pos <= position + context_chunks:
                            chunk_data["context_after"].append(c["text"])

                return chunk_data

            except Exception as e:
                logger.debug(f"Error searching KB {kb.id} for chunk {chunk_id}: {e}")

        return None

    def find_related(
        self,
        doc_id: str,
        kb_ids: Optional[list[str]] = None,
        relationship_types: Optional[list[str]] = None,
        max_hops: int = 2
    ) -> list[dict]:
        """
        Find documents related via graph connections.

        Args:
            doc_id: Source document UUID
            kb_ids: Limit to specific KBs (can find cross-KB relations)
            relationship_types: Filter by relationship type
            max_hops: Maximum graph traversal depth

        Returns:
            Related documents with relationship paths and scores.
        """
        if not self.graph_store:
            logger.warning("Graph store not available for find_related")
            return []

        # Default to all KBs
        if not kb_ids:
            kb_ids = [kb.id for kb in self.registry.list()]

        results = []
        seen_ids = set()

        for kb_id in kb_ids:
            try:
                related = self.graph_store.find_related_documents(
                    kb_id=kb_id,
                    document_id=doc_id,
                    relationship_types=relationship_types,
                    max_hops=max_hops
                )
                for doc in related:
                    if doc["id"] not in seen_ids:
                        doc["kb_id"] = kb_id
                        results.append(doc)
                        seen_ids.add(doc["id"])
            except Exception as e:
                logger.debug(f"Error finding related in KB {kb_id}: {e}")

        # Sort by score
        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return results

    def get_entities(
        self,
        doc_id: Optional[str] = None,
        kb_ids: Optional[list[str]] = None,
        entity_type: Optional[str] = None,
        top_k: int = 100
    ) -> list[dict]:
        """
        Get entities (people, organizations, concepts) from documents.

        Args:
            doc_id: Limit to specific document
            kb_ids: Limit to specific KBs
            entity_type: Filter by type (PERSON, ORG, CONCEPT, etc.)

        Returns:
            List of entities with names, types, and document references.
        """
        if not self.graph_store:
            logger.warning("Graph store not available for get_entities")
            return []

        # Default to all KBs
        if not kb_ids:
            kb_ids = [kb.id for kb in self.registry.list()]

        results = []
        seen = set()

        for kb_id in kb_ids:
            try:
                if doc_id:
                    entities = self.graph_store.get_document_entities(
                        kb_id, doc_id, entity_type
                    )
                else:
                    entities = self.graph_store.search_entities(
                        kb_id, "*", entity_type, top_k
                    )

                for e in entities:
                    # Include kb_id in key to preserve entities with same name across KBs
                    key = (kb_id, e.get("name", ""), e.get("type", ""))
                    if key not in seen:
                        e["kb_id"] = kb_id
                        results.append(e)
                        seen.add(key)

            except Exception as e:
                logger.debug(f"Error getting entities from KB {kb_id}: {e}")

        return results[:top_k]

    def ask(
        self,
        question: str,
        kb_ids: Optional[list[str]] = None,
        doc_ids: Optional[list[str]] = None,
        include_sources: bool = True,
        top_k: int = 5
    ) -> dict:
        """
        Ask a question using RAG over the knowledge base.

        This performs retrieval and formats context for an LLM.
        The actual LLM call should be done by the calling agent.

        Args:
            question: Natural language question
            kb_ids: Limit context to specific KBs
            doc_ids: Limit context to specific documents
            include_sources: Include source chunks in response

        Returns:
            Dict with retrieved context and source references.
        """
        if not question or not question.strip():
            return {"error": "Empty question", "context": "", "sources": []}

        # Default to all KBs
        if not kb_ids:
            kb_ids = [kb.id for kb in self.registry.list()]

        # Search for relevant chunks
        try:
            results = self.search(
                query=question,
                kb_ids=kb_ids,
                top_k=top_k,
                search_type="hybrid"
            )
        except Exception as e:
            logger.error(f"Search failed in ask(): {e}")
            return {"error": f"Search failed: {e}", "context": "", "sources": []}

        # Filter by doc_ids if specified
        if doc_ids:
            results = [r for r in results if r["document_id"] in doc_ids]

        # Build context string for LLM
        context_parts = []
        sources = []

        for i, r in enumerate(results, 1):
            # Format source reference
            source_ref = f"[{i}]"
            doc_info = f"{r['document_title']}"
            if r.get("chapter_title"):
                doc_info += f" - {r['chapter_title']}"
            if r.get("page_number"):
                doc_info += f" (p. {r['page_number']})"

            context_parts.append(f"{source_ref} {r['text']}")

            if include_sources:
                sources.append({
                    "ref": source_ref,
                    "document_id": r["document_id"],
                    "document_title": r["document_title"],
                    "chapter_title": r.get("chapter_title"),
                    "page_number": r.get("page_number"),
                    "text_preview": r["text"][:200] + "..." if len(r["text"]) > 200 else r["text"],
                    "score": r["score"]
                })

        context = "\n\n".join(context_parts)

        return {
            "question": question,
            "context": context,
            "sources": sources if include_sources else [],
            "kb_ids": kb_ids,
            "chunks_retrieved": len(results)
        }

    def get_kb_stats(self, kb_id: str) -> Optional[dict]:
        """
        Get detailed statistics for a knowledge base.

        Args:
            kb_id: Knowledge base identifier

        Returns:
            Statistics including document count, chunk count,
            entity breakdown, and storage usage.
        """
        kb = self.registry.get(kb_id)
        if not kb:
            return None

        stats = {
            "kb_id": kb_id,
            "name": kb.name,
            "description": kb.description,
            "document_count": kb.stats.document_count,
            "chapter_count": kb.stats.chapter_count,
            "chunk_count": kb.stats.chunk_count,
            "entity_count": kb.stats.entity_count,
            "created_at": kb.created_at.isoformat(),
            "updated_at": kb.updated_at.isoformat(),
        }

        # Add vector store stats
        try:
            vs_stats = self.vector_store.get_stats(kb_id)
            stats["vector_store"] = vs_stats
        except Exception as e:
            logger.debug(f"Could not get vector store stats: {e}")

        # Add graph store stats
        if self.graph_store:
            try:
                gs_stats = self.graph_store.get_stats(kb_id)
                stats["graph_store"] = gs_stats
            except Exception as e:
                logger.debug(f"Could not get graph store stats: {e}")

        return stats

    # =========================================================================
    # Phase 5: Advanced Features
    # =========================================================================

    def find_similar_documents(
        self,
        document_id: str,
        kb_id: str,
        target_kb_ids: Optional[list[str]] = None,
        top_k: int = 10,
        similarity_threshold: float = 0.7
    ) -> list[dict]:
        """
        Find documents similar to a given document.

        Uses vector similarity across document chunks to find
        related content in the same or other knowledge bases.

        Args:
            document_id: Source document ID
            kb_id: Source knowledge base ID
            target_kb_ids: KBs to search (None = same KB only)
            top_k: Maximum number of results
            similarity_threshold: Minimum similarity score (0-1)

        Returns:
            List of similar documents with similarity scores
        """
        try:
            return self.similarity_detector.find_similar_documents(
                document_id=document_id,
                source_kb_id=kb_id,
                target_kb_ids=target_kb_ids,
                top_k=top_k,
                similarity_threshold=similarity_threshold
            )
        except Exception as e:
            logger.error(f"find_similar_documents failed: {e}")
            return []

    def detect_citations(
        self,
        document_id: str,
        kb_id: str
    ) -> list[dict]:
        """
        Detect citations in a document and match to KB documents.

        Analyzes the document text for citation patterns and matches
        them against other documents in the knowledge base.

        Args:
            document_id: Document to analyze
            kb_id: Knowledge base ID

        Returns:
            List of potentially cited documents with confidence scores
        """
        # Get document text from chunks
        try:
            chunks = self._get_document_text(kb_id, document_id)
            if not chunks:
                return []

            document_text = " ".join(chunks)
            return self.citation_detector.find_cited_documents(
                document_id=document_id,
                kb_id=kb_id,
                document_text=document_text
            )
        except Exception as e:
            logger.error(f"detect_citations failed: {e}")
            return []

    def _get_document_text(self, kb_id: str, document_id: str) -> list[str]:
        """Get all text chunks for a document."""
        try:
            table_name = f"kb_{kb_id}"
            if table_name not in self.vector_store.db.table_names():
                return []

            table = self.vector_store.db.open_table(table_name)
            safe_doc_id = document_id.replace("'", "''")
            results = table.search().where(f"document_id = '{safe_doc_id}'").limit(1000).to_list()

            # Sort by position and extract text
            sorted_results = sorted(results, key=lambda x: x.get("position", 0))
            return [r["text"] for r in sorted_results]

        except Exception as e:
            logger.debug(f"Failed to get document text: {e}")
            return []

    def infer_relationships(
        self,
        document_id: str,
        kb_id: str
    ) -> list[dict]:
        """
        Infer potential relationships for a document.

        Combines multiple signals (shared entities, content similarity,
        citations) to suggest document relationships.

        Args:
            document_id: Document to analyze
            kb_id: Knowledge base ID

        Returns:
            List of inferred relationships with types and confidence
        """
        if not self.relationship_inferencer:
            logger.warning("Relationship inference unavailable: graph store not connected")
            return []

        try:
            # Get document text for citation detection
            chunks = self._get_document_text(kb_id, document_id)
            document_text = " ".join(chunks) if chunks else None

            return self.relationship_inferencer.infer_relationships(
                document_id=document_id,
                kb_id=kb_id,
                document_text=document_text
            )
        except Exception as e:
            logger.error(f"infer_relationships failed: {e}")
            return []

    def export_kb(
        self,
        kb_id: str,
        output_path: Optional[str] = None,
        include_embeddings: bool = False
    ) -> dict:
        """
        Export a knowledge base to a JSON file.

        Args:
            kb_id: Knowledge base ID to export
            output_path: Output file path (default: kb_dir/exports/{kb_id}.json)
            include_embeddings: Include vector embeddings (warning: large!)

        Returns:
            Export summary with file path and counts
        """
        if output_path is None:
            exports_dir = self.kb_dir / "exports"
            exports_dir.mkdir(exist_ok=True)
            output_path = exports_dir / f"{kb_id}.json"

        try:
            return self.exporter.export_kb(
                kb_id=kb_id,
                output_path=output_path,
                include_embeddings=include_embeddings,
                include_graph=True
            )
        except Exception as e:
            logger.error(f"export_kb failed: {e}")
            return {"error": str(e)}

    def import_kb(
        self,
        input_path: str,
        new_name: Optional[str] = None
    ) -> dict:
        """
        Import a knowledge base from an exported file.

        Args:
            input_path: Path to the exported JSON file
            new_name: Optional new name for the KB (creates new KB)

        Returns:
            Import summary with counts
        """
        try:
            return self.importer.import_kb(
                input_path=Path(input_path),
                new_kb_name=new_name,
                regenerate_embeddings=False
            )
        except Exception as e:
            logger.error(f"import_kb failed: {e}")
            return {"error": str(e)}

    def get_analytics(
        self,
        analytics_type: str = "search",
        days: int = 7
    ) -> dict:
        """
        Get usage analytics for the knowledge base system.

        Args:
            analytics_type: Type of analytics ("search", "documents", "all")
            days: Number of days to analyze

        Returns:
            Analytics data for the specified type and period
        """
        try:
            if analytics_type == "search":
                return self.analytics.get_search_analytics(days)
            elif analytics_type == "documents":
                return self.analytics.get_document_analytics(days)
            elif analytics_type == "all":
                return {
                    "search": self.analytics.get_search_analytics(days),
                    "documents": self.analytics.get_document_analytics(days)
                }
            else:
                return {"error": f"Unknown analytics type: {analytics_type}"}
        except Exception as e:
            logger.error(f"get_analytics failed: {e}")
            return {"error": str(e)}

    def log_search(
        self,
        query: str,
        kb_ids: list[str],
        results_count: int,
        search_type: str = "hybrid"
    ):
        """Log a search event for analytics."""
        try:
            self.analytics.log_search(query, kb_ids, results_count, search_type)
        except Exception as e:
            logger.debug(f"Failed to log search: {e}")

    def log_document_view(self, document_id: str, kb_id: str):
        """Log a document view for analytics."""
        try:
            self.analytics.log_document_view(document_id, kb_id)
        except Exception as e:
            logger.debug(f"Failed to log document view: {e}")

    # =========================================================================
    # Graph Navigation Tools
    # =========================================================================

    def browse_structure(
        self,
        doc_id: str,
        kb_id: Optional[str] = None,
        max_depth: int = 3
    ) -> dict:
        """
        Return the hierarchical outline of a document.

        Falls back to flat chapter list when graph data is unavailable.
        """
        # Try graph store for hierarchical sections
        if self.graph_store:
            if not kb_id:
                kb_id = self._find_kb_for_doc(doc_id)
            if kb_id:
                try:
                    tree = self.graph_store.get_document_structure(kb_id, doc_id, max_depth)
                    if tree:
                        return {"document_id": doc_id, "kb_id": kb_id, "sections": tree, "source": "graph"}
                except Exception as e:
                    logger.debug(f"Graph structure lookup failed: {e}")

        # Fallback: flat chapter list from registry
        doc = self._find_document(doc_id, kb_id)
        if not doc:
            return {"error": "Document not found"}

        chapters = []
        doc_data = doc.model_dump() if hasattr(doc, 'model_dump') else doc
        for ch in doc_data.get("chapters", []):
            chapters.append({
                "level": 0,
                "type": "chapter",
                "number": ch.get("number", 0),
                "title": ch.get("title", ""),
                "start_page": ch.get("start_page", 0),
                "children": []
            })
        return {"document_id": doc_id, "kb_id": kb_id, "sections": chapters, "source": "registry"}

    def explore_concepts(
        self,
        query: str,
        kb_ids: Optional[list[str]] = None,
        top_k: int = 20,
        include_related: bool = True
    ) -> list[dict]:
        """
        Return concepts and entities related to a query with cross-references.
        """
        if not self.graph_store:
            return []

        if not kb_ids:
            kb_ids = [kb.id for kb in self.registry.list()]

        results = []
        seen = set()

        for kb_id in kb_ids:
            try:
                entities = self.graph_store.search_entities(kb_id, query, top_k=top_k)
                for e in entities:
                    name = e.get("name", "")
                    if name in seen:
                        continue
                    seen.add(name)

                    entry = {
                        "name": name,
                        "type": e.get("type", ""),
                        "description": e.get("description", ""),
                        "kb_id": kb_id,
                        "score": e.get("score", 0),
                    }

                    # Add cross-references
                    if include_related:
                        try:
                            refs = self.graph_store.find_entity_cross_references(
                                kb_id, name, kb_ids=kb_ids
                            )
                            entry["documents"] = refs
                        except Exception:
                            entry["documents"] = []

                    results.append(entry)
            except Exception as e:
                logger.debug(f"Entity search failed for KB {kb_id}: {e}")

        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return results[:top_k]

    def get_section_content_tool(
        self,
        section_id: Optional[str] = None,
        doc_id: Optional[str] = None,
        section_title: Optional[str] = None,
        kb_id: Optional[str] = None,
        include_children: bool = True
    ) -> Optional[dict]:
        """
        Get section metadata and children. Supports lookup by ID or by doc+title.
        """
        if not self.graph_store:
            return {"error": "Graph store not available"}

        if not kb_id and doc_id:
            kb_id = self._find_kb_for_doc(doc_id)

        if not kb_id:
            return {"error": "Could not determine knowledge base"}

        # Resolve section_id from title if needed
        if not section_id and doc_id and section_title:
            section = self.graph_store.get_section_by_title(kb_id, doc_id, section_title)
            if section:
                section_id = section.get("id")
            else:
                return {"error": f"Section '{section_title}' not found in document"}

        if not section_id:
            return {"error": "Must provide section_id or (doc_id + section_title)"}

        return self.graph_store.get_section_content(kb_id, section_id, include_chunks=False)

    def find_cross_references(
        self,
        entity_name: str,
        entity_type: Optional[str] = None,
        kb_ids: Optional[list[str]] = None
    ) -> list[dict]:
        """Find all documents and sections mentioning an entity."""
        if not self.graph_store:
            return []

        if not kb_ids:
            kb_ids = [kb.id for kb in self.registry.list()]

        results = []
        for kb_id in kb_ids:
            try:
                refs = self.graph_store.find_entity_cross_references(
                    kb_id, entity_name, entity_type, kb_ids=[kb_id]
                )
                results.extend(refs)
            except Exception as e:
                logger.debug(f"Cross-reference search failed for KB {kb_id}: {e}")

        return results

    def get_kb_overview(self, kb_id: str) -> Optional[dict]:
        """
        Structural overview of a KB: document list, top entities, hierarchy depth.
        """
        kb = self.registry.get(kb_id)
        if not kb:
            return None

        overview = {
            "kb_id": kb_id,
            "name": kb.name,
            "description": kb.description,
            "stats": kb.stats.model_dump() if kb.stats else {},
            "documents": [],
            "entity_summary": {},
        }

        # Get document list with section info
        docs = self.registry.list_documents(kb_id) if hasattr(self.registry, 'list_documents') else []
        for doc in docs:
            doc_info = {
                "id": doc.id if hasattr(doc, 'id') else doc.get("id", ""),
                "title": doc.title if hasattr(doc, 'title') else doc.get("title", ""),
                "author": doc.author if hasattr(doc, 'author') else doc.get("author", ""),
                "chapters": len(doc.chapters) if hasattr(doc, 'chapters') else 0,
            }

            # Add section depth from graph if available
            if self.graph_store:
                try:
                    tree = self.graph_store.get_document_structure(kb_id, doc_info["id"], max_depth=10)
                    doc_info["section_count"] = self._count_sections(tree)
                    doc_info["max_depth"] = self._max_depth(tree)
                except Exception:
                    pass

            overview["documents"].append(doc_info)

        # Get entity type breakdown from graph
        if self.graph_store:
            try:
                gs_stats = self.graph_store.get_stats(kb_id)
                overview["graph_stats"] = gs_stats
            except Exception:
                pass

        return overview

    def get_learning_path_tool(
        self,
        from_concept: str,
        to_concept: str,
        kb_id: str
    ) -> list[dict]:
        """Find the path between two concepts through prerequisites and topic ordering."""
        if not self.graph_store:
            return []

        try:
            return self.graph_store.get_learning_path(from_concept, to_concept, kb_id)
        except Exception as e:
            logger.error(f"get_learning_path failed: {e}")
            return []

    # =========================================================================
    # Helper methods
    # =========================================================================

    def _find_kb_for_doc(self, doc_id: str) -> Optional[str]:
        """Find which KB contains a given document."""
        for kb in self.registry.list():
            doc = self.registry.get_document(kb.id, doc_id)
            if doc:
                return kb.id
        return None

    def _find_document(self, doc_id: str, kb_id: Optional[str] = None):
        """Find a document by ID, optionally scoped to a KB."""
        if kb_id:
            return self.registry.get_document(kb_id, doc_id)
        for kb in self.registry.list():
            doc = self.registry.get_document(kb.id, doc_id)
            if doc:
                return doc
        return None

    def _count_sections(self, tree: list) -> int:
        """Count total sections in a tree."""
        count = 0
        for node in tree:
            count += 1
            count += self._count_sections(node.get("children", []))
        return count

    def _max_depth(self, tree: list, current: int = 0) -> int:
        """Find maximum nesting depth in a tree."""
        if not tree:
            return current
        return max(
            self._max_depth(node.get("children", []), current + 1)
            for node in tree
        )


def create_kb_tools(kb_dir=None) -> KBTools:
    """
    Factory function to create KBTools with all dependencies.

    Args:
        kb_dir: Knowledge base directory (uses default if not provided)

    Returns:
        Configured KBTools instance
    """
    from pathlib import Path
    from config.settings import settings
    from knowledge_base import KBRegistry, VectorStore, Embedder

    if kb_dir is None:
        kb_dir = settings.KB_DIR

    kb_dir = Path(kb_dir)

    # Initialize core components
    registry = KBRegistry(kb_dir)
    vector_store = VectorStore(kb_dir / "lancedb")
    embedder = Embedder()

    # Try to initialize graph store (only if enabled in config)
    graph_store = None
    if settings.neo4j_enabled:
        try:
            from knowledge_base import GraphStore
            graph_store = GraphStore(
                settings.NEO4J_URI,
                settings.NEO4J_USER,
                settings.NEO4J_PASSWORD
            )
            logger.info("Graph store initialized for MCP tools")
        except Exception as e:
            logger.info(f"Graph store not available: {e}")

    # Create hybrid retriever
    hybrid_retriever = None
    try:
        from knowledge_base import HybridRetriever
        hybrid_retriever = HybridRetriever(
            vector_store=vector_store,
            embedder=embedder,
            graph_store=graph_store
        )
        logger.info("Hybrid retriever initialized for MCP tools")
    except Exception as e:
        logger.warning(f"Could not create hybrid retriever: {e}")

    return KBTools(
        kb_dir=kb_dir,
        registry=registry,
        vector_store=vector_store,
        embedder=embedder,
        graph_store=graph_store,
        hybrid_retriever=hybrid_retriever
    )
