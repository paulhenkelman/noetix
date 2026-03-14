"""
Stream Downloader - Download video streams using yt-dlp

Integrates with browser_bridge to download authenticated HLS/DASH streams.
"""

import logging
import subprocess
import tempfile
import shutil
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class StreamInfo:
    """Information about a captured stream."""
    url: str
    type: str  # 'hls' or 'dash'
    headers: dict
    timestamp: str


@dataclass
class DownloadResult:
    """Result of a stream download."""
    success: bool
    output_path: Optional[Path]
    error: Optional[str]
    duration_seconds: Optional[float]


def check_ytdlp_available() -> bool:
    """Check if yt-dlp is installed."""
    if shutil.which('yt-dlp') is not None:
        return True
    # Also check in conda environment
    import sys
    conda_ytdlp = Path(sys.executable).parent / 'yt-dlp'
    return conda_ytdlp.exists()


def check_ffmpeg_available() -> bool:
    """Check if ffmpeg is installed."""
    return shutil.which('ffmpeg') is not None


def get_ytdlp_path() -> str:
    """Get the path to yt-dlp executable."""
    if shutil.which('yt-dlp'):
        return 'yt-dlp'
    # Check in conda environment
    import sys
    conda_ytdlp = Path(sys.executable).parent / 'yt-dlp'
    if conda_ytdlp.exists():
        return str(conda_ytdlp)
    return 'yt-dlp'  # Fall back to PATH


def download_stream(
    stream_url: str,
    output_path: Path,
    cookies_file: Optional[Path] = None,
    referer: Optional[str] = None,
    user_agent: Optional[str] = None,
    extra_headers: Optional[dict] = None,
    format_selector: str = "best",
    verbose: bool = False
) -> DownloadResult:
    """
    Download a video stream using yt-dlp.

    Args:
        stream_url: URL to the m3u8/mpd manifest
        output_path: Where to save the downloaded video
        cookies_file: Path to Netscape format cookies file
        referer: Referer header
        user_agent: User-Agent header
        extra_headers: Additional headers
        format_selector: yt-dlp format string (default: best)
        verbose: Enable verbose output

    Returns:
        DownloadResult with success status and output path
    """
    if not check_ytdlp_available():
        return DownloadResult(
            success=False,
            output_path=None,
            error="yt-dlp not installed. Install with: pip install yt-dlp",
            duration_seconds=None
        )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Build yt-dlp command
    cmd = [
        get_ytdlp_path(),
        '--no-warnings',
        '-f', format_selector,
        '-o', str(output_path),
    ]

    # Add cookies file
    if cookies_file and Path(cookies_file).exists():
        cmd.extend(['--cookies', str(cookies_file)])

    # Add headers
    if referer:
        cmd.extend(['--referer', referer])

    if user_agent:
        cmd.extend(['--user-agent', user_agent])

    if extra_headers:
        for key, value in extra_headers.items():
            if key.lower() not in ['referer', 'user-agent']:
                cmd.extend(['--add-header', f'{key}:{value}'])

    # HLS-specific options for better compatibility
    if '.m3u8' in stream_url.lower():
        cmd.extend([
            '--hls-use-mpegts',  # Better for live/problematic streams
            '--downloader', 'ffmpeg',
        ])

    # Verbose output
    if verbose:
        cmd.append('-v')
    else:
        cmd.append('-q')

    # Add the URL
    cmd.append(stream_url)

    logger.info(f"Downloading stream: {stream_url[:100]}...")
    logger.debug(f"Command: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600  # 1 hour timeout
        )

        if result.returncode == 0:
            # Get video duration using ffprobe
            duration = get_video_duration(output_path)

            logger.info(f"Download complete: {output_path}")
            return DownloadResult(
                success=True,
                output_path=output_path,
                error=None,
                duration_seconds=duration
            )
        else:
            error_msg = result.stderr or result.stdout or "Unknown error"
            logger.error(f"yt-dlp failed: {error_msg}")
            return DownloadResult(
                success=False,
                output_path=None,
                error=error_msg,
                duration_seconds=None
            )

    except subprocess.TimeoutExpired:
        return DownloadResult(
            success=False,
            output_path=None,
            error="Download timed out after 1 hour",
            duration_seconds=None
        )
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return DownloadResult(
            success=False,
            output_path=None,
            error=str(e),
            duration_seconds=None
        )


def download_with_browser_cookies(
    stream_info: dict,
    output_path: Path,
    browser_bridge,
    format_selector: str = "best"
) -> DownloadResult:
    """
    Download a stream using cookies from the browser session.

    Args:
        stream_info: Stream dict from browser_bridge.stop_stream_capture()
        output_path: Where to save the video
        browser_bridge: BrowserBridge instance with active session
        format_selector: yt-dlp format string

    Returns:
        DownloadResult
    """
    # Extract stream details
    stream_url = stream_info['url']
    headers = stream_info.get('headers', {})

    # Create temp file for cookies
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        cookies_file = Path(f.name)

        # Get domain from stream URL
        from urllib.parse import urlparse
        domain = urlparse(stream_url).netloc

        # Save cookies
        browser_bridge.save_cookies_file(cookies_file, domain)

    try:
        return download_stream(
            stream_url=stream_url,
            output_path=output_path,
            cookies_file=cookies_file,
            referer=headers.get('referer'),
            user_agent=headers.get('user-agent'),
            format_selector=format_selector
        )
    finally:
        # Clean up temp cookies file
        try:
            cookies_file.unlink()
        except Exception:
            pass


