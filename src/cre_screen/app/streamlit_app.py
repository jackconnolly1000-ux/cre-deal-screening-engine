"""Review UI (SPEC section 10). Run: ``uv run streamlit run src/cre_screen/app/streamlit_app.py``.

The pipeline runs only when the Run button is pressed (and its result is cached by deal-folder
hash). Everything a reviewer does afterwards, such as clearing a finding or moving a T-12 line to a
different account, is applied to that saved package and re-runs only the pure gate, so no click
other than Run can ever reach the LLM.
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from cre_screen import runtime
from cre_screen.app import logic
from cre_screen.config import AppConfig, load_config
from cre_screen.errors import CreScreenError
from cre_screen.export.excel_writer import workbook_bytes
from cre_screen.ingest.parse import parse_date
from cre_screen.mapping.coa_mapper import append_learned
from cre_screen.mapping.cost import CostTracker
from cre_screen.pipeline import run_deal
from cre_screen.review import BUCKETS, apply_review
from cre_screen.schemas.deal import DealPackage
from cre_screen.schemas.findings import Override

DATA_DIR = runtime.REPO_ROOT / "data" / "synthetic"
REAL_DIR = runtime.REPO_ROOT / "data" / "real"
UPLOAD_DIR = runtime.REPO_ROOT / "out" / "uploads"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@st.cache_data(show_spinner="Running the pipeline...")
def run_pipeline(deal_dir: str, folder_hash: str, as_of: str, cache_mode: str) -> tuple[str, str]:
    """Extract, map, check. ``folder_hash`` is part of the cache key: edited files re-run."""
    cfg = runtime.with_overrides(load_config(), cache_mode=cache_mode)
    llm, tracker, _cache = runtime.make_llm(cfg)
    pkg = run_deal(
        Path(deal_dir),
        llm=llm,
        cfg=cfg,
        as_of=parse_date(as_of) if as_of else None,
        tracker=tracker,
        out_dir=runtime.REPO_ROOT / "out",
        learned_path=runtime.LEARNED,
    )
    return pkg.model_dump_json(), str(pkg.cost_usd)


@st.cache_data(show_spinner=False)
def review_package(
    deal_dir: str, folder_hash: str, as_of: str, cache_mode: str, overrides_json: str
) -> str:
    """The extracted package with the reviewer's overrides applied. Never calls the LLM."""
    base_json, _cost = run_pipeline(deal_dir, folder_hash, as_of, cache_mode)
    overrides = [Override.model_validate(o) for o in json.loads(overrides_json)]
    pkg = apply_review(
        DealPackage.model_validate_json(base_json),
        overrides,
        cfg=load_config(),
        deal_dir=Path(deal_dir),
    )
    return pkg.model_dump_json()


def _overrides() -> list[Override]:
    return [Override.model_validate(o) for o in st.session_state["overrides"]]


def _add_overrides(new: list[Override]) -> None:
    st.session_state["overrides"] = [
        *st.session_state["overrides"],
        *(json.loads(o.model_dump_json()) for o in new),
    ]


def sidebar(cfg: AppConfig) -> tuple[Path | None, str, str, bool]:
    """Draw the sidebar; return (deal folder, as-of text, cache mode, run pressed)."""
    deals = logic.list_deals(DATA_DIR, REAL_DIR)
    refs = {d.name: d.path for d in deals}
    if (uploaded := st.session_state.get("uploaded")) is not None:
        refs[uploaded["name"]] = Path(uploaded["path"])
    if "pending_deal" in st.session_state:  # set by the upload button, applied before the widget
        st.session_state["deal"] = st.session_state.pop("pending_deal")

    with st.sidebar:
        st.header("Deal")
        if not refs:
            st.warning("No deals found under data/synthetic or data/real. Upload one below.")
        choice = st.selectbox("Deal", list(refs), key="deal") if refs else None
        with st.expander("Upload a deal"):
            rent_roll = st.file_uploader("Rent roll (.pdf or .xlsx)", ["pdf", "xlsx"], key="up_rr")
            t12 = st.file_uploader("T-12 (.pdf or .xlsx)", ["pdf", "xlsx"], key="up_t12")
            proforma = st.file_uploader("proforma.yaml", ["yaml", "yml"], key="up_pf")
            if st.button("Use uploaded files", key="use_upload"):
                files = {
                    role: (f.name, f.getvalue())
                    for role, f in (("rent_roll", rent_roll), ("t12", t12), ("proforma", proforma))
                    if f is not None
                }
                if "rent_roll" not in files:
                    st.error("A rent roll is required.")
                else:
                    folder = logic.save_upload(files, UPLOAD_DIR)
                    name = f"upload_{folder.name}"
                    st.session_state["uploaded"] = {"name": name, "path": str(folder)}
                    st.session_state["pending_deal"] = name
                    st.rerun()
        as_of = st.text_input("As-of date (YYYY-MM-DD, optional)", key="as_of").strip()
        run = st.button("Run", type="primary", key="run", disabled=choice is None)
        st.text_input(
            "LLM cache mode (set in config or CRE_LLM_CACHE_MODE)",
            value=cfg.models.cache.mode,
            disabled=True,
            key="cache_mode_view",
        )
    return (refs[choice] if choice else None), as_of, cfg.models.cache.mode, run


