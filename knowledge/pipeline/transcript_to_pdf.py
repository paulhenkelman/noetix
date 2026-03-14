"""
Transcript to PDF Converter

Creates a searchable PDF document from transcribed audio chapters.
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def create_transcript_pdf(
    chapters: list,
    title: str,
    author: str,
    output_path: Path,
    page_size: str = "letter"
) -> Path:
    """
    Create a PDF document from transcription chapters.

    Args:
        chapters: List of chapter dicts with 'title' and 'text' keys
        title: Document title
        author: Document author
        output_path: Path for the output PDF
        page_size: Page size ("letter" or "a4")

    Returns:
        Path to the created PDF
    """
    try:
        from reportlab.lib.pagesizes import letter, A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, PageBreak,
            TableOfContents
        )
        from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
    except ImportError:
        raise ImportError(
            "reportlab package required. Install with: pip install reportlab"
        )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Select page size
    if page_size.lower() == "a4":
        pagesize = A4
    else:
        pagesize = letter

    # Create document
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=pagesize,
        rightMargin=inch,
        leftMargin=inch,
        topMargin=inch,
        bottomMargin=inch,
        title=title,
        author=author
    )

    # Define styles
    styles = getSampleStyleSheet()

    # Title style
    title_style = ParagraphStyle(
        'BookTitle',
        parent=styles['Title'],
        fontSize=24,
        spaceAfter=12,
        alignment=TA_CENTER
    )

    # Author style
    author_style = ParagraphStyle(
        'BookAuthor',
        parent=styles['Normal'],
        fontSize=14,
        spaceAfter=36,
        alignment=TA_CENTER,
        textColor='#666666'
    )

    # Chapter title style
    chapter_title_style = ParagraphStyle(
        'ChapterTitle',
        parent=styles['Heading1'],
        fontSize=18,
        spaceAfter=18,
        spaceBefore=24
    )

    # Body text style
    body_style = ParagraphStyle(
        'BodyText',
        parent=styles['Normal'],
        fontSize=11,
        leading=16,
        spaceAfter=12,
        alignment=TA_JUSTIFY
    )

    # Build document content
    story = []

    # Title page
    story.append(Spacer(1, 2 * inch))
    story.append(Paragraph(title, title_style))
    story.append(Paragraph(f"by {author}", author_style))
    story.append(Spacer(1, inch))
    story.append(Paragraph("Transcript", styles['Normal']))
    story.append(PageBreak())

    # Chapters
    for i, chapter in enumerate(chapters):
        ch_title = chapter.get("title", f"Chapter {i + 1}")
        ch_text = chapter.get("text", "")

        # Skip empty chapters
        if not ch_text.strip():
            continue

        # Chapter heading
        story.append(Paragraph(ch_title, chapter_title_style))

        # Chapter text - split into paragraphs
        # Use double newlines or very long segments as paragraph breaks
        paragraphs = split_into_paragraphs(ch_text)

        for para in paragraphs:
            if para.strip():
                # Escape special characters for reportlab
                safe_para = escape_for_reportlab(para)
                story.append(Paragraph(safe_para, body_style))

        # Page break between chapters (except last)
        if i < len(chapters) - 1:
            story.append(PageBreak())

    # Build PDF
    doc.build(story)

    logger.info(f"Created transcript PDF: {output_path} ({len(chapters)} chapters)")
    return output_path


def split_into_paragraphs(text: str, max_length: int = 1000) -> list:
    """
    Split text into paragraphs for better readability.

    Args:
        text: Raw transcript text
        max_length: Maximum paragraph length before forcing a break

    Returns:
        List of paragraph strings
    """
    # First, try to split on double newlines
    paragraphs = text.split('\n\n')

    result = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # If paragraph is too long, split on sentences
        if len(para) > max_length:
            sentences = split_into_sentences(para)
            current_para = ""

            for sentence in sentences:
                if len(current_para) + len(sentence) > max_length and current_para:
                    result.append(current_para.strip())
                    current_para = sentence
                else:
                    current_para += " " + sentence if current_para else sentence

            if current_para:
                result.append(current_para.strip())
        else:
            result.append(para)

    return result


def split_into_sentences(text: str) -> list:
    """
    Split text into sentences.

    Args:
        text: Text to split

    Returns:
        List of sentences
    """
    import re

    # Split on sentence-ending punctuation followed by space and capital letter
    # This is a simple approach that works for most English text
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', text)

    return [s.strip() for s in sentences if s.strip()]


def escape_for_reportlab(text: str) -> str:
    """
    Escape special characters for reportlab Paragraph.

    Args:
        text: Raw text

    Returns:
        Escaped text safe for reportlab
    """
    # Replace ampersands first
    text = text.replace('&', '&amp;')
    # Replace less than and greater than
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')

    return text
