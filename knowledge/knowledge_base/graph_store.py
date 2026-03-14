"""
Neo4j Graph Store

Provides graph storage for knowledge base structure and entities.
"""

import logging
from typing import Optional
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class GraphStore:
    """
    Neo4j-based graph store for knowledge base structure and entities.

    Each knowledge base's data is isolated via kb_id property on all nodes.
    """

    def __init__(self, uri: str, user: str, password: str):
        """
        Initialize connection to Neo4j.

        Args:
            uri: Neo4j bolt URI (e.g., bolt://localhost:7687)
            user: Neo4j username
            password: Neo4j password
        """
        try:
            from neo4j import GraphDatabase
            self.driver = GraphDatabase.driver(uri, auth=(user, password))
            # Verify connection
            self.driver.verify_connectivity()
            logger.info(f"Connected to Neo4j at {uri}")
        except ImportError:
            raise ImportError("neo4j package required. Install with: pip install neo4j")
        except Exception as e:
            logger.error(f"Failed to connect to Neo4j: {e}")
            raise

    def close(self):
        """Close the Neo4j connection."""
        if self.driver:
            self.driver.close()
            logger.info("Closed Neo4j connection")

    @contextmanager
    def _session(self):
        """Context manager for Neo4j sessions."""
        session = self.driver.session()
        try:
            yield session
        finally:
            session.close()

    def ensure_schema(self):
        """
        Create indexes and constraints if they don't exist.

        Creates:
        - Unique constraints on all node types
        - Indexes for common query patterns
        - Full-text indexes for search
        """
        constraints = [
            # Core document/content constraints
            "CREATE CONSTRAINT doc_id IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE",
            "CREATE CONSTRAINT chapter_id IF NOT EXISTS FOR (c:Chapter) REQUIRE c.id IS UNIQUE",
            "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.id IS UNIQUE",
            "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
            # Course hierarchy constraints
            "CREATE CONSTRAINT course_id IF NOT EXISTS FOR (c:Course) REQUIRE c.id IS UNIQUE",
            "CREATE CONSTRAINT module_id IF NOT EXISTS FOR (m:Module) REQUIRE m.id IS UNIQUE",
            "CREATE CONSTRAINT topic_id IF NOT EXISTS FOR (t:Topic) REQUIRE t.id IS UNIQUE",
            "CREATE CONSTRAINT concept_id IF NOT EXISTS FOR (c:Concept) REQUIRE c.id IS UNIQUE",
        ]

        indexes = [
            # Core document indexes
            "CREATE INDEX doc_kb_id IF NOT EXISTS FOR (d:Document) ON (d.kb_id)",
            "CREATE INDEX chapter_kb_id IF NOT EXISTS FOR (c:Chapter) ON (c.kb_id)",
            "CREATE INDEX chunk_kb_id IF NOT EXISTS FOR (c:Chunk) ON (c.kb_id)",
            "CREATE INDEX entity_kb_id IF NOT EXISTS FOR (e:Entity) ON (e.kb_id)",
            "CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)",
            "CREATE INDEX entity_type IF NOT EXISTS FOR (e:Entity) ON (e.type)",
            # Course hierarchy indexes
            "CREATE INDEX course_kb_id IF NOT EXISTS FOR (c:Course) ON (c.kb_id)",
            "CREATE INDEX course_code IF NOT EXISTS FOR (c:Course) ON (c.code)",
            "CREATE INDEX module_course_id IF NOT EXISTS FOR (m:Module) ON (m.course_id)",
            "CREATE INDEX module_order IF NOT EXISTS FOR (m:Module) ON (m.order)",
            "CREATE INDEX topic_module_id IF NOT EXISTS FOR (t:Topic) ON (t.module_id)",
            "CREATE INDEX topic_order IF NOT EXISTS FOR (t:Topic) ON (t.order)",
            "CREATE INDEX concept_topic_id IF NOT EXISTS FOR (c:Concept) ON (c.topic_id)",
        ]

        fulltext_indexes = [
            """CREATE FULLTEXT INDEX chunk_text IF NOT EXISTS
               FOR (c:Chunk) ON EACH [c.text]""",
            """CREATE FULLTEXT INDEX doc_search IF NOT EXISTS
               FOR (d:Document) ON EACH [d.title, d.author]""",
            """CREATE FULLTEXT INDEX entity_search IF NOT EXISTS
               FOR (e:Entity) ON EACH [e.name]""",
            # Course hierarchy full-text search
            """CREATE FULLTEXT INDEX course_search IF NOT EXISTS
               FOR (c:Course) ON EACH [c.title, c.code, c.description]""",
            """CREATE FULLTEXT INDEX concept_search IF NOT EXISTS
               FOR (c:Concept) ON EACH [c.name, c.definition]""",
        ]

        with self._session() as session:
            for constraint in constraints:
                try:
                    session.run(constraint)
                except Exception as e:
                    logger.debug(f"Constraint may already exist: {e}")

            for index in indexes:
                try:
                    session.run(index)
                except Exception as e:
                    logger.debug(f"Index may already exist: {e}")

            for ft_index in fulltext_indexes:
                try:
                    session.run(ft_index)
                except Exception as e:
                    logger.debug(f"Full-text index may already exist: {e}")

        logger.info("Neo4j schema ensured")

    # === Document/Chapter/Chunk Operations ===

    def add_document(self, kb_id: str, document) -> str:
        """
        Add a document node and its chapters to the graph.

        Creates Document node and Chapter nodes with HAS_CHAPTER relationships.

        Args:
            kb_id: Knowledge base ID
            document: Document model with chapters

        Returns:
            Document ID
        """
        with self._session() as session:
            # Create document node
            session.run(
                """
                MERGE (d:Document {id: $id})
                SET d.kb_id = $kb_id,
                    d.title = $title,
                    d.author = $author,
                    d.source_file = $source_file,
                    d.total_pages = $total_pages,
                    d.created_at = datetime($created_at),
                    d.ocr_required = $ocr_required
                """,
                id=document.id,
                kb_id=kb_id,
                title=document.title,
                author=document.author,
                source_file=document.source_file,
                total_pages=document.total_pages,
                created_at=document.created_at.isoformat(),
                ocr_required=document.ocr_required
            )

            # Create chapter nodes and relationships
            for chapter in document.chapters:
                session.run(
                    """
                    MERGE (c:Chapter {id: $id})
                    SET c.kb_id = $kb_id,
                        c.document_id = $document_id,
                        c.number = $number,
                        c.title = $title,
                        c.start_page = $start_page,
                        c.end_page = $end_page
                    WITH c
                    MATCH (d:Document {id: $document_id})
                    MERGE (d)-[:HAS_CHAPTER]->(c)
                    """,
                    id=chapter.id,
                    kb_id=kb_id,
                    document_id=document.id,
                    number=chapter.number,
                    title=chapter.title,
                    start_page=chapter.start_page,
                    end_page=chapter.end_page
                )

        logger.info(f"Added document {document.id} with {len(document.chapters)} chapters to graph")
        return document.id

    def add_chapter(
        self,
        kb_id: str,
        chapter,
        document_id: str
    ) -> str:
        """
        Add a single chapter node and link to document.

        Args:
            kb_id: Knowledge base ID
            chapter: Chapter model
            document_id: Parent document ID

        Returns:
            Chapter ID
        """
        with self._session() as session:
            session.run(
                """
                MERGE (c:Chapter {id: $id})
                SET c.kb_id = $kb_id,
                    c.document_id = $document_id,
                    c.number = $number,
                    c.title = $title,
                    c.start_page = $start_page,
                    c.end_page = $end_page
                WITH c
                MATCH (d:Document {id: $document_id})
                MERGE (d)-[:HAS_CHAPTER]->(c)
                """,
                id=chapter.id,
                kb_id=kb_id,
                document_id=document_id,
                number=chapter.number,
                title=chapter.title,
                start_page=chapter.start_page,
                end_page=chapter.end_page
            )
        logger.debug(f"Added chapter {chapter.id} to document {document_id}")
        return chapter.id

    def add_chunks(
        self,
        kb_id: str,
        chunks: list[dict],
        document_id: str,
        chapter_id: Optional[str] = None
    ) -> int:
        """
        Add chunk nodes and link to chapter/document.

        Creates Chunk nodes with CONTAINS relationships.

        Args:
            kb_id: Knowledge base ID
            chunks: List of chunk dicts with id, text, position, etc.
            document_id: Parent document ID
            chapter_id: Parent chapter ID (optional)

        Returns:
            Number of chunks added
        """
        if not chunks:
            return 0

        with self._session() as session:
            for chunk in chunks:
                # Create chunk node
                session.run(
                    """
                    MERGE (c:Chunk {id: $id})
                    SET c.kb_id = $kb_id,
                        c.document_id = $document_id,
                        c.chapter_id = $chapter_id,
                        c.text = $text,
                        c.page_number = $page_number,
                        c.position = $position,
                        c.token_count = $token_count
                    """,
                    id=chunk.get('id'),
                    kb_id=kb_id,
                    document_id=document_id,
                    chapter_id=chapter_id or "",
                    text=chunk.get('text', ''),
                    page_number=chunk.get('page_number', 0),
                    position=chunk.get('position', 0),
                    token_count=chunk.get('token_count', 0)
                )

                # Link to chapter if provided
                if chapter_id:
                    session.run(
                        """
                        MATCH (ch:Chapter {id: $chapter_id})
                        MATCH (c:Chunk {id: $chunk_id})
                        MERGE (ch)-[:CONTAINS]->(c)
                        """,
                        chapter_id=chapter_id,
                        chunk_id=chunk.get('id')
                    )

        logger.debug(f"Added {len(chunks)} chunks to graph for document {document_id}")
        return len(chunks)

    def add_entities(
        self,
        kb_id: str,
        entities: list,
        document_id: str,
        chapter_id: Optional[str] = None,
        chunk_ids: Optional[list[str]] = None
    ) -> int:
        """
        Add entity nodes and MENTIONS relationships.

        Uses MERGE to avoid duplicate entities (matched by name + type + kb_id).

        Args:
            kb_id: Knowledge base ID
            entities: List of Entity models
            document_id: Document that mentions these entities
            chapter_id: Chapter that mentions these entities (optional)
            chunk_ids: Specific chunks that mention entities (optional)

        Returns:
            Number of entities added/linked
        """
        if not entities:
            return 0

        with self._session() as session:
            for entity in entities:
                # Create or merge entity node (dedupe by name + type within KB)
                session.run(
                    """
                    MERGE (e:Entity {kb_id: $kb_id, name: $name, type: $type})
                    ON CREATE SET e.id = $id,
                                  e.description = $description,
                                  e.aliases = $aliases,
                                  e.first_seen_in = $document_id
                    ON MATCH SET e.aliases = CASE
                        WHEN size($aliases) > size(e.aliases) THEN $aliases
                        ELSE e.aliases
                    END
                    """,
                    id=entity.id,
                    kb_id=kb_id,
                    name=entity.name,
                    type=entity.type,
                    description=entity.description,
                    aliases=entity.aliases,
                    document_id=document_id
                )

                # Create MENTIONS relationship from document
                session.run(
                    """
                    MATCH (d:Document {id: $document_id})
                    MATCH (e:Entity {kb_id: $kb_id, name: $name, type: $type})
                    MERGE (d)-[:MENTIONS]->(e)
                    """,
                    document_id=document_id,
                    kb_id=kb_id,
                    name=entity.name,
                    type=entity.type
                )

                # Create MENTIONS relationship from chapter if provided
                if chapter_id:
                    session.run(
                        """
                        MATCH (c:Chapter {id: $chapter_id})
                        MATCH (e:Entity {kb_id: $kb_id, name: $name, type: $type})
                        MERGE (c)-[:MENTIONS]->(e)
                        """,
                        chapter_id=chapter_id,
                        kb_id=kb_id,
                        name=entity.name,
                        type=entity.type
                    )

        logger.debug(f"Added/linked {len(entities)} entities for document {document_id}")
        return len(entities)

    # === Deletion Operations ===

    def delete_document(self, kb_id: str, document_id: str) -> dict:
        """
        Delete a document and all associated nodes.

        Cascades to: chapters, chunks. Orphaned entities (only referenced
        by this document) are also deleted.

        Args:
            kb_id: Knowledge base ID
            document_id: Document to delete

        Returns:
            Dict with counts: {documents_deleted, chapters_deleted, chunks_deleted, entities_deleted}
        """
        counts = {
            "documents_deleted": 0,
            "chapters_deleted": 0,
            "chunks_deleted": 0,
            "entities_deleted": 0
        }

        with self._session() as session:
            # Count before deletion
            result = session.run(
                """
                MATCH (d:Document {id: $document_id, kb_id: $kb_id})
                OPTIONAL MATCH (d)-[:HAS_CHAPTER]->(ch:Chapter)
                OPTIONAL MATCH (ch)-[:CONTAINS]->(c:Chunk)
                RETURN count(DISTINCT d) as docs,
                       count(DISTINCT ch) as chapters,
                       count(DISTINCT c) as chunks
                """,
                document_id=document_id,
                kb_id=kb_id
            ).single()

            if result:
                counts["documents_deleted"] = result["docs"]
                counts["chapters_deleted"] = result["chapters"]
                counts["chunks_deleted"] = result["chunks"]

            # Count orphaned entities (only mentioned by this document)
            orphan_result = session.run(
                """
                MATCH (d:Document {id: $document_id, kb_id: $kb_id})-[:MENTIONS]->(e:Entity)
                WHERE NOT EXISTS {
                    MATCH (other:Document)-[:MENTIONS]->(e)
                    WHERE other.id <> $document_id
                }
                RETURN count(e) as orphaned_entities
                """,
                document_id=document_id,
                kb_id=kb_id
            ).single()

            if orphan_result:
                counts["entities_deleted"] = orphan_result["orphaned_entities"]

            # Delete orphaned entities first
            session.run(
                """
                MATCH (d:Document {id: $document_id, kb_id: $kb_id})-[:MENTIONS]->(e:Entity)
                WHERE NOT EXISTS {
                    MATCH (other:Document)-[:MENTIONS]->(e)
                    WHERE other.id <> $document_id
                }
                DETACH DELETE e
                """,
                document_id=document_id,
                kb_id=kb_id
            )

            # Delete chunks (both via chapter relationship and by document_id property)
            session.run(
                """
                MATCH (c:Chunk {document_id: $document_id, kb_id: $kb_id})
                DETACH DELETE c
                """,
                document_id=document_id,
                kb_id=kb_id
            )

            # Delete chapters
            session.run(
                """
                MATCH (d:Document {id: $document_id, kb_id: $kb_id})
                MATCH (d)-[:HAS_CHAPTER]->(ch:Chapter)
                DETACH DELETE ch
                """,
                document_id=document_id,
                kb_id=kb_id
            )

            # Delete document (and any remaining relationships)
            session.run(
                """
                MATCH (d:Document {id: $document_id, kb_id: $kb_id})
                DETACH DELETE d
                """,
                document_id=document_id,
                kb_id=kb_id
            )

        logger.info(f"Deleted document {document_id} from graph: {counts}")
        return counts

    def delete_kb(self, kb_id: str) -> dict:
        """
        Delete all nodes for a knowledge base.

        Args:
            kb_id: Knowledge base ID

        Returns:
            Dict with counts of deleted nodes by type
        """
        counts = {"documents": 0, "chapters": 0, "chunks": 0, "entities": 0}

        with self._session() as session:
            # Count before deletion
            for label, key in [("Document", "documents"), ("Chapter", "chapters"),
                               ("Chunk", "chunks"), ("Entity", "entities")]:
                result = session.run(
                    f"MATCH (n:{label} {{kb_id: $kb_id}}) RETURN count(n) as cnt",
                    kb_id=kb_id
                ).single()
                if result:
                    counts[key] = result["cnt"]

            # Delete all nodes with this kb_id
            session.run(
                """
                MATCH (n {kb_id: $kb_id})
                DETACH DELETE n
                """,
                kb_id=kb_id
            )

        logger.info(f"Deleted KB {kb_id} from graph: {counts}")
        return counts

    # === Query Operations ===

    def get_document(self, kb_id: str, document_id: str) -> Optional[dict]:
        """
        Get document with its chapters.

        Returns:
            Document dict with nested chapters list, or None if not found
        """
        with self._session() as session:
            result = session.run(
                """
                MATCH (d:Document {id: $document_id, kb_id: $kb_id})
                OPTIONAL MATCH (d)-[:HAS_CHAPTER]->(ch:Chapter)
                WITH d, ch ORDER BY ch.number
                WITH d, collect(ch {.*}) as chapters
                RETURN d {.*, chapters: chapters}
                """,
                document_id=document_id,
                kb_id=kb_id
            ).single()

            if result:
                return dict(result[0])
            return None

    def get_document_entities(
        self,
        kb_id: str,
        document_id: str,
        entity_type: Optional[str] = None
    ) -> list[dict]:
        """
        Get entities mentioned in a document.

        Args:
            kb_id: Knowledge base ID
            document_id: Document ID
            entity_type: Filter by type (PERSON, ORG, CONCEPT, LOCATION)

        Returns:
            List of entity dicts with mention counts (across all documents)
        """
        type_filter = "AND e.type = $entity_type" if entity_type else ""

        with self._session() as session:
            # Get entities with total mention count across all documents
            result = session.run(
                f"""
                MATCH (d:Document {{id: $document_id, kb_id: $kb_id}})-[:MENTIONS]->(e:Entity)
                WHERE e.kb_id = $kb_id {type_filter}
                WITH e
                OPTIONAL MATCH (any_doc:Document)-[m:MENTIONS]->(e)
                WITH e, count(m) as mention_count
                RETURN e.id as id, e.name as name, e.type as type,
                       e.description as description, e.aliases as aliases,
                       e.first_seen_in as first_seen_in, mention_count
                ORDER BY e.name
                """,
                document_id=document_id,
                kb_id=kb_id,
                entity_type=entity_type
            )
            return [dict(record) for record in result]

    def find_related_documents(
        self,
        kb_id: str,
        document_id: str,
        relationship_types: Optional[list[str]] = None,
        max_hops: int = 2
    ) -> list[dict]:
        """
        Find documents related via graph connections.

        Supports multiple relationship types and multi-hop traversal.

        Args:
            kb_id: Knowledge base ID
            document_id: Source document
            relationship_types: Filter by type - "shared_entity", "cites", "similar_to"
                              If None, searches all relationship types.
            max_hops: Maximum traversal depth (1-3, default 2)

        Returns:
            List of related documents with id, title, author, relationship_type, score
        """
        # Clamp max_hops to reasonable range
        max_hops = max(1, min(max_hops, 3))

        # Default to all relationship types if none specified
        if not relationship_types:
            relationship_types = ["shared_entity", "cites", "similar_to"]

        results = []
        seen_ids = set()

        with self._session() as session:
            # 1. Shared entities (documents mentioning same entities)
            if "shared_entity" in relationship_types:
                if max_hops == 1:
                    # Single hop: direct shared entities only
                    query = """
                        MATCH (d1:Document {id: $document_id, kb_id: $kb_id})-[:MENTIONS]->(e:Entity)<-[:MENTIONS]-(d2:Document)
                        WHERE d2.id <> $document_id AND d2.kb_id = $kb_id
                        WITH d2, count(DISTINCT e) as shared_count
                        RETURN d2.id as id, d2.title as title, d2.author as author,
                               'shared_entity' as relationship_type, toFloat(shared_count) as score,
                               1 as hops
                        ORDER BY shared_count DESC
                        LIMIT 20
                    """
                else:
                    # Multi-hop: find documents connected through chains of entities
                    # 2 hops: d1 -> entity -> d_intermediate -> entity -> d2
                    query = """
                        MATCH (d1:Document {id: $document_id, kb_id: $kb_id})-[:MENTIONS]->(e:Entity)<-[:MENTIONS]-(d2:Document)
                        WHERE d2.id <> $document_id AND d2.kb_id = $kb_id
                        WITH d2, count(DISTINCT e) as shared_count, 1 as hops
                        RETURN d2.id as id, d2.title as title, d2.author as author,
                               'shared_entity' as relationship_type, toFloat(shared_count) as score, hops
                        UNION
                        MATCH (d1:Document {id: $document_id, kb_id: $kb_id})-[:MENTIONS]->(e1:Entity)<-[:MENTIONS]-(d_mid:Document)-[:MENTIONS]->(e2:Entity)<-[:MENTIONS]-(d2:Document)
                        WHERE d2.id <> $document_id AND d2.kb_id = $kb_id AND d_mid.id <> $document_id AND d_mid.id <> d2.id
                        WITH d2, count(DISTINCT e2) as shared_count, 2 as hops
                        RETURN d2.id as id, d2.title as title, d2.author as author,
                               'shared_entity' as relationship_type, toFloat(shared_count) * 0.5 as score, hops
                        ORDER BY score DESC
                        LIMIT 20
                    """

                result = session.run(query, document_id=document_id, kb_id=kb_id)
                for record in result:
                    doc_id = record["id"]
                    if doc_id not in seen_ids:
                        results.append(dict(record))
                        seen_ids.add(doc_id)

            # 2. Direct CITES relationships
            if "cites" in relationship_types:
                # Documents that cite or are cited by this document
                query = """
                    MATCH (d1:Document {id: $document_id, kb_id: $kb_id})-[r:CITES]-(d2:Document)
                    WHERE d2.kb_id = $kb_id
                    RETURN d2.id as id, d2.title as title, d2.author as author,
                           'cites' as relationship_type, 1.0 as score, 1 as hops
                """
                result = session.run(query, document_id=document_id, kb_id=kb_id)
                for record in result:
                    doc_id = record["id"]
                    if doc_id not in seen_ids:
                        results.append(dict(record))
                        seen_ids.add(doc_id)

            # 3. Direct SIMILAR_TO relationships
            if "similar_to" in relationship_types:
                query = """
                    MATCH (d1:Document {id: $document_id, kb_id: $kb_id})-[r:SIMILAR_TO]-(d2:Document)
                    WHERE d2.kb_id = $kb_id
                    RETURN d2.id as id, d2.title as title, d2.author as author,
                           'similar_to' as relationship_type, coalesce(r.score, 0.5) as score, 1 as hops
                """
                result = session.run(query, document_id=document_id, kb_id=kb_id)
                for record in result:
                    doc_id = record["id"]
                    if doc_id not in seen_ids:
                        results.append(dict(record))
                        seen_ids.add(doc_id)

        # Sort by score descending
        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return results[:20]

    def full_text_search(
        self,
        kb_id: str,
        query: str,
        top_k: int = 10
    ) -> list[dict]:
        """
        Search chunks using Neo4j full-text index.

        Uses Lucene-based full-text search for keyword matching.

        Args:
            kb_id: Knowledge base ID
            query: Search query (supports Lucene syntax)
            top_k: Number of results

        Returns:
            List of matching chunks with full metadata
        """
        if not query or not query.strip():
            return []

        # Escape special Lucene characters
        special_chars = ['+', '-', '&', '|', '!', '(', ')', '{', '}', '[', ']', '^', '"', '~', '*', '?', ':', '\\', '/']
        escaped_query = query
        for char in special_chars:
            escaped_query = escaped_query.replace(char, f'\\{char}')

        with self._session() as session:
            result = session.run(
                """
                CALL db.index.fulltext.queryNodes('chunk_text', $query)
                YIELD node, score
                WHERE node.kb_id = $kb_id
                OPTIONAL MATCH (d:Document {id: node.document_id})
                OPTIONAL MATCH (ch:Chapter {id: node.chapter_id})
                RETURN node.id as chunk_id,
                       node.text as text,
                       score,
                       node.document_id as document_id,
                       node.chapter_id as chapter_id,
                       node.page_number as page_number,
                       node.position as position,
                       coalesce(d.title, '') as document_title,
                       coalesce(d.author, '') as document_author,
                       coalesce(ch.title, '') as chapter_title
                ORDER BY score DESC
                LIMIT $top_k
                """,
                query=escaped_query,
                kb_id=kb_id,
                top_k=top_k
            )
            return [dict(record) for record in result]

    def search_entities(
        self,
        kb_id: str,
        query: str,
        entity_type: Optional[str] = None,
        top_k: int = 20
    ) -> list[dict]:
        """
        Search for entities by name.

        Args:
            kb_id: Knowledge base ID
            query: Entity name or partial match
            entity_type: Filter by type
            top_k: Number of results

        Returns:
            List of matching entities
        """
        type_filter = "AND e.type = $entity_type" if entity_type else ""
        escaped_query = query.replace('"', '\\"')

        with self._session() as session:
            result = session.run(
                f"""
                CALL db.index.fulltext.queryNodes('entity_search', $query)
                YIELD node as e, score
                WHERE e.kb_id = $kb_id {type_filter}
                RETURN e {{.*, score: score}}
                ORDER BY score DESC
                LIMIT $top_k
                """,
                query=escaped_query,
                kb_id=kb_id,
                entity_type=entity_type,
                top_k=top_k
            )
            return [dict(record["e"]) for record in result]

    def get_stats(self, kb_id: str) -> dict:
        """
        Get statistics for a knowledge base.

        Returns:
            Dict with documents, chapters, chunks, entities, and relationships counts
        """
        stats = {
            "documents": 0,
            "chapters": 0,
            "chunks": 0,
            "entities": 0,
            "relationships": 0,
            "courses": 0,
            "modules": 0,
            "topics": 0,
            "concepts": 0,
            "exists": False
        }

        with self._session() as session:
            # Count nodes
            for label, key in [("Document", "documents"), ("Chapter", "chapters"),
                               ("Chunk", "chunks"), ("Entity", "entities"),
                               ("Course", "courses"), ("Module", "modules"),
                               ("Topic", "topics"), ("Concept", "concepts")]:
                result = session.run(
                    f"MATCH (n:{label} {{kb_id: $kb_id}}) RETURN count(n) as cnt",
                    kb_id=kb_id
                ).single()
                if result:
                    stats[key] = result["cnt"]

            # Count relationships
            result = session.run(
                """
                MATCH (n {kb_id: $kb_id})-[r]->()
                RETURN count(r) as cnt
                """,
                kb_id=kb_id
            ).single()
            if result:
                stats["relationships"] = result["cnt"]

            stats["exists"] = stats["documents"] > 0 or stats["courses"] > 0

        return stats

    # === Course Hierarchy Operations ===

    def add_course(self, kb_id: str, course) -> str:
        """
        Add a course to the graph.

        Args:
            kb_id: Knowledge base ID
            course: Course model

        Returns:
            Course ID
        """
        with self._session() as session:
            session.run(
                """
                MERGE (c:Course {id: $id})
                SET c.kb_id = $kb_id,
                    c.title = $title,
                    c.code = $code,
                    c.description = $description,
                    c.instructor = $instructor,
                    c.created_at = datetime($created_at)
                """,
                id=course.id,
                kb_id=kb_id,
                title=course.title,
                code=course.code,
                description=course.description,
                instructor=course.instructor,
                created_at=course.created_at.isoformat()
            )
        logger.info(f"Added course '{course.title}' ({course.code}) to graph")
        return course.id

    def add_module(self, course_id: str, module, kb_id: str) -> str:
        """
        Add a module to a course with FOLLOWS relationship to previous module.

        Args:
            course_id: Parent course ID
            module: Module model
            kb_id: Knowledge base ID

        Returns:
            Module ID
        """
        with self._session() as session:
            # Create module node and link to course
            session.run(
                """
                MERGE (m:Module {id: $id})
                SET m.kb_id = $kb_id,
                    m.course_id = $course_id,
                    m.number = $number,
                    m.title = $title,
                    m.description = $description,
                    m.order = $order
                WITH m
                MATCH (c:Course {id: $course_id})
                MERGE (c)-[:HAS_MODULE]->(m)
                """,
                id=module.id,
                kb_id=kb_id,
                course_id=course_id,
                number=module.number,
                title=module.title,
                description=module.description,
                order=module.order
            )

            # Create FOLLOWS relationship to previous module
            if module.order > 0:
                session.run(
                    """
                    MATCH (prev:Module {course_id: $course_id, order: $prev_order})
                    MATCH (curr:Module {id: $id})
                    MERGE (prev)-[:FOLLOWS]->(curr)
                    """,
                    course_id=course_id,
                    prev_order=module.order - 1,
                    id=module.id
                )

        logger.debug(f"Added module {module.number}: '{module.title}' to course {course_id}")
        return module.id

    def add_topic(self, module_id: str, topic, kb_id: str) -> str:
        """
        Add a topic to a module.

        Args:
            module_id: Parent module ID
            topic: Topic model
            kb_id: Knowledge base ID

        Returns:
            Topic ID
        """
        with self._session() as session:
            session.run(
                """
                MERGE (t:Topic {id: $id})
                SET t.kb_id = $kb_id,
                    t.module_id = $module_id,
                    t.title = $title,
                    t.description = $description,
                    t.order = $order
                WITH t
                MATCH (m:Module {id: $module_id})
                MERGE (m)-[:HAS_TOPIC]->(t)
                """,
                id=topic.id,
                kb_id=kb_id,
                module_id=module_id,
                title=topic.title,
                description=topic.description,
                order=topic.order
            )
        logger.debug(f"Added topic '{topic.title}' to module {module_id}")
        return topic.id

    def add_concept(self, topic_id: str, concept, kb_id: str) -> str:
        """
        Add a concept to a topic with links to content chunks.

        Args:
            topic_id: Parent topic ID
            concept: Concept model
            kb_id: Knowledge base ID

        Returns:
            Concept ID
        """
        with self._session() as session:
            # Create concept node and link to topic
            session.run(
                """
                MERGE (c:Concept {id: $id})
                SET c.kb_id = $kb_id,
                    c.topic_id = $topic_id,
                    c.name = $name,
                    c.definition = $definition
                WITH c
                MATCH (t:Topic {id: $topic_id})
                MERGE (t)-[:HAS_CONCEPT]->(c)
                """,
                id=concept.id,
                kb_id=kb_id,
                topic_id=topic_id,
                name=concept.name,
                definition=concept.definition
            )

            # Link concept to content chunks
            if concept.chunk_ids:
                for chunk_id in concept.chunk_ids:
                    session.run(
                        """
                        MATCH (c:Concept {id: $concept_id})
                        MATCH (ch:Chunk {id: $chunk_id})
                        MERGE (c)-[:COVERS]->(ch)
                        """,
                        concept_id=concept.id,
                        chunk_id=chunk_id
                    )

        logger.debug(f"Added concept '{concept.name}' to topic {topic_id}")
        return concept.id

    def add_prerequisite(self, topic_id: str, prerequisite_topic_id: str) -> None:
        """
        Mark a topic as prerequisite of another topic.

        Args:
            topic_id: Topic that requires the prerequisite
            prerequisite_topic_id: The prerequisite topic
        """
        with self._session() as session:
            session.run(
                """
                MATCH (prereq:Topic {id: $prereq_id})
                MATCH (topic:Topic {id: $topic_id})
                MERGE (prereq)-[:PREREQUISITE_OF]->(topic)
                """,
                prereq_id=prerequisite_topic_id,
                topic_id=topic_id
            )
        logger.debug(f"Added prerequisite: {prerequisite_topic_id} -> {topic_id}")

    def add_concept_relationship(
        self,
        concept_id: str,
        related_concept_id: str,
        relationship_type: str = "RELATED_TO"
    ) -> None:
        """
        Create a relationship between two concepts.

        Args:
            concept_id: Source concept ID
            related_concept_id: Target concept ID
            relationship_type: Type of relationship (default: RELATED_TO)
        """
        with self._session() as session:
            session.run(
                f"""
                MATCH (c1:Concept {{id: $id1}})
                MATCH (c2:Concept {{id: $id2}})
                MERGE (c1)-[:{relationship_type}]->(c2)
                """,
                id1=concept_id,
                id2=related_concept_id
            )
        logger.debug(f"Added concept relationship: {concept_id} -[{relationship_type}]-> {related_concept_id}")

    def get_course_structure(self, course_id: str) -> Optional[dict]:
        """
        Get the full hierarchical structure of a course.

        Returns:
            Course dict with nested modules, topics, and concepts
        """
        with self._session() as session:
            result = session.run(
                """
                MATCH (c:Course {id: $course_id})
                OPTIONAL MATCH (c)-[:HAS_MODULE]->(m:Module)
                OPTIONAL MATCH (m)-[:HAS_TOPIC]->(t:Topic)
                OPTIONAL MATCH (t)-[:HAS_CONCEPT]->(con:Concept)
                WITH c, m, t, collect(CASE WHEN con IS NOT NULL THEN con {.*} END) as concepts
                ORDER BY t.order
                WITH c, m, collect(CASE WHEN t IS NOT NULL THEN {topic: t {.*}, concepts: [x IN concepts WHERE x IS NOT NULL]} END) as topics
                ORDER BY m.order
                WITH c, collect(CASE WHEN m IS NOT NULL THEN {module: m {.*}, topics: [x IN topics WHERE x IS NOT NULL]} END) as modules
                RETURN c {.*, modules: [x IN modules WHERE x IS NOT NULL]}
                """,
                course_id=course_id
            ).single()

            if result:
                return dict(result[0])
            return None

    def get_prerequisites(self, topic_id: str) -> list[dict]:
        """
        Get prerequisite topics for a given topic.

        Args:
            topic_id: Topic ID

        Returns:
            List of prerequisite topic dicts
        """
        with self._session() as session:
            result = session.run(
                """
                MATCH (prereq:Topic)-[:PREREQUISITE_OF]->(t:Topic {id: $topic_id})
                OPTIONAL MATCH (prereq)<-[:HAS_TOPIC]-(m:Module)
                RETURN prereq {.*, module_title: m.title, module_number: m.number}
                ORDER BY m.order, prereq.order
                """,
                topic_id=topic_id
            )
            return [dict(record["prereq"]) for record in result]

    def get_learning_path(
        self,
        from_concept: str,
        to_concept: str,
        kb_id: str
    ) -> list[dict]:
        """
        Find optimal learning path between two concepts using graph traversal.

        Uses shortest path through the concept/topic relationship graph.

        Args:
            from_concept: Starting concept name
            to_concept: Target concept name
            kb_id: Knowledge base ID

        Returns:
            List of nodes in the learning path (concepts and topics)
        """
        with self._session() as session:
            # Find shortest path through concepts and topics
            result = session.run(
                """
                MATCH (start:Concept {kb_id: $kb_id})
                WHERE toLower(start.name) CONTAINS toLower($from_concept)
                MATCH (end:Concept {kb_id: $kb_id})
                WHERE toLower(end.name) CONTAINS toLower($to_concept)
                MATCH path = shortestPath((start)-[*..10]-(end))
                RETURN [node in nodes(path) |
                    CASE
                        WHEN 'Concept' IN labels(node) THEN {type: 'concept', id: node.id, name: node.name, definition: node.definition}
                        WHEN 'Topic' IN labels(node) THEN {type: 'topic', id: node.id, title: node.title, description: node.description}
                        WHEN 'Module' IN labels(node) THEN {type: 'module', id: node.id, title: node.title, number: node.number}
                        ELSE {type: 'unknown', id: node.id}
                    END
                ] as path
                LIMIT 1
                """,
                kb_id=kb_id,
                from_concept=from_concept,
                to_concept=to_concept
            ).single()

            if result:
                return result["path"]
            return []

    def infer_relationships(self, kb_id: str) -> dict:
        """
        Auto-infer RELATED_TO and PREREQUISITE_OF relationships from content similarity.

        Analyzes concept co-occurrence in chunks and module ordering to suggest relationships.

        Args:
            kb_id: Knowledge base ID

        Returns:
            Dict with counts: {related_to_created, prerequisite_created}
        """
        counts = {"related_to_created": 0, "prerequisite_created": 0}

        with self._session() as session:
            # Infer RELATED_TO from concepts that appear in same chunks
            result = session.run(
                """
                MATCH (c1:Concept {kb_id: $kb_id})-[:COVERS]->(ch:Chunk)<-[:COVERS]-(c2:Concept {kb_id: $kb_id})
                WHERE c1.id < c2.id AND NOT (c1)-[:RELATED_TO]-(c2)
                MERGE (c1)-[:RELATED_TO]->(c2)
                RETURN count(*) as created
                """,
                kb_id=kb_id
            ).single()
            if result:
                counts["related_to_created"] = result["created"]

            # Infer PREREQUISITE_OF from module/topic ordering
            # Topics in earlier modules are likely prerequisites for later topics
            result = session.run(
                """
                MATCH (t1:Topic {kb_id: $kb_id})<-[:HAS_TOPIC]-(m1:Module)-[:FOLLOWS*]->(m2:Module)-[:HAS_TOPIC]->(t2:Topic {kb_id: $kb_id})
                WHERE NOT (t1)-[:PREREQUISITE_OF]->(t2)
                WITH t1, t2
                LIMIT 100
                MERGE (t1)-[:PREREQUISITE_OF]->(t2)
                RETURN count(*) as created
                """,
                kb_id=kb_id
            ).single()
            if result:
                counts["prerequisite_created"] = result["created"]

        logger.info(f"Inferred relationships for KB {kb_id}: {counts}")
        return counts

    def search_courses(
        self,
        kb_id: str,
        query: str,
        top_k: int = 10
    ) -> list[dict]:
        """
        Search for courses by title, code, or description.

        Args:
            kb_id: Knowledge base ID
            query: Search query
            top_k: Maximum results

        Returns:
            List of matching courses
        """
        escaped_query = query.replace('"', '\\"')

        with self._session() as session:
            result = session.run(
                """
                CALL db.index.fulltext.queryNodes('course_search', $query)
                YIELD node as c, score
                WHERE c.kb_id = $kb_id
                RETURN c {.*, score: score}
                ORDER BY score DESC
                LIMIT $top_k
                """,
                query=escaped_query,
                kb_id=kb_id,
                top_k=top_k
            )
            return [dict(record["c"]) for record in result]

    def search_concepts(
        self,
        kb_id: str,
        query: str,
        top_k: int = 20
    ) -> list[dict]:
        """
        Search for concepts by name or definition.

        Args:
            kb_id: Knowledge base ID
            query: Search query
            top_k: Maximum results

        Returns:
            List of matching concepts with their topic context
        """
        escaped_query = query.replace('"', '\\"')

        with self._session() as session:
            result = session.run(
                """
                CALL db.index.fulltext.queryNodes('concept_search', $query)
                YIELD node as c, score
                WHERE c.kb_id = $kb_id
                OPTIONAL MATCH (c)<-[:HAS_CONCEPT]-(t:Topic)<-[:HAS_TOPIC]-(m:Module)<-[:HAS_MODULE]-(course:Course)
                RETURN c {.*, score: score,
                    topic_title: t.title,
                    module_title: m.title,
                    module_number: m.number,
                    course_title: course.title,
                    course_code: course.code}
                ORDER BY score DESC
                LIMIT $top_k
                """,
                query=escaped_query,
                kb_id=kb_id,
                top_k=top_k
            )
            return [dict(record["c"]) for record in result]

    def delete_course(self, course_id: str) -> dict:
        """
        Delete a course and all its modules, topics, and concepts.

        Args:
            course_id: Course ID to delete

        Returns:
            Dict with counts of deleted nodes
        """
        counts = {"courses": 0, "modules": 0, "topics": 0, "concepts": 0}

        with self._session() as session:
            # Count before deletion
            result = session.run(
                """
                MATCH (c:Course {id: $course_id})
                OPTIONAL MATCH (c)-[:HAS_MODULE]->(m:Module)
                OPTIONAL MATCH (m)-[:HAS_TOPIC]->(t:Topic)
                OPTIONAL MATCH (t)-[:HAS_CONCEPT]->(con:Concept)
                RETURN count(DISTINCT c) as courses,
                       count(DISTINCT m) as modules,
                       count(DISTINCT t) as topics,
                       count(DISTINCT con) as concepts
                """,
                course_id=course_id
            ).single()

            if result:
                counts = {
                    "courses": result["courses"],
                    "modules": result["modules"],
                    "topics": result["topics"],
                    "concepts": result["concepts"]
                }

            # Delete in order: concepts, topics, modules, course
            session.run(
                """
                MATCH (c:Course {id: $course_id})-[:HAS_MODULE]->(m:Module)-[:HAS_TOPIC]->(t:Topic)-[:HAS_CONCEPT]->(con:Concept)
                DETACH DELETE con
                """,
                course_id=course_id
            )
            session.run(
                """
                MATCH (c:Course {id: $course_id})-[:HAS_MODULE]->(m:Module)-[:HAS_TOPIC]->(t:Topic)
                DETACH DELETE t
                """,
                course_id=course_id
            )
            session.run(
                """
                MATCH (c:Course {id: $course_id})-[:HAS_MODULE]->(m:Module)
                DETACH DELETE m
                """,
                course_id=course_id
            )
            session.run(
                """
                MATCH (c:Course {id: $course_id})
                DETACH DELETE c
                """,
                course_id=course_id
            )

        logger.info(f"Deleted course {course_id}: {counts}")
        return counts
