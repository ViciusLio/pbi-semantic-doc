"""
CLI entry point for pbi-semantic-doc.

Usage:
    pbi-semantic-doc <model_path> [--output <path>] [--format md]

Examples:
    pbi-semantic-doc ./MyReport.SemanticModel
    pbi-semantic-doc ./MyReport.SemanticModel --output ./docs/MODEL.md
    pbi-semantic-doc . --output DOC.md
    pbi-semantic-doc ./MyProject --combined --output ./docs/FULL.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

from .parser import TmdlParser
from .generator import MarkdownGenerator
from .html_generator import HtmlGenerator
from .report_parser import ReportParser
from .report_generator import ReportGenerator
from .rag_generator import RagGenerator
from .benchmark import run_benchmark, format_report as format_benchmark


def _safe_name(name: str) -> str:
    """Convert a model/report name to a filesystem-safe string."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    safe = re.sub(r"\s+", "_", safe.strip())
    return safe or "output"


def _strip_h1(content: str) -> str:
    """Remove the first top-level heading from a Markdown document.

    Used when assembling a combined document so each sub-document's h1
    becomes an h2 section heading in the unified output.
    """
    lines = content.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    # Drop leading blank lines left after the removed h1
    while lines and not lines[0].strip():
        lines = lines[1:]
    return "\n".join(lines)


def _combined_markdown(
    model,
    report_metrics,
    project_name: str,
) -> str:
    """Build a single unified Markdown document with both model and report sections."""
    today = date.today().strftime("%Y-%m-%d")

    # ── top-level header ──────────────────────────────────────────────────
    header_lines = [
        f"# DOC — {project_name}",
        "",
        f"> Combined Power BI project documentation &middot; Generated {today}",
        "",
        "---",
        "",
        "## Contents",
        "",
    ]
    if model:
        tables = len(model.visible_tables)
        measures = sum(len(t.measures) for t in model.visible_tables)
        rels = len(model.relationships)
        header_lines.append(
            f"- [Semantic Model](#semantic-model)"
            f" — {tables} table{'s' if tables != 1 else ''},"
            f" {measures} measure{'s' if measures != 1 else ''},"
            f" {rels} relationship{'s' if rels != 1 else ''}"
        )
    if report_metrics:
        pages = report_metrics.total_pages
        visuals = report_metrics.total_visuals
        header_lines.append(
            f"- [Report](#report)"
            f" — {pages} page{'s' if pages != 1 else ''},"
            f" {visuals} visual{'s' if visuals != 1 else ''}"
        )
    header_lines.append("")

    parts: list[str] = ["\n".join(header_lines)]

    # ── semantic model section ────────────────────────────────────────────
    if model:
        gen = MarkdownGenerator()
        body = _strip_h1(gen.generate(model))
        parts.append(f"## Semantic Model\n\n{body}")

    # ── report section ────────────────────────────────────────────────────
    if report_metrics:
        gen = ReportGenerator()
        body = _strip_h1(gen.generate_markdown(report_metrics))
        parts.append(f"## Report\n\n{body}")

    return "\n\n---\n\n".join(parts)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pbi-semantic-doc",
        description="Generate documentation from a Power BI semantic model (TMDL format) or analyze report structure.",
    )
    p.add_argument(
        "model_path",
        metavar="PATH",
        help=(
            "Path to analyze. Accepts:\n"
            "  - a *.SemanticModel directory (for model documentation)\n"
            "  - a *.Report directory (for report analysis with --analyze-report)\n"
            "  - a .pbip project folder (for combined analysis with --combined)\n"
            "  - a parent folder containing the above"
        ),
    )
    p.add_argument(
        "--analyze-report",
        action="store_true",
        help="Analyze report structure instead of semantic model",
    )
    p.add_argument(
        "--combined",
        action="store_true",
        help="Analyze both semantic model and report (for .pbip projects)",
    )
    p.add_argument(
        "--output", "-o",
        metavar="OUTPUT_PATH",
        default=None,
        help=(
            "Where to write the output file. "
            "Defaults to DOC_<name>.md in the parent of the input folder."
        ),
    )
    p.add_argument(
        "--format", "-f",
        choices=["md", "html", "json", "text", "rag"],
        default="md",
        help=(
            "Output format: md (default), html (self-contained printable), "
            "json, text, rag (JSONL chunks for AI retrieval)"
        ),
    )
    p.add_argument(
        "--benchmark",
        action="store_true",
        help=(
            "Run a RAG efficiency benchmark: compares token usage between "
            "full-doc and RAG approaches. Output format controlled by --format."
        ),
    )

    # Embedding options (only meaningful with --format rag)
    emb = p.add_argument_group("embedding options (used with --format rag)")
    emb.add_argument(
        "--embed",
        choices=["voyage", "ollama", "fastembed"],
        default=None,
        metavar="PROVIDER",
        help="Embedding provider: voyage, ollama, fastembed",
    )
    emb.add_argument(
        "--embed-model",
        default=None,
        metavar="MODEL",
        help=(
            "Model to use for embeddings. "
            "Defaults: voyage→voyage-3, ollama→bge-m3, fastembed→BAAI/bge-m3"
        ),
    )
    emb.add_argument(
        "--embed-url",
        default=None,
        metavar="URL",
        help="Base URL for the Ollama server (default: http://localhost:11434)",
    )
    emb.add_argument(
        "--api-key",
        default=None,
        metavar="KEY",
        help="API key for the Voyage provider",
    )

    p.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress informational output.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    # Ensure stdout can handle Unicode on Windows (cp1252 terminals)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)

    if args.benchmark:
        return run_benchmark_cmd(args)
    elif args.combined:
        return analyze_combined(args)
    elif args.analyze_report:
        return analyze_report(args)
    else:
        return analyze_semantic_model(args)


