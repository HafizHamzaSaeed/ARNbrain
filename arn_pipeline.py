#!/usr/bin/env python3
"""Download every video from a YouTube channel and transcribe its audio with Gemini.

Usage:
    export GEMINI_API_KEY="your-key-here"
    python3 arn_pipeline.py
    python3 arn_pipeline.py --limit 3          # test on a few videos first
    python3 arn_pipeline.py --channel <url> --output-dir ./data

Resumable: videos already downloaded/transcribed are skipped on re-run.

Each transcript is named "<publish-date> - <title> [<video_id>].txt" and
carries a "[Published: <date>]" header, and every video's id/title/date is
recorded in data/manifest.jsonl — so downstream use (RAG, fine-tuning, etc.)
can tell *when* an opinion was expressed instead of just what was said.
"""

import argparse
import json
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
    "Transcribe this audio in Roman Urdu (Urdu written using the Latin/English "
    "alphabet, not Urdu or Arabic script). Keep the wording and meaning as close "
    "to the spoken audio as possible. If any portion is in Arabic (e.g. Quranic "
    "recitation) or English, transcribe that portion in its original "
    "script/language rather than transliterating it into Roman Urdu. Output "
    "only the transcript text, with no extra commentary or timestamps."
)
UNKNOWN_DATE = "unknown-date"

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


def format_date(upload_date: str | None) -> str:
    if not upload_date or len(upload_date) != 8:
        return UNKNOWN_DATE
    return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"


def transcript_filename(date_str: str, title: str, video_id: str) -> str:
    return f"{date_str} - {sanitize_filename(title)} [{video_id}].txt"


def find_existing_transcript(transcript_dir: Path, video_id: str) -> Path | None:
    # Plain suffix match, not glob — "[video_id]" contains "[" / "]", which
    # glob treats as a character class rather than literal brackets.
    suffix = f"[{video_id}].txt"
    for p in transcript_dir.iterdir():
        if p.name.endswith(suffix):
            return p
    return None


def load_known_metadata(manifest_path: Path) -> dict:
    known = {}
    if not manifest_path.exists():
        return known
    with manifest_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            known[record["video_id"]] = record
    return known


def append_manifest(manifest_path: Path, record: dict) -> None:
    with manifest_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def fetch_video_metadata(video_url: str) -> dict | None:
    ydl_opts = {"quiet": True, "skip_download": True, "ignoreerrors": True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except Exception as exc:  # noqa: BLE001 - metadata is best-effort
        log.warning("  could not fetch metadata for %s: %s", video_url, exc)
        return None
    if info is None:
        return None
    return {"title": info.get("title") or "", "date": format_date(info.get("upload_date"))}


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


def download_audio(video_id: str, video_url: str, audio_dir: Path) -> tuple[Path | None, dict | None]:
    existing = list(audio_dir.glob(f"{video_id}.*"))
    if existing:
        log.info("  [skip download] %s already downloaded", video_id)
        return existing[0], None

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
        info = ydl.extract_info(video_url, download=True)

    result = list(audio_dir.glob(f"{video_id}.*"))
    meta = {"title": info.get("title") or "", "date": format_date(info.get("upload_date"))} if info else None
    return (result[0] if result else None), meta


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
    manifest_path = output_dir / "manifest.jsonl"
    audio_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir.mkdir(parents=True, exist_ok=True)

    known = load_known_metadata(manifest_path)

    log.info("Fetching video list from %s ...", args.channel)
    videos = list_channel_videos(args.channel, args.limit)
    log.info("Found %d video(s) to process.", len(videos))

    failures = []
    for i, video in enumerate(videos, start=1):
        video_id = video.get("id")
        video_url = video.get("url") or f"https://www.youtube.com/watch?v={video_id}"
        fallback_title = video.get("title", video_id)
        log.info("[%d/%d] %s (%s)", i, len(videos), fallback_title, video_id)

        meta = known.get(video_id)

        # Backfill path: transcript already exists (e.g. from before this
        # feature existed) — rename it to include the date and record it in
        # the manifest, without re-downloading or re-transcribing.
        existing_transcript = find_existing_transcript(transcript_dir, video_id)
        if existing_transcript:
            if meta is None:
                meta = fetch_video_metadata(video_url) or {"title": fallback_title, "date": UNKNOWN_DATE}
            new_path = transcript_dir / transcript_filename(meta["date"], meta["title"] or fallback_title, video_id)
            old_path = existing_transcript
            if old_path != new_path:
                old_path.rename(new_path)
                log.info("  [backfill] renamed -> %s", new_path.name)
            if video_id not in known:
                record = {
                    "video_id": video_id,
                    "title": meta["title"] or fallback_title,
                    "publish_date": meta["date"],
                    "url": video_url,
                    "transcript_file": new_path.name,
                }
                append_manifest(manifest_path, record)
                known[video_id] = record
            log.info("  [skip transcribe] transcript already exists")
            continue

        try:
            audio_path, dl_meta = download_audio(video_id, video_url, audio_dir)
            if audio_path is None:
                raise RuntimeError("download produced no audio file")
        except Exception as exc:  # noqa: BLE001
            log.error("  download failed: %s", exc)
            failures.append((video_id, "download", str(exc)))
            continue

        if meta is None:
            meta = dl_meta or fetch_video_metadata(video_url) or {"title": fallback_title, "date": UNKNOWN_DATE}
        title = meta["title"] or fallback_title
        date_str = meta["date"]
        transcript_path = transcript_dir / transcript_filename(date_str, title, video_id)

        try:
            transcript = transcribe_audio(audio_path, args.model, args.request_delay)
            transcript_path.write_text(f"[Published: {date_str}]\n\n{transcript}", encoding="utf-8")
            log.info("  transcribed -> %s", transcript_path.name)
        except Exception as exc:  # noqa: BLE001
            log.error("  transcription failed: %s", exc)
            failures.append((video_id, "transcribe", str(exc)))
            continue

        record = {
            "video_id": video_id,
            "title": title,
            "publish_date": date_str,
            "url": video_url,
            "transcript_file": transcript_path.name,
        }
        append_manifest(manifest_path, record)
        known[video_id] = record

        time.sleep(args.request_delay)

    log.info("Done. %d succeeded, %d failed.", len(videos) - len(failures), len(failures))
    if failures:
        log.warning("Failures:")
        for video_id, stage, err in failures:
            log.warning("  %s [%s]: %s", video_id, stage, err)


if __name__ == "__main__":
    main()
