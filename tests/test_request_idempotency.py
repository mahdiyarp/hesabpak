# -*- coding: utf-8 -*-
"""True per-request idempotency for financial creation.

Document-number uniqueness is not idempotency. It only guards a request that
already carries a number, it guards it by *failing* rather than by replaying,
and it cannot see a resend whose number was generated server-side.

These tests pin the real contract on the paths where money moves:

* the same key replaying the original result instead of posting twice;
* the same key carrying different content being refused rather than silently
  treated as a duplicate;
* the claim and the mutation sharing a fate, so a failed attempt never poisons
  its key and a committed one always leaves a replayable record;
* the same guarantee for the assistant, which has no browser token and derives
  its key from the plan's own content.
"""

import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path

import pytest

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="hesabpak-idem-")
os.environ.setdefault("DATA_DIR", _TEST_DATA_DIR)

import app as app_module  # noqa: E402  (must follow the DATA_DIR default)
from extensions import db  # noqa: E402
from models.accounting_models import IdempotencyKey  # noqa: E402
from utils import idempotency  # noqa: E402


@pytest.fixture()
def books():
    """An empty schema, with no application context left open."""
    with app_module.app.app_context():
        db.drop_all()
        db.create_all()
    try:
        yield
    finally:
        with app_module.app.app_context():
            db.session.remove()
            db.drop_all()


