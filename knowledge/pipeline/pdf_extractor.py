"""
PDF Text Extraction Module with Chapter Detection
Extracts text from PDFs while identifying chapter boundaries.
"""

import re
import fitz  # PyMuPDF
from dataclasses import dataclass
from pathlib import Path
from typing import Generator


@dataclass
class Chapter:
    """Represents a chapter in the book."""
    number: int
    title: str
    start_page: int
    end_page: int
    text: str


@dataclass
class BookMetadata:
    """Metadata for the extracted book."""
    title: str
    author: str
    chapters: list[Chapter]
    total_pages: int


class PDFExtractor:
    """Extracts text and chapter structure from PDF files."""

    # Common chapter heading patterns
    CHAPTER_PATTERNS = [
        r'^Chapter\s+(\d+)[:\s]*(.*)$',
        r'^CHAPTER\s+(\d+)[:\s]*(.*)$',
        r'^(\d+)\s+([A-Z][a-zA-Z\s]+)$',  # "1 Introduction" style
        r'^Part\s+([IVX]+)[:\s]*(.*)$',
        r'^PART\s+([IVX]+)[:\s]*(.*)$',
    ]

    def __init__(self, pdf_path: str | Path, enable_ocr: bool = True, ocr_cache_path: Path | str = None):
        self.pdf_path = Path(pdf_path)
        self.doc = fitz.open(str(self.pdf_path))
        self.enable_ocr = enable_ocr
        self._ocr_engine = None
        self._ocr_page_cache = {}  # Cache OCR results per page (for on-demand OCR)
        self._needs_ocr = None  # Lazy-evaluated

        # Load pre-computed OCR cache if provided
        self._ocr_file_cache = None
        if ocr_cache_path:
            from ocr_engine import load_ocr_cache
            self._ocr_file_cache = load_ocr_cache(ocr_cache_path)
            if self._ocr_file_cache and self._ocr_file_cache.get("needs_ocr"):
                self._needs_ocr = True
                print(f"[OCR] Using pre-computed OCR cache ({len(self._ocr_file_cache.get('pages', {}))} pages)")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.doc.close()

    def close(self):
        self.doc.close()

    def get_page_count(self) -> int:
        return len(self.doc)

    @property
    def needs_ocr(self) -> bool:
        """Check if this PDF needs OCR (lazy-evaluated and cached)."""
        if self._needs_ocr is None:
            if not self.enable_ocr:
                self._needs_ocr = False
            else:
                self._needs_ocr = self._check_needs_ocr()
        return self._needs_ocr

    def _check_needs_ocr(self, sample_pages: int = 5) -> bool:
        """Check if PDF needs OCR by sampling pages from the middle of the document.

        We sample from the middle to avoid title pages, copyright, ToC at the start
        and indexes/appendices at the end which may have atypical text density.

        Also detects watermarked scanned PDFs where the only text is repetitive
        watermark content but the actual content is in page-sized images.
        """
        from ocr_engine import detect_watermarked_scan

        needs_ocr, info = detect_watermarked_scan(self.doc, sample_pages)

        if info["is_repetitive_text"]:
            print(f"[OCR] Detected repetitive text pattern (watermark): {info['uniqueness_ratio']:.1%} unique phrases")

        if info["is_repetitive_text"] and info["pages_with_full_images"] > 0:
            print(f"[OCR] PDF appears to be watermarked scanned document "
                  f"({info['pages_with_full_images']}/{info['pages_checked']} pages with full images)")

        if needs_ocr:
            print(f"[OCR] PDF needs OCR (avg {info['avg_chars']:.0f} chars/page from middle pages), OCR will be used")

        return needs_ocr

    @property
    def ocr_engine(self):
        """Lazy-load the OCR engine only when needed."""
        if self._ocr_engine is None:
            from ocr_engine import OCREngine
            print("[OCR] Initializing GPU-accelerated OCR engine...")
            self._ocr_engine = OCREngine(gpu=True)
        return self._ocr_engine

    def extract_page_text(self, page_num: int) -> str:
        """Extract text from a single page, using OCR cache or on-demand OCR if needed."""
        if page_num < 0 or page_num >= len(self.doc):
            return ""

        # First, check pre-computed OCR file cache
        if self._ocr_file_cache and self._ocr_file_cache.get("needs_ocr"):
            from ocr_engine import get_ocr_text_for_page
            text = get_ocr_text_for_page(self._ocr_file_cache, page_num)
            if text is not None:
                return text

        # Check if we need OCR for this PDF (and no file cache available)
        if self.needs_ocr:
            # Check page cache first
            if page_num in self._ocr_page_cache:
                return self._ocr_page_cache[page_num]

            # Run OCR on this page (fallback if no pre-computed cache)
            page = self.doc[page_num]
            text = self.ocr_engine.ocr_page(page)
            self._ocr_page_cache[page_num] = text
            return text
        else:
            # Standard text extraction with reading-order sort and dehyphenation
            page = self.doc[page_num]
            text = page.get_text(sort=True, flags=fitz.TEXT_DEHYPHENATE | fitz.TEXT_PRESERVE_WHITESPACE)

            # Also extract text from annotations (highlighted/commented text,
            # text boxes, sticky notes, etc.)
            annot_texts = []
            for annot in page.annots() or []:
                annot_info = annot.info
                content = annot_info.get("content", "").strip()
                if content:
                    annot_texts.append(content)
            if annot_texts:
                text += "\n" + "\n".join(annot_texts)

            return text

    def clean_text(self, text: str) -> str:
        """Clean extracted text for TTS."""
        # Remove common watermark patterns
        text = self._remove_watermarks(text)

        # Remove page numbers that appear alone
        text = re.sub(r'^\d+\s*$', '', text, flags=re.MULTILINE)

        # Remove excessive whitespace but preserve paragraph breaks
        text = re.sub(r'\n{3,}', '\n\n', text)

        # Fix hyphenated words at line breaks
        text = re.sub(r'(\w)-\n(\w)', r'\1\2', text)

        # Remove headers/footers (lines that are just chapter titles or page nums)
        lines = text.split('\n')
        cleaned_lines = []
        for line in lines:
            stripped = line.strip()
            # Skip very short lines that look like page numbers
            if stripped.isdigit() and len(stripped) <= 3:
                continue
            cleaned_lines.append(line)
        text = '\n'.join(cleaned_lines)

        # Normalize whitespace
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' +\n', '\n', text)

        return text.strip()

    def _remove_watermarks(self, text: str) -> str:
        """Remove common watermark patterns from text."""
        # Common watermark patterns (case-insensitive)
        watermark_patterns = [
            # Personal use / do not reproduce patterns
            r'personal\s+use\s+only[,.]?\s*do\s+not\s+reproduce\.?',
            r'do\s+not\s+reproduce\.?\s*personal\s+use\s+only',
            # Email-based watermarks with dates
            r'\d{4}[-/]\d{2}[-/]\d{2}\s+\S+@\S+\.\S+',
            r'\S+@\S+\.\S+\s+\d{4}[-/]\d{2}[-/]\d{2}',
            # Copyright watermarks
            r'©\s*\d{4}[^.]*\.?\s*all\s+rights\s+reserved\.?',
            # Confidential/proprietary
            r'confidential[:\s]+[^.]*\.?',
            r'proprietary[:\s]+[^.]*\.?',
            # Draft/sample watermarks
            r'\bdraft\b',
            r'\bsample\s+copy\b',
        ]

        for pattern in watermark_patterns:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE)

        # Remove lines that are just repeated short phrases (likely watermarks)
        lines = text.split('\n')
        cleaned_lines = []
        seen_short_lines = {}

        for line in lines:
            stripped = line.strip()
            # Track short lines (likely watermarks if repeated)
            if 10 < len(stripped) < 80:
                seen_short_lines[stripped] = seen_short_lines.get(stripped, 0) + 1
                # If we've seen this exact line more than 3 times, it's probably a watermark
                if seen_short_lines[stripped] > 3:
                    continue
            cleaned_lines.append(line)

        return '\n'.join(cleaned_lines)

    def detect_chapters_from_toc(self) -> list[dict]:
        """Try to detect chapters from PDF's table of contents."""
        toc = self.doc.get_toc()
        chapters = []

        # Look for meaningful TOC entries
        for level, title, page in toc:
            # Skip page-only entries
            if re.match(r'^Page_\d+\.pdf$', title):
                continue
            if re.match(r'^[\d\-]+\.pdf$', title):
                continue

            # Check for chapter-like entries
            match = None
            for pattern in self.CHAPTER_PATTERNS:
                match = re.match(pattern, title, re.IGNORECASE)
                if match:
                    chapters.append({
                        'level': level,
                        'title': title,
                        'page': page - 1,  # Convert to 0-indexed
                        'number': match.group(1) if match else None
                    })
                    break

        return chapters

    def detect_chapters_from_text(self, toc_info: dict = None) -> list[Chapter]:
        """
        Detect chapters by analyzing text content.
        Uses TOC info if provided, otherwise scans for chapter headings.
        """
        chapters = []

        # If we have a structured TOC with actual chapters, use it
        if toc_info and toc_info.get('chapters'):
            chapter_starts = toc_info['chapters']
            for i, ch_info in enumerate(chapter_starts):
                start_page = ch_info['start_page']
                end_page = chapter_starts[i + 1]['start_page'] - 1 if i + 1 < len(chapter_starts) else self.get_page_count() - 1

                # Extract text for this chapter
                text_parts = []
                for page_num in range(start_page, end_page + 1):
                    text_parts.append(self.extract_page_text(page_num))

                chapter_text = self.clean_text('\n'.join(text_parts))

                chapters.append(Chapter(
                    number=ch_info.get('number', i + 1),
                    title=ch_info['title'],
                    start_page=start_page,
                    end_page=end_page,
                    text=chapter_text
                ))

        return chapters

    def parse_structured_toc(self, toc_text: str) -> list[dict]:
        """Parse a table of contents from text."""
        chapters = []
        lines = toc_text.strip().split('\n')

        for line in lines:
            # Match patterns like "1 Representation and Computation 3"
            # or "Chapter 1: Introduction 15"
            match = re.match(r'^(\d+)\s+(.+?)\s+(\d+)\s*$', line.strip())
            if match:
                chapters.append({
                    'number': int(match.group(1)),
                    'title': match.group(2).strip(),
                    'start_page': int(match.group(3)) + 13,  # Adjust for front matter offset
                })

        return chapters

    def extract_with_manual_chapters(self, chapter_definitions: list[dict]) -> BookMetadata:
        """
        Extract book with manually defined chapter boundaries.

        chapter_definitions: list of dicts with 'number', 'title', 'start_page'
        """
        chapters = []

        for i, ch_def in enumerate(chapter_definitions):
            start_page = ch_def['start_page']
            # End page is start of next chapter - 1, or end of document
            if i + 1 < len(chapter_definitions):
                end_page = chapter_definitions[i + 1]['start_page'] - 1
            else:
                end_page = self.get_page_count() - 1

            # Extract text for this chapter
            text_parts = []
            for page_num in range(start_page, end_page + 1):
                page_text = self.extract_page_text(page_num)
                text_parts.append(page_text)

            chapter_text = self.clean_text('\n'.join(text_parts))

            chapters.append(Chapter(
                number=ch_def.get('number', i + 1),
                title=ch_def['title'],
                start_page=start_page,
                end_page=end_page,
                text=chapter_text
            ))

        # Try to extract title and author from first pages
        first_page_text = self.extract_page_text(0)
        title = self.pdf_path.stem  # Default to filename
        author = "Unknown"

        # Simple extraction from first page
        lines = first_page_text.strip().split('\n')
        if len(lines) >= 2:
            # Often title and author are in first few lines
            potential_title = ' '.join(lines[:3]).strip()
            if len(potential_title) < 100:  # Sanity check
                title = potential_title

        return BookMetadata(
            title=title,
            author=author,
            chapters=chapters,
            total_pages=self.get_page_count()
        )

    def extract_full_text(self) -> str:
        """Extract all text from the PDF."""
        text_parts = []
        for page_num in range(self.get_page_count()):
            text_parts.append(self.extract_page_text(page_num))
        return self.clean_text('\n'.join(text_parts))


