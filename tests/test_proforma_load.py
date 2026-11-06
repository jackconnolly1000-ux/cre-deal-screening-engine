from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from cre_screen.errors import ProformaError
from cre_screen.extract.proforma import load_proforma

D = Decimal
DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"

VALID = """\
property_name: "Erdrick Court Apartments"
asking_price: "7500000.00"
unit_count: 48
vacancy_rate: "0.03"
mgmt_fee_pct: null
source_note: "OM p.14, Pro Forma Year 1 table"
lines:
  GROSS_POTENTIAL_RENT: "1020000.00"
  VACANCY_LOSS: "-30600.00"
  REAL_ESTATE_TAXES: "98000.00"
  CAPEX_RESERVES: "12000.00"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "proforma.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_quoted_strings_load_as_exact_decimals(tmp_path: Path) -> None:
    pf = load_proforma(write(tmp_path, VALID))
    assert pf.asking_price == D("7500000.00")
    assert pf.vacancy_rate == D("0.03")
    assert pf.mgmt_fee_pct is None
    assert pf.lines["VACANCY_LOSS"] == D("-30600.00")
    assert all(isinstance(v, Decimal) for v in pf.lines.values())
    assert pf.unit_count == 48


def test_citation_is_a_manual_entry_pointing_at_the_file(tmp_path: Path) -> None:
    pf = load_proforma(write(tmp_path, VALID))
    assert pf.citation.provenance == "manual_entry"
    assert pf.citation.source_file == "proforma.yaml"
    assert pf.citation.raw_text.startswith("OM p.14")
    assert pf.citation.short() == "proforma.yaml (manual entry)"


@pytest.mark.parametrize("bucket", ["NON_OPERATING", "UNMAPPED"])
def test_control_buckets_are_rejected(tmp_path: Path, bucket: str) -> None:
    text = VALID.replace("  CAPEX_RESERVES:", f"  {bucket}:")
    with pytest.raises(ProformaError, match="control buckets"):
        load_proforma(write(tmp_path, text))


def test_unquoted_yaml_float_is_rejected_with_a_fix_hint(tmp_path: Path) -> None:
    text = VALID.replace('"98000.00"', "98000.10")
    with pytest.raises(ProformaError, match="YAML float"):
        load_proforma(write(tmp_path, text))


def test_unquoted_float_in_a_rate_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProformaError, match="vacancy_rate"):
        load_proforma(write(tmp_path, VALID.replace('"0.03"', "0.03")))


def test_unknown_bucket_and_unknown_key_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProformaError):
        load_proforma(write(tmp_path, VALID.replace("REAL_ESTATE_TAXES", "PROPERTY_TAXES")))
    with pytest.raises(ProformaError):
        load_proforma(write(tmp_path, VALID + "surprise: 1\n"))


def test_three_decimal_places_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProformaError):
        load_proforma(write(tmp_path, VALID.replace('"98000.00"', '"98000.005"')))


def test_unpriced_deal_may_use_null(tmp_path: Path) -> None:
    pf = load_proforma(write(tmp_path, VALID.replace('"7500000.00"', "null")))
    assert pf.asking_price is None


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "lines: 3\n"])
def test_wrong_shape_is_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(ProformaError):
        load_proforma(write(tmp_path, text))


def test_missing_file_and_bad_yaml(tmp_path: Path) -> None:
    with pytest.raises(ProformaError, match="cannot read"):
        load_proforma(tmp_path / "nope.yaml")
    with pytest.raises(ProformaError, match="invalid YAML"):
        load_proforma(write(tmp_path, "a: [unclosed\n"))


@pytest.mark.parametrize("n", range(1, 6))
def test_generated_proforma_files_load(n: int) -> None:
    pf = load_proforma(DATA / f"deal_{n:03d}" / "proforma.yaml")
    assert pf.unit_count and pf.asking_price and pf.vacancy_rate is not None
    assert "GROSS_POTENTIAL_RENT" in pf.lines and "REAL_ESTATE_TAXES" in pf.lines