def header(pkg: DealPackage, cfg: AppConfig, run_cost: str) -> None:
    tracker = CostTracker(cfg.models, runtime.LEDGER, run_id="ui")
    n = logic.counts(pkg)
    cols = st.columns(5)
    cols[0].metric("Gate state", pkg.final_state)
    cols[1].metric("Open block findings", n["block"])
    cols[2].metric("Open warn findings", n["warn"])
    cols[3].metric("Cost of this run", f"${run_cost}")
    cols[4].metric("Lifetime LLM spend", f"${tracker.lifetime_total():.4f}")


def findings_tab(pkg: DealPackage, cfg: AppConfig) -> None:
    ordered = logic.sorted_findings(pkg)
    if not ordered:
        st.success("No findings.")
    else:
        event = st.dataframe(
            logic.findings_rows(ordered),
            on_select="rerun",
            selection_mode="single-row",
            hide_index=True,
            width="stretch",
            key="findings_table",
        )
        selected = list(event.selection.rows) if event is not None else []
        if selected:
            st.session_state["focus"] = logic.focus_of(ordered[selected[0]])

    st.subheader("Review")
    if st.session_state.get("flash"):
        st.success(st.session_state.pop("flash"))
    open_findings = logic.reviewable(pkg)
    if not open_findings:
        st.info("Nothing is open.")
    else:
        index = st.selectbox(
            "Finding to review",
            list(range(len(open_findings))),
            format_func=lambda i: logic.finding_label(open_findings[i]),
            key="review_finding",
        )
        action = st.radio("Action", ["override", "clear"], horizontal=True, key="review_action")
        minimum = cfg.thresholds.review.override_note_min_chars
        note = st.text_input(f"Reviewer note (at least {minimum} characters)", key="review_note")
        if st.button("Apply", key="review_apply"):
            problem = logic.validate_note(note, cfg.thresholds)
            if problem:
                st.error(problem)
            else:
                chosen = open_findings[index]
                _add_overrides([logic.finding_override(chosen, action, note)])  # type: ignore[arg-type]
                st.session_state["flash"] = f"{action.capitalize()}d {chosen.check_id}."
                st.rerun()
    if st.session_state["overrides"] and st.button("Undo last override", key="review_undo"):
        st.session_state["overrides"] = st.session_state["overrides"][:-1]
        st.rerun()
    if st.session_state["overrides"]:
        st.caption("Overrides so far")
        st.dataframe(
            [
                {"Action": o.action, "Check": o.check_id, "Target": o.target, "Note": o.note}
                for o in _overrides()
            ],
            hide_index=True,
            width="stretch",
        )


def audit_tab(pkg: DealPackage, deal_dir: Path) -> None:
    focus = st.session_state.get("focus") or logic.default_focus(pkg)
    if focus is None:
        st.info("No citation to show. Select a finding in the Findings tab.")
        return
    left, right = st.columns(2)
    png, caption = logic.audit_image(deal_dir, pkg, focus)
    with left:
        st.caption(
            f"Focus: {focus['short']}   raw text: {focus['raw_text']!r}   via {focus['provenance']}"
        )
        if png is not None:
            st.image(png, caption=caption)
        else:
            st.info(caption)
    with right:
        st.caption("Extracted rows from this page")
        st.dataframe(
            logic.rows_on_page(pkg, focus["source_file"], focus.get("page")),
            hide_index=True,
            width="stretch",
        )


