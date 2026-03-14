"""
Audio Metadata Extraction

Extracts title, author, chapters, and duration from audio file metadata using ffprobe.
"""

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def extract_audio_metadata(audio_path: Path) -> dict:
    """
    Extract metadata from an audio file using ffprobe.

    Supports M4B, M4A, MP3, AAC, and other common audio formats.

    Args:
        audio_path: Path to the audio file

    Returns:
        {
            "title": str or None,
            "author": str or None,
            "album": str or None,
            "duration": float,
            "chapters": [{"title": str, "start": float, "end": float}],
            "format": str
        }
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_chapters",
        str(audio_path)
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        logger.error(f"ffprobe failed: {e.stderr}")
        raise RuntimeError(f"Failed to read audio metadata: {e.stderr}")
    except json.JSONDecodeError:
        logger.error("ffprobe output was not valid JSON")
        raise RuntimeError("Failed to parse audio metadata")

    fmt = data.get("format", {})
    tags = fmt.get("tags", {})

    # Normalize tag keys to lowercase for case-insensitive lookup
    tags_lower = {k.lower(): v for k, v in tags.items()}

    # Extract title (try multiple common tag names)
    title = (
        tags_lower.get("title") or
        tags_lower.get("album") or
        tags_lower.get("name") or
        None
    )

    # Extract author (try multiple common tag names)
    author = (
        tags_lower.get("artist") or
        tags_lower.get("author") or
        tags_lower.get("album_artist") or
        tags_lower.get("composer") or
        None
    )

    # Extract duration
    duration = float(fmt.get("duration", 0))

    # Extract chapters (M4B and some MP3s have chapter markers)
    chapters = []
    for ch in data.get("chapters", []):
        ch_tags = ch.get("tags", {})
        ch_tags_lower = {k.lower(): v for k, v in ch_tags.items()}

        chapter_title = ch_tags_lower.get("title", f"Chapter {len(chapters) + 1}")

        chapters.append({
            "title": chapter_title,
            "start": float(ch.get("start_time", 0)),
            "end": float(ch.get("end_time", 0))
        })

    logger.info(
        f"Extracted metadata from {audio_path.name}: "
        f"title='{title}', author='{author}', duration={duration:.1f}s, "
        f"{len(chapters)} chapters"
    )

    return {
        "title": title,
        "author": author,
        "album": tags_lower.get("album"),
        "duration": duration,
        "chapters": chapters,
        "format": fmt.get("format_name", "unknown")
    }


def mine_metadata_from_text(text: str, segments: list = None) -> dict:
    """
    Attempt to extract title and author from transcribed content.

    Looks for patterns commonly found at the beginning of audiobooks:
    - "This is [Title] by [Author]"
    - "[Title], written by [Author]"
    - "Read by [Narrator]"
    - "[Title] by [Author], narrated by [Narrator]"

    Args:
        text: Full transcription text or intro text
        segments: Optional list of segments (uses first 20 for intro)

    Returns:
        {
            "title": str or None,
            "author": str or None,
            "narrator": str or None
        }
    """
    # If segments provided, use first 20 for intro analysis
    if segments:
        intro_text = " ".join(s["text"] for s in segments[:20])
    else:
        intro_text = text[:2000] if len(text) > 2000 else text

    result = {"title": None, "author": None, "narrator": None}

    # Pattern: "This is [Title] by [Author]"
    match = re.search(
        r'(?:this is|presenting|welcome to)\s+["\']?([^"\']+?)["\']?\s+by\s+([^,.]+)',
        intro_text,
        re.IGNORECASE
    )
    if match:
        result["title"] = match.group(1).strip()
        result["author"] = match.group(2).strip()

    # Pattern: "[Title] by [Author]" at the start
    if not result["title"]:
        match = re.search(
            r'^["\']?([^"\']+?)["\']?\s+by\s+([^,.]+)',
            intro_text.strip(),
            re.IGNORECASE
        )
        if match:
            result["title"] = match.group(1).strip()
            result["author"] = match.group(2).strip()

    # Pattern: "written by [Author]"
    if not result["author"]:
        match = re.search(
            r'written by\s+([^,.]+)',
            intro_text,
            re.IGNORECASE
        )
        if match:
            result["author"] = match.group(1).strip()

    # Pattern: "read by [Narrator]" or "narrated by [Narrator]"
    match = re.search(
        r'(?:read|narrated)\s+by\s+([^,.]+)',
        intro_text,
        re.IGNORECASE
    )
    if match:
        result["narrator"] = match.group(1).strip()

    # Clean up results
    for key in result:
        if result[key]:
            # Remove common artifacts
            result[key] = result[key].strip()
            # Remove trailing punctuation
            result[key] = re.sub(r'[.,;:!?]+$', '', result[key])

    if result["title"] or result["author"]:
        logger.info(f"Mined metadata from text: {result}")

    return result


def get_audio_duration(audio_path: Path) -> float:
    """
    Get the duration of an audio file in seconds.

    Args:
        audio_path: Path to the audio file

    Returns:
        Duration in seconds
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path)
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError) as e:
        logger.error(f"Failed to get audio duration: {e}")
        return 0.0
