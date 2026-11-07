"""Aggregate DealScores and render the JSON and Markdown reports."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from cre_screen.schemas.deal import DealScore

ZERO = Decimal("0")
RATE = Decimal("0.0001")


def rate(num: Decimal | int, den: Decimal | int) -> Decimal:
    """num / den as a 4-place Decimal; an empty denominator is a perfect 1."""
    if not den:
        return Decimal("1").quantize(RATE)
    return (Decimal(num) / Decimal(den)).quantize(RATE)


def _aggregate_group(scores: list[DealScore]) -> dict[str, Any]:
    fields_total = sum(s.numeric_fields_total for s in scores)
    fields_correct = sum(s.numeric_fields_correct for s in scores)
    units_in_doc = sum(s.units_in_doc for s in scores)
    units_found = sum(s.units_found for s in scores)
    coa_total = sum((s.coa_dollars_total for s in scores), ZERO)
    coa_correct = sum((s.coa_dollars_correct for s in scores), ZERO)
    planted = sum(s.planted for s in scores)
    caught = sum(s.caught for s in scores)
    return {
        "deals": len(scores),
        "numeric_fields_total": fields_total,
        "numeric_fields_correct": fields_correct,
        "numeric_accuracy": str(rate(fields_correct, fields_total)),
        "units_in_doc": units_in_doc,
        "units_found": units_found,
        "unit_recall": str(rate(units_found, units_in_doc)),
        "phantom_units": sum(s.phantom_units for s in scores),
        "coa_dollars_total": str(coa_total),
        "coa_dollars_correct": str(coa_correct),
        "coa_accuracy": str(rate(coa_correct, coa_total)),
        "planted": planted,
        "caught": caught,
        "catch_rate": str(rate(caught, planted)),
        "false_positives": sum(s.false_positives for s in scores),
        "clean_control_findings": sum(s.clean_control_findings for s in scores),
        "cost_usd": str(sum((s.cost_usd for s in scores), ZERO)),
        "wall_seconds": round(sum(s.wall_seconds for s in scores), 3),
    }


def dominant_provenance(score: DealScore) -> str:
    if not score.provenance_mix:
        return "none"
    return max(score.provenance_mix.items(), key=lambda kv: (kv[1], kv[0]))[0]


def aggregate(scores: list[DealScore]) -> dict[str, Any]:
    by_split: dict[str, list[DealScore]] = {}
    by_prov: dict[str, list[DealScore]] = {}
    for s in scores:
        by_split.setdefault(s.split, []).append(s)
        by_prov.setdefault(dominant_provenance(s), []).append(s)
    return {
        "overall": _aggregate_group(scores),
        "by_split": {k: _aggregate_group(v) for k, v in sorted(by_split.items())},
        # DealScore records fields scored per provenance but not correct counts per provenance,
        # so deals are grouped by the provenance that supplied most of their numeric fields.
        "by_provenance": {k: _aggregate_group(v) for k, v in sorted(by_prov.items())},
    }


def _table(rows: dict[str, dict[str, Any]], label: str) -> list[str]:
    cols = [
        ("deals", "Deals"),
        ("numeric_accuracy", "Numeric acc."),
        ("unit_recall", "Unit recall"),
        ("phantom_units", "Phantoms"),
        ("coa_accuracy", "COA $ acc."),
        ("caught", "Caught"),
        ("planted", "Planted"),
        ("false_positives", "False pos."),
        ("clean_control_findings", "Clean-control findings"),
        ("cost_usd", "Cost USD"),
    ]
    lines = [
        "| " + " | ".join([label, *(c[1] for c in cols)]) + " |",
        "| " + " | ".join(["---"] * (len(cols) + 1)) + " |",
    ]
    for key, row in rows.items():
        lines.append("| " + " | ".join([key, *(str(row[c[0]]) for c in cols)]) + " |")
    return lines


def render_markdown(scores: list[DealScore], meta: dict[str, str]) -> str:
    agg = aggregate(scores)
    out = [f"# Eval report: {meta.get('split', '')}", ""]
    out += [f"- {k}: {v}" for k, v in meta.items()]
    out += ["", "## Aggregate", ""]
    out += _table({"all": agg["overall"]}, "Scope")
    out += ["", "## By split", "", *_table(agg["by_split"], "Split")]
    out += ["", "## By provenance (deals grouped by dominant provenance)", ""]
    out += _table(agg["by_provenance"], "Provenance")
    out += ["", "## Per deal", ""]
    out += [
        "| Deal | Numeric | Units | Phantoms | COA $ | Caught | False pos. | Clean findings |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in scores:
        out.append(
            f"| {s.deal_id} | {s.numeric_fields_correct}/{s.numeric_fields_total} "
            f"| {s.units_found}/{s.units_in_doc} | {s.phantom_units} "
            f"| {s.coa_dollars_correct}/{s.coa_dollars_total} | {s.caught}/{s.planted} "
            f"| {s.false_positives} | {s.clean_control_findings} |"
        )
    out += ["", "## Misses", ""]
    misses = [f"- {s.deal_id}: {m}" for s in scores for m in s.misses]
    out += misses or ["None."]
    return "\n".join(out) + "\n"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def write_reports(
    scores: list[DealScore], out_dir: Path, stem: str, meta: dict[str, str]
) -> tuple[Path, Path]:
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    _write_atomic(
        json_path, json.dumps([s.model_dump(mode="json") for s in scores], indent=2) + "\n"
    )
    _write_atomic(md_path, render_markdown(scores, meta))
    return json_path, md_path
