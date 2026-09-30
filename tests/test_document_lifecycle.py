# -*- coding: utf-8 -*-
"""Accounting invariants for the HesabPak document lifecycle.

These tests are written against *behaviour* and *invariant statements*, not
against the current shape of the implementation, so they keep their value if
the internals are refactored.

The central invariant under test:

    a document's effect on stock, on party balances, on cashbox balances and on
    the ledger is a single indivisible unit of work that is applied exactly once
    and reversed exactly once.
"""

from datetime import date

import app as app_module
import pytest
from conftest import balance_of, reload, stock_of
from extensions import db
from sqlalchemy import text
from utils import accounting

# =========================================================================== #
# Helpers
# =========================================================================== #


def post_invoice(
    person,
    item,
    *,
    kind="sales",
    qty=1.0,
    unit_price=100.0,
    number=None,
    doc_date=None,
    allow_negative_sales=False,
):
    return accounting.post_invoice(
        kind=kind,
        number=number or f"INV-{kind}-{item.id}",
        doc_date=doc_date or date(2026, 1, 1),
        person=person,
        lines=[{"item": item, "qty": qty, "unit_price": unit_price}],
        allow_negative_sales=allow_negative_sales,
    )


def post_cash(person, *, doc_type="receive", amount=100.0, number=None, cashbox=None):
    return accounting.post_cashdoc(
        doc_type=doc_type,
        number=number or f"{doc_type.upper()}-{person.id}-{int(amount)}",
        doc_date=date(2026, 1, 2),
        person=person,
        amount=amount,
        method="cash",
        cashbox=cashbox,
    )


def ledger_actions(object_type=None, object_id=None):
    query = db.session.query(app_module.LedgerEntry).order_by(app_module.LedgerEntry.id)
    if object_type is not None:
        query = query.filter(app_module.LedgerEntry.object_type == object_type)
    if object_id is not None:
        query = query.filter(app_module.LedgerEntry.object_id == str(object_id))
    return [entry.action for entry in query.all()]


def assert_books_reconcile(person):
    """The denormalized balance must equal the balance implied by the documents.

    This is the strongest statement available about person balances: it is
    computed independently from the documents themselves rather than from the
    same helper the production code uses.
    """
    assert balance_of(person) == pytest.approx(
        accounting.party_balance_recomputed(person.id), abs=1e-6
    )


# =========================================================================== #
# Sign conventions -- the basis every other test relies on
# =========================================================================== #


def test_sign_conventions_are_the_ones_the_books_are_built_on(ctx):
    assert accounting.invoice_stock_sign("sales") == -1.0
    assert accounting.invoice_stock_sign("purchase") == 1.0
    assert accounting.invoice_balance_sign("sales") == 1.0
    assert accounting.invoice_balance_sign("purchase") == -1.0
    # A receipt settles what the party owes, so it reduces the balance.
    assert accounting.cash_balance_sign("receive") == -1.0
    assert accounting.cash_balance_sign("payment") == 1.0
    # A receipt adds to the cashbox, a payment takes from it.
    assert accounting.cash_cashbox_sign("receive") == 1.0
    assert accounting.cash_cashbox_sign("payment") == -1.0


def test_unknown_document_kinds_are_rejected_not_defaulted(ctx):
    with pytest.raises(accounting.DocumentValidationError):
        accounting.invoice_stock_sign("sale")
    with pytest.raises(accounting.DocumentValidationError):
        accounting.cash_balance_sign("receipt")
    with pytest.raises(accounting.DocumentValidationError):
        accounting.invoice_balance_kind_guard = None
        accounting.normalize_cash_doc_type("transfer")


# =========================================================================== #
# Sales invoice lifecycle
# =========================================================================== #


def test_sales_invoice_reduces_stock_and_increases_balance(ctx, make_person, make_item):
    person = make_person(balance=0.0)
    item = make_item(stock=10.0)

    invoice = post_invoice(person, item, kind="sales", qty=3, unit_price=100.0)

    assert invoice.total == pytest.approx(300.0)
    assert stock_of(item) == pytest.approx(7.0)
    assert balance_of(person) == pytest.approx(300.0)
    assert_books_reconcile(person)


def test_purchase_invoice_increases_stock_and_decreases_balance(
    ctx, make_person, make_item
):
    person = make_person(balance=0.0)
    item = make_item(stock=2.0)

    post_invoice(person, item, kind="purchase", qty=4, unit_price=50.0)

    assert stock_of(item) == pytest.approx(6.0)
    assert balance_of(person) == pytest.approx(-200.0)
    assert_books_reconcile(person)


def test_sales_and_purchase_reversal_signs_are_mirrored(ctx, make_person, make_item):
    """A purchase reversal must not be confused with a sales reversal."""
    seller = make_person(name="فروشنده", code="202")
    buyer = make_person(name="خریدار", code="203")
    stock_item = make_item(name="کالای خرید", code="102", stock=0.0)
    sales_item = make_item(name="کالای فروش", code="103", stock=5.0)

    purchase = post_invoice(buyer, stock_item, kind="purchase", qty=5, unit_price=10.0)
    assert stock_of(stock_item) == pytest.approx(5.0)
    assert balance_of(buyer) == pytest.approx(-50.0)

    accounting.void_invoice(purchase, reason="test")
    # purchase: stock goes back down to 0, the buyer's debt is cleared
    assert stock_of(stock_item) == pytest.approx(0.0)
    assert balance_of(buyer) == pytest.approx(0.0)

    sale = post_invoice(buyer, sales_item, kind="sales", qty=5, unit_price=20.0)
    assert stock_of(sales_item) == pytest.approx(0.0)
    assert balance_of(buyer) == pytest.approx(100.0)

    accounting.void_invoice(sale, reason="test")
    # sales: stock comes back, the receivable is cleared
    assert stock_of(sales_item) == pytest.approx(5.0)
    assert balance_of(buyer) == pytest.approx(0.0)
    assert_books_reconcile(buyer)
    assert balance_of(seller) == pytest.approx(0.0)


# =========================================================================== #
# Cancel / delete on invoices
# =========================================================================== #


