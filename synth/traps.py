"""The 13 traps (SPEC 11.2) and their held-out variants.

Each trap mutates the document view and records what the reconciliation gate should find.

Phases, in order: ``tweak_sample`` (before units exist), ``pre`` (units built, T-12 not yet),
``post`` (T-12 built), ``finalize`` (stated rent roll totals computed), ``proforma``.
Within a phase traps run in REGISTRY order, so the duplicate-row trap copies a unit after any
other trap has changed it.

Held-out variants (``ctx.variant == "heldout"``, used only for deals 15 to 20) are harder
versions of five traps. Every branch is gated on the variant, so the dev draw sequence, and with
it the dev data, is identical to what it was before the variants existed.
"""

from __future__ import annotations

import statistics
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import ClassVar

from cre_screen.schemas.common import OPEX_BUCKETS, REVENUE_BUCKETS, CheckId, to_cents
from cre_screen.schemas.findings import ExpectedFinding
from synth.context import BuildCtx, WorkLine
from synth.layouts.rr_classic import ROWS_PER_PAGE
from synth.util import ONE, ZERO, rand_dec

D = Decimal

OCCUPIED_LIKE = ("occupied", "notice")
HELDOUT_MIN_UNITS = 60


class Trap:
    trap_id: ClassVar[str]
    check_id: ClassVar[CheckId]

    def tweak_sample(self, ctx: BuildCtx) -> None:
        return None

    def pre(self, ctx: BuildCtx) -> None:
        return None

    def post(self, ctx: BuildCtx) -> None:
        return None

    def finalize(self, ctx: BuildCtx) -> None:
        return None

    def proforma(self, ctx: BuildCtx) -> None:
        return None

    def expect(
        self, ctx: BuildCtx, unit_ids: list[str] | None = None, labels: list[str] | None = None
    ) -> None:
        ctx.expected.append(
            ExpectedFinding(
                check_id=self.check_id,
                trap_id=self.trap_id,
                unit_ids=unit_ids or [],
                line_labels=labels or [],
            )
        )


def footnote_lines(note: str, unit_ids: list[str], width: int = 95) -> list[str]:
    """A footnote naming units, wrapped so it stays inside the printed page."""
    lines: list[str] = []
    current = f"Concession: {note} - units "
    for index, unit_id in enumerate(unit_ids):
        piece = unit_id + (", " if index < len(unit_ids) - 1 else "")
        if len(current) + len(piece) > width:
            lines.append(current.rstrip())
            current = "  "
        current += piece
    lines.append(current.rstrip())
    return lines


def _line_for(ctx: BuildCtx, bucket: str) -> WorkLine:
    return next(ln for ln in ctx.lines if ln.bucket == bucket)


def _t12_total(ctx: BuildCtx, bucket: str) -> Decimal:
    return sum(
        (
            sum(ln.monthly, ZERO)
            for ln in ctx.lines
            if ln.bucket == bucket and not ln.is_nonrecurring
        ),
        ZERO,
    )


def _pf_lines(ctx: BuildCtx) -> dict[str, Decimal]:
    lines = ctx.pf["lines"]
    assert isinstance(lines, dict)
    return lines


def _pf_egi(ctx: BuildCtx) -> Decimal:
    lines = _pf_lines(ctx)
    return sum((v for k, v in lines.items() if k in REVENUE_BUCKETS), ZERO)


class RRFooting(Trap):
    trap_id = "TRAP_RR_FOOTING"
    check_id = "RR_FOOTING"

    def tweak_sample(self, ctx: BuildCtx) -> None:
        if ctx.variant == "heldout":
            # One omitted row must stay small against the rent roll vs T-3 gap threshold.
            top = max(HELDOUT_MIN_UNITS, ctx.params.unit_count.max)
            ctx.sample.n_units = ctx.rng.randint(HELDOUT_MIN_UNITS, top)

    def finalize(self, ctx: BuildCtx) -> None:
        if ctx.variant == "heldout":
            # A unit row is left out of the printed table but stays in the stated total.
            pool = [u for u in ctx.units if u.status in OCCUPIED_LIKE and u.in_place_rent]
            omitted = min(pool, key=lambda u: u.in_place_rent or ZERO)
            ctx.units.remove(omitted)
            self.expect(ctx)
            return
        total = ctx.rr_totals["in_place_rent"]
        whole = f"{int(total)}"
        cents = total - Decimal(int(total))
        swappable = [
            i
            for i in range(len(whole) - 1)
            if whole[i] != whole[i + 1] and not (i == 0 and whole[1] == "0")
        ]
        i = ctx.rng.choice(swappable)
        digits = list(whole)
        digits[i], digits[i + 1] = digits[i + 1], digits[i]
        ctx.rr_totals["in_place_rent"] = Decimal("".join(digits)) + cents
        self.expect(ctx)


