"""proforma.yaml -> BrokerProForma. Money values must be quoted strings, never YAML floats."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from cre_screen.errors import ProformaError
from cre_screen.schemas.citation import Citation
from cre_screen.schemas.proforma import BrokerProForma

MONEY_FIELDS = ("asking_price",)
RATE_FIELDS = ("vacancy_rate", "mgmt_fee_pct")


def _decimal(value: Any, where: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float):
        raise ProformaError(
            f'{where} is a YAML float ({value!r}); quote it as a string, e.g. "{value}"'
        )
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ProformaError(f"{where} must be a quoted number, got {value!r}")
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ProformaError(f"{where} is not a number: {value!r}") from exc


def load_proforma(path: Path, *, source_file: str | None = None) -> BrokerProForma:
    name = source_file or path.name
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProformaError(f"cannot read {path.as_posix()}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ProformaError(f"invalid YAML in {path.as_posix()}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProformaError(f"{name} must contain a mapping at the top level")

    data = dict(raw)
    for field in (*MONEY_FIELDS, *RATE_FIELDS):
        if field in data:
            data[field] = _decimal(data[field], field)
    lines = data.get("lines")
    if not isinstance(lines, dict):
        raise ProformaError(f"{name}: 'lines' must be a mapping of bucket to annual amount")
    data["lines"] = {str(k): _decimal(v, f"lines.{k}") for k, v in lines.items()}
    data["citation"] = Citation(
        source_file=name,
        row_index=0,
        raw_text=str(raw.get("source_note", "")),
        provenance="manual_entry",
    )
    try:
        return BrokerProForma(**data)
    except (ValidationError, TypeError) as exc:
        raise ProformaError(f"{name}: {exc}") from exc
