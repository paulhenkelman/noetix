"""
TTS Engine Module using Kokoro
High-quality text-to-speech generation with GPU acceleration.
"""

import os
import re
import torch
import soundfile as sf
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import Generator, Callable
from concurrent.futures import ThreadPoolExecutor
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class AudioSegment:
    """Represents a generated audio segment."""
    text: str
    audio: np.ndarray
    sample_rate: int
    duration: float


class TTSEngine:
    """
    High-quality TTS engine using Kokoro.
    Optimized for long-form content like audiobooks.
    """

    # Available voices in Kokoro (American English)
    VOICES = {
        'af_heart': 'American Female (Heart) - warm, expressive',
        'af_bella': 'American Female (Bella) - clear, professional',
        'af_nicole': 'American Female (Nicole) - soft, gentle',
        'af_sarah': 'American Female (Sarah) - bright, energetic',
        'af_sky': 'American Female (Sky) - youthful',
        'am_adam': 'American Male (Adam) - deep, authoritative',
        'am_michael': 'American Male (Michael) - warm, friendly',
    }

    DEFAULT_VOICE = 'af_heart'
    SAMPLE_RATE = 24000

    def __init__(self, voice: str = None, device: str = None):
        """
        Initialize the TTS engine.

        Args:
            voice: Voice ID to use (default: af_heart)
            device: 'cuda' or 'cpu' (auto-detected if None)
        """
        from kokoro import KPipeline

        self.voice = voice or self.DEFAULT_VOICE
        self.device = device or ('cuda' if torch.cuda.is_available() else 'cpu')

        logger.info(f"Initializing Kokoro TTS on {self.device}")
        logger.info(f"Using voice: {self.voice}")

        # Initialize pipeline
        self.pipeline = KPipeline(lang_code='a')  # American English

        if self.device == 'cuda':
            logger.info(f"GPU: {torch.cuda.get_device_name(0)}")
            logger.info(f"VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")

    def _split_into_sentences(self, text: str) -> list[str]:
        """Split text into sentences for processing."""
        # Basic sentence splitting
        sentences = re.split(r'(?<=[.!?])\s+', text)
        return [s.strip() for s in sentences if s.strip()]

    def _chunk_text(self, text: str, max_chars: int = 500) -> list[str]:
        """
        Split text into chunks suitable for TTS processing.
        Tries to split at sentence boundaries.
        """
        sentences = self._split_into_sentences(text)
        chunks = []
        current_chunk = []
        current_length = 0

        for sentence in sentences:
            sentence_len = len(sentence)

            # If single sentence exceeds max, split it further
            if sentence_len > max_chars:
                if current_chunk:
                    chunks.append(' '.join(current_chunk))
                    current_chunk = []
                    current_length = 0

                # Split long sentence at punctuation or spaces
                parts = re.split(r'([,;:])\s+', sentence)
                temp_chunk = []
                temp_length = 0

                for part in parts:
                    if temp_length + len(part) > max_chars and temp_chunk:
                        chunks.append(' '.join(temp_chunk))
                        temp_chunk = [part]
                        temp_length = len(part)
                    else:
                        temp_chunk.append(part)
                        temp_length += len(part) + 1

                if temp_chunk:
                    chunks.append(' '.join(temp_chunk))

            elif current_length + sentence_len > max_chars:
                if current_chunk:
                    chunks.append(' '.join(current_chunk))
                current_chunk = [sentence]
                current_length = sentence_len
            else:
                current_chunk.append(sentence)
                current_length += sentence_len + 1

        if current_chunk:
            chunks.append(' '.join(current_chunk))

        return chunks

    def synthesize(self, text: str, progress_callback: Callable = None) -> Generator[AudioSegment, None, None]:
        """
        Synthesize speech from text.

        Args:
            text: Text to synthesize
            progress_callback: Optional callback(current, total) for progress

        Yields:
            AudioSegment objects containing the generated audio
        """
        # Clean and prepare text
        text = self._clean_for_tts(text)
        chunks = self._chunk_text(text)
        total_chunks = len(chunks)

        logger.info(f"Processing {total_chunks} text chunks")

        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                continue

            try:
                # Generate audio using Kokoro
                generator = self.pipeline(chunk, voice=self.voice)

                audio_parts = []
                for gs, ps, audio in generator:
                    audio_parts.append(audio)

                if audio_parts:
                    # Concatenate all audio parts
                    full_audio = np.concatenate(audio_parts)
                    duration = len(full_audio) / self.SAMPLE_RATE

                    yield AudioSegment(
                        text=chunk,
                        audio=full_audio,
                        sample_rate=self.SAMPLE_RATE,
                        duration=duration
                    )

                if progress_callback:
                    progress_callback(i + 1, total_chunks)

            except Exception as e:
                logger.error(f"Error synthesizing chunk {i}: {e}")
                # Continue with next chunk rather than failing entirely
                continue

    def _clean_for_tts(self, text: str) -> str:
        """Clean text for better TTS output."""
        # Replace Unicode characters that confuse TTS alignment
        unicode_replacements = {
            '\u2018': "'", '\u2019': "'",  # Smart single quotes
            '\u201c': '"', '\u201d': '"',  # Smart double quotes
            '\u2013': '-', '\u2014': ', ',  # En/em dashes
            '\u2026': '...',               # Ellipsis
            '\u00a0': ' ',                 # Non-breaking space
            '\u200b': '',                  # Zero-width space
            '\u200c': '', '\u200d': '',    # Zero-width non/joiner
            '\ufeff': '',                  # BOM
            '\u00ad': '',                  # Soft hyphen
            '\u2022': ', ',                # Bullet
            '\u25cf': ', ',                # Black circle bullet
            '\u00b7': ', ',                # Middle dot
            '\u2212': '-',                 # Minus sign
            '\u00d7': ' times ',           # Multiplication sign
            '\u00f7': ' divided by ',      # Division sign
            '\u2248': ' approximately ',   # Almost equal to
            '\u2260': ' not equal to ',    # Not equal to
            '\u2264': ' less than or equal to ',
            '\u2265': ' greater than or equal to ',
        }
        for char, replacement in unicode_replacements.items():
            text = text.replace(char, replacement)

        # Strip remaining non-ASCII that isn't basic Latin
        # Keep accented letters (Latin-1 Supplement) but drop symbols
        text = re.sub(r'[^\x00-\x7f\xc0-\xff]', ' ', text)

        # Remove control characters except newlines
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)

        # Normalize whitespace
        text = re.sub(r'\s+', ' ', text)

        # Expand common abbreviations
        abbreviations = {
            'Dr.': 'Doctor',
            'Mr.': 'Mister',
            'Mrs.': 'Missus',
            'Ms.': 'Miss',
            'Prof.': 'Professor',
            'e.g.': 'for example',
            'i.e.': 'that is',
            'etc.': 'et cetera',
            'vs.': 'versus',
            'Fig.': 'Figure',
            'fig.': 'figure',
            'Ch.': 'Chapter',
            'ch.': 'chapter',
            'p.': 'page',
            'pp.': 'pages',
            'vol.': 'volume',
            'ed.': 'edition',
            'no.': 'number',
        }
        for abbr, expansion in abbreviations.items():
            text = text.replace(abbr, expansion)

        # Remove URLs
        text = re.sub(r'https?://\S+', '', text)

        # Remove email addresses
        text = re.sub(r'\S+@\S+\.\S+', '', text)

        # Clean up citations like (Smith, 2005) or (Smith & Jones, 2005)
        text = re.sub(r'\([A-Za-z&\s]+,?\s*\d{4}[a-z]?\)', '', text)
        # Multi-citations like (Smith, 2005; Jones, 2010)
        text = re.sub(r'\((?:[A-Za-z]+,?\s*\d{4}[a-z]?[;,]\s*)+[A-Za-z]+,?\s*\d{4}[a-z]?\)', '', text)

        # Remove figure/table references like [Figure 1.2] or (Table 3)
        text = re.sub(r'[\[(](?:Figure|Table|Fig|Tab)\.?\s+[\d.]+[\])]', '', text, flags=re.IGNORECASE)
        # Remove standalone figure labels like "Figure 1.2:" or "Table 3."
        text = re.sub(r'^(?:Figure|Table|Fig|Tab)\.?\s+[\d.]+[.:]\s*', '', text, flags=re.IGNORECASE | re.MULTILINE)

        # Remove page references like "(p. 42)" or "(pp. 42-45)"
        text = re.sub(r'\(p{1,2}\.\s*\d+(?:\s*[-–]\s*\d+)?\)', '', text)

        # Remove copyright/trademark symbols and surrounding text
        text = re.sub(r'[©®™]', '', text)

        # Remove bracketed numbers like [1] [23] used as footnote markers
        text = re.sub(r'\[\d+\]', '', text)
        # Remove superscript-like single digits that follow words (footnote artifacts)
        text = re.sub(r'(?<=\w)\s*(\d)\s*(?=[A-Z])', r' ', text)

        # Remove excessive punctuation
        text = re.sub(r'[.]{2,}', '.', text)
        text = re.sub(r'[,]{2,}', ',', text)
        text = re.sub(r'[-]{3,}', ', ', text)

        # Remove isolated single characters (extraction artifacts)
        text = re.sub(r'(?<!\w)\s+[^aAI\s]\s+(?!\w)', ' ', text)

        # Clean up multiple spaces and leading/trailing whitespace per line
        text = re.sub(r' {2,}', ' ', text)

        return text.strip()

    def synthesize_to_file(self, text: str, output_path: str | Path,
                          progress_callback: Callable = None) -> float:
        """
        Synthesize text and save to a single audio file.

        Args:
            text: Text to synthesize
            output_path: Path to save the audio file
            progress_callback: Optional progress callback

        Returns:
            Duration of generated audio in seconds
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        all_audio = []
        total_duration = 0

        for segment in self.synthesize(text, progress_callback):
            all_audio.append(segment.audio)
            total_duration += segment.duration

        if all_audio:
            combined = np.concatenate(all_audio)
            sf.write(str(output_path), combined, self.SAMPLE_RATE)
            logger.info(f"Saved audio to {output_path} ({total_duration:.1f}s)")

        return total_duration

    def synthesize_chapter(self, chapter_num: int, title: str, text: str,
                          output_dir: Path, progress_callback: Callable = None) -> tuple[Path, float]:
        """
        Synthesize a complete chapter.

        Returns:
            Tuple of (output_path, duration_seconds)
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Create chapter intro
        intro_text = f"Chapter {chapter_num}. {title}."

        # Combine intro with chapter text
        full_text = intro_text + "\n\n" + text

        # Output filename
        safe_title = re.sub(r'[^\w\s-]', '', title)[:50]
        filename = f"chapter_{chapter_num:02d}_{safe_title.replace(' ', '_')}.wav"
        output_path = output_dir / filename

        duration = self.synthesize_to_file(full_text, output_path, progress_callback)

        return output_path, duration

    @classmethod
    def list_voices(cls) -> dict[str, str]:
        """Return available voices."""
        return cls.VOICES.copy()


if __name__ == "__main__":
    # Test the TTS engine
    engine = TTSEngine(voice='af_heart')

    test_text = """
    Welcome to this introduction to cognitive science.
    This book explores how the mind works through computational models.
    """

    print("Testing TTS synthesis...")

    def progress(current, total):
        print(f"Progress: {current}/{total}")

    duration = engine.synthesize_to_file(test_text, "test_output.wav", progress)
    print(f"Generated {duration:.1f} seconds of audio")
