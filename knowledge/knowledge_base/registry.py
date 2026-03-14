"""
Knowledge Base Registry

Manages the lifecycle and metadata of knowledge bases.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import KnowledgeBase, KBStats, KBSettings, Document

logger = logging.getLogger(__name__)


class KBRegistry:
    """
    Registry for managing knowledge bases.

    Stores metadata in registry.json and provides CRUD operations.
    """

    def __init__(self, kb_dir: Path):
        """
        Initialize the registry.

        Args:
            kb_dir: Base directory for knowledge bases
        """
        self.kb_dir = Path(kb_dir)
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.kb_dir / "registry.json"
        self._cache: dict[str, KnowledgeBase] = {}
        self._load()

    def _load(self):
        """Load registry from disk."""
        if self.registry_path.exists():
            try:
                with open(self.registry_path, 'r') as f:
                    data = json.load(f)
                self._cache = {
                    kb_id: KnowledgeBase(**kb_data)
                    for kb_id, kb_data in data.items()
                }
                logger.info(f"Loaded {len(self._cache)} knowledge bases from registry")
            except Exception as e:
                logger.error(f"Failed to load registry: {e}")
                self._cache = {}
        else:
            self._cache = {}

    def _save(self):
        """Save registry to disk."""
        data = {
            kb_id: kb.model_dump(mode='json')
            for kb_id, kb in self._cache.items()
        }
        with open(self.registry_path, 'w') as f:
            json.dump(data, f, indent=2, default=str)

    def create(
        self,
        name: str,
        description: str = "",
        settings: Optional[KBSettings] = None
    ) -> KnowledgeBase:
        """
        Create a new knowledge base.

        Args:
            name: Display name for the KB
            description: Optional description
            settings: Optional custom settings

        Returns:
            The created KnowledgeBase
        """
        kb = KnowledgeBase(
            name=name,
            description=description,
            settings=settings or KBSettings()
        )

        self._cache[kb.id] = kb
        self._save()

        logger.info(f"Created knowledge base: {kb.id} ({name})")
        return kb

    def get(self, kb_id: str) -> Optional[KnowledgeBase]:
        """
        Get a knowledge base by ID.

        Args:
            kb_id: Knowledge base ID

        Returns:
            KnowledgeBase or None if not found
        """
        # Reload from disk to ensure we have latest stats
        self._load()
        return self._cache.get(kb_id)

    def reload(self):
        """Reload registry from disk to get latest data."""
        self._load()

    def list(self) -> list[KnowledgeBase]:
        """
        List all knowledge bases.

        Returns:
            List of all knowledge bases
        """
        # Reload from disk to ensure we have latest stats
        self._load()
        return list(self._cache.values())

    def update(self, kb_id: str, **kwargs) -> Optional[KnowledgeBase]:
        """
        Update a knowledge base.

        Args:
            kb_id: Knowledge base ID
            **kwargs: Fields to update (name, description, stats, settings)

        Returns:
            Updated KnowledgeBase or None if not found
        """
        kb = self._cache.get(kb_id)
        if not kb:
            return None

        for key, value in kwargs.items():
            if hasattr(kb, key):
                setattr(kb, key, value)

        kb.updated_at = datetime.now()
        self._cache[kb_id] = kb
        self._save()

        return kb

    def update_stats(self, kb_id: str, stats: KBStats) -> Optional[KnowledgeBase]:
        """
        Update statistics for a knowledge base.

        Args:
            kb_id: Knowledge base ID
            stats: New statistics

        Returns:
            Updated KnowledgeBase or None if not found
        """
        return self.update(kb_id, stats=stats)

    def increment_stats(
        self,
        kb_id: str,
        documents: int = 0,
        chapters: int = 0,
        chunks: int = 0,
        entities: int = 0
    ) -> Optional[KnowledgeBase]:
        """
        Increment statistics counters.

        Args:
            kb_id: Knowledge base ID
            documents: Number of documents to add
            chapters: Number of chapters to add
            chunks: Number of chunks to add
            entities: Number of entities to add

        Returns:
            Updated KnowledgeBase or None if not found
        """
        kb = self._cache.get(kb_id)
        if not kb:
            return None

        kb.stats.document_count += documents
        kb.stats.chapter_count += chapters
        kb.stats.chunk_count += chunks
        kb.stats.entity_count += entities
        kb.updated_at = datetime.now()

        self._save()
        return kb

    def delete(self, kb_id: str) -> bool:
        """
        Delete a knowledge base.

        Note: This removes the registry entry and documents file.
        Vector store and graph data must be cleaned up separately.

        Args:
            kb_id: Knowledge base ID

        Returns:
            True if deleted, False if not found
        """
        if kb_id in self._cache:
            del self._cache[kb_id]
            self._save()

            # Also delete documents file if it exists
            docs_path = self._docs_path(kb_id)
            if docs_path.exists():
                try:
                    docs_path.unlink()
                    logger.debug(f"Deleted documents file for KB {kb_id}")
                except Exception as e:
                    logger.warning(f"Failed to delete documents file for KB {kb_id}: {e}")

            logger.info(f"Deleted knowledge base: {kb_id}")
            return True
        return False

    def exists(self, kb_id: str) -> bool:
        """Check if a knowledge base exists."""
        return kb_id in self._cache

    # Document persistence methods

    def _docs_path(self, kb_id: str) -> Path:
        """Get path to documents file for a KB."""
        return self.kb_dir / f"documents_{kb_id}.json"

    def save_document(self, document: Document) -> None:
        """
        Save a document to persistent storage.

        Args:
            document: Document to save
        """
        docs_path = self._docs_path(document.kb_id)
        docs = self.list_documents(document.kb_id)

        # Update or add document
        doc_ids = [d.id for d in docs]
        if document.id in doc_ids:
            docs = [d if d.id != document.id else document for d in docs]
        else:
            docs.append(document)

        # Save to file
        data = [doc.model_dump(mode='json') for doc in docs]
        with open(docs_path, 'w') as f:
            json.dump(data, f, indent=2, default=str)

        logger.debug(f"Saved document {document.id} to KB {document.kb_id}")

    def list_documents(self, kb_id: str) -> list[Document]:
        """
        List all documents in a knowledge base.

        Args:
            kb_id: Knowledge base ID

        Returns:
            List of Document objects
        """
        docs_path = self._docs_path(kb_id)
        if not docs_path.exists():
            return []

        try:
            with open(docs_path, 'r') as f:
                data = json.load(f)
            return [Document(**doc_data) for doc_data in data]
        except Exception as e:
            logger.error(f"Failed to load documents for KB {kb_id}: {e}")
            return []

    def get_document(self, kb_id: str, doc_id: str) -> Optional[Document]:
        """
        Get a specific document by ID.

        Args:
            kb_id: Knowledge base ID
            doc_id: Document ID

        Returns:
            Document or None if not found
        """
        docs = self.list_documents(kb_id)
        for doc in docs:
            if doc.id == doc_id:
                return doc
        return None

    def delete_document(self, kb_id: str, doc_id: str) -> bool:
        """
        Delete a document from the registry.

        Note: This only removes the registry entry.
        Vector store data must be cleaned up separately.

        Args:
            kb_id: Knowledge base ID
            doc_id: Document ID

        Returns:
            True if deleted, False if not found
        """
        docs = self.list_documents(kb_id)
        original_count = len(docs)
        docs = [d for d in docs if d.id != doc_id]

        if len(docs) < original_count:
            docs_path = self._docs_path(kb_id)
            data = [doc.model_dump(mode='json') for doc in docs]
            with open(docs_path, 'w') as f:
                json.dump(data, f, indent=2, default=str)
            logger.info(f"Deleted document {doc_id} from KB {kb_id}")
            return True
        return False
