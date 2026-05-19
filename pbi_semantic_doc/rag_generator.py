"""
RAG chunk generator for pbi-semantic-doc.

Produces JSONL output where each line is a semantically self-contained chunk
ready for embedding and retrieval. Each chunk pre-resolves DAX dependencies
so an AI never needs to parse raw DAX to understand entity relationships.

Usage:
    from pbi_semantic_doc.rag_generator import RagGenerator

    gen = RagGenerator()
    jsonl = gen.generate_jsonl(model=model, report_metrics=metrics)
    Path("rag_chunks.jsonl").write_text(jsonl, encoding="utf-8")
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .parser import SemanticModel, Measure, Table, Relationship
    from .report_models import ReportMetrics, ReportPage


@dataclass
class RagChunk:
    id: str
    type: str        # overview | table | measure | relationship | report_page
    text: str        # human-readable, embedding-ready prose
    metadata: dict
    embedding: Optional[list[float]] = None

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "type": self.type,
            "text": self.text,
            "metadata": self.metadata,
        }
        if self.embedding is not None:
            d["embedding"] = self.embedding
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class RagGenerator:
    """
    Converts a parsed SemanticModel (and optionally ReportMetrics) into
    a list of RagChunks, one per logical entity.

    Each chunk is self-contained: DAX dependencies and table relationships
    are pre-resolved so downstream retrieval requires no further parsing.
    """

    def generate(
        self,
        model: Optional["SemanticModel"] = None,
        report_metrics: Optional["ReportMetrics"] = None,
        report=None,  # raw Report object — provides page chunks
    ) -> list[RagChunk]:
        from .lineage import ModelLineage

        chunks: list[RagChunk] = []
        lineage_map: dict = {}

        if model:
            try:
                ml = ModelLineage(model)
                lineage_map = ml.resolve_all()
            except Exception:
                pass

            chunks.append(self._overview_chunk(model))

            for rel in model.relationships:
                chunks.append(self._relationship_chunk(rel))

            for table in model.visible_tables:
                chunks.append(self._table_chunk(table, model))
                for measure in table.measures:
                    lin = lineage_map.get((table.name, measure.name))
                    chunks.append(self._measure_chunk(measure, table.name, lin))

        # Page chunks come from the raw Report object (ReportMetrics is aggregate-only)
        report_name = (report_metrics.report_name if report_metrics else None) or (report.name if report else None)
        if report and report_name:
            for page in report.pages:
                chunks.append(self._page_chunk(page, report_name))

        return chunks

    def generate_jsonl(
        self,
        model: Optional["SemanticModel"] = None,
        report_metrics: Optional["ReportMetrics"] = None,
        report=None,
    ) -> str:
        chunks = self.generate(model, report_metrics, report)
        return "\n".join(c.to_json() for c in chunks) + "\n"

    # ── chunk builders ────────────────────────────────────────────────────────

    def _overview_chunk(self, model: "SemanticModel") -> RagChunk:
        tables = model.visible_tables
        n_measures = sum(len(t.measures) for t in tables)
        n_rels = len(model.relationships)
        table_names = ", ".join(t.name for t in tables)

        text_parts = [
            f"Semantic Model: {model.name}",
            f"Contains {len(tables)} tables: {table_names}",
            f"Total measures: {n_measures}",
            f"Relationships: {n_rels}",
        ]
        if model.roles:
            role_names = ", ".join(r.name for r in model.roles)
            text_parts.append(f"Row Level Security roles: {role_names}")

        return RagChunk(
            id=f"overview::{model.name}",
            type="overview",
            text="\n".join(text_parts),
            metadata={
                "model_name": model.name,
                "table_count": len(tables),
                "measure_count": n_measures,
                "relationship_count": n_rels,
            },
        )

    def _table_chunk(self, table: "Table", model: "SemanticModel") -> RagChunk:
        visible_cols = [c for c in table.columns if not c.is_hidden]
        col_list = ", ".join(
            f"{c.name} ({c.data_type})" for c in visible_cols[:10]
        )
        if len(visible_cols) > 10:
            col_list += f" [+{len(visible_cols) - 10} more]"

        related: list[str] = []
        for rel in model.relationships:
            if rel.from_table == table.name:
                related.append(
                    f"{rel.to_table} (via {rel.from_column} → {rel.to_column})"
                )
            elif rel.to_table == table.name:
                related.append(
                    f"{rel.from_table} (via {rel.to_column} ← {rel.from_column})"
                )

        text_parts = [f"Table: {table.name}"]
        if table.description:
            text_parts.append(f"Description: {table.description}")
        text_parts.append(f"Columns ({len(visible_cols)}): {col_list}")
        if table.measures:
            measure_names = ", ".join(
                m.name for m in sorted(table.measures, key=lambda m: m.name)
            )
            text_parts.append(f"Measures ({len(table.measures)}): {measure_names}")
        if related:
            text_parts.append(f"Related tables: {'; '.join(related)}")
        if table.effective_mode not in ("unknown", ""):
            text_parts.append(f"Storage mode: {table.effective_mode}")

        return RagChunk(
            id=f"table::{table.name}",
            type="table",
            text="\n".join(text_parts),
            metadata={
                "name": table.name,
                "description": table.description,
                "column_count": len(visible_cols),
                "measure_count": len(table.measures),
                "mode": table.effective_mode,
                "related_tables": [r.split(" (via ")[0] for r in related],
            },
        )

    def _measure_chunk(
        self,
        measure: "Measure",
        table_name: str,
        lineage=None,
    ) -> RagChunk:
        text_parts = [
            f"Measure: {measure.name}",
            f"Table: {table_name}",
        ]
        if measure.description:
            text_parts.append(f"Description: {measure.description}")
        elif measure.auto_description():
            text_parts.append(f"Auto-description: {measure.auto_description()}")
        if measure.format_string:
            text_parts.append(f"Format: {measure.format_string}")
        if measure.display_folder:
            text_parts.append(f"Folder: {measure.display_folder}")
        if measure.expression:
            text_parts.append(f"DAX formula:\n{measure.expression}")

        dep_measures: list[str] = []
        base_tables: list[str] = []
        compatible: list[str] = []
        flags: list[str] = []
        ref_cols: list[str] = []

        if lineage:
            if lineage.all_measure_deps:
                dep_measures = list(lineage.all_measure_deps)
                text_parts.append(f"Depends on measures: {', '.join(dep_measures)}")
            if lineage.all_base_tables:
                base_tables = sorted(lineage.all_base_tables)
                text_parts.append(f"Aggregates data from: {', '.join(base_tables)}")
            if lineage.referenced_columns:
                ref_cols = [
                    f"{t}[{c}]" for t, c in sorted(lineage.referenced_columns)
                ]
                text_parts.append(f"References columns: {', '.join(ref_cols)}")
            if lineage.compatible_tables:
                compatible = sorted(lineage.compatible_tables)
                text_parts.append(f"Compatible slicers: {', '.join(compatible)}")
            if lineage.filter_removed_tables:
                removed = sorted(lineage.filter_removed_tables)
                text_parts.append(
                    f"Filter removed (ALL/ALLEXCEPT): {', '.join(removed)}"
                )
            if lineage.uses_time_intelligence:
                flags.append("time_intelligence")
                text_parts.append("Uses time intelligence functions")
            if lineage.uses_inactive_relationship:
                flags.append("inactive_relationship")
                text_parts.append("Uses inactive relationship (USERELATIONSHIP)")
            if lineage.has_cycle:
                flags.append("circular_dependency")
                text_parts.append("Warning: circular dependency detected")

        return RagChunk(
            id=f"measure::{table_name}::{measure.name}",
            type="measure",
            text="\n".join(text_parts),
            metadata={
                "name": measure.name,
                "table": table_name,
                "description": measure.description,
                "format_string": measure.format_string,
                "display_folder": measure.display_folder,
                "is_hidden": measure.is_hidden,
                "depends_on_measures": dep_measures,
                "base_tables": base_tables,
                "compatible_slicers": compatible,
                "referenced_columns": ref_cols,
                "flags": flags,
            },
        )

    def _relationship_chunk(self, rel: "Relationship") -> RagChunk:
        active_str = "active" if rel.is_active else "inactive"
        text = (
            f"Relationship: {rel.from_table}[{rel.from_column}]"
            f" → {rel.to_table}[{rel.to_column}]\n"
            f"Cardinality: {rel.cardinality}\n"
            f"Cross-filter direction: {rel.cross_filter}\n"
            f"Status: {active_str}"
        )
        return RagChunk(
            id=(
                f"relationship::{rel.from_table}.{rel.from_column}"
                f"::{rel.to_table}.{rel.to_column}"
            ),
            type="relationship",
            text=text,
            metadata={
                "from_table": rel.from_table,
                "from_column": rel.from_column,
                "to_table": rel.to_table,
                "to_column": rel.to_column,
                "cardinality": rel.cardinality,
                "cross_filter": rel.cross_filter,
                "is_active": rel.is_active,
            },
        )

    def _page_chunk(self, page: "ReportPage", report_name: str) -> RagChunk:
        visual_types: dict[str, int] = {}
        for visual in page.visuals:
            vtype = visual.visual_type.value if hasattr(visual.visual_type, "value") else str(visual.visual_type)
            visual_types[vtype] = visual_types.get(vtype, 0) + 1

        type_summary = ", ".join(
            f"{count}× {vtype}" for vtype, count in sorted(visual_types.items())
        )

        text_parts = [
            f"Report page: {page.display_name}",
            f"Report: {report_name}",
            f"Visuals ({len(page.visuals)}): {type_summary}",
        ]
        if page.is_hidden:
            text_parts.append("Page is hidden")
        if page.has_drillthrough:
            text_parts.append("Page has drillthrough configured")

        return RagChunk(
            id=f"page::{report_name}::{page.display_name}",
            type="report_page",
            text="\n".join(text_parts),
            metadata={
                "report_name": report_name,
                "page_name": page.display_name,
                "visual_count": len(page.visuals),
                "visual_types": visual_types,
                "is_hidden": page.is_hidden,
                "has_drillthrough": page.has_drillthrough,
            },
        )
