# -*- coding: utf-8 -*-
"""Read-only integrity reconciliation for the accounting books.

Nothing in this module writes. It is meant to be run against a live database --
from the CLI, from cron, or from the admin health panel -- so that drift is
*detected* rather than discovered later by an angry customer or a tax audit.

Two deliberate design choices:

**The recomputation does not use the posting helpers.** Stock and party balances
are recomputed here by walking documents directly, with the sign table written
out again below. That duplication is the point: if the poster's sign convention
is wrong, a checker that called the poster's own helper would happily confirm
the wrong answer. Every sign in :data:`_SIGNS` is a literal, so flipping the
poster's table makes this module disagree with it -- which is the behaviour a
checker must have.

**Findings are classified, not just reported.** "Something is off" is not
actionable. A mismatch is labelled as ``corruption`` (the books are wrong),
``bug`` (a state the code cannot produce), ``historical_import`` (a document
that predates the ledger, so its absence is expected), or ``expected`` (a
legitimate condition an operator might otherwise mistake for damage).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from models.accounting_models import (CashBox, CashDoc, Entity, Invoice,
                                      InvoiceLine, LedgerEntry)
from sqlalchemy import case, func

# Status values are restated rather than imported from `utils.accounting`, for
# the same reason the sign table below is: a checker that shares the poster's
# constants cannot notice the poster's constants being wrong. These two strings
# decide which documents count as live, so they are part of what is verified.
STATUS_ACTIVE = "active"
STATUS_VOID = "void"

# Money is stored as float; the tolerance absorbs representation noise only.
TOLERANCE = 1e-6

CLASS_CORRUPTION = "corruption"
CLASS_BUG = "bug"
CLASS_HISTORICAL = "historical_import"
CLASS_EXPECTED = "expected"

SEV_CRITICAL = "critical"
SEV_WARNING = "warning"
SEV_INFO = "info"

SEVERITY_ORDER = {SEV_CRITICAL: 0, SEV_WARNING: 1, SEV_INFO: 2}

# The sign table, restated independently of utils.accounting on purpose.
#   invoice sales     stock -= qty    party balance += total
#   invoice purchase  stock += qty    party balance -= total
#   cash receive      party balance -= amount   cashbox += amount
#   cash payment      party balance += amount   cashbox -= amount
_SIGNS = {
    ("invoice", "sales"): {"stock": -1.0, "party": +1.0},
    ("invoice", "purchase"): {"stock": +1.0, "party": -1.0},
    ("cashdoc", "receive"): {"stock": 0.0, "party": -1.0, "cashbox": +1.0},
    ("cashdoc", "payment"): {"stock": 0.0, "party": +1.0, "cashbox": -1.0},
}


def _num(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class Finding:
    """One integrity problem, with enough context to act on it."""

    code: str
    severity: str
    classification: str
    message: str
    object_type: str = ""
    object_id: str = ""
    expected: Optional[float] = None
    actual: Optional[float] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "classification": self.classification,
            "message": self.message,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.detail,
        }


@dataclass
class Report:
    findings: List[Finding] = field(default_factory=list)
    checked: Dict[str, int] = field(default_factory=dict)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    @property
    def by_severity(self) -> List[Finding]:
        return sorted(
            self.findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.code)
        )

    @property
    def counts(self) -> Dict[str, int]:
        out = {SEV_CRITICAL: 0, SEV_WARNING: 0, SEV_INFO: 0}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out

    @property
    def ok(self) -> bool:
        """True only when nothing needs an operator's attention."""
        return not any(f.severity in (SEV_CRITICAL, SEV_WARNING) for f in self.findings)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "counts": self.counts,
            "findings": [f.as_dict() for f in self.by_severity],
        }


# --------------------------------------------------------------------------- #
# Independent recomputation
# --------------------------------------------------------------------------- #


