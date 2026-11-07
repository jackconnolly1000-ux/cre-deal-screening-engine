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
from cre_screen.ingest.reread import make_rereader
from cre_screen.mapping.cost import CostTracker
from cre_screen.mapping.llm_protocol import LLMClient
from cre_screen.read_inputs import read_rent_roll, read_t12, rent_roll_routing
from cre_screen.reconcile.context import CheckContext
from cre_screen.reconcile.gate import run_gate
from cre_screen.review import apply_data_overrides, status_overrides
from cre_screen.schemas.deal import DealPackage
from cre_screen.schemas.findings import Override

logger = logging.getLogger(__name__)

DEFAULT_OUT = Path("out")


def find_input(deal_dir: Path, stem: str, *, required: bool = True) -> Path | None:
    found = [p for p in (deal_dir / f"{stem}.pdf", deal_dir / f"{stem}.xlsx") if p.exists()]
    if len(found) == 1:
        return found[0]
    if not found and not required:
        return None
    raise CreScreenError(f"{deal_dir.as_posix()} needs exactly one {stem}.pdf or {stem}.xlsx")


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
    rr_routing = rent_roll_routing(rr_path, cfg)
    rent_roll, _read = read_rent_roll(
        rr_path, cfg, sheet=sheet, cache_dir=cache_dir, llm=llm, routing=rr_routing
    )

    t12_path = find_input(deal_dir, "t12", required=False)
    t12 = None
    if t12_path is not None:
        t12, _ = read_t12(
            t12_path,
            cfg,
            cache_dir=cache_dir,
            llm=llm,
            learned_path=learned,
            routing=rent_roll_routing(t12_path, cfg),
        )
    elif rr_routing is not None and rr_routing.has_t12:
        t12, _ = read_t12(
            rr_path, cfg, cache_dir=cache_dir, llm=llm, learned_path=learned, routing=rr_routing
        )
    pf_path = deal_dir / "proforma.yaml"
    proforma = load_proforma(pf_path) if pf_path.exists() else None

    resolved = as_of or rent_roll.as_of_date
    if resolved is None:
        raise CreScreenError("no as-of date found in the document; pass --as-of YYYY-MM-DD")
    rent_roll = rent_roll.model_copy(update={"as_of_date": resolved})
    rent_roll, t12 = apply_data_overrides(rent_roll, t12, overrides or [])

    ctx = CheckContext(
        rent_roll=rent_roll, t12=t12, proforma=proforma, thresholds=cfg.thresholds, as_of=resolved
    )
    result = run_gate(
        ctx,
        reread=make_rereader(rr_path, rent_roll, llm),
        overrides=status_overrides(overrides or []),
    )

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
