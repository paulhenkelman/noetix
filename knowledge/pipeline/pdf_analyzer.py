#!/usr/bin/env python3
"""
PDF Analyzer Module

Extracts book metadata and chapter structure from PDFs using the
gateway /api/analyze endpoint (routes through Codex app-server).
"""

import json
import re
import time
from pathlib import Path
from typing import Optional
import logging

import fitz  # PyMuPDF
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Gateway URL — overridden from knowledge.config via Settings
_gateway_url = None


def _get_gateway_url() -> str:
    global _gateway_url
    if _gateway_url is None:
        try:
            from config.settings import settings
            _gateway_url = settings.gateway_url
        except Exception:
            _gateway_url = "http://localhost:8788"
    return _gateway_url


def _call_analyze(prompt: str, timeout: int = 120) -> str:
    """Send a prompt to the gateway /api/analyze endpoint with 1 retry."""
    url = f"{_get_gateway_url()}/api/analyze"
    last_err = None
    for attempt in range(2):
        try:
            resp = requests.post(
                url,
                json={"prompt": prompt},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("result", "")
        except Exception as e:
            last_err = e
            if attempt == 0:
                logger.warning(f"Gateway analyze call failed (attempt 1): {e}, retrying in 5s...")
                time.sleep(5)
    raise RuntimeError(f"Gateway analyze failed after 2 attempts: {last_err}")


def _parse_json_response(response_text: str) -> dict:
    """Extract JSON object from a model response that may contain markdown."""
    text = response_text.strip()

    # Strip markdown code blocks
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]

    # Find JSON object
    json_match = re.search(r'\{[\s\S]*\}', text)
    if json_match:
        text = json_match.group()

    return json.loads(text)


class PDFAnalyzer:
    """Analyzes PDF books to extract metadata and chapter structure."""

    def __init__(self, ocr_cache_path: Path | str = None):
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

        use_ocr_cache = self.ocr_cache and self.ocr_cache.get("needs_ocr")

        text_parts = []
        pages_to_extract = min(max_pages, total_pages)

        for page_num in range(pages_to_extract):
            if use_ocr_cache:
                from ocr_engine import get_ocr_text_for_page
                text = get_ocr_text_for_page(self.ocr_cache, page_num) or ""
            else:
                page = doc[page_num]
                text = page.get_text()

            text_parts.append(f"=== PAGE {page_num + 1} ===\n{text}")

        doc.close()

        return "\n\n".join(text_parts), total_pages

    def analyze(self, pdf_path: Path | str) -> dict:
        """
        Analyze a PDF and extract metadata and chapter structure.

        Args:
            pdf_path: Path to the PDF file

        Returns:
            Dictionary with title, author, chapters, and optionally structure
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        logger.info(f"Analyzing PDF: {pdf_path.name}")

        sample_text, total_pages = self.extract_sample_text(pdf_path)

        prompt = f"""Analyze this PDF and extract structured metadata. The text includes
the first ~25 pages which should contain the title page, copyright, and TOC.

Total pages in PDF: {total_pages}

TEXT FROM PDF:
{sample_text}

---

Return ONLY a JSON object with this structure:
{{
    "title": "Full book title",
    "author": "Author name(s)",
    "structure": [
        {{
            "level": 0,
            "type": "part|chapter|section|lesson|unit|module",
            "number": 1,
            "title": "Title",
            "start_page": 15,
            "children": []
        }}
    ]
}}

GUIDELINES:
1. Detect the document's NATIVE organizational vocabulary — Chapter, Lesson,
   Unit, Part, Section, Module, Lecture, Topic, or numbered outlines
2. Preserve hierarchical nesting as found in the TOC or inferred from headings
3. Always include level (0-based depth) and start_page (1-indexed PDF page)
4. For documents with no clear TOC, infer structure from heading patterns
5. Include front matter (preface, intro) and back matter (appendix, glossary)
6. Convert book page numbers to PDF page numbers using the observed offset
7. If structure is completely flat (no sub-sections), still use the structure
   format with level=0 for each entry

