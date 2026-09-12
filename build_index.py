#!/usr/bin/env python3
"""Build a searchable vector index from the ARN Brain transcripts.

Usage:
    export GEMINI_API_KEY="your-key-here"
    python3 build_index.py
    python3 build_index.py --limit 5      # test on a few videos first

Reads data/manifest.jsonl + data/transcripts/*.txt, splits each transcript
into overlapping chunks, embeds each chunk with Gemini, and stores them in a
local Chroma vector database at data/index/. Resumable: videos already
indexed are tracked in data/index/indexed_videos.jsonl and skipped on
re-run, the same pattern arn_pipeline.py uses for downloads/transcripts.
Excluded videos (data/excluded/) are never indexed, since they're already
out of manifest.jsonl.
"""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import chromadb
import google.generativeai as genai

EMBED_MODEL = "models/text-embedding-004"
CHUNK_SIZE = 1200
CHUNK_OVERLAP = 150

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("build_index")


def load_manifest(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def load_indexed_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            ids.add(json.loads(line)["video_id"])
    return ids


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        chunks.append(text[start:end])
        if end == n:
            break
        start = end - overlap
    return chunks


def embed(text: str, task_type: str) -> list[float]:
    result = genai.embed_content(model=EMBED_MODEL, content=text, task_type=task_type)
    return result["embedding"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="data", help="Where manifest/transcripts/index live")
    parser.add_argument("--limit", type=int, help="Only index the first N not-yet-indexed videos (for testing)")
    parser.add_argument("--request-delay", type=float, default=0.3, help="Delay between embedding calls (seconds)")
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("Set the GEMINI_API_KEY environment variable first.")
    genai.configure(api_key=api_key)

    output_dir = Path(args.output_dir)
    manifest_path = output_dir / "manifest.jsonl"
    transcript_dir = output_dir / "transcripts"
    index_dir = output_dir / "index"
    indexed_path = index_dir / "indexed_videos.jsonl"
    index_failures_path = index_dir / "index_failures.jsonl"
    index_dir.mkdir(parents=True, exist_ok=True)

    client = chromadb.PersistentClient(path=str(index_dir / "chroma"))
    collection = client.get_or_create_collection("arn_transcripts")

    manifest = load_manifest(manifest_path)
    already_indexed = load_indexed_ids(indexed_path)
    pending = [r for r in manifest if r["video_id"] not in already_indexed]
    if args.limit:
        pending = pending[: args.limit]

    log.info("%d video(s) already indexed, %d pending", len(already_indexed), len(pending))

    indexed_count = 0
    for i, record in enumerate(pending, 1):
        video_id = record["video_id"]
        title = record.get("title", "")
        transcript_file = record.get("transcript_file")
        if not transcript_file:
            continue
        transcript_path = transcript_dir / transcript_file
        if not transcript_path.exists():
            log.warning("[%d/%d] %s: transcript file missing, skipping", i, len(pending), title)
            continue

        log.info("[%d/%d] %s (%s)", i, len(pending), title, video_id)
        text = transcript_path.read_text(encoding="utf-8")
        chunks = chunk_text(text)
        if not chunks:
            continue

        ids, embeddings, documents, metadatas = [], [], [], []
        try:
            for chunk_idx, chunk in enumerate(chunks):
                embeddings.append(embed(chunk, task_type="retrieval_document"))
                ids.append(f"{video_id}::{chunk_idx}")
                documents.append(chunk)
                metadatas.append({
                    "video_id": video_id,
                    "title": title,
                    "publish_date": record.get("publish_date", ""),
                    "url": record.get("url", ""),
                    "chunk_index": chunk_idx,
                })
                time.sleep(args.request_delay)
        except Exception as exc:  # noqa: BLE001 - rate limit or transient error shouldn't stop the run
            log.error("  embedding failed: %s", exc)
            append_jsonl(index_failures_path, {"video_id": video_id, "title": title, "error": str(exc)})
            continue

        collection.add(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)
        append_jsonl(indexed_path, {
            "video_id": video_id,
            "chunk_count": len(chunks),
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        })
        indexed_count += 1

    log.info("Done. %d video(s) newly indexed this run.", indexed_count)


if __name__ == "__main__":
    main()
