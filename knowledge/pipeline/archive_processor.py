"""
Archive Processor Module

Extracts ZIP archives and organizes video files for batch processing.
"""

import logging
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Supported file extensions
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.m4v'}
AUDIO_EXTENSIONS = {'.mp3', '.m4a', '.aac', '.m4b', '.wav', '.flac'}
PDF_EXTENSIONS = {'.pdf'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}


@dataclass
class ArchiveContents:
    """Represents contents of an extracted archive."""
    archive_path: Path
    extract_dir: Path
    video_files: list[Path] = field(default_factory=list)
    audio_files: list[Path] = field(default_factory=list)
    pdf_files: list[Path] = field(default_factory=list)
    image_files: list[Path] = field(default_factory=list)
    other_files: list[Path] = field(default_factory=list)
    total_size_bytes: int = 0
    file_count: int = 0


class ArchiveProcessor:
    """
    Handles extraction and organization of archive contents.

    Supports ZIP files containing video lectures and other course materials.
    """

    def __init__(self, temp_base_dir: Path = None):
        """
        Initialize the archive processor.

        Args:
            temp_base_dir: Base directory for extraction. Uses system temp if None.
        """
        self.temp_base_dir = Path(temp_base_dir) if temp_base_dir else Path(tempfile.gettempdir())
        self.temp_base_dir.mkdir(parents=True, exist_ok=True)

    def extract(
        self,
        archive_path: Path,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> ArchiveContents:
        """
        Extract archive and analyze contents.

        Args:
            archive_path: Path to the archive file
            progress_callback: Optional callback(current_file, total_files)

        Returns:
            ArchiveContents with categorized files
        """
        archive_path = Path(archive_path)
        if not archive_path.exists():
            raise FileNotFoundError(f"Archive not found: {archive_path}")

        # Create unique extraction directory
        extract_dir = self.temp_base_dir / f"extract_{archive_path.stem}_{id(self)}"
        extract_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Extracting {archive_path.name} to {extract_dir}")

        try:
            # Extract based on archive type
            if archive_path.suffix.lower() == '.zip':
                self._extract_zip(archive_path, extract_dir, progress_callback)
            else:
                raise ValueError(f"Unsupported archive format: {archive_path.suffix}")

            # Analyze extracted contents
            contents = self._analyze_contents(archive_path, extract_dir)

            logger.info(
                f"Extracted {contents.file_count} files: "
                f"{len(contents.video_files)} videos, "
                f"{len(contents.audio_files)} audio, "
                f"{len(contents.pdf_files)} PDFs"
            )

            return contents

        except Exception as e:
            # Cleanup on failure
            self.cleanup(extract_dir)
            raise

    def _extract_zip(
        self,
        archive_path: Path,
        extract_dir: Path,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ):
        """Extract a ZIP archive."""
        with zipfile.ZipFile(archive_path, 'r') as zf:
            members = zf.namelist()
            total = len(members)

            for i, member in enumerate(members):
                # Skip directories and hidden files
                if member.endswith('/') or member.startswith('__MACOSX'):
                    continue

                # Extract file
                zf.extract(member, extract_dir)

                if progress_callback:
                    progress_callback(i + 1, total)

    def _analyze_contents(
        self,
        archive_path: Path,
        extract_dir: Path
    ) -> ArchiveContents:
        """Analyze extracted files and categorize them."""
        contents = ArchiveContents(
            archive_path=archive_path,
            extract_dir=extract_dir
        )

        # Walk through extracted directory
        for file_path in extract_dir.rglob('*'):
            if not file_path.is_file():
                continue

            # Skip hidden files
            if file_path.name.startswith('.'):
                continue

            contents.file_count += 1
            contents.total_size_bytes += file_path.stat().st_size

            # Categorize by extension
            ext = file_path.suffix.lower()
            if ext in VIDEO_EXTENSIONS:
                contents.video_files.append(file_path)
            elif ext in AUDIO_EXTENSIONS:
                contents.audio_files.append(file_path)
            elif ext in PDF_EXTENSIONS:
                contents.pdf_files.append(file_path)
            elif ext in IMAGE_EXTENSIONS:
                contents.image_files.append(file_path)
            else:
                contents.other_files.append(file_path)

        # Sort video files naturally
        contents.video_files = self.natural_sort_videos(contents.video_files)

        return contents

    def natural_sort_videos(self, video_paths: list[Path]) -> list[Path]:
        """
        Sort video files by natural order.

        Handles cases like: lecture_1.mp4, lecture_2.mp4, lecture_10.mp4
        which should sort as 1, 2, 10 (not 1, 10, 2).
        """
        def natural_key(path: Path) -> list:
            """Generate a key for natural sorting."""
            name = path.stem.lower()
            # Split on numbers and convert numeric parts to integers
            parts = re.split(r'(\d+)', name)
            return [int(part) if part.isdigit() else part for part in parts]

        return sorted(video_paths, key=natural_key)

    def detect_file_types(self, directory: Path) -> dict[str, list[Path]]:
        """
        Classify files in a directory by type.

        Args:
            directory: Directory to scan

        Returns:
            Dict mapping file type to list of paths
        """
        result = {
            'video': [],
            'audio': [],
            'pdf': [],
            'image': [],
            'other': []
        }

        for file_path in Path(directory).rglob('*'):
            if not file_path.is_file() or file_path.name.startswith('.'):
                continue

            ext = file_path.suffix.lower()
            if ext in VIDEO_EXTENSIONS:
                result['video'].append(file_path)
            elif ext in AUDIO_EXTENSIONS:
                result['audio'].append(file_path)
            elif ext in PDF_EXTENSIONS:
                result['pdf'].append(file_path)
            elif ext in IMAGE_EXTENSIONS:
                result['image'].append(file_path)
            else:
                result['other'].append(file_path)

        return result

    def cleanup(self, extract_dir: Path):
        """
        Remove extracted temporary files.

        Args:
            extract_dir: Directory to remove
        """
        if extract_dir and extract_dir.exists():
            try:
                shutil.rmtree(extract_dir)
                logger.info(f"Cleaned up extraction directory: {extract_dir}")
            except Exception as e:
                logger.warning(f"Failed to cleanup {extract_dir}: {e}")

    def get_archive_info(self, archive_path: Path) -> dict:
        """
        Get information about an archive without extracting.

        Args:
            archive_path: Path to archive

        Returns:
            Dict with file count, size, and file list
        """
        archive_path = Path(archive_path)

        if archive_path.suffix.lower() == '.zip':
            with zipfile.ZipFile(archive_path, 'r') as zf:
                members = [m for m in zf.namelist() if not m.endswith('/')]
                total_size = sum(info.file_size for info in zf.infolist())

                # Count by type
                videos = [m for m in members if Path(m).suffix.lower() in VIDEO_EXTENSIONS]

                return {
                    'file_count': len(members),
                    'total_size_bytes': total_size,
                    'video_count': len(videos),
                    'files': members[:50]  # First 50 for preview
                }

        raise ValueError(f"Unsupported archive format: {archive_path.suffix}")
