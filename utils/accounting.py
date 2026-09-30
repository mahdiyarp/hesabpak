# -*- coding: utf-8 -*-
"""Single source of truth for HesabPak accounting postings.

Every mutation of a financial document (invoice, cash document) must go
through this module. Routes and the AI assistant are *transport* layers; they
parse and authorize input and then delegate here. That guarantees the mission
invariant:

    a single document is never applied, reversed or re-priced through two
    different code paths with divergent semantics.

Sign conventions (defined once, here, and nowhere else)
------------------------------------------------------
``Entity.balance`` is *what the party owes us*::

    invoice sales     balance += total     stock -= qty
    invoice purchase   balance -= total     stock += qty
    cash receive       balance -= amount   cashbox += amount
    cash payment       balance += amount   cashbox -= amount

A sales invoice followed by a matching receive therefore nets the balance back
to zero, which is the behaviour the application has always had.

Atomicity
---------
Every public mutation is wrapped in :func:`atomic`. Either the document rows,
the denormalized stock/balance columns and the ledger entry are all committed,
or none of them are. The ledger is written *inside* that transaction, so it can
never advertise a success that was rolled back, and a committed mutation can
never end up with no audit trail.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from markupsafe import Markup

from extensions import db
from models.accounting_models import (CashDoc, Entity, Invoice, InvoiceLine,
                                      LedgerEntry, PriceHistory)
from sqlalchemy import case, func, text

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

EPS = 1e-9

STATUS_ACTIVE = "active"
STATUS_VOID = "void"

INVOICE_KINDS = ("sales", "purchase")
CASH_DOC_TYPES = ("receive", "payment")

LEDGER_CREATE = "create"
LEDGER_UPDATE = "update"
LEDGER_VOID = "void"
LEDGER_DELETE = "delete"
LEDGER_REVERSE = "reverse"

# Per-session nesting depth of `atomic()`; see that function.
_TX_DEPTH_KEY = "hesabpak_accounting_tx_depth"


class AccountingError(ValueError):
    """Base class for every rejected accounting mutation.

    Derives from ``ValueError`` because callers (and the pre-existing test
    suite) treat a rejected posting as an invalid-value condition; keeping that
    contract avoids breaking every ``except ValueError`` at the call sites.
    """


class DocumentStateError(AccountingError):
    """The document is not in a state that allows the requested operation.

    Raised for double reversals, editing a voided document, or purging a
    document whose accounting effects could not be reversed.
    """


class InsufficientStockError(AccountingError):
    """A stock movement would drive an item below zero."""


class DocumentValidationError(AccountingError):
    """The document payload is structurally invalid."""


# --------------------------------------------------------------------------- #
# Sign conventions and small helpers
# --------------------------------------------------------------------------- #


def normalize_invoice_kind(kind: Optional[str]) -> str:
    value = (kind or "").strip().lower()
    if value not in INVOICE_KINDS:
        raise DocumentValidationError("نوع فاکتور معتبر نیست.")
    return value


def normalize_cash_doc_type(doc_type: Optional[str]) -> str:
    value = (doc_type or "").strip().lower()
    if value not in CASH_DOC_TYPES:
        raise DocumentValidationError("نوع سند دریافت/پرداخت معتبر نیست.")
    return value


def invoice_stock_sign(kind: Optional[str]) -> float:
    """``-1`` for sales (stock leaves), ``+1`` for purchase (stock arrives)."""
    return -1.0 if normalize_invoice_kind(kind) == "sales" else 1.0


def invoice_balance_sign(kind: Optional[str]) -> float:
    """``+1`` for sales (party owes us more), ``-1`` for purchase (we owe more)."""
    return 1.0 if normalize_invoice_kind(kind) == "sales" else -1.0


def cash_balance_sign(doc_type: Optional[str]) -> float:
    """Delta applied to ``Entity.balance`` for a cash document of this type."""
    return -1.0 if normalize_cash_doc_type(doc_type) == "receive" else 1.0


def cash_cashbox_sign(doc_type: Optional[str]) -> float:
    """Delta applied to a cashbox's derived balance (receive adds, payment subtracts)."""
    return 1.0 if normalize_cash_doc_type(doc_type) == "receive" else -1.0


def is_void(document: Any) -> bool:
    """True when *document* has already been cancelled/voided."""
    if document is None:
        return False
    return (getattr(document, "status", None) or STATUS_ACTIVE) != STATUS_ACTIVE


def is_active(document: Any) -> bool:
    return not is_void(document)


def active_invoices():
    """Query of invoices that currently affect stock/balances/reports."""
    return Invoice.query.filter(
        (Invoice.status.is_(None)) | (Invoice.status == STATUS_ACTIVE)
    )


def active_cashdocs():
    """Query of cash documents that currently affect balances/cashbox totals."""
    return CashDoc.query.filter(
        (CashDoc.status.is_(None)) | (CashDoc.status == STATUS_ACTIVE)
    )


def _approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(float(a) - float(b)) <= tol


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


# --------------------------------------------------------------------------- #
# Transaction boundary
# --------------------------------------------------------------------------- #


