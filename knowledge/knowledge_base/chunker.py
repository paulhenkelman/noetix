"""
Text Chunking Utilities

Splits documents into chunks suitable for embedding and retrieval.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


class TextChunker:
    """
    Token-aware text chunker with overlap.

    Splits text into chunks of approximately `chunk_size` tokens,
    with `overlap` tokens shared between consecutive chunks.
    """

    def __init__(
        self,
        chunk_size: int = 512,
        overlap: int = 50,
        encoding_name: str = "cl100k_base"
    ):
        """
        Initialize the chunker.

        Args:
            chunk_size: Target chunk size in tokens
            overlap: Number of overlapping tokens between chunks
            encoding_name: Tiktoken encoding to use (cl100k_base for GPT-4/embeddings)
        """
        self.chunk_size = chunk_size
        self.overlap = overlap

        try:
            import tiktoken
            self.encoding = tiktoken.get_encoding(encoding_name)
            logger.info(f"Initialized chunker: {chunk_size} tokens, {overlap} overlap")
        except ImportError:
            raise ImportError("tiktoken package required. Install with: pip install tiktoken")

    def count_tokens(self, text: str) -> int:
        """Count tokens in text."""
        return len(self.encoding.encode(text))

    def chunk_text(self, text: str, metadata: Optional[dict] = None) -> list[dict]:
        """
        Split text into overlapping chunks.

        Args:
            text: Text to chunk
            metadata: Optional metadata to attach to each chunk

        Returns:
            List of chunk dicts with 'text', 'position', 'token_count', and metadata
        """
        if not text or not text.strip():
            return []

        # Tokenize
        tokens = self.encoding.encode(text)
        total_tokens = len(tokens)

        if total_tokens <= self.chunk_size:
            # Text fits in single chunk
            return [{
                'text': text,
                'position': 0,
                'token_count': total_tokens,
                **(metadata or {})
            }]

        chunks = []
        start = 0
        position = 0

        while start < total_tokens:
            # Calculate end position
            end = min(start + self.chunk_size, total_tokens)

            # Decode tokens back to text
            chunk_tokens = tokens[start:end]
            chunk_text = self.encoding.decode(chunk_tokens)

            chunks.append({
                'text': chunk_text,
                'position': position,
                'token_count': len(chunk_tokens),
                **(metadata or {})
            })

            position += 1

            # If we've reached the end, stop
            if end >= total_tokens:
                break

            # Move start position (with overlap)
            start = end - self.overlap

        logger.debug(f"Created {len(chunks)} chunks from {total_tokens} tokens")
        return chunks

    def chunk_by_paragraphs(
        self,
        text: str,
        metadata: Optional[dict] = None
    ) -> list[dict]:
        """
        Split text into chunks respecting paragraph boundaries.

        Tries to keep paragraphs together when possible, but will
        split long paragraphs if they exceed chunk_size.

        Args:
            text: Text to chunk
            metadata: Optional metadata to attach to each chunk

        Returns:
            List of chunk dicts
        """
        if not text or not text.strip():
            return []

        # Split into paragraphs
        paragraphs = re.split(r'\n\s*\n', text)
        paragraphs = [p.strip() for p in paragraphs if p.strip()]

        chunks = []
        current_chunk = []
        current_tokens = 0
        position = 0

        # Count tokens for paragraph separator
        separator = '\n\n'
        separator_tokens = self.count_tokens(separator)

        for para in paragraphs:
            para_tokens = self.count_tokens(para)

            # If single paragraph exceeds chunk size, chunk it directly
            if para_tokens > self.chunk_size:
                # Flush current chunk first
                if current_chunk:
                    chunk_text = separator.join(current_chunk)
                    # Recalculate actual token count for accuracy
                    actual_tokens = self.count_tokens(chunk_text)
                    chunks.append({
                        'text': chunk_text,
                        'position': position,
                        'token_count': actual_tokens,
                        **(metadata or {})
                    })
                    position += 1
                    current_chunk = []
                    current_tokens = 0

                # Chunk the long paragraph
                para_chunks = self.chunk_text(para, metadata)
                for pc in para_chunks:
                    pc['position'] = position
                    chunks.append(pc)
                    position += 1
                continue

            # Calculate tokens needed including separator if not first paragraph in chunk
            tokens_needed = para_tokens
            if current_chunk:
                tokens_needed += separator_tokens

            # Check if adding this paragraph exceeds limit
            if current_tokens + tokens_needed > self.chunk_size and current_chunk:
                # Flush current chunk
                chunk_text = separator.join(current_chunk)
                actual_tokens = self.count_tokens(chunk_text)
                chunks.append({
                    'text': chunk_text,
                    'position': position,
                    'token_count': actual_tokens,
                    **(metadata or {})
                })
                position += 1
                current_chunk = []
                current_tokens = 0
                tokens_needed = para_tokens  # Reset since this will be first in new chunk

            # Add paragraph to current chunk
            current_chunk.append(para)
            current_tokens += tokens_needed

        # Flush remaining
        if current_chunk:
            chunk_text = separator.join(current_chunk)
            actual_tokens = self.count_tokens(chunk_text)
            chunks.append({
                'text': chunk_text,
                'position': position,
                'token_count': actual_tokens,
                **(metadata or {})
            })

        logger.debug(f"Created {len(chunks)} paragraph-aware chunks")
        return chunks
