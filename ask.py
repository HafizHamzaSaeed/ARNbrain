#!/usr/bin/env python3
"""Ask a question against the ARN Brain knowledge base.

Usage:
    export GEMINI_API_KEY="your-key-here"
    python3 ask.py "What has ARN said about investing in gold?"
    python3 ask.py                          # interactive mode, ask multiple questions

Requires build_index.py to have been run first. Retrieves the most relevant
transcript chunks (via Gemini embeddings + Chroma), then asks Gemini to
answer using only that context, citing which video(s) and date(s) it's
drawing from — since ARN's views may have changed over time, more recent
statements should be weighted more heavily when they conflict with older ones.
"""

import argparse
import os
from pathlib import Path

import chromadb
import google.generativeai as genai

EMBED_MODEL = "models/text-embedding-004"
ANSWER_MODEL = "gemini-3.5-flash"

ANSWER_PROMPT = """You are answering a question about what Abdul Rehman Najam (ARN) \
has said in his videos and podcast appearances, using only the transcript excerpts \
below as your source of truth. Each excerpt is labeled with its publish date and \
video title.

Instructions:
- Answer only from the excerpts given. If they don't contain enough to answer, say so.
- ARN's views on some topics may have changed over time. If excerpts from different \
dates seem to disagree, say so explicitly and give more weight to the more recent one.
- Cite the date and video title for each claim you make.
- Answer in English, but you may quote short Roman Urdu phrases from the source \
material directly if they're the clearest evidence for a claim.

Question: {question}

Transcript excerpts:
{context}

Answer:"""


def format_context(results) -> str:
    parts = []
    for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
        parts.append(
            f"[{meta.get('publish_date', 'unknown date')}] {meta.get('title', '')} "
            f"({meta.get('url', '')})\n{doc}"
        )
    return "\n\n---\n\n".join(parts)


def answer(question: str, collection, model, k: int) -> str:
    query_embedding = genai.embed_content(model=EMBED_MODEL, content=question, task_type="retrieval_query")["embedding"]
    results = collection.query(query_embeddings=[query_embedding], n_results=k)
    if not results["documents"] or not results["documents"][0]:
        return "Nothing relevant found in the index yet — has build_index.py been run?"
    context = format_context(results)
    response = model.generate_content(ANSWER_PROMPT.format(question=question, context=context))
    sources = "\n".join(
        f"  - [{m.get('publish_date', '?')}] {m.get('title', '')}"
        for m in results["metadatas"][0]
    )
    return f"{response.text.strip()}\n\nSources consulted:\n{sources}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?", help="Question to ask (omit for interactive mode)")
    parser.add_argument("--output-dir", default="data", help="Where the index lives")
    parser.add_argument("--k", type=int, default=8, help="How many transcript chunks to retrieve")
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("Set the GEMINI_API_KEY environment variable first.")
    genai.configure(api_key=api_key)

    index_dir = Path(args.output_dir) / "index"
    client = chromadb.PersistentClient(path=str(index_dir / "chroma"))
    collection = client.get_or_create_collection("arn_transcripts")
    model = genai.GenerativeModel(ANSWER_MODEL)

    if args.question:
        print(answer(args.question, collection, model, args.k))
        return

    print("ARN Brain — ask a question (blank line to quit)")
    while True:
        question = input("\n> ").strip()
        if not question:
            break
        print(answer(question, collection, model, args.k))


if __name__ == "__main__":
    main()