class RRZeroRent(Trap):
    trap_id = "TRAP_RR_ZERO_RENT"
    check_id = "RR_ZERO_RENT_OCC"

    def pre(self, ctx: BuildCtx) -> None:
        chosen = ctx.pick_units(ctx.rng.randint(1, 2), lambda u: u.status == "occupied")
        for u in chosen:
            u.in_place_rent = to_cents(ZERO)
        self.expect(ctx, unit_ids=[u.unit_id for u in chosen])


class RRExpiredLease(Trap):
    trap_id = "TRAP_RR_EXPIRED_LEASE"
    check_id = "RR_EXPIRED_LEASE"

    def pre(self, ctx: BuildCtx) -> None:
        chosen = ctx.pick_units(ctx.rng.randint(2, 5), lambda u: u.status == "occupied")
        for u in chosen:
            end = ctx.as_of - timedelta(days=ctx.rng.randint(30, 270))
            u.lease_end = end
            u.lease_start = end - timedelta(days=364)
        self.expect(ctx, unit_ids=[u.unit_id for u in chosen])


class RRHiddenConcession(Trap):
    trap_id = "TRAP_RR_HIDDEN_CONCESSION"
    check_id = "RR_CONCESSION"
    NOTES: ClassVar[tuple[str, ...]] = (
        "1 mo free on 12 mo lease",
        "$100 off/mo",
        "$150 off/mo",
    )

    def pre(self, ctx: BuildCtx) -> None:
        share = rand_dec(ctx.rng, D("0.15"), D("0.30"), 2)
        k = max(1, int((share * len(ctx.units)).to_integral_value(ROUND_HALF_UP)))
        chosen = ctx.pick_units(k, lambda u: u.status == "occupied")
        if ctx.variant == "heldout":
            # Nothing in the row: the concession appears only in a footnote listing unit numbers.
            note = ctx.rng.choice(self.NOTES)
            ctx.footnotes.extend(footnote_lines(note, [u.unit_id for u in chosen]))
        else:
            for u in chosen:
                u.concession_note = ctx.rng.choice(self.NOTES)
        self.expect(ctx, unit_ids=[u.unit_id for u in chosen])


class XRentPush(Trap):
    trap_id = "TRAP_X_RENT_PUSH"
    check_id = "X_RR_VS_T3"

    def post(self, ctx: BuildCtx) -> None:
        push = rand_dec(ctx.rng, D("0.08"), D("0.15"), 3)
        for u in ctx.units:
            if u.status in OCCUPIED_LIKE and u.in_place_rent:
                u.in_place_rent = to_cents(u.in_place_rent * (ONE + push))
        self.expect(ctx)


class T12Footing(Trap):
    trap_id = "TRAP_T12_FOOTING"
    check_id = "T12_FOOTING"
    EXCLUDED: ClassVar[tuple[str, ...]] = (
        "REAL_ESTATE_TAXES",
        "INSURANCE",
        "REPAIRS_MAINTENANCE",
    )

    def post(self, ctx: BuildCtx) -> None:
        pool = [
            ln
            for ln in ctx.lines
            if ln.bucket in OPEX_BUCKETS
            and ln.bucket not in self.EXCLUDED
            and ln.label not in ctx.used_lines
        ]
        line = ctx.rng.choice(pool)
        ctx.used_lines.add(line.label)
        line.frozen_total = sum(line.monthly, ZERO)
        month = ctx.rng.randrange(12)
        bump = rand_dec(ctx.rng, D("0.10"), D("0.30"), 2)
        line.monthly[month] = to_cents(line.monthly[month] * (ONE + bump))
        self.expect(ctx, labels=[line.label])


class T12OneTimeIncome(Trap):
    trap_id = "TRAP_T12_ONE_TIME_INCOME"
    check_id = "T12_NONRECURRING"

    def post(self, ctx: BuildCtx) -> None:
        label = ctx.rng.choice(["Insurance Proceeds", "Lease Buyout Fee"])
        if ctx.variant == "heldout":
            label = "Other Income - Misc Adj"  # ambiguous: nothing in the label says one-time
        monthly = [to_cents(ZERO)] * 12
        monthly[ctx.rng.randrange(12)] = rand_dec(ctx.rng, D("5000"), D("25000"), 2)
        last_revenue = max(i for i, ln in enumerate(ctx.lines) if ln.bucket in REVENUE_BUCKETS)
        ctx.lines.insert(
            last_revenue + 1,
            WorkLine(label=label, bucket="OTHER_INCOME", monthly=monthly, is_nonrecurring=True),
        )
        ctx.used_lines.add(label)
        self.expect(ctx, labels=[label])


