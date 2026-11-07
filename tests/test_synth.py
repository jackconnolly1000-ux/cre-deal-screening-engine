from __future__ import annotations

import json
import math
import re
import statistics
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import get_args

import pdfplumber
import pytest
import yaml

from cre_screen.schemas.citation import Citation
from cre_screen.schemas.common import CheckId
from cre_screen.schemas.deal import DealTruth
from cre_screen.schemas.proforma import BrokerProForma
from synth.generate import main
from synth.layouts import RR_COLUMNS, rr_grouped
from synth.layouts.rr_classic import ROWS_PER_PAGE
from synth.params import load_params
from synth.plan import DEV_DEALS, HELDOUT_DEALS, PLAN
from synth.traps import DEAL_PLAN, DEV_TRAP_IDS, REGISTRY
from synth.truth import DealBuild, build_deal

D = Decimal
SEED = 7
OCC = ("occupied", "notice")
VACANT_LIKE = ("vacant", "down")
SPEC_DEV_TRAPS = {
    "TRAP_RR_FOOTING",
    "TRAP_RR_ZERO_RENT",
    "TRAP_RR_EXPIRED_LEASE",
    "TRAP_RR_DUP_UNIT",
    "TRAP_RR_HIDDEN_CONCESSION",
    "TRAP_X_RENT_PUSH",
    "TRAP_T12_FOOTING",
    "TRAP_T12_ONE_TIME_INCOME",
    "TRAP_T12_SPIKE",
    "TRAP_PF_TAX_DROP",
    "TRAP_PF_INS_DROP",
    "TRAP_PF_VACANCY",
    "TRAP_PF_MGMT_FEE",
}
ALL_DEALS = list(DEV_DEALS)  # the held-out deals are never built here


@pytest.fixture(scope="module")
def params():
    return load_params()


@pytest.fixture(scope="module")
def builds(params) -> dict[int, DealBuild]:
    return {n: build_deal(n, SEED, params) for n in ALL_DEALS}


@pytest.fixture(scope="module")
def gen_dirs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    dirs = []
    for name in ("a", "b"):
        out = tmp_path_factory.mktemp(name)
        assert main(["--split", "dev", "--deals", "1-5", "--seed", "7", "--out", str(out)]) == 0
        dirs.append(out)
    return dirs[0], dirs[1]


def pdf_text(path: Path) -> list[str]:
    with pdfplumber.open(path) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def load_truth(path: Path) -> DealTruth:
    return DealTruth.model_validate_json(path.read_bytes())


# ---------- generator contract ----------


def test_generating_twice_gives_identical_files_and_pdf_text(gen_dirs: tuple[Path, Path]) -> None:
    a, b = gen_dirs
    for n in range(1, 6):
        name = f"deal_{n:03d}"
        for fname in ("truth.json", "meta.json", "proforma.yaml"):
            assert (a / name / fname).read_bytes() == (b / name / fname).read_bytes()
        for fname in ("rent_roll.pdf", "t12.pdf"):
            assert pdf_text(a / name / fname) == pdf_text(b / name / fname)


def test_each_deal_folder_has_exactly_the_expected_files(gen_dirs: tuple[Path, Path]) -> None:
    for n in range(1, 6):
        files = sorted(p.name for p in (gen_dirs[0] / f"deal_{n:03d}").iterdir())
        assert files == ["meta.json", "proforma.yaml", "rent_roll.pdf", "t12.pdf", "truth.json"]


def test_truth_json_round_trips_and_matches_meta(gen_dirs: tuple[Path, Path]) -> None:
    for n in range(1, 6):
        folder = gen_dirs[0] / f"deal_{n:03d}"
        truth = load_truth(folder / "truth.json")
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        assert (
            truth.model_dump_json()
            == DealTruth.model_validate_json(truth.model_dump_json()).model_dump_json()
        )
        assert meta["traps"] == truth.traps
        assert meta["scanned"] is False
        assert truth.rr_format == "pdf_text"
        assert truth.split == "dev"
        assert truth.seed == SEED