@pytest.fixture()
def admin_client(books):
    users_file = Path(app_module.app.config["DATA_DIR"]) / "users.json"
    users_file.write_text(
        json.dumps(
            {
                "users": [
                    {
                        "username": "admin",
                        "password": app_module.generate_password_hash("admin-pass"),
                        "role": "admin",
                        "permissions": app_module.ADMIN_PERMISSIONS,
                        "is_active": True,
                        "email": "mahdiyarp@gmail.com",
                    },
                    {
                        "username": "operator2",
                        "password": app_module.generate_password_hash("other-pass"),
                        "role": "admin",
                        "permissions": app_module.ADMIN_PERMISSIONS,
                        "is_active": True,
                        "email": "mahdiyarp@gmail.com",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = "admin"
        sess["_fresh"] = True
        sess["login_at_utc"] = "2026-01-01T00:00:00"
    return client


@pytest.fixture()
def other_client(books):
    """A second, fully-permissioned operator -- a real login, not a forged id."""
    users_file = Path(app_module.app.config["DATA_DIR"]) / "users.json"
    users_file.write_text(
        json.dumps(
            {
                "users": [
                    {
                        "username": "operator2",
                        "password": app_module.generate_password_hash("other-pass"),
                        "role": "admin",
                        "permissions": app_module.ADMIN_PERMISSIONS,
                        "is_active": True,
                        "email": "mahdiyarp@gmail.com",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = "operator2"
        sess["_fresh"] = True
        sess["login_at_utc"] = "2026-01-01T00:00:00"
    return client


@contextlib.contextmanager
def ctx():
    with app_module.app.app_context():
        yield


@pytest.fixture()
def item(books):
    with ctx():
        ent = app_module.Entity(
            type="item",
            code="101",
            name="کالای تست",
            unit="عدد",
            level=1,
            stock_qty=100.0,
        )
        db.session.add(ent)
        db.session.commit()
        return ent.id


@pytest.fixture()
def person(books):
    with ctx():
        ent = app_module.Entity(
            type="person", code="201", name="مشتری تست", unit="شرکت", level=1
        )
        db.session.add(ent)
        db.session.commit()
        return ent.id


@pytest.fixture()
def cashbox(books):
    with ctx():
        cb = app_module.CashBox(
            name="صندوق اصلی", kind="cash", is_active=True, account_no="IR-TEST-1"
        )
        db.session.add(cb)
        db.session.commit()
        return cb.id


def sales_form(person_id, item_id, *, number="INV-1", qty=10.0, price=2000.0):
    return {
        "invoice_kind": "sales",
        "inv_number": number,
        "inv_date_greg": "2026-04-03",
        "person_token": str(person_id),
        "item_id[]": [str(item_id)],
        "item_code[]": ["101"],
        "qty[]": [str(qty)],
        "unit_price[]": [str(price)],
    }


def cash_form(person_id, cashbox_id, *, number="RCV-1", amount=5000.0):
    return {
        "doc_type": "receive",
        "doc_number": number,
        "doc_date_greg": "2026-04-03",
        "person_token": str(person_id),
        "amount": str(amount),
        "method": "cash",
        "cashbox_id": str(cashbox_id),
    }


def _books_state(item_id, person_id):
    with ctx():
        ent = db.session.get(app_module.Entity, item_id)
        party = db.session.get(app_module.Entity, person_id)
        return (
            db.session.query(app_module.Invoice).count(),
            db.session.query(app_module.InvoiceLine).count(),
            ent.stock_qty,
            party.balance,
        )


# --------------------------------------------------------------------------- #
# Same key, same request -> replay
# --------------------------------------------------------------------------- #


def test_a_resent_sale_replays_instead_of_posting_twice(admin_client, item, person):
    """A retry of one logical sale must move the books exactly once.

    This is the lost-response case: the operator's first POST succeeded but the
    answer never arrived, so the same request is sent again with the same key.
    The correct answer is the original document, not a second charge.
    """
    form = sales_form(person, item, number="INV-1")
    form["idempotency_key"] = "key-sale-1"

    first = admin_client.post("/sales", data=form)
    assert first.status_code == 302
    second = admin_client.post("/sales", data=form, follow_redirects=True)
    assert second.status_code == 200

    count, lines, stock, balance = _books_state(item, person)
    assert count == 1, "the resent request created a second document"
    assert lines == 1
    assert stock == 90.0, "stock moved once per request instead of once per sale"
    assert balance == 20000.0, "the customer was charged twice"


def test_the_key_is_honoured_from_the_idempotency_header(admin_client, item, person):
    """An API-style retry carries its token in a header, not a form field."""
    form = sales_form(person, item, number="INV-H")
    admin_client.post("/sales", data=form, headers={"Idempotency-Key": "hdr-key-1"})
    admin_client.post("/sales", data=form, headers={"Idempotency-Key": "hdr-key-1"})

    count, _lines, stock, balance = _books_state(item, person)
    assert (count, stock, balance) == (1, 90.0, 20000.0)


def test_a_resent_cash_receipt_replays_instead_of_collecting_twice(
    admin_client, person, cashbox
):
    form = cash_form(person, cashbox, number="RCV-1", amount=5000.0)
    form["idempotency_key"] = "key-cash-1"

    first = admin_client.post("/cash_doc", data=form)
    second = admin_client.post("/cash_doc", data=form)
    assert second.status_code == 302
    with ctx():
        receipt = db.session.query(app_module.CashDoc).one()
    assert first.headers["Location"] == f"/cash/{receipt.id}"
    assert second.headers["Location"] == first.headers["Location"], (
        "a replayed receipt must lead back to the original document"
    )

    with ctx():
        from utils import accounting

        assert db.session.query(app_module.CashDoc).count() == 1
        assert (
            accounting.cashbox_balance(cashbox) == 5000.0
        ), "the money was collected twice"
        party = db.session.get(app_module.Entity, person)
        assert party.balance == -5000.0, "the party was credited twice"


def test_a_retried_auto_numbered_sale_replays(admin_client, item, person):
    """A blank number is filled in by the server, so the retry's number differs.

    Only the client's own value may be fingerprinted; if the generated number
    were part of it, a legitimate retry would look like a conflicting request and
    the operator would be told their sale failed.
    """
    form = sales_form(person, item, number="")
    form["idempotency_key"] = "key-auto-1"

    first = admin_client.post("/sales", data=form)
    second = admin_client.post("/sales", data=form)
    assert second.status_code == 302

    assert first.headers["Location"] == second.headers["Location"], (
        "a retried auto-numbered sale must replay, not be refused as a conflict"
    )
    assert second.headers["Location"].startswith("/receive?invoice_id=")

    count, _lines, stock, balance = _books_state(item, person)
    assert count == 1, "the auto-numbered retry posted a second time"
    assert stock == 90.0
    assert balance == 20000.0


def test_a_replay_points_at_the_original_document(admin_client, item, person):
    """The replayed answer is the first document, so the UI stays consistent."""
    form = sales_form(person, item, number="INV-L")
    form["idempotency_key"] = "key-loc-1"
    first = admin_client.post("/sales", data=form)
    second = admin_client.post("/sales", data=form)

    with ctx():
        original = db.session.query(app_module.Invoice).one()
        record = db.session.query(IdempotencyKey).filter_by(key="key-loc-1").one()
        assert record.status == "succeeded"
        assert record.object_type == "invoice"
        assert record.object_id == str(original.id)
        # A replay reproduces the answer the first request gave, which is the
        # post-sale settlement page, not a bare document view.
        assert record.result_location == first.headers["Location"]
    assert second.headers["Location"] == first.headers["Location"]


# --------------------------------------------------------------------------- #
# Same key, different request -> refused
# --------------------------------------------------------------------------- #


def test_a_key_reused_with_a_different_amount_is_refused(admin_client, item, person):
    """A changed payload under a used key is a client bug, not a retry.

    Replaying it would return a document the operator never asked for, which
    is how a wrong amount gets silently 'fixed' to whatever was first.
    """
    form = sales_form(person, item, number="INV-A", price=2000.0)
    form["idempotency_key"] = "key-amt"
    admin_client.post("/sales", data=form)

    changed = sales_form(person, item, number="INV-A", price=9999.0)
    changed["idempotency_key"] = "key-amt"
    resp = admin_client.post("/sales", data=changed, follow_redirects=True)
    assert resp.status_code == 200

    with ctx():
        inv = db.session.query(app_module.Invoice).one()
        assert float(inv.total) == 20000.0, "the second payload overwrote the first"
        party = db.session.get(app_module.Entity, person)
        assert party.balance == 20000.0


def test_a_key_reused_with_a_different_quantity_is_refused(admin_client, item, person):
    form = sales_form(person, item, number="INV-Q", qty=10.0)
    form["idempotency_key"] = "key-qty"
    admin_client.post("/sales", data=form)

    changed = sales_form(person, item, number="INV-Q", qty=25.0)
    changed["idempotency_key"] = "key-qty"
    admin_client.post("/sales", data=changed, follow_redirects=True)

    with ctx():
        inv = db.session.query(app_module.Invoice).one()
        assert len(inv.lines) == 1
        assert float(inv.lines[0].qty) == 10.0
        ent = db.session.get(app_module.Entity, item)
        assert ent.stock_qty == 90.0


def test_a_key_reused_with_a_different_item_is_refused(admin_client, item, person):
    with ctx():
        other = app_module.Entity(
            type="item",
            code="102",
            name="کالای دوم",
            unit="عدد",
            level=1,
            stock_qty=50.0,
        )
        db.session.add(other)
        db.session.commit()
        other_id = other.id

    form = sales_form(person, item, number="INV-I")
    form["idempotency_key"] = "key-item"
    admin_client.post("/sales", data=form)

    changed = sales_form(person, other_id, number="INV-I")
    changed["item_code[]"] = ["102"]
    changed["idempotency_key"] = "key-item"
    admin_client.post("/sales", data=changed, follow_redirects=True)

    with ctx():
        assert db.session.query(app_module.Invoice).count() == 1
        assert float(db.session.get(app_module.Entity, item).stock_qty) == 90.0
        assert float(db.session.get(app_module.Entity, other_id).stock_qty) == 50.0


def test_line_order_does_not_make_a_retry_look_like_a_conflict(
    admin_client, item, person, books
):
    """Line order carries no financial meaning, so it must not change identity."""
    with ctx():
        second = app_module.Entity(
            type="item",
            code="103",
            name="کالای سوم",
            unit="عدد",
            level=1,
            stock_qty=40.0,
        )
        db.session.add(second)
        db.session.commit()
        second_id = second.id

    def two_lines(a, b, ap, bp):
        return {
            "invoice_kind": "sales",
            "inv_number": "INV-ORDER",
            "inv_date_greg": "2026-04-03",
            "person_token": str(person),
            "item_id[]": [str(a), str(b)],
            "item_code[]": ["101", "103"],
            "qty[]": ["2", "3"],
            "unit_price[]": [str(ap), str(bp)],
            "idempotency_key": "key-order",
        }

    admin_client.post("/sales", data=two_lines(item, second_id, 100.0, 200.0))
    admin_client.post(
        "/sales", data=two_lines(second_id, item, 200.0, 100.0), follow_redirects=True
    )

    count, lines, _stock, balance = _books_state(item, person)
    assert count == 1, "a reordered retry was refused instead of replayed"
    assert lines == 2
    assert balance == 100 * 2 + 200 * 3


def test_a_key_cannot_be_replayed_by_another_operator(
    admin_client, other_client, books, item, person
):
    """A leaked token must not become a way to read someone else's document.

    The number is left blank on purpose. With an explicit number the duplicate
    check would reject the second operator anyway, so the test would keep
    passing even with the ownership check removed -- proving nothing at all.
    """
    form = sales_form(person, item, number="")
    form["idempotency_key"] = "key-sec"
    admin_client.post("/sales", data=form)

    with ctx():
        record = db.session.query(IdempotencyKey).filter_by(key="key-sec").one()
        assert record.actor == "admin"
        original_id = int(record.object_id)

    # A second operator presenting the same key must be refused outright. The
    # redirect is inspected without following it: a replay would send this
    # operator straight to the first operator's settlement page.
    resp = other_client.post("/sales", data=form)
    assert resp.status_code == 302
    assert f"/receive?invoice_id={original_id}" not in resp.headers["Location"], (
        "the stolen token handed over the first operator's document"
    )
    assert resp.headers["Location"].endswith("/invoice?kind=sales")

    count, _lines, stock, balance = _books_state(item, person)
    assert count == 1, "the second operator created a document with a stolen key"
    assert stock == 90.0, "the books moved for a request nobody authorised"
    assert balance == 20000.0


def test_claim_refuses_a_key_that_belongs_to_another_operator(books):
    """The ownership rule itself, independent of any HTTP surface."""
    # claim()/complete() only mean anything inside the transaction that also
    # carries the money, which is exactly what accounting.atomic() provides.
    with ctx():
        from utils import accounting

        with accounting.atomic():
            claimed = idempotency.claim(
                idempotency.SCOPE_INVOICE_CREATE, "own-key", "fp-1", actor="admin"
            )
            assert claimed.is_new
            idempotency.complete(
                claimed, object_type="invoice", object_id=7, location="/invoice/7"
            )

    with ctx():
        with pytest.raises(idempotency.IdempotencyConflict):
            idempotency.claim(
                idempotency.SCOPE_INVOICE_CREATE, "own-key", "fp-1", actor="mallory"
            )

    # The rightful owner still replays normally.
    with ctx():
        again = idempotency.claim(
            idempotency.SCOPE_INVOICE_CREATE, "own-key", "fp-1", actor="admin"
        )
        assert again.is_replay
        assert again.location == "/invoice/7"



# --------------------------------------------------------------------------- #
# The claim and the money share a fate
# --------------------------------------------------------------------------- #


def test_a_rejected_posting_leaves_the_key_usable(admin_client, person):
    """A failed attempt must not poison its key.

    The claim is written in the same transaction as the money, so a rollback
    frees the key: the operator can correct the form and submit again with the
    same token instead of being told their key was already spent.
    """
    form = cash_form(person, 999999, number="RCV-BAD", amount=0)
    form["idempotency_key"] = "key-rollback"

    first = admin_client.post("/cash_doc", data=form, follow_redirects=True)
    assert first.status_code == 200
    with ctx():
        assert (
            db.session.query(IdempotencyKey).count() == 0
        ), "a failed posting left a committed claim behind"


def test_a_rolled_back_key_can_be_reused_successfully(
    admin_client, person, cashbox, item
):
    """After a rollback the very same key must still work."""
    broken = cash_form(person, cashbox, number="RCV-RETRY", amount=0)
    broken["idempotency_key"] = "key-reuse"
    admin_client.post("/cash_doc", data=broken, follow_redirects=True)

    good = cash_form(person, cashbox, number="RCV-RETRY", amount=7000.0)
    good["idempotency_key"] = "key-reuse"
    admin_client.post("/cash_doc", data=good, follow_redirects=True)

    with ctx():
        assert db.session.query(IdempotencyKey).filter_by(key="key-reuse").count() == 1
        docs = db.session.query(app_module.CashDoc).all()
        from utils import accounting

        assert len(docs) == 1
        assert float(docs[0].amount) == 7000.0
        assert accounting.cashbox_balance(cashbox) == 7000.0


def test_a_completed_claim_and_its_document_commit_together(admin_client, item, person):
    """No claim may exist without its document, and vice versa."""
    form = sales_form(person, item, number="INV-ATOMIC")
    form["idempotency_key"] = "key-atomic"
    admin_client.post("/sales", data=form)

    with ctx():
        records = db.session.query(IdempotencyKey).all()
        invoices = db.session.query(app_module.Invoice).all()
        assert len(records) == 1 and len(invoices) == 1
        assert records[0].object_id == str(invoices[0].id)
        assert records[0].status == "succeeded"
        assert records[0].completed_at is not None


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_two_simultaneous_sales_with_one_key_post_once(admin_client, item, person):
    """Two requests racing on one key must produce one document.

    Both are the same logical request, so one of them wins the claim and the
    other observes the committed claim and replays. Whichever ordering the
    database chooses, the books must reflect a single sale.
    """
    form = sales_form(person, item, number="INV-RACE")
    form["idempotency_key"] = "key-race"

    barrier = threading.Barrier(2)
    results = {}

    def submit(tag):
        with app_module.app.test_client() as client:
            with client.session_transaction() as sess:
                sess["_user_id"] = "admin"
                sess["_fresh"] = True
                sess["login_at_utc"] = "2026-01-01T00:00:00"
            barrier.wait()
            results[tag] = client.post("/sales", data=form).status_code

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    count, lines, stock, balance = _books_state(item, person)
    assert count == 1, f"the race produced {count} documents (statuses={results})"
    assert lines == 1
    assert stock == 90.0, "stock moved twice for one logical sale"
    assert balance == 20000.0, "the customer was charged twice"


def test_two_simultaneous_receipts_with_one_key_collect_once(
    admin_client, person, cashbox
):
    form = cash_form(person, cashbox, number="RCV-RACE", amount=4000.0)
    form["idempotency_key"] = "key-race-cash"

    barrier = threading.Barrier(2)

    def submit():
        with app_module.app.test_client() as client:
            with client.session_transaction() as sess:
                sess["_user_id"] = "admin"
                sess["_fresh"] = True
                sess["login_at_utc"] = "2026-01-01T00:00:00"
            barrier.wait()
            client.post("/cash_doc", data=form)

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    with ctx():
        from utils import accounting

        assert db.session.query(app_module.CashDoc).count() == 1
        assert accounting.cashbox_balance(cashbox) == 4000.0


# --------------------------------------------------------------------------- #
# The assistant has no browser token
# --------------------------------------------------------------------------- #


def test_the_assistant_replaying_a_plan_does_not_sell_twice(admin_client, item, person):
    """Re-running an identical plan is a retry, and must not post again."""
    with ctx():
        person_row = db.session.get(app_module.Entity, person)
        item_row = db.session.get(app_module.Entity, item)

    plan = {
        "kind": "sales",
        "partner": {"entity_id": person},
        "date": "2026-04-03",
        "items": [{"entity_id": item, "qty": 4, "unit_price": 500.0}],
    }
    with ctx():
        first = app_module._apply_invoice_plan(dict(plan))
        second = app_module._apply_invoice_plan(dict(plan))

    assert first["invoice"].id == second["invoice"].id, "the plan posted twice"
    assert second.get("replayed") is True

    with ctx():
        assert db.session.query(app_module.Invoice).count() == 1
        assert float(db.session.get(app_module.Entity, item).stock_qty) == 96.0
        assert float(db.session.get(app_module.Entity, person).balance) == 2000.0


def test_the_assistant_refuses_to_reuse_a_key_for_a_different_plan(
    admin_client, item, person
):
    first_plan = {
        "kind": "sales",
        "partner": {"entity_id": person},
        "number": "PLAN-1",
        "date": "2026-04-03",
        "items": [{"entity_id": item, "qty": 2, "unit_price": 100.0}],
        "idempotency_key": "plan-key-1",
    }
    with ctx():
        app_module._apply_invoice_plan(dict(first_plan))

    changed = dict(first_plan)
    changed["items"] = [{"entity_id": item, "qty": 9, "unit_price": 100.0}]
    with ctx(), pytest.raises(idempotency.IdempotencyConflict):
        app_module._apply_invoice_plan(changed)

    with ctx():
        inv = db.session.query(app_module.Invoice).one()
        assert len(inv.lines) == 1
        assert float(inv.lines[0].qty) == 2.0
        assert float(db.session.get(app_module.Entity, item).stock_qty) == 98.0


def test_the_assistant_rejects_a_duplicate_explicit_number(admin_client, item, person):
    """A *different* plan asking for a taken number is refused, not renamed."""
    base = {
        "kind": "sales",
        "partner": {"entity_id": person},
        "date": "2026-04-03",
        "items": [{"entity_id": item, "qty": 1, "unit_price": 100.0}],
    }
    first = dict(base, number="PLAN-DUP")
    with ctx():
        app_module._apply_invoice_plan(first)

    second = dict(base, number="PLAN-DUP")
    second["items"] = [{"entity_id": item, "qty": 2, "unit_price": 100.0}]
    with ctx(), pytest.raises(ValueError):
        app_module._apply_invoice_plan(second)

    with ctx():
        assert db.session.query(app_module.Invoice).count() == 1
        assert float(db.session.get(app_module.Entity, item).stock_qty) == 99.0


def test_the_assistant_replaying_a_cash_plan_does_not_pay_twice(
    admin_client, person, cashbox
):
    plan = {
        "doc_type": "receive",
        "person": {"entity_id": person},
        "amount": 2500.0,
        "method": "cash",
        "bank_account": "IR-TEST-1",
        "date": "2026-04-03",
    }
    with ctx():
        first = app_module._apply_cash_plan(dict(plan))
        second = app_module._apply_cash_plan(dict(plan))

    assert first["doc"].id == second["doc"].id
    with ctx():
        from utils import accounting

        assert db.session.query(app_module.CashDoc).count() == 1
        assert accounting.cashbox_balance(cashbox) == 2500.0


# --------------------------------------------------------------------------- #
# Fingerprint contract
# --------------------------------------------------------------------------- #


def test_a_fingerprint_ignores_number_formatting_but_not_its_value(item, person):
    """Quantities arrive as strings from a form and as numbers from the API."""
    as_text = idempotency.request_fingerprint(
        kind="sales",
        number="INV-1",
        doc_date=None,
        person_id=person,
        lines=[{"item_id": item, "qty": "5", "unit_price": "1000"}],
    )
    as_numbers = idempotency.request_fingerprint(
        kind="sales",
        number="INV-1",
        doc_date=None,
        person_id=person,
        lines=[{"item_id": item, "qty": 5, "unit_price": 1000.0}],
    )
    assert as_text == as_numbers, "a form retry changed identity because of its types"

    different = idempotency.request_fingerprint(
        kind="sales",
        number="INV-1",
        doc_date=None,
        person_id=person,
        lines=[{"item_id": item, "qty": 6, "unit_price": 1000.0}],
    )
    assert different != as_numbers


def test_requests_without_a_key_still_work(admin_client, item, person):
    """Idempotency is opt-in: a client that sends no key is not broken."""
    form = sales_form(person, item, number="INV-NOKEY")
    assert "idempotency_key" not in form
    resp = admin_client.post("/sales", data=form)
    assert resp.status_code == 302
    with ctx():
        assert db.session.query(app_module.Invoice).count() == 1
        assert db.session.query(IdempotencyKey).count() == 0


def test_a_key_is_scoped_so_two_endpoints_cannot_replay_each_other(
    admin_client, item, person, cashbox
):
    """The same token on a sale and a receipt is two different requests.

    Scoping by operation is what stops an unrelated endpoint from replaying this
    one merely because the operator's client reused a token.
    """
    sale = sales_form(person, item, number="INV-SCOPE")
    sale["idempotency_key"] = "shared-token"

    receipt = cash_form(person, cashbox, number="RCV-SCOPE", amount=1000.0)
    receipt["idempotency_key"] = "shared-token"

    admin_client.post("/sales", data=sale)
    admin_client.post("/cash_doc", data=receipt, follow_redirects=True)

    with ctx():
        assert db.session.query(app_module.Invoice).count() == 1
        assert (
            db.session.query(app_module.CashDoc).count() == 1
        ), "a cash request replayed a sale's claim"
        assert db.session.query(IdempotencyKey).count() == 2


def test_a_failed_assistant_plan_leaves_its_key_free(
    admin_client, books, item, person
):
    """A rejected plan must not burn the key it was derived from.

    This pins the transactionally-consistent half of the contract for the
    assistant. It also guards a trap that is invisible in the happy path: a
    claim taken inside a SAVEPOINT is *committed* when the savepoint is
    released under pysqlite, so a later rollback cannot remove it. The failed
    plan would then leave a permanently "in progress" key and the corrected
    retry would be refused forever.
    """
    with ctx():
        item_row = db.session.get(app_module.Entity, item)

    plan = {
        "kind": "sales",
        "number": "PLAN-ROLLBACK",
        "date": "2026-04-03",
        "partner": {"entity_id": person},
        # More than the fixture's 100 units exist, so the first attempt fails.
        "items": [{"entity_id": item, "qty": 500, "unit_price": 100.0}],
    }

    with ctx(), pytest.raises(Exception):
        app_module._apply_invoice_plan(dict(plan))

    with ctx():
        assert db.session.query(IdempotencyKey).count() == 0, (
            "a rejected plan left a committed claim that can never be released"
        )
        assert db.session.query(app_module.Invoice).count() == 0
        assert float(db.session.get(app_module.Entity, item).stock_qty) == 100.0

    # The corrected retry reuses the identical plan, so the same key is reused.
    with ctx():
        app_module.Setting.set("allow_negative_sales", "on")
        try:
            out = app_module._apply_invoice_plan(dict(plan))
            assert out["invoice"].number == "PLAN-ROLLBACK"
        finally:
            app_module.Setting.set("allow_negative_sales", "off")
            db.session.rollback()

    with ctx():
        assert db.session.query(IdempotencyKey).count() == 1
        assert float(db.session.get(app_module.Entity, item).stock_qty) == -400.0


def test_a_lost_number_race_is_retried_with_another_number(books, item, person):
    """A generated number can be taken between choosing it and inserting it.

    ``free_invoice_number`` reads the highest id and walks forward, so another
    request committing first leaves this one holding a number that no longer
    exists. The UNIQUE constraint has to arbitrate that race: the loser must
    re-allocate and post, not surface a raw driver error -- and above all must
    not be quietly given the operator's typed number, which is a different
    failure and is asserted separately.
    """
    with ctx():
        # A document that already owns the number we are about to choose.
        taken = app_module.Invoice(
            number="TAKEN-1",
            kind="sales",
            date=app_module.datetime.utcnow().date(),
            person_id=person,
            total=0.0,
        )
        db.session.add(taken)
        db.session.commit()

    attempts = {"n": 0}

    def allocate():
        attempts["n"] += 1
        # First guess is the one the concurrent request just took.
        return ("TAKEN-1", True) if attempts["n"] == 1 else ("TAKEN-2", True)

    def post(number):
        # Runs inside guard_and_post's transaction and the ambient app context.
        if True:
            person_row = db.session.get(app_module.Entity, person)
            item_row = db.session.get(app_module.Entity, item)
            return app_module.accounting.post_invoice(
                kind="sales",
                number=number,
                doc_date=app_module.datetime.utcnow().date(),
                person=person_row,
                lines=[{"item": item_row, "qty": 1, "unit_price": 100.0}],
            )

    with ctx():
        outcome = idempotency.guard_and_post(
            scope=idempotency.SCOPE_INVOICE_CREATE,
            key="race-key",
            request_fingerprint="fp-race",
            actor="admin",
            allocate_number=allocate,
            post=post,
            object_type="invoice",
            location_for=lambda doc: f"/invoice/{doc.id}",
        )
        # Asserted inside the context: the returned ORM object is bound to the
        # session that goes away when it exits.
        assert attempts["n"] == 2, "the collision was not retried"
        assert outcome.result.number == "TAKEN-2"
        assert outcome.replayed is False
        assert db.session.query(app_module.Invoice).filter_by(
            number="TAKEN-2"
        ).count() == 1
        assert float(db.session.get(app_module.Entity, item).stock_qty) == 99.0


def test_a_typed_number_that_loses_the_race_is_reported_not_swapped(
    books, item, person
):
    """A number the operator chose must never be replaced by a different one.

    Re-allocating here would post the sale under a number nobody typed, which is
    the original duplicate-submission bug in a new disguise.
    """
    with ctx():
        db.session.add(
            app_module.Invoice(
                number="MINE-1",
                kind="sales",
                date=app_module.datetime.utcnow().date(),
                person_id=person,
                total=0.0,
            )
        )
        db.session.commit()

    def post(number):
        # Runs inside guard_and_post's transaction and the ambient app context.
        if True:
            person_row = db.session.get(app_module.Entity, person)
            item_row = db.session.get(app_module.Entity, item)
            return app_module.accounting.post_invoice(
                kind="sales",
                number=number,
                doc_date=app_module.datetime.utcnow().date(),
                person=person_row,
                lines=[{"item": item_row, "qty": 1, "unit_price": 100.0}],
            )

    with ctx():
        with pytest.raises(idempotency.NumberTaken):
            idempotency.guard_and_post(
                scope=idempotency.SCOPE_INVOICE_CREATE,
                key="typed-race",
                request_fingerprint="fp-typed",
                actor="admin",
                allocate_number=lambda: ("MINE-1", False),  # not generated
                post=post,
                object_type="invoice",
                location_for=lambda doc: f"/invoice/{doc.id}",
            )

    with ctx():
        assert db.session.query(app_module.Invoice).count() == 1
        assert float(db.session.get(app_module.Entity, item).stock_qty) == 100.0
        assert db.session.query(IdempotencyKey).filter_by(key="typed-race").count() == 0
