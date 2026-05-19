"""
RAG benchmark — quantifies token savings of the RAG approach vs full-doc.

No external dependencies: token counting uses a BPE approximation (len/4),
retrieval simulation uses term-frequency scoring (no vectors required).

Usage:
    from pbi_semantic_doc.benchmark import run_benchmark, format_report

    result = run_benchmark(model)
    print(format_report(result, fmt="md"))
    print(format_report(result, fmt="html"))
    print(format_report(result, fmt="json"))
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .parser import SemanticModel
    from .report_models import ReportMetrics

from .rag_generator import RagGenerator, RagChunk


# ── token counting ─────────────────────────────────────────────────────────────

def _count_tokens(text: str) -> int:
    """BPE approximation: ~4 chars per token."""
    return max(1, len(text) // 4)


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9À-ɏ]+", text.lower())


# ── TF-based retrieval simulation ─────────────────────────────────────────────

def _tf_score(query_tokens: list[str], chunk: RagChunk) -> float:
    doc_tokens = _tokenize(chunk.text)
    if not doc_tokens:
        return 0.0
    freq: dict[str, int] = {}
    for t in doc_tokens:
        freq[t] = freq.get(t, 0) + 1
    score = sum(freq.get(qt, 0) for qt in query_tokens)
    return score / len(doc_tokens)


def _retrieve_top_k(query: str, chunks: list[RagChunk], k: int = 3) -> list[RagChunk]:
    q_tokens = _tokenize(query)
    scored = [(c, _tf_score(q_tokens, c)) for c in chunks]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [c for c, _ in scored[:k]]


# ── question generation ───────────────────────────────────────────────────────

def _generate_questions(
    model: "SemanticModel",
) -> list[tuple[str, str]]:
    """Return (question_text, expected_chunk_id) pairs from model structure."""
    questions: list[tuple[str, str]] = []

    for table in model.visible_tables:
        questions.append((
            f"What columns does the {table.name} table contain?",
            f"table::{table.name}",
        ))
        for measure in table.measures[:4]:
            questions.append((
                f"What does the measure {measure.name} calculate?",
                f"measure::{table.name}::{measure.name}",
            ))

    for rel in model.relationships[:4]:
        questions.append((
            f"How are {rel.from_table} and {rel.to_table} related?",
            f"relationship::{rel.from_table}.{rel.from_column}"
            f"::{rel.to_table}.{rel.to_column}",
        ))

    return questions[:20]


# ── result dataclasses ─────────────────────────────────────────────────────────

@dataclass
class QuestionResult:
    question: str
    expected_id: str
    full_doc_tokens: int
    rag_tokens: int
    retrieved_correctly: bool

    @property
    def token_reduction_pct(self) -> float:
        if self.full_doc_tokens == 0:
            return 0.0
        return (self.full_doc_tokens - self.rag_tokens) / self.full_doc_tokens


@dataclass
class BenchmarkResult:
    model_name: str
    questions: list[QuestionResult] = field(default_factory=list)
    full_doc_tokens: int = 0
    chunk_count: int = 0
    top_k: int = 3

    @property
    def avg_rag_tokens(self) -> float:
        if not self.questions:
            return 0.0
        return sum(q.rag_tokens for q in self.questions) / len(self.questions)

    @property
    def avg_reduction_pct(self) -> float:
        if not self.questions:
            return 0.0
        return sum(q.token_reduction_pct for q in self.questions) / len(self.questions)

    @property
    def retrieval_precision(self) -> float:
        if not self.questions:
            return 0.0
        return sum(1 for q in self.questions if q.retrieved_correctly) / len(self.questions)

    def cost_usd(self, tokens: float, llm: str = "claude-sonnet") -> float:
        """Estimate input-token cost in USD (per-million pricing)."""
        rates = {
            "claude-sonnet": 3.00,
            "claude-haiku":  0.25,
            "claude-opus":  15.00,
            "gpt-4o":        2.50,
            "gpt-4o-mini":   0.15,
        }
        return tokens * rates.get(llm, 3.00) / 1_000_000


# ── public API ─────────────────────────────────────────────────────────────────

def run_benchmark(
    model: "SemanticModel",
    report_metrics: Optional["ReportMetrics"] = None,
    top_k: int = 3,
) -> BenchmarkResult:
    """
    Run the benchmark and return a BenchmarkResult.

    1. Generates a full Markdown doc and counts its tokens.
    2. Generates RAG chunks.
    3. For each auto-generated question, simulates retrieval and counts tokens.
    """
    from .generator import MarkdownGenerator

    full_doc_md = MarkdownGenerator().generate(model)
    full_doc_tokens = _count_tokens(full_doc_md)

    chunks = RagGenerator().generate(model, report_metrics)

    questions = _generate_questions(model)
    results: list[QuestionResult] = []

    for q_text, expected_id in questions:
        retrieved = _retrieve_top_k(q_text, chunks, k=top_k)
        rag_text = "\n\n".join(c.text for c in retrieved)
        rag_tokens = _count_tokens(rag_text)
        retrieved_correctly = any(c.id == expected_id for c in retrieved)
        results.append(QuestionResult(
            question=q_text,
            expected_id=expected_id,
            full_doc_tokens=full_doc_tokens,
            rag_tokens=rag_tokens,
            retrieved_correctly=retrieved_correctly,
        ))

    return BenchmarkResult(
        model_name=model.name,
        questions=results,
        full_doc_tokens=full_doc_tokens,
        chunk_count=len(chunks),
        top_k=top_k,
    )


# ── formatters ─────────────────────────────────────────────────────────────────

def format_report(result: BenchmarkResult, fmt: str = "md") -> str:
    if fmt == "json":
        return _fmt_json(result)
    if fmt == "html":
        return _fmt_html(result)
    return _fmt_md(result)


def _fmt_md(result: BenchmarkResult) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    avg_rag = int(result.avg_rag_tokens)
    reduction = result.avg_reduction_pct
    precision = result.retrieval_precision
    n_correct = sum(1 for q in result.questions if q.retrieved_correctly)

    cost_full = result.cost_usd(result.full_doc_tokens)
    cost_rag = result.cost_usd(result.avg_rag_tokens)

    lines = [
        f"# RAG Benchmark — {result.model_name}",
        "",
        f"> Generated {ts} &nbsp;·&nbsp; "
        f"Questions tested: {len(result.questions)} &nbsp;·&nbsp; "
        f"RAG chunks: {result.chunk_count} &nbsp;·&nbsp; "
        f"Top-K: {result.top_k}",
        "",
        "## Summary",
        "",
        f"| | Full Document | RAG (top-{result.top_k}) | Savings |",
        "|---|---:|---:|---:|",
        f"| Tokens / query | {result.full_doc_tokens:,} | {avg_rag:,} | **{reduction:.1%}** |",
        f"| Cost / query* | ${cost_full:.4f} | ${cost_rag:.4f} | **{reduction:.1%}** |",
        f"| Cost / 1,000 queries | ${cost_full * 1000:.2f} | ${cost_rag * 1000:.2f} | **{reduction:.1%}** |",
        "",
        f"\\* Claude Sonnet pricing (~$3/M input tokens)",
        "",
        f"**Retrieval precision:** {precision:.1%} "
        f"({n_correct}/{len(result.questions)} questions found the correct chunk in top-{result.top_k})",
        "",
        "## Question Detail",
        "",
        "| Question | Full doc | RAG | Reduction | Hit? |",
        "|---|---:|---:|---:|:---:|",
    ]

    for q in result.questions:
        q_display = q.question[:65] + ("…" if len(q.question) > 65 else "")
        hit = "✓" if q.retrieved_correctly else "✗"
        lines.append(
            f"| {q_display} "
            f"| {q.full_doc_tokens:,} "
            f"| {q.rag_tokens:,} "
            f"| {q.token_reduction_pct:.1%} "
            f"| {hit} |"
        )

    lines += [
        "",
        "---",
        "",
        "*Generated by "
        "[pbi-semantic-doc](https://github.com/ViciusLio/pbi-semantic-doc)*",
    ]
    return "\n".join(lines) + "\n"


def _fmt_json(result: BenchmarkResult) -> str:
    data = {
        "model_name": result.model_name,
        "summary": {
            "full_doc_tokens": result.full_doc_tokens,
            "avg_rag_tokens": int(result.avg_rag_tokens),
            "avg_token_reduction_pct": round(result.avg_reduction_pct * 100, 1),
            "retrieval_precision_pct": round(result.retrieval_precision * 100, 1),
            "chunk_count": result.chunk_count,
            "top_k": result.top_k,
            "question_count": len(result.questions),
        },
        "questions": [
            {
                "question": q.question,
                "expected_chunk_id": q.expected_id,
                "full_doc_tokens": q.full_doc_tokens,
                "rag_tokens": q.rag_tokens,
                "token_reduction_pct": round(q.token_reduction_pct * 100, 1),
                "retrieved_correctly": q.retrieved_correctly,
            }
            for q in result.questions
        ],
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


def _fmt_html(result: BenchmarkResult) -> str:
    avg_rag = int(result.avg_rag_tokens)
    reduction_pct = result.avg_reduction_pct * 100
    precision_pct = result.retrieval_precision * 100
    cost_full = result.cost_usd(result.full_doc_tokens)
    cost_rag = result.cost_usd(result.avg_rag_tokens)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows = ""
    for q in result.questions:
        hit_cell = (
            '<td style="color:#22863a;text-align:center">✓</td>'
            if q.retrieved_correctly
            else '<td style="color:#cb2431;text-align:center">✗</td>'
        )
        q_display = q.question[:72] + ("…" if len(q.question) > 72 else "")
        rows += (
            f"<tr>"
            f"<td>{q_display}</td>"
            f"<td style='text-align:right'>{q.full_doc_tokens:,}</td>"
            f"<td style='text-align:right'>{q.rag_tokens:,}</td>"
            f"<td style='text-align:right'>{q.token_reduction_pct:.1%}</td>"
            f"{hit_cell}"
            f"</tr>\n"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RAG Benchmark — {result.model_name}</title>
<style>
  :root {{
    --blue: #0066cc; --green: #22863a; --red: #cb2431;
    --bg: #f6f8fa; --border: #e1e4e8;
  }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 960px; margin: 2rem auto; padding: 0 1.5rem; color: #24292e;
  }}
  h1 {{ font-size: 1.6rem; color: #1a1a2e; margin-bottom: 0.25rem; }}
  .meta {{ color: #6a737d; font-size: 0.85rem; margin-bottom: 2rem; }}
  .cards {{
    display: grid; grid-template-columns: repeat(3, 1fr);
    gap: 1rem; margin-bottom: 2rem;
  }}
  .card {{
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 1.25rem; text-align: center;
  }}
  .card .value {{ font-size: 2rem; font-weight: 700; color: var(--blue); }}
  .card .label {{ font-size: 0.8rem; color: #6a737d; margin-top: 0.3rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th {{
    background: #1a1a2e; color: #fff; padding: 0.65rem 0.75rem;
    text-align: left; font-weight: 600;
  }}
  td {{ padding: 0.55rem 0.75rem; border-bottom: 1px solid var(--border); }}
  tr:hover td {{ background: #f0f4ff; }}
  footer {{ margin-top: 2rem; font-size: 0.8rem; color: #6a737d; }}
  @media(max-width:600px) {{ .cards {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<h1>RAG Benchmark — {result.model_name}</h1>
<p class="meta">
  Generated {ts} &nbsp;·&nbsp;
  {len(result.questions)} questions &nbsp;·&nbsp;
  {result.chunk_count} chunks &nbsp;·&nbsp;
  Top-{result.top_k} retrieval
</p>

<div class="cards">
  <div class="card">
    <div class="value">{reduction_pct:.0f}%</div>
    <div class="label">Average token savings</div>
  </div>
  <div class="card">
    <div class="value">{precision_pct:.0f}%</div>
    <div class="label">Retrieval precision</div>
  </div>
  <div class="card">
    <div class="value">${cost_full:.4f} → ${cost_rag:.4f}</div>
    <div class="label">Cost per query (Claude Sonnet)</div>
  </div>
</div>

<table>
<thead>
  <tr>
    <th>Question</th>
    <th style="text-align:right">Full doc tokens</th>
    <th style="text-align:right">RAG tokens</th>
    <th style="text-align:right">Reduction</th>
    <th style="text-align:center">Hit?</th>
  </tr>
</thead>
<tbody>
{rows}</tbody>
</table>

<footer>
  * Claude Sonnet pricing (~$3/M input tokens) &nbsp;·&nbsp;
  Generated by
  <a href="https://github.com/ViciusLio/pbi-semantic-doc">pbi-semantic-doc</a>
</footer>
</body>
</html>
"""