def test_proforma_yaml_loads_into_the_schema(gen_dirs: tuple[Path, Path]) -> None:
    for n in range(1, 6):
        raw = yaml.safe_load((gen_dirs[0] / f"deal_{n:03d}" / "proforma.yaml").read_text("utf-8"))
        assert all(isinstance(v, str) for v in raw["lines"].values())
        raw["asking_price"] = D(raw["asking_price"])
        raw["vacancy_rate"] = D(raw["vacancy_rate"])
        raw["mgmt_fee_pct"] = D(raw["mgmt_fee_pct"])
        raw["lines"] = {k: D(v) for k, v in raw["lines"].items()}
        raw["citation"] = Citation(
            source_file="proforma.yaml", row_index=0, raw_text="", provenance="manual_entry"
        )
        BrokerProForma(**raw)


def test_pdf_page_count_and_first_row_per_page(
    gen_dirs: tuple[Path, Path],
) -> None:
    for n in range(1, 6):
        folder = gen_dirs[0] / f"deal_{n:03d}"
        truth = load_truth(folder / "truth.json")
        pages = pdf_text(folder / "rent_roll.pdf")
        assert len(pages) == math.ceil(len(truth.units) / ROWS_PER_PAGE)
        for page_no, text in enumerate(pages, start=1):
            first = next(u for u in truth.units if u.page == page_no)
            assert any(line.startswith(f"{first.unit_id} ") for line in text.splitlines())


def test_every_t12_label_is_printed(gen_dirs: tuple[Path, Path]) -> None:
    for n in range(1, 6):
        folder = gen_dirs[0] / f"deal_{n:03d}"
        text = "\n".join(pdf_text(folder / "t12.pdf"))
        for line in load_truth(folder / "truth.json").t12_lines:
            assert line.label in text


def test_heldout_generation_writes_six_deals_and_their_manifest(tmp_path: Path) -> None:
    code = main(["--split", "heldout", "--deals", "15-20", "--seed", "77", "--out", str(tmp_path)])
    assert code == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "HELDOUT_MANIFEST.sha256",
        *[f"deal_{n:03d}" for n in range(15, 21)],
    ]


def test_existing_deal_folder_needs_overwrite_flag(tmp_path: Path) -> None:
    args = ["--split", "dev", "--deals", "1", "--seed", "7", "--out", str(tmp_path)]
    assert main(args) == 0
    assert main(args) == 2
    assert main([*args, "--overwrite-dev"]) == 0


def test_dev_split_rejects_deals_outside_1_to_14(tmp_path: Path) -> None:
    assert main(["--split", "dev", "--deals", "15", "--seed", "7", "--out", str(tmp_path)]) == 2


# ---------- deal plan ----------


def test_registry_has_exactly_the_13_dev_traps() -> None:
    assert set(DEV_TRAP_IDS) == SPEC_DEV_TRAPS
    assert len(DEV_TRAP_IDS) == 13


def test_every_registered_trap_names_a_valid_check_id() -> None:
    assert {t.check_id for t in REGISTRY.values()} <= set(get_args(CheckId))


def test_deal_plan_matches_spec_counts() -> None:
    assert DEAL_PLAN[1] == DEAL_PLAN[2] == ()
    for n in range(3, 15):
        assert 2 <= len(DEAL_PLAN[n]) <= 4
        assert len(set(DEAL_PLAN[n])) == len(DEAL_PLAN[n])


def test_every_trap_appears_at_least_twice_across_dev() -> None:
    counts = {tid: 0 for tid in DEV_TRAP_IDS}
    for n in DEV_DEALS:
        for tid in DEAL_PLAN[n]:
            counts[tid] += 1
    assert min(counts.values()) >= 2, counts


def test_no_float_calls_in_synth_source() -> None:
    for path in Path("synth").rglob("*.py"):
        assert "float(" not in path.read_text(encoding="utf-8"), path


# ---------- truth invariants ----------


@pytest.mark.parametrize("n", ALL_DEALS)
def test_expected_findings_name_valid_checks_and_match_traps(
    n: int, builds: dict[int, DealBuild]
) -> None:
    truth = builds[n].truth
    assert truth.traps == [t for t in DEV_TRAP_IDS if t in DEAL_PLAN[n]]
    for e in truth.expected:
        assert e.check_id in get_args(CheckId)
        assert e.trap_id in truth.traps
    assert {e.trap_id for e in truth.expected} == set(truth.traps)


@pytest.mark.parametrize("n", [1, 2])
def test_clean_controls_have_no_traps_and_no_expected_findings(
    n: int, builds: dict[int, DealBuild]
) -> None:
    assert builds[n].truth.traps == []
    assert builds[n].truth.expected == []