@contextmanager
def atomic():
    """Run a block as one all-or-nothing accounting transaction.

    Any exception rolls the whole unit of work back -- document rows, the
    denormalized ``entities.stock_qty`` / ``entities.balance`` columns and the
    ledger entry -- and then propagates so the caller can report the failure.

    The context manager is **re-entrant and per session**: a nested call joins
    the enclosing transaction instead of committing on its own. Without that,
    a composite operation such as :func:`delete_invoice` -- which reverses the
    posting and then purges the rows -- would commit the reversal separately
    and could leave a voided document whose ledger never recorded the deletion.
    The depth is stored on the session (``Session.info``) rather than in a
    module global, so concurrent requests on different sessions cannot observe
    each other's nesting.
    """
    depth = int(db.session.info.get(_TX_DEPTH_KEY, 0))
    if depth:
        db.session.info[_TX_DEPTH_KEY] = depth + 1
        try:
            yield db.session
        finally:
            db.session.info[_TX_DEPTH_KEY] = depth
        return

    db.session.info[_TX_DEPTH_KEY] = 1
    try:
        yield db.session
    except Exception:
        db.session.info[_TX_DEPTH_KEY] = 0
        db.session.rollback()
        raise
    else:
        db.session.info[_TX_DEPTH_KEY] = 0
        db.session.commit()


# --------------------------------------------------------------------------- #
# Ledger (written inside the accounting transaction)
# --------------------------------------------------------------------------- #


def compute_entry_hash(prev_hash: Optional[str], payload_text: str, ts_iso: str) -> str:
    digest = hashlib.sha256()
    digest.update((prev_hash or "").encode("utf-8"))
    digest.update(ts_iso.encode("utf-8"))
    digest.update((payload_text or "").encode("utf-8"))
    return digest.hexdigest()


def _lock_chain_head() -> None:
    """Acquire the chain-head critical section inside the current transaction.

    ``LedgerChain`` holds exactly one row. Bumping it is a real write, so it takes
    the database's writer lock / the row lock *before* the head is read. Without
    it, two concurrent appends can both read the same head and emit two entries
    sharing one ``prev_hash``, silently forking the audit chain.

    A missing lock row is created on the spot so a fresh database behaves
    identically to an existing one.
    """
    updated = db.session.execute(
        text(
            "UPDATE ledger_chain SET generation = COALESCE(generation, 0) + 1 WHERE id = 1"
        )
    )
    if updated.rowcount == 0:
        db.session.execute(
            text(
                "INSERT INTO ledger_chain (id, generation) VALUES (1, 1) "
                "ON CONFLICT(id) DO UPDATE SET generation = COALESCE(generation, 0) + 1"
            )
        )


def append_ledger(
    object_type: str,
    object_id: Any,
    action: str,
    payload: Optional[Dict[str, Any]],
) -> LedgerEntry:
    """Append a hash-chained ledger entry **inside the current transaction**.

    The chain head is read from the same transaction, under the chain lock, so it
    observes every entry committed before it and no two entries can share a
    ``prev_hash``. Because the caller is inside :func:`atomic`, the entry shares
    the fate of the accounting mutation: rolled back together on failure,
    committed together on success.

    ``db.session.flush()`` is deliberate -- it surfaces a constraint violation
    here, while the caller can still roll the accounting mutation back, instead
    of at commit time after the damage is done.
    """
    payload_text = json.dumps(
        payload or {}, ensure_ascii=False, sort_keys=True, default=str
    )
    _lock_chain_head()
    head = (
        db.session.query(LedgerEntry.id, LedgerEntry.hash)
        .order_by(LedgerEntry.id.desc())
        .first()
    )
    prev_hash = head.hash if head else None
    # The hashed timestamp and the stored timestamp MUST be the same value, or
    # verify_ledger_chain() could never reproduce the digest.
    now = datetime.utcnow()

    entry = LedgerEntry(
        created_at=now,
        object_type=str(object_type or "unknown"),
        object_id=str(object_id) if object_id is not None else None,
        action=str(action or "unknown"),
        payload=payload_text,
        prev_hash=prev_hash,
        hash=compute_entry_hash(
            prev_hash, payload_text, now.isoformat(timespec="microseconds")
        ),
    )
    db.session.add(entry)
    db.session.flush()
    return entry


def verify_ledger_chain(limit: Optional[int] = None) -> Dict[str, Any]:
    """Walk the chain and report the first break, if any.

    Used by tests and by the admin ledger view to prove the audit trail is
    intact rather than trusting it.
    """
    query = db.session.query(LedgerEntry).order_by(LedgerEntry.id.asc())
    if limit:
        query = query.limit(int(limit))
    expected_prev: Optional[str] = None
    checked = 0
    for entry in query.all():
        if (entry.prev_hash or None) != expected_prev:
            return {
                "ok": False,
                "checked": checked,
                "broken_at": entry.id,
                "reason": "prev_hash_mismatch",
            }
        expected_hash = compute_entry_hash(
            entry.prev_hash,
            entry.payload or "",
            entry.created_at.isoformat(timespec="microseconds"),
        )
        # The stored hash commits to the timestamp it was created with; the ORM
        # round-trips it at microsecond resolution, which is what we hashed.
        if entry.hash != expected_hash:
            return {
                "ok": False,
                "checked": checked,
                "broken_at": entry.id,
                "reason": "payload_hash_mismatch",
            }
        expected_prev = entry.hash
        checked += 1
    return {"ok": True, "checked": checked, "broken_at": None, "reason": None}


# --------------------------------------------------------------------------- #
# Denormalized column writers (atomic SQL read-modify-write)
# --------------------------------------------------------------------------- #


