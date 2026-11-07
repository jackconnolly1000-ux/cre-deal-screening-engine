from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path

import openpyxl
import pdfplumber
import pytest
from PIL import Image

from cre_screen.config import load_config
from cre_screen.ingest.render import images_to_pdf, page_count, page_image
from cre_screen.ingest.reread import VisionRereader
from cre_screen.ingest.router import detect_format
from cre_screen.pipeline import run_deal
from cre_screen.schemas.deal import DealTruth
from synth.degrade import MAX_ANGLE, degrade_page, degrade_pdf
from synth.generate import main
from synth.params import load_params
from synth.plan import DEV_DEALS, PLAN
from synth.traps import footnote_lines
from synth.truth import build_deal
from tests.fake_vision import PerfectOcr, perfect_ocr_for
from tests.fakes import CannedCoaLLM

D = Decimal
CFG = load_config()
PARAMS = load_params()
SCANNED = [n for n in DEV_DEALS if PLAN[n].scanned]


@pytest.fixture(scope="module")
def fresh(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("layouts")
    assert main(["--split", "dev", "--deals", "1-14", "--seed", "7", "--out", str(out)]) == 0
    return out


@pytest.fixture(scope="module")
def llm(fresh: Path) -> PerfectOcr:
    return perfect_ocr_for({n: fresh / f"deal_{n:03d}" for n in SCANNED}, fallback=CannedCoaLLM())


@pytest.fixture(scope="module")
def packages(fresh: Path, llm: PerfectOcr, tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("pkgs")
    return {n: run_deal(fresh / f"deal_{n:03d}", llm=llm, cfg=CFG, out_dir=out) for n in DEV_DEALS}


def truth_of(fresh: Path, n: int) -> DealTruth:
    return DealTruth.model_validate_json((fresh / f"deal_{n:03d}" / "truth.json").read_bytes())


def first_with(layout_attr: str, layout: str) -> int:
    return next(n for n in DEV_DEALS if getattr(PLAN[n], layout_attr) == layout)


# ---------- every rent roll family renders and extracts exactly ----------


@pytest.mark.parametrize("n", DEV_DEALS)
def test_every_family_extracts_every_unit_with_zero_phantoms(n: int, fresh: Path, packages) -> None:
    truth, pkg = truth_of(fresh, n), packages[n]
    assert [u.unit_id for u in pkg.rent_roll.units] == [u.unit_id for u in truth.units]
    for got, want in zip(pkg.rent_roll.units, truth.units, strict=True):
        for field in ("unit_type", "beds", "baths", "sq_ft", "status", "in_place_rent",
                      "market_rent", "lease_start", "lease_end"):  # fmt: skip
            assert getattr(got, field) == getattr(want, field), (n, want.unit_id, field)
    assert pkg.rent_roll.stated_totals == {k: v for k, v in truth.rr_stated_totals.items()}


@pytest.mark.parametrize("n", DEV_DEALS)
def test_every_family_extracts_the_t12_exactly(n: int, fresh: Path, packages) -> None:
    truth, pkg = truth_of(fresh, n), packages[n]
    assert pkg.t12 is not None
    assert [ln.label for ln in pkg.t12.lines] == [ln.label for ln in truth.t12_lines]
    for got, want in zip(pkg.t12.lines, truth.t12_lines, strict=True):
        assert got.bucket == want.true_bucket, (n, got.label)
        assert got.monthly == want.monthly, (n, got.label)
        assert got.stated_total == want.stated_total, (n, got.label)
    assert pkg.t12.period_end == truth.as_of_date


@pytest.mark.parametrize("n", DEV_DEALS)
def test_every_planted_trap_is_caught_on_every_family(n: int, fresh: Path, packages) -> None:
    from evals.scoring import score_deal

    score = score_deal(packages[n], truth_of(fresh, n))
    assert score.caught == score.planted and score.misses == []
    assert score.false_positives == 0 and score.clean_control_findings == 0


def test_the_file_formats_on_disk_match_the_plan(fresh: Path) -> None:
    for n in DEV_DEALS:
        folder, plan = fresh / f"deal_{n:03d}", PLAN[n]
        assert (folder / "rent_roll.xlsx").exists() == (plan.rr_layout == "rr_excel")
        assert (folder / "rent_roll.pdf").exists() == (plan.rr_layout != "rr_excel")
        assert (folder / "t12.xlsx").exists() == (plan.t12_layout == "t12_excel")
        assert detect_format(next(folder.glob("rent_roll.*"))) == plan.rr_format


# ---------- family-specific behavior ----------


def test_grouped_layout_gives_a_subtotal_per_type_that_foots_and_a_grand_total(
    fresh: Path, packages
) -> None:
    n = first_with("rr_layout", "rr_grouped")
    rr = packages[n].rent_roll
    scopes = [s.scope for s in rr.subtotals if s.column == "in_place_rent"]
    assert scopes.count("grand_total") == 1 and scopes.count("unit_type") >= 3
    assert scopes[-1] == "grand_total"
    types = [u.unit_type for u in rr.units]
    assert types == sorted(types, key=lambda t: ["Studio", "1x1", "2x1", "2x2", "3x2"].index(t))
    for sub in rr.subtotals:
        if sub.scope == "unit_type":
            covered = [u for u in rr.units if u.unit_id in set(sub.covers_unit_ids)]
            assert len({u.unit_type for u in covered}) == 1


def test_wide_layout_maps_tenant_and_ignores_deposit_and_balance(packages) -> None:
    rr = packages[first_with("rr_layout", "rr_wide")].rent_roll
    mapped = {c.maps_to for c in rr.header_map.columns}
    assert "tenant_name" in mapped and "lease_start" in mapped
    ignored = [c.header_text for c in rr.header_map.columns if c.maps_to == "ignore"]
    assert len(ignored) == 2 and any("ep" in h for h in ignored)
    assert rr.header_map.source == "synonym_table"


def test_excel_rent_roll_cites_sheet_and_cell_with_excel_provenance(packages) -> None:
    rr = packages[first_with("rr_layout", "rr_excel")].rent_roll
    cite = rr.units[0].citations["in_place_rent"]
    assert (cite.sheet, cite.provenance, cite.page, cite.bbox) == ("Rent Roll", "excel", None, None)
    assert cite.cell and cite.cell[0].isalpha()
    assert rr.property_name and rr.as_of_date is not None
    assert rr.source_format == "xlsx"


def test_excel_inputs_hold_values_never_formulas_and_numbers_not_text(fresh: Path) -> None:
    for n in DEV_DEALS:
        for name in ("rent_roll.xlsx", "t12.xlsx"):
            path = fresh / f"deal_{n:03d}" / name
            if not path.exists():
                continue
            ws = openpyxl.load_workbook(path).active
            kinds = {c.data_type for row in ws.iter_rows() for c in row if c.value is not None}
            assert "f" not in kinds, (n, name)
            assert "n" in kinds
            cached = openpyxl.load_workbook(path, data_only=True).active
            assert any(isinstance(c.value, (int, float)) for row in cached.iter_rows() for c in row)


def test_xlsx_generation_is_byte_deterministic(fresh: Path, tmp_path: Path) -> None:
    assert main(["--split", "dev", "--deals", "8", "--seed", "7", "--out", str(tmp_path)]) == 0
    for name in ("rent_roll.xlsx", "t12.xlsx"):
        assert (tmp_path / "deal_008" / name).read_bytes() == (
            fresh / "deal_008" / name
        ).read_bytes()


def test_sections_layout_prints_deductions_positive_and_extraction_restores_the_sign(
    fresh: Path, packages
) -> None:
    n = first_with("t12_layout", "t12_sections")
    text = "\n".join(
        page.extract_text() or ""
        for page in pdfplumber.open(fresh / f"deal_{n:03d}" / "t12.pdf").pages
    )
    assert "Less:" in text and "Income" in text and "Expenses" in text
    truth = truth_of(fresh, n)
    vacancy = next(ln for ln in truth.t12_lines if ln.true_bucket == "VACANCY_LOSS")
    assert vacancy.stated_total < 0, "the truth keeps the chart-of-accounts sign"
    pkg_line = next(ln for ln in packages[n].t12.lines if ln.label == vacancy.label)
    assert pkg_line.monthly == vacancy.monthly and "sign_normalized" in pkg_line.audit_flags


@pytest.mark.parametrize("layout", ["t12_sections", "t12_excel"])
def test_subtotal_rows_become_stated_subtotals_that_foot(layout: str, packages) -> None:
    n = first_with("t12_layout", layout)
    t12 = packages[n].t12
    assert {s.kind for s in t12.stated_subtotals} == {"total_income", "total_expenses", "noi"}
    assert all(
        s.total is not None and all(m is not None for m in s.monthly) for s in t12.stated_subtotals
    )
    assert not [
        f
        for f in packages[n].findings
        if f.check_id == "T12_FOOTING" and "subtotal" in f.details.get("kind", "")
    ]
    assert packages[n].t12.lines and not any(
        ln.label in {s.label for s in t12.stated_subtotals} for ln in t12.lines
    )


def test_the_grid_layout_has_no_subtotals(packages) -> None:
    assert packages[first_with("t12_layout", "t12_grid")].t12.stated_subtotals == []


def test_t12_footing_subtotals_stay_consistent_when_a_line_is_tampered(
    fresh: Path, packages
) -> None:
    """On a sections/excel deal with TRAP_T12_FOOTING only the planted line is reported."""
    for n in DEV_DEALS:
        truth = truth_of(fresh, n)
        if "TRAP_T12_FOOTING" in truth.traps and PLAN[n].t12_layout != "t12_grid":
            findings = [f for f in packages[n].findings if f.check_id == "T12_FOOTING"]
            expected = [e for e in truth.expected if e.check_id == "T12_FOOTING"]
            assert [f.line_labels for f in findings] == [e.line_labels for e in expected], n


# ---------- scanned deals ----------


@pytest.mark.parametrize("n", SCANNED)
def test_scanned_deals_are_image_only_and_go_through_the_vision_path(
    n: int, fresh: Path, packages, llm: PerfectOcr
) -> None:
    pdf = fresh / f"deal_{n:03d}" / "rent_roll.pdf"
    assert detect_format(pdf) == "pdf_scanned"
    pkg = packages[n]
    assert pkg.rent_roll.source_format == "pdf_scanned"
    cites = [c for u in pkg.rent_roll.units for c in u.citations.values()]
    assert {c.provenance for c in cites} == {"llm_vision"}
    assert all(c.bbox is None and c.cell is None and c.page for c in cites)
    assert any(c["media_type"] == "image/jpeg" for c in llm.vision_calls)


def test_a_scanned_deal_costs_one_vision_call_per_page(fresh: Path, tmp_path: Path) -> None:
    n = SCANNED[0]
    fake = perfect_ocr_for({n: fresh / f"deal_{n:03d}"}, fallback=CannedCoaLLM())
    run_deal(fresh / f"deal_{n:03d}", llm=fake, cfg=CFG, out_dir=tmp_path)
    assert len(fake.vision_calls) == page_count(fresh / f"deal_{n:03d}" / "rent_roll.pdf")


def test_the_vision_reread_agrees_with_a_flawless_first_read(fresh: Path, packages, llm) -> None:
    n = SCANNED[0]
    rr = packages[n].rent_roll
    result = VisionRereader(fresh / f"deal_{n:03d}" / "rent_roll.pdf", rr, llm)(rr.units)
    assert result.agrees


# ---------- degradation ----------


def text_pdf(tmp_path: Path) -> Path:
    from synth.layouts import rr_classic

    build = build_deal(3, 7, PARAMS)
    path = tmp_path / "clean.pdf"
    rr_classic.render(
        path,
        units=build.truth.units,
        stated_totals=build.truth.rr_stated_totals,
        as_of=build.truth.as_of_date,
        style=build.style,
    )
    return path


def test_degrade_makes_an_image_only_pdf_with_the_same_page_count_and_size(tmp_path: Path) -> None:
    clean = text_pdf(tmp_path)
    scan = tmp_path / "scan.pdf"
    degrade_pdf(clean, scan, seed=1)
    assert detect_format(clean) == "pdf_text" and detect_format(scan) == "pdf_scanned"
    assert page_count(scan) == page_count(clean)
    data, media_type = page_image(scan, 1)
    assert media_type == "image/jpeg"
    width, height = Image.open(io.BytesIO(data)).size
    assert (width, height) == (round(792 / 72 * 150), round(612 / 72 * 150))


def test_degrade_is_deterministic_for_a_seed_and_differs_between_seeds(tmp_path: Path) -> None:
    clean = text_pdf(tmp_path)
    a, b, c = (tmp_path / f"{n}.pdf" for n in "abc")
    degrade_pdf(clean, a, seed=5)
    degrade_pdf(clean, b, seed=5)
    degrade_pdf(clean, c, seed=6)
    assert a.read_bytes() == b.read_bytes() != c.read_bytes()


def test_degrade_page_adds_noise_and_a_bounded_rotation() -> None:
    import random

    white = io.BytesIO()
    Image.new("L", (300, 200), 255).save(white, format="PNG")
    angles = []
    for seed in range(20):
        rng = random.Random(seed)
        out = Image.open(io.BytesIO(degrade_page(white.getvalue(), rng))).convert("L")
        assert out.size == (300, 200)
        assert out.getextrema()[0] < 255, "noise darkens some pixels"
        angles.append(random.Random(seed).uniform(-MAX_ANGLE, MAX_ANGLE))
    assert max(abs(a) for a in angles) <= MAX_ANGLE and len({round(a, 3) for a in angles}) > 10


def test_jpeg_quality_makes_the_scan_lossy(tmp_path: Path) -> None:
    clean = text_pdf(tmp_path)
    scan = tmp_path / "scan.pdf"
    degrade_pdf(clean, scan, seed=2)
    data, _ = page_image(scan, 1)
    assert data[:2] == b"\xff\xd8" and Image.open(io.BytesIO(data)).format == "JPEG"
    images_to_pdf([data], [(792.0, 612.0)], tmp_path / "again.pdf")
    assert page_image(tmp_path / "again.pdf", 1)[1] == "image/jpeg"


# ---------- held-out variants, built with a throwaway seed (never the real one) ----------


def heldout(n: int, seed: int = 77):
    return build_deal(n, seed, PARAMS, split="heldout")


def test_heldout_footing_omits_a_row_that_is_still_in_the_stated_total() -> None:
    build = heldout(15)
    printed = sum((u.in_place_rent or D(0) for u in build.truth.units), D(0))
    stated = build.truth.rr_stated_totals["in_place_rent"]
    gap = stated - printed
    assert gap >= D("500") and len(build.truth.units) >= 59
    ids = [u.unit_id for u in build.truth.units]
    assert len(ids) == len(set(ids)), "the omitted row is simply absent"
    assert [e.check_id for e in build.truth.expected if e.trap_id == "TRAP_RR_FOOTING"] == [
        "RR_FOOTING"
    ]


def test_heldout_duplicate_sits_on_the_same_page_but_not_next_to_the_original() -> None:
    truth = heldout(16).truth
    ids = [u.unit_id for u in truth.units]
    (dup,) = {i for i in ids if ids.count(i) > 1}
    first, second = [i for i, x in enumerate(ids) if x == dup]
    assert truth.units[first].page == truth.units[second].page
    assert second - first >= 2


def test_heldout_concession_is_a_footnote_naming_units_and_the_rows_show_nothing() -> None:
    build = heldout(17)
    assert all(u.concession_note is None for u in build.truth.units)
    (expected,) = [e for e in build.truth.expected if e.trap_id == "TRAP_RR_HIDDEN_CONCESSION"]
    text = " ".join(build.footnotes)
    assert text.startswith("Concession:") and expected.unit_ids
    assert all(unit_id in text for unit_id in expected.unit_ids)


def test_heldout_one_time_income_uses_an_ambiguous_label() -> None:
    truth = heldout(16).truth
    (line,) = [ln for ln in truth.t12_lines if ln.is_nonrecurring]
    assert line.label == "Other Income - Misc Adj" and line.true_bucket == "OTHER_INCOME"


def test_heldout_tax_drop_notes_that_an_appeal_is_pending() -> None:
    assert heldout(15).proforma["source_note"].endswith("Tax appeal pending.")
    assert "appeal" not in build_deal(4, 7, PARAMS).proforma["source_note"]


def test_dev_variants_are_unchanged_by_the_existence_of_held_out_ones(fresh: Path) -> None:
    dev = build_deal(15, 77, PARAMS)  # same deal and seed, dev variant
    assert dev.footnotes == [] and "appeal" not in dev.proforma["source_note"]
    assert not any(ln.label == "Other Income - Misc Adj" for ln in dev.truth.t12_lines)


def test_footnote_lines_wrap_and_never_split_a_unit_id() -> None:
    lines = footnote_lines("1 mo free on 12 mo lease", [f"{i}01" for i in range(1, 40)], width=60)
    assert len(lines) > 2 and all(len(line) <= 62 for line in lines)
    joined = " ".join(line.strip() for line in lines)
    assert all(f"{i}01" in joined for i in range(1, 40))