def test_cancel_invoice_reverses_stock_and_balance_but_keeps_the_row(
    ctx, make_person, make_item
):
    person = make_person(balance=0.0)
    item = make_item(stock=10.0)
    invoice = post_invoice(person, item, qty=4, unit_price=25.0)

    accounting.void_invoice(invoice, reason="customer returned goods")

    assert stock_of(item) == pytest.approx(10.0)
    assert balance_of(person) == pytest.approx(0.0)
    assert_books_reconcile(person)

    # Logical cancel keeps the document for audit...
    stored = db.session.get(app_module.Invoice, invoice.id)
    assert stored is not None
    assert accounting.is_void(stored)
    assert stored.void_reason == "customer returned goods"
    assert stored.voided_at is not None
    # ...and a cancelled invoice must no longer affect any aggregate.
    assert accounting.active_invoices().count() == 0


def test_delete_invoice_reverses_effects_and_purges_rows(ctx, make_person, make_item):
    person = make_person(balance=0.0)
    item = make_item(stock=10.0)
    invoice = post_invoice(person, item, qty=4, unit_price=25.0)

    accounting.delete_invoice(invoice, reason="entered twice")

    assert stock_of(item) == pytest.approx(10.0)
    assert balance_of(person) == pytest.approx(0.0)
    assert_books_reconcile(person)
    assert db.session.get(app_module.Invoice, invoice.id) is None
    assert app_module.InvoiceLine.query.filter_by(invoice_id=invoice.id).count() == 0


def test_cancelling_twice_does_not_reverse_twice(ctx, make_person, make_item):
    person = make_person(balance=0.0)
    item = make_item(stock=10.0)
    invoice = post_invoice(person, item, qty=4, unit_price=25.0)

    accounting.void_invoice(invoice, reason="first")

    with pytest.raises(accounting.DocumentStateError):
        accounting.void_invoice(invoice, reason="second")

    assert stock_of(item) == pytest.approx(10.0)
    assert balance_of(person) == pytest.approx(0.0)
    # exactly one reversal in the audit trail
    assert ledger_actions("invoice", invoice.id).count(accounting.LEDGER_VOID) == 1


def test_deleting_an_already_cancelled_invoice_does_not_reverse_again(
    ctx, make_person, make_item
):
    person = make_person(balance=0.0)
    item = make_item(stock=10.0)
    invoice = post_invoice(person, item, qty=4, unit_price=25.0)

    accounting.void_invoice(invoice, reason="cancelled first")
    accounting.delete_invoice(invoice, reason="purged later")

    assert stock_of(item) == pytest.approx(10.0)
    assert balance_of(person) == pytest.approx(0.0)
    actions = ledger_actions("invoice", invoice.id)
    assert actions.count(accounting.LEDGER_VOID) == 1
    assert actions.count(accounting.LEDGER_DELETE) == 1
    assert db.session.get(app_module.Invoice, invoice.id) is None


def test_purchase_cannot_be_cancelled_once_the_stock_was_consumed(
    ctx, make_person, make_item
):
    """Reversing a purchase would fabricate negative inventory -- it must fail."""
    supplier = make_person(name="تأمین‌کننده", code="204")
    customer = make_person(name="خریدار", code="205")
    item = make_item(name="کالای محدود", code="104", stock=0.0)

    purchase = post_invoice(supplier, item, kind="purchase", qty=5, unit_price=10.0)
    post_invoice(customer, item, kind="sales", qty=5, unit_price=20.0)
    assert stock_of(item) == pytest.approx(0.0)

    with pytest.raises(accounting.InsufficientStockError):
        accounting.void_invoice(purchase, reason="too late")

    # Nothing moved: the document is still fully applied, not half reversed.
    assert stock_of(item) == pytest.approx(0.0)
    assert balance_of(supplier) == pytest.approx(-50.0)
    stored = db.session.get(app_module.Invoice, purchase.id)
    assert not accounting.is_void(stored)
    assert_books_reconcile(supplier)


# =========================================================================== #
# Invoice edit (re-price)
# =========================================================================== #


def test_edit_invoice_recalculates_the_stock_delta(ctx, make_person, make_item):
    person = make_person(balance=0.0)
    item = make_item(stock=20.0)
    invoice = post_invoice(person, item, qty=4, unit_price=25.0)
    assert stock_of(item) == pytest.approx(16.0)
    assert balance_of(person) == pytest.approx(100.0)

    accounting.reprice_invoice(
        invoice,
        lines=[{"item": item, "qty": 7, "unit_price": 30.0}],
        person=person,
    )

    # net movement only: 4 -> 7 units sold, price 25 -> 30
    assert stock_of(item) == pytest.approx(13.0)
    assert balance_of(person) == pytest.approx(210.0)
    assert_books_reconcile(person)
    lines = app_module.InvoiceLine.query.filter_by(invoice_id=invoice.id).all()
    assert len(lines) == 1
    assert lines[0].qty == pytest.approx(7.0)
    assert lines[0].unit_price == pytest.approx(30.0)


def test_edit_invoice_can_remove_a_line_and_returns_its_stock(
    ctx, make_person, make_item
):
    person = make_person(balance=0.0)
    kept = make_item(name="کالای اول", code="105", stock=10.0)
    dropped = make_item(name="کالای دوم", code="106", stock=10.0)

    invoice = accounting.post_invoice(
        kind="sales",
        number="INV-MULTI",
        doc_date=date(2026, 1, 1),
        person=person,
        lines=[
            {"item": kept, "qty": 2, "unit_price": 10.0},
            {"item": dropped, "qty": 3, "unit_price": 20.0},
        ],
    )
    assert stock_of(kept) == pytest.approx(8.0)
    assert stock_of(dropped) == pytest.approx(7.0)
    assert balance_of(person) == pytest.approx(80.0)

    accounting.reprice_invoice(
        invoice, lines=[{"item": kept, "qty": 2, "unit_price": 15.0}], person=person
    )

    assert stock_of(kept) == pytest.approx(8.0)  # quantity unchanged
    assert stock_of(dropped) == pytest.approx(10.0)  # line removed -> stock back
    assert balance_of(person) == pytest.approx(30.0)  # 80 - 60 + 10
    assert_books_reconcile(person)