def _build_embedder(args):
    """Return an EmbeddingProvider from CLI args, or None if --embed not set."""
    if not getattr(args, "embed", None):
        return None
    from .embedder import get_provider
    return get_provider(
        name=args.embed,
        model=getattr(args, "embed_model", None),
        api_key=getattr(args, "api_key", None),
        base_url=getattr(args, "embed_url", None),
    )


def _embed_chunks(chunks, embedder, quiet: bool) -> list:
    """Attach embeddings to a list of RagChunks, with progress output."""
    if not embedder:
        return chunks
    total = len(chunks)
    if not quiet:
        print(f"  Embedding {total} chunks with {embedder.model_name}...")
    texts = [c.text for c in chunks]
    try:
        vectors = embedder.embed_batch(texts)
    except Exception as exc:
        print(f"Warning: embedding failed — {exc}", file=sys.stderr)
        return chunks
    for chunk, vec in zip(chunks, vectors):
        chunk.embedding = vec
    if not quiet:
        dim = len(vectors[0]) if vectors else "?"
        print(f"  Done. Dimension: {dim}")
    return chunks


def run_benchmark_cmd(args) -> int:
    """Run the RAG benchmark and output the report."""
    model_path = Path(args.model_path).resolve()
    if not model_path.exists():
        print(f"Error: path does not exist: {model_path}", file=sys.stderr)
        return 1

    try:
        parser = TmdlParser()
        model = parser.parse(model_path)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Unexpected error while parsing: {exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"Running benchmark on: {model.name}")

    result = run_benchmark(model)

    fmt = args.format if args.format in ("md", "html", "json") else "md"
    content = format_benchmark(result, fmt=fmt)

    if args.output:
        output_path = Path(args.output).resolve()
    else:
        ext = ".html" if fmt == "html" else (".json" if fmt == "json" else ".md")
        output_path = model_path.parent / f"BENCHMARK_{_safe_name(model.name)}{ext}"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")

    if not args.quiet:
        print(
            f"✓ Benchmark saved: {output_path}\n"
            f"  Questions tested : {len(result.questions)}\n"
            f"  Token savings    : {result.avg_reduction_pct:.1%}\n"
            f"  Retrieval prec.  : {result.retrieval_precision:.1%}"
        )
    return 0


def analyze_semantic_model(args) -> int:
    """Analizza solo il modello semantico."""
    model_path = Path(args.model_path).resolve()
    if not model_path.exists():
        print(f"Error: path does not exist: {model_path}", file=sys.stderr)
        return 1

    # Parse first so we can use the model name in the default output filename
    try:
        parser = TmdlParser()
        model = parser.parse(model_path)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Unexpected error while parsing: {exc}", file=sys.stderr)
        return 1

    # Determine output path — place NEXT TO (not inside) the .SemanticModel folder
    if args.output:
        output_path = Path(args.output).resolve()
    else:
        if args.format == "html":
            ext = ".html"
        elif args.format == "rag":
            ext = ".jsonl"
        else:
            ext = ".md"
        output_path = model_path.parent / f"DOC_{_safe_name(model.name)}{ext}"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if args.format == "html":
        content = HtmlGenerator().generate(model)
    elif args.format == "rag":
        embedder = _build_embedder(args)
        rag_gen = RagGenerator()
        chunks = rag_gen.generate(model=model)
        chunks = _embed_chunks(chunks, embedder, args.quiet)
        content = "\n".join(c.to_json() for c in chunks) + "\n"
    else:
        content = MarkdownGenerator().generate(model)
    output_path.write_text(content, encoding="utf-8")

    if not args.quiet:
        tables = len(model.visible_tables)
        measures = sum(len(t.measures) for t in model.visible_tables)
        relationships = len(model.relationships)
        if args.format == "rag":
            chunk_count = content.count("\n")
            print(
                f"✓ RAG chunks generated: {output_path}\n"
                f"  Model   : {model.name}\n"
                f"  Chunks  : {chunk_count}\n"
                f"  Tables  : {tables}\n"
                f"  Measures: {measures}\n"
                f"  Rels    : {relationships}"
            )
        else:
            print(
                f"✓ Documentation generated: {output_path}\n"
                f"  Model   : {model.name}\n"
                f"  Tables  : {tables}\n"
                f"  Measures: {measures}\n"
                f"  Rels    : {relationships}"
            )

    return 0


