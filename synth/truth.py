"""Build one synthetic deal: ground truth, document style, pro forma, and metadata."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from random import Random

from faker import Faker
from reportlab.pdfbase.pdfmetrics import stringWidth

from cre_screen.config import load_config
from cre_screen.schemas.common import OPEX_BUCKETS, REVENUE_BUCKETS, to_cents
from cre_screen.schemas.deal import DealTruth, TruthT12Line, TruthUnit
from synth import traps as traps_mod
from synth.context import BuildCtx, Sample, Style, WorkLine
from synth.formats import (
    DATE_STYLES,
    MONEY_STYLES,
    MONTH_STYLES,
    NEG_STYLES,
    STATUS_TEXT,
    fmt_month,
)
from synth.layouts import RR, RR_COLUMNS, rr_grouped
from synth.layouts._pdf_rr import Column
from synth.params import Params
from synth.plan import PLAN, LayoutPlan
from synth.util import ONE, ZERO, month_end, rand_dec, shift_month

D = Decimal
BEDS_BATHS = {"studio": (0, 1), "1x1": (1, 1), "2x1": (2, 1), "2x2": (2, 2), "3x2": (3, 2)}
PROPERTY_SUFFIX = ("Court", "Commons", "Gardens", "Flats", "Terrace", "Residences")
OPEX_ORDER = (
    "REAL_ESTATE_TAXES",
    "INSURANCE",
    "UTILITIES",
    "REPAIRS_MAINTENANCE",
    "TURNOVER",
    "CONTRACT_SERVICES",
    "PAYROLL",
    "MANAGEMENT_FEE",
    "GENERAL_ADMIN",
    "MARKETING",
)
VACANT_LIKE = ("vacant", "down")
OUTLIER_LIMIT = D("0.12")  # base deals stay under half of rr_rent_outlier.deviation_pct (0.25)


AUX_SEED_MASK = 0x5EED
HEADER_FONT = ("Helvetica-Bold", 7)
HEADER_INSETS = 6 + 3  # left padding plus right padding used by the layouts


@dataclass
class DealBuild:
    plan: LayoutPlan
    footnotes: list[str]
    truth: DealTruth
    style: Style
    month_labels: list[str]
    proforma: dict[str, object]
    meta: dict[str, object]


def unit_type_label(key: str) -> str:
    return "Studio" if key == "studio" else key


def outlier_metric(units: list[TruthUnit]) -> Decimal:
    """Largest relative deviation from the median positive rent within a unit type."""
    groups: dict[str | None, list[Decimal]] = {}
    for u in units:
        if u.in_place_rent and u.in_place_rent > 0:
            groups.setdefault(u.unit_type, []).append(u.in_place_rent)
    worst = ZERO
    for rents in groups.values():
        if len(rents) < 3:
            continue
        median = statistics.median(rents)
        worst = max(worst, max(abs(r - median) / median for r in rents))
    return worst


# ---------- sampling ----------


def _sample(rng: Random, params: Params) -> Sample:
    return Sample(
        n_units=rng.randint(params.unit_count.min, params.unit_count.max),
        psf=rand_dec(rng, params.market_rent_per_sf.min, params.market_rent_per_sf.max, 2),
        vac_rate=rand_dec(rng, params.physical_vacancy.min, params.physical_vacancy.max, 3),
        mgmt_pct=rand_dec(rng, params.mgmt_fee_pct_of_egi.min, params.mgmt_fee_pct_of_egi.max, 3),
        growth=rand_dec(rng, D("0.000"), D("0.030"), 3),
        pf_growth=rand_dec(rng, D("0.000"), D("0.020"), 3),
    )


def _fits(header: str, width: int) -> bool:
    return stringWidth(header, *HEADER_FONT) + HEADER_INSETS <= width


def _style(rng: Random, fake: Faker, columns: tuple[Column, ...], aux_seed: int) -> Style:
    """Per-deal look. Header synonyms are drawn only from those that fit their column."""
    cfg = load_config().rr_headers
    headers: dict[str, str] = {}
    for field, width, _align in columns:
        if field in cfg.fields:
            options = [h for h in cfg.fields[field] if _fits(h, width)] or cfg.fields[field]
            headers[field] = rng.choice(options)
    return Style(
        rr_headers=headers,
        t12_label_header=rng.choice(["Description", "Account", "Line Item"]),
        money_style=rng.choice(MONEY_STYLES),
        t12_neg=rng.choice(NEG_STYLES),
        zero_dash=rng.random() < 0.5,
        date_style=rng.choice(DATE_STYLES),
        month_style=rng.choice(MONTH_STYLES),
        status_style=rng.choice(sorted(STATUS_TEXT)),
        property_name=f"{fake.last_name()} {rng.choice(PROPERTY_SUFFIX)} Apartments",
        aux_seed=aux_seed,
    )


# ---------- rent roll ----------


def _build_units(ctx: BuildCtx) -> None:
    rng, p, s = ctx.rng, ctx.params, ctx.sample
    n = s.n_units
    types: list[str] = []
    cumulative = ZERO
    previous = 0
    for key, share in p.unit_mix.items():
        cumulative += share
        upto = int((cumulative * n).to_integral_value(ROUND_HALF_UP))
        types.extend([key] * (upto - previous))
        previous = upto
    rng.shuffle(types)

    order = list(range(n))
    rng.shuffle(order)
    status = ["occupied"] * n
    n_vac = max(1, int((s.vac_rate * n).to_integral_value(ROUND_HALF_UP)))
    for rank, idx in enumerate(order[:n_vac]):
        status[idx] = "down" if (rank == 0 and n_vac >= 3 and rng.random() < 0.25) else "vacant"
    cursor = n_vac
    for _ in range(rng.randint(0, p.model_or_employee_units.max)):
        status[order[cursor]] = "model"
        cursor += 1
    for _ in range(rng.randint(0, max(1, n // 20))):
        status[order[cursor]] = "notice"
        cursor += 1

    base_rent = {
        key: (D((lo + hi) // 2) * s.psf).quantize(ONE, rounding=ROUND_HALF_UP)
        for key, (lo, hi) in p.sq_ft.items()
    }
    half_noise = p.rent_noise_within_type / 2
    for _attempt in range(500):
        units: list[TruthUnit] = []
        for i, key in enumerate(types):
            beds, baths = BEDS_BATHS[key]
            lo, hi = p.sq_ft[key]
            market = (base_rent[key] * (ONE + rand_dec(rng, -half_noise, half_noise, 3))).quantize(
                ONE, rounding=ROUND_HALF_UP
            )
            ratio = rand_dec(rng, p.inplace_vs_market.min, p.inplace_vs_market.max, 2)
            in_place: Decimal | None
            if status[i] in ("occupied", "notice"):
                in_place = to_cents((market * ratio).quantize(ONE, rounding=ROUND_HALF_UP))
            elif status[i] == "model":
                in_place = to_cents(ZERO)
            else:
                in_place = None
            units.append(
                TruthUnit(
                    unit_id=f"{i // 8 + 1}{i % 8 + 1:02d}",
                    unit_type=unit_type_label(key),
                    beds=beds,
                    baths=D(baths),
                    sq_ft=rng.randint(lo, hi),
                    status=status[i],
                    in_place_rent=in_place,
                    market_rent=to_cents(market),
                    lease_start=None,
                    lease_end=None,
                )
            )
        if outlier_metric(units) <= OUTLIER_LIMIT:
            break
    else:
        raise RuntimeError("could not sample rents below the outlier limit")

    for u in units:
        if u.status in ("occupied", "notice"):
            u.lease_start = ctx.as_of - timedelta(days=rng.randint(0, 330))
            u.lease_end = u.lease_start + timedelta(days=364)
    ctx.units = units


# ---------- T-12 ----------


def _pick_label(rng: Random, synonyms: dict[str, list[str]], bucket: str, taken: set[str]) -> str:
    options = [x for x in synonyms[bucket][:3] if x not in taken]
    label = rng.choice(options)
    taken.add(label)
    return label


def _build_t12(ctx: BuildCtx) -> None:
    rng, p, s = ctx.rng, ctx.params, ctx.sample
    coa = load_config().coa.buckets
    synonyms = {b: cfg.synonyms for b, cfg in coa.items()}
    taken: set[str] = set()
    n = s.n_units

    gpr = sum((u.market_rent or ZERO for u in ctx.units), ZERO)
    vacancy = -sum((u.market_rent or ZERO for u in ctx.units if u.status in VACANT_LIKE), ZERO)
    # Loss to lease covers every unit that is not vacant/down, so GPR + LTL + vacancy equals the
    # in-place rent of occupied units (model/employee units pay nothing).
    ltl = -sum(
        (
            (u.market_rent or ZERO) - (u.in_place_rent or ZERO)
            for u in ctx.units
            if u.status not in VACANT_LIKE
        ),
        ZERO,
    )

    def scale(month: int) -> Decimal:
        if month >= 10:
            return ONE
        return ONE - s.growth * D(10 - month) / D(9)

    def noise(pct: str) -> Decimal:
        bound = D(pct)
        return ONE + rand_dec(rng, -bound, bound, 3)

    revenue: list[WorkLine] = []
    for bucket, total in (
        ("GROSS_POTENTIAL_RENT", gpr),
        ("LOSS_TO_LEASE", ltl),
        ("VACANCY_LOSS", vacancy),
    ):
        monthly = [to_cents(total * scale(m)) for m in range(1, 13)]
        revenue.append(WorkLine(_pick_label(rng, synonyms, bucket, taken), bucket, monthly))

    other_total = (
        rand_dec(rng, p.other_income_per_unit_month.min, p.other_income_per_unit_month.max, 2) * n
    )
    first_share = rand_dec(rng, D("0.50"), D("0.70"), 2)
    pool = [x for x in synonyms["OTHER_INCOME"][1:6]]
    first, second = rng.sample(pool, 2)
    taken.update((first, second))
    for label, share in ((first, first_share), (second, ONE - first_share)):
        monthly = [to_cents(other_total * share * noise("0.050")) for _ in range(12)]
        revenue.append(WorkLine(label, "OTHER_INCOME", monthly))

    egi = [sum((ln.monthly[m] for ln in revenue), ZERO) for m in range(12)]

    expenses: list[WorkLine] = []
    for bucket in OPEX_ORDER:
        label = _pick_label(rng, synonyms, bucket, taken)
        if bucket == "MANAGEMENT_FEE":
            monthly = [to_cents(egi[m] * s.mgmt_pct) for m in range(12)]
        else:
            lo, hi = p.opex_per_unit_year[bucket]
            annual = to_cents(rand_dec(rng, lo, hi, 0) * n)
            if bucket == "REAL_ESTATE_TAXES":
                half = to_cents(annual / 2)
                monthly = [half if m in (3, 9) else to_cents(ZERO) for m in range(12)]
            elif bucket == "INSURANCE":
                monthly = [annual if m == 0 else to_cents(ZERO) for m in range(12)]
            else:
                monthly = [to_cents(annual / 12 * noise("0.040")) for _ in range(12)]
        expenses.append(WorkLine(label, bucket, monthly))

    lo, hi = p.capex_per_unit_year
    capex_annual = rand_dec(rng, lo, hi, 0) * n
    capex = WorkLine(
        _pick_label(rng, synonyms, "CAPEX_RESERVES", taken),
        "CAPEX_RESERVES",
        [to_cents(capex_annual / 12)] * 12,
    )
    ctx.lines = [*revenue, *expenses, capex]


# ---------- pro forma ----------


def _uw_total(ctx: BuildCtx, bucket: str) -> Decimal:
    return sum(
        (
            sum(ln.monthly, ZERO)
            for ln in ctx.lines
            if ln.bucket == bucket and not ln.is_nonrecurring
        ),
        ZERO,
    )


def _build_proforma(ctx: BuildCtx) -> None:
    rng, s = ctx.rng, ctx.sample
    growth = ONE + s.pf_growth
    unique = {u.unit_id: u for u in ctx.units}
    physical = D(sum(1 for u in unique.values() if u.status in VACANT_LIKE)) / D(len(unique))
    vac_rate = min(
        ONE,
        physical.quantize(D("0.0001"), rounding=ROUND_CEILING) + rand_dec(rng, ZERO, D("0.010"), 3),
    )

    lines: dict[str, Decimal] = {}
    gpr = _uw_total(ctx, "GROSS_POTENTIAL_RENT")
    lines["GROSS_POTENTIAL_RENT"] = to_cents(gpr)
    lines["LOSS_TO_LEASE"] = to_cents(_uw_total(ctx, "LOSS_TO_LEASE"))
    lines["VACANCY_LOSS"] = -to_cents(gpr * vac_rate)
    lines["OTHER_INCOME"] = to_cents(_uw_total(ctx, "OTHER_INCOME") * growth)
    egi = sum((v for k, v in lines.items() if k in REVENUE_BUCKETS), ZERO)
    for bucket in OPEX_ORDER:
        if bucket == "MANAGEMENT_FEE":
            lines[bucket] = to_cents(egi * s.mgmt_pct)
        else:
            lines[bucket] = to_cents(_uw_total(ctx, bucket) * growth)
    lines["CAPEX_RESERVES"] = to_cents(_uw_total(ctx, "CAPEX_RESERVES") * growth)
    ctx.pf = {"lines": lines, "vacancy_rate": vac_rate, "mgmt_fee_pct": s.mgmt_pct}


def _finish_proforma(ctx: BuildCtx, style: Style) -> dict[str, object]:
    lines = ctx.pf["lines"]
    assert isinstance(lines, dict)
    egi = sum((v for k, v in lines.items() if k in REVENUE_BUCKETS), ZERO)
    opex = sum((v for k, v in lines.items() if k in OPEX_BUCKETS), ZERO)
    cap = rand_dec(ctx.rng, D("0.055"), D("0.070"), 3)
    price = (max(egi - opex, D("100000")) / cap / D(10000)).quantize(ONE, rounding=ROUND_HALF_UP)
    ordered = {
        b: format(lines[b], "f")
        for b in (*REVENUE_BUCKETS, *OPEX_BUCKETS, "CAPEX_RESERVES")
        if b in lines
    }
    return {
        "property_name": style.property_name,
        "asking_price": format(to_cents(price * 10000), "f"),
        "unit_count": ctx.sample.n_units,
        "vacancy_rate": format(ctx.pf["vacancy_rate"], "f"),
        "mgmt_fee_pct": format(ctx.pf["mgmt_fee_pct"], "f"),
        "source_note": " ".join(
            ["Synthetic OM p.14, 'Pro Forma Year 1' table", str(ctx.pf.get("note_suffix", ""))]
        ).strip(),
        "lines": ordered,
    }


# ---------- orchestration ----------


def build_deal(deal_number: int, seed: int, params: Params, *, split: str = "dev") -> DealBuild:
    deal_seed = seed + deal_number
    rng = Random(deal_seed)
    fake = Faker()
    fake.seed_instance(deal_seed)

    plan = PLAN[deal_number]
    as_of = month_end(2026, rng.randint(6, 10))
    sample = _sample(rng, params)
    style = _style(rng, fake, RR_COLUMNS[plan.rr_layout], deal_seed ^ AUX_SEED_MASK)
    ctx = BuildCtx(
        rng=rng,
        fake=fake,
        params=params,
        deal_number=deal_number,
        as_of=as_of,
        sample=sample,
        rr_layout=plan.rr_layout,
        variant="heldout" if split == "heldout" else "dev",
    )
    active = traps_mod.traps_for(deal_number)

    for t in active:
        t.tweak_sample(ctx)
    _build_units(ctx)
    for t in active:
        t.pre(ctx)
    _build_t12(ctx)
    for t in active:
        t.post(ctx)

    ctx.rr_totals = {
        "in_place_rent": sum((u.in_place_rent or ZERO for u in ctx.units), ZERO),
        "market_rent": sum((u.market_rent or ZERO for u in ctx.units), ZERO),
    }
    for t in active:
        t.finalize(ctx)
    stated = {k: to_cents(v) for k, v in ctx.rr_totals.items()}
    if plan.rr_layout == "rr_grouped":
        ctx.units = rr_grouped.order(ctx.units)
        pages = rr_grouped.paginate(ctx.units, stated)
    else:
        pages = RR[plan.rr_layout].paginate(ctx.units)
    for u, page in zip(ctx.units, pages, strict=True):
        u.page = page

    _build_proforma(ctx)
    for t in active:
        t.proforma(ctx)
    proforma = _finish_proforma(ctx, style)

    labels = [
        fmt_month(*shift_month(as_of.year, as_of.month, i - 11), style.month_style)
        for i in range(12)
    ]
    truth = DealTruth(
        deal_id=f"deal_{deal_number:03d}",
        seed=seed,
        split=split,  # type: ignore[arg-type]
        rr_layout=plan.rr_layout,
        t12_layout=plan.t12_layout,
        rr_format=plan.rr_format,  # type: ignore[arg-type]
        t12_format=plan.t12_format,  # type: ignore[arg-type]
        as_of_date=as_of,
        units=ctx.units,
        rr_stated_totals=stated,
        t12_lines=[
            TruthT12Line(
                label=ln.label,
                true_bucket=ln.bucket,
                monthly=ln.monthly,
                stated_total=to_cents(ln.stated_total()),
                is_nonrecurring=ln.is_nonrecurring,
            )
            for ln in ctx.lines
        ],
        traps=[t.trap_id for t in active],
        expected=ctx.expected,
    )
    meta: dict[str, object] = {
        "deal_id": truth.deal_id,
        "seed": seed,
        "deal_seed": deal_seed,
        "split": split,
        "rr_layout": truth.rr_layout,
        "t12_layout": truth.t12_layout,
        "rr_format": truth.rr_format,
        "t12_format": truth.t12_format,
        "scanned": plan.scanned,
        "traps": truth.traps,
        "property_name": style.property_name,
    }
    return DealBuild(
        plan=plan,
        footnotes=ctx.footnotes,
        truth=truth,
        style=style,
        month_labels=labels,
        proforma=proforma,
        meta=meta,
    )
