#!/usr/bin/env python3
"""
PDF to M4B Audiobook Pipeline
A complete, reusable pipeline for converting PDF books to high-quality M4B audiobooks.

Features:
- Automatic chapter detection from PDF structure
- High-quality TTS using Kokoro (GPU accelerated)
- M4B generation with chapter markers and metadata
- Progress tracking and resumable processing
- Parallel processing support

Usage:
    python pipeline.py input.pdf [--output-dir OUTPUT] [--voice VOICE] [--resume]

Example:
    python pipeline.py "My Book.pdf" --output-dir ./audiobooks --voice af_heart
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import Optional
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('pipeline.log')
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class PipelineState:
    """Tracks pipeline progress for resumability."""
    pdf_path: str
    output_dir: str
    voice: str
    total_chapters: int
    completed_chapters: list[int]
    chapter_audio_files: dict[int, str]
    started_at: str
    last_updated: str
    status: str  # 'in_progress', 'completed', 'failed'


class AudiobookPipeline:
    """
    Complete pipeline for converting PDF to M4B audiobook.
    """

    AVAILABLE_VOICES = {
        'af_heart': 'American Female (Heart) - warm, expressive [RECOMMENDED]',
        'af_bella': 'American Female (Bella) - clear, professional',
        'af_nicole': 'American Female (Nicole) - soft, gentle',
        'af_sarah': 'American Female (Sarah) - bright, energetic',
        'af_sky': 'American Female (Sky) - youthful',
        'am_adam': 'American Male (Adam) - deep, authoritative',
        'am_michael': 'American Male (Michael) - warm, friendly',
    }

    def __init__(self, pdf_path: str | Path, output_dir: str | Path = None,
                 voice: str = 'af_heart', resume: bool = False, ocr_cache_path: str | Path = None):
        """
        Initialize the audiobook pipeline.

        Args:
            pdf_path: Path to the PDF file
            output_dir: Output directory (default: same as PDF)
            voice: Kokoro voice ID
            resume: Whether to resume from previous state
            ocr_cache_path: Path to pre-computed OCR cache (for scanned PDFs)
        """
        self.pdf_path = Path(pdf_path).resolve()

        if not self.pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {self.pdf_path}")

        # Setup output directory
        if output_dir:
            self.output_dir = Path(output_dir).resolve()
        else:
            self.output_dir = self.pdf_path.parent / f"{self.pdf_path.stem}_audiobook"

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.chapters_dir = self.output_dir / "chapters"
        self.chapters_dir.mkdir(exist_ok=True)

        self.voice = voice
        self.ocr_cache_path = Path(ocr_cache_path) if ocr_cache_path else None
        self.state_file = self.output_dir / "pipeline_state.json"

        # Load or create state
        if resume and self.state_file.exists():
            self.state = self._load_state()
            logger.info(f"Resuming from previous state: {len(self.state.completed_chapters)}/{self.state.total_chapters} chapters done")
        else:
            self.state = None

        # Lazy-loaded components
        self._extractor = None
        self._tts_engine = None
        self._metadata = None

    def _load_state(self) -> PipelineState:
        """Load pipeline state from file."""
        with open(self.state_file, 'r') as f:
            data = json.load(f)
        return PipelineState(**data)

    def _save_state(self):
        """Save current pipeline state."""
        if self.state:
            self.state.last_updated = datetime.now().isoformat()
            with open(self.state_file, 'w') as f:
                json.dump(asdict(self.state), f, indent=2)

    @property
    def extractor(self):
        """Lazy-load PDF extractor."""
        if self._extractor is None:
            from pdf_extractor import PDFExtractor
            self._extractor = PDFExtractor(self.pdf_path, ocr_cache_path=self.ocr_cache_path)
        return self._extractor

    @property
    def tts_engine(self):
        """Lazy-load TTS engine."""
        if self._tts_engine is None:
            from tts_engine import TTSEngine
            self._tts_engine = TTSEngine(voice=self.voice)
        return self._tts_engine

    def extract_chapters(self, chapter_definitions: list[dict] = None) -> list:
        """
        Extract chapters from PDF.

        Args:
            chapter_definitions: Optional manual chapter definitions
                                 [{'number': 1, 'title': 'Intro', 'start_page': 0}, ...]

        Returns:
            List of Chapter objects
        """
        from pdf_extractor import create_thagard_chapters

        if chapter_definitions is None:
            # First, check for AI analysis file
            analysis_path = self.pdf_path.with_suffix('.analysis.json')
            if analysis_path.exists():
                logger.info(f"Loading AI analysis from {analysis_path.name}")
                try:
                    with open(analysis_path) as f:
                        analysis = json.load(f)
                    chapter_definitions = analysis.get('chapters', [])
                    if chapter_definitions:
                        # AI analyzer returns 1-indexed page numbers; convert to 0-indexed
                        for ch in chapter_definitions:
                            if ch.get('start_page', 0) >= 1:
                                ch['start_page'] = ch['start_page'] - 1
                        logger.info(f"Using AI-detected chapters: {len(chapter_definitions)} chapters")
                except Exception as e:
                    logger.warning(f"Failed to load analysis file: {e}")
                    chapter_definitions = None

        if chapter_definitions is None:
            # Check if this is the Thagard book
            first_page = self.extractor.extract_page_text(0)
            if 'thagard' in first_page.lower() or 'cognitive science' in first_page.lower():
                logger.info("Detected Thagard's 'Mind' - using predefined chapters")
                chapter_definitions = create_thagard_chapters()
            else:
                # Fallback: treat entire book as one chapter
                logger.info("No chapter info available - treating as single chapter")
                chapter_definitions = [{
                    'number': 1,
                    'title': self.pdf_path.stem,
                    'start_page': 0
                }]

        self._metadata = self.extractor.extract_with_manual_chapters(chapter_definitions)
        return self._metadata.chapters

    def generate_chapter_audio(self, chapter, chapter_num: int) -> tuple[Path, float]:
        """
        Generate audio for a single chapter.

        Returns:
            Tuple of (audio_file_path, duration_seconds)
        """
        logger.info(f"Generating audio for Chapter {chapter_num}: {chapter.title}")

        def progress_callback(current, total):
            pct = (current / total) * 100
            print(f"\r  Progress: {current}/{total} chunks ({pct:.1f}%)", end='', flush=True)

        audio_path, duration = self.tts_engine.synthesize_chapter(
            chapter_num=chapter_num,
            title=chapter.title,
            text=chapter.text,
            output_dir=self.chapters_dir,
            progress_callback=progress_callback
        )

        print()  # New line after progress
        logger.info(f"  Completed: {duration:.1f} seconds of audio")

        return audio_path, duration

    def build_m4b(self, chapter_audio_files: list[Path], title: str = None,
                  author: str = None, aac_converter=None) -> Path:
        """
        Build final M4B from chapter audio files.
        """
        from m4b_builder import M4BBuilder, AudiobookMetadata

        if title is None:
            title = self._metadata.title if self._metadata else self.pdf_path.stem

        if author is None:
            author = self._metadata.author if self._metadata else "Unknown"

        logger.info(f"Building M4B: {title} by {author}")

        # Wait for any pending AAC conversions
        if aac_converter:
            logger.info("Waiting for background AAC conversions to complete...")
            aac_converter.wait_all()

        builder = M4BBuilder(self.output_dir)
        metadata = AudiobookMetadata(
            title=title,
            author=author,
            narrator=f"Kokoro AI ({self.voice})",
            year=str(datetime.now().year)
        )

        m4b_path = builder.build_m4b(
            chapter_audio_files,
            metadata,
            aac_converter=aac_converter
        )

        return m4b_path

    def run(self, chapter_definitions: list[dict] = None,
            title: str = None, author: str = None) -> Path:
        """
        Run the complete pipeline.

        Args:
            chapter_definitions: Optional manual chapter definitions
            title: Book title (auto-detected if None)
            author: Book author (auto-detected if None)

        Returns:
            Path to the generated M4B file
        """
        start_time = time.time()
        logger.info("=" * 60)
        logger.info(f"Starting PDF to M4B conversion")
        logger.info(f"Input: {self.pdf_path}")
        logger.info(f"Output: {self.output_dir}")
        logger.info(f"Voice: {self.voice}")
        logger.info("=" * 60)

        # Step 1: Extract chapters
        logger.info("\n[Step 1/3] Extracting chapters from PDF...")
        chapters = self.extract_chapters(chapter_definitions)
        logger.info(f"Found {len(chapters)} chapters")

        # Initialize state if not resuming
        if self.state is None:
            self.state = PipelineState(
                pdf_path=str(self.pdf_path),
                output_dir=str(self.output_dir),
                voice=self.voice,
                total_chapters=len(chapters),
                completed_chapters=[],
                chapter_audio_files={},
                started_at=datetime.now().isoformat(),
                last_updated=datetime.now().isoformat(),
                status='in_progress'
            )
            self._save_state()

        # Step 2: Generate audio for each chapter (with parallel AAC conversion)
        logger.info("\n[Step 2/3] Generating TTS audio (with parallel AAC conversion)...")

        # Create AAC converter for parallel processing
        from m4b_builder import AACConverter
        temp_dir = self.output_dir / "temp"
        aac_converter = AACConverter(temp_dir)  # Uses CPU count for parallelism

        chapter_audio_files = []
        total_duration = 0

        try:
            for i, chapter in enumerate(chapters):
                chapter_num = chapter.number

                # Check if already completed (for resume)
                if chapter_num in self.state.completed_chapters:
                    audio_path = Path(self.state.chapter_audio_files[str(chapter_num)])
                    if audio_path.exists():
                        logger.info(f"Skipping Chapter {chapter_num} (already completed)")
                        chapter_audio_files.append(audio_path)
                        # Still submit for AAC conversion if WAV
                        if audio_path.suffix.lower() == '.wav':
                            aac_converter.submit_conversion(audio_path)
                        continue

                # Skip chapters with no text content
                if not chapter.text.strip():
                    logger.warning(f"Skipping Chapter {chapter_num}: no text content")
                    continue

                try:
                    audio_path, duration = self.generate_chapter_audio(chapter, chapter_num)
                    chapter_audio_files.append(audio_path)
                    total_duration += duration

                    # Submit for background AAC conversion immediately
                    # This runs in parallel with the next chapter's TTS
                    aac_converter.submit_conversion(audio_path)
                    logger.info(f"  Queued for AAC conversion (running in background)")

                    # Update state
                    self.state.completed_chapters.append(chapter_num)
                    self.state.chapter_audio_files[str(chapter_num)] = str(audio_path)
                    self._save_state()

                except Exception as e:
                    logger.error(f"Error processing Chapter {chapter_num}: {e}")
                    self.state.status = 'failed'
                    self._save_state()
                    raise

            logger.info(f"\nTotal audio generated: {total_duration / 60:.1f} minutes")

            # Step 3: Build M4B
            logger.info("\n[Step 3/3] Building M4B audiobook...")

            if title is None and self._metadata:
                title = self._metadata.title

            if author is None and self._metadata:
                author = self._metadata.author

            m4b_path = self.build_m4b(chapter_audio_files, title, author, aac_converter)

        finally:
            aac_converter.shutdown()

        # Update final state
        self.state.status = 'completed'
        self._save_state()

        # Summary
        elapsed = time.time() - start_time
        elapsed_str = str(timedelta(seconds=int(elapsed)))

        logger.info("\n" + "=" * 60)
        logger.info("CONVERSION COMPLETE!")
        logger.info("=" * 60)
        logger.info(f"Output file: {m4b_path}")
        logger.info(f"File size: {m4b_path.stat().st_size / (1024*1024):.1f} MB")
        logger.info(f"Audio duration: {total_duration / 60:.1f} minutes")
        logger.info(f"Processing time: {elapsed_str}")
        logger.info("=" * 60)

        return m4b_path

    def cleanup(self):
        """Clean up resources."""
        if self._extractor:
            self._extractor.close()


def main():
    """Command-line interface for the pipeline."""
    parser = argparse.ArgumentParser(
        description='Convert PDF books to M4B audiobooks with high-quality TTS',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Available voices:
  af_heart    - American Female (Heart) - warm, expressive [RECOMMENDED]
  af_bella    - American Female (Bella) - clear, professional
  af_nicole   - American Female (Nicole) - soft, gentle
  af_sarah    - American Female (Sarah) - bright, energetic
  af_sky      - American Female (Sky) - youthful
  am_adam     - American Male (Adam) - deep, authoritative
  am_michael  - American Male (Michael) - warm, friendly

Examples:
  python pipeline.py book.pdf
  python pipeline.py book.pdf --voice am_adam --output-dir ./audiobooks
  python pipeline.py book.pdf --resume  # Resume interrupted conversion
        """
    )

    parser.add_argument('pdf', help='Path to the PDF file')
    parser.add_argument('--output-dir', '-o', help='Output directory')
    parser.add_argument('--voice', '-v', default='af_heart',
                        choices=list(AudiobookPipeline.AVAILABLE_VOICES.keys()),
                        help='TTS voice to use (default: af_heart)')
    parser.add_argument('--resume', '-r', action='store_true',
                        help='Resume from previous state if available')
    parser.add_argument('--title', '-t', help='Book title (auto-detected if not provided)')
    parser.add_argument('--author', '-a', help='Book author (auto-detected if not provided)')
    parser.add_argument('--ocr-cache', help='Path to pre-computed OCR cache file')
    parser.add_argument('--list-voices', action='store_true',
                        help='List available voices and exit')

    args = parser.parse_args()

    if args.list_voices:
        print("\nAvailable TTS Voices:")
        print("-" * 60)
        for voice_id, description in AudiobookPipeline.AVAILABLE_VOICES.items():
            print(f"  {voice_id:12} - {description}")
        print()
        return 0

    try:
        pipeline = AudiobookPipeline(
            pdf_path=args.pdf,
            output_dir=args.output_dir,
            voice=args.voice,
            resume=args.resume,
            ocr_cache_path=args.ocr_cache
        )

        m4b_path = pipeline.run(title=args.title, author=args.author)
        pipeline.cleanup()

        print(f"\n✓ Audiobook created: {m4b_path}")
        return 0

    except FileNotFoundError as e:
        logger.error(str(e))
        return 1
    except KeyboardInterrupt:
        logger.info("\nInterrupted by user. Progress has been saved.")
        logger.info("Run with --resume to continue.")
        return 130
    except Exception as e:
        logger.exception(f"Pipeline failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
