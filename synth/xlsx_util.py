"""Deterministic .xlsx writing: fixed workbook properties and fixed zip timestamps.

Input workbooks hold values, never formulas (openpyxl returns None for formula cells in files
Excel never opened), and money goes in as Decimal, never float.
"""

from __future__ import annotations

import io
import os
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from openpyxl import Workbook
from openpyxl.writer.excel import ExcelWriter

FIXED_STAMP = datetime(2026, 1, 1, 0, 0, 0)
ZIP_STAMP = (2026, 1, 1, 0, 0, 0)


def write_xlsx(wb: Workbook, path: Path) -> None:
    """Save ``wb`` so the same content always produces the same bytes."""
    wb.properties.creator = "synthetic"
    wb.properties.created = wb.properties.modified = FIXED_STAMP
    buffer = io.BytesIO()
    # wb.save() stamps `modified` with the current time; ExcelWriter is save() minus that line.
    ExcelWriter(wb, ZipFile(buffer, "w", ZIP_DEFLATED, allowZip64=True)).save()
    buffer.seek(0)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with ZipFile(buffer) as source, ZipFile(tmp, "w", ZIP_DEFLATED) as target:
        for info in source.infolist():
            fixed = ZipInfo(info.filename, date_time=ZIP_STAMP)
            fixed.compress_type = ZIP_DEFLATED
            fixed.external_attr = info.external_attr
            target.writestr(fixed, source.read(info.filename))
    os.replace(tmp, path)