def recomputed_stock() -> Dict[int, float]:
    """Stock per item, from active invoice lines only."""
    signed = case(
        (Invoice.kind == "purchase", InvoiceLine.qty),
        else_=-InvoiceLine.qty,
    )
    rows = (
        _db()
        .query(InvoiceLine.item_id, func.coalesce(func.sum(signed), 0.0))
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .filter(Invoice.status == STATUS_ACTIVE)
        .group_by(InvoiceLine.item_id)
        .all()
    )
    return {int(item_id): _num(total) for item_id, total in rows}


def recomputed_party_balances() -> Dict[int, float]:
    """Party balance per person, from active invoices and active cash documents.

    The two halves are accumulated separately and combined here, because they
    live in different tables and are signed by different rules.
    """
    from_invoices = case(
        (Invoice.kind == "purchase", -Invoice.total), else_=Invoice.total
    )
    inv_rows = (
        _db()
        .query(Invoice.person_id, func.coalesce(func.sum(from_invoices), 0.0))
        .filter(Invoice.status == STATUS_ACTIVE, Invoice.person_id.isnot(None))
        .group_by(Invoice.person_id)
        .all()
    )
    cash_sign = case(
        (CashDoc.doc_type == "payment", CashDoc.amount), else_=-CashDoc.amount
    )
    cash_rows = (
        _db()
        .query(CashDoc.person_id, func.coalesce(func.sum(cash_sign), 0.0))
        .filter(CashDoc.status == STATUS_ACTIVE, CashDoc.person_id.isnot(None))
        .group_by(CashDoc.person_id)
        .all()
    )

    totals: Dict[int, float] = {}
    for person_id, value in inv_rows:
        totals[int(person_id)] = _num(value)
    for person_id, value in cash_rows:
        totals[int(person_id)] = totals.get(int(person_id), 0.0) + _num(value)
    return totals


