#!/usr/bin/env python3
"""Download every video from a YouTube channel and transcribe its audio with Gemini.

Usage:
    export GEMINI_API_KEY="your-key-here"
    python3 arn_pipeline.py
    python3 arn_pipeline.py --limit 3          # test on a few videos first
    python3 arn_pipeline.py --channel <url> --output-dir ./data

Resumable: videos already downloaded/transcribed are skipped on re-run.
"""

import argparse
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yt_dlp
import google.generativeai as genai

DEFAULT_CHANNEL_URL = "https://www.youtube.com/@AbdulRehmanNajamOfficial/videos"
DEFAULT_MODEL = "gemini-3.5-flash"
# Files above this are rejected by the inline-audio request path, so longer
# audio is split into chunks of this length and transcribed piece by piece.
INLINE_SIZE_LIMIT_BYTES = 19 * 1024 * 1024
CHUNK_SECONDS = 600
TRANSCRIBE_PROMPT = (
    "Transcribe this audio verbatim. Output only the transcript text, "
    "with no extra commentary or timestamps."
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("arn_pipeline")

INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def sanitize_filename(title: str, max_length: int = 120) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("", title).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:max_length].rstrip()


def transcript_filename(title: str, video_id: str) -> str:
    return f"{sanitize_filename(title)} [{video_id}].txt"


def list_channel_videos(channel_url: str, limit: int | None) -> list[dict]:
    ydl_opts = {"extract_flat": True, "quiet": True, "ignoreerrors": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)
    if info is None:
        raise RuntimeError(
            f"Could not fetch channel page for {channel_url} "
            "(network error or the channel/URL is unreachable)."
        )
    entries = info.get("entries") or []
    # Channel "videos" tabs sometimes nest another level of entries.
    flat = []
    for e in entries:
        if e is None:
            continue
        if e.get("_type") == "playlist" and e.get("entries"):
            flat.extend(x for x in e["entries"] if x)
        else:
            flat.append(e)
    if limit:
        flat = flat[:limit]
    return flat


def download_audio(video_id: str, video_url: str, audio_dir: Path) -> Path | None:
    existing = list(audio_dir.glob(f"{video_id}.*"))
    if existing:
        log.info("  [skip download] %s already downloaded", video_id)
        return existing[0]

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(audio_dir / f"{video_id}.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }
        ],
        "quiet": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])

    result = list(audio_dir.glob(f"{video_id}.*"))
    return result[0] if result else None


AUDIO_MIME_TYPES = {".mp3": "audio/mp3", ".m4a": "audio/mp4", ".wav": "audio/wav", ".ogg": "audio/ogg"}


def split_audio(audio_path: Path, chunk_dir: Path, chunk_seconds: int) -> list[Path]:
    pattern = str(chunk_dir / f"chunk_%04d{audio_path.suffix}")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(audio_path),
            "-f", "segment", "-segment_time", str(chunk_seconds),
            "-c", "copy", pattern,
        ],
        check=True,
        capture_output=True,
    )
    return sorted(chunk_dir.glob(f"chunk_*{audio_path.suffix}"))


def transcribe_chunk(data: bytes, mime_type: str, model: "genai.GenerativeModel", max_retries: int) -> str:
    for attempt in range(1, max_retries + 1):
        try:
            response = model.generate_content(
                [TRANSCRIBE_PROMPT, {"mime_type": mime_type, "data": data}]
            )
            return response.text
        except Exception as exc:  # noqa: BLE001 - retry on any transient API error
            log.warning("    chunk transcription attempt %d/%d failed: %s", attempt, max_retries, exc)
            if attempt == max_retries:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def transcribe_audio(audio_path: Path, model_name: str, request_delay: float, max_retries: int = 3) -> str:
    mime_type = AUDIO_MIME_TYPES.get(audio_path.suffix.lower(), "audio/mp3")
    model = genai.GenerativeModel(model_name)
    size = audio_path.stat().st_size

    if size <= INLINE_SIZE_LIMIT_BYTES:
        return transcribe_chunk(audio_path.read_bytes(), mime_type, model, max_retries)

    log.info("  audio is %.1fMB, splitting into %ds chunks", size / 1024 / 1024, CHUNK_SECONDS)
    with tempfile.TemporaryDirectory() as tmp:
        chunks = split_audio(audio_path, Path(tmp), CHUNK_SECONDS)
        parts = []
        for i, chunk_path in enumerate(chunks, start=1):
            log.info("  transcribing chunk %d/%d", i, len(chunks))
            parts.append(transcribe_chunk(chunk_path.read_bytes(), mime_type, model, max_retries))
            if i < len(chunks):
                time.sleep(request_delay)
        return "\n\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default=DEFAULT_CHANNEL_URL, help="YouTube channel URL")
    parser.add_argument("--output-dir", default="data", help="Where audio/transcripts are stored")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model name")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N videos")
    parser.add_argument(
        "--request-delay",
        type=float,
        default=4.0,
        help="Seconds to sleep between Gemini requests (rate-limit safety)",
    )
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.error("GEMINI_API_KEY environment variable is not set. Aborting.")
        sys.exit(1)
    genai.configure(api_key=api_key)

    output_dir = Path(args.output_dir)
    audio_dir = output_dir / "audio"
    transcript_dir = output_dir / "transcripts"
    audio_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir.mkdir(parents=True, exist_ok=True)

    log.info("Fetching video list from %s ...", args.channel)
    videos = list_channel_videos(args.channel, args.limit)
    log.info("Found %d video(s) to process.", len(videos))

    failures = []
    for i, video in enumerate(videos, start=1):
        video_id = video.get("id")
        video_url = video.get("url") or f"https://www.youtube.com/watch?v={video_id}"
        title = video.get("title", video_id)
        log.info("[%d/%d] %s (%s)", i, len(videos), title, video_id)

        existing_transcript = list(transcript_dir.glob(f"*[{video_id}].txt"))
        if existing_transcript:
            log.info("  [skip transcribe] transcript already exists")
            continue
        transcript_path = transcript_dir / transcript_filename(title, video_id)

        try:
            audio_path = download_audio(video_id, video_url, audio_dir)
            if audio_path is None:
                raise RuntimeError("download produced no audio file")
        except Exception as exc:  # noqa: BLE001
            log.error("  download failed: %s", exc)
            failures.append((video_id, "download", str(exc)))
            continue

        try:
            transcript = transcribe_audio(audio_path, args.model, args.request_delay)
            transcript_path.write_text(transcript, encoding="utf-8")
            log.info("  transcribed -> %s", transcript_path)
        except Exception as exc:  # noqa: BLE001
            log.error("  transcription failed: %s", exc)
            failures.append((video_id, "transcribe", str(exc)))
            continue

        time.sleep(args.request_delay)

    log.info("Done. %d succeeded, %d failed.", len(videos) - len(failures), len(failures))
    if failures:
        log.warning("Failures:")
        for video_id, stage, err in failures:
            log.warning("  %s [%s]: %s", video_id, stage, err)


if __name__ == "__main__":
    main()