@pytest.mark.parametrize("n", ALL_DEALS)
def test_stated_totals_equal_printed_rents_unless_footing_trap(
    n: int, builds: dict[int, DealBuild]
) -> None:
    truth = builds[n].truth
    printed_in_place = sum((u.in_place_rent or D(0) for u in truth.units), D(0))
    printed_market = sum((u.market_rent or D(0) for u in truth.units), D(0))
    assert truth.rr_stated_totals["market_rent"] == printed_market
    if "TRAP_RR_FOOTING" in truth.traps:
        delta = abs(truth.rr_stated_totals["in_place_rent"] - printed_in_place)
        assert delta >= D("9")
    else:
        assert truth.rr_stated_totals["in_place_rent"] == printed_in_place


@pytest.mark.parametrize("n", ALL_DEALS)
def test_units_have_pages_and_ids_are_unique_unless_dup_trap(
    n: int, builds: dict[int, DealBuild]
) -> None:
    truth = builds[n].truth
    layout = truth.rr_layout
    if layout == "rr_excel":
        assert all(u.page is None for u in truth.units), "a workbook has no printed pages"
    elif layout == "rr_grouped":
        assert [u.page for u in truth.units] == rr_grouped.paginate(
            truth.units, truth.rr_stated_totals
        )
    else:
        assert [u.page for u in truth.units] == [
            i // ROWS_PER_PAGE + 1 for i in range(len(truth.units))
        ]
    ids = [u.unit_id for u in truth.units]
    if "TRAP_RR_DUP_UNIT" in truth.traps:
        dupes = {i for i in ids if ids.count(i) > 1}
        assert len(dupes) == 1
        (dup,) = dupes
        positions = [i for i, x in enumerate(ids) if x == dup]
        assert positions[1] % ROWS_PER_PAGE == 0, "copy must be the first row of a page"
        assert positions[0] < positions[1]
    else:
        assert len(ids) == len(set(ids))


@pytest.mark.parametrize("n", ALL_DEALS)
def test_t12_lines_are_well_formed(n: int, builds: dict[int, DealBuild]) -> None:
    truth = builds[n].truth
    labels = [ln.label for ln in truth.t12_lines]
    assert len(labels) == len(set(labels))
    for ln in truth.t12_lines:
        assert ln.true_bucket != "UNMAPPED"
        frozen = "TRAP_T12_FOOTING" in truth.traps
        if not frozen:
            assert ln.stated_total == sum(ln.monthly, D(0))


# ---------- base-deal constraint: no-trap checks stay under half their threshold ----------


def expected_checks(b: DealBuild) -> set[str]:
    return {e.check_id for e in b.truth.expected}


def physical_vacancy(b: DealBuild) -> Decimal:
    unique = {u.unit_id: u for u in b.truth.units}
    return D(sum(u.status in VACANT_LIKE for u in unique.values())) / D(len(unique))


def uw_total(b: DealBuild, bucket: str) -> Decimal:
    return sum(
        (
            sum(ln.monthly, D(0))
            for ln in b.truth.t12_lines
            if ln.true_bucket == bucket and not ln.is_nonrecurring
        ),
        D(0),
    )


@pytest.mark.parametrize("n", ALL_DEALS)
def test_base_constraint_rent_roll_checks(n: int, builds: dict[int, DealBuild]) -> None:
    b = builds[n]
    checks = expected_checks(b)
    units = b.truth.units
    as_of: date = b.truth.as_of_date
    if "RR_ZERO_RENT_OCC" not in checks:
        assert not [u for u in units if u.status in OCC and not u.in_place_rent]
    if "RR_EXPIRED_LEASE" not in checks:
        assert not [
            u for u in units if u.status in OCC and (u.lease_end is None or u.lease_end < as_of)
        ]
    if "RR_CONCESSION" not in checks:
        assert not [u for u in units if u.concession_note]
    # Outlier: no trap ever plants it, so the limit is half of 0.25 in every deal.
    groups: dict[str | None, list[Decimal]] = {}
    for u in units:
        if u.in_place_rent and u.in_place_rent > 0:
            groups.setdefault(u.unit_type, []).append(u.in_place_rent)
    for rents in groups.values():
        if len(rents) >= 3:
            median = statistics.median(rents)
            assert max(abs(r - median) / median for r in rents) <= D("0.125")


