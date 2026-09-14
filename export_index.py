#!/usr/bin/env python3
"""Export the local Chroma index into a small, git-friendly format for the
public Q&A web app (app.py) to deploy with.

Usage:
    python3 export_index.py

Reads embeddings + chunk text from data/index/chroma (built by
build_index.py) and writes:
    data/public_index/embeddings.npy   - one row per chunk, float32
    data/public_index/chunks.jsonl     - one line per chunk, same row order

Run this whenever you want the deployed web app to pick up newly-indexed
videos (after running build_index.py again) — it's a full re-export each
time, not incremental, and safe to run as often as you like. Commit the
two output files to git so Streamlit Community Cloud picks them up on
redeploy: the raw Chroma database itself stays out of git (binary, bloats
on every rebuild — see ARCHITECTURE.md) but this exported form is compact
enough to track normally.
"""

import argparse
import json
from pathlib import Path

import chromadb
import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="data", help="Where the index/ and public_index/ folders live")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    index_dir = output_dir / "index"
    public_dir = output_dir / "public_index"
    public_dir.mkdir(parents=True, exist_ok=True)

    client = chromadb.PersistentClient(path=str(index_dir / "chroma"))
    collection = client.get_or_create_collection("arn_transcripts")

    result = collection.get(include=["embeddings", "documents", "metadatas"])
    ids = result["ids"]
    embeddings = np.array(result["embeddings"], dtype=np.float32)
    documents = result["documents"]
    metadatas = result["metadatas"]

    if not ids:
        raise SystemExit("No chunks found in data/index/chroma — run build_index.py first.")

    np.save(public_dir / "embeddings.npy", embeddings)

    chunks_path = public_dir / "chunks.jsonl"
    with chunks_path.open("w", encoding="utf-8") as f:
        for chunk_id, doc, meta in zip(ids, documents, metadatas):
            f.write(json.dumps({
                "id": chunk_id,
                "text": doc,
                "video_id": meta.get("video_id", ""),
                "title": meta.get("title", ""),
                "publish_date": meta.get("publish_date", ""),
                "url": meta.get("url", ""),
            }, ensure_ascii=False) + "\n")

    print(f"Exported {len(ids)} chunks ({embeddings.shape[1]}-dim) to {public_dir}/")


if __name__ == "__main__":
    main()
