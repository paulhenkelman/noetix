#!/usr/bin/env python3
"""
PDF Analyzer Module using Claude Code CLI
Automatically extracts book metadata and chapter structure from PDFs.
"""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
import logging

import fitz  # PyMuPDF

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CLAUDE_CLI_PATH = os.environ.get("CLAUDE_CLI_PATH", "claude")


class PDFAnalyzer:
    """
    Analyzes PDF books using Claude Code CLI to extract metadata and chapter structure.
    """

    def __init__(self, ocr_cache_path: Path | str = None):
        """
        Initialize the analyzer.

        Args:
            ocr_cache_path: Path to pre-computed OCR cache file (from preprocess_pdf_with_ocr)
        """
        self.ocr_cache = None
        if ocr_cache_path:
            from ocr_engine import load_ocr_cache
            self.ocr_cache = load_ocr_cache(ocr_cache_path)
            if self.ocr_cache and self.ocr_cache.get("needs_ocr"):
                logger.info(f"Using OCR cache with {len(self.ocr_cache.get('pages', {}))} pages")

    def extract_sample_text(self, pdf_path: Path, max_pages: int = 25) -> tuple[str, int]:
        """
        Extract text from the beginning of the PDF for analysis.
        Focuses on title page, copyright, and table of contents.
        Uses pre-computed OCR cache if available.

        Returns:
            Tuple of (extracted_text, total_page_count)
        """
        doc = fitz.open(str(pdf_path))
        total_pages = len(doc)

        # Check if we have OCR cache
        use_ocr_cache = self.ocr_cache and self.ocr_cache.get("needs_ocr")

        text_parts = []
        pages_to_extract = min(max_pages, total_pages)

        for page_num in range(pages_to_extract):
            if use_ocr_cache:
                # Use cached OCR text
                from ocr_engine import get_ocr_text_for_page
                text = get_ocr_text_for_page(self.ocr_cache, page_num) or ""
            else:
                # Standard text extraction
                page = doc[page_num]
                text = page.get_text()

            text_parts.append(f"=== PAGE {page_num + 1} ===\n{text}")

        doc.close()

        return "\n\n".join(text_parts), total_pages

    def analyze(self, pdf_path: Path | str) -> dict:
        """
        Analyze a PDF and extract metadata and chapter structure using Claude Code CLI.

        Args:
            pdf_path: Path to the PDF file

        Returns:
            Dictionary with:
            - title: Book title
            - author: Book author
            - chapters: List of chapter definitions
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        logger.info(f"Analyzing PDF: {pdf_path.name}")

        # Extract sample text
        sample_text, total_pages = self.extract_sample_text(pdf_path)

        # Create a temporary file with the prompt and text
        prompt = f"""Analyze this PDF book text and extract structured metadata. The text includes the first ~25 pages which should contain the title page, copyright page, and table of contents.

Total pages in PDF: {total_pages}

TEXT FROM PDF:
{sample_text}

---

Please analyze this text and return ONLY a JSON object (no markdown, no explanation) with the following structure:
{{
    "title": "The full book title",
    "author": "Author name(s)",
    "chapters": [
        {{"number": 0, "title": "Preface", "start_page": 5}},
        {{"number": 1, "title": "Chapter Title", "start_page": 10}},
        ...
    ]
}}

IMPORTANT GUIDELINES:
1. Extract the exact book title from the title page
2. Extract the author name(s) from the title or copyright page
3. Find the Table of Contents and extract ALL chapters with their page numbers
4. The start_page should be the PDF page number (1-indexed as shown in PAGE markers above)
5. Include preface, introduction, and any front matter as chapter 0 or early chapters
6. Include appendices, glossary, references, and index as later chapters if present
7. Convert Roman numeral page numbers to their corresponding PDF page positions
8. If you see page numbers in the TOC like "1", "23", "45" - these are BOOK page numbers, not PDF page numbers. Calculate the offset by finding where page 1 of the book content starts in the PDF.