def recomputed_cashbox_balances() -> Dict[int, float]:
    """Cashbox position per cashbox, from active cash documents."""
    sign = case((CashDoc.doc_type == "receive", CashDoc.amount), else_=-CashDoc.amount)
    rows = (
        _db()
        .query(CashDoc.cashbox_id, func.coalesce(func.sum(sign), 0.0))
        .filter(CashDoc.status == STATUS_ACTIVE, CashDoc.cashbox_id.isnot(None))
        .group_by(CashDoc.cashbox_id)
        .all()
    )
    return {int(box_id): _num(total) for box_id, total in rows}


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def check_aggregate_drift(report: Report) -> None:
    """Stored stock and balances against the recomputed values.

    Absolute quantities cannot be verified without a known opening
    baseline, so this check compares ``stored == opening + net`` where
    ``opening`` is captured by the poster the first time a ledger entry
    touches the entity.  When no baseline exists the finding is
    informational: the checker is blind, not wrong.
    """
    expected_stock = recomputed_stock()
    items = (
        _db()
        .query(
            Entity.id,
            Entity.code,
            Entity.name,
            Entity.stock_qty,
            Entity.opening_stock_qty,
        )
        .filter(Entity.type == "item")
        .all()
    )
    report.checked["items"] = len(items)

    for entity_id, code, name, stored, opening in items:
        have = _num(stored)
        net = _num(expected_stock.get(int(entity_id), 0.0))
        if opening is not None:
            expected = _num(opening) + net
            if abs(expected - have) <= TOLERANCE:
                continue
            report.add(
                Finding(
                    code="stock.drift",
                    severity=SEV_CRITICAL,
                    classification=CLASS_CORRUPTION,
                    message=(f"موجودی کالای «{name}» با اسناد فعال مطابقت ندارد."),
                    object_type="entity",
                    object_id=str(entity_id),
                    expected=expected,
                    actual=have,
                    detail={"code": code},
                )
            )
        else:
            report.add(
                Finding(
                    code="opening_balance.unverifiable",
                    severity=SEV_INFO,
                    classification=CLASS_EXPECTED,
                    message=(
                        f"کالای «{name}» مقدار موجودی {have:g} دارد ولی مبنای افتتاحیه"
                        " ثبت نشده — ناسازگاری قابل تأیید نیست."
                    ),
                    object_type="entity",
                    object_id=str(entity_id),
                    expected=0.0,
                    actual=have,
                    detail={"code": code},
                )
            )

    expected_balance = recomputed_party_balances()
    people = (
        _db()
        .query(
            Entity.id, Entity.code, Entity.name, Entity.balance, Entity.opening_balance
        )
        .filter(Entity.type == "person")
        .all()
    )
    report.checked["parties"] = len(people)

    for entity_id, code, name, stored, opening in people:
        have = _num(stored)
        net = _num(expected_balance.get(int(entity_id), 0.0))
        if opening is not None:
            expected = _num(opening) + net
            if abs(expected - have) <= TOLERANCE:
                continue
            report.add(
                Finding(
                    code="balance.drift",
                    severity=SEV_CRITICAL,
                    classification=CLASS_CORRUPTION,
                    message=f"مانده حساب «{name}» با اسناد فعال مطابقت ندارد.",
                    object_type="entity",
                    object_id=str(entity_id),
                    expected=expected,
                    actual=have,
                    detail={"code": code},
                )
            )
        else:
            report.add(
                Finding(
                    code="opening_balance.unverifiable",
                    severity=SEV_INFO,
                    classification=CLASS_EXPECTED,
                    message=(
                        f"طرف حساب «{name}» مانده {have:g} دارد ولی مبنای افتتاحیه"
                        " ثبت نشده — ناسازگاری قابل تأیید نیست."
                    ),
                    object_type="entity",
                    object_id=str(entity_id),
                    expected=0.0,
                    actual=have,
                    detail={"code": code},
                )
            )

    # The cashbox position is derived, so it cannot drift; what can be wrong is
    # a document pointing at a cashbox that no longer exists.
    boxes = {int(b.id): b for b in _db().query(CashBox).all()}
    for doc in _db().query(CashDoc).filter(CashDoc.cashbox_id.isnot(None)).all():
        if int(doc.cashbox_id) not in boxes:
            report.add(
                Finding(
                    code="ref.cashbox_missing",
                    severity=SEV_CRITICAL,
                    classification=CLASS_CORRUPTION,
                    message=(
                        f"سند «{doc.number}» به صندوقی اشاره می‌کند که وجود ندارد."
                    ),
                    object_type="cashdoc",
                    object_id=str(doc.id),
                )
            )


