"""
LanceDB Vector Store

Provides vector storage and similarity search using LanceDB.
"""

import logging
from pathlib import Path
from typing import Optional
import json

logger = logging.getLogger(__name__)


class VectorStore:
    """
    LanceDB-based vector store for knowledge base chunks.

    Each knowledge base gets its own table for isolation.
    """

    def __init__(self, db_path: Path):
        """
        Initialize the vector store.

        Args:
            db_path: Path to LanceDB database directory
        """
        self.db_path = Path(db_path)
        self.db_path.mkdir(parents=True, exist_ok=True)

        try:
            import lancedb
            self.db = lancedb.connect(str(self.db_path))
            logger.info(f"Connected to LanceDB at {self.db_path}")
        except ImportError:
            raise ImportError("lancedb package required. Install with: pip install lancedb")

    def _table_name(self, kb_id: str) -> str:
        """Generate table name for a knowledge base."""
        return f"kb_{kb_id}"

    def _get_or_create_table(self, kb_id: str, embedding_dim: int = 3072):
        """Get existing table or create new one."""
        import pyarrow as pa

        table_name = self._table_name(kb_id)

        # Try to open existing table first
        try:
            if table_name in self.db.table_names():
                return self.db.open_table(table_name)
        except Exception:
            pass

        # Define schema
        schema = pa.schema([
            pa.field("id", pa.string()),
            pa.field("kb_id", pa.string()),
            pa.field("document_id", pa.string()),
            pa.field("chapter_id", pa.string()),
            pa.field("text", pa.string()),
            pa.field("page_number", pa.int32()),
            pa.field("position", pa.int32()),
            pa.field("token_count", pa.int32()),
            pa.field("document_title", pa.string()),
            pa.field("document_author", pa.string()),
            pa.field("chapter_title", pa.string()),
            pa.field("embedding", pa.list_(pa.float32(), embedding_dim)),
        ])

        # Create table with exist_ok to handle race conditions / stale table_names cache
        try:
            table = self.db.create_table(table_name, schema=schema, exist_ok=True)
            logger.info(f"Created table {table_name} for KB {kb_id}")
            return table
        except Exception as e:
            if "already exists" in str(e).lower():
                logger.info(f"Table {table_name} already exists, opening")
                return self.db.open_table(table_name)
            raise

    def add_chunks(
        self,
        kb_id: str,
        chunks: list[dict],
        embeddings: list[list[float]],
        document_info: dict,
        chapter_info: Optional[dict] = None
    ) -> int:
        """
        Add chunks with embeddings to the vector store.

        Args:
            kb_id: Knowledge base ID
            chunks: List of chunk dicts from TextChunker
            embeddings: Corresponding embedding vectors
            document_info: Document metadata (id, title, author)
            chapter_info: Optional chapter metadata (id, title)

        Returns:
            Number of chunks added
        """
        if len(chunks) != len(embeddings):
            raise ValueError(f"Chunks ({len(chunks)}) and embeddings ({len(embeddings)}) must match")

        if not chunks:
            return 0

        embedding_dim = len(embeddings[0])
        table = self._get_or_create_table(kb_id, embedding_dim)

        # Prepare records
        records = []
        for chunk, embedding in zip(chunks, embeddings):
            # Handle nullable fields - LanceDB/PyArrow needs empty string instead of None for string fields
            chapter_id = chapter_info.get("id", "") if chapter_info else ""
            chapter_title = chapter_info.get("title", "") if chapter_info else ""
            page_number = chunk.get("page_number")
            if page_number is None:
                page_number = 0

            record = {
                "id": chunk.get("id", f"{document_info['id']}_{chunk['position']}"),
                "kb_id": kb_id,
                "document_id": document_info["id"],
                "chapter_id": chapter_id,
                "text": chunk["text"],
                "page_number": page_number,
                "position": chunk["position"],
                "token_count": chunk["token_count"],
                "document_title": document_info.get("title", ""),
                "document_author": document_info.get("author", ""),
                "chapter_title": chapter_title,
                "embedding": embedding,
            }
            records.append(record)

        table.add(records)
        logger.info(f"Added {len(records)} chunks to KB {kb_id}")
        return len(records)

    def search(
        self,
        kb_id: str,
        query_embedding: list[float],
        top_k: int = 10,
        filter_doc_id: Optional[str] = None,
        filter_chapter_id: Optional[str] = None
    ) -> list[dict]:
        """
        Search for similar chunks.

        Args:
            kb_id: Knowledge base ID
            query_embedding: Query vector
            top_k: Number of results to return
            filter_doc_id: Optional document ID filter
            filter_chapter_id: Optional chapter ID filter

        Returns:
            List of result dicts with chunk data and similarity score
        """
        table_name = self._table_name(kb_id)

        if table_name not in self.db.table_names():
            logger.warning(f"Table {table_name} not found")
            return []

        table = self.db.open_table(table_name)

        # Build search query
        query = table.search(query_embedding).limit(top_k)

        # Build filter expression
        filters = []
        if filter_doc_id:
            safe_doc_id = filter_doc_id.replace("'", "''")
            filters.append(f"document_id = '{safe_doc_id}'")
        if filter_chapter_id:
            safe_chapter_id = filter_chapter_id.replace("'", "''")
            filters.append(f"chapter_id = '{safe_chapter_id}'")

        if filters:
            query = query.where(" AND ".join(filters))

        results = query.to_list()

        # Convert to dicts with scores
        output = []
        for r in results:
            output.append({
                "id": r["id"],
                "kb_id": r["kb_id"],
                "document_id": r["document_id"],
                "chapter_id": r["chapter_id"],
                "text": r["text"],
                "page_number": r["page_number"],
                "position": r["position"],
                "token_count": r["token_count"],
                "document_title": r["document_title"],
                "document_author": r["document_author"],
                "chapter_title": r["chapter_title"],
                "score": r.get("_distance", 0),  # LanceDB uses _distance
            })

        return output

    def search_multiple_kbs(
        self,
        kb_ids: list[str],
        query_embedding: list[float],
        top_k: int = 10,
        filter_doc_id: Optional[str] = None,
        filter_chapter_id: Optional[str] = None
    ) -> list[dict]:
        """
        Search across multiple knowledge bases.

        Args:
            kb_ids: List of knowledge base IDs to search
            query_embedding: Query vector
            top_k: Number of results per KB (results are merged and re-ranked)
            filter_doc_id: Optional document ID to filter results
            filter_chapter_id: Optional chapter ID to filter results

        Returns:
            List of result dicts sorted by score
        """
        all_results = []

        for kb_id in kb_ids:
            results = self.search(kb_id, query_embedding, top_k, filter_doc_id, filter_chapter_id)
            all_results.extend(results)

        # Sort by score (lower distance = better match in LanceDB)
        all_results.sort(key=lambda x: x["score"])

        return all_results[:top_k]

    def delete_document(self, kb_id: str, document_id: str) -> int:
        """
        Delete all chunks for a document.

        Args:
            kb_id: Knowledge base ID
            document_id: Document ID to delete

        Returns:
            Number of chunks deleted
        """
        table_name = self._table_name(kb_id)

        if table_name not in self.db.table_names():
            return 0

        table = self.db.open_table(table_name)

        # Count before delete
        before_count = table.count_rows()

        # Delete matching rows (sanitize input)
        safe_doc_id = document_id.replace("'", "''")
        table.delete(f"document_id = '{safe_doc_id}'")

        after_count = table.count_rows()
        deleted = before_count - after_count

        logger.info(f"Deleted {deleted} chunks for document {document_id} from KB {kb_id}")
        return deleted

    def delete_kb(self, kb_id: str) -> bool:
        """
        Delete entire knowledge base table.

        Args:
            kb_id: Knowledge base ID

        Returns:
            True if deleted, False if table didn't exist
        """
        table_name = self._table_name(kb_id)

        if table_name in self.db.table_names():
            self.db.drop_table(table_name)
            logger.info(f"Deleted table {table_name}")
            return True
        return False

    def get_stats(self, kb_id: str) -> dict:
        """
        Get statistics for a knowledge base.

        Args:
            kb_id: Knowledge base ID

        Returns:
            Dict with chunk_count and other stats
        """
        table_name = self._table_name(kb_id)

        if table_name not in self.db.table_names():
            return {"chunk_count": 0, "exists": False}

        table = self.db.open_table(table_name)

        return {
            "chunk_count": table.count_rows(),
            "exists": True,
        }
