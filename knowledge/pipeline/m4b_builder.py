"""
M4B Audiobook Builder Module
Creates M4B audiobooks with chapter markers and metadata.
"""

import subprocess
import json
import os
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Callable
from concurrent.futures import ThreadPoolExecutor, Future
import threading
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class AACConverter:
    """
    Handles parallel AAC conversion alongside TTS generation.
    Multiple conversions run concurrently using a thread pool.
    """

    def __init__(self, temp_dir: Path, max_workers: int = None, bitrate: str = "128k"):
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.bitrate = bitrate

        # Default to CPU count for parallel AAC encoding
        if max_workers is None:
            import os
            max_workers = min(os.cpu_count() or 4, 8)  # Cap at 8 to avoid thrashing

        self.max_workers = max_workers
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.futures: dict[str, Future] = {}
        self.converted_files: dict[str, Path] = {}
        self.lock = threading.Lock()
        self.active_count = 0

        logger.info(f"[AAC] Initialized parallel converter with {max_workers} workers")

    def submit_conversion(self, wav_path: Path, callback: Callable = None) -> Future:
        """
        Submit a WAV file for parallel AAC conversion.
        Returns immediately - conversion runs in thread pool.
        """
        aac_path = self.temp_dir / f"{wav_path.stem}.m4a"

        def convert():
            with self.lock:
                self.active_count += 1
                active = self.active_count

            logger.info(f"[AAC] Starting: {wav_path.name} ({active} active conversions)")
            try:
                subprocess.run([
                    'ffmpeg', '-y', '-i', str(wav_path),
                    '-c:a', 'aac', '-b:a', self.bitrate,
                    '-ar', '44100',
                    str(aac_path)
                ], check=True, capture_output=True)

                with self.lock:
                    self.converted_files[str(wav_path)] = aac_path
                    self.active_count -= 1
                    active = self.active_count

                logger.info(f"[AAC] Completed: {wav_path.name} ({active} still active)")

                if callback:
                    callback(wav_path, aac_path)

                return aac_path
            except Exception as e:
                with self.lock:
                    self.active_count -= 1
                logger.error(f"[AAC] Failed {wav_path.name}: {e}")
                raise

        future = self.executor.submit(convert)
        with self.lock:
            self.futures[str(wav_path)] = future
            pending = len([f for f in self.futures.values() if not f.done()])

        logger.info(f"[AAC] Queued: {wav_path.name} ({pending} in queue)")
        return future

    def get_aac_path(self, wav_path: Path) -> Path:
        """Get the AAC path for a WAV file (waits if conversion in progress)."""
        wav_key = str(wav_path)

        with self.lock:
            if wav_key in self.converted_files:
                return self.converted_files[wav_key]

            if wav_key in self.futures:
                future = self.futures[wav_key]
            else:
                return None

        # Wait for conversion to complete
        return future.result()

    def wait_all(self) -> list[Path]:
        """Wait for all pending conversions to complete."""
        with self.lock:
            futures = list(self.futures.values())

        results = []
        for future in futures:
            try:
                results.append(future.result())
            except Exception as e:
                logger.error(f"Conversion failed: {e}")

        return results

    def shutdown(self):
        """Shutdown the executor."""
        self.executor.shutdown(wait=True)


@dataclass
class ChapterMarker:
    """Represents a chapter in the audiobook."""
    title: str
    start_ms: int
    end_ms: int


@dataclass
class AudiobookMetadata:
    """Metadata for the audiobook."""
    title: str
    author: str
    narrator: str
    year: Optional[str] = None
    description: Optional[str] = None
    cover_image: Optional[Path] = None