def adjust_item_stock_delta(
    item: Entity, delta: float, allow_negative: bool = False
) -> None:
    """Change an item's stock by *delta* inside the current transaction.

    The update is a single SQL statement (``col = col + delta``) so concurrent
    movements cannot lose each other. For stock-out movements the same
    statement carries the non-negative guard, so the check and the write are
    one indivisible step.
    """
    delta = _num(delta)
    if abs(delta) < EPS:
        return
    item_id = getattr(item, "id", None)
    if item_id is None:
        new_value = _num(getattr(item, "stock_qty", 0.0)) + delta
        if new_value < -EPS and not allow_negative:
            raise InsufficientStockError(
                "موجودی کالای «{}» کافی نیست.".format(getattr(item, "name", "نامشخص"))
            )
        item.stock_qty = new_value
        return

    name = getattr(item, "name", "نامشخص")
    if delta < 0 and not allow_negative:
        result = db.session.execute(
            text(
                "UPDATE entities "
                "SET stock_qty = COALESCE(stock_qty, 0) + :delta "
                "WHERE id = :id AND type = 'item' "
                "AND COALESCE(stock_qty, 0) + :delta >= 0"
            ),
            {"delta": delta, "id": int(item_id)},
        )
    else:
        result = db.session.execute(
            text(
                "UPDATE entities "
                "SET stock_qty = COALESCE(stock_qty, 0) + :delta "
                "WHERE id = :id AND type = 'item'"
            ),
            {"delta": delta, "id": int(item_id)},
        )

    if result.rowcount != 1:
        raise InsufficientStockError("موجودی کالای «{}» کافی نیست.".format(name))
    db.session.refresh(item, attribute_names=["stock_qty"])


def adjust_person_balance_delta(person: Entity, delta: float) -> None:
    """Change a party's denormalized balance inside the current transaction."""
    delta = _num(delta)
    if abs(delta) < EPS:
        return
    person_id = getattr(person, "id", None)
    if person_id is None:
        person.balance = _num(getattr(person, "balance", 0.0)) + delta
        return

    result = db.session.execute(
        text(
            "UPDATE entities "
            "SET balance = COALESCE(balance, 0) + :delta "
            "WHERE id = :id AND type = 'person'"
        ),
        {"delta": delta, "id": int(person_id)},
    )
    if result.rowcount != 1:
        raise AccountingError("طرف حساب برای به‌روزرسانی مانده پیدا نشد.")
    db.session.refresh(person, attribute_names=["balance"])


def adjust_cash_person_balance(
    doc_type: str, person: Entity, old_amount: float, new_amount: float
) -> None:
    """Re-price a cash document's effect on the party's balance.

    Expressed as a delta between the old and the new amount so that editing a
    document only ever moves the balance by the actual difference.
    """
    delta = _num(new_amount) - _num(old_amount)
    adjust_person_balance_delta(person, cash_balance_sign(doc_type) * delta)


# --------------------------------------------------------------------------- #
# Lifecycle state claims (idempotency guards)
# --------------------------------------------------------------------------- #


def _claim_void(document: Any, reason: Optional[str]) -> bool:
    """Atomically flip ``active -> void``. Returns False if already void.

    The conditional UPDATE is what makes reversal idempotent and safe under
    concurrency: two simultaneous cancel requests cannot both win, because only
    one of them matches the ``status = 'active'`` predicate. The loser gets
    ``False`` and raises :class:`DocumentStateError`; it never double-reverses.
    """
    table = type(document).__tablename__
    result = db.session.execute(
        text(
            "UPDATE {table} "
            "SET status = :void, voided_at = :voided_at, void_reason = :reason "
            "WHERE id = :id AND (status IS NULL OR status = :active)".format(
                table=table
            )
        ),
        {
            "void": STATUS_VOID,
            "active": STATUS_ACTIVE,
            "voided_at": datetime.utcnow(),
            "reason": (reason or None),
            "id": int(document.id),
        },
    )
    claimed = result.rowcount == 1
    if claimed:
        db.session.refresh(
            document, attribute_names=["status", "voided_at", "void_reason"]
        )
    return claimed


# --------------------------------------------------------------------------- #
# Invoice: create
# --------------------------------------------------------------------------- #


def _touch_price_history(person_id: int, item_id: int, unit_price: float) -> None:
    row = (
        db.session.query(PriceHistory)
        .filter(PriceHistory.person_id == person_id, PriceHistory.item_id == item_id)
        .first()
    )
    if row is None:
        db.session.add(
            PriceHistory(
                person_id=person_id, item_id=item_id, last_price=_num(unit_price)
            )
        )
    else:
        row.last_price = _num(unit_price)


