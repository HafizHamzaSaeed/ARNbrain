#!/usr/bin/env python3
"""Download every video from a YouTube channel and transcribe its audio with Gemini.

Usage:
    export GEMINI_API_KEY="your-key-here"
    python3 arn_pipeline.py
    python3 arn_pipeline.py --limit 3                       # test on a few videos first
    python3 arn_pipeline.py --channel <url1> <url2> ...      # scan multiple channels in one run
    python3 arn_pipeline.py --output-dir ./data
    python3 arn_pipeline.py --cleanup-excluded              # retroactively remove EXCLUDE_TITLE_PATTERNS matches

Resumable: videos already downloaded/transcribed are skipped on re-run.
Scans multiple channels by default (see DEFAULT_CHANNEL_URLS) — currently
both of ARN's channels. Videos whose title matches EXCLUDE_TITLE_PATTERNS
(default: anything with "shafy" in it, for the other uploader on the main
channel) are skipped entirely — never downloaded or transcribed. Run with
--cleanup-excluded once after adding/changing a pattern to retroactively
move any already-processed matches out of data/ (into data/excluded/,
nothing is deleted) and out of the manifest.

Guest podcast appearances (ARN on other channels) aren't covered by the
channel scan above. To include them, add their video URLs — one per line —
to extra_urls.txt next to this script; they're picked up automatically on
the next run, merged in and de-duplicated the same as everything else. If
that file is missing or empty, a reminder is logged each run so this isn't
forgotten once the main channel backlog is done.

Pulls from the channel's Videos, Shorts, and Live/streams tabs (not just
Videos) so nothing is silently missed. Both audio and transcript files are
named "<publish-date> - <title> [<video_id>].<ext>", and every video's
id/title/date/duration/description is recorded in data/manifest.jsonl —
so downstream use (RAG, fine-tuning, etc.) can tell *when* an opinion was
expressed instead of just what was said. Possible duplicate uploads (same
normalized title) and low-confidence transcripts are flagged in the
manifest for manual review rather than silently dropped or accepted.
Failures are logged to data/failures.jsonl as they happen.
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

DEFAULT_CHANNEL_URLS = [
    "https://www.youtube.com/@AbdulRehmanNajamOfficial/videos",
    "https://www.youtube.com/@AbdulRehmanNajam2/videos",
]
DEFAULT_MODEL = "gemini-3.5-flash"
# Case-insensitive substring match against the video title. Anything matching
# is skipped entirely (never downloaded or transcribed) — used to exclude
# other uploaders' content from a shared channel. Confirm/adjust this list
# for your channel; it's a heuristic, not a guarantee.
EXCLUDE_TITLE_PATTERNS = ["shafy"]
CHANNEL_TABS = ("videos", "shorts", "streams")
# Files above this are rejected by the inline-audio request path, so longer
# audio is split into chunks of this length and transcribed piece by piece.
INLINE_SIZE_LIMIT_BYTES = 19 * 1024 * 1024
CHUNK_SECONDS = 600
CHUNK_OVERLAP_SECONDS = 15
TRANSCRIBE_PROMPT = (
    "Transcribe this audio in Roman Urdu (Urdu written using the Latin/English "
    "alphabet, not Urdu or Arabic script). Keep the wording and meaning as close "
    "to the spoken audio as possible. If any portion is in Arabic (e.g. Quranic "
    "recitation) or English, transcribe that portion in its original "
    "script/language rather than transliterating it into Roman Urdu. If more than "
    "one person speaks (e.g. a guest, co-host, or interviewer), label each "
    "speaker's lines clearly, marking Abdul Rehman Najam's (ARN's) own speech "
    "distinctly from anyone else's; if only one person speaks, no labels are "
    "needed. Output only the transcript text, with no extra commentary or "
    "timestamps."
)
UNKNOWN_DATE = "unknown-date"
EXTRA_URLS_FILE = "extra_urls.txt"
ARABIC_SCRIPT_RE = re.compile(r"[؀-ۿݐ-ݿ]")

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


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def format_date(upload_date: str | None) -> str:
    if not upload_date or len(upload_date) != 8:
        return UNKNOWN_DATE
    return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"


def dated_stem(date_str: str, title: str, video_id: str) -> str:
    return f"{date_str} - {sanitize_filename(title)} [{video_id}]"


def transcript_filename(date_str: str, title: str, video_id: str) -> str:
    return f"{dated_stem(date_str, title, video_id)}.txt"


def find_by_id_suffix(directory: Path, video_id: str) -> Path | None:
    """Find a file for this video_id, whether it's in the old bare-ID format
    (e.g. "abc123.mp3") or the new dated format (e.g. "... [abc123].mp3")."""
    bracket_suffix = f"[{video_id}]"
    for p in directory.iterdir():
        if p.stem == video_id or p.stem.endswith(bracket_suffix):
            return p
    return None


def find_existing_transcript(transcript_dir: Path, video_id: str) -> Path | None:
    # Plain suffix match, not glob — "[video_id]" contains "[" / "]", which
    # glob treats as a character class rather than literal brackets.
    return find_by_id_suffix(transcript_dir, video_id)


def find_existing_audio(audio_dir: Path, video_id: str) -> Path | None:
    return find_by_id_suffix(audio_dir, video_id)


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
            # The manifest stores the date as "publish_date", but every
            # in-memory meta dict (from fetch_video_metadata / download_audio)
            # uses "date" — alias it so a manifest-loaded record can be used
            # as `meta` interchangeably with a freshly-fetched one.
            record.setdefault("date", record.get("publish_date", UNKNOWN_DATE))
            known[record["video_id"]] = record
    return known


def append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
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
    return {
        "title": info.get("title") or "",
        "date": format_date(info.get("upload_date")),
        "description": info.get("description") or "",
    }


def list_tab_videos(tab_url: str) -> list[dict]:
    ydl_opts = {"extract_flat": True, "quiet": True, "ignoreerrors": True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(tab_url, download=False)
    except Exception as exc:  # noqa: BLE001 - a channel may not have this tab
        log.warning("  could not list %s: %s", tab_url, exc)
        return []
    if info is None:
        return []
    entries = info.get("entries") or []
    # Channel tabs sometimes nest another level of entries.
    flat = []
    for e in entries:
        if e is None:
            continue
        if e.get("_type") == "playlist" and e.get("entries"):
            flat.extend(x for x in e["entries"] if x)
        else:
            flat.append(e)
    return flat


def channel_base_url(channel_url: str) -> str:
    trimmed = channel_url.rstrip("/")
    for suffix in ("/videos", "/shorts", "/streams", "/featured"):
        if trimmed.endswith(suffix):
            return trimmed[: -len(suffix)]
    return trimmed


def list_channel_videos(channel_urls: list[str], limit: int | None) -> list[dict]:
    seen_ids = set()
    combined = []
    for channel_url in channel_urls:
        base = channel_base_url(channel_url)
        for tab in CHANNEL_TABS:
            for entry in list_tab_videos(f"{base}/{tab}"):
                vid = entry.get("id")
                if vid and vid not in seen_ids:
                    seen_ids.add(vid)
                    combined.append(entry)
    if limit:
        combined = combined[:limit]
    return combined


def is_excluded(title: str, patterns: list[str]) -> bool:
    lowered = title.lower()
    return any(p.lower() in lowered for p in patterns)


def load_extra_urls(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def resolve_extra_url(url: str) -> dict | None:
    ydl_opts = {"quiet": True, "skip_download": True, "ignoreerrors": True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 - a bad/unreachable URL shouldn't stop the run
        log.warning("  could not resolve extra URL %s: %s", url, exc)
        return None
    if info is None or not info.get("id"):
        return None
    return {"id": info["id"], "title": info.get("title") or "", "url": url}


def rename_if_bare(path: Path, video_id: str, meta: dict) -> Path:
    """If `path` is still in the old bare-ID format, rename it to the dated
    format now that we know the video's title/date."""
    if path.stem != video_id:
        return path
    new_path = path.with_name(f"{dated_stem(meta['date'], meta['title'], video_id)}{path.suffix}")
    if new_path != path:
        path.rename(new_path)
        log.info("  [backfill] renamed -> %s", new_path.name)
    return new_path