def test_edit_invoice_moving_the_counterparty_moves_the_balance(
    ctx, make_person, make_item
):
    old_person = make_person(name="مشتری اول", code="206", balance=0.0)
    new_person = make_person(name="مشتری دوم", code="207", balance=0.0)
    item = make_item(stock=10.0)

    invoice = post_invoice(old_person, item, qty=2, unit_price=50.0)
    assert balance_of(old_person) == pytest.approx(100.0)

    accounting.reprice_invoice(
        invoice,
        lines=[{"item": item, "qty": 2, "unit_price": 50.0}],
        person=new_person,
    )

    assert balance_of(old_person) == pytest.approx(0.0)
    assert balance_of(new_person) == pytest.approx(100.0)
    assert_books_reconcile(old_person)
    assert_books_reconcile(new_person)


def test_edit_invoice_can_add_a_line_for_a_new_item(ctx, make_person, make_item):
    person = make_person(balance=0.0)
    first = make_item(name="کالای اول", code="107", stock=10.0)
    second = make_item(name="کالای دوم", code="108", stock=10.0)

    invoice = post_invoice(person, first, qty=1, unit_price=100.0)
    accounting.reprice_invoice(
        invoice,
        lines=[
            {"item": first, "qty": 1, "unit_price": 100.0},
            {"item": second, "qty": 2, "unit_price": 30.0},
        ],
        person=person,
    )

    assert stock_of(first) == pytest.approx(9.0)
    assert stock_of(second) == pytest.approx(8.0)
    assert balance_of(person) == pytest.approx(160.0)
    assert_books_reconcile(person)


def test_edit_cannot_push_stock_negative(ctx, make_person, make_item):
    person = make_person(balance=0.0)
    item = make_item(stock=5.0)
    invoice = post_invoice(person, item, qty=2, unit_price=10.0)

    with pytest.raises(accounting.InsufficientStockError):
        accounting.reprice_invoice(
            invoice,
            lines=[{"item": item, "qty": 50, "unit_price": 10.0}],
            person=person,
        )

    # the rejected edit must not have moved anything at all
    assert stock_of(item) == pytest.approx(3.0)
    assert balance_of(person) == pytest.approx(20.0)
    assert_books_reconcile(person)


def test_edit_does_not_transiently_breach_stock_even_when_the_edit_is_valid(
    ctx, make_person, make_item
):
    """An end-to-end valid edit must not be rejected by an intermediate state.

    Replacing one item with another whose stock cannot cover the whole new
    quantity, while returning the old item's stock in the same transaction,
    must succeed -- the net result is what has to be feasible.
    """
    person = make_person(balance=0.0)
    sold = make_item(name="کالای فروخته‌شده", code="109", stock=10.0)
    bought = make_item(name="کالای جایگزین", code="110", stock=6.0)

    invoice = post_invoice(person, sold, qty=2, unit_price=10.0)
    assert stock_of(bought) == pytest.approx(6.0)

    accounting.reprice_invoice(
        invoice, lines=[{"item": bought, "qty": 6, "unit_price": 10.0}], person=person
    )

    assert stock_of(sold) == pytest.approx(10.0)  # fully returned
    assert stock_of(bought) == pytest.approx(0.0)  # fully consumed, never negative
    assert_books_reconcile(person)


def test_cancelled_invoice_cannot_be_edited(ctx, make_person, make_item):
    person = make_person(balance=0.0)
    item = make_item(stock=10.0)
    invoice = post_invoice(person, item, qty=2, unit_price=10.0)
    accounting.void_invoice(invoice, reason="cancelled")

    with pytest.raises(accounting.DocumentStateError):
        accounting.reprice_invoice(
            invoice, lines=[{"item": item, "qty": 5, "unit_price": 10.0}], person=person
        )

    assert stock_of(item) == pytest.approx(10.0)
    assert balance_of(person) == pytest.approx(0.0)


# =========================================================================== #
# Negative stock policy
# =========================================================================== #


def test_negative_stock_policy_is_enforced_for_sales(ctx, make_person, make_item):
    person = make_person(balance=0.0)
    item = make_item(stock=1.0)

    with pytest.raises(accounting.InsufficientStockError):
        post_invoice(person, item, qty=2, unit_price=10.0)

    assert stock_of(item) == pytest.approx(1.0)
    assert balance_of(person) == pytest.approx(0.0)
    assert db.session.query(app_module.Invoice).count() == 0


def test_negative_stock_can_be_allowed_explicitly_for_sales(
    ctx, make_person, make_item
):
    person = make_person(balance=0.0)
    item = make_item(stock=1.0)

    post_invoice(person, item, qty=3, unit_price=10.0, allow_negative_sales=True)

    assert stock_of(item) == pytest.approx(-2.0)
    assert balance_of(person) == pytest.approx(30.0)
    # cancelling returns the units, even from a negative position
    invoice = db.session.query(app_module.Invoice).one()
    accounting.void_invoice(invoice, reason="returned")
    assert stock_of(item) == pytest.approx(1.0)
    assert balance_of(person) == pytest.approx(0.0)


def test_same_item_on_several_lines_aggregates_the_stock_requirement(
    ctx, make_person, make_item
):
    person = make_person(balance=0.0)
    item = make_item(stock=5.0)

    with pytest.raises(accounting.InsufficientStockError):
        accounting.post_invoice(
            kind="sales",
            number="INV-DUP-ITEM",
            doc_date=date(2026, 1, 1),
            person=person,
            lines=[
                {"item": item, "qty": 3, "unit_price": 10.0},
                {"item": item, "qty": 3, "unit_price": 10.0},
            ],
        )

    assert stock_of(item) == pytest.approx(5.0)


# =========================================================================== #
# Cash documents
# =========================================================================== #


def test_receive_reduces_balance_and_payment_increases_it(ctx, make_person, make_item):
    person = make_person(balance=0.0)

    post_cash(person, doc_type="receive", amount=250.0, number="RCV-1")
    assert balance_of(person) == pytest.approx(-250.0)

    post_cash(person, doc_type="payment", amount=100.0, number="PAY-1")
    assert balance_of(person) == pytest.approx(-150.0)
    assert_books_reconcile(person)


def test_edit_cash_document_moves_the_balance_by_the_delta(ctx, make_person):
    person = make_person(balance=0.0)
    doc = post_cash(person, doc_type="receive", amount=250.0, number="RCV-2")

    accounting.reprice_cashdoc(doc, amount=100.0)
    assert balance_of(person) == pytest.approx(-100.0)

    accounting.reprice_cashdoc(doc, amount=400.0)
    assert balance_of(person) == pytest.approx(-400.0)
    assert_books_reconcile(person)