def check_document_states(report: Report) -> None:
    """Internal consistency of each document, independent of the ledger."""
    invoices = _db().query(Invoice).all()
    report.checked["invoices"] = len(invoices)

    for inv in invoices:
        voided = inv.status == STATUS_VOID
        if voided and not inv.voided_at:
            report.add(
                Finding(
                    code="state.void_without_timestamp",
                    severity=SEV_WARNING,
                    classification=CLASS_BUG,
                    message=f"فاکتور «{inv.number}» ابطال شده ولی زمان ابطال ندارد.",
                    object_type="invoice",
                    object_id=str(inv.id),
                )
            )
        if not voided and inv.voided_at:
            report.add(
                Finding(
                    code="state.voided_but_active",
                    severity=SEV_CRITICAL,
                    classification=CLASS_CORRUPTION,
                    message=(
                        f"فاکتور «{inv.number}» فعال است ولی زمان ابطال دارد؛"
                        " یعنی اثر آن در کتاب‌ها هست و در وضعیت نیست."
                    ),
                    object_type="invoice",
                    object_id=str(inv.id),
                )
            )
        if not voided and inv.status != STATUS_ACTIVE:
            report.add(
                Finding(
                    code="state.unknown_status",
                    severity=SEV_CRITICAL,
                    classification=CLASS_CORRUPTION,
                    message=(
                        f"فاکتور «{inv.number}» وضعیت ناشناخته «{inv.status}» دارد."
                    ),
                    object_type="invoice",
                    object_id=str(inv.id),
                    detail={"status": inv.status},
                )
            )

        lines = inv.lines or []
        if not voided:
            line_total = sum(_num(li.line_total) for li in lines)
            expect = line_total - _num(inv.discount) + _num(inv.tax)
            if abs(expect - _num(inv.total)) > 0.01:
                report.add(
                    Finding(
                        code="state.total_mismatch",
                        severity=SEV_CRITICAL,
                        classification=CLASS_CORRUPTION,
                        message=(
                            f"جمع فاکتور «{inv.number}» با جمع ردیف‌هایش نمی‌خواند."
                        ),
                        object_type="invoice",
                        object_id=str(inv.id),
                        expected=expect,
                        actual=_num(inv.total),
                    )
                )
            if _num(inv.total) <= 0:
                report.add(
                    Finding(
                        code="state.nonpositive_total",
                        severity=SEV_WARNING,
                        classification=CLASS_CORRUPTION,
                        message=f"فاکتور فعال «{inv.number}» جمع صفر یا منفی دارد.",
                        object_type="invoice",
                        object_id=str(inv.id),
                        actual=_num(inv.total),
                    )
                )

    cash_docs = _db().query(CashDoc).all()
    report.checked["cash_docs"] = len(cash_docs)
    for doc in cash_docs:
        if doc.status not in (STATUS_ACTIVE, STATUS_VOID):
            report.add(
                Finding(
                    code="state.unknown_status",
                    severity=SEV_CRITICAL,
                    classification=CLASS_CORRUPTION,
                    message=f"سند «{doc.number}» وضعیت ناشناخته «{doc.status}» دارد.",
                    object_type="cashdoc",
                    object_id=str(doc.id),
                    detail={"status": doc.status},
                )
            )
        if doc.status == STATUS_VOID and not doc.void_reason:
            report.add(
                Finding(
                    code="state.void_without_reason",
                    severity=SEV_WARNING,
                    classification=CLASS_BUG,
                    message=f"سند «{doc.number}» بدون دلیل ابطال شده است.",
                    object_type="cashdoc",
                    object_id=str(doc.id),
                )
            )


def check_references(report: Report) -> None:
    """Every document points at a party, and every line at an existing item."""
    item_ids = {
        int(i) for (i,) in _db().query(Entity.id).filter(Entity.type == "item").all()
    }
    person_ids = {
        int(p) for (p,) in _db().query(Entity.id).filter(Entity.type == "person").all()
    }

    for line in _db().query(InvoiceLine).all():
        if int(line.item_id) not in item_ids:
            report.add(
                Finding(
                    code="ref.item_missing",
                    severity=SEV_CRITICAL,
                    classification=CLASS_CORRUPTION,
                    message=(
                        f"ردیف فاکتور به کالای ناموجود {line.item_id} اشاره می‌کند."
                    ),
                    object_type="invoice_line",
                    object_id=str(line.id),
                    detail={"invoice_id": str(line.invoice_id)},
                )
            )
    for inv in _db().query(Invoice).all():
        if inv.person_id is not None and int(inv.person_id) not in person_ids:
            report.add(
                Finding(
                    code="ref.person_missing",
                    severity=SEV_CRITICAL,
                    classification=CLASS_CORRUPTION,
                    message=(
                        f"فاکتور «{inv.number}» به طرف حساب ناموجود"
                        f" {inv.person_id} اشاره می‌کند."
                    ),
                    object_type="invoice",
                    object_id=str(inv.id),
                )
            )