class T12Spike(Trap):
    trap_id = "TRAP_T12_SPIKE"
    check_id = "T12_SPIKE"

    def post(self, ctx: BuildCtx) -> None:
        line = _line_for(ctx, "REPAIRS_MAINTENANCE")
        ctx.used_lines.add(line.label)
        median = statistics.median(line.monthly)
        month = ctx.rng.choice([i for i, v in enumerate(line.monthly) if v >= median])
        factor = rand_dec(ctx.rng, D("4.0"), D("6.0"), 1)
        line.monthly[month] = to_cents(median * factor)
        self.expect(ctx, labels=[line.label])


class RRDupUnit(Trap):
    trap_id = "TRAP_RR_DUP_UNIT"
    check_id = "RR_DUP_UNIT"
    MIN_UNITS = 60  # keeps the doubled row's effect on the RR vs T-3 gap under half its threshold

    def tweak_sample(self, ctx: BuildCtx) -> None:
        top = max(self.MIN_UNITS, ctx.params.unit_count.max)
        ctx.sample.n_units = ctx.rng.randint(self.MIN_UNITS, top)

    def post(self, ctx: BuildCtx) -> None:
        if ctx.rr_layout != "rr_classic":
            raise ValueError("the duplicate-row traps rely on classic fixed-size pages")
        if ctx.variant == "heldout":
            self._same_page(ctx)
            return
        boundary = ctx.rng.choice(range(ROWS_PER_PAGE, len(ctx.units), ROWS_PER_PAGE))
        # Duplicate the cheapest unit printed so far (a vacant one if any).
        source = min(ctx.units[:boundary], key=lambda u: u.in_place_rent or ZERO)
        ctx.units.insert(boundary, source.model_copy(deep=True))
        self.expect(ctx, unit_ids=[source.unit_id])

    def _same_page(self, ctx: BuildCtx) -> None:
        """Held-out variant: the copy sits on the same page as the original, not next to it."""
        starts = list(range(0, len(ctx.units) - ROWS_PER_PAGE + 1, ROWS_PER_PAGE))
        start = ctx.rng.choice(starts)
        window = ctx.units[start : start + ROWS_PER_PAGE - 4]
        first = start + min(range(len(window)), key=lambda k: window[k].in_place_rent or ZERO)
        second = ctx.rng.randint(first + 2, start + ROWS_PER_PAGE - 2)
        source = ctx.units[first]
        ctx.units.insert(second, source.model_copy(deep=True))
        self.expect(ctx, unit_ids=[source.unit_id])


class PFTaxDrop(Trap):
    trap_id = "TRAP_PF_TAX_DROP"
    check_id = "PF_TAX_DROP"

    def proforma(self, ctx: BuildCtx) -> None:
        cut = rand_dec(ctx.rng, D("0.10"), D("0.25"), 3)
        _pf_lines(ctx)["REAL_ESTATE_TAXES"] = to_cents(
            _t12_total(ctx, "REAL_ESTATE_TAXES") * (ONE - cut)
        )
        if ctx.variant == "heldout":
            ctx.pf["note_suffix"] = "Tax appeal pending."
        self.expect(ctx)


class PFInsDrop(Trap):
    trap_id = "TRAP_PF_INS_DROP"
    check_id = "PF_INS_DROP"

    def proforma(self, ctx: BuildCtx) -> None:
        cut = rand_dec(ctx.rng, D("0.20"), D("0.35"), 3)
        _pf_lines(ctx)["INSURANCE"] = to_cents(_t12_total(ctx, "INSURANCE") * (ONE - cut))
        self.expect(ctx)


class PFVacancy(Trap):
    trap_id = "TRAP_PF_VACANCY"
    check_id = "PF_VACANCY"

    def tweak_sample(self, ctx: BuildCtx) -> None:
        ctx.sample.vac_rate = rand_dec(ctx.rng, D("0.06"), D("0.09"), 3)

    def proforma(self, ctx: BuildCtx) -> None:
        rate = rand_dec(ctx.rng, D("0.02"), D("0.03"), 3)
        lines = _pf_lines(ctx)
        lines["VACANCY_LOSS"] = -to_cents(lines["GROSS_POTENTIAL_RENT"] * rate)
        ctx.pf["vacancy_rate"] = rate
        self.expect(ctx)