Return ONLY the JSON object, nothing else."""

        try:
            logger.info("Calling gateway for PDF analysis...")
            response_text = _call_analyze(prompt)

            try:
                result = _parse_json_response(response_text)

                if "title" not in result:
                    result["title"] = pdf_path.stem
                if "author" not in result:
                    result["author"] = "Unknown"

                # Build flat chapters from structure for backward compatibility
                if "structure" in result:
                    result["chapters"] = _flatten_to_chapters(result["structure"])
                elif "chapters" not in result:
                    result["chapters"] = [{"number": 1, "title": pdf_path.stem, "start_page": 1}]

                logger.info(f"Extracted: '{result['title']}' by {result['author']}")
                logger.info(f"Found {len(result['chapters'])} chapters")

                return result

            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse response as JSON: {e}")
                logger.error(f"Response was: {response_text[:500]}")

                return {
                    "title": pdf_path.stem,
                    "author": "Unknown",
                    "chapters": [{"number": 1, "title": pdf_path.stem, "start_page": 1}]
                }

        except Exception as e:
            logger.error(f"PDF analysis failed: {e}")
            return {
                "title": pdf_path.stem,
                "author": "Unknown",
                "chapters": [{"number": 1, "title": pdf_path.stem, "start_page": 1}]
            }

    def analyze_multiple(self, pdf_paths: list[Path]) -> tuple[dict, list[Path]]:
        """
        Analyze multiple PDFs to determine if they belong to the same book,
        their correct order, and extract metadata.

        Returns:
            Tuple of (analysis_dict, ordered_pdf_paths)
        """
        logger.info(f"Analyzing {len(pdf_paths)} PDFs for ordering and metadata")

        pdf_samples = []
        for pdf_path in pdf_paths:
            sample_text, total_pages = self.extract_sample_text(pdf_path, max_pages=10)
            pdf_samples.append({
                "filename": pdf_path.name,
                "path": str(pdf_path),
                "total_pages": total_pages,
                "sample_text": sample_text[:5000]
            })

        prompt = f"""Analyze these {len(pdf_paths)} PDF files. They may be parts of the same book or related documents.

PDF FILES:
"""
        for i, sample in enumerate(pdf_samples):
            prompt += f"""
--- PDF {i+1}: {sample['filename']} ({sample['total_pages']} pages) ---
{sample['sample_text']}
"""

        prompt += """
---

Analyze these PDFs and return ONLY a JSON object with:
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
            logger.info("Calling gateway for multi-PDF analysis...")
            response_text = _call_analyze(prompt, timeout=180)

            analysis = _parse_json_response(response_text)

            # Reorder pdf_paths based on analysis
            ordered_filenames = analysis.get("ordered_files", [p.name for p in pdf_paths])
            filename_to_path = {p.name: p for p in pdf_paths}

            ordered_paths = []
            for filename in ordered_filenames:
                if filename in filename_to_path:
                    ordered_paths.append(filename_to_path[filename])
                else:
                    for name, path in filename_to_path.items():
                        if filename in name or name in filename:
                            ordered_paths.append(path)
                            break

            for path in pdf_paths:
                if path not in ordered_paths:
                    ordered_paths.append(path)

            logger.info(f"Determined order: {[p.name for p in ordered_paths]}")
            logger.info(f"Extracted: '{analysis.get('title')}' by {analysis.get('author')}")

            return analysis, ordered_paths

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse response: {e}")
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
        """Analyze a PDF and save the results to a JSON file."""
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


def _flatten_to_chapters(structure, chapters=None):
    """Convert hierarchical structure to flat chapter list for backward compat."""
    if chapters is None:
        chapters = []
    for node in structure:
        chapters.append({
            "number": node.get("number", len(chapters) + 1),
            "title": node.get("title", "Untitled"),
            "start_page": node.get("start_page", 1)
        })
        if "children" in node and node["children"]:
            _flatten_to_chapters(node["children"], chapters)
    return chapters


def analyze_pdf(pdf_path: str | Path) -> dict:
    """Convenience function to analyze a PDF."""
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