def mapping_tab(pkg: DealPackage, cfg: AppConfig) -> None:
    rows = logic.t12_rows(pkg)
    if not rows:
        st.info("This deal has no T-12.")
        return
    edited = st.data_editor(
        rows,
        column_config={
            "Bucket": st.column_config.SelectboxColumn(
                "Bucket", options=list(BUCKETS), required=True
            )
        },
        disabled=["Label", "Mapped by", "Confidence", "Annual total"],
        hide_index=True,
        num_rows="fixed",
        width="stretch",
        key=f"t12_editor_{len(st.session_state['overrides'])}",
    )
    changes = logic.remap_changes(pkg, {r["Label"]: r["Bucket"] for r in edited})
    if not changes:
        return
    st.write("Pending remaps")
    st.dataframe(
        [{"Line": a, "From": b, "To": c} for a, b, c in changes], hide_index=True, width="stretch"
    )
    minimum = cfg.thresholds.review.override_note_min_chars
    note = st.text_input(f"Note for these remaps (at least {minimum} characters)", key="remap_note")
    if st.button("Apply remaps", key="remap_apply"):
        problem = logic.validate_note(note, cfg.thresholds)
        if problem:
            st.error(problem)
        else:
            _add_overrides(logic.remap_overrides(changes, note))
            for label, _old, new in changes:
                append_learned(runtime.LEARNED, new, label)
            st.session_state["flash"] = f"Remapped {len(changes)} line(s); learned for next time."
            st.rerun()


def summary_tab(pkg: DealPackage) -> None:
    m = logic.summary_metrics(pkg)
    a, b, c = st.columns(3)
    a.metric("Physical occupancy", f"{m['physical_occupancy']:.1%}")
    b.metric("Economic occupancy (T-12)", f"{m['economic_occupancy']:.1%}")
    c.metric("Monthly loss to lease", f"${m['monthly_loss_to_lease']:,.2f}")
    st.subheader("NOI: T-12 to underwritten")
    cols = st.columns(4)
    cols[0].metric("T-12 actual", f"${m['noi_t12']:,.0f}")
    cols[1].metric("T-3 annualized", f"${m['noi_t3']:,.0f}")
    cols[2].metric("Broker pro forma", f"${m['noi_broker']:,.0f}")
    cols[3].metric("Underwritten", f"${m['noi_underwritten']:,.0f}")
    st.caption(
        f"Underwritten DSCR {m['dscr_underwritten']:.2f}x at a purchase price of "
        f"${m['purchase_price']:,.0f}. Values come from the same math as the workbook."
    )


def export_row(pkg: DealPackage, deal_dir: Path) -> None:
    blocks = pkg.open_blocks()
    synthetic = (deal_dir / "truth.json").exists()
    data = b"" if blocks else workbook_bytes(pkg, synthetic=synthetic)
    left, right = st.columns([1, 3])
    left.download_button(
        "Download workbook",
        data,
        file_name=f"{pkg.deal_id}.xlsx",
        mime=XLSX_MIME,
        disabled=bool(blocks),
        key="export",
    )
    right.write(
        f"{len(blocks)} open block finding(s): clear or override them to enable the export."
        if blocks
        else "No open block findings: the workbook is ready."
    )


def main() -> None:
    st.set_page_config(page_title="cre-screen", layout="wide")
    st.title("cre-screen")
    st.session_state.setdefault("overrides", [])
    st.session_state.setdefault("focus", None)
    cfg = load_config()

    deal_dir, as_of, cache_mode, run = sidebar(cfg)
    if deal_dir is None:
        st.info("Pick or upload a deal, then press Run.")
        return

    if run:
        try:
            if as_of:
                parse_date(as_of)
            _json, run_cost = run_pipeline(
                str(deal_dir), logic.folder_hash(deal_dir), as_of, cache_mode
            )
        except CreScreenError as exc:
            st.error(str(exc))
            st.session_state["base_key"] = None
            return
        st.session_state.update(
            base_key=(str(deal_dir), logic.folder_hash(deal_dir), as_of, cache_mode),
            run_cost=run_cost,
            overrides=[],
            focus=None,
        )

    key = st.session_state.get("base_key")
    if key is None or key[0] != str(deal_dir):
        st.info(f"Press Run to screen {deal_dir.name}.")
        return

    overrides_json = json.dumps(st.session_state["overrides"], sort_keys=True)
    try:
        pkg = DealPackage.model_validate_json(review_package(*key, overrides_json))
    except CreScreenError as exc:
        st.error(str(exc))
        return

    header(pkg, cfg, st.session_state.get("run_cost", "0"))
    export_row(pkg, deal_dir)
    findings, audit, mapping, summary = st.tabs(
        ["Findings", "Audit view", "T-12 mapping", "Summary"]
    )
    with findings:
        findings_tab(pkg, cfg)
    with audit:
        audit_tab(pkg, deal_dir)
    with mapping:
        mapping_tab(pkg, cfg)
    with summary:
        summary_tab(pkg)


main()