def test_cash_document_rejects_a_non_positive_amount(ctx, make_person):
    person = make_person(balance=0.0)
    doc = post_cash(person, doc_type="receive", amount=250.0, number="RCV-3")

    with pytest.raises(accounting.DocumentValidationError):
        accounting.reprice_cashdoc(doc, amount=0.0)
    with pytest.raises(accounting.DocumentValidationError):
        accounting.reprice_cashdoc(doc, amount=-5.0)

    assert balance_of(person) == pytest.approx(-250.0)


def test_cancel_cash_document_restores_the_previous_balance(ctx, make_person):
    person = make_person(balance=0.0)
    doc = post_cash(person, doc_type="receive", amount=250.0, number="RCV-4")

    accounting.void_cashdoc(doc, reason="wrong entry")

    assert balance_of(person) == pytest.approx(0.0)
    assert_books_reconcile(person)
    stored = db.session.get(app_module.CashDoc, doc.id)
    assert accounting.is_void(stored)
    assert stored.void_reason == "wrong entry"


def test_delete_cash_document_restores_the_previous_balance(ctx, make_person):
    person = make_person(balance=0.0)
    doc = post_cash(person, doc_type="payment", amount=300.0, number="PAY-2")

    accounting.delete_cashdoc(doc, reason="duplicate")

    assert balance_of(person) == pytest.approx(0.0)
    assert_books_reconcile(person)
    assert db.session.get(app_module.CashDoc, doc.id) is None


def test_cancelling_a_cash_document_twice_does_not_double_reverse(ctx, make_person):
    person = make_person(balance=0.0)
    doc = post_cash(person, doc_type="receive", amount=250.0, number="RCV-5")

    accounting.void_cashdoc(doc, reason="once")
    with pytest.raises(accounting.DocumentStateError):
        accounting.void_cashdoc(doc, reason="twice")

    assert balance_of(person) == pytest.approx(0.0)
    assert ledger_actions("cashdoc", doc.id).count(accounting.LEDGER_VOID) == 1


def test_a_sales_invoice_and_its_receipt_net_out(ctx, make_person, make_item):
    person = make_person(balance=0.0)
    item = make_item(stock=10.0)
    invoice = post_invoice(person, item, qty=2, unit_price=500.0)

    accounting.post_cashdoc(
        doc_type="receive",
        number="RCV-6",
        doc_date=date(2026, 1, 3),
        person=person,
        amount=invoice.total,
    )
    assert balance_of(person) == pytest.approx(0.0)
    assert_books_reconcile(person)

    # cancelling the invoice must now expose the receipt as the remaining claim
    accounting.void_invoice(invoice, reason="sales cancelled")
    assert balance_of(person) == pytest.approx(-1000.0)
    assert_books_reconcile(person)

    accounting.void_cashdoc(
        db.session.query(app_module.CashDoc).filter_by(number="RCV-6").one(),
        reason="receipt cancelled",
    )
    assert balance_of(person) == pytest.approx(0.0)
    assert_books_reconcile(person)


# =========================================================================== #
# Cashbox (derived balances)
# =========================================================================== #


def cashbox_total(box_id):
    return accounting.cashbox_balance(box_id)


def test_cashbox_balance_reflects_receive_and_payment(ctx, make_person, make_cashbox):
    person = make_person(balance=0.0)
    box = make_cashbox()

    post_cash(person, doc_type="receive", amount=1000.0, number="RCV-B1", cashbox=box)
    assert cashbox_total(box.id) == pytest.approx(1000.0)

    post_cash(person, doc_type="payment", amount=400.0, number="PAY-B1", cashbox=box)
    assert cashbox_total(box.id) == pytest.approx(600.0)


def test_cashbox_balance_follows_edit_cancel_and_delete(ctx, make_person, make_cashbox):
    person = make_person(balance=0.0)
    box = make_cashbox()
    receive = post_cash(
        person, doc_type="receive", amount=1000.0, number="RCV-B2", cashbox=box
    )
    payment = post_cash(
        person, doc_type="payment", amount=200.0, number="PAY-B2", cashbox=box
    )
    assert cashbox_total(box.id) == pytest.approx(800.0)

    accounting.reprice_cashdoc(receive, amount=1500.0)
    assert cashbox_total(box.id) == pytest.approx(1300.0)

    accounting.void_cashdoc(payment, reason="returned")
    assert cashbox_total(box.id) == pytest.approx(1500.0)

    accounting.delete_cashdoc(receive, reason="wrong entry")
    assert cashbox_total(box.id) == pytest.approx(0.0)
    assert balance_of(person) == pytest.approx(0.0)
    assert_books_reconcile(person)


def test_voided_cashbox_totals_need_no_second_reversal(ctx, make_person, make_cashbox):
    """The cashbox balance is derived, so voiding must move it exactly once."""
    person = make_person(balance=0.0)
    box = make_cashbox()
    doc = post_cash(
        person, doc_type="receive", amount=750.0, number="RCV-B3", cashbox=box
    )

    accounting.void_cashdoc(doc, reason="cancelled")
    assert cashbox_total(box.id) == pytest.approx(0.0)

    with pytest.raises(accounting.DocumentStateError):
        accounting.void_cashdoc(doc, reason="again")
    assert cashbox_total(box.id) == pytest.approx(0.0)


# =========================================================================== #
# Atomicity / failure injection
# =========================================================================== #


def test_failure_after_the_stock_change_rolls_everything_back(
    ctx, make_person, make_item, monkeypatch
):
    person = make_person(balance=0.0)
    item = make_item(stock=10.0)

    real = accounting.adjust_person_balance_delta

    def boom(person_arg, delta):
        raise RuntimeError("injected failure after stock movement")

    monkeypatch.setattr(accounting, "adjust_person_balance_delta", boom)
    with pytest.raises(RuntimeError):
        post_invoice(person, item, qty=3, unit_price=100.0)
    monkeypatch.setattr(accounting, "adjust_person_balance_delta", real)

    assert stock_of(item) == pytest.approx(10.0)
    assert balance_of(person) == pytest.approx(0.0)
    assert db.session.query(app_module.Invoice).count() == 0
    assert app_module.InvoiceLine.query.count() == 0
    assert app_module.LedgerEntry.query.count() == 0
    assert_books_reconcile(person)