def analyze_report(args) -> int:
    """Analizza solo il report."""
    report_path = Path(args.model_path).resolve()
    if not report_path.exists():
        print(f"Error: path does not exist: {report_path}", file=sys.stderr)
        return 1

    try:
        parser = ReportParser()
        report = parser.parse(report_path)
        metrics = report.calculate_metrics()
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Unexpected error while parsing: {exc}", file=sys.stderr)
        return 1

    # Determine output path — place NEXT TO (not inside) the .Report folder
    if args.output:
        output_path = Path(args.output).resolve()
    else:
        safe = _safe_name(metrics.report_name)
        if args.format == "json":
            output_path = report_path.parent / f"DOC_{safe}.json"
        elif args.format == "text":
            output_path = None  # print to console
        elif args.format == "html":
            output_path = report_path.parent / f"DOC_{safe}.html"
        else:
            output_path = report_path.parent / f"DOC_{safe}.md"

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)

    rpt_gen = ReportGenerator()

    if args.format == "json":
        content = rpt_gen.generate_json(metrics)
    elif args.format == "text":
        content = rpt_gen.generate_text(metrics)
    elif args.format == "html":
        content = HtmlGenerator().generate_report(metrics)
    else:
        content = rpt_gen.generate_markdown(metrics)

    if output_path:
        output_path.write_text(content, encoding="utf-8")
        if not args.quiet:
            print(
                f"✓ Report analysis saved: {output_path}\n"
                f"  Report  : {metrics.report_name}\n"
                f"  Format  : {metrics.report_format}\n"
                f"  Pages   : {metrics.total_pages}\n"
                f"  Visuals : {metrics.total_visuals}\n"
                f"  Complexity: {metrics.complexity_index:.0%}"
            )
    else:
        print(content)

    return 0


def analyze_combined(args) -> int:
    """Analizza sia modello semantico che report, producendo un unico documento."""
    project_path = Path(args.model_path).resolve()
    if not project_path.exists():
        print(f"Error: path does not exist: {project_path}", file=sys.stderr)
        return 1

    # Discover .SemanticModel and .Report subfolders
    semantic_model_path = None
    report_path = None

    for child in project_path.iterdir():
        if child.is_dir():
            if ".SemanticModel" in child.name:
                semantic_model_path = child
            elif ".Report" in child.name:
                report_path = child

    if not semantic_model_path and not report_path:
        print(
            f"Error: No .SemanticModel or .Report folders found in: {project_path}\n"
            f"Expected a .pbip project structure",
            file=sys.stderr,
        )
        return 1

    # Parse both components first so we can use the real model/report name
    model = None
    report_metrics = None

    if semantic_model_path:
        try:
            tmdl_parser = TmdlParser()
            model = tmdl_parser.parse(semantic_model_path)
        except Exception as exc:
            print(f"Warning: Failed to parse semantic model: {exc}", file=sys.stderr)

    if report_path:
        try:
            rpt_parser = ReportParser()
            report = rpt_parser.parse(report_path)
            report_metrics = report.calculate_metrics()
        except Exception as exc:
            print(f"Warning: Failed to parse report: {exc}", file=sys.stderr)

    # Determine output path — use real model/report name, not the folder name
    if args.output:
        output_path = Path(args.output).resolve()
    else:
        project_name = (
            model.name if model
            else (report_metrics.report_name if report_metrics else project_path.name)
        )
        if args.format == "json":
            ext = ".json"
        elif args.format == "html":
            ext = ".html"
        elif args.format == "rag":
            ext = ".jsonl"
        else:
            ext = ".md"
        output_path = project_path / f"DOC_{_safe_name(project_name)}{ext}"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Generate unified output
    project_name = (
        model.name if model
        else (report_metrics.report_name if report_metrics else project_path.name)
    )

    if args.format == "json":
        combined: dict = {}
        if model:
            combined["semantic_model"] = {
                "name": model.name,
                "tables": len(model.visible_tables),
                "measures": sum(len(t.measures) for t in model.visible_tables),
                "relationships": len(model.relationships),
            }
        if report_metrics:
            combined["report"] = report_metrics.to_dict()
        content = json.dumps(combined, indent=2, ensure_ascii=False)
    elif args.format == "html":
        content = HtmlGenerator().generate_combined(model, report_metrics, project_name)
    elif args.format == "rag":
        embedder = _build_embedder(args)
        rag_gen = RagGenerator()
        chunks = rag_gen.generate(model=model, report_metrics=report_metrics)
        chunks = _embed_chunks(chunks, embedder, args.quiet)
        content = "\n".join(c.to_json() for c in chunks) + "\n"
    else:  # markdown — unified single document
        content = _combined_markdown(model, report_metrics, project_name)

    output_path.write_text(content, encoding="utf-8")

    if not args.quiet:
        print(f"✓ Combined documentation saved: {output_path}")
        if model:
            print(f"  Model : {model.name} ({len(model.visible_tables)} tables, "
                  f"{sum(len(t.measures) for t in model.visible_tables)} measures)")
        if report_metrics:
            print(f"  Report: {report_metrics.report_name} "
                  f"({report_metrics.total_pages} pages, {report_metrics.total_visuals} visuals)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
