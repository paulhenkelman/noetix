"""
Hybrid Retriever

Combines vector search (LanceDB), keyword search (Neo4j full-text),
and graph-based retrieval for comprehensive RAG.
"""

import hashlib
import logging
from typing import Optional

from .models import SearchResult, Chunk
from .vector_store import VectorStore
from .embedder import Embedder

logger = logging.getLogger(__name__)


class HybridRetriever:
    """
    Hybrid search combining multiple retrieval strategies.

    Strategies:
    - semantic: Vector similarity search (LanceDB)
    - keyword: Full-text search (Neo4j Lucene index)
    - graph: Entity-based traversal (Neo4j)
    - hybrid: Combination with reciprocal rank fusion
    """

    def __init__(
        self,
        vector_store: VectorStore,
        embedder: Embedder,
        graph_store=None,  # Optional - GraphStore
        semantic_weight: float = 0.6,
        keyword_weight: float = 0.4
    ):
        """
        Initialize the hybrid retriever.

        Args:
            vector_store: LanceDB vector store
            embedder: OpenAI embedder for query encoding
            graph_store: Optional Neo4j graph store for keyword search
            semantic_weight: Weight for vector search results (default: 0.6)
            keyword_weight: Weight for keyword search results (default: 0.4)
        """
        self.vector_store = vector_store
        self.embedder = embedder
        self.graph_store = graph_store
        self.semantic_weight = semantic_weight
        self.keyword_weight = keyword_weight

    def search(
        self,
        query: str,
        kb_ids: Optional[list[str]] = None,
        top_k: int = 10,
        search_type: str = "hybrid",
        filter_doc_id: Optional[str] = None,
        filter_chapter_id: Optional[str] = None
    ) -> list[SearchResult]:
        """
        Search across knowledge bases.

        Args:
            query: Search query text
            kb_ids: List of KB IDs to search (None = all)
            top_k: Number of results to return
            search_type: "hybrid", "semantic", or "keyword"
            filter_doc_id: Optional filter to specific document
            filter_chapter_id: Optional filter to specific chapter

        Returns:
            List of SearchResult objects sorted by relevance
        """
        if not query or not query.strip():
            return []

        if not kb_ids:
            logger.warning("No KB IDs provided for search")
            return []

        results = []

        if search_type == "semantic":
            results = self._semantic_search(query, kb_ids, top_k, filter_doc_id, filter_chapter_id)
        elif search_type == "keyword":
            results = self._keyword_search(query, kb_ids, top_k, filter_doc_id, filter_chapter_id)
        elif search_type == "hybrid":
            results = self._hybrid_search(query, kb_ids, top_k, filter_doc_id, filter_chapter_id)
        else:
            logger.warning(f"Unknown search type: {search_type}, falling back to hybrid")
            results = self._hybrid_search(query, kb_ids, top_k, filter_doc_id, filter_chapter_id)

        return results[:top_k]

    def _semantic_search(
        self,
        query: str,
        kb_ids: list[str],
        top_k: int,
        filter_doc_id: Optional[str] = None,
        filter_chapter_id: Optional[str] = None
    ) -> list[SearchResult]:
        """Vector similarity search via LanceDB."""
        try:
            # Generate query embedding
            query_embedding = self.embedder.embed(query)

            # Search across KBs
            if len(kb_ids) == 1:
                raw_results = self.vector_store.search(
                    kb_ids[0],
                    query_embedding,
                    top_k=top_k,
                    filter_doc_id=filter_doc_id,
                    filter_chapter_id=filter_chapter_id
                )
            else:
                raw_results = self.vector_store.search_multiple_kbs(
                    kb_ids,
                    query_embedding,
                    top_k=top_k,
                    filter_doc_id=filter_doc_id,
                    filter_chapter_id=filter_chapter_id
                )

            # Convert to SearchResult objects
            results = []
            for r in raw_results:
                chunk = Chunk(
                    id=r["id"],
                    kb_id=r["kb_id"],
                    document_id=r["document_id"],
                    chapter_id=r.get("chapter_id"),
                    text=r["text"],
                    page_number=r.get("page_number"),
                    position=r.get("position", 0),
                    token_count=r.get("token_count", 0)
                )
                # LanceDB uses distance (lower = better), convert to similarity score
                # Distance is typically L2 or cosine distance
                distance = r.get("score", 0)
                score = 1.0 / (1.0 + distance) if distance >= 0 else 0.0

                result = SearchResult(
                    chunk=chunk,
                    score=score,
                    document_title=r.get("document_title", ""),
                    document_author=r.get("document_author", ""),
                    chapter_title=r.get("chapter_title")
                )
                results.append(result)

            return results

        except Exception as e:
            logger.error(f"Semantic search failed: {e}")
            return []

    def _keyword_search(
        self,
        query: str,
        kb_ids: list[str],
        top_k: int,
        filter_doc_id: Optional[str] = None,
        filter_chapter_id: Optional[str] = None
    ) -> list[SearchResult]:
        """Full-text search via Neo4j."""
        if not self.graph_store:
            logger.warning("Graph store not available for keyword search")
            return []

        results = []

        try:
            for kb_id in kb_ids:
                raw_results = self.graph_store.full_text_search(kb_id, query, top_k)

                for r in raw_results:
                    doc_id = r.get("document_id", "")
                    chapter_id = r.get("chapter_id", "")

                    # Apply document filter if specified
                    if filter_doc_id and doc_id != filter_doc_id:
                        continue

                    # Apply chapter filter if specified
                    if filter_chapter_id and chapter_id != filter_chapter_id:
                        continue

                    # Full-text search returns chunk info from graph
                    chunk = Chunk(
                        id=r.get("chunk_id", ""),
                        kb_id=kb_id,
                        document_id=doc_id,
                        chapter_id=chapter_id,
                        text=r.get("text", ""),
                        page_number=r.get("page_number"),
                        position=r.get("position", 0),
                    )
                    result = SearchResult(
                        chunk=chunk,
                        score=r.get("score", 0),
                        document_title=r.get("document_title", ""),
                        document_author=r.get("document_author", ""),
                        chapter_title=r.get("chapter_title")
                    )
                    results.append(result)

            # Sort by score (higher = better for Lucene)
            results.sort(key=lambda x: x.score, reverse=True)

        except Exception as e:
            logger.error(f"Keyword search failed: {e}")

        return results[:top_k]

    def _hybrid_search(
        self,
        query: str,
        kb_ids: list[str],
        top_k: int,
        filter_doc_id: Optional[str] = None,
        filter_chapter_id: Optional[str] = None
    ) -> list[SearchResult]:
        """
        Combine semantic and keyword search with reciprocal rank fusion.

        Uses RRF to merge rankings from different search methods.
        """
        # Get more results from each method for better fusion
        fetch_k = top_k * 3

        # Semantic search
        semantic_results = self._semantic_search(query, kb_ids, fetch_k, filter_doc_id, filter_chapter_id)

        # Keyword search (if graph store available)
        keyword_results = []
        if self.graph_store:
            keyword_results = self._keyword_search(query, kb_ids, fetch_k, filter_doc_id, filter_chapter_id)

        # If only one method available, return its results
        if not keyword_results:
            return semantic_results[:top_k]
        if not semantic_results:
            return keyword_results[:top_k]

        # Reciprocal Rank Fusion
        rrf_k = 60  # Standard RRF constant

        # Calculate RRF scores
        scores = {}  # key: (document_id, text_hash) -> RRF score

        def get_key(result: SearchResult) -> str:
            """Generate unique key for result deduplication using deterministic hash."""
            text = result.chunk.text or ""
            text_hash = hashlib.md5(text.encode()).hexdigest()[:16]
            chunk_id = result.chunk.id or ""
            return f"{result.chunk.document_id}:{chunk_id}:{text_hash}"

        # Process semantic results
        for rank, result in enumerate(semantic_results, 1):
            key = get_key(result)
            rrf_score = self.semantic_weight / (rrf_k + rank)
            if key in scores:
                scores[key]["score"] += rrf_score
            else:
                scores[key] = {"result": result, "score": rrf_score}

        # Process keyword results
        for rank, result in enumerate(keyword_results, 1):
            key = get_key(result)
            rrf_score = self.keyword_weight / (rrf_k + rank)
            if key in scores:
                scores[key]["score"] += rrf_score
            else:
                scores[key] = {"result": result, "score": rrf_score}

        # Sort by combined score
        fused = sorted(scores.values(), key=lambda x: x["score"], reverse=True)

        # Return top_k with updated scores
        results = []
        for item in fused[:top_k]:
            result = item["result"]
            # Update score to RRF score
            result = SearchResult(
                chunk=result.chunk,
                score=item["score"],
                document_title=result.document_title,
                document_author=result.document_author,
                chapter_title=result.chapter_title
            )
            results.append(result)

        return results

    def search_with_context(
        self,
        query: str,
        kb_ids: Optional[list[str]] = None,
        top_k: int = 5,
        context_chunks: int = 1
    ) -> list[dict]:
        """
        Search and include surrounding context for each result.

        Args:
            query: Search query
            kb_ids: Knowledge bases to search
            top_k: Number of results
            context_chunks: Number of chunks before/after to include

        Returns:
            List of results with context
        """
        results = self.search(query, kb_ids, top_k, search_type="hybrid")

        enriched = []
        for result in results:
            # Get surrounding chunks from same document/chapter
            context = self._get_chunk_context(
                result.chunk.kb_id,
                result.chunk.document_id,
                result.chunk.chapter_id,
                result.chunk.position,
                context_chunks
            )

            enriched.append({
                "chunk": result.chunk.model_dump(),
                "score": result.score,
                "document_title": result.document_title,
                "document_author": result.document_author,
                "chapter_title": result.chapter_title,
                "context_before": context.get("before", []),
                "context_after": context.get("after", [])
            })

        return enriched

    def _get_chunk_context(
        self,
        kb_id: str,
        document_id: str,
        chapter_id: Optional[str],
        position: int,
        context_size: int
    ) -> dict:
        """
        Get chunks before and after the given position.

        Queries LanceDB for chunks in the same document/chapter
        and filters by position to get surrounding context.

        Args:
            kb_id: Knowledge base ID
            document_id: Document ID
            chapter_id: Chapter ID (optional)
            position: Position of the target chunk
            context_size: Number of chunks before/after to retrieve

        Returns:
            Dict with "before" and "after" lists of chunk texts
        """
        if context_size <= 0:
            return {"before": [], "after": []}

        try:
            # Get table for this KB
            table_name = f"kb_{kb_id}"
            if table_name not in self.vector_store.db.table_names():
                return {"before": [], "after": []}

            table = self.vector_store.db.open_table(table_name)

            # Build filter for same document (and optionally chapter)
            safe_doc_id = document_id.replace("'", "''")
            filter_expr = f"document_id = '{safe_doc_id}'"

            if chapter_id:
                safe_chapter_id = chapter_id.replace("'", "''")
                filter_expr += f" AND chapter_id = '{safe_chapter_id}'"

            # Query for chunks in the same document/chapter
            # Get enough chunks to find context (position range)
            results = table.search().where(filter_expr).limit(200).to_list()

            # Sort by position
            sorted_chunks = sorted(results, key=lambda x: x.get("position", 0))

            # Extract context before and after
            before = []
            after = []

            for chunk in sorted_chunks:
                chunk_pos = chunk.get("position", 0)
                if position - context_size <= chunk_pos < position:
                    before.append(chunk["text"])
                elif position < chunk_pos <= position + context_size:
                    after.append(chunk["text"])

            return {"before": before, "after": after}

        except Exception as e:
            logger.debug(f"Failed to get chunk context: {e}")
            return {"before": [], "after": []}

    def find_related_documents(
        self,
        document_id: str,
        kb_id: str,
        max_results: int = 10
    ) -> list[dict]:
        """
        Find documents related to the given document.

        Uses graph-based entity connections.

        Args:
            document_id: Source document
            kb_id: Knowledge base ID
            max_results: Maximum results

        Returns:
            List of related document dicts
        """
        if not self.graph_store:
            return []

        try:
            return self.graph_store.find_related_documents(
                kb_id, document_id, max_hops=2
            )[:max_results]
        except Exception as e:
            logger.error(f"Find related documents failed: {e}")
            return []


def create_hybrid_retriever(
    kb_dir,
    graph_store=None,
    semantic_weight: float = 0.6,
    keyword_weight: float = 0.4
) -> HybridRetriever:
    """
    Factory function to create a HybridRetriever.

    Args:
        kb_dir: Knowledge base directory (contains lancedb/)
        graph_store: Optional GraphStore instance for keyword search
        semantic_weight: Weight for vector results (default: 0.6)
        keyword_weight: Weight for keyword results (default: 0.4)

    Returns:
        Configured HybridRetriever
    """
    from pathlib import Path
    from config.settings import settings

    kb_path = Path(kb_dir)
    lancedb_path = kb_path / "lancedb"

    vector_store = VectorStore(lancedb_path)
    embedder = Embedder()

    return HybridRetriever(
        vector_store=vector_store,
        embedder=embedder,
        graph_store=graph_store,
        semantic_weight=semantic_weight,
        keyword_weight=keyword_weight
    )