def check_numbering(report: Report) -> None:
    """Document numbers must be unique, since they are the operator's handle."""
    for number, count in (
        _db()
        .query(Invoice.number, func.count(Invoice.id))
        .group_by(Invoice.number)
        .having(func.count(Invoice.id) > 1)
        .all()
    ):
        report.add(
            Finding(
                code="numbering.duplicate",
                severity=SEV_CRITICAL,
                classification=CLASS_CORRUPTION,
                message=f"شماره فاکتور «{number}» برای {count} سند استفاده شده است.",
                object_type="invoice",
                detail={"number": number, "count": int(count)},
            )
        )
    for number, count in (
        _db()
        .query(CashDoc.number, func.count(CashDoc.id))
        .group_by(CashDoc.number)
        .having(func.count(CashDoc.id) > 1)
        .all()
    ):
        report.add(
            Finding(
                code="numbering.duplicate",
                severity=SEV_CRITICAL,
                classification=CLASS_CORRUPTION,
                message=f"شماره سند «{number}» برای {count} سند استفاده شده است.",
                object_type="cashdoc",
                detail={"number": number, "count": int(count)},
            )
        )


def _ledger_rows() -> List[Tuple[str, str, str, str]]:
    return [
        (e.object_type, str(e.object_id), e.action, e.payload or "")
        for e in _db().query(LedgerEntry).order_by(LedgerEntry.id.asc()).all()
    ]


def _first_ledger_id() -> Optional[int]:
    value = _db().query(func.min(LedgerEntry.id)).scalar()
    return int(value) if value is not None else None


