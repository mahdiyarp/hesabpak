# -*- coding: utf-8 -*-
"""Accounting domain models.

These models were previously defined inline in ``app.py``. They now live in a
dedicated module so that :mod:`utils.accounting` -- the single owner of
apply/reverse/edit-delta semantics -- can import them without a circular
dependency on the Flask application module.

The table names, columns and relationships are unchanged, so existing databases
and the existing ``db.create_all()`` bootstrap keep working as before.
"""

from datetime import datetime

from extensions import db


# ---------------------------------------------------------------- ledger ----
class LedgerEntry(db.Model):
    """Append-only, hash-chained audit entry.

    Entries are inserted through :func:`utils.accounting.append_ledger` inside
    the *same* transaction as the accounting mutation they describe, so an
    entry can never claim a success that was rolled back, and a committed
    mutation can never silently lack its audit trail.
    """

    __tablename__ = "ledger_entries"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, index=True
    )
    object_type = db.Column(db.String(64), nullable=False)
    object_id = db.Column(db.String(64), nullable=True)
    action = db.Column(db.String(64), nullable=False)
    payload = db.Column(db.Text, nullable=True)
    prev_hash = db.Column(db.String(128), nullable=True)
    hash = db.Column(db.String(128), nullable=False, unique=True, index=True)


class LedgerChain(db.Model):
    """Single-row lock row that serialises ledger chain appends.

    Reading the chain head and inserting the next entry must not interleave.
    ``append_ledger`` bumps ``generation`` on this row *before* reading the head:
    on SQLite that write acquires the database's RESERVED lock, so a competing
    transaction either has not started (and sees the committed head) or waits and
    then sees the head this transaction committed. On PostgreSQL/MySQL the same
    ``UPDATE`` takes a row lock with the same effect.

    Without this, two concurrent appends could both read the same head and emit
    two entries sharing one ``prev_hash``, forking the chain -- which is exactly
    the "concurrent ledger append protection" invariant.
    """

    __tablename__ = "ledger_chain"
    id = db.Column(db.Integer, primary_key=True)
    generation = db.Column(db.Integer, nullable=False, default=0)


# ------------------------------------------------------------- chart of accounts ----
class Account(db.Model):
    __tablename__ = "accounts"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(16), nullable=False, unique=True, index=True)
    name = db.Column(db.String(255), nullable=False)
    level = db.Column(
        db.Integer, nullable=False, default=1
    )  # 1:3digit, 2:6digit, 3:9digit
    parent_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    locked = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    parent = db.relationship("Account", remote_side=[id], lazy="joined")


# ------------------------------------------------------- parties and stock ----
class Entity(db.Model):
    """A person (receivable/payable balance) or an item (stock quantity)."""

    __tablename__ = "entities"
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(16), nullable=False)  # person / item
    code = db.Column(db.String(16), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    unit = db.Column(
        db.String(64), nullable=True
    )  # person: title/company/..., item: unit
    serial_no = db.Column(db.String(255), nullable=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("entities.id"), nullable=True)
    level = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )
    stock_qty = db.Column(
        db.Float, nullable=False, default=0.0
    )  # meaningful for type=item
    balance = db.Column(
        db.Float, nullable=False, default=0.0
    )  # meaningful for type=person

    parent = db.relationship("Entity", remote_side=[id], lazy="joined")
    __table_args__ = (db.UniqueConstraint("type", "code", name="uq_entity_type_code"),)


