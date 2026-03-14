"""
Video Processor Module

Extracts audio for STT and frames for GPT-5.1 Vision analysis.
Merges transcript and visual descriptions by timestamp.
"""

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class FrameClassification(str, Enum):
    """Classification of frame content type."""
    TALKING_HEAD = "talking_head"      # Just a person talking - skip entirely
    TITLE_SLIDE = "title_slide"        # Title/intro slide - capture text only
    CONTENT_SLIDE = "content_slide"    # Educational slide content
    DIAGRAM = "diagram"                # Visual diagram, chart, graph
    CODE = "code"                      # Programming code display
    DEMONSTRATION = "demonstration"    # Physical demonstration or example
    OTHER = "other"                    # Other visual content


@dataclass
class FrameAnalysis:
    """Result of analyzing a single video frame."""
    timestamp: float
    frame_path: Path
    has_visual_content: bool
    description: str
    classification: FrameClassification = FrameClassification.OTHER
    phash: Optional[str] = None  # Perceptual hash for deduplication


@dataclass
class VideoSegment:
    """A segment of video content with transcript and optional visual description."""
    start: float
    end: float
    transcript: str
    visual_description: Optional[str] = None


class VideoProcessor:
    """
    Processes video files by extracting audio and frames,
    transcribing audio, and analyzing frames with GPT-5.1 Vision.
    """

    def __init__(
        self,
        openai_client,
        stt_engine,
        temp_dir: Path = None,
        frame_interval: int = 60,
        min_scene_gap: int = 10,
        scene_threshold: float = 0.3,
        model: str = None
    ):
        """
        Initialize the video processor.

        Args:
            openai_client: OpenAI client for Vision API
            stt_engine: STTEngine instance for transcription
            temp_dir: Directory for temporary files
            frame_interval: Maximum seconds between frame captures
            min_scene_gap: Minimum seconds between scene-change captures
            scene_threshold: ffmpeg scene change detection threshold (0-1)
            model: OpenAI model for Vision API (default from EXTRACTION_MODEL env)
        """
        self.openai_client = openai_client
        self.stt_engine = stt_engine
        self.temp_dir = Path(temp_dir) if temp_dir else Path(tempfile.mkdtemp())
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.model = model or os.environ.get("EXTRACTION_MODEL", "gpt-5.1")

        self.frame_interval = frame_interval
        self.min_scene_gap = min_scene_gap
        self.scene_threshold = scene_threshold

    def extract_audio(self, video_path: Path, output_path: Path = None) -> Path:
        """
        Extract audio from video file.

        Args:
            video_path: Path to input video
            output_path: Path for output audio (default: temp dir)

        Returns:
            Path to extracted audio file (WAV format)
        """
        video_path = Path(video_path)
        if not video_path.exists():
            raise FileNotFoundError(f"Video not found: {video_path}")

        if output_path is None:
            output_path = self.temp_dir / f"{video_path.stem}_audio.wav"

        logger.info(f"Extracting audio from {video_path.name}")

        cmd = [
            'ffmpeg', '-y', '-i', str(video_path),
            '-vn',  # No video
            '-acodec', 'pcm_s16le',  # WAV format
            '-ar', '16000',  # 16kHz for Whisper
            '-ac', '1',  # Mono
            str(output_path)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"Audio extraction failed: {result.stderr}")
            raise RuntimeError(f"Failed to extract audio: {result.stderr}")

        logger.info(f"Audio extracted: {output_path}")
        return output_path

    def get_video_duration(self, video_path: Path) -> float:
        """Get video duration in seconds."""
        cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            str(video_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return float(result.stdout.strip())

    def extract_frames_scene_change(
        self,
        video_path: Path,
        output_dir: Path = None,
        progress_callback: Callable = None
    ) -> list[tuple[float, Path]]:
        """
        Extract frames at scene changes and regular intervals.

        Strategy:
        - Extract frame when scene change detected (threshold 0.3)
        - AND at least min_scene_gap seconds since last frame
        - OR frame_interval seconds since last frame (guaranteed capture)

        Args:
            video_path: Path to input video
            output_dir: Directory for output frames
            progress_callback: Optional callback(current, total) for progress

        Returns:
            List of (timestamp_seconds, frame_path) tuples
        """
        video_path = Path(video_path)
        if output_dir is None:
            output_dir = self.temp_dir / "frames"
        output_dir.mkdir(parents=True, exist_ok=True)

        duration = self.get_video_duration(video_path)
        logger.info(f"Extracting frames from {video_path.name} ({duration:.1f}s)")

        # Step 1: Detect scene changes
        scene_cmd = [
            'ffprobe', '-v', 'quiet',
            '-show_entries', 'frame=pts_time',
            '-select_streams', 'v:0',
            '-of', 'json',
            '-f', 'lavfi',
            f"movie={str(video_path)},select='gt(scene,{self.scene_threshold})'"
        ]

        result = subprocess.run(scene_cmd, capture_output=True, text=True)
        scene_times = []

        if result.returncode == 0 and result.stdout.strip():
            try:
                data = json.loads(result.stdout)
                for frame in data.get('frames', []):
                    pts = frame.get('pts_time')
                    if pts:
                        scene_times.append(float(pts))
            except json.JSONDecodeError:
                logger.warning("Could not parse scene detection output")

        logger.info(f"Detected {len(scene_times)} scene changes")

        # Step 2: Build frame extraction times
        # Include scene changes (with min gap) and interval captures
        frame_times = []
        last_time = -self.min_scene_gap

        for t in sorted(scene_times):
            if t - last_time >= self.min_scene_gap:
                frame_times.append(t)
                last_time = t

        # Add interval captures to ensure coverage
        interval_time = 0.0
        while interval_time < duration:
            # Check if we need an interval capture (no scene capture nearby)
            needs_capture = True
            for ft in frame_times:
                if abs(ft - interval_time) < self.min_scene_gap:
                    needs_capture = False
                    break

            if needs_capture:
                frame_times.append(interval_time)

            interval_time += self.frame_interval

        frame_times = sorted(set(frame_times))
        logger.info(f"Will extract {len(frame_times)} frames")

        # Step 3: Extract frames
        frames = []
        for i, timestamp in enumerate(frame_times):
            frame_path = output_dir / f"frame_{i:04d}_{timestamp:.2f}.jpg"

            cmd = [
                'ffmpeg', '-y',
                '-ss', str(timestamp),
                '-i', str(video_path),
                '-vframes', '1',
                '-q:v', '2',  # High quality JPEG
                str(frame_path)
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0 and frame_path.exists():
                frames.append((timestamp, frame_path))

            if progress_callback:
                progress_callback(i + 1, len(frame_times))

        logger.info(f"Extracted {len(frames)} frames")
        return frames

    def encode_image_base64(self, image_path: Path) -> str:
        """Encode image to base64 for API."""
        with open(image_path, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')

    def compute_phash(self, image_path: Path) -> Optional[str]:
        """
        Compute perceptual hash for an image.

        Uses average hash algorithm for fast similarity comparison.
        Returns hex string representation of hash.
        """
        try:
            from PIL import Image
            import hashlib

            # Open and resize to 8x8 for hash computation
            img = Image.open(image_path).convert('L')  # Grayscale
            img = img.resize((8, 8), Image.Resampling.LANCZOS)

            # Compute average
            pixels = list(img.getdata())
            avg = sum(pixels) / len(pixels)

            # Build hash: 1 if pixel > avg, 0 otherwise
            bits = ''.join('1' if p > avg else '0' for p in pixels)

            # Convert to hex
            return format(int(bits, 2), '016x')

        except ImportError:
            logger.warning("PIL not available for pHash computation")
            return None
        except Exception as e:
            logger.warning(f"pHash computation failed: {e}")
            return None

    def phash_similarity(self, hash1: str, hash2: str) -> float:
        """
        Compute similarity between two perceptual hashes.

        Returns similarity as float 0-1 (1 = identical).
        """
        if not hash1 or not hash2:
            return 0.0

        try:
            # Convert hex to int
            h1 = int(hash1, 16)
            h2 = int(hash2, 16)

            # XOR to find differing bits
            xor = h1 ^ h2

            # Count differing bits (Hamming distance)
            diff_bits = bin(xor).count('1')

            # 64 total bits (8x8), return similarity
            return 1.0 - (diff_bits / 64.0)

        except Exception as e:
            logger.warning(f"pHash similarity failed: {e}")
            return 0.0

    def are_frames_similar_vision(
        self,
        frame1_path: Path,
        frame2_path: Path
    ) -> bool:
        """
        Use GPT Vision to determine if two frames show the same content.

        Called for borderline pHash similarity cases.
        Returns True if frames are duplicates.
        """
        try:
            img1_b64 = self.encode_image_base64(frame1_path)
            img2_b64 = self.encode_image_base64(frame2_path)

            response = self.openai_client.chat.completions.create(
                model=self.model,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Compare these two video frames. Do they show essentially the same "
                                "educational content? (Same slide, same diagram, same code, etc.) "
                                "Minor differences like cursor position or small animations don't matter. "
                                "Reply with only 'SAME' or 'DIFFERENT'."
                            )
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{img1_b64}",
                                "detail": "low"
                            }
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{img2_b64}",
                                "detail": "low"
                            }
                        }
                    ]
                }],
                max_completion_tokens=10
            )

            answer = response.choices[0].message.content.strip().upper()
            return answer.startswith('SAME')

        except Exception as e:
            logger.warning(f"Vision similarity check failed: {e}")
            # Default to not similar if check fails
            return False

    def classify_and_filter_frame(
        self,
        frame_path: Path,
        timestamp: float
    ) -> tuple[bool, FrameClassification]:
        """
        Pass 1: Classify frame and determine if it has useful visual content.

        Args:
            frame_path: Path to frame image
            timestamp: Timestamp in video

        Returns:
            Tuple of (has_visual_content, classification)
        """
        image_base64 = self.encode_image_base64(frame_path)

        response = self.openai_client.chat.completions.create(
            model=self.model,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Classify this video frame into one of these categories:\n"
                            "- TALKING_HEAD: Just a person talking with no visual aids\n"
                            "- TITLE_SLIDE: Title or intro slide with mainly text\n"
                            "- CONTENT_SLIDE: Educational slide with points, lists, explanations\n"
                            "- DIAGRAM: Chart, graph, flowchart, architecture diagram\n"
                            "- CODE: Programming code display\n"
                            "- DEMONSTRATION: Physical demonstration or hands-on example\n"
                            "- OTHER: Other visual content\n\n"
                            "Reply with ONLY the category name, nothing else."
                        )
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}",
                            "detail": "low"
                        }
                    }
                ]
            }],
            max_completion_tokens=20
        )

        answer = response.choices[0].message.content.strip().upper()

        # Map response to classification
        classification_map = {
            "TALKING_HEAD": FrameClassification.TALKING_HEAD,
            "TITLE_SLIDE": FrameClassification.TITLE_SLIDE,
            "CONTENT_SLIDE": FrameClassification.CONTENT_SLIDE,
            "DIAGRAM": FrameClassification.DIAGRAM,
            "CODE": FrameClassification.CODE,
            "DEMONSTRATION": FrameClassification.DEMONSTRATION,
            "OTHER": FrameClassification.OTHER
        }

        classification = classification_map.get(answer, FrameClassification.OTHER)

        # TALKING_HEAD has no visual content; everything else does
        has_content = classification != FrameClassification.TALKING_HEAD

        return has_content, classification

    def analyze_frame_pass1(self, frame_path: Path, timestamp: float) -> bool:
        """
        Pass 1: Quick filter to check if frame has visual content.

        Args:
            frame_path: Path to frame image
            timestamp: Timestamp in video

        Returns:
            True if frame has visual content beyond talking head
        """
        image_base64 = self.encode_image_base64(frame_path)

        response = self.openai_client.chat.completions.create(
            model=self.model,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Is there educational visual content in this frame beyond just a person talking? "
                            "Look for: slides, text on screen, diagrams, charts, equations, demonstrations, "
                            "code, or anything that adds information visually. "
                            "Reply with only 'YES' or 'NO'."
                        )
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}",
                            "detail": "low"  # Low detail for quick filtering
                        }
                    }
                ]
            }],
            max_completion_tokens=10
        )

        answer = response.choices[0].message.content.strip().upper()
        return answer.startswith('YES')

    def analyze_frame_pass2(
        self,
        frame_path: Path,
        timestamp: float,
        nearby_transcript: str = ""
    ) -> str:
        """
        Pass 2: Detailed analysis of frame with visual content.

        Args:
            frame_path: Path to frame image
            timestamp: Timestamp in video
            nearby_transcript: Transcript text from around this timestamp

        Returns:
            Detailed description of visual content
        """
        image_base64 = self.encode_image_base64(frame_path)

        prompt = f"""This frame is from an educational video at timestamp {timestamp:.1f} seconds.
{f'The audio around this time says: "{nearby_transcript[:500]}"' if nearby_transcript else ''}

Describe the visual content that adds information beyond what's being said:
- Text on screen (slides, code, labels)
- Diagrams, charts, graphs, equations
- Demonstrations or visual examples
- Important visual context

Be concise but capture all key visual information. Focus on educational content."""

        response = self.openai_client.chat.completions.create(
            model=self.model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}",
                            "detail": "high"  # High detail for full analysis
                        }
                    }
                ]
            }],
            max_completion_tokens=500
        )

        return response.choices[0].message.content.strip()

    def analyze_frames(
        self,
        frames: list[tuple[float, Path]],
        transcript_segments: list[dict],
        progress_callback: Callable = None,
        dedup_threshold_low: float = 0.70,
        dedup_threshold_high: float = 0.90
    ) -> list[FrameAnalysis]:
        """
        Analyze frames using classification and hybrid deduplication.

        Uses two-pass approach:
        1. Classify frame type and filter out TALKING_HEAD
        2. Detailed description for frames with content

        Plus hybrid deduplication:
        - pHash similarity < 70%: different frames, keep
        - pHash similarity > 90%: same frame, skip
        - pHash 70-90%: use Vision API to decide

        Args:
            frames: List of (timestamp, frame_path) tuples
            transcript_segments: Transcription segments for context
            progress_callback: Optional callback(current, total, status)
            dedup_threshold_low: Below this similarity, frames are different
            dedup_threshold_high: Above this similarity, frames are same

        Returns:
            List of FrameAnalysis objects
        """
        results = []
        content_frames = 0
        skipped_duplicate = 0

        # Track recent content frames for deduplication
        recent_content_frames = []  # List of (phash, frame_path) for recent content frames

        for i, (timestamp, frame_path) in enumerate(frames):
            if progress_callback:
                progress_callback(i + 1, len(frames), "classifying")

            # Compute pHash for this frame
            frame_phash = self.compute_phash(frame_path)

            # Check for duplicates against recent content frames
            is_duplicate = False
            if frame_phash and recent_content_frames:
                for recent_phash, recent_path in recent_content_frames[-5:]:  # Check last 5 content frames
                    if not recent_phash:
                        continue

                    similarity = self.phash_similarity(frame_phash, recent_phash)

                    if similarity >= dedup_threshold_high:
                        # Clearly the same frame
                        is_duplicate = True
                        logger.debug(f"Frame at {timestamp}s skipped (pHash {similarity:.2f} similar to recent)")
                        break
                    elif similarity >= dedup_threshold_low:
                        # Borderline - use Vision to decide
                        if progress_callback:
                            progress_callback(i + 1, len(frames), "dedup_check")

                        if self.are_frames_similar_vision(frame_path, recent_path):
                            is_duplicate = True
                            logger.debug(f"Frame at {timestamp}s skipped (Vision confirmed duplicate)")
                            break

            if is_duplicate:
                skipped_duplicate += 1
                # Still add to results but mark as no content (duplicate)
                results.append(FrameAnalysis(
                    timestamp=timestamp,
                    frame_path=frame_path,
                    has_visual_content=False,
                    description="",
                    classification=FrameClassification.OTHER,
                    phash=frame_phash
                ))
                continue

            # Pass 1: Classify frame
            if progress_callback:
                progress_callback(i + 1, len(frames), "filtering")

            has_content, classification = self.classify_and_filter_frame(frame_path, timestamp)

            description = ""
            if has_content:
                # Track for deduplication
                recent_content_frames.append((frame_phash, frame_path))

                # Get nearby transcript for context
                nearby_text = self._get_nearby_transcript(transcript_segments, timestamp)

                if progress_callback:
                    progress_callback(i + 1, len(frames), "analyzing")

                # Pass 2: Detailed analysis
                description = self.analyze_frame_pass2(frame_path, timestamp, nearby_text)
                content_frames += 1

            results.append(FrameAnalysis(
                timestamp=timestamp,
                frame_path=frame_path,
                has_visual_content=has_content,
                description=description,
                classification=classification,
                phash=frame_phash
            ))

        logger.info(
            f"Analyzed {len(frames)} frames: "
            f"{content_frames} with content, "
            f"{skipped_duplicate} duplicates skipped"
        )
        return results

    def _get_nearby_transcript(
        self,
        segments: list[dict],
        timestamp: float,
        window: float = 15.0
    ) -> str:
        """Get transcript text near a timestamp."""
        nearby = []
        for seg in segments:
            if abs(seg['start'] - timestamp) <= window or abs(seg['end'] - timestamp) <= window:
                nearby.append(seg['text'])
            elif seg['start'] <= timestamp <= seg['end']:
                nearby.append(seg['text'])

        return " ".join(nearby)

    def merge_content(
        self,
        transcription: dict,
        frame_analyses: list[FrameAnalysis],
        chapters: list[dict] = None
    ) -> list[dict]:
        """
        Merge transcript and visual descriptions into chapters.

        Args:
            transcription: Transcription result from STT
            frame_analyses: Frame analysis results
            chapters: Optional chapter markers (from audio metadata or detection)

        Returns:
            List of chapter dicts with merged content:
            [{"title": str, "start": float, "end": float, "text": str, "visuals": [...]}]
        """
        segments = transcription.get('segments', [])

        # Use provided chapters or create a single chapter
        if not chapters:
            duration = transcription.get('duration', 0)
            if segments:
                duration = max(duration, segments[-1]['end'])
            chapters = [{"title": "Full Video", "start": 0, "end": duration}]

        # Create visual lookup by timestamp
        visuals_by_time = {}
        for fa in frame_analyses:
            if fa.has_visual_content and fa.description:
                visuals_by_time[fa.timestamp] = {
                    'timestamp': fa.timestamp,
                    'description': fa.description,
                    'frame_path': str(fa.frame_path),
                    'classification': fa.classification.value if fa.classification else 'other'
                }

        result = []
        for chapter in chapters:
            ch_start = chapter.get('start', 0)
            ch_end = chapter.get('end', float('inf'))

            # Get transcript segments for this chapter
            ch_segments = [
                s for s in segments
                if s['start'] >= ch_start and s['end'] <= ch_end
            ]
            ch_text = " ".join(s['text'] for s in ch_segments)

            # Get visuals for this chapter
            ch_visuals = [
                v for t, v in sorted(visuals_by_time.items())
                if ch_start <= t <= ch_end
            ]

            result.append({
                'title': chapter.get('title', 'Untitled'),
                'start': ch_start,
                'end': ch_end,
                'text': ch_text,
                'visuals': ch_visuals
            })

        return result

    def create_merged_text(self, chapters: list[dict]) -> str:
        """
        Create text with visual descriptions inserted at appropriate points.

        Args:
            chapters: Merged chapter content

        Returns:
            Full text with [Visual: ...] markers inserted
        """
        full_text = []

        for chapter in chapters:
            full_text.append(f"\n\n## {chapter['title']}\n\n")
            full_text.append(chapter['text'])

            # Add visual descriptions
            if chapter.get('visuals'):
                full_text.append("\n\n### Visual Content\n\n")
                for v in chapter['visuals']:
                    timestamp = v['timestamp']
                    mins = int(timestamp // 60)
                    secs = int(timestamp % 60)
                    full_text.append(f"[{mins}:{secs:02d}] {v['description']}\n\n")

        return "".join(full_text)

    def process_video(
        self,
        video_path: Path,
        progress_callback: Callable = None
    ) -> dict:
        """
        Full video processing pipeline.

        Args:
            video_path: Path to input video
            progress_callback: Optional callback(stage, current, total, detail)

        Returns:
            {
                "transcription": dict,
                "frame_analyses": list[FrameAnalysis],
                "chapters": list[dict],  # Merged content
                "merged_text": str,
                "duration": float,
                "audio_path": Path,
                "frames_dir": Path
            }
        """
        video_path = Path(video_path)
        logger.info(f"Processing video: {video_path.name}")

        # Stage 1: Extract audio
        if progress_callback:
            progress_callback("audio_extraction", 0, 1, "Extracting audio...")

        audio_path = self.extract_audio(video_path)

        if progress_callback:
            progress_callback("audio_extraction", 1, 1, "Audio extracted")

        # Stage 2: Transcribe audio
        if progress_callback:
            progress_callback("transcription", 0, 1, "Transcribing audio...")

        transcription = self.stt_engine.transcribe(audio_path)

        if progress_callback:
            progress_callback("transcription", 1, 1, f"Transcribed {len(transcription['segments'])} segments")

        # Stage 3: Extract frames
        def frame_progress(current, total):
            if progress_callback:
                progress_callback("frame_extraction", current, total, f"Extracting frame {current}/{total}")

        frames = self.extract_frames_scene_change(video_path, progress_callback=frame_progress)

        # Stage 4: Analyze frames with Vision
        def vision_progress(current, total, status):
            if progress_callback:
                detail = f"{'Filtering' if status == 'filtering' else 'Analyzing'} frame {current}/{total}"
                progress_callback("vision_analysis", current, total, detail)

        frame_analyses = self.analyze_frames(
            frames,
            transcription['segments'],
            progress_callback=vision_progress
        )

        # Stage 5: Detect chapters from transcription
        if progress_callback:
            progress_callback("merging", 0, 1, "Detecting chapters...")

        chapters = self.stt_engine.detect_chapters(transcription['segments'])

        # Stage 6: Merge content
        if progress_callback:
            progress_callback("merging", 1, 2, "Merging content...")

        merged_chapters = self.merge_content(transcription, frame_analyses, chapters)
        merged_text = self.create_merged_text(merged_chapters)

        if progress_callback:
            progress_callback("merging", 2, 2, "Processing complete")

        content_frames = sum(1 for fa in frame_analyses if fa.has_visual_content)
        logger.info(
            f"Video processing complete: {len(transcription['segments'])} transcript segments, "
            f"{len(frames)} frames extracted, {content_frames} with visual content"
        )

        return {
            "transcription": transcription,
            "frame_analyses": frame_analyses,
            "chapters": merged_chapters,
            "merged_text": merged_text,
            "duration": transcription.get('duration', 0),
            "audio_path": audio_path,
            "frames_dir": self.temp_dir / "frames"
        }

    def cleanup(self):
        """Clean up temporary files."""
        import shutil
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)
            logger.info(f"Cleaned up temp dir: {self.temp_dir}")
