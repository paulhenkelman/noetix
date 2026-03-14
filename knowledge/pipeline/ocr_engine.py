"""
OCR Engine Module
GPU-accelerated OCR for scanned PDFs using EasyOCR.
"""

import fitz  # PyMuPDF
import numpy as np
import cv2
import re
from pathlib import Path
from typing import Optional
import logging
import tempfile
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def detect_watermarked_scan(doc: fitz.Document, sample_pages: int = 5) -> tuple[bool, dict]:
    """
    Detect if a PDF is a watermarked scanned document.

    Checks for:
    1. Full-page images (indicates scanned content)
    2. Repetitive text patterns (indicates watermarks)

    Args:
        doc: PyMuPDF document object
        sample_pages: Number of pages to sample

    Returns:
        Tuple of (needs_ocr: bool, info: dict with detection details)
    """
    total_pages = len(doc)
    pages_to_check = min(sample_pages, total_pages)

    # Calculate middle section for sampling (skip first/last 10%)
    start_offset = max(0, total_pages // 10)
    end_offset = max(start_offset + 1, total_pages - start_offset)
    middle_range = end_offset - start_offset

    if middle_range <= 0 or total_pages < 10:
        mid = total_pages // 2
        page_indices = [max(0, mid - 2 + i) for i in range(min(5, total_pages))]
    else:
        step = max(1, middle_range // sample_pages)
        page_indices = [start_offset + (i * step) for i in range(pages_to_check)]
        page_indices = [min(idx, total_pages - 1) for idx in page_indices]

    total_chars = 0
    all_texts = []
    pages_with_full_images = 0

    for page_idx in page_indices:
        page = doc[page_idx]
        text = page.get_text().strip()
        total_chars += len(text)
        all_texts.append(text)

        # Check if page has full-page images (scanned content indicator)
        page_rect = page.rect
        page_area = page_rect.width * page_rect.height
        images = page.get_images()

        for img in images:
            try:
                xref = img[0]
                img_info = doc.extract_image(xref)
                img_area = img_info.get("width", 0) * img_info.get("height", 0)
                if img_area > page_area * 0.5:
                    pages_with_full_images += 1
                    break
            except Exception:
                pass

    avg_chars = total_chars / pages_to_check if pages_to_check > 0 else 0
    info = {
        "avg_chars": avg_chars,
        "pages_checked": pages_to_check,
        "pages_with_full_images": pages_with_full_images,
        "is_repetitive_text": False,
        "uniqueness_ratio": 1.0,
    }

    # Standard check: low text content
    needs_ocr = avg_chars < 100

    # Full-page images on most pages = scanned content, regardless of embedded text
    # Scanned PDFs often have text layers from prior OCR or scanner software that
    # PyMuPDF can extract, but the text is typically low-quality or garbled.
    if not needs_ocr and pages_with_full_images >= pages_to_check * 0.5:
        needs_ocr = True
        info["is_scanned_with_text_layer"] = True

    # Check for repetitive/watermark text
    if not needs_ocr and all_texts:
        combined_text = " ".join(all_texts)
        phrases = re.split(r'[.!?\n]+', combined_text)
        phrases = [p.strip() for p in phrases if p.strip() and len(p.strip()) > 10]
        if phrases:
            unique_phrases = set(phrases)
            uniqueness_ratio = len(unique_phrases) / len(phrases)
            info["uniqueness_ratio"] = uniqueness_ratio

            if uniqueness_ratio < 0.2:
                info["is_repetitive_text"] = True
                # Need OCR if we have full-page images with repetitive text
                if pages_with_full_images >= pages_to_check * 0.5:
                    needs_ocr = True

    return needs_ocr, info


class OCREngine:
    """
    GPU-accelerated OCR engine using EasyOCR.
    Converts scanned PDF pages to text.
    """

    def __init__(self, languages: list[str] = None, gpu: bool = True):
        """
        Initialize the OCR engine.

        Args:
            languages: List of language codes (default: ['en'])
            gpu: Whether to use GPU acceleration
        """
        self.languages = languages or ['en']
        self.gpu = gpu
        self._reader = None
        logger.info(f"OCR Engine initialized (GPU: {gpu}, languages: {self.languages})")

    @property
    def reader(self):
        """Lazy-load the EasyOCR reader."""
        if self._reader is None:
            import easyocr
            logger.info("Loading EasyOCR model (first use)...")
            self._reader = easyocr.Reader(self.languages, gpu=self.gpu)
            logger.info("EasyOCR model loaded")
        return self._reader

    def needs_ocr(self, pdf_path: Path | str, sample_pages: int = 5) -> bool:
        """
        Check if a PDF needs OCR by sampling pages for text content.

        Also detects watermarked scanned PDFs where the only text is repetitive
        watermark content but the actual content is in page-sized images.

        Args:
            pdf_path: Path to the PDF file
            sample_pages: Number of pages to sample

        Returns:
            True if OCR is needed (no extractable text or watermarked scan), False otherwise
        """
        pdf_path = Path(pdf_path)
        doc = fitz.open(str(pdf_path))

        try:
            needs_ocr, info = detect_watermarked_scan(doc, sample_pages)

            if info["is_repetitive_text"] and info["pages_with_full_images"] > 0:
                logger.info(f"PDF appears to be watermarked scanned document "
                           f"({info['pages_with_full_images']}/{info['pages_checked']} pages with full images, "
                           f"{info['uniqueness_ratio']:.1%} unique text)")

            logger.info(f"PDF check: {len(doc)} pages, avg {info['avg_chars']:.0f} chars/page, {info['pages_with_full_images']}/{info['pages_checked']} full-image pages, needs_ocr={needs_ocr}")
            return needs_ocr

        finally:
            doc.close()

    def _preprocess_image(self, img: np.ndarray, denoise: bool = False) -> np.ndarray:
        """
        Preprocess an image for better OCR accuracy.

        Args:
            img: Input image as numpy array (RGB or grayscale)
            denoise: Apply denoising (slower but helps with noisy scans)

        Returns:
            Preprocessed grayscale image
        """
        # Convert to grayscale
        if len(img.shape) == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        else:
            gray = img

        # Apply CLAHE contrast enhancement for faded/uneven text
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray = clahe.apply(gray)

        # Optional denoising
        if denoise:
            gray = cv2.fastNlMeansDenoising(gray, h=10)

        return gray

    def _reconstruct_text(self, results: list, confidence_threshold: float = 0.25) -> str:
        """
        Reconstruct text from OCR results with layout awareness.

        Filters low-confidence detections, sorts by position, groups into lines,
        and inserts paragraph breaks at vertical gaps.

        Args:
            results: EasyOCR results list of [bbox, text, confidence]
            confidence_threshold: Minimum confidence to keep a detection

        Returns:
            Reconstructed text with paragraph structure
        """
        # Filter by confidence
        filtered = [(r[0], r[1], r[2]) for r in results if r[2] >= confidence_threshold]
        if not filtered:
            return ''

        # Calculate vertical center and height for each detection
        detections = []
        for bbox, text, conf in filtered:
            y_coords = [pt[1] for pt in bbox]
            x_coords = [pt[0] for pt in bbox]
            y_center = sum(y_coords) / len(y_coords)
            x_center = sum(x_coords) / len(x_coords)
            height = max(y_coords) - min(y_coords)
            detections.append({
                'text': text,
                'y_center': y_center,
                'x_center': x_center,
                'height': height,
            })

        # Compute median text height for line grouping
        heights = [d['height'] for d in detections if d['height'] > 0]
        median_height = sorted(heights)[len(heights) // 2] if heights else 20

        # Sort by Y then X
        detections.sort(key=lambda d: (d['y_center'], d['x_center']))

        # Group into lines: detections within half median height are on the same line
        lines = []
        current_line = [detections[0]]
        for det in detections[1:]:
            if abs(det['y_center'] - current_line[-1]['y_center']) < median_height * 0.5:
                current_line.append(det)
            else:
                lines.append(current_line)
                current_line = [det]
        lines.append(current_line)

        # Sort words within each line by X position
        for line in lines:
            line.sort(key=lambda d: d['x_center'])

        # Build text with paragraph breaks at large vertical gaps
        output_parts = []
        for i, line in enumerate(lines):
            line_text = ' '.join(d['text'] for d in line)
            if i > 0:
                prev_y = sum(d['y_center'] for d in lines[i - 1]) / len(lines[i - 1])
                curr_y = sum(d['y_center'] for d in line) / len(line)
                gap = curr_y - prev_y
                if gap > median_height * 1.5:
                    output_parts.append('')  # paragraph break
            output_parts.append(line_text)

        return '\n'.join(output_parts)

    def ocr_page(self, page: fitz.Page, dpi: int = 300, auto_rotate: bool = True,
                 confidence_threshold: float = 0.25, denoise: bool = False) -> str:
        """
        Perform OCR on a single PDF page.

        Args:
            page: PyMuPDF page object
            dpi: Resolution for rendering (higher = better quality but slower)
            auto_rotate: Try different rotations if OCR yields poor results
            confidence_threshold: Minimum confidence to keep a detection (0.0-1.0)
            denoise: Apply denoising preprocessing (slower but helps noisy scans)

        Returns:
            Extracted text from the page
        """
        # Render page to image
        pix = page.get_pixmap(dpi=dpi)

        # Convert to numpy array for EasyOCR
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)

        # If RGBA, convert to RGB
        if pix.n == 4:
            img = img[:, :, :3]

        # Preprocess for better OCR
        img = self._preprocess_image(img, denoise=denoise)

        # Run OCR
        results = self.reader.readtext(img)
        text = self._reconstruct_text(results, confidence_threshold=confidence_threshold)

        # If we got very little text and auto_rotate is enabled, try rotations
        if auto_rotate and len(text.strip()) < 50:
            best_text = text
            best_len = len(text.strip())

            # Try 90°, 180°, 270° rotations
            for rotation in [90, 180, 270]:
                rotated_img = np.rot90(img, k=rotation // 90)
                rot_results = self.reader.readtext(rotated_img)
                rot_text = self._reconstruct_text(rot_results, confidence_threshold=confidence_threshold)

                if len(rot_text.strip()) > best_len:
                    best_text = rot_text
                    best_len = len(rot_text.strip())
                    logger.info(f"[OCR] Rotation {rotation}° gave better results ({best_len} chars)")

                # If we found good text, stop trying
                if best_len > 200:
                    break

            return best_text

        return text

    def ocr_pdf(self, pdf_path: Path | str, dpi: int = 300,
                progress_callback: callable = None) -> dict[int, str]:
        """
        Perform OCR on an entire PDF.

        Args:
            pdf_path: Path to the PDF file
            dpi: Resolution for rendering
            progress_callback: Optional callback(page_num, total_pages)

        Returns:
            Dictionary mapping page numbers (0-indexed) to extracted text
        """
        pdf_path = Path(pdf_path)
        doc = fitz.open(str(pdf_path))
        results = {}

        try:
            total_pages = len(doc)
            logger.info(f"Starting OCR on {total_pages} pages...")

            for page_num in range(total_pages):
                page = doc[page_num]
                text = self.ocr_page(page, dpi=dpi)
                results[page_num] = text

                if progress_callback:
                    progress_callback(page_num + 1, total_pages)

                if (page_num + 1) % 10 == 0:
                    logger.info(f"OCR progress: {page_num + 1}/{total_pages} pages")

            logger.info(f"OCR complete: {total_pages} pages processed")
            return results

        finally:
            doc.close()

    def ocr_pdf_to_text_pdf(self, input_path: Path | str, output_path: Path | str = None,
                            dpi: int = 300, progress_callback: callable = None) -> Path:
        """
        Convert a scanned PDF to a searchable PDF using ocrmypdf.

        Uses ocrmypdf + tesseract to add a properly positioned invisible text
        layer behind each page image, producing a genuinely searchable PDF.

        Args:
            input_path: Path to input scanned PDF
            output_path: Path for output PDF (default: input_path with _ocr suffix)
            dpi: Resolution for OCR
            progress_callback: Optional callback(page_num, total_pages)

        Returns:
            Path to the output PDF with OCR text layer
        """
        import ocrmypdf

        input_path = Path(input_path)
        if output_path is None:
            output_path = input_path.with_stem(input_path.stem + "_ocr")
        output_path = Path(output_path)

        doc = fitz.open(str(input_path))
        total_pages = len(doc)
        doc.close()

        logger.info(f"Running ocrmypdf on {total_pages} pages at {dpi} DPI...")

        result = ocrmypdf.ocr(
            input_path,
            output_path,
            image_dpi=dpi,
            force_ocr=True,       # Re-OCR even if text layer exists (old layer is garbage)
            optimize=1,           # Light optimization to keep file size down
            progress_bar=False,   # We handle progress ourselves
        )

        if result != ocrmypdf.ExitCode.ok:
            raise RuntimeError(f"ocrmypdf failed with exit code: {result}")

        logger.info(f"OCR PDF saved to: {output_path}")
        return output_path


def check_pdf_needs_ocr(pdf_path: Path | str) -> bool:
    """
    Quick check if a PDF needs OCR.

    Args:
        pdf_path: Path to PDF file

    Returns:
        True if OCR is needed
    """
    engine = OCREngine()
    return engine.needs_ocr(pdf_path)


def ocr_pdf_to_text(pdf_path: Path | str, dpi: int = 300) -> str:
    """
    Convenience function to extract all text from a scanned PDF.

    Args:
        pdf_path: Path to PDF file
        dpi: Resolution for OCR

    Returns:
        All extracted text concatenated
    """
    engine = OCREngine()
    page_texts = engine.ocr_pdf(pdf_path, dpi=dpi)
    return '\n\n'.join(page_texts.values())


def preprocess_pdf_with_ocr(pdf_path: Path | str, cache_path: Path | str = None,
                            dpi: int = 300, progress_callback: callable = None) -> Path:
    """
    Pre-process a scanned PDF with OCR and cache results.

    This should be called once before analysis/extraction to avoid
    running OCR multiple times.

    Args:
        pdf_path: Path to PDF file
        cache_path: Path to save OCR cache (default: pdf_path.ocr_cache.json)
        dpi: Resolution for OCR
        progress_callback: Optional callback(page_num, total_pages, status_msg)

    Returns:
        Path to the OCR cache file
    """
    import json

    pdf_path = Path(pdf_path)
    if cache_path is None:
        cache_path = pdf_path.with_suffix('.ocr_cache.json')
    cache_path = Path(cache_path)

    # Check if cache already exists
    if cache_path.exists():
        logger.info(f"OCR cache already exists: {cache_path}")
        return cache_path

    engine = OCREngine(gpu=True)

    # Check if OCR is needed
    if not engine.needs_ocr(pdf_path):
        logger.info("PDF has extractable text, OCR not needed")
        # Create empty cache to indicate no OCR needed
        with open(cache_path, 'w') as f:
            json.dump({"needs_ocr": False}, f)
        return cache_path

    logger.info(f"Starting OCR pre-processing for {pdf_path.name}...")

    # Run OCR on all pages
    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)
    ocr_results = {
        "needs_ocr": True,
        "total_pages": total_pages,
        "pages": {}
    }

    try:
        for page_num in range(total_pages):
            if progress_callback:
                progress_callback(page_num + 1, total_pages, f"OCR page {page_num + 1}/{total_pages}")

            page = doc[page_num]
            text = engine.ocr_page(page, dpi=dpi)
            ocr_results["pages"][str(page_num)] = text

            if (page_num + 1) % 25 == 0:
                logger.info(f"OCR progress: {page_num + 1}/{total_pages} pages")

        # Save cache
        with open(cache_path, 'w') as f:
            json.dump(ocr_results, f)

        logger.info(f"OCR complete. Cache saved to: {cache_path}")
        return cache_path

    finally:
        doc.close()


def load_ocr_cache(cache_path: Path | str) -> dict | None:
    """Load OCR cache from file."""
    import json

    cache_path = Path(cache_path)
    if not cache_path.exists():
        return None

    with open(cache_path) as f:
        return json.load(f)


def get_ocr_text_for_page(cache: dict, page_num: int) -> str | None:
    """Get OCR text for a specific page from cache."""
    if not cache or not cache.get("needs_ocr"):
        return None
    return cache.get("pages", {}).get(str(page_num))


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python ocr_engine.py <pdf_path>")
        sys.exit(1)

    pdf_path = sys.argv[1]
    engine = OCREngine()

    if engine.needs_ocr(pdf_path):
        print(f"PDF needs OCR: {pdf_path}")
        print("Running OCR...")
        text = ocr_pdf_to_text(pdf_path)
        print(f"\nExtracted {len(text)} characters")
        print("\nFirst 1000 characters:")
        print(text[:1000])
    else:
        print(f"PDF has extractable text, OCR not needed: {pdf_path}")