def download_audio(video_id: str, video_url: str, audio_dir: Path, meta: dict) -> tuple[Path | None, dict | None]:
    existing = find_existing_audio(audio_dir, video_id)
    if existing:
        existing = rename_if_bare(existing, video_id, meta)
        log.info("  [skip download] %s already downloaded", video_id)
        return existing, None

    outtmpl = str(audio_dir / f"{dated_stem(meta['date'], meta['title'], video_id)}.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
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

    result = find_existing_audio(audio_dir, video_id)
    dl_meta = None
    if info:
        dl_meta = {
            "title": info.get("title") or "",
            "date": format_date(info.get("upload_date")),
            "description": info.get("description") or "",
        }
    return result, dl_meta


AUDIO_MIME_TYPES = {".mp3": "audio/mp3", ".m4a": "audio/mp4", ".wav": "audio/wav", ".ogg": "audio/ogg"}


def probe_duration(audio_path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
            "-of", "csv=p=0", str(audio_path),
        ],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def split_audio(audio_path: Path, chunk_dir: Path, duration: float, chunk_seconds: int, overlap_seconds: int) -> list[Path]:
    step = max(chunk_seconds - overlap_seconds, 1)
    chunks = []
    start = 0.0
    idx = 0
    while start < duration:
        out_path = chunk_dir / f"chunk_{idx:04d}{audio_path.suffix}"
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(audio_path),
                "-ss", str(start), "-t", str(chunk_seconds),
                "-c", "copy", str(out_path),
            ],
            check=True, capture_output=True,
        )
        chunks.append(out_path)
        start += step
        idx += 1
    return chunks