def check_ledger(report: Report) -> None:
    """Coverage, per-document effects, and the hash chain.

    Three distinct questions, deliberately kept apart:

    * does every document have the entries its own history implies?
    * do the recorded deltas add up to the effect the document actually has?
    * is the chain still intact?
    """
    rows = _ledger_rows()
    report.checked["ledger_entries"] = len(rows)

    # -- coverage -------------------------------------------------------- #
    seen: Dict[Tuple[str, str], List[str]] = {}
    for object_type, object_id, action, _payload in rows:
        seen.setdefault((object_type, object_id), []).append(action)

    for inv in _db().query(Invoice).all():
        actions = seen.get(("invoice", str(inv.id)), [])
        if not actions:
            _classify_missing_entry(report, "invoice", inv.id, inv.number)
        elif not any(a.endswith("create") for a in actions):
            report.add(
                Finding(
                    code="ledger.missing_create",
                    severity=SEV_CRITICAL,
                    classification=CLASS_BUG,
                    message=(
                        f"فاکتور «{inv.number}» ورود اولیه در دفتر کل ندارد"
                        f" (فقط: {', '.join(actions)})."
                    ),
                    object_type="invoice",
                    object_id=str(inv.id),
                    detail={"actions": actions},
                )
            )
        if inv.status == STATUS_VOID and not any(a.endswith("void") for a in actions):
            report.add(
                Finding(
                    code="ledger.missing_void",
                    severity=SEV_CRITICAL,
                    classification=CLASS_CORRUPTION,
                    message=f"ابطال فاکتور «{inv.number}» در دفتر کل ثبت نشده است.",
                    object_type="invoice",
                    object_id=str(inv.id),
                    detail={"actions": actions},
                )
            )

    for doc in _db().query(CashDoc).all():
        actions = seen.get(("cashdoc", str(doc.id)), [])
        if not actions:
            _classify_missing_entry(report, "cashdoc", doc.id, doc.number)
        elif not any(a.endswith("create") for a in actions):
            report.add(
                Finding(
                    code="ledger.missing_create",
                    severity=SEV_CRITICAL,
                    classification=CLASS_BUG,
                    message=(
                        f"سند «{doc.number}» ورود اولیه در دفتر کل ندارد"
                        f" (فقط: {', '.join(actions)})."
                    ),
                    object_type="cashdoc",
                    object_id=str(doc.id),
                    detail={"actions": actions},
                )
            )
        if doc.status == STATUS_VOID and not any(a.endswith("void") for a in actions):
            report.add(
                Finding(
                    code="ledger.missing_void",
                    severity=SEV_CRITICAL,
                    classification=CLASS_CORRUPTION,
                    message=f"ابطال سند «{doc.number}» در دفتر کل ثبت نشده است.",
                    object_type="cashdoc",
                    object_id=str(doc.id),
                    detail={"actions": actions},
                )
            )

    # -- per-document effect --------------------------------------------- #
    person_delta: Dict[Tuple[str, str], float] = {}
    stock_delta: Dict[Tuple[str, str], Dict[int, float]] = {}
    cashbox_delta: Dict[Tuple[str, str], float] = {}
    for object_type, object_id, action, payload_text in rows:
        try:
            payload = json.loads(payload_text) if payload_text else {}
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        identity = (object_type, object_id)

        if "person_balance_delta" in payload:
            person_delta[identity] = person_delta.get(identity, 0.0) + _num(
                payload.get("person_balance_delta")
            )
        for item_id, value in (payload.get("stock_delta") or {}).items():
            try:
                item = int(item_id)
            except (TypeError, ValueError):
                continue
            # Keyed per item, because a document's net stock effect is only
            # meaningful item by item.
            per_item = stock_delta.setdefault(identity, {})
            per_item[item] = per_item.get(item, 0.0) + _num(value)

        doc_type = payload.get("doc_type")
        if object_type == "cashdoc" and doc_type in ("receive", "payment"):
            magnitude = _num(payload.get("amount"))
            # A create applies the effect; a void or delete unwinds it.
            unwinds = action.endswith("void") or action.endswith("delete")
            direction = -1.0 if unwinds else 1.0
            cashbox_delta[identity] = cashbox_delta.get(identity, 0.0) + (
                _SIGNS[("cashdoc", doc_type)]["cashbox"] * magnitude * direction
            )

    for inv in _db().query(Invoice).all():
        identity = ("invoice", str(inv.id))
        want_party = 0.0
        want_stock: Dict[int, float] = {}
        if inv.status == STATUS_ACTIVE:
            rule = _SIGNS[("invoice", inv.kind or "sales")]
            want_party = rule["party"] * _num(inv.total)
            for line in inv.lines or []:
                want_stock[int(line.item_id)] = want_stock.get(
                    int(line.item_id), 0.0
                ) + rule["stock"] * _num(line.qty)
        have_party = person_delta.get(identity, 0.0)
        if abs(want_party - have_party) > 0.01:
            report.add(
                Finding(
                    code="ledger.party_effect_mismatch",
                    severity=SEV_CRITICAL,
                    classification=CLASS_CORRUPTION,
                    message=(
                        f"اثر ثبت‌شده برای فاکتور «{inv.number}» در دفتر کل با وضعیت"
                        " واقعی آن نمی‌خواند."
                    ),
                    object_type="invoice",
                    object_id=str(inv.id),
                    expected=want_party,
                    actual=have_party,
                )
            )
        for item_id, want in want_stock.items():
            have = stock_delta.get(identity, {}).get(item_id, 0.0)
            if abs(want - have) > 0.01:
                report.add(
                    Finding(
                        code="ledger.stock_effect_mismatch",
                        severity=SEV_CRITICAL,
                        classification=CLASS_CORRUPTION,
                        message=(
                            f"اثر ثبت‌شده کالا {item_id} برای فاکتور «{inv.number}»"
                            " با ردیف‌های آن نمی‌خواند."
                        ),
                        object_type="invoice",
                        object_id=str(inv.id),
                        expected=want,
                        actual=have,
                        detail={"item_id": item_id},
                    )
                )

    for doc in _db().query(CashDoc).all():
        identity = ("cashdoc", str(doc.id))
        rule = _SIGNS.get(("cashdoc", doc.doc_type or "receive"))
        want_party = 0.0
        want_cashbox = 0.0
        if doc.status == STATUS_ACTIVE and rule is not None:
            want_party = rule["party"] * _num(doc.amount)
            want_cashbox = rule["cashbox"] * _num(doc.amount)
        have_party = person_delta.get(identity, 0.0)
        if abs(want_party - have_party) > 0.01:
            report.add(
                Finding(
                    code="ledger.party_effect_mismatch",
                    severity=SEV_CRITICAL,
                    classification=CLASS_CORRUPTION,
                    message=(
                        f"اثر ثبت‌شده برای سند «{doc.number}» در دفتر کل با وضعیت"
                        " واقعی آن نمی‌خواند."
                    ),
                    object_type="cashdoc",
                    object_id=str(doc.id),
                    expected=want_party,
                    actual=have_party,
                )
            )
        have_cashbox = cashbox_delta.get(identity, 0.0)
        if abs(want_cashbox - have_cashbox) > 0.01:
            report.add(
                Finding(
                    code="ledger.cashbox_effect_mismatch",
                    severity=SEV_CRITICAL,
                    classification=CLASS_CORRUPTION,
                    message=(f"اثر صندوق ثبت‌شده برای سند «{doc.number}» نمی‌خواند."),
                    object_type="cashdoc",
                    object_id=str(doc.id),
                    expected=want_cashbox,
                    actual=have_cashbox,
                )
            )

    # -- chain ------------------------------------------------------------ #
    from utils.accounting import verify_ledger_chain

    result = verify_ledger_chain()
    report.checked["chain_entries"] = int(result.get("checked") or 0)
    if not result.get("ok"):
        report.add(
            Finding(
                code="ledger.chain_broken",
                severity=SEV_CRITICAL,
                classification=CLASS_CORRUPTION,
                message=(
                    f"زنجیره هش دفتر کل در ردیف {result.get('broken_at')} شکسته است"
                    f" ({result.get('reason')})."
                ),
                object_type="ledger",
                object_id=str(result.get("broken_at")),
                detail=dict(result),
            )
        )


