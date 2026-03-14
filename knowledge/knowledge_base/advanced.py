"""
Advanced Knowledge Base Operations

Phase 5 features:
- Cross-KB search with unified ranking
- Document similarity detection
- Citation detection
- Relationship inference
- KB export/import
- Usage analytics
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import Document, Entity, SearchResult

logger = logging.getLogger(__name__)


class DocumentSimilarityDetector:
    """
    Detects similar documents across knowledge bases using vector similarity.
    """

    def __init__(self, vector_store, embedder, graph_store=None):
        """
        Initialize the similarity detector.

        Args:
            vector_store: VectorStore instance
            embedder: Embedder instance
            graph_store: Optional GraphStore for creating SIMILAR_TO relationships
        """
        self.vector_store = vector_store
        self.embedder = embedder
        self.graph_store = graph_store

    def find_similar_documents(
        self,
        document_id: str,
        source_kb_id: str,
        target_kb_ids: Optional[list[str]] = None,
        top_k: int = 10,
        similarity_threshold: float = 0.7
    ) -> list[dict]:
        """
        Find documents similar to a given document.

        Uses the document's chunk embeddings to find similar content
        in other documents/KBs.

        Args:
            document_id: Source document ID
            source_kb_id: Source KB ID
            target_kb_ids: KBs to search (None = same KB only)
            top_k: Number of similar documents to return
            similarity_threshold: Minimum similarity score (0-1)

        Returns:
            List of similar documents with scores
        """
        if target_kb_ids is None:
            target_kb_ids = [source_kb_id]

        # Get representative chunks from the source document
        source_chunks = self._get_document_chunks(source_kb_id, document_id)
        if not source_chunks:
            logger.warning(f"No chunks found for document {document_id}")
            return []

        # Use the embeddings from source chunks to find similar content
        similar_docs = {}

        for chunk in source_chunks[:5]:  # Use top 5 chunks as representatives
            embedding = chunk.get("embedding")
            if not embedding:
                continue

            # Search in target KBs
            for kb_id in target_kb_ids:
                results = self.vector_store.search(
                    kb_id, embedding, top_k=20
                )

                for r in results:
                    # Skip chunks from the same document
                    if r["document_id"] == document_id:
                        continue

                    doc_key = (r["kb_id"], r["document_id"])
                    # Convert distance to similarity (0-1 scale)
                    distance = r.get("score", 0)
                    similarity = 1.0 / (1.0 + distance)

                    if similarity < similarity_threshold:
                        continue

                    if doc_key not in similar_docs:
                        similar_docs[doc_key] = {
                            "document_id": r["document_id"],
                            "kb_id": r["kb_id"],
                            "document_title": r.get("document_title", ""),
                            "document_author": r.get("document_author", ""),
                            "similarity_scores": [],
                            "matching_chunks": 0
                        }

                    similar_docs[doc_key]["similarity_scores"].append(similarity)
                    similar_docs[doc_key]["matching_chunks"] += 1

        # Calculate aggregate scores
        results = []
        for doc_key, doc_info in similar_docs.items():
            scores = doc_info["similarity_scores"]
            # Use average of top scores
            top_scores = sorted(scores, reverse=True)[:3]
            avg_score = sum(top_scores) / len(top_scores) if top_scores else 0

            results.append({
                "document_id": doc_info["document_id"],
                "kb_id": doc_info["kb_id"],
                "document_title": doc_info["document_title"],
                "document_author": doc_info["document_author"],
                "similarity_score": avg_score,
                "matching_chunks": doc_info["matching_chunks"],
                "relationship_type": "similar_to"
            })

        # Sort by similarity score
        results.sort(key=lambda x: x["similarity_score"], reverse=True)
        return results[:top_k]

    def _get_document_chunks(self, kb_id: str, document_id: str) -> list[dict]:
        """Get all chunks for a document from the vector store."""
        try:
            table_name = f"kb_{kb_id}"
            if table_name not in self.vector_store.db.table_names():
                return []

            table = self.vector_store.db.open_table(table_name)
            safe_doc_id = document_id.replace("'", "''")
            results = table.search().where(f"document_id = '{safe_doc_id}'").limit(100).to_list()
            return results

        except Exception as e:
            logger.error(f"Failed to get document chunks: {e}")
            return []

    def create_similarity_relationships(
        self,
        document_id: str,
        source_kb_id: str,
        target_kb_ids: Optional[list[str]] = None,
        similarity_threshold: float = 0.8
    ) -> int:
        """
        Find similar documents and create SIMILAR_TO relationships in the graph.

        Args:
            document_id: Source document
            source_kb_id: Source KB
            target_kb_ids: Target KBs to search
            similarity_threshold: Minimum similarity for relationship

        Returns:
            Number of relationships created
        """
        if not self.graph_store:
            logger.warning("Graph store not available for creating relationships")
            return 0

        similar = self.find_similar_documents(
            document_id, source_kb_id, target_kb_ids,
            top_k=10, similarity_threshold=similarity_threshold
        )

        created = 0
        for doc in similar:
            try:
                # Only create relationships within same KB for now
                if doc["kb_id"] != source_kb_id:
                    continue

                with self.graph_store._session() as session:
                    session.run(
                        """
                        MATCH (d1:Document {id: $doc1_id, kb_id: $kb_id})
                        MATCH (d2:Document {id: $doc2_id, kb_id: $kb_id})
                        MERGE (d1)-[r:SIMILAR_TO]-(d2)
                        SET r.score = $score, r.created_at = datetime()
                        """,
                        doc1_id=document_id,
                        doc2_id=doc["document_id"],
                        kb_id=source_kb_id,
                        score=doc["similarity_score"]
                    )
                    created += 1
            except Exception as e:
                logger.debug(f"Failed to create similarity relationship: {e}")

        logger.info(f"Created {created} SIMILAR_TO relationships for document {document_id}")
        return created


class CitationDetector:
    """
    Detects citations and references between documents.
    """

    # Common citation patterns
    CITATION_PATTERNS = [
        # Author (Year) style
        r'\b([A-Z][a-z]+(?:\s+(?:and|&)\s+[A-Z][a-z]+)?)\s*\((\d{4})\)',
        # [Author, Year] style
        r'\[([A-Z][a-z]+(?:\s+(?:et\s+al\.?))?),?\s*(\d{4})\]',
        # [Number] style references
        r'\[(\d{1,3})\]',
        # "According to Author" style
        r'(?:according to|as shown by|as described by)\s+([A-Z][a-z]+(?:\s+(?:and|&)\s+[A-Z][a-z]+)?)',
    ]

    def __init__(self, registry, graph_store=None):
        """
        Initialize the citation detector.

        Args:
            registry: KBRegistry instance
            graph_store: Optional GraphStore for creating CITES relationships
        """
        self.registry = registry
        self.graph_store = graph_store
        self._compiled_patterns = [re.compile(p, re.IGNORECASE) for p in self.CITATION_PATTERNS]

    def detect_citations(self, text: str) -> list[dict]:
        """
        Detect potential citations in text.

        Args:
            text: Text to analyze

        Returns:
            List of detected citations with type and extracted info
        """
        citations = []

        for i, pattern in enumerate(self._compiled_patterns):
            matches = pattern.findall(text)
            for match in matches:
                if isinstance(match, tuple):
                    citation = {
                        "pattern_type": i,
                        "author": match[0] if len(match) > 0 else None,
                        "year": match[1] if len(match) > 1 else None,
                        "raw": match
                    }
                else:
                    citation = {
                        "pattern_type": i,
                        "reference": match,
                        "raw": match
                    }
                citations.append(citation)

        return citations

    def find_cited_documents(
        self,
        document_id: str,
        kb_id: str,
        document_text: str
    ) -> list[dict]:
        """
        Find documents that might be cited by the given document.

        Matches detected citations against document metadata in the KB.

        Args:
            document_id: Source document
            kb_id: Knowledge base ID
            document_text: Full text of the document

        Returns:
            List of potentially cited documents with confidence scores
        """
        citations = self.detect_citations(document_text)
        if not citations:
            return []

        # Get all documents in KB
        documents = self.registry.list_documents(kb_id)

        cited = []
        for doc in documents:
            if doc.id == document_id:
                continue

            score = 0
            matches = []

            for citation in citations:
                # Check if citation matches document author
                author = citation.get("author", "")
                if author and doc.author:
                    author_lower = author.lower().strip()
                    doc_author_lower = doc.author.lower().strip()

                    # Skip if either is empty after stripping
                    if not author_lower or not doc_author_lower:
                        continue

                    # More robust author matching to reduce false positives:
                    # 1. Exact match (highest confidence)
                    # 2. Citation author is substring of doc author (e.g., "Smith" in "John Smith")
                    # 3. Last name match (for "Smith" matching "J. Smith" or "Smith, J.")

                    author_parts = author_lower.split()
                    doc_author_parts = doc_author_lower.split()

                    match_found = False

                    # Exact match
                    if author_lower == doc_author_lower:
                        score += 2
                        match_found = True
                    # Citation author (likely last name) appears as a word in doc author
                    elif len(author_parts) == 1 and author_parts[0] in doc_author_parts:
                        # Single word citation (likely last name) matches a word in doc author
                        score += 1
                        match_found = True
                    # Multi-word citation author matches substantial part of doc author
                    elif len(author_parts) > 1 and len(doc_author_parts) > 0:
                        # Check if last names match (last word of each)
                        if author_parts[-1] == doc_author_parts[-1]:
                            score += 1
                            match_found = True
                        # Or first+last of citation in doc author
                        elif author_parts[0] in doc_author_parts and author_parts[-1] in doc_author_parts:
                            score += 1
                            match_found = True

                    if match_found:
                        matches.append({"type": "author", "citation": citation})

                # Check if citation matches document title
                if doc.title:
                    title_words = set(doc.title.lower().split())
                    # Very basic title matching - could be improved
                    if len(title_words) > 2:
                        text_lower = document_text.lower()
                        if doc.title.lower() in text_lower:
                            score += 2
                            matches.append({"type": "title", "citation": citation})

            if score > 0:
                cited.append({
                    "document_id": doc.id,
                    "document_title": doc.title,
                    "document_author": doc.author,
                    "confidence_score": min(score / 3.0, 1.0),  # Normalize to 0-1
                    "matches": matches,
                    "relationship_type": "cites"
                })

        # Sort by confidence
        cited.sort(key=lambda x: x["confidence_score"], reverse=True)
        return cited

    def create_citation_relationships(
        self,
        document_id: str,
        kb_id: str,
        document_text: str,
        confidence_threshold: float = 0.5
    ) -> int:
        """
        Detect citations and create CITES relationships in the graph.

        Args:
            document_id: Source document
            kb_id: Knowledge base ID
            document_text: Full text of the document
            confidence_threshold: Minimum confidence to create relationship

        Returns:
            Number of relationships created
        """
        if not self.graph_store:
            logger.warning("Graph store not available for creating relationships")
            return 0

        cited = self.find_cited_documents(document_id, kb_id, document_text)

        created = 0
        for doc in cited:
            if doc["confidence_score"] < confidence_threshold:
                continue

            try:
                with self.graph_store._session() as session:
                    session.run(
                        """
                        MATCH (d1:Document {id: $citing_id, kb_id: $kb_id})
                        MATCH (d2:Document {id: $cited_id, kb_id: $kb_id})
                        MERGE (d1)-[r:CITES]->(d2)
                        SET r.confidence = $confidence, r.created_at = datetime()
                        """,
                        citing_id=document_id,
                        cited_id=doc["document_id"],
                        kb_id=kb_id,
                        confidence=doc["confidence_score"]
                    )
                    created += 1
            except Exception as e:
                logger.debug(f"Failed to create citation relationship: {e}")

        logger.info(f"Created {created} CITES relationships for document {document_id}")
        return created


class RelationshipInferencer:
    """
    Infers potential relationships between documents based on various signals.
    """

    def __init__(self, registry, graph_store, vector_store, embedder):
        """
        Initialize the relationship inferencer.

        Args:
            registry: KBRegistry instance
            graph_store: GraphStore instance
            vector_store: VectorStore instance
            embedder: Embedder instance
        """
        self.registry = registry
        self.graph_store = graph_store
        self.vector_store = vector_store
        self.embedder = embedder
        self.similarity_detector = DocumentSimilarityDetector(
            vector_store, embedder, graph_store
        )
        self.citation_detector = CitationDetector(registry, graph_store)

    def infer_relationships(
        self,
        document_id: str,
        kb_id: str,
        document_text: Optional[str] = None
    ) -> list[dict]:
        """
        Infer all potential relationships for a document.

        Combines multiple signals:
        - Shared entities
        - Content similarity
        - Citation patterns

        Args:
            document_id: Document to analyze
            kb_id: Knowledge base ID
            document_text: Optional document text for citation detection

        Returns:
            List of inferred relationships with confidence scores
        """
        relationships = []

        # 1. Find similar documents
        try:
            similar = self.similarity_detector.find_similar_documents(
                document_id, kb_id, [kb_id], top_k=5, similarity_threshold=0.6
            )
            for doc in similar:
                relationships.append({
                    "target_document_id": doc["document_id"],
                    "target_document_title": doc["document_title"],
                    "relationship_type": "similar_to",
                    "confidence": doc["similarity_score"],
                    "reason": f"Content similarity: {doc['matching_chunks']} matching chunks"
                })
        except Exception as e:
            logger.debug(f"Similarity detection failed: {e}")

        # 2. Find shared entity connections
        if self.graph_store:
            try:
                related = self.graph_store.find_related_documents(
                    kb_id, document_id,
                    relationship_types=["shared_entity"],
                    max_hops=1
                )
                for doc in related[:5]:
                    # Check if already in relationships
                    if not any(r["target_document_id"] == doc["id"] for r in relationships):
                        relationships.append({
                            "target_document_id": doc["id"],
                            "target_document_title": doc["title"],
                            "relationship_type": "shared_entity",
                            "confidence": min(doc.get("score", 1) / 10.0, 1.0),
                            "reason": f"Shares {int(doc.get('score', 0))} entities"
                        })
            except Exception as e:
                logger.debug(f"Entity relationship detection failed: {e}")

        # 3. Detect citations
        if document_text:
            try:
                cited = self.citation_detector.find_cited_documents(
                    document_id, kb_id, document_text
                )
                for doc in cited[:5]:
                    # Check if already in relationships
                    existing = next(
                        (r for r in relationships if r["target_document_id"] == doc["document_id"]),
                        None
                    )
                    if existing:
                        # Upgrade relationship type if citation detected
                        existing["relationship_type"] = "cites"
                        existing["confidence"] = max(existing["confidence"], doc["confidence_score"])
                        existing["reason"] += f"; citation detected"
                    else:
                        relationships.append({
                            "target_document_id": doc["document_id"],
                            "target_document_title": doc["document_title"],
                            "relationship_type": "cites",
                            "confidence": doc["confidence_score"],
                            "reason": f"Citation pattern detected"
                        })
            except Exception as e:
                logger.debug(f"Citation detection failed: {e}")

        # Sort by confidence
        relationships.sort(key=lambda x: x["confidence"], reverse=True)
        return relationships


class KBExporter:
    """
    Exports knowledge base data to portable formats.
    """

    def __init__(self, registry, vector_store, graph_store=None):
        """
        Initialize the exporter.

        Args:
            registry: KBRegistry instance
            vector_store: VectorStore instance
            graph_store: Optional GraphStore instance
        """
        self.registry = registry
        self.vector_store = vector_store
        self.graph_store = graph_store

    def export_kb(
        self,
        kb_id: str,
        output_path: Path,
        include_embeddings: bool = False,
        include_graph: bool = True
    ) -> dict:
        """
        Export a knowledge base to a JSON file.

        Args:
            kb_id: Knowledge base ID
            output_path: Output file path
            include_embeddings: Include vector embeddings (large!)
            include_graph: Include graph relationships

        Returns:
            Export summary with counts
        """
        kb = self.registry.get(kb_id)
        if not kb:
            raise ValueError(f"Knowledge base not found: {kb_id}")

        export_data = {
            "format_version": "1.0",
            "exported_at": datetime.now().isoformat(),
            "knowledge_base": kb.model_dump(mode='json'),
            "documents": [],
            "chunks": [],
            "entities": [],
            "relationships": []
        }

        # Export documents
        documents = self.registry.list_documents(kb_id)
        for doc in documents:
            export_data["documents"].append(doc.model_dump(mode='json'))

        # Export chunks from vector store with batching to avoid OOM
        try:
            table_name = f"kb_{kb_id}"
            if table_name in self.vector_store.db.table_names():
                table = self.vector_store.db.open_table(table_name)

                # Use Arrow batching for memory-efficient export
                # to_arrow() returns a pyarrow Table which can be processed in batches
                arrow_table = table.to_arrow()
                total_exported = 0
                batch_size = 1000

                # Process in batches using Arrow's slice method
                total_rows = arrow_table.num_rows
                for start_idx in range(0, total_rows, batch_size):
                    end_idx = min(start_idx + batch_size, total_rows)
                    batch = arrow_table.slice(start_idx, end_idx - start_idx)
                    chunks = batch.to_pylist()

                    for chunk in chunks:
                        chunk_data = {
                            "id": chunk["id"],
                            "kb_id": chunk["kb_id"],
                            "document_id": chunk["document_id"],
                            "chapter_id": chunk.get("chapter_id", ""),
                            "text": chunk["text"],
                            "page_number": chunk.get("page_number"),
                            "position": chunk.get("position", 0),
                            "token_count": chunk.get("token_count", 0),
                        }
                        if include_embeddings:
                            chunk_data["embedding"] = chunk.get("embedding", [])
                        export_data["chunks"].append(chunk_data)

                    total_exported += len(chunks)

                    # Progress logging for large exports
                    if total_exported % 10000 == 0 and total_exported > 0:
                        logger.info(f"Exported {total_exported}/{total_rows} chunks...")

        except Exception as e:
            logger.error(f"Failed to export chunks: {e}")

        # Export graph data
        if include_graph and self.graph_store:
            try:
                # Export entities
                with self.graph_store._session() as session:
                    result = session.run(
                        """
                        MATCH (e:Entity {kb_id: $kb_id})
                        RETURN e.id as id, e.name as name, e.type as type,
                               e.description as description, e.aliases as aliases
                        """,
                        kb_id=kb_id
                    )
                    for record in result:
                        export_data["entities"].append(dict(record))

                    # Export relationships
                    result = session.run(
                        """
                        MATCH (d1:Document {kb_id: $kb_id})-[r]->(d2:Document {kb_id: $kb_id})
                        RETURN d1.id as source_id, type(r) as relationship_type,
                               d2.id as target_id, properties(r) as properties
                        """,
                        kb_id=kb_id
                    )
                    for record in result:
                        export_data["relationships"].append(dict(record))

            except Exception as e:
                logger.error(f"Failed to export graph data: {e}")

        # Write to file
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, 'w') as f:
            json.dump(export_data, f, indent=2, default=str)

        summary = {
            "kb_id": kb_id,
            "output_path": str(output_path),
            "documents": len(export_data["documents"]),
            "chunks": len(export_data["chunks"]),
            "entities": len(export_data["entities"]),
            "relationships": len(export_data["relationships"]),
            "file_size_mb": output_path.stat().st_size / (1024 * 1024)
        }

        logger.info(f"Exported KB {kb_id}: {summary}")
        return summary


class KBImporter:
    """
    Imports knowledge base data from exported files.
    """

    def __init__(self, registry, vector_store, embedder, graph_store=None):
        """
        Initialize the importer.

        Args:
            registry: KBRegistry instance
            vector_store: VectorStore instance
            embedder: Embedder instance (for re-generating embeddings if needed)
            graph_store: Optional GraphStore instance
        """
        self.registry = registry
        self.vector_store = vector_store
        self.embedder = embedder
        self.graph_store = graph_store

    def import_kb(
        self,
        input_path: Path,
        new_kb_name: Optional[str] = None,
        regenerate_embeddings: bool = False
    ) -> dict:
        """
        Import a knowledge base from an exported file.

        Args:
            input_path: Path to exported JSON file
            new_kb_name: Optional new name (creates new KB instead of merging)
            regenerate_embeddings: Re-generate embeddings instead of using exported

        Returns:
            Import summary with counts
        """
        input_path = Path(input_path)
        if not input_path.exists():
            raise ValueError(f"Import file not found: {input_path}")

        with open(input_path, 'r') as f:
            export_data = json.load(f)

        # Validate format
        if export_data.get("format_version") != "1.0":
            raise ValueError(f"Unsupported export format: {export_data.get('format_version')}")

        # Create or get KB
        kb_data = export_data["knowledge_base"]
        if new_kb_name:
            kb = self.registry.create(
                name=new_kb_name,
                description=kb_data.get("description", "")
            )
            kb_id = kb.id
        else:
            kb_id = kb_data["id"]
            if not self.registry.get(kb_id):
                kb = self.registry.create(
                    name=kb_data["name"],
                    description=kb_data.get("description", "")
                )
                kb_id = kb.id

        # Import documents
        from .models import Document, Chapter
        docs_imported = 0
        for doc_data in export_data.get("documents", []):
            try:
                # Create Document model
                chapters = [Chapter(**ch) for ch in doc_data.get("chapters", [])]
                doc = Document(
                    id=doc_data["id"] if not new_kb_name else None,
                    kb_id=kb_id,
                    title=doc_data["title"],
                    author=doc_data.get("author", "Unknown"),
                    source_file=doc_data.get("source_file", ""),
                    total_pages=doc_data.get("total_pages", 0),
                    chapters=chapters
                )
                self.registry.save_document(doc)
                docs_imported += 1
            except Exception as e:
                logger.error(f"Failed to import document: {e}")

        # Import chunks
        chunks_imported = 0
        chunks = export_data.get("chunks", [])
        if chunks:
            # Group chunks by document for batch processing
            chunks_by_doc = {}
            for chunk in chunks:
                doc_id = chunk["document_id"]
                if doc_id not in chunks_by_doc:
                    chunks_by_doc[doc_id] = []
                chunks_by_doc[doc_id].append(chunk)

            for doc_id, doc_chunks in chunks_by_doc.items():
                try:
                    # Get or generate embeddings
                    if regenerate_embeddings or not doc_chunks[0].get("embedding"):
                        texts = [c["text"] for c in doc_chunks]
                        embeddings = self.embedder.embed_batch(texts)
                    else:
                        embeddings = [c.get("embedding", []) for c in doc_chunks]

                    # Add to vector store
                    doc_info = {"id": doc_id, "title": "", "author": ""}
                    self.vector_store.add_chunks(kb_id, doc_chunks, embeddings, doc_info)
                    chunks_imported += len(doc_chunks)

                except Exception as e:
                    logger.error(f"Failed to import chunks for doc {doc_id}: {e}")

        # Import graph data
        entities_imported = 0
        relationships_imported = 0

        if self.graph_store:
            # Import entities
            for entity_data in export_data.get("entities", []):
                try:
                    with self.graph_store._session() as session:
                        session.run(
                            """
                            MERGE (e:Entity {kb_id: $kb_id, name: $name, type: $type})
                            SET e.id = $id, e.description = $description,
                                e.aliases = $aliases
                            """,
                            kb_id=kb_id,
                            id=entity_data.get("id", ""),
                            name=entity_data["name"],
                            type=entity_data["type"],
                            description=entity_data.get("description", ""),
                            aliases=entity_data.get("aliases", [])
                        )
                        entities_imported += 1
                except Exception as e:
                    logger.debug(f"Failed to import entity: {e}")

            # Import relationships
            for rel_data in export_data.get("relationships", []):
                try:
                    with self.graph_store._session() as session:
                        rel_type = rel_data["relationship_type"]
                        props = rel_data.get("properties", {}) or {}
                        # Build SET clause for properties if any exist
                        set_clause = ""
                        if props:
                            prop_assignments = [f"r.{k} = ${k}" for k in props.keys()]
                            set_clause = "SET " + ", ".join(prop_assignments)

                        query = f"""
                            MATCH (d1:Document {{id: $source_id, kb_id: $kb_id}})
                            MATCH (d2:Document {{id: $target_id, kb_id: $kb_id}})
                            MERGE (d1)-[r:{rel_type}]->(d2)
                            {set_clause}
                            """
                        params = {
                            "source_id": rel_data["source_id"],
                            "target_id": rel_data["target_id"],
                            "kb_id": kb_id,
                            **props
                        }
                        session.run(query, **params)
                        relationships_imported += 1
                except Exception as e:
                    logger.debug(f"Failed to import relationship: {e}")

        # Update KB stats
        self.registry.update(kb_id, stats={
            "document_count": docs_imported,
            "chunk_count": chunks_imported,
            "entity_count": entities_imported
        })

        summary = {
            "kb_id": kb_id,
            "documents_imported": docs_imported,
            "chunks_imported": chunks_imported,
            "entities_imported": entities_imported,
            "relationships_imported": relationships_imported
        }

        logger.info(f"Imported KB: {summary}")
        return summary


class UsageAnalytics:
    """
    Tracks and reports usage analytics for knowledge bases.
    """

    def __init__(self, kb_dir: Path):
        """
        Initialize analytics tracker.

        Args:
            kb_dir: Knowledge base directory
        """
        self.kb_dir = Path(kb_dir)
        self.analytics_path = self.kb_dir / "analytics.json"
        self._load()

    def _load(self):
        """Load analytics data from disk."""
        if self.analytics_path.exists():
            try:
                with open(self.analytics_path, 'r') as f:
                    self._data = json.load(f)
            except Exception:
                self._data = self._empty_data()
        else:
            self._data = self._empty_data()

    def _empty_data(self) -> dict:
        """Return empty analytics structure."""
        return {
            "searches": [],
            "document_views": [],
            "kb_stats_snapshots": [],
            "created_at": datetime.now().isoformat()
        }

    def _save(self):
        """Save analytics data to disk."""
        try:
            with open(self.analytics_path, 'w') as f:
                json.dump(self._data, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Failed to save analytics: {e}")

    def log_search(
        self,
        query: str,
        kb_ids: list[str],
        results_count: int,
        search_type: str = "hybrid"
    ):
        """Log a search event."""
        self._data["searches"].append({
            "timestamp": datetime.now().isoformat(),
            "query": query,
            "kb_ids": kb_ids,
            "results_count": results_count,
            "search_type": search_type
        })
        # Keep last 1000 searches
        self._data["searches"] = self._data["searches"][-1000:]
        self._save()

    def log_document_view(self, document_id: str, kb_id: str):
        """Log a document view event."""
        self._data["document_views"].append({
            "timestamp": datetime.now().isoformat(),
            "document_id": document_id,
            "kb_id": kb_id
        })
        # Keep last 1000 views
        self._data["document_views"] = self._data["document_views"][-1000:]
        self._save()

    def snapshot_kb_stats(self, registry):
        """Take a snapshot of all KB statistics."""
        kbs = registry.list()
        snapshot = {
            "timestamp": datetime.now().isoformat(),
            "knowledge_bases": []
        }

        for kb in kbs:
            snapshot["knowledge_bases"].append({
                "id": kb.id,
                "name": kb.name,
                "document_count": kb.stats.document_count,
                "chunk_count": kb.stats.chunk_count,
                "entity_count": kb.stats.entity_count
            })

        self._data["kb_stats_snapshots"].append(snapshot)
        # Keep last 100 snapshots
        self._data["kb_stats_snapshots"] = self._data["kb_stats_snapshots"][-100:]
        self._save()

    def get_search_analytics(self, days: int = 7) -> dict:
        """
        Get search analytics for the specified period.

        Args:
            days: Number of days to analyze

        Returns:
            Analytics summary
        """
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(days=days)

        recent_searches = [
            s for s in self._data["searches"]
            if datetime.fromisoformat(s["timestamp"]) > cutoff
        ]

        # Count queries
        query_counts = {}
        kb_counts = {}
        type_counts = {}

        for search in recent_searches:
            # Query frequency
            query = search["query"].lower().strip()
            query_counts[query] = query_counts.get(query, 0) + 1

            # KB usage
            for kb_id in search.get("kb_ids", []):
                kb_counts[kb_id] = kb_counts.get(kb_id, 0) + 1

            # Search type
            search_type = search.get("search_type", "unknown")
            type_counts[search_type] = type_counts.get(search_type, 0) + 1

        # Sort by frequency
        top_queries = sorted(query_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        top_kbs = sorted(kb_counts.items(), key=lambda x: x[1], reverse=True)

        return {
            "period_days": days,
            "total_searches": len(recent_searches),
            "unique_queries": len(query_counts),
            "top_queries": [{"query": q, "count": c} for q, c in top_queries],
            "kb_usage": [{"kb_id": k, "count": c} for k, c in top_kbs],
            "search_types": type_counts,
            "avg_results": sum(s.get("results_count", 0) for s in recent_searches) / max(len(recent_searches), 1)
        }

    def get_document_analytics(self, days: int = 7) -> dict:
        """
        Get document view analytics.

        Args:
            days: Number of days to analyze

        Returns:
            Analytics summary
        """
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(days=days)

        recent_views = [
            v for v in self._data["document_views"]
            if datetime.fromisoformat(v["timestamp"]) > cutoff
        ]

        # Count views
        doc_counts = {}
        for view in recent_views:
            doc_id = view["document_id"]
            doc_counts[doc_id] = doc_counts.get(doc_id, 0) + 1

        top_docs = sorted(doc_counts.items(), key=lambda x: x[1], reverse=True)[:10]

        return {
            "period_days": days,
            "total_views": len(recent_views),
            "unique_documents": len(doc_counts),
            "top_documents": [{"document_id": d, "views": c} for d, c in top_docs]
        }