def post_invoice(
    *,
    kind: str,
    number: str,
    doc_date,
    person: Entity,
    lines: Sequence[Dict[str, Any]],
    discount: float = 0.0,
    tax: float = 0.0,
    allow_negative_sales: bool = False,
    ledger_payload: Optional[Dict[str, Any]] = None,
) -> Invoice:
    """Create an invoice and apply its stock/balance effects atomically.

    ``lines`` is a sequence of ``{"item": Entity, "qty": float,
    "unit_price": float}`` mappings. Quantity and price are validated here, so
    every caller gets identical behaviour.
    """
    kind = normalize_invoice_kind(kind)
    if person is None or getattr(person, "type", None) != "person":
        raise DocumentValidationError("طرف حساب فاکتور معتبر نیست.")
    if not number or not str(number).strip():
        raise DocumentValidationError("شماره فاکتور مشخص نشده است.")

    normalized: List[Tuple[Entity, float, float]] = []
    total = 0.0
    for raw in lines or []:
        item = raw.get("item")
        qty = _num(raw.get("qty"))
        unit_price = _num(raw.get("unit_price"))
        if item is None or getattr(item, "type", None) != "item":
            continue
        if qty <= 0:
            continue
        if unit_price < 0:
            raise DocumentValidationError("قیمت واحد کالا نمی‌تواند منفی باشد.")
        normalized.append((item, qty, unit_price))
        total += qty * unit_price

    if not normalized:
        raise DocumentValidationError("هیچ ردیف کالایی معتبر نیست.")

    total = total - _num(discount) + _num(tax)
    if total <= 0:
        raise DocumentValidationError("جمع کل فاکتور باید بزرگ‌تر از صفر باشد.")

    with atomic():
        invoice = Invoice(
            number=str(number).strip(),
            date=doc_date,
            person_id=person.id,
            kind=kind,
            discount=_num(discount),
            tax=_num(tax),
            total=total,
            status=STATUS_ACTIVE,
        )
        db.session.add(invoice)
        db.session.flush()

        stock_sign = invoice_stock_sign(kind)
        balance_sign = invoice_balance_sign(kind)

        for item, qty, unit_price in normalized:
            db.session.add(
                InvoiceLine(
                    invoice_id=invoice.id,
                    item_id=item.id,
                    qty=qty,
                    unit_price=unit_price,
                    line_total=qty * unit_price,
                )
            )
            # Purchase stock-in is always allowed; sales honour the negative
            # stock policy. The guard lives inside the UPDATE statement, so the
            # check and the write cannot disagree under concurrency.
            adjust_item_stock_delta(
                item,
                stock_sign * qty,
                allow_negative=(allow_negative_sales or kind == "purchase"),
            )
            if kind == "sales":
                _touch_price_history(person.id, item.id, unit_price)

        adjust_person_balance_delta(person, balance_sign * total)

        payload = {
            "number": invoice.number,
            "kind": kind,
            "total": total,
            "person_id": person.id,
            "date": (
                doc_date.isoformat()
                if hasattr(doc_date, "isoformat")
                else str(doc_date)
            ),
            "lines": [
                {"item_id": int(i.id), "qty": q, "unit_price": up}
                for i, q, up in normalized
            ],
            "stock_delta": {str(i.id): stock_sign * q for i, q, _ in normalized},
            "person_balance_delta": balance_sign * total,
        }
        if ledger_payload:
            payload.update(ledger_payload)
        append_ledger("invoice", invoice.id, LEDGER_CREATE, payload)

    return invoice


# --------------------------------------------------------------------------- #
# Invoice: re-price (edit)
# --------------------------------------------------------------------------- #


def reprice_invoice(
    invoice: Invoice,
    *,
    lines: Sequence[Dict[str, Any]],
    person: Optional[Entity] = None,
    doc_date=None,
    discount: Optional[float] = None,
    tax: Optional[float] = None,
    allow_negative_sales: bool = False,
    ledger_payload: Optional[Dict[str, Any]] = None,
) -> Invoice:
    """Re-price an existing invoice as the **net** difference of its effects.

    Rather than reversing the old posting and applying the new one, this
    computes a per-item net quantity delta and a net balance delta and applies
    each exactly once. That has three consequences that matter:

    * an item that stays in the invoice but changes quantity moves stock by the
      difference only;
    * an item removed from the invoice gets its original stock back exactly
      once;
    * the intermediate state never transiently violates the negative-stock
      policy, so an edit that is valid end-to-end is not rejected halfway.
    """
    if is_void(invoice):
        raise DocumentStateError("فاکتور ابطال‌شده قابل ویرایش نیست.")

    kind = normalize_invoice_kind(invoice.kind or "sales")
    new_person = person if person is not None else invoice.person
    if new_person is None or getattr(new_person, "type", None) != "person":
        raise DocumentValidationError("طرف حساب فاکتور معتبر نیست.")

    normalized: List[Tuple[Entity, float, float]] = []
    new_subtotal = 0.0
    for raw in lines or []:
        item = raw.get("item")
        qty = _num(raw.get("qty"))
        unit_price = _num(raw.get("unit_price"))
        if item is None or getattr(item, "type", None) != "item":
            continue
        if qty <= 0:
            continue
        if unit_price < 0:
            raise DocumentValidationError("قیمت واحد کالا نمی‌تواند منفی باشد.")
        normalized.append((item, qty, unit_price))
        new_subtotal += qty * unit_price

    if not normalized:
        raise DocumentValidationError("حداقل یک ردیف کالای معتبر با تعداد وارد کنید.")

    new_discount = invoice.discount if discount is None else _num(discount)
    new_tax = invoice.tax if tax is None else _num(tax)
    new_total = new_subtotal - new_discount + new_tax
    if new_total <= 0:
        raise DocumentValidationError("جمع کل فاکتور باید بزرگ‌تر از صفر باشد.")

    stock_sign = invoice_stock_sign(kind)
    balance_sign = invoice_balance_sign(kind)
    old_person = invoice.person
    old_total = _num(invoice.total)
    # Captured before the mutation below, for the same reason as the cash
    # document path: reading invoice.date afterwards would always yield the new
    # value and the audit trail would claim the date never moved.
    old_date = invoice.date
    new_date = doc_date if doc_date is not None else old_date
    date_changed = new_date != old_date

    # Net quantity per item: new lines minus the lines currently on the invoice.
    net_qty: Dict[int, float] = {}
    item_by_id: Dict[int, Entity] = {}
    for item, qty, _unit_price in normalized:
        net_qty[item.id] = net_qty.get(item.id, 0.0) + qty
        item_by_id[item.id] = item
    old_lines = list(invoice.lines or [])
    for line in old_lines:
        net_qty[line.item_id] = net_qty.get(line.item_id, 0.0) - _num(line.qty)
        if line.item_id not in item_by_id:
            item_by_id[line.item_id] = line.item

    with atomic():
        # 1) Stock: one guarded statement per item, carrying the net delta.
        for item_id, qty_delta in net_qty.items():
            if abs(qty_delta) < EPS:
                continue
            item = item_by_id.get(item_id)
            if item is None:
                raise AccountingError("کالای ردیف فاکتور یافت نشد.")
            adjust_item_stock_delta(
                item,
                stock_sign * qty_delta,
                allow_negative=(allow_negative_sales or kind == "purchase"),
            )

        # 2) Balance: unwind the old posting and apply the new one. When the
        #    counterparty changed these land on two different rows, which is
        #    exactly what "edit the party" must do.
        if old_person is not None and old_person.id == new_person.id:
            delta = balance_sign * (new_total - old_total)
            adjust_person_balance_delta(new_person, delta)
        else:
            if old_person is not None:
                adjust_person_balance_delta(old_person, -balance_sign * old_total)
            adjust_person_balance_delta(new_person, balance_sign * new_total)

        # 3) Rewrite the document rows. Assigning the new collection lets the
        #    ``delete-orphan`` cascade remove the previous lines; doing it this
        #    way keeps a single owner of the rows and avoids a second, racing
        #    DELETE statement for the same line.
        invoice.lines = [
            InvoiceLine(
                item_id=item.id,
                qty=qty,
                unit_price=unit_price,
                line_total=qty * unit_price,
            )
            for item, qty, unit_price in normalized
        ]
        invoice.total = new_total
        # Bump the CAS token so a second operator holding this same form is told
        # their copy is stale instead of silently overwriting this change.
        invoice.revision = int(invoice.revision or 0) + 1
        invoice.discount = new_discount
        invoice.tax = new_tax
        if doc_date is not None:
            invoice.date = doc_date
        if new_person.id != invoice.person_id:
            invoice.person_id = new_person.id
        db.session.flush()

        # 4) Denormalized last-price cache follows the new counterparty.
        if kind == "sales":
            for item, _qty, unit_price in normalized:
                _touch_price_history(new_person.id, item.id, unit_price)

        payload = {
            "number": invoice.number,
            "kind": kind,
            "total_before": old_total,
            "total_after": new_total,
            "person_before": old_person.id if old_person is not None else None,
            "person_after": new_person.id,
            "date_before": str(old_date) if date_changed else None,
            "date_after": str(new_date) if date_changed else None,
            "stock_delta": {
                str(k): stock_sign * v for k, v in net_qty.items() if abs(v) >= EPS
            },
            "person_balance_delta": balance_sign * (new_total - old_total),
            "lines": [
                {"item_id": int(i.id), "qty": q, "unit_price": up}
                for i, q, up in normalized
            ],
        }
        if ledger_payload:
            payload.update(ledger_payload)
        append_ledger("invoice", invoice.id, LEDGER_UPDATE, payload)

    return invoice


