"""
Entity Extraction using GLiNER with GPT Fallback

Extracts named entities from text using GLiNER (zero-shot NER),
with OpenAI GPT fallback for low-confidence extractions.
"""

import logging
import os
import re
from typing import Optional

from .models import Entity, generate_id

logger = logging.getLogger(__name__)


class EntityExtractor:
    """
    Extracts named entities from text using GLiNER.

    GLiNER provides zero-shot entity extraction - no training needed
    for custom entity types. Falls back to GPT for edge cases.
    """

    # Default entity types for educational content
    DEFAULT_ENTITY_TYPES = [
        "concept",       # Key ideas, theories (e.g., "machine learning", "recursion")
        "technique",     # Methods, algorithms (e.g., "backpropagation", "A* search")
        "term",          # Domain vocabulary (e.g., "epoch", "gradient")
        "person",        # People (e.g., "Alan Turing")
        "organization",  # Institutions (e.g., "Georgia Tech")
        "tool",          # Software/tools (e.g., "Python", "TensorFlow")
        "work",          # Papers, books (e.g., "Attention Is All You Need")
    ]

    # Map GLiNER labels to our schema types
    TYPE_MAP = {
        "concept": "CONCEPT",
        "technique": "TECHNIQUE",
        "term": "TERM",
        "person": "PERSON",
        "organization": "ORGANIZATION",
        "tool": "TOOL",
        "work": "WORK",
        # Legacy mappings for backward compatibility
        "location": "ORGANIZATION",  # Map to nearest equivalent
        "product": "TOOL",
        "event": "CONCEPT",
        "date": "TERM",
        "work of art": "WORK",
    }

    def __init__(
        self,
        model: str = "urchade/gliner_medium-v2.1",
        use_gpu: bool = True,
        confidence_threshold: float = 0.5,
        enable_gpt_fallback: bool = True,
        gpt_fallback_threshold: float = 0.4,
        gpt_model: str = None
    ):
        """
        Initialize the entity extractor.

        Args:
            model: GLiNER model name from HuggingFace
            use_gpu: Whether to use GPU if available
            confidence_threshold: Minimum confidence for entity acceptance
            enable_gpt_fallback: Enable GPT for low-confidence cases
            gpt_fallback_threshold: Trigger GPT when avg confidence below this
            gpt_model: OpenAI model for GPT fallback (default from EXTRACTION_MODEL env)
        """
        self.model_name = model
        self.confidence_threshold = confidence_threshold
        self.enable_gpt_fallback = enable_gpt_fallback
        self.gpt_fallback_threshold = gpt_fallback_threshold
        self.gpt_model = gpt_model or os.environ.get("EXTRACTION_MODEL_MINI", "gpt-5-mini")
        self._gliner_model = None
        self._openai_client = None
        self._use_gpu = use_gpu

        # Lazy load models
        self._init_gliner()

    def _init_gliner(self):
        """Initialize GLiNER model."""
        try:
            from gliner import GLiNER

            device = "cuda" if self._use_gpu else "cpu"
            if self._use_gpu:
                try:
                    import torch
                    if not torch.cuda.is_available():
                        device = "cpu"
                        logger.info("CUDA not available, using CPU for GLiNER")
                except ImportError:
                    device = "cpu"

            self._gliner_model = GLiNER.from_pretrained(self.model_name)
            if device == "cuda":
                self._gliner_model = self._gliner_model.to(device)

            logger.info(f"Loaded GLiNER model: {self.model_name} on {device}")

        except ImportError:
            raise ImportError("gliner package required. Install with: pip install gliner")
        except Exception as e:
            logger.error(f"Failed to load GLiNER model: {e}")
            raise

    def _init_openai(self):
        """Initialize OpenAI client for GPT fallback."""
        if self._openai_client is not None:
            return

        try:
            from openai import OpenAI
            from config.settings import settings

            api_key = settings.get_openai_key()
            self._openai_client = OpenAI(api_key=api_key)
            logger.info("Initialized OpenAI client for entity extraction fallback")

        except Exception as e:
            logger.warning(f"Could not initialize OpenAI client: {e}")
            self._openai_client = None

    def extract(
        self,
        text: str,
        entity_types: Optional[list[str]] = None,
        existing_entities: Optional[list[Entity]] = None,
        kb_id: str = ""
    ) -> list[Entity]:
        """
        Extract entities from text.

        Args:
            text: Text to process
            entity_types: Entity types to extract (uses defaults if None)
            existing_entities: Known entities for deduplication
            kb_id: Knowledge base ID for created entities

        Returns:
            List of Entity models (deduplicated)
        """
        if not text or not text.strip():
            return []

        entity_types = entity_types or self.DEFAULT_ENTITY_TYPES

        # Extract with GLiNER
        entities, avg_confidence = self._extract_with_gliner(text, entity_types, kb_id)

        # Check if GPT fallback is needed
        # Only use fallback when:
        # 1. Fallback is enabled
        # 2. GLiNER found entities but with low confidence, OR GLiNER found no entities
        # 3. Text appears to have entities (indicators present)
        should_fallback = (
            self.enable_gpt_fallback and
            self._has_entity_indicators(text, entities) and
            (len(entities) == 0 or avg_confidence < self.gpt_fallback_threshold)
        )

        if should_fallback:
            logger.info(f"Using GPT fallback (GLiNER: {len(entities)} entities, confidence: {avg_confidence:.2f})")
            gpt_entities = self._extract_with_gpt(text, entity_types, kb_id)
            entities = self._merge_entity_lists(entities, gpt_entities)

        # Deduplicate against existing entities
        if existing_entities:
            entities = self._deduplicate(entities, existing_entities)

        return entities

    def extract_batch(
        self,
        texts: list[str],
        entity_types: Optional[list[str]] = None,
        kb_id: str = ""
    ) -> list[list[Entity]]:
        """
        Extract entities from multiple texts.

        Args:
            texts: List of texts to process
            entity_types: Entity types to extract
            kb_id: Knowledge base ID

        Returns:
            List of entity lists, one per input text
        """
        results = []
        all_entities = []  # Track for cross-text deduplication

        for text in texts:
            entities = self.extract(
                text,
                entity_types=entity_types,
                existing_entities=all_entities,
                kb_id=kb_id
            )
            results.append(entities)
            all_entities.extend(entities)

        return results

    def _extract_with_gliner(
        self,
        text: str,
        entity_types: list[str],
        kb_id: str
    ) -> tuple[list[Entity], float]:
        """
        Extract entities using GLiNER.

        Returns:
            Tuple of (entities, average_confidence)
        """
        entities = []
        confidences = []

        try:
            # GLiNER prediction
            predictions = self._gliner_model.predict_entities(
                text,
                entity_types,
                threshold=self.confidence_threshold
            )

            for pred in predictions:
                entity_text = pred.get("text", "").strip()
                entity_type = pred.get("label", "").lower()
                score = pred.get("score", 0.0)

                if not entity_text or len(entity_text) < 2:
                    continue

                # Map to our schema type
                mapped_type = self.TYPE_MAP.get(entity_type, "CONCEPT")

                entity = Entity(
                    id=generate_id(),
                    kb_id=kb_id,
                    name=entity_text,
                    type=mapped_type,
                    description="",
                    aliases=[]
                )
                entities.append(entity)
                confidences.append(score)

        except Exception as e:
            logger.error(f"GLiNER extraction failed: {e}")

        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0
        return entities, avg_confidence

    def _extract_with_gpt(
        self,
        text: str,
        entity_types: list[str],
        kb_id: str
    ) -> list[Entity]:
        """
        Extract entities using GPT (fallback).

        Returns:
            List of Entity models
        """
        self._init_openai()

        if not self._openai_client:
            logger.warning("OpenAI client not available for fallback")
            return []

        entities = []

        try:
            # Build prompt
            type_list = ", ".join(entity_types)
            prompt = f"""Extract named entities from the following text.
For each entity, provide:
- name: The entity name as it appears in text
- type: One of [{type_list}]

Text:
{text[:4000]}

Respond in this exact format, one entity per line:
name|type

Only include clear, unambiguous entities. Do not include pronouns or generic terms."""

            response = self._openai_client.chat.completions.create(
                model=self.gpt_model,
                messages=[
                    {"role": "system", "content": "You are an expert at named entity recognition. Extract entities precisely."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_completion_tokens=1000
            )

            # Parse response with null checks
            if not response.choices:
                logger.warning("GPT returned empty choices")
                return entities

            message_content = response.choices[0].message.content
            if not message_content:
                logger.warning("GPT returned empty content")
                return entities

            content = message_content.strip()
            for line in content.split("\n"):
                line = line.strip()
                if "|" in line:
                    parts = line.split("|", 1)
                    if len(parts) == 2:
                        name = parts[0].strip()
                        raw_type = parts[1].strip().lower()

                        if not name or len(name) < 2:
                            continue

                        # Map to our schema type
                        mapped_type = self.TYPE_MAP.get(raw_type, "CONCEPT")

                        entity = Entity(
                            id=generate_id(),
                            kb_id=kb_id,
                            name=name,
                            type=mapped_type,
                            description="",
                            aliases=[]
                        )
                        entities.append(entity)

            logger.debug(f"GPT extracted {len(entities)} entities")

        except Exception as e:
            logger.error(f"GPT extraction failed: {e}")

        return entities

    def _has_entity_indicators(self, text: str, found_entities: list[Entity]) -> bool:
        """
        Check if text likely has entities that were missed.

        Returns True if text has entity indicators but few entities were found.
        """
        # Case-sensitive patterns (require proper capitalization)
        case_sensitive_patterns = [
            r'\b(Dr\.|Prof\.|Mr\.|Mrs\.|Ms\.)\s+[A-Z]',  # Titles followed by capital
            r'\b[A-Z][a-z]+\s+[A-Z][a-z]+\b',  # Proper names (two capitalized words)
        ]

        # Case-insensitive patterns
        case_insensitive_patterns = [
            r'\b(University|Institute|Corporation|Inc\.|Ltd\.)\b',  # Organizations
            r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\b',  # Dates
        ]

        indicator_count = sum(
            len(re.findall(pattern, text))
            for pattern in case_sensitive_patterns
        )
        indicator_count += sum(
            len(re.findall(pattern, text, re.IGNORECASE))
            for pattern in case_insensitive_patterns
        )

        # If many indicators but few entities, likely missed some
        entity_count = len(found_entities)
        word_count = len(text.split())

        # Heuristic: expect ~1 entity per 200 words in typical prose
        expected_min = max(1, word_count // 200)

        return indicator_count > 3 and entity_count < expected_min

    def _merge_entity_lists(
        self,
        list1: list[Entity],
        list2: list[Entity]
    ) -> list[Entity]:
        """
        Merge two entity lists, deduplicating by name similarity.
        """
        merged = list(list1)
        existing_names = {self._normalize_name(e.name) for e in list1}

        for entity in list2:
            normalized = self._normalize_name(entity.name)
            if normalized not in existing_names:
                merged.append(entity)
                existing_names.add(normalized)

        return merged

    def _deduplicate(
        self,
        new_entities: list[Entity],
        existing_entities: list[Entity]
    ) -> list[Entity]:
        """
        Remove entities that already exist.
        """
        existing_names = {self._normalize_name(e.name) for e in existing_entities}

        return [
            e for e in new_entities
            if self._normalize_name(e.name) not in existing_names
        ]

    def _normalize_name(self, name: str) -> str:
        """
        Normalize entity name for comparison.
        """
        return name.lower().strip()
