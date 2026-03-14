"""
Video to PDF Converter

Creates a PDF document from video content with transcript and embedded screenshots.
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def create_video_pdf(
    chapters: list,
    title: str,
    author: str,
    output_path: Path,
    page_size: str = "letter",
    include_screenshots: bool = True,
    max_image_width: float = 5.0,
    skip_descriptions_with_images: bool = False
) -> Path:
    """
    Create a PDF document from video content with transcript and screenshots.

    Args:
        chapters: List of chapter dicts with 'title', 'text', and 'visuals' keys
                  visuals: [{'timestamp': float, 'description': str, 'frame_path': str}]
        title: Document title
        author: Document author
        output_path: Path for the output PDF
        page_size: Page size ("letter" or "a4")
        include_screenshots: Whether to embed screenshot images
        max_image_width: Maximum width for embedded images in inches
        skip_descriptions_with_images: If True, skip text descriptions when image
                                       is successfully embedded (images speak for themselves)

    Returns:
        Path to the created PDF
    """
    try:
        from reportlab.lib.pagesizes import letter, A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, PageBreak,
            Image, KeepTogether
        )
        from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
        from reportlab.lib.colors import HexColor
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

    # Visual description style
    visual_style = ParagraphStyle(
        'VisualDescription',
        parent=styles['Normal'],
        fontSize=10,
        leading=14,
        spaceAfter=8,
        spaceBefore=4,
        leftIndent=20,
        textColor=HexColor('#333333'),
        backColor=HexColor('#f5f5f5')
    )

    # Timestamp style
    timestamp_style = ParagraphStyle(
        'Timestamp',
        parent=styles['Normal'],
        fontSize=9,
        textColor=HexColor('#666666'),
        spaceAfter=4
    )

    # Caption style for images
    caption_style = ParagraphStyle(
        'ImageCaption',
        parent=styles['Normal'],
        fontSize=9,
        alignment=TA_CENTER,
        textColor=HexColor('#666666'),
        spaceAfter=12
    )

    # Build document content
    story = []

    # Title page
    story.append(Spacer(1, 2 * inch))
    story.append(Paragraph(escape_for_reportlab(title), title_style))
    story.append(Paragraph(f"by {escape_for_reportlab(author)}", author_style))
    story.append(Spacer(1, inch))
    story.append(Paragraph("Video Transcript with Visual Notes", styles['Normal']))
    story.append(PageBreak())

    # Calculate available width for images
    page_width = pagesize[0] - 2 * inch  # Account for margins
    max_img_width = min(max_image_width * inch, page_width)

    # Chapters
    for i, chapter in enumerate(chapters):
        ch_title = chapter.get("title", f"Chapter {i + 1}")
        ch_text = chapter.get("text", "")
        ch_visuals = chapter.get("visuals", [])

        # Skip empty chapters
        if not ch_text.strip() and not ch_visuals:
            continue

        # Chapter heading
        story.append(Paragraph(escape_for_reportlab(ch_title), chapter_title_style))

        # Chapter duration info
        start = chapter.get("start", 0)
        end = chapter.get("end", 0)
        if end > start:
            duration_mins = int((end - start) // 60)
            duration_secs = int((end - start) % 60)
            start_mins = int(start // 60)
            start_secs = int(start % 60)
            time_info = f"[{start_mins}:{start_secs:02d} - Duration: {duration_mins}:{duration_secs:02d}]"
            story.append(Paragraph(time_info, timestamp_style))

        # Chapter text - split into paragraphs
        if ch_text.strip():
            paragraphs = split_into_paragraphs(ch_text)
            for para in paragraphs:
                if para.strip():
                    safe_para = escape_for_reportlab(para)
                    story.append(Paragraph(safe_para, body_style))

        # Visual content section
        if ch_visuals:
            story.append(Spacer(1, 0.3 * inch))
            story.append(Paragraph("<b>Visual Content</b>", styles['Heading3']))

            for visual in ch_visuals:
                timestamp = visual.get('timestamp', 0)
                description = visual.get('description', '')
                frame_path = visual.get('frame_path', '')

                mins = int(timestamp // 60)
                secs = int(timestamp % 60)
                time_label = f"[{mins}:{secs:02d}]"

                visual_elements = []

                # Add timestamp
                visual_elements.append(
                    Paragraph(time_label, timestamp_style)
                )

                # Add screenshot if available and requested
                image_added = False
                if include_screenshots and frame_path and Path(frame_path).exists():
                    try:
                        img = Image(frame_path)
                        # Scale image to fit
                        aspect = img.imageHeight / img.imageWidth
                        img_width = min(max_img_width, img.imageWidth)
                        img_height = img_width * aspect

                        # Limit height too
                        max_height = 4 * inch
                        if img_height > max_height:
                            img_height = max_height
                            img_width = img_height / aspect

                        img.drawWidth = img_width
                        img.drawHeight = img_height

                        visual_elements.append(img)
                        visual_elements.append(
                            Paragraph(f"Frame at {time_label}", caption_style)
                        )
                        image_added = True
                    except Exception as e:
                        logger.warning(f"Could not add image {frame_path}: {e}")

                # Add description (skip if image was added and skip_descriptions_with_images is True)
                if description and not (image_added and skip_descriptions_with_images):
                    safe_desc = escape_for_reportlab(description)
                    visual_elements.append(
                        Paragraph(safe_desc, visual_style)
                    )

                # Keep visual elements together
                story.append(KeepTogether(visual_elements))
                story.append(Spacer(1, 0.2 * inch))

        # Page break between chapters (except last)
        if i < len(chapters) - 1:
            story.append(PageBreak())

    # Build PDF
    doc.build(story)

    logger.info(f"Created video PDF: {output_path} ({len(chapters)} chapters)")
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