class PFMgmtFee(Trap):
    trap_id = "TRAP_PF_MGMT_FEE"
    check_id = "PF_MGMT_FEE"

    def tweak_sample(self, ctx: BuildCtx) -> None:
        ctx.sample.mgmt_pct = rand_dec(ctx.rng, D("0.04"), D("0.05"), 3)

    def proforma(self, ctx: BuildCtx) -> None:
        pct = D("0.02")
        _pf_lines(ctx)["MANAGEMENT_FEE"] = to_cents(_pf_egi(ctx) * pct)
        ctx.pf["mgmt_fee_pct"] = pct
        self.expect(ctx)


# Order matters: see module docstring.
REGISTRY: dict[str, Trap] = {
    t.trap_id: t
    for t in (
        RRZeroRent(),
        RRExpiredLease(),
        RRHiddenConcession(),
        XRentPush(),
        T12Footing(),
        T12OneTimeIncome(),
        T12Spike(),
        RRDupUnit(),
        RRFooting(),
        PFTaxDrop(),
        PFInsDrop(),
        PFVacancy(),
        PFMgmtFee(),
    )
}

DEV_TRAP_IDS: tuple[str, ...] = tuple(REGISTRY)

# Deals 001-002 are clean controls; 003-014 carry 2-4 traps and every trap appears at least twice.
# The plan is static data, so tests can check it without touching any held-out file.
DEAL_PLAN: dict[int, tuple[str, ...]] = {
    1: (),
    2: (),
    3: ("TRAP_RR_FOOTING", "TRAP_RR_ZERO_RENT", "TRAP_T12_SPIKE"),
    4: (
        "TRAP_RR_EXPIRED_LEASE",
        "TRAP_RR_HIDDEN_CONCESSION",
        "TRAP_PF_TAX_DROP",
        "TRAP_T12_FOOTING",
    ),
    5: ("TRAP_RR_DUP_UNIT", "TRAP_X_RENT_PUSH", "TRAP_T12_ONE_TIME_INCOME"),
    6: ("TRAP_PF_INS_DROP", "TRAP_PF_VACANCY", "TRAP_RR_FOOTING"),
    7: ("TRAP_PF_MGMT_FEE", "TRAP_RR_ZERO_RENT"),
    8: ("TRAP_T12_SPIKE", "TRAP_RR_EXPIRED_LEASE", "TRAP_PF_INS_DROP"),
    9: ("TRAP_RR_HIDDEN_CONCESSION", "TRAP_PF_TAX_DROP", "TRAP_PF_VACANCY"),
    10: ("TRAP_T12_FOOTING", "TRAP_RR_DUP_UNIT", "TRAP_PF_MGMT_FEE"),
    11: ("TRAP_X_RENT_PUSH", "TRAP_T12_ONE_TIME_INCOME"),
    12: ("TRAP_RR_FOOTING", "TRAP_T12_SPIKE", "TRAP_PF_TAX_DROP"),
    13: (
        "TRAP_RR_ZERO_RENT",
        "TRAP_RR_EXPIRED_LEASE",
        "TRAP_X_RENT_PUSH",
        "TRAP_T12_FOOTING",
    ),
    14: ("TRAP_RR_HIDDEN_CONCESSION", "TRAP_RR_DUP_UNIT", "TRAP_T12_ONE_TIME_INCOME"),
    # Held-out (deals 015 to 019 carry 2-4 traps, 020 is a clean control). Every held-out variant
    # appears at least once: footing and tax drop in 015, duplicate and one-time income in 016,
    # concession footnote in 017.
    15: ("TRAP_RR_FOOTING", "TRAP_PF_TAX_DROP", "TRAP_T12_SPIKE"),
    16: ("TRAP_RR_DUP_UNIT", "TRAP_T12_ONE_TIME_INCOME", "TRAP_RR_ZERO_RENT"),
    17: ("TRAP_RR_HIDDEN_CONCESSION", "TRAP_RR_EXPIRED_LEASE", "TRAP_PF_VACANCY"),
    18: ("TRAP_X_RENT_PUSH", "TRAP_T12_FOOTING", "TRAP_PF_MGMT_FEE"),
    19: ("TRAP_PF_INS_DROP", "TRAP_RR_ZERO_RENT", "TRAP_T12_SPIKE"),
    20: (),
}


def traps_for(deal_number: int) -> list[Trap]:
    """The deal's traps in REGISTRY order."""
    wanted = set(DEAL_PLAN[deal_number])
    return [t for tid, t in REGISTRY.items() if tid in wanted]