@pytest.mark.parametrize("n", ALL_DEALS)
def test_base_constraint_rr_vs_t3_gap(n: int, builds: dict[int, DealBuild]) -> None:
    b = builds[n]
    if "X_RR_VS_T3" in expected_checks(b):
        return
    rr_annual = 12 * sum((u.in_place_rent or D(0) for u in b.truth.units if u.status in OCC), D(0))
    t3_annual = D(0)
    for ln in b.truth.t12_lines:
        if ln.true_bucket in ("GROSS_POTENTIAL_RENT", "LOSS_TO_LEASE", "VACANCY_LOSS"):
            t3_annual += sum(ln.monthly[9:12], D(0)) * 4
    assert abs(rr_annual - t3_annual) / t3_annual <= D("0.025")


@pytest.mark.parametrize("n", ALL_DEALS)
def test_base_constraint_t12_checks(n: int, builds: dict[int, DealBuild]) -> None:
    b = builds[n]
    checks = expected_checks(b)
    if "T12_FOOTING" not in checks:
        for ln in b.truth.t12_lines:
            assert abs(sum(ln.monthly, D(0)) - ln.stated_total) <= D("0.50")
    if "T12_NONRECURRING" not in checks:
        assert not [ln for ln in b.truth.t12_lines if ln.is_nonrecurring]
    if "T12_SPIKE" not in checks:
        exempt = ("REAL_ESTATE_TAXES", "INSURANCE", "CAPEX_RESERVES")
        for ln in b.truth.t12_lines:
            if ln.true_bucket in exempt:
                continue
            median = statistics.median(abs(v) for v in ln.monthly)
            if median > 0:
                assert max(abs(v) for v in ln.monthly) <= median * D("1.5"), ln.label


@pytest.mark.parametrize("n", ALL_DEALS)
def test_base_constraint_pro_forma_checks(n: int, builds: dict[int, DealBuild]) -> None:
    b = builds[n]
    checks = expected_checks(b)
    pf = {k: D(v) for k, v in b.proforma["lines"].items()}  # type: ignore[union-attr]
    if "PF_TAX_DROP" not in checks:
        assert pf["REAL_ESTATE_TAXES"] >= uw_total(b, "REAL_ESTATE_TAXES")
    if "PF_INS_DROP" not in checks:
        t12_ins = uw_total(b, "INSURANCE")
        assert (t12_ins - pf["INSURANCE"]) / t12_ins <= D("0.05")
    if "PF_VACANCY" not in checks:
        assert physical_vacancy(b) - D(str(b.proforma["vacancy_rate"])) <= D("0.0025")
    if "PF_MGMT_FEE" not in checks:
        egi = sum(
            (
                uw_total(b, k)
                for k in (
                    "GROSS_POTENTIAL_RENT",
                    "LOSS_TO_LEASE",
                    "VACANCY_LOSS",
                    "CONCESSIONS",
                    "BAD_DEBT",
                    "UTILITY_REIMBURSEMENT",
                    "OTHER_INCOME",
                )
            ),
            D(0),
        )
        t12_pct = uw_total(b, "MANAGEMENT_FEE") / egi
        assert t12_pct - D(str(b.proforma["mgmt_fee_pct"])) <= D("0.0025")


# ---------- each trap actually plants what the spec says ----------


def trap_deals(trap_id: str) -> list[int]:
    return [n for n in ALL_DEALS if trap_id in DEAL_PLAN[n]]


def expected_for(b: DealBuild, trap_id: str):
    return next(e for e in b.truth.expected if e.trap_id == trap_id)


@pytest.mark.parametrize("n", trap_deals("TRAP_RR_ZERO_RENT"))
def test_zero_rent_trap_plants_one_or_two_occupied_zero_units(
    n: int, builds: dict[int, DealBuild]
) -> None:
    b = builds[n]
    ids = expected_for(b, "TRAP_RR_ZERO_RENT").unit_ids
    assert 1 <= len(ids) <= 2
    for u in b.truth.units:
        if u.unit_id in ids:
            assert u.status == "occupied" and u.in_place_rent == D("0.00")


@pytest.mark.parametrize("n", trap_deals("TRAP_RR_EXPIRED_LEASE"))
def test_expired_lease_trap_plants_two_to_five_units_one_to_nine_months_back(
    n: int, builds: dict[int, DealBuild]
) -> None:
    b = builds[n]
    ids = expected_for(b, "TRAP_RR_EXPIRED_LEASE").unit_ids
    assert 2 <= len(ids) <= 5
    for u in b.truth.units:
        if u.unit_id in ids:
            months_back = (b.truth.as_of_date - u.lease_end).days / 30.4
            assert 0.9 <= months_back <= 9.1