Return ONLY the JSON object, nothing else."""

        # Write prompt to temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            logger.info("Calling Claude Code CLI for analysis...")

            # Call claude CLI with the prompt
            result = subprocess.run(
                [CLAUDE_CLI_PATH, '-p', prompt, '--output-format', 'text'],
                capture_output=True,
                text=True,
                timeout=120
            )

            if result.returncode != 0:
                logger.error(f"Claude CLI error: {result.stderr}")
                raise RuntimeError(f"Claude CLI failed: {result.stderr}")

            response_text = result.stdout.strip()

            # Try to extract JSON from response
            try:
                # Handle case where response might have markdown code blocks
                if "```json" in response_text:
                    response_text = response_text.split("```json")[1].split("```")[0]
                elif "```" in response_text:
                    response_text = response_text.split("```")[1].split("```")[0]

                # Find JSON object in response
                json_match = re.search(r'\{[\s\S]*\}', response_text)
                if json_match:
                    response_text = json_match.group()

                result = json.loads(response_text)

                # Validate structure
                if "title" not in result:
                    result["title"] = pdf_path.stem
                if "author" not in result:
                    result["author"] = "Unknown"
                if "chapters" not in result:
                    result["chapters"] = [{"number": 1, "title": pdf_path.stem, "start_page": 1}]

                logger.info(f"Extracted: '{result['title']}' by {result['author']}")
                logger.info(f"Found {len(result['chapters'])} chapters")

                return result

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse Claude response as JSON: {e}")
                logger.error(f"Response was: {response_text[:500]}")

                # Return fallback
                return {
                    "title": pdf_path.stem,
                    "author": "Unknown",
                    "chapters": [{"number": 1, "title": pdf_path.stem, "start_page": 1}]
                }

        finally:
            # Cleanup temp file
            os.unlink(prompt_file)

    def analyze_multiple(self, pdf_paths: list[Path]) -> tuple[dict, list[Path]]:
        """
        Analyze multiple PDFs to determine if they belong to the same book,
        their correct order, and extract metadata.

        Args:
            pdf_paths: List of paths to PDF files

        Returns:
            Tuple of (analysis_dict, ordered_pdf_paths)
        """
        logger.info(f"Analyzing {len(pdf_paths)} PDFs for ordering and metadata")

        # Extract sample text from each PDF
        pdf_samples = []
        for pdf_path in pdf_paths:
            sample_text, total_pages = self.extract_sample_text(pdf_path, max_pages=10)
            pdf_samples.append({
                "filename": pdf_path.name,
                "path": str(pdf_path),
                "total_pages": total_pages,
                "sample_text": sample_text[:5000]  # Limit to first 5000 chars per PDF
            })

        # Build prompt for Claude
        prompt = f"""Analyze these {len(pdf_paths)} PDF files. They may be parts of the same book (e.g., chapters split into separate files) or related documents.

PDF FILES:
"""
        for i, sample in enumerate(pdf_samples):
            prompt += f"""
--- PDF {i+1}: {sample['filename']} ({sample['total_pages']} pages) ---
{sample['sample_text']}
"""

        prompt += """
---

Please analyze these PDFs and return ONLY a JSON object (no markdown, no explanation) with:
1. Whether they belong to the same book/document
2. The correct reading order (by filename)
3. Combined metadata

Return this exact JSON structure:
{
    "is_single_book": true,
    "title": "The combined book title",
    "author": "Author name(s)",
    "ordered_files": ["file1.pdf", "file2.pdf", ...],
    "chapters": [
        {"number": 1, "title": "Chapter Title", "start_page": 1, "source_pdf": "file1.pdf"},
        ...
    ]
}

IMPORTANT:
1. ordered_files should list the PDF filenames in the correct reading order
2. For chapters, start_page is relative to the COMBINED document (sum pages from previous PDFs)
3. If PDFs are numbered (part1.pdf, part2.pdf) or have volume numbers, use that for ordering
4. Look at table of contents, chapter numbers, and page numbers to determine order
5. source_pdf indicates which PDF file contains that chapter