def test_failure_after_the_balance_change_rolls_everything_back(
    ctx, make_person, make_item, monkeypatch
):
    person = make_person(balance=0.0)
    item = make_item(stock=10.0)

    def boom(*args, **kwargs):
        raise RuntimeError("injected failure at ledger time")

    monkeypatch.setattr(accounting, "append_ledger", boom)
    with pytest.raises(RuntimeError):
        post_invoice(person, item, qty=3, unit_price=100.0)

    assert stock_of(item) == pytest.approx(10.0)
    assert balance_of(person) == pytest.approx(0.0)
    assert db.session.query(app_module.Invoice).count() == 0
    # The ledger must not claim a mutation that never happened.
    assert app_module.LedgerEntry.query.count() == 0


def test_ledger_failure_rolls_back_a_cash_document_too(ctx, make_person, monkeypatch):
    person = make_person(balance=0.0)

    def boom(*args, **kwargs):
        raise RuntimeError("injected ledger failure")

    monkeypatch.setattr(accounting, "append_ledger", boom)
    with pytest.raises(RuntimeError):
        post_cash(person, doc_type="receive", amount=500.0, number="RCV-FAIL")

    assert balance_of(person) == pytest.approx(0.0)
    assert db.session.query(app_module.CashDoc).count() == 0
    assert app_module.LedgerEntry.query.count() == 0


def test_failure_during_a_cancel_leaves_the_document_fully_applied(
    ctx, make_person, make_item, monkeypatch
):
    person = make_person(balance=0.0)
    item = make_item(stock=10.0)
    invoice = post_invoice(person, item, qty=4, unit_price=25.0)

    # Fail *after* the stock has been given back but *before* the balance is
    # unwound. A partial reversal must not survive.
    def boom(person_arg, delta):
        raise RuntimeError("injected failure mid-cancel")

    monkeypatch.setattr(accounting, "adjust_person_balance_delta", boom)
    with pytest.raises(RuntimeError):
        accounting.void_invoice(invoice, reason="will fail")
    monkeypatch.undo()

    # The rollback must restore the *fully applied* state: the units that the
    # cancel had already given back must be taken again, and the receivable
    # must still stand.
    assert stock_of(item) == pytest.approx(6.0)
    assert balance_of(person) == pytest.approx(100.0)
    stored = db.session.get(app_module.Invoice, invoice.id)
    # the status claim was rolled back too: the document is fully active again
    assert not accounting.is_void(stored)
    assert stored.voided_at is None
    assert_books_reconcile(person)


def test_failure_while_creating_a_dependent_record_rolls_back(
    ctx, make_person, make_item, monkeypatch
):
    person = make_person(balance=0.0)
    item = make_item(stock=10.0)

    real_append = accounting.append_ledger

    def boom(*args, **kwargs):
        raise RuntimeError("dependent record failed")

    monkeypatch.setattr(accounting, "append_ledger", boom)
    with pytest.raises(RuntimeError):
        accounting.post_invoice(
            kind="purchase",
            number="INV-DEP",
            doc_date=date(2026, 1, 1),
            person=person,
            lines=[{"item": item, "qty": 5, "unit_price": 10.0}],
        )
    monkeypatch.setattr(accounting, "append_ledger", real_append)

    assert stock_of(item) == pytest.approx(10.0)
    assert balance_of(person) == pytest.approx(0.0)
    assert db.session.query(app_module.Invoice).count() == 0


# =========================================================================== #
# Ledger
# =========================================================================== #


def test_every_mutation_appends_exactly_one_ledger_entry(ctx, make_person, make_item):
    person = make_person(balance=0.0)
    item = make_item(stock=10.0)

    invoice = post_invoice(person, item, qty=2, unit_price=50.0)
    assert ledger_actions("invoice", invoice.id) == [accounting.LEDGER_CREATE]

    accounting.reprice_invoice(
        invoice, lines=[{"item": item, "qty": 4, "unit_price": 60.0}], person=person
    )
    assert ledger_actions("invoice", invoice.id) == [
        accounting.LEDGER_CREATE,
        accounting.LEDGER_UPDATE,
    ]

    accounting.void_invoice(invoice, reason="cancelled")
    assert ledger_actions("invoice", invoice.id) == [
        accounting.LEDGER_CREATE,
        accounting.LEDGER_UPDATE,
        accounting.LEDGER_VOID,
    ]


def test_cash_document_lifecycle_is_fully_audited(ctx, make_person):
    person = make_person(balance=0.0)
    doc = post_cash(person, doc_type="receive", amount=100.0, number="RCV-L1")
    accounting.reprice_cashdoc(doc, amount=150.0)
    accounting.void_cashdoc(doc, reason="cancelled")

    assert ledger_actions("cashdoc", doc.id) == [
        accounting.LEDGER_CREATE,
        accounting.LEDGER_UPDATE,
        accounting.LEDGER_VOID,
    ]


def test_ledger_payload_records_the_actual_deltas(ctx, make_person, make_item):
    import json

    person = make_person(balance=0.0)
    item = make_item(stock=10.0)
    invoice = post_invoice(person, item, qty=3, unit_price=100.0)

    entry = app_module.LedgerEntry.query.filter_by(
        object_type="invoice", object_id=str(invoice.id)
    ).one()
    payload = json.loads(entry.payload)
    assert payload["total"] == pytest.approx(300.0)
    assert payload["person_balance_delta"] == pytest.approx(300.0)
    assert payload["stock_delta"][str(item.id)] == pytest.approx(-3.0)


def test_ledger_chain_is_ordered_and_verifiable(ctx, make_person, make_item):
    person = make_person(balance=0.0)
    item = make_item(stock=50.0)

    for i in range(5):
        post_invoice(
            person,
            item,
            qty=1,
            unit_price=10.0,
            number=f"INV-CHAIN-{i}",
        )

    result = accounting.verify_ledger_chain()
    assert result["ok"] is True, result
    assert result["checked"] >= 5

    entries = app_module.LedgerEntry.query.order_by(app_module.LedgerEntry.id).all()
    assert entries[0].prev_hash is None
    for previous, current in zip(entries, entries[1:]):
        assert current.prev_hash == previous.hash