@pytest.mark.parametrize("n", trap_deals("TRAP_RR_HIDDEN_CONCESSION"))
def test_hidden_concession_trap_notes_15_to_30_percent_of_units(
    n: int, builds: dict[int, DealBuild]
) -> None:
    b = builds[n]
    noted = [u for u in b.truth.units if u.concession_note]
    assert {u.unit_id for u in noted} == set(expected_for(b, "TRAP_RR_HIDDEN_CONCESSION").unit_ids)
    share = len(noted) / len(b.truth.units)
    assert 0.12 <= share <= 0.32
    assert all(re.search(r"mo free|off/mo", u.concession_note or "") for u in noted)


@pytest.mark.parametrize("n", trap_deals("TRAP_X_RENT_PUSH"))
def test_rent_push_trap_opens_a_gap_over_the_threshold(
    n: int, builds: dict[int, DealBuild]
) -> None:
    b = builds[n]
    rr_annual = 12 * sum((u.in_place_rent or D(0) for u in b.truth.units if u.status in OCC), D(0))
    t3 = sum(
        (
            sum(ln.monthly[9:12], D(0)) * 4
            for ln in b.truth.t12_lines
            if ln.true_bucket in ("GROSS_POTENTIAL_RENT", "LOSS_TO_LEASE", "VACANCY_LOSS")
        ),
        D(0),
    )
    assert (rr_annual - t3) / t3 > D("0.05")


@pytest.mark.parametrize("n", trap_deals("TRAP_T12_FOOTING"))
def test_t12_footing_trap_breaks_exactly_one_line(n: int, builds: dict[int, DealBuild]) -> None:
    b = builds[n]
    broken = [
        ln for ln in b.truth.t12_lines if abs(sum(ln.monthly, D(0)) - ln.stated_total) > D("1.00")
    ]
    assert [ln.label for ln in broken] == expected_for(b, "TRAP_T12_FOOTING").line_labels


@pytest.mark.parametrize("n", trap_deals("TRAP_T12_ONE_TIME_INCOME"))
def test_one_time_income_trap_adds_a_single_month_nonrecurring_line(
    n: int, builds: dict[int, DealBuild]
) -> None:
    b = builds[n]
    (line,) = [ln for ln in b.truth.t12_lines if ln.is_nonrecurring]
    assert line.label in ("Insurance Proceeds", "Lease Buyout Fee")
    assert line.true_bucket == "OTHER_INCOME"
    assert sum(1 for v in line.monthly if v != 0) == 1


@pytest.mark.parametrize("n", trap_deals("TRAP_T12_SPIKE"))
def test_spike_trap_makes_one_rm_month_four_to_six_times_median(
    n: int, builds: dict[int, DealBuild]
) -> None:
    b = builds[n]
    (label,) = expected_for(b, "TRAP_T12_SPIKE").line_labels
    line = next(ln for ln in b.truth.t12_lines if ln.label == label)
    assert line.true_bucket == "REPAIRS_MAINTENANCE"
    median = statistics.median(line.monthly)
    ratio = max(line.monthly) / median
    assert D("3.99") <= ratio <= D("6.01")
    assert max(line.monthly) - median > D("1000")


@pytest.mark.parametrize("n", trap_deals("TRAP_PF_TAX_DROP"))
def test_pf_tax_drop_trap(n: int, builds: dict[int, DealBuild]) -> None:
    b = builds[n]
    drop = 1 - D(b.proforma["lines"]["REAL_ESTATE_TAXES"]) / uw_total(b, "REAL_ESTATE_TAXES")  # type: ignore[index]
    assert D("0.099") <= drop <= D("0.251")


@pytest.mark.parametrize("n", trap_deals("TRAP_PF_INS_DROP"))
def test_pf_ins_drop_trap(n: int, builds: dict[int, DealBuild]) -> None:
    b = builds[n]
    drop = 1 - D(b.proforma["lines"]["INSURANCE"]) / uw_total(b, "INSURANCE")  # type: ignore[index]
    assert D("0.199") <= drop <= D("0.351")


@pytest.mark.parametrize("n", trap_deals("TRAP_PF_VACANCY"))
def test_pf_vacancy_trap(n: int, builds: dict[int, DealBuild]) -> None:
    b = builds[n]
    assert D("0.02") <= D(str(b.proforma["vacancy_rate"])) <= D("0.03")
    assert physical_vacancy(b) - D(str(b.proforma["vacancy_rate"])) > D("0.005")


