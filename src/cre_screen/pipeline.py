"""run_deal: extract, map, load the pro forma, run the gate, return a DealPackage."""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from cre_screen.config import AppConfig
from cre_screen.errors import CreScreenError
from cre_screen.extract.proforma import load_proforma
from cre_screen.extract.rent_roll import build_rent_roll
from cre_screen.extract.t12 import build_t12
from cre_screen.ingest.excel_tables import extract_excel_table
from cre_screen.ingest.pdf_tables import extract_pdf_t12_table, extract_pdf_table
from cre_screen.ingest.raw import RawTable
from cre_screen.ingest.reread import make_rereader
from cre_screen.ingest.router import detect_format
from cre_screen.mapping.coa_mapper import load_learned
from cre_screen.mapping.cost import CostTracker
from cre_screen.mapping.header_mapper import map_headers
from cre_screen.mapping.llm_protocol import LLMClient
from cre_screen.reconcile.context import CheckContext
from cre_screen.reconcile.gate import run_gate
from cre_screen.schemas.deal import DealPackage
from cre_screen.schemas.findings import Override
from cre_screen.schemas.t12 import T12Statement

logger = logging.getLogger(__name__)

DEFAULT_OUT = Path("out")


def find_input(deal_dir: Path, stem: str, *, required: bool = True) -> Path | None:
    found = [p for p in (deal_dir / f"{stem}.pdf", deal_dir / f"{stem}.xlsx") if p.exists()]
    if len(found) == 1:
        return found[0]
    if not found and not required:
        return None
    raise CreScreenError(f"{deal_dir.as_posix()} needs exactly one {stem}.pdf or {stem}.xlsx")


def read_rr_table(path: Path, cfg: AppConfig, sheet: str | None, cache_dir: Path) -> RawTable:
    fmt = detect_format(path)
    if fmt == "pdf_scanned":
        raise CreScreenError(f"{path.name} is a scanned PDF; the vision path is not built yet")
    if fmt == "xlsx":
        return extract_excel_table(path, cfg.rr_headers, sheet=sheet)
    return extract_pdf_table(path, cfg.rr_headers, cache_dir=cache_dir)


def sample_rows(table: RawTable) -> list[dict[str, str]]:
    units = [r for r in table.rows if r.kind == "unit"][:3]
    return [{h: c.text for h, c in r.cells.items()} for r in units]


def _read_t12(
    path: Path, cfg: AppConfig, llm: LLMClient, cache_dir: Path, learned_path: Path
) -> T12Statement:
    fmt = detect_format(path)
    if fmt != "pdf_text":
        raise CreScreenError(f"{path.name}: only text-layer PDF T-12s are supported so far ({fmt})")
    table = extract_pdf_t12_table(path, cfg.coa, cache_dir=cache_dir)
    return build_t12(table, cfg, llm=llm, learned=load_learned(learned_path))


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def run_deal(
    deal_dir: Path,
    *,
    llm: LLMClient,
    cfg: AppConfig,
    as_of: date | None = None,
    overrides: list[Override] | None = None,
    tracker: CostTracker | None = None,
    out_dir: Path = DEFAULT_OUT,
    learned_path: Path | None = None,
    sheet: str | None = None,
) -> DealPackage:
    """Run one deal end to end. Writes ``<out_dir>/<deal_id>/package.json``, never inside data/."""
    started = time.perf_counter()
    cache_dir = out_dir / "cache" / "extract"
    learned = learned_path or Path("config") / "coa_learned.yaml"

    rr_path = find_input(deal_dir, "rent_roll")
    assert rr_path is not None
    table = read_rr_table(rr_path, cfg, sheet, cache_dir)
    header_map, header_warnings = map_headers(
        table.headers, sample_rows(table), cfg.rr_headers, llm
    )
    rent_roll = build_rent_roll(table, header_map, cfg, llm=llm)
    if header_warnings:
        rent_roll = rent_roll.model_copy(
            update={"extraction_warnings": [*header_warnings, *rent_roll.extraction_warnings]}
        )

    t12_path = find_input(deal_dir, "t12", required=False)
    t12 = _read_t12(t12_path, cfg, llm, cache_dir, learned) if t12_path else None
    pf_path = deal_dir / "proforma.yaml"
    proforma = load_proforma(pf_path) if pf_path.exists() else None

    resolved = as_of or rent_roll.as_of_date
    if resolved is None:
        raise CreScreenError("no as-of date found in the document; pass --as-of YYYY-MM-DD")
    rent_roll = rent_roll.model_copy(update={"as_of_date": resolved})

    ctx = CheckContext(
        rent_roll=rent_roll, t12=t12, proforma=proforma, thresholds=cfg.thresholds, as_of=resolved
    )
    result = run_gate(ctx, reread=make_rereader(rr_path, rent_roll), overrides=overrides or [])

    created = datetime.now(UTC)
    run_id = tracker.run_id if tracker else f"{created:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    package = DealPackage(
        deal_id=deal_dir.name,
        run_id=run_id,
        model_id=cfg.models.default_model,
        rent_roll=rent_roll,
        t12=t12,
        proforma=proforma,
        findings=result.findings,
        gate_log=result.gate_log,
        final_state=result.final_state,
        overrides=overrides or [],
        cost_usd=tracker.run_total() if tracker else DealPackage.model_fields["cost_usd"].default,
        wall_seconds=round(time.perf_counter() - started, 3),
        created_at=created,
    )
    _write_atomic(
        out_dir / deal_dir.name / "package.json", package.model_dump_json(indent=2) + "\n"
    )
    return package
