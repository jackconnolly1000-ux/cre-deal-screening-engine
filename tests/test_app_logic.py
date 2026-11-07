from __future__ import annotations

import io
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image

from cre_screen.app import logic
from cre_screen.config import load_config
from cre_screen.export.uw_math import compute_uw
from cre_screen.pipeline import run_deal
from cre_screen.schemas.citation import Citation
from cre_screen.schemas.findings import Finding
from tests.builders import cite
from tests.fake_vision import perfect_ocr_for
from tests.fakes import CannedCoaLLM

D = Decimal
CFG = load_config()
DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"
NOW = datetime(2026, 11, 6, 9, 30, 15, tzinfo=UTC)


@pytest.fixture(scope="module")
def text_pkg(tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("logic_out")
    return run_deal(DATA / "deal_003", llm=CannedCoaLLM(), cfg=CFG, out_dir=out)


@pytest.fixture(scope="module")
def scanned_pkg(tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("logic_scan_out")
    llm = perfect_ocr_for({10: DATA / "deal_010"}, fallback=CannedCoaLLM())
    return run_deal(DATA / "deal_010", llm=llm, cfg=CFG, out_dir=out)


@pytest.fixture(scope="module")
def excel_pkg(tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("logic_xlsx_out")
    return run_deal(DATA / "deal_008", llm=CannedCoaLLM(), cfg=CFG, out_dir=out)


def finding(
    severity="warn", check="RR_ZERO_RENT_OCC", units=(), labels=(), citations=(), status="open"
):
    return Finding(
        check_id=check,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        message="a message",
        unit_ids=list(units),
        line_labels=list(labels),
        citations=list(citations),
        status=status,  # type: ignore[arg-type]
    )


# ---------- listing deals ----------


def make_tree(tmp_path: Path, numbers: range) -> tuple[Path, Path]:
    data, real = tmp_path / "synthetic", tmp_path / "real"
    for n in numbers:
        (data / f"deal_{n:03d}").mkdir(parents=True)
    (real / "maple_court").mkdir(parents=True)
    (real / "maple_court" / "rent_roll.pdf").write_bytes(b"x")
    (real / "empty_folder").mkdir()
    (real / "stray.txt").write_text("x", encoding="utf-8")
    return data, real


def test_dev_deals_and_real_deals_are_listed_and_held_out_ones_are_not(tmp_path: Path) -> None:
    data, real = make_tree(tmp_path, range(1, 21))
    deals = logic.list_deals(data, real, heldout=False)
    assert [d.name for d in deals] == [f"deal_{n:03d}" for n in range(1, 15)] + ["maple_court"]
    assert [d.kind for d in deals][-1] == "real" and deals[0].kind == "synthetic"
    assert deals[0].path == data / "deal_001"


def test_held_out_deals_appear_only_when_switched_on(tmp_path: Path, monkeypatch) -> None:
    data, real = make_tree(tmp_path, range(1, 21))
    assert len(logic.list_deals(data, real, heldout=True)) == 21
    monkeypatch.delenv("CRE_SHOW_HELDOUT", raising=False)
    assert len(logic.list_deals(data, real)) == 15
    monkeypatch.setenv("CRE_SHOW_HELDOUT", "1")
    assert len(logic.list_deals(data, real)) == 21
    monkeypatch.setenv("CRE_SHOW_HELDOUT", "yes")
    assert len(logic.list_deals(data, real)) == 15, "only the exact value 1 switches it on"


def test_missing_folders_give_an_empty_list(tmp_path: Path) -> None:
    assert logic.list_deals(tmp_path / "a", tmp_path / "b", heldout=False) == []


def test_the_default_listing_of_the_real_repo_has_the_14_dev_deals_only() -> None:
    names = [d.name for d in logic.list_deals(DATA, DATA.parent / "real", heldout=False)]
    assert names[:14] == [f"deal_{n:03d}" for n in range(1, 15)]


# ---------- hashing and uploads ----------


def test_folder_hash_changes_with_the_documents_and_ignores_the_truth(tmp_path: Path) -> None:
    (tmp_path / "rent_roll.pdf").write_bytes(b"one")
    (tmp_path / "proforma.yaml").write_text("a: 1", encoding="utf-8")
    first = logic.folder_hash(tmp_path)
    assert first == logic.folder_hash(tmp_path)
    (tmp_path / "truth.json").write_text("{}", encoding="utf-8")
    assert logic.folder_hash(tmp_path) == first
    (tmp_path / "rent_roll.pdf").write_bytes(b"two")
    assert logic.folder_hash(tmp_path) != first


def test_overrides_hash_depends_on_content_and_order(text_pkg) -> None:
    a = logic.finding_override(finding(units=["1"]), "clear", "first note here", NOW)
    b = logic.finding_override(finding(units=["2"]), "clear", "second note here", NOW)
    assert logic.overrides_hash([a, b]) == logic.overrides_hash([a, b])
    assert logic.overrides_hash([a, b]) != logic.overrides_hash([b, a])
    assert logic.overrides_hash([]) != logic.overrides_hash([a])


def test_uploads_are_saved_under_a_timestamped_folder_with_canonical_names(tmp_path: Path) -> None:
    folder = logic.save_upload(
        {
            "rent_roll": ("My Rent Roll (final).PDF", b"%PDF"),
            "t12": ("t12 export.xlsx", b"PK"),
            "proforma": ("broker.yml", b"a: 1"),
        },
        tmp_path / "uploads",
        NOW,
    )
    assert folder == tmp_path / "uploads" / "20261106T093015"
    assert sorted(p.name for p in folder.iterdir()) == [
        "proforma.yaml",
        "rent_roll.pdf",
        "t12.xlsx",
    ]
    assert (folder / "rent_roll.pdf").read_bytes() == b"%PDF"


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"rent_roll": ("notes.txt", b"x")}, "only .pdf and .xlsx"),
        ({"rent_roll": ("old.xls", b"x")}, "only .pdf and .xlsx"),
        ({"deed": ("deed.pdf", b"x")}, "unknown upload role"),
    ],
)
def test_bad_uploads_are_rejected(tmp_path: Path, files, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        logic.save_upload(files, tmp_path, NOW)


# ---------- findings ----------


def test_findings_are_sorted_block_warn_info_then_check_id(text_pkg) -> None:
    ordered = logic.sorted_findings(text_pkg)
    assert [(f.severity, f.check_id) for f in ordered] == [
        ("block", "RR_FOOTING"),
        ("warn", "RR_ZERO_RENT_OCC"),
        ("warn", "T12_SPIKE"),
        ("info", "RR_ZERO_RENT_OCC"),
    ]


def test_findings_rows_carry_the_columns_the_table_shows(text_pkg) -> None:
    rows = logic.findings_rows(logic.sorted_findings(text_pkg))
    assert list(rows[0]) == [
        "Check",
        "Severity",
        "Status",
        "Message",
        "Delta",
        "Units / lines",
        "First citation",
    ]
    assert rows[0]["Delta"] == "-36,000.00" and rows[0]["First citation"].startswith(
        "rent_roll.pdf p."
    )
    assert rows[1]["Units / lines"] == "204"


def test_long_finding_labels_are_shortened_with_their_severity_and_check() -> None:
    long = finding(severity="block", check="RR_FOOTING").model_copy(update={"message": "x" * 200})
    label = logic.finding_label(long)
    assert label.startswith("[block] RR_FOOTING: ") and label.endswith("...") and len(label) < 100


def test_only_open_findings_can_be_reviewed(text_pkg) -> None:
    findings = [finding(status="cleared"), finding(status="open"), finding(status="overridden")]
    pkg = text_pkg.model_copy(update={"findings": findings})
    assert [f.status for f in logic.reviewable(pkg)] == ["open"]
    assert logic.counts(pkg) == {"block": 0, "warn": 1, "info": 0}
    assert logic.counts(text_pkg) == {"block": 1, "warn": 2, "info": 1}


# ---------- focus ----------


def test_a_finding_focuses_its_first_citation(text_pkg) -> None:
    first = logic.sorted_findings(text_pkg)[0]
    focus = logic.focus_of(first)
    assert focus is not None and focus["source_file"] == "rent_roll.pdf"
    assert focus["page"] == first.citations[0].page and focus["bbox"] is not None
    assert focus["short"] == first.citations[0].short()
    assert logic.focus_of(finding()) is None


def test_the_default_focus_is_the_worst_findings_citation(text_pkg) -> None:
    assert logic.default_focus(text_pkg) == logic.focus_of(logic.sorted_findings(text_pkg)[0])
    assert logic.default_focus(text_pkg.model_copy(update={"findings": []})) is None


def test_citation_dict_keeps_a_manual_entry_without_a_page() -> None:
    manual = Citation(
        source_file="proforma.yaml", row_index=0, raw_text="x", provenance="manual_entry"
    )
    data = logic.citation_dict(manual)
    assert (
        data["page"] is None and data["bbox"] is None and data["short"].endswith("(manual entry)")
    )


# ---------- the audit view ----------


def test_every_citation_box_on_a_page_is_collected_once(text_pkg) -> None:
    boxes = logic.boxes_on_page(text_pkg, "rent_roll.pdf", 1)
    assert len(boxes) > 100 and len(boxes) == len(set(boxes))
    assert all(len(b) == 4 and b[0] < b[2] and b[1] < b[3] for b in boxes)
    assert logic.boxes_on_page(text_pkg, "rent_roll.pdf", 99) == []
    assert logic.boxes_on_page(text_pkg, "t12.pdf", 1) != []


def test_text_pages_are_drawn_with_a_highlighted_focus_box(text_pkg) -> None:
    focus = logic.default_focus(text_pkg)
    png, caption = logic.audit_image(DATA / "deal_003", text_pkg, focus)
    assert png is not None and caption == f"rent_roll.pdf page {focus['page']}"
    image = Image.open(io.BytesIO(png)).convert("RGB")
    scale = logic.RENDER_DPI / 72
    x0, top, _x1, bottom = focus["bbox"]
    left_edge = [
        image.getpixel((int(x0 * scale), y)) for y in range(int(top * scale), int(bottom * scale))
    ]
    assert any(r > 230 and 80 < g < 190 and b < 90 for r, g, b in left_edge), (
        "orange highlight edge"
    )
    plain = logic.render.render_page_png(
        DATA / "deal_003" / "rent_roll.pdf", focus["page"], logic.RENDER_DPI
    )
    assert png != plain


def test_scanned_pages_show_no_boxes_and_say_who_read_them(scanned_pkg) -> None:
    focus = logic.citation_dict(scanned_pkg.rent_roll.units[0].citations["unit_id"])
    png, caption = logic.audit_image(DATA / "deal_010", scanned_pkg, focus)
    assert caption == "values transcribed by vision model"
    plain = logic.render.render_page_png(
        DATA / "deal_010" / "rent_roll.pdf", focus["page"], logic.RENDER_DPI
    )
    assert png == plain, "nothing is drawn on a scan"
    assert logic.boxes_on_page(scanned_pkg, "rent_roll.pdf", 1) == []


def test_workbooks_and_manual_entries_have_no_page_image(excel_pkg, text_pkg) -> None:
    focus = logic.citation_dict(excel_pkg.rent_roll.units[0].citations["unit_id"])
    png, caption = logic.audit_image(DATA / "deal_008", excel_pkg, focus)
    assert png is None and "no page image" in caption and focus["sheet"] == "Rent Roll"
    manual = logic.citation_dict(text_pkg.proforma.citation)
    assert logic.audit_image(DATA / "deal_003", text_pkg, manual)[0] is None


def test_a_missing_source_file_is_handled(text_pkg, tmp_path: Path) -> None:
    focus = logic.default_focus(text_pkg)
    assert logic.audit_image(tmp_path, text_pkg, focus)[0] is None


def test_rows_on_a_page_are_the_extracted_rows_with_their_sources(text_pkg) -> None:
    rows = logic.rows_on_page(text_pkg, "rent_roll.pdf", 1)
    assert 0 < len(rows) <= 20 and rows[0]["Unit"] == "101"
    assert rows[0]["Source"].startswith("rent_roll.pdf p.1 row")
    assert logic.rows_on_page(text_pkg, "rent_roll.pdf", 2)[0]["Unit"] != "101"
    lines = logic.rows_on_page(text_pkg, "t12.pdf", 1)
    assert len(lines) == len(text_pkg.t12.lines) and lines[0]["Bucket"] == "GROSS_POTENTIAL_RENT"
    assert logic.rows_on_page(text_pkg, "other.pdf", 1) == []


def test_a_workbook_has_no_pages_so_every_row_is_listed(excel_pkg) -> None:
    rows = logic.rows_on_page(excel_pkg, "rent_roll.xlsx", None)
    assert len(rows) == len(excel_pkg.rent_roll.units)


# ---------- reviewing ----------


@pytest.mark.parametrize(
    ("note", "ok"),
    [("", False), ("123456789", False), ("1234567890", True), ("   short   ", False),
     ("  ten chars!  ", True), ("seller confirmed the total", True)],
)  # fmt: skip
def test_note_validation_uses_the_threshold_from_the_config(note: str, ok: bool) -> None:
    error = logic.validate_note(note, CFG.thresholds)
    assert (error is None) is ok
    if not ok:
        assert "at least 10 characters" in error


def test_the_minimum_note_length_follows_thresholds_yaml() -> None:
    longer = CFG.thresholds.model_copy(
        update={"review": CFG.thresholds.review.model_copy(update={"override_note_min_chars": 25})}
    )
    assert (
        logic.validate_note("seller confirmed it", longer)
        == "A reviewer note needs at least 25 characters."
    )


def test_an_override_targets_the_findings_first_unit_or_line_else_everything() -> None:
    assert (
        logic.finding_override(
            finding(units=["204", "9"]), "clear", " a long enough note ", NOW
        ).target
        == "204"
    )
    assert (
        logic.finding_override(
            finding(labels=["R&M"]), "override", "a long enough note", NOW
        ).target
        == "R&M"
    )
    whole = logic.finding_override(
        finding(check="RR_FOOTING"), "override", "a long enough note", NOW
    )
    assert whole.target == "*" and whole.check_id == "RR_FOOTING" and whole.at == NOW
    assert whole.note == "a long enough note", "the note is stored trimmed"


def test_remap_changes_lists_only_lines_that_moved(text_pkg) -> None:
    first, second = text_pkg.t12.lines[0], text_pkg.t12.lines[1]
    edited = {ln.label: ln.bucket for ln in text_pkg.t12.lines}
    assert logic.remap_changes(text_pkg, edited) == []
    edited[second.label] = "MARKETING"
    assert logic.remap_changes(text_pkg, edited) == [(second.label, second.bucket, "MARKETING")]
    assert first.label in edited
    assert logic.remap_changes(text_pkg.model_copy(update={"t12": None}), edited) == []


def test_remap_overrides_carry_the_note_and_the_target_encoding(text_pkg) -> None:
    label = text_pkg.t12.lines[1].label
    (o,) = logic.remap_overrides(
        [(label, "UTILITIES", "MARKETING")], "  vendor is a marketing firm ", NOW
    )
    assert (o.action, o.check_id, o.target) == ("remap_line", "T12_UNMAPPED", f"{label}=MARKETING")
    assert o.note == "vendor is a marketing firm"
    with pytest.raises(ValueError, match="unknown bucket"):
        logic.remap_overrides([(label, "UTILITIES", "NOPE")], "a long enough note", NOW)


def test_t12_rows_show_mapping_provenance(text_pkg) -> None:
    rows = logic.t12_rows(text_pkg)
    assert rows[0] == {
        "Label": text_pkg.t12.lines[0].label,
        "Bucket": "GROSS_POTENTIAL_RENT",
        "Mapped by": "synonym_table",
        "Confidence": 1.0,
        "Annual total": f"{text_pkg.t12.lines[0].computed_total():,.2f}",
    }
    assert logic.t12_rows(text_pkg.model_copy(update={"t12": None})) == []


def test_summary_metrics_come_from_the_workbooks_python_mirror(text_pkg) -> None:
    metrics = logic.summary_metrics(text_pkg)
    uw = compute_uw(text_pkg)
    assert metrics["noi_underwritten"] == uw["E24"] and metrics["noi_t12"] == uw["B24"]
    assert (
        metrics["physical_occupancy"] == uw["E35"] and metrics["monthly_loss_to_lease"] == uw["E37"]
    )
    assert D("0") < metrics["physical_occupancy"] <= D("1") and metrics["dscr_underwritten"] > 0
    assert cite().page == 1


# ---------- resolving a footing failure ----------


def footing_finding(pkg):
    return next(f for f in pkg.findings if f.check_id == "RR_FOOTING")


def test_footing_facts_expose_the_two_numbers_to_choose_between(text_pkg) -> None:
    facts = logic.footing_facts(footing_finding(text_pkg))
    assert facts is not None and facts.column == "in_place_rent" and facts.grand_total
    assert (facts.stated, facts.calculated, facts.difference) == (
        D("73703.00"),
        D("37703.00"),
        D("-36000.00"),
    )
    assert facts.strategies() == ["use_calculated_sum", "use_stated_total", "edit_line_item"]


def test_other_findings_have_no_footing_facts(text_pkg) -> None:
    other = next(f for f in text_pkg.findings if f.check_id != "RR_FOOTING")
    assert logic.footing_facts(other) is None


def test_a_subtotal_failure_cannot_be_resolved_with_the_calculated_sum() -> None:
    sub = Finding(
        check_id="RR_FOOTING",
        severity="block",
        message="x",
        details={
            "column": "in_place_rent",
            "scope": "floor",
            "stated": "10.00",
            "computed": "9.00",
        },
    )
    facts = logic.footing_facts(sub)
    assert facts is not None and not facts.grand_total
    assert facts.strategies() == ["use_stated_total", "edit_line_item"]


def test_use_calculated_sum_builds_a_resolve_footing_override_with_the_sum(text_pkg) -> None:
    o = logic.footing_overrides(
        text_pkg,
        footing_finding(text_pkg),
        "use_calculated_sum",
        "digits swapped in print",
        now=NOW,
    )
    assert (o.action, o.target, o.corrected_value) == (
        "resolve_footing",
        "in_place_rent",
        D("37703.00"),
    )
    assert o.resolution_strategy == "use_calculated_sum" and o.check_id == "RR_FOOTING"


def test_use_stated_total_requires_the_amount_that_makes_the_rows_foot(text_pkg) -> None:
    finding = footing_finding(text_pkg)
    with pytest.raises(ValueError, match=r"still miss the stated total by -1\.00"):
        logic.footing_overrides(
            text_pkg, finding, "use_stated_total", "keying it in", unit_id="104", amount="35999.00"
        )
    o = logic.footing_overrides(
        text_pkg, finding, "use_stated_total", "keying it in", unit_id="104", amount="36000.00"
    )
    assert (o.action, o.target, o.corrected_value) == (
        "edit_value",
        "104:in_place_rent",
        D("36000.00"),
    )
    assert o.resolution_strategy == "use_stated_total"


def test_edit_line_item_accepts_any_amount_and_needs_a_unit(text_pkg) -> None:
    finding = footing_finding(text_pkg)
    o = logic.footing_overrides(
        text_pkg, finding, "edit_line_item", "rent misread", unit_id="104", amount="$1,500.50"
    )
    assert o.corrected_value == D("1500.50") and o.resolution_strategy == "edit_line_item"
    with pytest.raises(ValueError, match="Choose the unit"):
        logic.footing_overrides(text_pkg, finding, "edit_line_item", "rent misread", amount="1")


def test_a_footing_choice_must_be_one_of_the_offered_strategies(text_pkg) -> None:
    with pytest.raises(ValueError, match="which number governs"):
        logic.footing_overrides(text_pkg, footing_finding(text_pkg), "just_ignore", "a long note")
    with pytest.raises(ValueError, match="not a rent-roll footing"):
        logic.footing_overrides(text_pkg, finding(), "use_calculated_sum", "a long note")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1250", D("1250.00")), ("$1,250.5", D("1250.50")), ("(5.00)", D("-5.00"))],
)
def test_parse_amount_is_exact_decimal(raw: str, expected: Decimal) -> None:
    assert logic.parse_amount(raw) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [("abc", "not an amount"), ("", "Enter the corrected"), ("12.345", "two decimal places")],
)
def test_parse_amount_rejects_bad_input(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        logic.parse_amount(raw)


def test_a_chosen_resolution_makes_the_reviewed_package_foot(text_pkg) -> None:
    from cre_screen.review import apply_review

    o = logic.footing_overrides(
        text_pkg,
        footing_finding(text_pkg),
        "use_stated_total",
        "keying it in",
        unit_id="104",
        amount="36000.00",
    )
    reviewed = apply_review(text_pkg, [o], cfg=CFG, deal_dir=DATA / "deal_003")
    unit = next(u for u in reviewed.rent_roll.units if u.unit_id == "104")
    assert unit.in_place_rent == D("36000.00")
    assert not [f for f in reviewed.findings if f.check_id == "RR_FOOTING"]
