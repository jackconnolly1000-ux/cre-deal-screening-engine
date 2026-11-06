# cre-screen

A multifamily deal-screening engine: extract rent rolls and T-12s with citations, map T-12 lines to a fixed chart of accounts, run a deterministic reconciliation gate, and export a live-formula Excel workbook. See `SPEC.md` for the build contract.

Status: M0 (skeleton). Nothing user-facing yet.

## Thresholds

Every default in `config/thresholds.yaml` is a starting placeholder to be calibrated on real deals.

## License

AGPL-3.0, because PyMuPDF (used for page rendering) is AGPL.