def get_video_duration(video_path: Path) -> Optional[float]:
    """Get video duration in seconds using ffprobe."""
    if not video_path.exists():
        return None

    try:
        result = subprocess.run([
            'ffprobe',
            '-v', 'quiet',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            str(video_path)
        ], capture_output=True, text=True, timeout=30)

        if result.returncode == 0:
            return float(result.stdout.strip())
    except Exception:
        pass

    return None


def get_stream_info(stream_url: str, cookies_file: Optional[Path] = None) -> dict:
    """
    Get information about a stream without downloading.

    Args:
        stream_url: URL to the stream manifest
        cookies_file: Optional cookies file

    Returns:
        Dict with stream info (formats, duration, etc.)
    """
    if not check_ytdlp_available():
        return {'error': 'yt-dlp not installed'}

    cmd = [get_ytdlp_path(), '-j', '--no-warnings']

    if cookies_file and Path(cookies_file).exists():
        cmd.extend(['--cookies', str(cookies_file)])

    cmd.append(stream_url)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode == 0:
            import json
            return json.loads(result.stdout)
        else:
            return {'error': result.stderr or 'Failed to get stream info'}

    except Exception as e:
        return {'error': str(e)}


class StreamDownloader:
    """
    High-level class for downloading streams from authenticated pages.

    Usage:
        from browser_bridge import get_browser_bridge
        from stream_downloader import StreamDownloader

        bridge = get_browser_bridge()
        downloader = StreamDownloader(bridge)

        # User logs in via noVNC and navigates to video page
        # Then:
        streams = downloader.capture_page_streams(play_video=True, wait_seconds=10)
        result = downloader.download_best_stream(streams, output_path)
    """

    def __init__(self, browser_bridge):
        """
        Initialize downloader with browser bridge.

        Args:
            browser_bridge: BrowserBridge instance with active session
        """
        self.bridge = browser_bridge

    def capture_page_streams(
        self,
        play_video: bool = False,
        play_selector: str = "video, .play-button, [aria-label*='play']",
        wait_seconds: int = 10
    ) -> list:
        """
        Capture stream URLs from the current page.

        Args:
            play_video: Try to click play button to trigger stream load
            play_selector: CSS selector for play button
            wait_seconds: How long to wait for streams to load

        Returns:
            List of captured stream dicts
        """
        import time

        # Start capture
        self.bridge.start_stream_capture()

        # Optionally click play
        if play_video:
            try:
                self.bridge.click(play_selector, timeout=5000)
                logger.info("Clicked play button")
            except Exception as e:
                logger.debug(f"Could not click play: {e}")

        # Wait for streams to load
        logger.info(f"Waiting {wait_seconds}s for streams to load...")
        time.sleep(wait_seconds)

        # Stop and get results
        result = self.bridge.stop_stream_capture()

        streams = result.get('streams', [])
        master_streams = result.get('master_streams', [])

        logger.info(f"Captured {len(streams)} streams ({len(master_streams)} masters)")

        return master_streams if master_streams else streams

    def download_best_stream(
        self,
        streams: list,
        output_path: Path,
        format_selector: str = "best"
    ) -> DownloadResult:
        """
        Download the best stream from captured list.

        Args:
            streams: List of stream dicts from capture_page_streams()
            output_path: Where to save video
            format_selector: yt-dlp format string

        Returns:
            DownloadResult
        """
        if not streams:
            return DownloadResult(
                success=False,
                output_path=None,
                error="No streams captured",
                duration_seconds=None
            )

        # Prefer master playlists
        stream = streams[0]

        logger.info(f"Downloading stream: {stream['url'][:80]}...")

        return download_with_browser_cookies(
            stream_info=stream,
            output_path=output_path,
            browser_bridge=self.bridge,
            format_selector=format_selector
        )


# Convenience function
def capture_and_download(
    browser_bridge,
    output_path: Path,
    wait_seconds: int = 10,
    click_play: bool = True
) -> DownloadResult:
    """
    One-shot function to capture and download video from current page.

    Args:
        browser_bridge: BrowserBridge with active session on video page
        output_path: Where to save video
        wait_seconds: How long to wait for stream URLs
        click_play: Whether to try clicking play button

    Returns:
        DownloadResult
    """
    downloader = StreamDownloader(browser_bridge)
    streams = downloader.capture_page_streams(
        play_video=click_play,
        wait_seconds=wait_seconds
    )
    return downloader.download_best_stream(streams, output_path)
