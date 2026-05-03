"""
Pydantic models for Noetix Knowledge Base
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field
import uuid


def generate_id() -> str:
    """Generate a unique ID."""
    return str(uuid.uuid4())[:12]


class KBSettings(BaseModel):
    """Settings for a knowledge base."""
    chunk_size: int = 512
    chunk_overlap: int = 50
    embedding_model: str = "openai:text-embedding-3-large"


class KBStats(BaseModel):
    """Statistics for a knowledge base."""
    document_count: int = 0
    chapter_count: int = 0
    chunk_count: int = 0
    entity_count: int = 0  # Phase 3


class KnowledgeBase(BaseModel):
    """Knowledge base metadata."""
    id: str = Field(default_factory=generate_id)
    name: str
    description: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    stats: KBStats = Field(default_factory=KBStats)
    settings: KBSettings = Field(default_factory=KBSettings)


class Chapter(BaseModel):
    """Chapter within a document."""
    id: str = Field(default_factory=generate_id)
    document_id: str
    number: int
    title: str
    start_page: int
    end_page: Optional[int] = None
    text: str = ""


class Section(BaseModel):
    """Generalized structural unit within a document (part, chapter, section, subsection)."""
    id: str = Field(default_factory=generate_id)
    document_id: str
    parent_id: Optional[str] = None  # None = top-level
    level: int = 0                   # 0=part, 1=chapter, 2=section, 3=subsection
    section_type: str = "chapter"    # part, chapter, section, subsection, lesson, unit, module
    number: int = 0
    title: str
    start_page: int = 0
    end_page: Optional[int] = None
    text: str = ""
    order: int = 0                   # position among siblings


class Document(BaseModel):
    """Document in a knowledge base."""
    id: str = Field(default_factory=generate_id)
    kb_id: str
    title: str
    author: str = "Unknown"
    source_file: str
    total_pages: int = 0
    created_at: datetime = Field(default_factory=datetime.now)
    ocr_required: bool = False
    chapters: list[Chapter] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)


class Chunk(BaseModel):
    """Text chunk for vector storage."""
    id: str = Field(default_factory=generate_id)
    kb_id: str
    document_id: str
    chapter_id: Optional[str] = None
    text: str
    page_number: Optional[int] = None
    position: int = 0  # Position within chapter/document
    token_count: int = 0


class ChunkWithEmbedding(Chunk):
    """Chunk with embedding vector (for LanceDB storage)."""
    embedding: list[float] = Field(default_factory=list)


class SearchResult(BaseModel):
    """Result from a search query."""
    chunk: Chunk
    score: float
    document_title: str
    document_author: str
    chapter_title: Optional[str] = None
    section_path: Optional[list[str]] = None     # ["Part I", "Ch 3", "Section 3.2"]
    related_concepts: Optional[list[str]] = None  # entities in this chunk's section


class Entity(BaseModel):
    """Named entity extracted from documents."""
    id: str = Field(default_factory=generate_id)
    kb_id: str
    name: str
    type: str  # CONCEPT, TECHNIQUE, TERM, PERSON, ORGANIZATION, TOOL, WORK
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    first_seen_in: Optional[str] = None  # document_id where first encountered


# =========================================================================
# Educational Content Entity Types
# =========================================================================

ENTITY_TYPES = [
    "CONCEPT",       # Key ideas, theories (e.g., "machine learning", "recursion")
    "TECHNIQUE",     # Methods, algorithms (e.g., "backpropagation", "A* search")
    "TERM",          # Domain vocabulary (e.g., "epoch", "gradient")
    "PERSON",        # People (e.g., "Alan Turing")
    "ORGANIZATION",  # Institutions (e.g., "Georgia Tech")
    "TOOL",          # Software/tools (e.g., "Python", "TensorFlow")
    "WORK",          # Papers, books (e.g., "Attention Is All You Need")
]


# =========================================================================
# Course Hierarchy Models
# =========================================================================

class Course(BaseModel):
    """A course within a knowledge base."""
    id: str = Field(default_factory=generate_id)
    kb_id: str
    title: str
    code: str = ""  # e.g., "CS7637"
    description: str = ""
    instructor: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class Module(BaseModel):
    """A module/unit within a course."""
    id: str = Field(default_factory=generate_id)
    course_id: str
    number: int
    title: str
    description: str = ""
    order: int = 0


class Topic(BaseModel):
    """A topic within a module."""
    id: str = Field(default_factory=generate_id)
    module_id: str
    title: str
    description: str = ""
    order: int = 0


class Concept(BaseModel):
    """A concept within a topic, linked to content chunks."""
    id: str = Field(default_factory=generate_id)
    topic_id: str
    name: str
    definition: str = ""
    chunk_ids: list[str] = Field(default_factory=list)


# =========================================================================
# Library Item Tagging - Applies hierarchy to library items
# =========================================================================

class LibraryItemTags(BaseModel):
    """
    Tags for organizing library items within the course hierarchy.

    Library items can be tagged at any level of the hierarchy:
    - organization: e.g., "Georgia Tech"
    - course_code: e.g., "CS7637"
    - course_id: Link to Course model (if in KB)
    - module_id: Link to Module model (if in KB)
    - topic_ids: Link to Topic models (if in KB)
    - custom_tags: Freeform tags for additional categorization
    """
    organization: str = ""
    course_code: str = ""
    course_id: Optional[str] = None
    module_id: Optional[str] = None
    topic_ids: list[str] = Field(default_factory=list)
    custom_tags: list[str] = Field(default_factory=list)