# --------------------------------------------------------------------------- #
# Invoice: reverse (cancel) and purge (delete)
# --------------------------------------------------------------------------- #


def reverse_invoice_posting(
    invoice: Invoice,
    *,
    reason: Optional[str] = None,
    allow_stock_shortage: bool = False,
    ledger_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Cancel an invoice: undo stock and balance exactly once, then void it.

    The status claim happens *first* and is conditional, so:

    * a second cancel/delete of the same document cannot reverse anything again;
    * if any later step fails, the claim is rolled back with everything else,
      leaving the document fully active and fully applied -- never half-reversed.
    """
    if invoice is None:
        raise DocumentValidationError("فاکتور یافت نشد.")
    if is_void(invoice):
        raise DocumentStateError("این فاکتور قبلاً ابطال شده است.")

    kind = normalize_invoice_kind(invoice.kind or "sales")
    stock_sign = invoice_stock_sign(kind)
    balance_sign = invoice_balance_sign(kind)
    total = _num(invoice.total)
    lines = list(invoice.lines or [])
    person = invoice.person

    with atomic():
        if not _claim_void(invoice, reason):
            raise DocumentStateError("این فاکتور قبلاً ابطال شده است.")

        stock_delta: Dict[str, float] = {}
        for line in lines:
            qty = _num(line.qty)
            if abs(qty) < EPS:
                continue
            item = line.item
            if item is None:
                raise AccountingError("کالای ردیف فاکتور یافت نشد.")
            # Undo the original movement. A sales cancellation puts stock back
            # (always safe). A purchase cancellation removes the stock that was
            # added, so it must still be present -- we refuse to fabricate
            # inventory that a later sale already consumed.
            adjust_item_stock_delta(
                item,
                -stock_sign * qty,
                allow_negative=(allow_stock_shortage or kind == "sales"),
            )
            stock_delta[str(item.id)] = -stock_sign * qty

        balance_delta = -balance_sign * total
        if person is not None:
            adjust_person_balance_delta(person, balance_delta)

        payload = {
            "number": invoice.number,
            "kind": kind,
            "total": total,
            "person_id": person.id if person is not None else None,
            "reason": reason,
            "stock_delta": stock_delta,
            "person_balance_delta": balance_delta,
        }
        if ledger_payload:
            payload.update(ledger_payload)
        append_ledger("invoice", invoice.id, LEDGER_VOID, payload)

    return {
        "invoice_id": invoice.id,
        "kind": kind,
        "total": total,
        "stock_delta": stock_delta,
        "person_balance_delta": balance_delta,
    }


def void_invoice(
    invoice: Invoice, *, reason: Optional[str] = None, **kwargs
) -> Dict[str, Any]:
    """Logical cancel: keep the document for audit, remove its accounting effect."""
    return reverse_invoice_posting(invoice, reason=reason, **kwargs)


def delete_invoice(
    invoice: Invoice,
    *,
    reason: Optional[str] = None,
    allow_stock_shortage: bool = False,
    ledger_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Hard delete: reverse the posting, then purge the document and its lines.

    Hard delete and cancel share :func:`reverse_invoice_posting`, so a document
    can never be unwound through two different semantic paths. The only
    difference is whether the rows survive for audit.
    """
    if invoice is None:
        raise DocumentValidationError("فاکتور یافت نشد.")

    number = invoice.number
    kind = invoice.kind
    total = _num(invoice.total)
    invoice_id = invoice.id

    with atomic():
        already_void = is_void(invoice)
        reversal: Dict[str, Any]
        if already_void:
            # A voided document has no accounting effect left to unwind; purging
            # it must not touch stock or balances a second time.
            reversal = {
                "invoice_id": invoice_id,
                "kind": kind,
                "total": total,
                "stock_delta": {},
                "person_balance_delta": 0.0,
                "already_void": True,
            }
        else:
            reversal = reverse_invoice_posting(
                invoice, reason=reason, allow_stock_shortage=allow_stock_shortage
            )

        # The ``delete-orphan`` cascade removes the lines together with the
        # invoice, so the rows are deleted exactly once.
        db.session.delete(invoice)
        db.session.flush()

        append_ledger(
            "invoice",
            invoice_id,
            LEDGER_DELETE,
            {
                "number": number,
                "kind": kind,
                "total": total,
                "reason": reason,
                "already_void": already_void,
                **({"reversal": reversal} if reversal else {}),
                **(ledger_payload or {}),
            },
        )

    return reversal


# --------------------------------------------------------------------------- #
# Cash documents: create
# --------------------------------------------------------------------------- #

CASH_METHODS = ("pos", "cash", "bank", "cheque")


def normalize_cash_method(method: Optional[str]) -> str:
    """Whitelist the payment method. Anything unknown becomes plain cash."""
    value = (method or "").strip().lower()
    return value if value in CASH_METHODS else "cash"


def normalize_cheque_fields(
    method: Optional[str],
    *,
    cheque_number: Optional[str] = None,
    cheque_bank: Optional[str] = None,
    cheque_branch: Optional[str] = None,
    cheque_account: Optional[str] = None,
    cheque_owner: Optional[str] = None,
    cheque_due_date=None,
) -> Dict[str, Any]:
    """Return cheque metadata, or empty values when the method is not a cheque.

    Cheque details on a non-cheque document are meaningless and would corrupt
    the "upcoming cheques" report, so they are always cleared here rather than
    at each individual call site.
    """
    if normalize_cash_method(method) != "cheque":
        return {
            "cheque_number": None,
            "cheque_bank": None,
            "cheque_branch": None,
            "cheque_account": None,
            "cheque_owner": None,
            "cheque_due_date": None,
        }
    number = "".join(ch for ch in (cheque_number or "") if ch.isdigit())
    return {
        "cheque_number": number or None,
        "cheque_bank": (cheque_bank or "").strip() or None,
        "cheque_branch": (cheque_branch or "").strip() or None,
        "cheque_account": (cheque_account or "").strip() or None,
        "cheque_owner": (cheque_owner or "").strip() or None,
        "cheque_due_date": cheque_due_date,
    }


def post_cashdoc(
    *,
    doc_type: str,
    number: Optional[str],
    doc_date,
    person: Entity,
    amount: float,
    method: Optional[str] = None,
    note: Optional[str] = None,
    cashbox=None,
    cheque_number: Optional[str] = None,
    cheque_bank: Optional[str] = None,
    cheque_branch: Optional[str] = None,
    cheque_account: Optional[str] = None,
    cheque_owner: Optional[str] = None,
    cheque_due_date=None,
    ledger_payload: Optional[Dict[str, Any]] = None,
) -> CashDoc:
    """Create a receive/payment document and post its effect atomically.

    The party's balance moves by the cash sign convention and the ledger entry
    lands in the same transaction as the row.
    """
    doc_type = normalize_cash_doc_type(doc_type)
    if person is None or getattr(person, "type", None) != "person":
        raise DocumentValidationError("طرف حساب سند معتبر نیست.")

    amount = _num(amount)
    if amount <= 0:
        raise DocumentValidationError("مبلغ باید بزرگ‌تر از صفر باشد.")

    method = normalize_cash_method(method)
    cheque = normalize_cheque_fields(
        method,
        cheque_number=cheque_number,
        cheque_bank=cheque_bank,
        cheque_branch=cheque_branch,
        cheque_account=cheque_account,
        cheque_owner=cheque_owner,
        cheque_due_date=cheque_due_date,
    )
    if method == "cheque" and cashbox is None:
        raise DocumentValidationError("برای ثبت چک، یک حساب بانکی فعال انتخاب کنید.")
    if method == "cheque" and getattr(cashbox, "kind", None) != "bank":
        raise DocumentValidationError("برای ثبت چک، یک حساب بانکی فعال انتخاب کنید.")
    if method == "cheque" and not cheque["cheque_number"]:
        raise DocumentValidationError("شماره صیادی چک الزامی است.")
    # Enforced here, not only in the web form, so the assistant and every other
    # transport are held to the same rule: a cheque number that is not a 16-digit
    # Sayyad number cannot be presented and would poison the cheques report.
    if method == "cheque" and len(cheque["cheque_number"] or "") != 16:
        raise DocumentValidationError("شماره صیادی چک باید ۱۶ رقم باشد.")

    resolved_number = str(number).strip() if number else None
    if not resolved_number:
        raise DocumentValidationError("شماره سند مشخص نشده است.")

    with atomic():
        doc = CashDoc(
            doc_type=doc_type,
            number=resolved_number,
            date=doc_date,
            person_id=person.id,
            amount=amount,
            method=method,
            note=(note or "").strip() or None,
            cashbox_id=getattr(cashbox, "id", None) if cashbox is not None else None,
            status=STATUS_ACTIVE,
            **cheque,
        )
        db.session.add(doc)
        db.session.flush()

        adjust_cash_person_balance(doc_type, person, 0.0, amount)

        payload = {
            "doc_type": doc_type,
            "number": doc.number,
            "amount": amount,
            "person_id": person.id,
            "cashbox_id": doc.cashbox_id,
            "method": method,
            "date": (
                doc_date.isoformat()
                if hasattr(doc_date, "isoformat")
                else str(doc_date)
            ),
            "person_balance_delta": cash_balance_sign(doc_type) * amount,
        }
        if ledger_payload:
            payload.update(ledger_payload)
        append_ledger("cashdoc", doc.id, LEDGER_CREATE, payload)

    return doc


# --------------------------------------------------------------------------- #
# Cash documents: re-price (edit)
# --------------------------------------------------------------------------- #


def reprice_cashdoc(
    doc: CashDoc,
    *,
    amount: float,
    person: Optional[Entity] = None,
    doc_date=None,
    note: Optional[str] = None,
    ledger_payload: Optional[Dict[str, Any]] = None,
) -> CashDoc:
    """Edit a cash document, moving every affected balance by its exact delta.

    Editable: the amount, the counterparty and the document date.

    * amount -- the party's balance moves by ``sign * (new - old)``;
    * person -- the old party is unwound completely and the new party is
      applied completely, so the two effects can never be added up wrongly and
      a half-finished reassignment cannot survive a failure;
    * date -- a period attribute only; it changes no balance, stock or cashbox
      figure, so it is applied alongside the rest in the same transaction.

    Deliberately **not** editable: the document type, the payment method and
    the cashbox. The type would flip the sign of every effect and the cashbox
    decides which account the money is booked against; both must be voided and
    re-posted instead.
    """
    if is_void(doc):
        raise DocumentStateError("سند ابطال‌شده قابل ویرایش نیست.")
    doc_type = normalize_cash_doc_type(doc.doc_type)
    current_person = doc.person
    if current_person is None:
        raise DocumentValidationError("طرف حساب سند یافت نشد.")

    new_person = person if person is not None else current_person
    if getattr(new_person, "id", None) is None:
        raise DocumentValidationError("طرف حساب جدید یافت نشد.")
    if getattr(new_person, "type", "person") != "person":
        raise DocumentValidationError("طرف حساب باید از نوع شخص باشد.")

    new_amount = _num(amount)
    if new_amount <= 0:
        raise DocumentValidationError("مبلغ سند باید بزرگ‌تر از صفر باشد.")
    old_amount = _num(doc.amount)

    new_date = doc_date if doc_date is not None else doc.date
    if new_date is None:
        raise DocumentValidationError("تاریخ سند یافت نشد.")

    person_changed = new_person.id != current_person.id
    old_date = doc.date
    date_changed = new_date != old_date
    with atomic():
        if person_changed:
            # Unwind the old counterparty and book the new one. Both halves are
            # in this transaction, so a failure cannot leave the amount booked
            # against neither party.
            adjust_cash_person_balance(doc_type, current_person, old_amount, 0.0)
            adjust_cash_person_balance(doc_type, new_person, 0.0, new_amount)
            doc.person_id = new_person.id
        else:
            adjust_cash_person_balance(doc_type, current_person, old_amount, new_amount)

        doc.amount = new_amount
        doc.revision = int(doc.revision or 0) + 1
        doc.date = new_date
        if note is not None:
            doc.note = (note or "").strip() or None
        db.session.flush()

        payload = {
            "doc_type": doc_type,
            "number": doc.number,
            "person_id": new_person.id,
            "previous_person_id": current_person.id if person_changed else None,
            "cashbox_id": doc.cashbox_id,
            "method": doc.method,
            "amount_before": old_amount,
            "amount_after": new_amount,
            "date_before": str(old_date) if date_changed else None,
            "date_after": str(new_date) if date_changed else None,
            "person_balance_delta": (
                cash_balance_sign(doc_type) * (new_amount - old_amount)
                if not person_changed
                else 0.0
            ),
        }
        if ledger_payload:
            payload.update(ledger_payload)
        append_ledger("cashdoc", doc.id, LEDGER_UPDATE, payload)

    return doc


def reverse_cashdoc_posting(
    doc: CashDoc,
    *,
    reason: Optional[str] = None,
    ledger_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Cancel a cash document: unwind its effect on the party's balance once.

    Cashbox totals are *derived* (``SUM(amount)`` per cashbox), so voiding the
    document is what removes it from every cashbox balance -- there is no second
    cached number to keep in sync.
    """
    if doc is None:
        raise DocumentValidationError("سند یافت نشد.")
    if is_void(doc):
        raise DocumentStateError("این سند قبلاً ابطال شده است.")

    doc_type = normalize_cash_doc_type(doc.doc_type)
    amount = _num(doc.amount)
    person = doc.person

    with atomic():
        if not _claim_void(doc, reason):
            raise DocumentStateError("این سند قبلاً ابطال شده است.")
        balance_delta = 0.0
        if person is not None:
            # Unwind exactly what posting applied: delta = 0 - amount.
            adjust_cash_person_balance(doc_type, person, amount, 0.0)
            balance_delta = cash_balance_sign(doc_type) * (0.0 - amount)

        append_ledger(
            "cashdoc",
            doc.id,
            LEDGER_VOID,
            {
                "doc_type": doc_type,
                "number": doc.number,
                "person_id": person.id if person is not None else None,
                "cashbox_id": doc.cashbox_id,
                "method": doc.method,
                "amount": amount,
                "person_balance_delta": balance_delta,
                "reason": reason,
                **(ledger_payload or {}),
            },
        )

    return {
        "doc_id": doc.id,
        "doc_type": doc_type,
        "amount": amount,
        "person_balance_delta": balance_delta,
    }


def void_cashdoc(
    doc: CashDoc, *, reason: Optional[str] = None, **kwargs
) -> Dict[str, Any]:
    """Logical cancel: keep the document for audit, remove its accounting effect."""
    return reverse_cashdoc_posting(doc, reason=reason, **kwargs)


def delete_cashdoc(
    doc: CashDoc,
    *,
    reason: Optional[str] = None,
    ledger_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Hard delete: reverse the posting, then purge the row."""
    if doc is None:
        raise DocumentValidationError("سند یافت نشد.")

    doc_type = doc.doc_type
    amount = _num(doc.amount)
    number = doc.number
    doc_id = doc.id

    with atomic():
        already_void = is_void(doc)
        reversal: Dict[str, Any]
        if already_void:
            reversal = {
                "doc_id": doc_id,
                "doc_type": doc_type,
                "amount": amount,
                "person_balance_delta": 0.0,
                "already_void": True,
            }
        else:
            reversal = reverse_cashdoc_posting(doc, reason=reason)

        db.session.delete(doc)
        db.session.flush()

        append_ledger(
            "cashdoc",
            doc_id,
            LEDGER_DELETE,
            {
                "doc_type": doc_type,
                "number": number,
                "amount": amount,
                "reason": reason,
                "already_void": already_void,
                "reversal": reversal,
                **(ledger_payload or {}),
            },
        )

    return reversal


# --------------------------------------------------------------------------- #
# Derived balances (cashbox / dashboard) -- void-aware by construction
# --------------------------------------------------------------------------- #


def signed_cash_amount(doc_type):
    """SQL expression for a cash document's contribution to a cashbox balance.

    Receives add to the box, payments take from it, so the sign can be applied
    inside the aggregate instead of being summed in Python afterwards.
    """
    return case((CashDoc.doc_type == "payment", -CashDoc.amount), else_=CashDoc.amount)


def apply_void_filter(query, model):
    """Restrict an arbitrary query to documents that still have accounting effect.

    Every aggregate in the app (dashboard, reports, cashbox totals, upcoming
    cheques) must go through this so a voided document can never leak back into a
    balance. ``status IS NULL`` is accepted because rows written before the
    lifecycle column existed have no value stored.
    """
    return query.filter((model.status.is_(None)) | (model.status == STATUS_ACTIVE))


def lifecycle_html(document: Any) -> Markup:
    """Void banner for a document view page, as ready-to-render markup.

    Returns empty markup for an active document, so a template can print it
    unconditionally. The void reason is user input, so it is escaped here -- once,
    in the one place that renders it -- and the result is wrapped in ``Markup`` so
    a template variable does not escape our own tags a second time.
    """
    if not is_void(document):
        return Markup("")
    from markupsafe import escape

    parts = [
        "<b>سابقه ابطال:</b>",
        f"<br><b>تاریخ ابطال:</b> {escape(_fmt_ts(getattr(document, 'voided_at', None)))}",
    ]
    reason = (getattr(document, "void_reason", None) or "").strip()
    if reason:
        parts.append(f"<br><b>دلیل:</b> {escape(reason)}")
    return Markup('<div class="doc-lifecycle is-void no-print">'
                  + "".join(parts) + "</div>")


def _fmt_ts(value: Any) -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def active_cashdocs_query(query):
    """Apply the void filter to an existing CashDoc query."""
    return apply_void_filter(query, CashDoc)


def active_invoices_query(query):
    """Apply the void filter to an existing Invoice query."""
    return apply_void_filter(query, Invoice)


def cashbox_balance(cashbox_id: Optional[int]) -> float:
    """Net cash position of a cashbox: receives minus payments, voids excluded."""
    if cashbox_id is None:
        return 0.0
    query = db.session.query(
        func.coalesce(func.sum(signed_cash_amount(CashDoc.doc_type)), 0.0)
    ).filter(CashDoc.cashbox_id == int(cashbox_id))
    return float(active_cashdocs_query(query).scalar() or 0.0)


def party_balance_recomputed(person_id: int) -> float:
    """Balance of a party recomputed from source documents, for reconciliation.

    Tests and the admin UI use this to prove the denormalized ``balance`` column
    still matches the documents it was derived from.
    """

    def _sum_doc_total(model, **filters):
        query = db.session.query(func.coalesce(func.sum(model.total), 0.0)).filter_by(
            **filters
        )
        return float(apply_void_filter(query, model).scalar() or 0.0)

    def _sum_cash(**filters):
        query = db.session.query(
            func.coalesce(func.sum(CashDoc.amount), 0.0)
        ).filter_by(**filters)
        return float(active_cashdocs_query(query).scalar() or 0.0)

    sales = _sum_doc_total(Invoice, person_id=int(person_id), kind="sales")
    purchase = _sum_doc_total(Invoice, person_id=int(person_id), kind="purchase")
    receive = _sum_cash(person_id=int(person_id), doc_type="receive")
    payment = _sum_cash(person_id=int(person_id), doc_type="payment")
    # matches invoice_balance_sign / cash_balance_sign
    return sales - purchase - receive + payment
