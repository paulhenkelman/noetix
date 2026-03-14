"""
Speech-to-Text Engine using faster-whisper

Uses Whisper Large V3 Turbo model (~6GB VRAM) for high-quality transcription.
"""

import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class STTEngine:
    """
    Speech-to-Text engine using faster-whisper.

    Transcribes audio files to text with timestamps and chapter detection.
    """

    def __init__(self, model_name: str = "large-v3-turbo", device: str = "cuda"):
        """
        Initialize the STT engine.

        Args:
            model_name: Whisper model to use (large-v3-turbo recommended for quality)
            device: "cuda" for GPU or "cpu" for CPU processing
        """
        self.model_name = model_name
        self.device = device
        self.model = None

    def load_model(self):
        """Load the faster-whisper model (lazy loading)."""
        if self.model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                raise ImportError(
                    "faster-whisper package required. Install with: pip install faster-whisper"
                )

            compute_type = "float16" if self.device == "cuda" else "int8"

            logger.info(f"Loading STT model: {self.model_name} on {self.device}...")
            self.model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=compute_type
            )
            logger.info(f"STT model loaded successfully")

    def transcribe(
        self,
        audio_path: Path,
        language: Optional[str] = None,
        task: str = "transcribe"
    ) -> dict:
        """
        Transcribe audio file to text with timestamps.

        Args:
            audio_path: Path to the audio file (M4B, MP3, AAC, etc.)
            language: Language code (e.g., "en"). Auto-detected if None.
            task: "transcribe" or "translate" (to English)

        Returns:
            {
                "text": str,  # Full transcription
                "segments": [{"start": float, "end": float, "text": str}],
                "language": str,
                "duration": float
            }
        """
        self.load_model()

        audio_path = Path(audio_path)
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        logger.info(f"Transcribing: {audio_path.name}")

        segments_gen, info = self.model.transcribe(
            str(audio_path),
            language=language,
            task=task,
            beam_size=5,
            word_timestamps=True,
            vad_filter=True,  # Filter out silence
            vad_parameters=dict(
                min_silence_duration_ms=500,
                speech_pad_ms=200
            )
        )

        all_segments = []
        full_text = []

        for segment in segments_gen:
            all_segments.append({
                "start": segment.start,
                "end": segment.end,
                "text": segment.text.strip()
            })
            full_text.append(segment.text.strip())

        logger.info(f"Transcription complete: {len(all_segments)} segments, {info.duration:.1f}s")

        return {
            "text": " ".join(full_text),
            "segments": all_segments,
            "language": info.language,
            "duration": info.duration
        }

    def detect_chapters(
        self,
        segments: list,
        min_gap: float = 3.0,
        min_chapter_segments: int = 10
    ) -> list:
        """
        Detect chapter boundaries from transcription segments.

        Heuristics:
        - Long pauses (> min_gap seconds) between segments
        - Phrases like "Chapter X", "Part X", "Section X"
        - Minimum segments per chapter to avoid over-splitting

        Args:
            segments: List of transcription segments with start, end, text
            min_gap: Minimum silence duration to consider as chapter break
            min_chapter_segments: Minimum segments before allowing a chapter break

        Returns:
            [{"title": str, "start": float, "end": float}]
        """
        if not segments:
            return []

        # Pattern to detect chapter markers
        chapter_pattern = re.compile(
            r'^(chapter|part|section|book|prologue|epilogue)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten|[ivxlc]+)',
            re.IGNORECASE
        )

        chapters = []
        current_chapter = {
            "title": "Chapter 1",
            "start": segments[0]["start"] if segments else 0.0,
            "segments": []
        }
        chapter_num = 1

        for i, seg in enumerate(segments):
            text = seg["text"].strip()

            # Check for explicit chapter marker in text
            match = chapter_pattern.match(text)
            if match and len(current_chapter["segments"]) >= min_chapter_segments:
                # Save current chapter
                if current_chapter["segments"]:
                    current_chapter["end"] = current_chapter["segments"][-1]["end"]
                    chapters.append(current_chapter)

                # Start new chapter with detected title
                chapter_num += 1
                detected_title = text[:80]  # Use first 80 chars of detected text
                current_chapter = {
                    "title": detected_title,
                    "start": seg["start"],
                    "segments": [seg]
                }
                continue

            # Check for long gap (potential chapter break)
            if i > 0 and len(current_chapter["segments"]) >= min_chapter_segments:
                gap = seg["start"] - segments[i - 1]["end"]
                if gap > min_gap:
                    # Significant pause - might be chapter break
                    current_chapter["end"] = segments[i - 1]["end"]
                    chapters.append(current_chapter)

                    chapter_num += 1
                    current_chapter = {
                        "title": f"Chapter {chapter_num}",
                        "start": seg["start"],
                        "segments": [seg]
                    }
                    continue

            current_chapter["segments"].append(seg)

        # Save final chapter
        if current_chapter["segments"]:
            current_chapter["end"] = current_chapter["segments"][-1]["end"]
            chapters.append(current_chapter)

        # If no chapters were detected (only one), create a default
        if len(chapters) == 0:
            chapters = [{
                "title": "Full Recording",
                "start": 0.0,
                "end": segments[-1]["end"] if segments else 0.0,
                "segments": segments
            }]

        logger.info(f"Detected {len(chapters)} chapters from transcription")
        return chapters

    def get_chapter_text(self, transcription: dict, chapter: dict) -> str:
        """
        Extract text for a specific chapter from transcription.

        Args:
            transcription: Full transcription result from transcribe()
            chapter: Chapter dict with start/end times

        Returns:
            Concatenated text for segments within the chapter's time range
        """
        start = chapter.get("start", 0)
        end = chapter.get("end", float("inf"))

        chapter_segments = [
            s for s in transcription.get("segments", [])
            if s["start"] >= start and s["end"] <= end
        ]

        return " ".join(s["text"] for s in chapter_segments)