def test_a_failed_mutation_leaves_the_ledger_chain_intact(
    ctx, make_person, make_item, monkeypatch
):
    person = make_person(balance=0.0)
    item = make_item(stock=10.0)
    post_invoice(person, item, qty=1, unit_price=10.0, number="INV-OK-1")

    before = app_module.LedgerEntry.query.count()

    def boom(*args, **kwargs):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(accounting, "append_ledger", boom)
    with pytest.raises(RuntimeError):
        post_invoice(person, item, qty=1, unit_price=10.0, number="INV-BAD")

    assert app_module.LedgerEntry.query.count() == before
    assert accounting.verify_ledger_chain()["ok"] is True


# =========================================================================== #
# Concurrency
# =========================================================================== #


def test_concurrent_balance_updates_do_not_lose_each_other(ctx, make_person):
    """Ten sequential postings on separate sessions must sum exactly."""
    from concurrent.futures import ThreadPoolExecutor

    person = make_person(balance=0.0)
    person_id = person.id

    def receive(index):
        with app_module.app.app_context():
            row = db.session.get(app_module.Entity, person_id)
            accounting.post_cashdoc(
                doc_type="receive",
                number=f"RCV-CONC-{index}",
                doc_date=date(2026, 1, 5),
                person=row,
                amount=100.0,
                method="cash",
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(receive, range(8)))

    db.session.expire_all()
    assert float(db.session.get(app_module.Entity, person_id).balance) == pytest.approx(
        -800.0, abs=1e-6
    )
    assert accounting.party_balance_recomputed(person_id) == pytest.approx(
        -800.0, abs=1e-6
    )


def test_only_one_of_two_competing_sales_can_take_the_last_unit(
    ctx, make_person, make_item
):
    from concurrent.futures import ThreadPoolExecutor

    person = make_person(balance=0.0)
    item = make_item(stock=1.0)
    person_id, item_id = person.id, item.id

    def sell(index):
        with app_module.app.app_context():
            try:
                p = db.session.get(app_module.Entity, person_id)
                it = db.session.get(app_module.Entity, item_id)
                accounting.post_invoice(
                    kind="sales",
                    number=f"INV-RACE-{index}",
                    doc_date=date(2026, 1, 6),
                    person=p,
                    lines=[{"item": it, "qty": 1, "unit_price": 100.0}],
                )
                return "posted"
            except accounting.AccountingError:
                db.session.rollback()
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(sell, (1, 2)))

    assert sorted(outcomes) == ["posted", "rejected"]
    db.session.expire_all()
    assert float(db.session.get(app_module.Entity, item_id).stock_qty) == pytest.approx(
        0.0, abs=1e-9
    )
    assert accounting.party_balance_recomputed(person_id) == pytest.approx(
        100.0, abs=1e-6
    )


def test_concurrent_cancels_reverse_exactly_once(ctx, make_person, make_item):
    from concurrent.futures import ThreadPoolExecutor

    person = make_person(balance=0.0)
    item = make_item(stock=10.0)
    invoice = post_invoice(person, item, qty=4, unit_price=25.0)
    invoice_id = invoice.id

    def cancel(_index):
        with app_module.app.app_context():
            row = db.session.get(app_module.Invoice, invoice_id)
            try:
                accounting.void_invoice(row, reason="race")
                return "reversed"
            except accounting.DocumentStateError:
                db.session.rollback()
                return "already"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(cancel, (1, 2)))

    assert sorted(outcomes) == ["already", "reversed"]
    db.session.expire_all()
    assert float(db.session.get(app_module.Entity, item.id).stock_qty) == pytest.approx(
        10.0, abs=1e-9
    )
    assert float(db.session.get(app_module.Entity, person.id).balance) == pytest.approx(
        0.0, abs=1e-6
    )
    voids = (
        db.session.query(app_module.LedgerEntry)
        .filter_by(
            object_type="invoice",
            object_id=str(invoice_id),
            action=accounting.LEDGER_VOID,
        )
        .count()
    )
    assert voids == 1
    assert accounting.verify_ledger_chain()["ok"] is True


def test_concurrent_ledger_appends_form_a_valid_chain(ctx):
    from concurrent.futures import ThreadPoolExecutor

    def append(index):
        with app_module.app.app_context():
            # append_ledger deliberately does NOT commit on its own: the entry
            # must share the fate of the transaction that produced it.
            with accounting.atomic():
                accounting.append_ledger(
                    "concurrency", str(index), "create", {"i": index}
                )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append, range(8)))

    result = accounting.verify_ledger_chain()
    assert result["ok"] is True, result
    assert result["checked"] == 8


# --------------------------------------------------------------------------- #
# The whole unit of work is one transaction
# --------------------------------------------------------------------------- #


def _post_sales_invoice(ctx, make_person, make_item, qty=2.0, price=50.0):
    person = make_person(balance=0.0)
    item = make_item(stock=10.0)
    invoice = accounting.post_invoice(
        kind="sales",
        number="INV-TX-1",
        doc_date=date(2026, 3, 1),
        person=person,
        lines=[{"item": item, "qty": qty, "unit_price": price}],
    )
    return invoice, person, item


def test_hard_delete_is_a_single_transaction(ctx, make_person, make_item, monkeypatch):
    """A failure while purging must not leave the reversal committed on its own.

    ``delete_invoice`` reverses the posting *and* deletes the rows. If the
    reversal committed separately, a failure in the second half would leave a
    voided document whose effects are already unwound, and the operator would be
    told the delete failed.
    """
    invoice, person, item = _post_sales_invoice(ctx, make_person, make_item)
    invoice_id = invoice.id
    assert stock_of(item) == pytest.approx(8.0)
    assert balance_of(person) == pytest.approx(100.0)

    def exploding_append_ledger(object_type, object_id, action, payload, **kwargs):
        if action == accounting.LEDGER_DELETE:
            raise RuntimeError("ledger storage is unavailable")
        return real_append(object_type, object_id, action, payload, **kwargs)

    real_append = accounting.append_ledger
    monkeypatch.setattr(accounting, "append_ledger", exploding_append_ledger)

    with pytest.raises(RuntimeError):
        accounting.delete_invoice(
            db.session.get(app_module.Invoice, invoice_id), reason="tx-test"
        )

    # Everything is back to "posted and fully applied": no half-reversal.
    stored = db.session.get(app_module.Invoice, invoice_id)
    assert stored is not None
    assert (stored.status or "active") == "active"
    assert stored.void_reason is None
    assert stock_of(item) == pytest.approx(8.0)
    assert balance_of(person) == pytest.approx(100.0)
    assert (
        db.session.query(app_module.LedgerEntry)
        .filter_by(object_type="invoice", object_id=str(invoice_id))
        .count()
        == 1
    )  # only the create entry