class M4BBuilder:
    """
    Builds M4B audiobooks from WAV files with chapter markers.
    Uses ffmpeg for audio processing.
    """

    def __init__(self, output_dir: Path | str):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = self.output_dir / "temp"
        self.temp_dir.mkdir(exist_ok=True)

    def get_audio_duration_ms(self, audio_path: Path) -> int:
        """Get duration of audio file in milliseconds."""
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', str(audio_path)],
            capture_output=True, text=True
        )
        duration_sec = float(result.stdout.strip())
        return int(duration_sec * 1000)

    def create_chapter_file(self, chapters: list[ChapterMarker], output_path: Path):
        """Create ffmpeg chapters metadata file."""
        with open(output_path, 'w') as f:
            f.write(";FFMETADATA1\n")
            for chapter in chapters:
                f.write("\n[CHAPTER]\n")
                f.write("TIMEBASE=1/1000\n")
                f.write(f"START={chapter.start_ms}\n")
                f.write(f"END={chapter.end_ms}\n")
                # Escape special characters in title
                title = chapter.title.replace('=', '\\=').replace(';', '\\;').replace('#', '\\#').replace('\\', '\\\\')
                f.write(f"title={title}\n")

    def convert_wav_to_aac(self, wav_path: Path, aac_path: Path, bitrate: str = "128k"):
        """Convert WAV to AAC format."""
        logger.info(f"Converting {wav_path.name} to AAC...")
        subprocess.run([
            'ffmpeg', '-y', '-i', str(wav_path),
            '-c:a', 'aac', '-b:a', bitrate,
            '-ar', '44100',  # Standard sample rate
            str(aac_path)
        ], check=True, capture_output=True)

    def concatenate_audio_files(self, audio_files: list[Path], output_path: Path) -> list[ChapterMarker]:
        """
        Concatenate multiple audio files and return chapter markers.

        Returns:
            List of ChapterMarker objects with timing information
        """
        # Create concat file for ffmpeg
        concat_file = self.temp_dir / "concat_list.txt"
        chapters = []
        current_position_ms = 0

        with open(concat_file, 'w') as f:
            for audio_file in audio_files:
                # Escape single quotes in path
                escaped_path = str(audio_file).replace("'", "'\\''")
                f.write(f"file '{escaped_path}'\n")

                # Get duration and create chapter marker
                duration_ms = self.get_audio_duration_ms(audio_file)

                # Extract chapter info from filename
                # Expected format: chapter_01_Title_Here.wav
                stem = audio_file.stem
                match = re.match(r'chapter_(\d+)_(.+)', stem)
                if match:
                    chapter_num = int(match.group(1))
                    title = match.group(2).replace('_', ' ')
                    chapter_title = f"Chapter {chapter_num}: {title}"
                else:
                    chapter_title = stem

                chapters.append(ChapterMarker(
                    title=chapter_title,
                    start_ms=current_position_ms,
                    end_ms=current_position_ms + duration_ms
                ))

                current_position_ms += duration_ms

        # Concatenate files
        logger.info(f"Concatenating {len(audio_files)} audio files...")
        subprocess.run([
            'ffmpeg', '-y', '-f', 'concat', '-safe', '0',
            '-i', str(concat_file),
            '-c', 'copy',
            str(output_path)
        ], check=True, capture_output=True)

        return chapters

    def build_m4b(self, chapter_audio_files: list[Path], metadata: AudiobookMetadata,
                  output_filename: str = None, aac_converter: AACConverter = None) -> Path:
        """
        Build a complete M4B audiobook from chapter audio files.

        Args:
            chapter_audio_files: List of WAV/AAC files, one per chapter
            metadata: Audiobook metadata
            output_filename: Output filename (without extension)
            aac_converter: Optional AACConverter with pre-converted files

        Returns:
            Path to the generated M4B file
        """
        if not output_filename:
            safe_title = re.sub(r'[^\w\s-]', '', metadata.title)[:50]
            output_filename = safe_title.replace(' ', '_')

        # Step 1: Get AAC files (use pre-converted if available)
        aac_files = []
        for wav_file in chapter_audio_files:
            if wav_file.suffix.lower() == '.wav':
                # Check if already converted by background process
                if aac_converter:
                    aac_path = aac_converter.get_aac_path(wav_file)
                    if aac_path and aac_path.exists():
                        logger.info(f"Using pre-converted: {aac_path.name}")
                        aac_files.append(aac_path)
                        continue

                # Fall back to synchronous conversion
                aac_path = self.temp_dir / f"{wav_file.stem}.m4a"
                self.convert_wav_to_aac(wav_file, aac_path)
                aac_files.append(aac_path)
            else:
                aac_files.append(wav_file)

        # Step 2: Concatenate all audio files
        combined_audio = self.temp_dir / "combined.m4a"
        chapters = self.concatenate_audio_files(aac_files, combined_audio)

        logger.info(f"Created {len(chapters)} chapter markers")

        # Step 3: Create chapters metadata file
        chapters_file = self.temp_dir / "chapters.txt"
        self.create_chapter_file(chapters, chapters_file)

        # Step 4: Create final M4B with metadata and chapters
        output_path = self.output_dir / f"{output_filename}.m4b"

        ffmpeg_cmd = [
            'ffmpeg', '-y',
            '-i', str(combined_audio),
            '-i', str(chapters_file),
            '-map_metadata', '1',
            '-c', 'copy',
            '-metadata', f'title={metadata.title}',
            '-metadata', f'artist={metadata.narrator}',
            '-metadata', f'album={metadata.title}',
            '-metadata', f'album_artist={metadata.author}',
            '-metadata', f'composer={metadata.author}',
            '-metadata', 'genre=Audiobook',
        ]

        if metadata.year:
            ffmpeg_cmd.extend(['-metadata', f'date={metadata.year}'])

        if metadata.description:
            ffmpeg_cmd.extend(['-metadata', f'description={metadata.description}'])

        # Add cover image if provided
        if metadata.cover_image and metadata.cover_image.exists():
            ffmpeg_cmd.extend([
                '-i', str(metadata.cover_image),
                '-map', '0:a', '-map', '2:v',
                '-c:v', 'copy',
                '-disposition:v', 'attached_pic'
            ])

        ffmpeg_cmd.append(str(output_path))

        logger.info("Building final M4B file...")
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)

        if result.returncode != 0:
            logger.error(f"FFmpeg error: {result.stderr}")
            raise RuntimeError(f"Failed to create M4B: {result.stderr}")

        logger.info(f"Successfully created: {output_path}")

        # Cleanup temp files
        self._cleanup_temp()

        return output_path

    def _cleanup_temp(self):
        """Clean up temporary files."""
        import shutil
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)
            self.temp_dir.mkdir(exist_ok=True)

    def convert_audio_to_m4b(
        self,
        source_path: Path,
        output_path: Path,
        title: str = None,
        author: str = None,
        chapters: list = None,
        bitrate: str = "128k"
    ) -> Path:
        """
        Convert an audio file (MP3, AAC, M4A) to M4B with chapter markers.

        Args:
            source_path: Path to source audio file
            output_path: Path for output M4B file
            title: Book title (for metadata)
            author: Book author (for metadata)
            chapters: List of chapter dicts with 'title', 'start', 'end' keys (times in seconds)
            bitrate: Audio bitrate for encoding

        Returns:
            Path to created M4B file
        """
        source_path = Path(source_path)
        output_path = Path(output_path)

        if not source_path.exists():
            raise FileNotFoundError(f"Source audio not found: {source_path}")

        # Default metadata
        if not title:
            title = source_path.stem
        if not author:
            author = "Unknown"

        # Convert source to AAC if needed (M4B requires AAC)
        source_ext = source_path.suffix.lower()
        if source_ext in ['.mp3', '.aac', '.wav']:
            aac_path = self.temp_dir / "converted.m4a"
            logger.info(f"Converting {source_path.name} to AAC...")
            subprocess.run([
                'ffmpeg', '-y', '-i', str(source_path),
                '-c:a', 'aac', '-b:a', bitrate,
                '-ar', '44100',
                str(aac_path)
            ], check=True, capture_output=True)
            audio_source = aac_path
        else:
            # Assume already AAC compatible
            audio_source = source_path

        # Build ffmpeg command
        ffmpeg_cmd = [
            'ffmpeg', '-y',
            '-i', str(audio_source),
        ]

        # Create and add chapters if provided
        if chapters and len(chapters) > 0:
            chapters_file = self.temp_dir / "chapters.txt"
            chapter_markers = []

            for i, ch in enumerate(chapters):
                start_ms = int(ch.get("start", 0) * 1000)
                end_ms = int(ch.get("end", 0) * 1000)
                ch_title = ch.get("title", f"Chapter {i + 1}")

                chapter_markers.append(ChapterMarker(
                    title=ch_title,
                    start_ms=start_ms,
                    end_ms=end_ms
                ))

            self.create_chapter_file(chapter_markers, chapters_file)

            ffmpeg_cmd.extend([
                '-i', str(chapters_file),
                '-map_metadata', '1',
            ])

        # Add audio codec and metadata
        ffmpeg_cmd.extend([
            '-c', 'copy',
            '-metadata', f'title={title}',
            '-metadata', f'artist={author}',
            '-metadata', f'album={title}',
            '-metadata', f'album_artist={author}',
            '-metadata', f'composer={author}',
            '-metadata', 'genre=Audiobook',
            str(output_path)
        ])

        logger.info(f"Building M4B: {output_path.name}")
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)

        if result.returncode != 0:
            logger.error(f"FFmpeg error: {result.stderr}")
            raise RuntimeError(f"Failed to create M4B: {result.stderr}")

        logger.info(f"Successfully created M4B: {output_path}")

        # Cleanup temp files
        self._cleanup_temp()

        return output_path

    def get_m4b_info(self, m4b_path: Path) -> dict:
        """Get information about an M4B file."""
        result = subprocess.run([
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_format', '-show_chapters',
            str(m4b_path)
        ], capture_output=True, text=True)

        return json.loads(result.stdout)


def create_m4b_from_chapters(
    chapter_files: list[Path],
    title: str,
    author: str,
    output_dir: Path,
    narrator: str = "AI Narrator",
    year: str = None,
    cover_image: Path = None
) -> Path:
    """
    Convenience function to create an M4B from chapter audio files.

    Args:
        chapter_files: List of chapter audio files (WAV or M4A)
        title: Book title
        author: Book author
        output_dir: Output directory
        narrator: Narrator name
        year: Publication year
        cover_image: Path to cover image

    Returns:
        Path to created M4B file
    """
    builder = M4BBuilder(output_dir)

    metadata = AudiobookMetadata(
        title=title,
        author=author,
        narrator=narrator,
        year=year,
        cover_image=cover_image
    )

    return builder.build_m4b(chapter_files, metadata)


if __name__ == "__main__":
    # Test the M4B builder
    print("M4B Builder module loaded successfully")
    print("Use create_m4b_from_chapters() to build audiobooks")
