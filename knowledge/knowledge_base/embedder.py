"""
OpenAI Embedding Client

Generates embeddings using OpenAI's text-embedding models.
"""

import logging
from typing import Optional
import os

logger = logging.getLogger(__name__)


class Embedder:
    """
    Wrapper for OpenAI embeddings API.

    Uses text-embedding-3-large by default for high-quality embeddings.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "text-embedding-3-large"
    ):
        """
        Initialize the embedder.

        Args:
            api_key: OpenAI API key (defaults to loading from ~/.openai)
            model: Embedding model to use
        """
        self.model = model

        # Load API key
        if api_key:
            self.api_key = api_key
        else:
            from config.settings import settings
            self.api_key = settings.get_openai_key()

        # Initialize OpenAI client
        try:
            from openai import OpenAI
            self.client = OpenAI(api_key=self.api_key)
            logger.info(f"Initialized embedder with model: {model}")
        except ImportError:
            raise ImportError("openai package required. Install with: pip install openai")

    def embed(self, text: str) -> list[float]:
        """
        Generate embedding for a single text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector

        Raises:
            ValueError: If text is empty
            Exception: If API call fails after retries
        """
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text")

        import time

        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.client.embeddings.create(
                    input=text,
                    model=self.model
                )
                return response.data[0].embedding
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    logger.warning(f"Embedding API failed (attempt {attempt + 1}), retrying in {wait_time}s: {e}")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Embedding API failed after {max_retries} attempts: {e}")
                    raise

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count for a text (4 chars per token heuristic)."""
        return max(1, len(text) // 3)  # Conservative: 3 chars/token to stay safe

    def embed_batch(self, texts: list[str], batch_size: int = 100) -> list[list[float]]:
        """
        Generate embeddings for multiple texts with token-aware batching.

        Respects the OpenAI API limit of 300,000 tokens per request by
        building batches based on estimated token count, not just text count.

        Args:
            texts: List of texts to embed
            batch_size: Maximum texts per API call

        Returns:
            List of embedding vectors

        Raises:
            ValueError: If any text is empty
            Exception: If API call fails after retries
        """
        import time

        if not texts:
            return []

        # Validate all texts are non-empty
        for i, text in enumerate(texts):
            if not text or not text.strip():
                raise ValueError(f"Cannot embed empty text at index {i}")

        MAX_TOKENS_PER_REQUEST = 250_000  # Stay under 300k limit with margin
        all_embeddings = []
        max_retries = 3

        # Build token-aware batches
        i = 0
        batch_num = 0
        while i < len(texts):
            batch = []
            batch_tokens = 0

            while i < len(texts) and len(batch) < batch_size:
                est = self._estimate_tokens(texts[i])
                if batch and batch_tokens + est > MAX_TOKENS_PER_REQUEST:
                    break
                batch.append(texts[i])
                batch_tokens += est
                i += 1

            batch_num += 1

            for attempt in range(max_retries):
                try:
                    response = self.client.embeddings.create(
                        input=batch,
                        model=self.model
                    )

                    # Sort by index to maintain order
                    sorted_data = sorted(response.data, key=lambda x: x.index)
                    batch_embeddings = [item.embedding for item in sorted_data]
                    all_embeddings.extend(batch_embeddings)

                    logger.debug(f"Embedded batch {batch_num}, {len(batch)} texts (~{batch_tokens} tokens)")
                    break  # Success, exit retry loop

                except Exception as e:
                    err_msg = str(e).lower()
                    # If we hit token limit, split batch in half and retry
                    if "max_tokens_per_request" in err_msg and len(batch) > 1:
                        logger.warning(f"Batch exceeded token limit ({len(batch)} texts, ~{batch_tokens} tokens), splitting")
                        mid = len(batch) // 2
                        # Re-queue: put second half back, retry first half
                        i -= len(batch) - mid
                        batch = batch[:mid]
                        batch_tokens = sum(self._estimate_tokens(t) for t in batch)
                        continue  # Retry with smaller batch (same attempt number)

                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        logger.warning(f"Batch embedding failed (attempt {attempt + 1}), retrying in {wait_time}s: {e}")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"Batch embedding failed after {max_retries} attempts: {e}")
                        raise

        return all_embeddings

    @property
    def dimensions(self) -> int:
        """Return the embedding dimensions for the current model."""
        # Known dimensions for OpenAI models
        dims = {
            "text-embedding-3-large": 3072,
            "text-embedding-3-small": 1536,
            "text-embedding-ada-002": 1536,
        }
        return dims.get(self.model, 3072)