def create_thagard_chapters() -> list[dict]:
    """
    Create chapter definitions for Thagard's "Mind: Introduction to Cognitive Science"
    Based on the TOC found on page 8.
    Page numbers are 0-indexed, and we add offset for front matter.
    """
    # Front matter ends around page 13 (0-indexed), content starts at page 14
    # TOC shows content pages, we need to add offset
    offset = 13  # Adjust based on where actual content starts

    return [
        # Front matter
        {'number': 0, 'title': 'Preface', 'start_page': 8},

        # Part I: Approaches to Cognitive Science
        {'number': 1, 'title': 'Representation and Computation', 'start_page': 3 + offset},
        {'number': 2, 'title': 'Logic', 'start_page': 23 + offset},
        {'number': 3, 'title': 'Rules', 'start_page': 43 + offset},
        {'number': 4, 'title': 'Concepts', 'start_page': 59 + offset},
        {'number': 5, 'title': 'Analogies', 'start_page': 77 + offset},
        {'number': 6, 'title': 'Images', 'start_page': 95 + offset},
        {'number': 7, 'title': 'Connections', 'start_page': 111 + offset},
        {'number': 8, 'title': 'Review and Evaluation', 'start_page': 133 + offset},

        # Part II: Extensions to Cognitive Science
        {'number': 9, 'title': 'Brains', 'start_page': 147 + offset},
        {'number': 10, 'title': 'Emotions', 'start_page': 161 + offset},
        {'number': 11, 'title': 'Consciousness', 'start_page': 175 + offset},
        {'number': 12, 'title': 'Bodies, the World, and Dynamic Systems', 'start_page': 191 + offset},
        {'number': 13, 'title': 'Societies', 'start_page': 205 + offset},
        {'number': 14, 'title': 'The Future of Cognitive Science', 'start_page': 217 + offset},

        # Back matter
        {'number': 15, 'title': 'Glossary', 'start_page': 229 + offset},
        {'number': 16, 'title': 'References', 'start_page': 235 + offset},
    ]


if __name__ == "__main__":
    # Test extraction
    pdf_path = "Thagard.pdf"

    with PDFExtractor(pdf_path) as extractor:
        print(f"Total pages: {extractor.get_page_count()}")

        # Use predefined chapters for Thagard
        chapters = create_thagard_chapters()
        metadata = extractor.extract_with_manual_chapters(chapters)

        print(f"\nBook: {metadata.title}")
        print(f"Author: {metadata.author}")
        print(f"\nChapters found: {len(metadata.chapters)}")

        for ch in metadata.chapters:
            print(f"  {ch.number}. {ch.title} (pp. {ch.start_page}-{ch.end_page}, {len(ch.text)} chars)")