Return ONLY the JSON object."""

        try:
            logger.info("Calling Claude Code CLI for multi-PDF analysis...")

            result = subprocess.run(
                [CLAUDE_CLI_PATH, '-p', prompt, '--output-format', 'text'],
                capture_output=True,
                text=True,
                timeout=180  # Longer timeout for multiple PDFs
            )

            if result.returncode != 0:
                logger.error(f"Claude CLI error: {result.stderr}")
                raise RuntimeError(f"Claude CLI failed: {result.stderr}")

            response_text = result.stdout.strip()

            # Parse JSON from response
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0]
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0]

            json_match = re.search(r'\{[\s\S]*\}', response_text)
            if json_match:
                response_text = json_match.group()

            analysis = json.loads(response_text)

            # Reorder pdf_paths based on AI analysis
            ordered_filenames = analysis.get("ordered_files", [p.name for p in pdf_paths])
            filename_to_path = {p.name: p for p in pdf_paths}

            ordered_paths = []
            for filename in ordered_filenames:
                if filename in filename_to_path:
                    ordered_paths.append(filename_to_path[filename])
                else:
                    # Try partial match
                    for name, path in filename_to_path.items():
                        if filename in name or name in filename:
                            ordered_paths.append(path)
                            break

            # Add any missing paths at the end
            for path in pdf_paths:
                if path not in ordered_paths:
                    ordered_paths.append(path)

            logger.info(f"AI determined order: {[p.name for p in ordered_paths]}")
            logger.info(f"Extracted: '{analysis.get('title')}' by {analysis.get('author')}")

            return analysis, ordered_paths

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response: {e}")
            # Return fallback with original order
            return {
                "title": pdf_paths[0].stem,
                "author": "Unknown",
                "chapters": [{"number": 1, "title": pdf_paths[0].stem, "start_page": 1}]
            }, pdf_paths

        except Exception as e:
            logger.error(f"Multi-PDF analysis failed: {e}")
            return {
                "title": pdf_paths[0].stem,
                "author": "Unknown",
                "chapters": [{"number": 1, "title": pdf_paths[0].stem, "start_page": 1}]
            }, pdf_paths

    def analyze_and_save(self, pdf_path: Path | str, output_path: Path | str = None) -> dict:
        """
        Analyze a PDF and save the results to a JSON file.

        Args:
            pdf_path: Path to the PDF file
            output_path: Path to save JSON (default: same name as PDF with .json extension)

        Returns:
            The analysis results dictionary
        """
        pdf_path = Path(pdf_path)
        if output_path is None:
            output_path = pdf_path.with_suffix('.analysis.json')
        else:
            output_path = Path(output_path)

        result = self.analyze(pdf_path)

        with open(output_path, 'w') as f:
            json.dump(result, f, indent=2)

        logger.info(f"Saved analysis to: {output_path}")

        return result


def analyze_pdf(pdf_path: str | Path) -> dict:
    """
    Convenience function to analyze a PDF.

    Args:
        pdf_path: Path to the PDF file

    Returns:
        Analysis results dictionary
    """
    analyzer = PDFAnalyzer()
    return analyzer.analyze(pdf_path)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python pdf_analyzer.py <pdf_path>")
        sys.exit(1)

    pdf_path = sys.argv[1]

    try:
        analyzer = PDFAnalyzer()
        result = analyzer.analyze_and_save(pdf_path)

        print("\n=== ANALYSIS RESULTS ===")
        print(f"Title: {result['title']}")
        print(f"Author: {result['author']}")
        print(f"\nChapters ({len(result['chapters'])}):")
        for ch in result['chapters']:
            print(f"  {ch['number']:2d}. {ch['title']} (page {ch['start_page']})")

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