def merge_overlapping_text(a: str, b: str, max_check_chars: int = 300) -> str:
    """Chunks are extracted from overlapping audio, so consecutive transcripts
    usually repeat a stretch of text at the seam. Trim it by finding the
    longest suffix of `a` that also appears as a prefix of `b`."""
    a_tail = a[-max_check_chars:]
    for length in range(min(len(a_tail), len(b)), 10, -1):
        if a_tail[-length:] == b[:length]:
            return a + b[length:]
    return a + b


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


def transcribe_audio(audio_path: Path, model_name: str, request_delay: float, duration: float, max_retries: int = 3) -> str:
    mime_type = AUDIO_MIME_TYPES.get(audio_path.suffix.lower(), "audio/mp3")
    model = genai.GenerativeModel(model_name)
    size = audio_path.stat().st_size

    if size <= INLINE_SIZE_LIMIT_BYTES:
        return transcribe_chunk(audio_path.read_bytes(), mime_type, model, max_retries)

    log.info("  audio is %.1fMB / %.0fs, splitting into overlapping %ds chunks", size / 1024 / 1024, duration, CHUNK_SECONDS)
    with tempfile.TemporaryDirectory() as tmp:
        chunks = split_audio(audio_path, Path(tmp), duration, CHUNK_SECONDS, CHUNK_OVERLAP_SECONDS)
        combined = None
        for i, chunk_path in enumerate(chunks, start=1):
            log.info("  transcribing chunk %d/%d", i, len(chunks))
            part = transcribe_chunk(chunk_path.read_bytes(), mime_type, model, max_retries)
            combined = part if combined is None else merge_overlapping_text(combined, part)
            if i < len(chunks):
                time.sleep(request_delay)
        return combined or ""


def flag_quality_issues(transcript: str, duration: float) -> list[str]:
    issues = []
    char_count = len(transcript.strip())
    if duration > 30 and char_count < duration * 1.0:
        issues.append("transcript unusually short for audio length")
    if char_count > 0:
        arabic_frac = len(ARABIC_SCRIPT_RE.findall(transcript)) / char_count
        if arabic_frac > 0.4:
            issues.append("mostly Arabic/Urdu script text (expected Roman Urdu)")
    return issues


