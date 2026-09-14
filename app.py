#!/usr/bin/env python3
"""ARN Brain — public web app for asking questions about ARN's transcripts.

Deployed on Streamlit Community Cloud. Reads the exported index
(data/public_index/) committed to this repo by export_index.py, and uses
a Gemini API key stored in Streamlit's secrets (never in this file or the
repo) to embed questions and generate answers.

To run locally for testing:
    export GEMINI_API_KEY="your-key-here"
    streamlit run app.py
"""

import json
import os
from pathlib import Path

import google.generativeai as genai
import numpy as np
import streamlit as st

EMBED_MODEL = "models/gemini-embedding-001"
ANSWER_MODEL = "gemini-3.5-flash"
TOP_K = 8

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


@st.cache_resource
def load_index():
    public_dir = Path("data/public_index")
    embeddings = np.load(public_dir / "embeddings.npy")
    chunks = [json.loads(line) for line in (public_dir / "chunks.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    return embeddings, chunks


@st.cache_resource
def configure_gemini():
    api_key = st.secrets.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        st.error("GEMINI_API_KEY is not set in Streamlit secrets.")
        st.stop()
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(ANSWER_MODEL)


def top_k_chunks(question_embedding: np.ndarray, embeddings: np.ndarray, chunks: list, k: int) -> list:
    norms = np.linalg.norm(embeddings, axis=1) * np.linalg.norm(question_embedding)
    norms[norms == 0] = 1e-10
    similarities = (embeddings @ question_embedding) / norms
    top_indices = np.argsort(-similarities)[:k]
    return [chunks[i] for i in top_indices]


def format_context(matched_chunks: list) -> str:
    parts = []
    for c in matched_chunks:
        parts.append(f"[{c['publish_date']}] {c['title']} ({c['url']})\n{c['text']}")
    return "\n\n---\n\n".join(parts)


def answer(question: str, embeddings: np.ndarray, chunks: list, model) -> tuple[str, list]:
    question_embedding = np.array(
        genai.embed_content(model=EMBED_MODEL, content=question, task_type="retrieval_query")["embedding"],
        dtype=np.float32,
    )
    matched_chunks = top_k_chunks(question_embedding, embeddings, chunks, TOP_K)
    context = format_context(matched_chunks)
    response = model.generate_content(ANSWER_PROMPT.format(question=question, context=context))
    return response.text.strip(), matched_chunks


st.set_page_config(page_title="ARN Brain", page_icon="🧠")
st.title("ARN Brain")
st.caption("Ask a question about what Abdul Rehman Najam has said across his videos and podcast appearances.")

embeddings, chunks = load_index()
model = configure_gemini()

question = st.text_input("Your question")
if question:
    with st.spinner("Searching transcripts and generating an answer..."):
        try:
            result_text, sources = answer(question, embeddings, chunks, model)
        except Exception as exc:  # noqa: BLE001 - surface API/rate-limit errors to the viewer plainly
            st.error(f"Something went wrong: {exc}")
        else:
            st.markdown(result_text)
            with st.expander("Sources consulted"):
                for c in sources:
                    st.markdown(f"- [{c['publish_date']}] {c['title']}")
