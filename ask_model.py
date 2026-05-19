"""
ask_model.py — Query your Power BI semantic model using RAG + Claude.

Usage:
    python ask_model.py <chunks.jsonl> "<question>"
    python ask_model.py <chunks.jsonl>          # interactive mode

Options:
    --api-key <key>    Anthropic API key (or set ANTHROPIC_API_KEY env var)
    --top-k <n>        Number of chunks to retrieve per query (default: 12)
    --all-chunks       Send all chunks as context (for broad questions)

Requires:
    pip install anthropic
"""

import json
import os
import re
import sys
from pathlib import Path


def load_chunks(jsonl_path: str) -> list[dict]:
    chunks = []
    for line in Path(jsonl_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            chunks.append(json.loads(line))
    return chunks


def _tf_score(query: str, text: str) -> float:
    tokens = re.findall(r"\w+", query.lower())
    text_lower = text.lower()
    return sum(text_lower.count(tok) for tok in tokens) / max(len(tokens), 1)


def retrieve(chunks: list[dict], question: str, top_k: int = 12) -> list[dict]:
    scored = [(c, _tf_score(question, c["text"])) for c in chunks]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = [c for c, _ in scored[:top_k]]
    # always include the overview chunk
    overview = next((c for c in chunks if c["type"] == "overview"), None)
    if overview and overview not in top:
        top = [overview] + top[:top_k - 1]
    return top


def build_context(chunks: list[dict]) -> str:
    parts = []
    for c in chunks:
        parts.append(f"[{c['type'].upper()}] {c['text']}")
    return "\n\n---\n\n".join(parts)


def ask(
    question: str,
    chunks: list[dict],
    api_key: str,
    top_k: int = 12,
    all_chunks: bool = False,
    model: str = "claude-haiku-4-5-20251001",
) -> tuple[str, int]:
    import anthropic

    relevant = chunks if all_chunks else retrieve(chunks, question, top_k)
    context = build_context(relevant)

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=1024,
        system=(
            "You are an expert Power BI consultant. "
            "Answer questions about the semantic model using ONLY the context provided. "
            "Be concise and precise. If the answer is not in the context, say so."
        ),
        messages=[
            {
                "role": "user",
                "content": f"Context from the semantic model:\n\n{context}\n\n---\n\nQuestion: {question}",
            }
        ],
    )
    return message.content[0].text, len(relevant)


def _pop_arg(args: list[str], flag: str, default=None):
    if flag in args:
        idx = args.index(flag)
        val = args[idx + 1]
        del args[idx:idx + 2]
        return val
    return default


def main():
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    api_key = _pop_arg(args, "--api-key") or os.environ.get("ANTHROPIC_API_KEY", "")
    top_k = int(_pop_arg(args, "--top-k") or 12)
    all_chunks = "--all-chunks" in args
    if all_chunks:
        args.remove("--all-chunks")

    if not api_key:
        print("Error: set ANTHROPIC_API_KEY or pass --api-key <key>")
        sys.exit(1)

    jsonl_path = args[0]
    question = args[1] if len(args) > 1 else None

    if not Path(jsonl_path).exists():
        print(f"Error: file not found: {jsonl_path}")
        sys.exit(1)

    chunks = load_chunks(jsonl_path)
    print(f"Loaded {len(chunks)} chunks from {jsonl_path}")
    print()

    if question:
        answer, n = ask(question, chunks, api_key, top_k, all_chunks)
        print(f"Q: {question}  [{n} chunks used]")
        print(f"\nA: {answer}")
    else:
        print("Interactive mode — type your question (Ctrl+C to exit)\n")
        while True:
            try:
                q = input("Q: ").strip()
                if not q:
                    continue
                answer, n = ask(q, chunks, api_key, top_k, all_chunks)
                print(f"\nA: {answer}  [{n} chunks used]\n")
            except KeyboardInterrupt:
                print("\nBye!")
                break


if __name__ == "__main__":
    main()
