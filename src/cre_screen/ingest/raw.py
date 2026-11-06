"""Raw table types shared by the PDF and Excel readers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from cre_screen.schemas.common import SourceFormat

RowKind = Literal["unit", "subtotal", "header", "continuation", "footnote", "blank", "line"]


@dataclass(frozen=True)
class RawCell:
    text: str
    bbox: tuple[float, float, float, float] | None  # PDF only
    cell: str | None  # Excel only, e.g. "D14"


@dataclass
class RawRow:
    page: int | None
    sheet: str | None
    row_index: int
    kind: RowKind
    cells: dict[str, RawCell]  # keyed by header_text; blank cells are omitted


@dataclass
class RawTable:
    source_file: str
    source_format: SourceFormat
    headers: list[str]
    rows: list[RawRow]
    page_count: int
    header_texts_by_page: dict[int, list[str]]
    preamble_text: str  # text above the first header (as-of date, property name)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "source_format": self.source_format,
            "headers": self.headers,
            "rows": [
                {
                    "page": r.page,
                    "sheet": r.sheet,
                    "row_index": r.row_index,
                    "kind": r.kind,
                    "cells": {
                        k: {
                            "text": c.text,
                            "bbox": list(c.bbox) if c.bbox else None,
                            "cell": c.cell,
                        }
                        for k, c in r.cells.items()
                    },
                }
                for r in self.rows
            ],
            "page_count": self.page_count,
            "header_texts_by_page": {str(k): v for k, v in self.header_texts_by_page.items()},
            "preamble_text": self.preamble_text,
        }

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> RawTable:
        rows = [
            RawRow(
                page=r["page"],
                sheet=r["sheet"],
                row_index=r["row_index"],
                kind=r["kind"],
                cells={
                    k: RawCell(
                        text=c["text"],
                        bbox=tuple(c["bbox"]) if c["bbox"] else None,  # type: ignore[arg-type]
                        cell=c["cell"],
                    )
                    for k, c in r["cells"].items()
                },
            )
            for r in data["rows"]
        ]
        return cls(
            source_file=data["source_file"],
            source_format=data["source_format"],
            headers=list(data["headers"]),
            rows=rows,
            page_count=data["page_count"],
            header_texts_by_page={int(k): v for k, v in data["header_texts_by_page"].items()},
            preamble_text=data["preamble_text"],
        )