def run_cleanup(output_dir: Path, patterns: list[str]) -> None:
    """Move already-processed videos matching EXCLUDE_TITLE_PATTERNS out of
    the main audio/transcripts folders (into data/excluded/) and out of the
    manifest, without deleting anything. Run once after adding/changing an
    exclusion pattern to retroactively clean up videos processed before the
    pattern existed."""
    manifest_path = output_dir / "manifest.jsonl"
    known = load_known_metadata(manifest_path)
    excluded_dir = output_dir / "excluded"
    (excluded_dir / "audio").mkdir(parents=True, exist_ok=True)
    (excluded_dir / "transcripts").mkdir(parents=True, exist_ok=True)

    kept, moved = [], 0
    for video_id, record in known.items():
        if not is_excluded(record.get("title", ""), patterns):
            kept.append(record)
            continue
        moved += 1
        log.info("  [excluded] %s (%s)", record.get("title"), video_id)
        for field, subdir in (("audio_file", "audio"), ("transcript_file", "transcripts")):
            fname = record.get(field)
            if not fname:
                continue
            src = output_dir / subdir / fname
            if src.exists():
                src.rename(excluded_dir / subdir / fname)

    manifest_path.write_text("", encoding="utf-8")
    for record in kept:
        record = dict(record)
        record.pop("date", None)  # internal alias, not part of the on-disk schema
        append_jsonl(manifest_path, record)

    log.info("Cleanup done: moved %d excluded video(s) to %s, %d remain in the manifest.", moved, excluded_dir, len(kept))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", nargs="+", default=DEFAULT_CHANNEL_URLS, help="One or more YouTube channel URLs")
    parser.add_argument("--output-dir", default="data", help="Where audio/transcripts are stored")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model name")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N videos")
    parser.add_argument(
        "--request-delay",
        type=float,
        default=4.0,
        help="Seconds to sleep between Gemini requests (rate-limit safety)",
    )
    parser.add_argument(
        "--cleanup-excluded",
        action="store_true",
        help="Move already-processed videos matching EXCLUDE_TITLE_PATTERNS out of the "
             "manifest/folders and exit, instead of running the pipeline.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    if args.cleanup_excluded:
        run_cleanup(output_dir, EXCLUDE_TITLE_PATTERNS)
        return

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.error("GEMINI_API_KEY environment variable is not set. Aborting.")
        sys.exit(1)
    genai.configure(api_key=api_key)

    audio_dir = output_dir / "audio"
    transcript_dir = output_dir / "transcripts"
    manifest_path = output_dir / "manifest.jsonl"
    failures_path = output_dir / "failures.jsonl"
    audio_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir.mkdir(parents=True, exist_ok=True)

    known = load_known_metadata(manifest_path)
    title_index: dict[str, list[str]] = {}
    for vid, record in known.items():
        title_index.setdefault(normalize_title(record.get("title", "")), []).append(vid)

    log.info("Fetching video list from %s (videos + shorts + streams) ...", ", ".join(args.channel))
    videos = list_channel_videos(args.channel, args.limit)

    if EXCLUDE_TITLE_PATTERNS:
        before = len(videos)
        excluded = [v for v in videos if is_excluded(v.get("title", ""), EXCLUDE_TITLE_PATTERNS)]
        videos = [v for v in videos if v not in excluded]
        if excluded:
            log.info(
                "Excluded %d video(s) matching %s (e.g. %s)",
                len(excluded), EXCLUDE_TITLE_PATTERNS, excluded[0].get("title"),
            )

    extra_urls_path = Path(EXTRA_URLS_FILE)
    extra_urls = load_extra_urls(extra_urls_path)
    if extra_urls:
        log.info("Resolving %d extra URL(s) from %s ...", len(extra_urls), extra_urls_path)
        seen_ids = {v.get("id") for v in videos}
        for url in extra_urls:
            resolved = resolve_extra_url(url)
            if resolved and resolved["id"] not in seen_ids:
                videos.append(resolved)
                seen_ids.add(resolved["id"])
    else:
        log.info(
            "Reminder: guest podcast appearances aren't included in the channel scan. "
            "Add their video URLs (one per line) to %s when you're ready to include them.",
            extra_urls_path,
        )

    log.info("Found %d video(s) to process.", len(videos))

    failure_count = 0
    for i, video in enumerate(videos, start=1):
        video_id = video.get("id")
        video_url = video.get("url") or f"https://www.youtube.com/watch?v={video_id}"
        fallback_title = video.get("title", video_id)
        log.info("[%d/%d] %s (%s)", i, len(videos), fallback_title, video_id)

        meta = known.get(video_id)

        # Backfill path: transcript already exists (e.g. from before this
        # feature existed) — rename transcript + audio to the dated format
        # and record it in the manifest, without re-downloading/re-transcribing.
        existing_transcript = find_existing_transcript(transcript_dir, video_id)
        if existing_transcript:
            if meta is None:
                meta = fetch_video_metadata(video_url) or {"title": fallback_title, "date": UNKNOWN_DATE, "description": ""}
            new_path = transcript_dir / transcript_filename(meta["date"], meta["title"] or fallback_title, video_id)
            if existing_transcript != new_path:
                existing_transcript.rename(new_path)
                log.info("  [backfill] renamed -> %s", new_path.name)
            existing_audio = find_existing_audio(audio_dir, video_id)
            if existing_audio:
                rename_if_bare(existing_audio, video_id, meta)
            if video_id not in known:
                title = meta["title"] or fallback_title
                norm = normalize_title(title)
                is_dup = bool(title_index.get(norm))
                record = {
                    "video_id": video_id,
                    "title": title,
                    "publish_date": meta["date"],
                    "url": video_url,
                    "description": meta.get("description", ""),
                    "transcript_file": new_path.name,
                    "possible_duplicate": is_dup,
                }
                append_jsonl(manifest_path, record)
                known[video_id] = record
                title_index.setdefault(norm, []).append(video_id)
            log.info("  [skip transcribe] transcript already exists")
            continue

        if meta is None:
            meta = fetch_video_metadata(video_url) or {"title": fallback_title, "date": UNKNOWN_DATE, "description": ""}

        try:
            audio_path, dl_meta = download_audio(video_id, video_url, audio_dir, meta)
            if audio_path is None:
                raise RuntimeError("download produced no audio file")
        except Exception as exc:  # noqa: BLE001
            log.error("  download failed: %s", exc)
            append_jsonl(failures_path, {"video_id": video_id, "url": video_url, "stage": "download", "error": str(exc)})
            failure_count += 1
            continue

        if dl_meta:
            meta = dl_meta  # authoritative title/date/description from the actual download

        title = meta["title"] or fallback_title
        date_str = meta["date"]
        norm_title = normalize_title(title)
        is_duplicate = bool(title_index.get(norm_title))
        if is_duplicate:
            log.warning("  possible duplicate of %s (same title)", title_index[norm_title][0])
        transcript_path = transcript_dir / transcript_filename(date_str, title, video_id)

        try:
            duration = probe_duration(audio_path)
            transcript = transcribe_audio(audio_path, args.model, args.request_delay, duration)
            transcript_path.write_text(f"[Published: {date_str}]\n\n{transcript}", encoding="utf-8")
            log.info("  transcribed -> %s", transcript_path.name)
        except Exception as exc:  # noqa: BLE001
            log.error("  transcription failed: %s", exc)
            append_jsonl(failures_path, {"video_id": video_id, "url": video_url, "stage": "transcribe", "error": str(exc)})
            failure_count += 1
            continue

        quality_flags = flag_quality_issues(transcript, duration)
        if quality_flags:
            log.warning("  quality flags: %s", ", ".join(quality_flags))

        record = {
            "video_id": video_id,
            "title": title,
            "publish_date": date_str,
            "url": video_url,
            "description": meta.get("description", ""),
            "duration_seconds": round(duration),
            "model": args.model,
            "audio_file": audio_path.name,
            "transcript_file": transcript_path.name,
            "possible_duplicate": is_duplicate,
            "quality_flags": quality_flags,
        }
        append_jsonl(manifest_path, record)
        known[video_id] = record
        title_index.setdefault(norm_title, []).append(video_id)

        time.sleep(args.request_delay)

    log.info("Done. %d succeeded, %d failed.", len(videos) - failure_count, failure_count)
    if failure_count:
        log.warning("See %s for details on failed videos.", failures_path)


if __name__ == "__main__":
    main()