def _classify_missing_entry(report: Report, kind: str, pk: int, number: str) -> None:
    """A document with no ledger entry: corruption, or a legacy import?

    In this application the poster always writes the ledger inside the
    same transaction as the document.  If the ledger table already has
    entries but this document has none, the posting path was bypassed
    (corruption).  If the ledger is completely empty the document simply
    predates the ledger (historical import) and is expected.
    """
    first = _first_ledger_id()
    report.add(
        Finding(
            code="ledger.missing_entry",
            severity=SEV_CRITICAL if first is not None else SEV_WARNING,
            classification=CLASS_CORRUPTION if first is not None else CLASS_HISTORICAL,
            message=(
                f"{'فاکتور' if kind == 'invoice' else 'سند'} «{number}»"
                " هیچ ورودی در دفتر کل ندارد."
            ),
            object_type=kind,
            object_id=str(pk),
            detail={"first_ledger_id": first},
        )
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _db():
    from extensions import db

    return db.session


def run() -> Report:
    """Reconcile every dimension the books can drift in. Read-only."""
    report = Report()
    check_aggregate_drift(report)
    check_document_states(report)
    check_references(report)
    check_numbering(report)
    check_ledger(report)
    return report


def format_text(report: Report, limit: int = 50) -> str:
    """A human-readable summary, in Persian, for a terminal or a log line."""
    counts = report.counts
    lines = [
        "گزارش یکپارچگی حسابداری (فقط خواندنی)",
        "-" * 48,
        f"بررسی‌شده: {report.checked}",
        f"یافته‌ها: {len(report.findings)} "
        f"(بحرانی: {counts[SEV_CRITICAL]}، هشدار: {counts[SEV_WARNING]}،"
        f" اطلاعی: {counts[SEV_INFO]})",
    ]
    if not report.findings:
        lines.append("هیچ ناسازگاری‌ای پیدا نشد.")
        return "\n".join(lines)
    lines.append("")
    for finding in report.by_severity[:limit]:
        lines.append(
            f"[{finding.severity}/{finding.classification}] {finding.code}: "
            f"{finding.message}"
        )
        if finding.expected is not None or finding.actual is not None:
            lines.append(f"    expected={finding.expected!r} actual={finding.actual!r}")
    if len(report.findings) > limit:
        lines.append(f"... و {len(report.findings) - limit} یافته دیگر")
    return "\n".join(lines)