@pytest.mark.parametrize("n", trap_deals("TRAP_PF_MGMT_FEE"))
def test_pf_mgmt_fee_trap(n: int, builds: dict[int, DealBuild]) -> None:
    b = builds[n]
    assert D(str(b.proforma["mgmt_fee_pct"])) == D("0.02")
    egi = sum(
        (
            uw_total(b, k)
            for k in ("GROSS_POTENTIAL_RENT", "LOSS_TO_LEASE", "VACANCY_LOSS", "OTHER_INCOME")
        ),
        D(0),
    )
    assert D("0.04") - D("0.001") <= uw_total(b, "MANAGEMENT_FEE") / egi <= D("0.05") + D("0.001")


@pytest.mark.parametrize("layout", sorted(RR_COLUMNS))
def test_every_possible_rent_roll_header_fits_its_column(layout: str) -> None:
    """A header wider than its column runs into its neighbour and merges into one phrase."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    from cre_screen.config import load_config

    synonyms = load_config().rr_headers.fields
    insets = 6 + 3  # left padding plus right padding used by the layouts
    columns = RR_COLUMNS[layout]
    for field, width, _align in columns:
        for header in synonyms.get(field, []):
            fits = stringWidth(header, "Helvetica-Bold", 7) + insets <= width
            if layout == "rr_classic":
                assert fits, (field, header)  # classic must fit every synonym
    assert sum(width for _f, width, _a in columns) in (720, 936)


@pytest.mark.parametrize("layout", ["rr_grouped", "rr_wide"])
def test_every_header_the_generator_can_pick_for_a_new_layout_fits(layout: str, params) -> None:
    """The style only draws synonyms that fit, so a picked header never overflows its column."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    widths = {f: w for f, w, _a in RR_COLUMNS[layout]}
    deal = next(n for n in DEV_DEALS if PLAN[n].rr_layout == layout)
    for seed in range(7, 40):
        headers = build_deal(deal, seed, params).style.rr_headers
        for field, text in headers.items():
            assert stringWidth(text, "Helvetica-Bold", 7) + 9 <= widths[field], (seed, field, text)


# ---------- layout plan and formats (SPEC 11.2) ----------


def test_format_counts_match_the_spec() -> None:
    def counts(deals) -> tuple[int, int, int]:
        rr_xlsx = sum(PLAN[n].rr_format == "xlsx" for n in deals)
        scanned = sum(PLAN[n].rr_format == "pdf_scanned" for n in deals)
        t12_xlsx = sum(PLAN[n].t12_format == "xlsx" for n in deals)
        return rr_xlsx, scanned, t12_xlsx

    assert counts(DEV_DEALS) == (4, 2, 4)
    assert counts(HELDOUT_DEALS) == (2, 2, 2)


def test_every_layout_family_is_used_in_dev() -> None:
    assert {PLAN[n].rr_layout for n in DEV_DEALS} == {
        "rr_classic",
        "rr_grouped",
        "rr_wide",
        "rr_excel",
    }
    assert {PLAN[n].t12_layout for n in DEV_DEALS} == {"t12_grid", "t12_sections", "t12_excel"}


def test_every_deal_with_a_duplicate_row_trap_uses_the_classic_layout() -> None:
    for n, traps in DEAL_PLAN.items():
        if "TRAP_RR_DUP_UNIT" in traps:
            assert PLAN[n].rr_layout == "rr_classic", n


def test_deals_one_to_five_keep_the_original_classic_pdf_layouts() -> None:
    assert all(
        PLAN[n].rr_layout == "rr_classic" and PLAN[n].t12_layout == "t12_grid" for n in range(1, 6)
    )
    assert not any(PLAN[n].scanned for n in range(1, 6))


def test_committed_dev_truth_equals_a_fresh_generation(params, tmp_path: Path) -> None:
    """Committed files equal today's output for the seed (truth, not PDF bytes)."""
    out = tmp_path / "fresh"
    assert main(["--split", "dev", "--deals", "1-14", "--seed", "7", "--out", str(out)]) == 0
    for n in DEV_DEALS:
        name = f"deal_{n:03d}"
        for fname in ("truth.json", "meta.json", "proforma.yaml"):
            fresh = (out / name / fname).read_bytes()
            assert fresh == (DATA / name / fname).read_bytes(), (name, fname)


DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"