def test_cash_hard_delete_is_a_single_transaction(
    ctx, make_person, make_cashbox, monkeypatch
):
    person = make_person(balance=0.0)
    box = make_cashbox()
    doc = accounting.post_cashdoc(
        doc_type="receive",
        number="RCV-TX-1",
        doc_date=date(2026, 3, 2),
        person=person,
        amount=400.0,
        method="cash",
        cashbox=box,
    )
    doc_id = doc.id
    assert balance_of(person) == pytest.approx(-400.0)
    assert accounting.cashbox_balance(box.id) == pytest.approx(400.0)

    def exploding_append_ledger(object_type, object_id, action, payload, **kwargs):
        if action == accounting.LEDGER_DELETE:
            raise RuntimeError("ledger storage is unavailable")
        return real_append(object_type, object_id, action, payload, **kwargs)

    real_append = accounting.append_ledger
    monkeypatch.setattr(accounting, "append_ledger", exploding_append_ledger)

    with pytest.raises(RuntimeError):
        accounting.delete_cashdoc(
            db.session.get(app_module.CashDoc, doc_id), reason="tx-test"
        )

    stored = db.session.get(app_module.CashDoc, doc_id)
    assert stored is not None
    assert (stored.status or "active") == "active"
    assert balance_of(person) == pytest.approx(-400.0)
    assert accounting.cashbox_balance(box.id) == pytest.approx(400.0)


def test_nested_atomic_blocks_share_one_transaction(ctx, make_person, make_item):
    """A nested ``atomic()`` must join the outer transaction, not commit it."""
    person = make_person(balance=0.0)
    item = make_item(stock=5.0)

    with pytest.raises(RuntimeError):
        with accounting.atomic():
            accounting.adjust_item_stock_delta(item, -1.0)
            with accounting.atomic():
                accounting.adjust_person_balance_delta(person, 42.0)
            raise RuntimeError("boom after the nested block")

    # Neither the outer nor the nested change survived.
    assert stock_of(item) == pytest.approx(5.0)
    assert balance_of(person) == pytest.approx(0.0)


def test_startup_backfill_reports_a_legacy_blank_status_as_active(
    ctx, make_person, make_item
):
    """A document written before the lifecycle column existed is repaired."""
    invoice, person, item = _post_sales_invoice(ctx, make_person, make_item)

    # Simulate a pre-migration row: the column exists but holds no value.
    db.session.execute(
        text("UPDATE invoices SET status = '' WHERE id = :i"), {"i": invoice.id}
    )
    db.session.commit()

    assert app_module._backfill_document_lifecycle_status() == 1
    assert (db.session.get(app_module.Invoice, invoice.id).status or "") == "active"
    # repairing the label must not touch the books
    assert stock_of(item) == pytest.approx(8.0)
    assert balance_of(person) == pytest.approx(100.0)


def test_startup_backfill_never_reactivates_a_voided_document(
    ctx, make_person, make_item
):
    """Running the lifecycle backfill again must leave voids alone.

    The backfill runs on every application start. If it also rewrote 'void'
    rows to 'active', the next restart would put a cancelled document back into
    every aggregate even though its effects have already been reversed.
    """
    invoice, person, item = _post_sales_invoice(ctx, make_person, make_item)
    invoice_id = invoice.id
    accounting.void_invoice(
        db.session.get(app_module.Invoice, invoice_id), reason="void"
    )
    assert (db.session.get(app_module.Invoice, invoice_id).status or "") == "void"

    assert app_module._backfill_document_lifecycle_status() == 0
    assert (db.session.get(app_module.Invoice, invoice_id).status or "") == "void"
    assert stock_of(item) == pytest.approx(10.0)
    assert balance_of(person) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Cash document: editing the counterparty and the date
# --------------------------------------------------------------------------- #


def test_editing_a_cash_document_moves_the_amount_to_the_new_counterparty(
    ctx, make_person
):
    old = make_person(name="طرف اول", code="301", balance=0.0)
    new = make_person(name="طرف دوم", code="302", balance=0.0)
    doc = accounting.post_cashdoc(
        doc_type="receive",
        number="RCV-MOVE-1",
        doc_date=date(2026, 4, 1),
        person=old,
        amount=300.0,
        method="cash",
    )
    assert balance_of(old) == pytest.approx(-300.0)
    assert balance_of(new) == pytest.approx(0.0)

    accounting.reprice_cashdoc(doc, amount=450.0, person=new)

    # The old counterparty is unwound entirely, the new one carries the whole
    # amount -- the two effects are never added together or lost.
    assert balance_of(old) == pytest.approx(0.0)
    assert balance_of(new) == pytest.approx(-450.0)
    stored = db.session.get(app_module.CashDoc, doc.id)
    assert stored.person_id == new.id
    assert float(stored.amount) == 450.0
    reconcile_party(db, old, 0.0)
    reconcile_party(db, new, -450.0)


def reconcile_party(session, person, expected):
    assert balance_of(person) == pytest.approx(expected, abs=1e-6)


def test_reassigning_a_cash_document_does_not_move_the_cashbox(
    ctx, make_person, make_cashbox
):
    old = make_person(name="طرف اول", code="303", balance=0.0)
    new = make_person(name="طرف دوم", code="304", balance=0.0)
    box = make_cashbox()
    doc = accounting.post_cashdoc(
        doc_type="receive",
        number="RCV-MOVE-2",
        doc_date=date(2026, 4, 2),
        person=old,
        amount=250.0,
        method="cash",
        cashbox=box,
    )
    accounting.reprice_cashdoc(doc, amount=250.0, person=new)
    # The money is in the same cashbox; only the attribution moved.
    assert accounting.cashbox_balance(box.id) == pytest.approx(250.0)
    assert balance_of(old) == pytest.approx(0.0)
    assert balance_of(new) == pytest.approx(-250.0)


