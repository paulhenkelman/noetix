"""
Batch Video Processor Module

Coordinates processing of multiple videos from an archive as a cohesive course.
Combines transcripts, visual content, and chapters with global numbering.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from pipeline.archive_processor import ArchiveContents

logger = logging.getLogger(__name__)


@dataclass
class Visual:
    """A visual content item from a video frame."""
    timestamp: float              # Timestamp within source video
    global_timestamp: float       # Timestamp across all videos
    frame_path: Path
    description: str
    classification: str = "content"  # talking_head, title_slide, content_slide, diagram, code


@dataclass
class EnhancedChapter:
    """Chapter with full time-sync and visual content."""
    number: int                   # Global chapter number
    video_index: int              # Which video this came from (0-based)
    video_title: str              # Source video name
    title: str                    # Chapter title
    start: float                  # Start time (global)
    end: float                    # End time (global)
    local_start: float            # Start time within source video
    local_end: float              # End time within source video
    text: str                     # Transcript text
    visuals: list[Visual] = field(default_factory=list)

    def get_text_for_m4b(self) -> str:
        """
        Get text with visual descriptions for audiobook narration.

        Interleaves visual descriptions at appropriate points in the transcript.
        """
        if not self.visuals:
            return self.text

        # Insert visual descriptions at sentence breaks near timestamps
        result = self.text

        # Sort visuals by timestamp
        sorted_visuals = sorted(self.visuals, key=lambda v: v.timestamp)

        # For each visual, insert "[On screen: ...]" at appropriate point
        # This is a simple approach - could be made smarter
        visual_inserts = []
        for visual in sorted_visuals:
            if visual.classification == "talking_head":
                continue  # Skip talking heads
            if visual.description:
                visual_inserts.append(f"\n\n[On screen: {visual.description}]\n\n")

        if visual_inserts:
            result = result + "\n" + "".join(visual_inserts)

        return result

    def get_content_for_pdf(self) -> tuple[str, list[Visual]]:
        """
        Get text and visual references for PDF generation.

        Returns transcript text and list of visuals to embed.
        """
        # Filter out talking heads for PDF
        pdf_visuals = [
            v for v in self.visuals
            if v.classification != "talking_head"
        ]
        return self.text, pdf_visuals

    def get_content_for_kb(self) -> dict:
        """Get structured content for knowledge base ingestion."""
        return {
            'number': self.number,
            'title': self.title,
            'text': self.text,
            'video_source': self.video_title,
            'video_index': self.video_index,
            'start_time': self.start,
            'end_time': self.end,
            'local_start': self.local_start,
            'local_end': self.local_end,
            'visuals': [
                {
                    'timestamp': v.timestamp,
                    'global_timestamp': v.global_timestamp,
                    'description': v.description,
                    'classification': v.classification,
                    'frame_path': str(v.frame_path)
                }
                for v in self.visuals
            ]
        }


@dataclass
class VideoProcessingResult:
    """Result of processing a single video."""
    video_path: Path
    video_index: int
    video_title: str
    duration: float
    chapters: list[dict]         # Raw chapter data from VideoProcessor
    transcription: dict          # Full transcription
    frame_analyses: list         # Frame analysis results
    audio_path: Optional[Path] = None
    frames_dir: Optional[Path] = None
    success: bool = True
    error: Optional[str] = None


@dataclass
class BatchProcessingResult:
    """Result of processing an entire archive of videos."""
    archive_path: Path
    video_results: list[VideoProcessingResult] = field(default_factory=list)
    combined_chapters: list[EnhancedChapter] = field(default_factory=list)
    total_duration: float = 0.0
    course_title: str = ""
    course_author: str = ""
    video_count: int = 0
    success_count: int = 0
    failed_count: int = 0


class BatchVideoProcessor:
    """
    Processes multiple videos from an archive as a cohesive course.

    Coordinates with VideoProcessor for individual video processing,
    then combines results with global chapter numbering and time offsets.
    """

    def __init__(
        self,
        openai_client,
        stt_engine,
        temp_dir: Path = None,
        frame_interval: int = 60,
        min_scene_gap: int = 10
    ):
        """
        Initialize the batch processor.

        Args:
            openai_client: OpenAI client for Vision API
            stt_engine: STTEngine instance for transcription
            temp_dir: Directory for temporary files
            frame_interval: Max seconds between frame captures (passed to VideoProcessor)
            min_scene_gap: Min seconds between scene-change captures
        """
        self.openai_client = openai_client
        self.stt_engine = stt_engine
        self.temp_dir = Path(temp_dir) if temp_dir else None
        self.frame_interval = frame_interval
        self.min_scene_gap = min_scene_gap

        # Lazy import to avoid circular dependency
        self._video_processor = None

    def _get_video_processor(self):
        """Get or create VideoProcessor instance."""
        if self._video_processor is None:
            from video_processor import VideoProcessor
            self._video_processor = VideoProcessor(
                openai_client=self.openai_client,
                stt_engine=self.stt_engine,
                temp_dir=self.temp_dir,
                frame_interval=self.frame_interval,
                min_scene_gap=self.min_scene_gap
            )
        return self._video_processor

    def process_archive(
        self,
        archive_contents: ArchiveContents,
        progress_callback: Optional[Callable] = None
    ) -> BatchProcessingResult:
        """
        Process all videos in archive.

        Args:
            archive_contents: Extracted archive contents
            progress_callback: Optional callback(video_idx, video_total, stage, current, total, detail)

        Returns:
            BatchProcessingResult with all processed content
        """
        result = BatchProcessingResult(
            archive_path=archive_contents.archive_path,
            video_count=len(archive_contents.video_files)
        )

        if not archive_contents.video_files:
            logger.warning("No video files found in archive")
            return result

        logger.info(f"Processing {len(archive_contents.video_files)} videos from archive")

        video_results = []
        cumulative_duration = 0.0

        for idx, video_path in enumerate(archive_contents.video_files):
            logger.info(f"Processing video {idx + 1}/{len(archive_contents.video_files)}: {video_path.name}")

            # Create progress wrapper for this video
            def video_progress(stage, current, total, detail):
                if progress_callback:
                    progress_callback(
                        idx,
                        len(archive_contents.video_files),
                        stage,
                        current,
                        total,
                        detail
                    )

            try:
                video_result = self._process_single_video(
                    video_path,
                    idx,
                    cumulative_duration,
                    video_progress
                )
                video_results.append(video_result)

                if video_result.success:
                    cumulative_duration += video_result.duration
                    result.success_count += 1
                else:
                    result.failed_count += 1

            except Exception as e:
                logger.error(f"Failed to process video {video_path.name}: {e}")
                video_results.append(VideoProcessingResult(
                    video_path=video_path,
                    video_index=idx,
                    video_title=video_path.stem,
                    duration=0,
                    chapters=[],
                    transcription={},
                    frame_analyses=[],
                    success=False,
                    error=str(e)
                ))
                result.failed_count += 1

        result.video_results = video_results
        result.total_duration = cumulative_duration

        # Combine chapters from all videos
        result.combined_chapters = self._combine_chapters(video_results)

        # Infer course metadata
        result.course_title, result.course_author = self._infer_course_metadata(
            archive_contents,
            video_results
        )

        logger.info(
            f"Processed {result.success_count}/{result.video_count} videos, "
            f"{len(result.combined_chapters)} total chapters, "
            f"{result.total_duration:.1f}s total duration"
        )

        return result

    def _process_single_video(
        self,
        video_path: Path,
        video_index: int,
        time_offset: float,
        progress_callback: Optional[Callable] = None
    ) -> VideoProcessingResult:
        """
        Process a single video.

        Args:
            video_path: Path to video file
            video_index: Index of this video in the batch
            time_offset: Cumulative duration of previous videos
            progress_callback: Optional progress callback

        Returns:
            VideoProcessingResult with processed content
        """
        processor = self._get_video_processor()

        # Process video
        proc_result = processor.process_video(video_path, progress_callback)

        return VideoProcessingResult(
            video_path=video_path,
            video_index=video_index,
            video_title=video_path.stem,
            duration=proc_result.get('duration', 0),
            chapters=proc_result.get('chapters', []),
            transcription=proc_result.get('transcription', {}),
            frame_analyses=proc_result.get('frame_analyses', []),
            audio_path=proc_result.get('audio_path'),
            frames_dir=proc_result.get('frames_dir'),
            success=True
        )

    def _combine_chapters(
        self,
        video_results: list[VideoProcessingResult]
    ) -> list[EnhancedChapter]:
        """
        Combine all video chapters with proper numbering and context.

        Args:
            video_results: List of processed video results

        Returns:
            List of EnhancedChapter objects with global numbering
        """
        combined = []
        global_chapter_num = 1
        cumulative_time = 0.0

        for video_result in video_results:
            if not video_result.success:
                cumulative_time += video_result.duration
                continue

            for chapter in video_result.chapters:
                # Convert visuals to Visual objects with global timestamps
                visuals = []
                for v in chapter.get('visuals', []):
                    visuals.append(Visual(
                        timestamp=v.get('timestamp', 0),
                        global_timestamp=v.get('timestamp', 0) + cumulative_time,
                        frame_path=Path(v.get('frame_path', '')),
                        description=v.get('description', ''),
                        classification=v.get('classification', 'content')
                    ))

                enhanced = EnhancedChapter(
                    number=global_chapter_num,
                    video_index=video_result.video_index,
                    video_title=video_result.video_title,
                    title=chapter.get('title', f'Chapter {global_chapter_num}'),
                    start=chapter.get('start', 0) + cumulative_time,
                    end=chapter.get('end', 0) + cumulative_time,
                    local_start=chapter.get('start', 0),
                    local_end=chapter.get('end', 0),
                    text=chapter.get('text', ''),
                    visuals=visuals
                )
                combined.append(enhanced)
                global_chapter_num += 1

            cumulative_time += video_result.duration

        return combined

    def _infer_course_metadata(
        self,
        archive_contents: ArchiveContents,
        video_results: list[VideoProcessingResult]
    ) -> tuple[str, str]:
        """
        Infer course title and author from archive and video names.

        Args:
            archive_contents: Archive metadata
            video_results: Processed video results

        Returns:
            Tuple of (title, author)
        """
        # Try to get title from archive name
        archive_name = archive_contents.archive_path.stem

        # Clean up common patterns
        title = archive_name
        for pattern in ['_Lectures', '_Videos', '_Course', '-Lectures', '-Videos']:
            title = title.replace(pattern, '')

        # Replace underscores with spaces
        title = title.replace('_', ' ')

        # Author is unknown by default - could be mined from video content
        author = "Unknown"

        # Try to extract from first video's transcription
        if video_results and video_results[0].success:
            text = video_results[0].transcription.get('text', '')[:1000]
            # Look for patterns like "I'm Professor X" or "taught by Y"
            import re
            prof_match = re.search(r"(?:I'm|I am|this is)\s+(?:Professor|Dr\.?)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", text)
            if prof_match:
                author = prof_match.group(1)

        return title, author


def interleave_visual_descriptions(
    transcript: str,
    visuals: list[Visual],
    format_template: str = "[On screen: {description}]"
) -> str:
    """
    Interleave visual descriptions into transcript at appropriate points.

    For M4B audiobook narration - inserts descriptions at sentence breaks.

    Args:
        transcript: Original transcript text
        visuals: List of Visual objects to insert
        format_template: Template for formatting visual description

    Returns:
        Transcript with visual descriptions inserted
    """
    if not visuals:
        return transcript

    # Filter out talking heads and sort by timestamp
    content_visuals = [
        v for v in visuals
        if v.classification != "talking_head" and v.description
    ]

    if not content_visuals:
        return transcript

    # Simple approach: append all visual descriptions at the end
    # A more sophisticated approach would insert at sentence breaks
    # near the visual timestamp

    visual_text = "\n\n"
    for v in sorted(content_visuals, key=lambda x: x.timestamp):
        desc = format_template.format(description=v.description)
        visual_text += f"{desc}\n\n"

    return transcript + visual_text
