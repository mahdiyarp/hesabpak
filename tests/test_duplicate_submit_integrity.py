# -*- coding: utf-8 -*-
"""Duplicate-submission integrity for invoice posting.

A document number is the operator-visible identity of a financial document, and
``Invoice.number`` carries a UNIQUE constraint precisely so that one number can
never describe two documents. These tests pin that guarantee end to end, because
losing it is how a double-clicked Save button charges a customer twice.
"""

import contextlib
import json
import os
import tempfile
from pathlib import Path

import pytest

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="hesabpak-dupfix-")
os.environ.setdefault("DATA_DIR", _TEST_DATA_DIR)

import app as app_module  # noqa: E402  (must follow the DATA_DIR default)
from extensions import db  # noqa: E402


@pytest.fixture()
def books():
    """An empty schema, with no application context left open.

    Holding a context open would make every test client share one ``flask.g``
    and therefore one cached Flask-Login user, so authorization assertions
    would silently stop testing anything.
    """
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
    """A test client logged in as a full-permission admin."""
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
                    }
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


@contextlib.contextmanager
def ctx():
    with app_module.app.app_context():
        yield


@pytest.fixture()
def item(books):
    with ctx():
        ent = app_module.Entity(
            type="item", code="101", name="کالای تست", unit="عدد", level=1, stock_qty=100.0
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


def sales_form(person_id, item_id, *, number, qty=10.0, price=2000.0):
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


def test_a_double_submitted_sale_is_posted_exactly_once(
    admin_client, item, person
):
    """BUG (P0): a resubmitted sale silently became a second document.

    The create route resolved a number collision by *renaming* the document
    (``number = generate_invoice_number()``) and posting anyway, which defeated
    the UNIQUE constraint and the operator's intent at once: a double-clicked
    Save, a browser retry or a flaky-network resend each produced one more real
    document, one more stock movement and one more charge to the party.
    """
    form = sales_form(person, item, number="INV-DUP-1")
    first = admin_client.post("/sales", data=form)
    assert first.status_code == 302

    second = admin_client.post("/sales", data=form, follow_redirects=True)
    assert second.status_code == 200

    with ctx():
        invoices = db.session.query(app_module.Invoice).all()
        assert len(invoices) == 1, [i.number for i in invoices]
        assert invoices[0].number == "INV-DUP-1"

        ent = db.session.get(app_module.Entity, item)
        assert ent.stock_qty == 90.0, "stock moved once per submission, not once per click"
        party = db.session.get(app_module.Entity, person)
        assert party.balance == 20000.0, "the customer was charged twice"
        assert db.session.query(app_module.InvoiceLine).count() == 1


def test_a_duplicate_number_is_reported_instead_of_silently_renumbered(
    admin_client, item, person
):
    """The operator must learn that their number was taken.

    The old code answered a duplicate with a *success* flash for a document
    under a number they never typed, so the books silently disagreed with the
    paperwork.
    """
    admin_client.post(
        "/sales", data=sales_form(person, item, number="INV-DUP-2"),
        follow_redirects=True,
    )
    response = admin_client.post(
        "/sales", data=sales_form(person, item, number="INV-DUP-2"),
        follow_redirects=True,
    )
    body = response.data.decode()
    assert "flash danger" in body, body[:1500]
    assert "تکرار" in body or "قبلاً ثبت" in body, body[:1500]
    # and definitely not the success banner for a document that was never created
    assert "«INV-DUP-2» ثبت شد" not in body

    with ctx():
        assert db.session.query(app_module.Invoice).count() == 1


def test_an_empty_number_still_receives_a_generated_one(admin_client, item, person):
    """Auto-numbering stays available; only *collisions* are refused."""
    form = sales_form(person, item, number="")
    form["inv_number"] = ""
    assert admin_client.post("/sales", data=form).status_code == 302
    form2 = sales_form(person, item, number="")
    form2["inv_number"] = ""
    assert admin_client.post("/sales", data=form2).status_code == 302

    with ctx():
        numbers = [i.number for i in db.session.query(app_module.Invoice).all()]
        assert len(numbers) == 2
        assert all(n for n in numbers), numbers
        assert len(set(numbers)) == 2, numbers


def test_the_assistant_plan_path_refuses_a_duplicate_number(item, person):
    """Site 2: the same silent renaming reached the assistant/chat path.

    ``_apply_invoice_plan`` re-numbered a taken number too, so a retried plan
    posted a second document while the caller believed the first number had
    been reused.
    """
    plan = {
        "kind": "sales",
        "number": "PLAN-DUP-1",
        "date": "2026-04-03",
        "partner": {"entity_id": person, "name": "مشتری تست", "code": "201"},
        "items": [{"entity_id": item, "name": "کالای تست", "qty": 1, "unit_price": 500.0}],
    }
    with ctx():
        first = app_module._apply_invoice_plan(dict(plan))
        assert first["invoice"].number == "PLAN-DUP-1"

        with pytest.raises(ValueError):
            app_module._apply_invoice_plan(dict(plan))

        assert db.session.query(app_module.Invoice).count() == 1
        ent = db.session.get(app_module.Entity, item)
        assert ent.stock_qty == 99.0, "the retried plan moved stock a second time"
        party = db.session.get(app_module.Entity, person)
        assert party.balance == 500.0