def test_editing_a_cash_document_date_moves_no_balance(ctx, make_person):
    person = make_person(balance=0.0)
    doc = accounting.post_cashdoc(
        doc_type="payment",
        number="PAY-DATE-1",
        doc_date=date(2026, 4, 3),
        person=person,
        amount=100.0,
        method="cash",
    )
    accounting.reprice_cashdoc(doc, amount=100.0, doc_date=date(2026, 5, 20))
    stored = db.session.get(app_module.CashDoc, doc.id)
    assert stored.date == date(2026, 5, 20)
    assert balance_of(person) == pytest.approx(100.0)


def test_cash_date_and_amount_change_are_auditable_in_the_ledger(ctx, make_person):
    """The update entry must record the values as they were *before* the edit."""
    import json

    person = make_person(balance=0.0)
    doc = accounting.post_cashdoc(
        doc_type="payment",
        number="PAY-LEDGER-DATE",
        doc_date=date(2026, 4, 3),
        person=person,
        amount=100.0,
        method="cash",
    )
    accounting.reprice_cashdoc(doc, amount=150.0, doc_date=date(2026, 5, 20))

    entry = app_module.LedgerEntry.query.filter_by(
        object_type="cashdoc", object_id=str(doc.id), action=accounting.LEDGER_UPDATE
    ).one()
    payload = json.loads(entry.payload)
    assert payload["date_before"] == "2026-04-03"
    assert payload["date_after"] == "2026-05-20"
    assert payload["amount_before"] == pytest.approx(100.0)
    assert payload["amount_after"] == pytest.approx(150.0)


def test_a_failed_counterparty_change_books_the_amount_nowhere(
    ctx, make_person, monkeypatch
):
    """If the reassignment fails midway, neither party may end up charged."""
    old = make_person(name="طرف اول", code="305", balance=0.0)
    new = make_person(name="طرف دوم", code="306", balance=0.0)
    doc = accounting.post_cashdoc(
        doc_type="receive",
        number="RCV-MOVE-3",
        doc_date=date(2026, 4, 4),
        person=old,
        amount=500.0,
        method="cash",
    )

    real_append = accounting.append_ledger

    def exploding_append(*args, **kwargs):
        raise RuntimeError("ledger storage is unavailable")

    monkeypatch.setattr(accounting, "append_ledger", exploding_append)
    with pytest.raises(RuntimeError):
        accounting.reprice_cashdoc(
            db.session.get(app_module.CashDoc, doc.id), amount=500.0, person=new
        )
    monkeypatch.setattr(accounting, "append_ledger", real_append)

    assert balance_of(old) == pytest.approx(-500.0)
    assert balance_of(new) == pytest.approx(0.0)
    assert db.session.get(app_module.CashDoc, doc.id).person_id == old.id


def test_a_cash_document_cannot_be_reassigned_to_a_non_person(
    ctx, make_person, make_item
):
    person = make_person(balance=0.0)
    item = make_item(stock=1.0)
    doc = accounting.post_cashdoc(
        doc_type="receive",
        number="RCV-MOVE-4",
        doc_date=date(2026, 4, 5),
        person=person,
        amount=100.0,
        method="cash",
    )
    with pytest.raises(accounting.DocumentValidationError):
        accounting.reprice_cashdoc(
            db.session.get(app_module.CashDoc, doc.id), amount=100.0, person=item
        )
    assert balance_of(person) == pytest.approx(-100.0)


# --------------------------------------------------------------------------- #
# Cheque rules are the same for every transport
# --------------------------------------------------------------------------- #


def test_a_cheque_needs_a_sixteen_digit_sayyad_number(ctx, make_person, make_cashbox):
    """The web form and the assistant must enforce the same cheque rule."""
    person = make_person(balance=0.0)
    bank = make_cashbox(name="حساب بانکی", kind="bank")
    cash = make_cashbox(name="صندوق", kind="cash")

    def post(number, box=bank):
        return accounting.post_cashdoc(
            doc_type="receive",
            number=f"CHQ-{number}",
            doc_date=date(2026, 6, 1),
            person=person,
            amount=100.0,
            method="cheque",
            cashbox=box,
            cheque_number=number,
        )

    with pytest.raises(accounting.DocumentValidationError):
        post("1234")
    with pytest.raises(accounting.DocumentValidationError):
        post("1" * 15)
    # a cheque booked against a cash box is not a cheque
    with pytest.raises(accounting.DocumentValidationError):
        post("1" * 16, box=cash)

    doc = post("1234567890123456")
    assert doc.cheque_number == "1234567890123456"
    assert balance_of(person) == pytest.approx(-100.0)


def test_cheque_details_are_dropped_from_a_non_cheque_document(
    ctx, make_person, make_cashbox
):
    person = make_person(balance=0.0)
    box = make_cashbox()
    doc = accounting.post_cashdoc(
        doc_type="receive",
        number="RCV-NOT-A-CHEQUE",
        doc_date=date(2026, 6, 2),
        person=person,
        amount=50.0,
        method="cash",
        cashbox=box,
        cheque_number="1234567890123456",
        cheque_bank="ملت",
    )
    stored = db.session.get(app_module.CashDoc, doc.id)
    assert stored.method == "cash"
    assert stored.cheque_number is None
    assert stored.cheque_bank is None


def test_invoice_date_change_is_auditable_in_the_ledger(ctx, make_person, make_item):
    """BUG: reprice_invoice could rewrite a posted invoice's date with the
    ledger recording only total/person -- the date change left no audit trace
    at all, while the cash-document edit path does record it."""
    import json
    from datetime import date as _date

    person = make_person(balance=0.0)
    item = make_item(stock=10.0)
    invoice = post_invoice(person, item, qty=2, unit_price=100.0, doc_date=_date(2026, 3, 3))
    assert invoice.date == _date(2026, 3, 3)

    accounting.reprice_invoice(
        invoice,
        lines=[{"item": item, "qty": 2, "unit_price": 100.0}],
        person=person,
        doc_date=_date(2026, 7, 19),
    )

    entry = app_module.LedgerEntry.query.filter_by(
        object_type="invoice", object_id=str(invoice.id), action=accounting.LEDGER_UPDATE
    ).one()
    payload = json.loads(entry.payload)
    assert payload.get("date_before") == "2026-03-03", payload
    assert payload.get("date_after") == "2026-07-19", payload