# ---------------------------------------------------------------- invoices ----
class Invoice(db.Model):
    __tablename__ = "invoices"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(32), nullable=False, unique=True)
    date = db.Column(db.Date, nullable=False)
    person_id = db.Column(
        db.Integer, db.ForeignKey("entities.id"), nullable=False
    )  # type=person
    kind = db.Column(db.String(16), nullable=False, default="sales")  # sales | purchase
    discount = db.Column(db.Float, nullable=False, default=0.0)
    tax = db.Column(db.Float, nullable=False, default=0.0)
    total = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    # Lifecycle state. ``active`` documents are the only ones that may affect
    # stock, person balances, cashbox totals and reports. A voided document
    # keeps its rows (and therefore its audit history) but is excluded from
    # every aggregate through utils.accounting.active_invoices()/active_cashdocs().
    # Monotonic optimistic-concurrency token. Two operators can hold the same
    # edit form; without this the second save silently discards the first one's
    # changes, because the browser has no way to say "I was looking at an older
    # version of this document".
    revision = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    status = db.Column(
        db.String(16),
        nullable=False,
        default="active",
        server_default="active",
        index=True,
    )
    voided_at = db.Column(db.DateTime, nullable=True)
    void_reason = db.Column(db.String(255), nullable=True)

    person = db.relationship("Entity", lazy="joined")


class InvoiceLine(db.Model):
    __tablename__ = "invoice_lines"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    item_id = db.Column(
        db.Integer, db.ForeignKey("entities.id"), nullable=False
    )  # type=item
    qty = db.Column(db.Float, nullable=False, default=1.0)
    unit_price = db.Column(db.Float, nullable=False, default=0.0)
    line_total = db.Column(db.Float, nullable=False, default=0.0)

    invoice = db.relationship(
        "Invoice",
        backref=db.backref("lines", lazy="selectin", cascade="all, delete-orphan"),
    )
    item = db.relationship("Entity", lazy="joined")


class PriceHistory(db.Model):
    __tablename__ = "price_history"
    id = db.Column(db.Integer, primary_key=True)
    person_id = db.Column(db.Integer, db.ForeignKey("entities.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("entities.id"), nullable=False)
    last_price = db.Column(db.Float, nullable=False, default=0.0)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )
    __table_args__ = (
        db.UniqueConstraint("person_id", "item_id", name="uq_price_person_item"),
    )


# --------------------------------------------------------------- cash side ----
class CashBox(db.Model):
    __tablename__ = "cash_boxes"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)
    kind = db.Column(db.String(16), nullable=False, default="cash")  # cash | bank
    bank_name = db.Column(db.String(128), nullable=True)
    account_no = db.Column(db.String(64), nullable=True)
    iban = db.Column(db.String(64), nullable=True)
    description = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)


class CashDoc(db.Model):
    __tablename__ = "cash_docs"
    id = db.Column(db.Integer, primary_key=True)
    doc_type = db.Column(db.String(16), nullable=False)  # receive | payment
    number = db.Column(db.String(32), nullable=False, unique=True)
    date = db.Column(db.Date, nullable=False)
    person_id = db.Column(
        db.Integer, db.ForeignKey("entities.id"), nullable=False
    )  # type=person
    amount = db.Column(db.Float, nullable=False, default=0.0)
    method = db.Column(db.String(64), nullable=True)  # نقد، کارت، حواله...
    note = db.Column(db.String(255), nullable=True)
    cashbox_id = db.Column(db.Integer, db.ForeignKey("cash_boxes.id"), nullable=True)
    cheque_number = db.Column(db.String(32), nullable=True, index=True)
    cheque_bank = db.Column(db.String(128), nullable=True)
    cheque_branch = db.Column(db.String(128), nullable=True)
    cheque_account = db.Column(db.String(64), nullable=True)
    cheque_owner = db.Column(db.String(128), nullable=True)
    cheque_due_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    # Lifecycle state -- see Invoice.status for the contract.
    # Monotonic optimistic-concurrency token. Two operators can hold the same
    # edit form; without this the second save silently discards the first one's
    # changes, because the browser has no way to say "I was looking at an older
    # version of this document".
    revision = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    status = db.Column(
        db.String(16),
        nullable=False,
        default="active",
        server_default="active",
        index=True,
    )
    voided_at = db.Column(db.DateTime, nullable=True)
    void_reason = db.Column(db.String(255), nullable=True)

    person = db.relationship("Entity", lazy="joined")
    cashbox = db.relationship("CashBox", lazy="joined")
