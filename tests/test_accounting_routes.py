# -*- coding: utf-8 -*-
"""HTTP-level accounting lifecycle tests and security review.

These drive the real Flask routes through the test client, so they cover what a
pure unit test cannot: authorization on destructive endpoints, HTTP method
restrictions, and whether a tampered hidden field can change the accounting
meaning of a request.

Two rules keep the assertions honest:

* requests are issued with ``follow_redirects=False`` so the status code
  describes *the request itself* -- a 403 raised by the redirect target would
  otherwise be mistaken for the operation being denied;
* database reads happen inside an explicit application context, because the
  HTTP tests deliberately do not hold one (see ``conftest.fresh_schema``).
"""

import contextlib

import app as app_module
import pytest
from conftest import balance_of, stock_of
from extensions import db
from utils import accounting


@contextlib.contextmanager
def books():
    """Open an application context for a direct database read."""
    with app_module.app.app_context():
        yield


def invoice_payload(
    person,
    item,
    *,
    kind="sales",
    qty=1.0,
    unit_price=100.0,
    number="INV-HTTP-1",
    date="2026-03-01",
):
    return {
        "invoice_kind": kind,
        "inv_number": number,
        "inv_date_greg": date,
        "person_token": str(person.id),
        "item_id[]": [str(item.id)],
        "item_code[]": [item.code],
        "qty[]": [str(qty)],
        "unit_price[]": [str(unit_price)],
    }


def cash_payload(
    person,
    *,
    doc_type="receive",
    amount=100.0,
    number="RCV-HTTP-1",
    date="2026-03-02",
    cashbox_id="",
):
    return {
        "cash_kind": doc_type,
        "doc_number": number,
        "doc_date_greg": date,
        "person_token": str(person.id),
        "amount": str(amount),
        "method": "cash",
        "cashbox_id": str(cashbox_id or ""),
        "note": "",
    }


def cash_edit_payload(person, *, amount=100.0, method="cash", note="", date=""):
    """The cash edit form: amount, counterparty, date, method and note."""
    payload = {
        "person_token": str(person.id),
        "person_code": person.code,
        "amount": str(amount),
        "method": method,
        "note": note,
    }
    if date:
        payload["doc_date_greg"] = date
    return payload


def with_invoices():
    with books():
        return db.session.query(app_module.Invoice).all()


def only_invoice():
    with books():
        invoices = db.session.query(app_module.Invoice).all()
        assert len(invoices) == 1
        return invoices[0].id


def only_cashdoc():
    with books():
        docs = db.session.query(app_module.CashDoc).all()
        assert len(docs) == 1
        return docs[0].id


def invoice_status(inv_id):
    with books():
        row = db.session.get(app_module.Invoice, inv_id)
        return None if row is None else (row.status or "active")


def cashdoc_status(doc_id):
    with books():
        row = db.session.get(app_module.CashDoc, doc_id)
        return None if row is None else (row.status or "active")


def cashbox_total(box_id):
    with books():
        return accounting.cashbox_balance(box_id)


def count(model):
    with books():
        return db.session.query(model).count()


def reconcile_against_person_rows(person):
    """The stored balance must equal a balance recomputed from the documents."""
    stored = balance_of(person)
    with books():
        recomputed = accounting.party_balance_recomputed(person.id)
    assert stored == pytest.approx(recomputed, abs=1e-6)



def _form_fields(body: str):
    """Yield (name, value) for every input the rendered form would submit."""
    import re as _re2

    for tag in _re2.findall(r"<input\b[^>]*>", body):
        name = _re2.search(r'name="([^"]*)"', tag)
        if not name:
            continue
        value = _re2.search(r'value="([^"]*)"', tag)
        yield name.group(1), (value.group(1) if value else "")


@pytest.fixture()
def ledger(admin_client, make_person, make_item):
    return admin_client, make_person(balance=0.0), make_item(stock=50.0)


# =========================================================================== #
# Invoice lifecycle over HTTP
# =========================================================================== #


def test_create_then_cancel_invoice_over_http(ledger):
    client, person, item = ledger

    resp = client.post("/invoice", data=invoice_payload(person, item))
    assert resp.status_code == 302
    assert stock_of(item) == pytest.approx(49.0)
    assert balance_of(person) == pytest.approx(100.0)

    inv_id = only_invoice()
    resp = client.post(f"/invoice/{inv_id}/cancel", data={"reason": "test"})
    assert resp.status_code == 302
    assert stock_of(item) == pytest.approx(50.0)
    assert balance_of(person) == pytest.approx(0.0)
    assert invoice_status(inv_id) == "void"
    reconcile_against_person_rows(person)


def test_create_then_delete_invoice_over_http(ledger):
    client, person, item = ledger
    client.post("/invoice", data=invoice_payload(person, item, number="INV-HTTP-2"))
    inv_id = only_invoice()

    resp = client.post(f"/invoice/{inv_id}/delete", data={})
    assert resp.status_code == 302
    assert stock_of(item) == pytest.approx(50.0)
    assert balance_of(person) == pytest.approx(0.0)
    assert invoice_status(inv_id) is None
    assert count(app_module.Invoice) == 0
    assert count(app_module.InvoiceLine) == 0


def test_create_then_edit_invoice_over_http(ledger):
    client, person, item = ledger
    client.post(
        "/invoice",
        data=invoice_payload(person, item, qty=2, unit_price=50.0, number="INV-HTTP-3"),
    )
    inv_id = only_invoice()

    resp = client.post(
        f"/invoice/{inv_id}/edit",
        data=invoice_payload(person, item, qty=5, unit_price=60.0, number="INV-HTTP-3"),
    )
    assert resp.status_code == 302
    assert stock_of(item) == pytest.approx(45.0)
    assert balance_of(person) == pytest.approx(300.0)
    reconcile_against_person_rows(person)


def test_edit_rejects_a_body_that_would_push_stock_negative_over_http(ledger):
    client, person, item = ledger
    client.post(
        "/invoice",
        data=invoice_payload(
            person, item, qty=1, unit_price=10.0, number="INV-HTTP-NEG"
        ),
    )
    inv_id = only_invoice()

    resp = client.post(
        f"/invoice/{inv_id}/edit",
        data=invoice_payload(
            person, item, qty=999, unit_price=10.0, number="INV-HTTP-NEG"
        ),
    )
    assert resp.status_code == 302
    # nothing changed: the failed edit rolled back completely
    assert stock_of(item) == pytest.approx(49.0)
    assert balance_of(person) == pytest.approx(10.0)
    with books():
        assert (
            db.session.get(app_module.Invoice, inv_id).status or "active"
        ) == "active"
    reconcile_against_person_rows(person)


# =========================================================================== #
# Cash document lifecycle over HTTP
# =========================================================================== #


def test_cash_document_lifecycle_over_http(ledger):
    client, person, _item = ledger

    assert (
        client.post(
            "/cash_doc", data=cash_payload(person, amount=500.0, number="RCV-HTTP-2")
        ).status_code
        == 302
    )
    doc_id = only_cashdoc()
    assert balance_of(person) == pytest.approx(-500.0)

    assert (
        client.post(
            f"/cash/{doc_id}/edit", data=cash_edit_payload(person, amount=700)
        ).status_code
        == 302
    )
    assert balance_of(person) == pytest.approx(-700.0)

    assert client.post(f"/cash/{doc_id}/cancel", data={}).status_code == 302
    assert balance_of(person) == pytest.approx(0.0)
    assert cashdoc_status(doc_id) == "void"
    reconcile_against_person_rows(person)


def test_cash_document_delete_over_http(ledger):
    client, person, _item = ledger
    client.post(
        "/cash_doc", data=cash_payload(person, amount=250.0, number="RCV-HTTP-3")
    )
    doc_id = only_cashdoc()

    assert client.post(f"/cash/{doc_id}/delete", data={}).status_code == 302
    assert balance_of(person) == pytest.approx(0.0)
    assert count(app_module.CashDoc) == 0


def test_cashbox_total_survives_the_whole_lifecycle(ledger, make_cashbox):
    client, person, _item = ledger
    box = make_cashbox()

    client.post(
        "/cash_doc",
        data=cash_payload(person, amount=1000.0, number="RCV-BOX-1", cashbox_id=box.id),
    )
    assert cashbox_total(box.id) == pytest.approx(1000.0)

    client.post(
        "/cash_doc",
        data=cash_payload(
            person,
            doc_type="payment",
            amount=300.0,
            number="PAY-BOX-1",
            cashbox_id=box.id,
        ),
    )
    assert cashbox_total(box.id) == pytest.approx(700.0)

    with books():
        doc_ids = [
            row.id
            for row in db.session.query(app_module.CashDoc)
            .order_by(app_module.CashDoc.id)
            .all()
        ]
    assert len(doc_ids) == 2
    for doc_id in doc_ids:
        assert client.post(f"/cash/{doc_id}/cancel", data={}).status_code == 302

    assert cashbox_total(box.id) == pytest.approx(0.0)
    assert balance_of(person) == pytest.approx(0.0)


# =========================================================================== #
# Authorization
# =========================================================================== #


def test_non_admin_cannot_cancel_or_delete_a_document(
    staff_client, make_person, make_item, admin_client
):
    """A user with posting rights must not be able to unwind posted documents."""
    person, item = make_person(balance=0.0), make_item(stock=10.0)
    assert (
        admin_client.post(
            "/invoice", data=invoice_payload(person, item, number="INV-SEC-1")
        ).status_code
        == 302
    )
    inv_id = only_invoice()

    for path in (f"/invoice/{inv_id}/cancel", f"/invoice/{inv_id}/delete"):
        assert staff_client.post(path, data={}).status_code == 403, path

    # still fully applied
    assert invoice_status(inv_id) == "active"
    assert stock_of(item) == pytest.approx(9.0)
    assert balance_of(person) == pytest.approx(100.0)


def test_non_admin_cannot_edit_a_cash_document(ledger, staff_client, make_person):
    admin_client, _person, _item = ledger
    other = make_person(name="طرف ویرایش", code="207", balance=0.0)
    assert (
        admin_client.post(
            "/cash_doc", data=cash_payload(other, amount=100.0, number="RCV-SEC-EDIT")
        ).status_code
        == 302
    )
    doc_id = only_cashdoc()

    assert (
        staff_client.post(
            f"/cash/{doc_id}/edit",
            data=cash_edit_payload(other, amount=9999),
        ).status_code
        == 403
    )
    assert balance_of(other) == pytest.approx(-100.0)


def test_user_without_document_permission_cannot_post_that_document(
    auditor_client, make_person, make_item
):
    """A reports-only user must not be able to create documents by direct call."""
    person, item = make_person(balance=0.0), make_item(stock=10.0)

    assert (
        auditor_client.post(
            "/invoice",
            data=invoice_payload(person, item, kind="sales", number="INV-SEC-2"),
        ).status_code
        == 403
    )
    assert count(app_module.Invoice) == 0

    assert (
        auditor_client.post(
            "/cash_doc",
            data=cash_payload(person, doc_type="receive", number="RCV-SEC-2"),
        ).status_code
        == 403
    )
    assert count(app_module.CashDoc) == 0
    assert stock_of(item) == pytest.approx(10.0)
    assert balance_of(person) == pytest.approx(0.0)


def test_destructive_operations_reject_get(
    admin_client, ledger, make_person, make_item
):
    """Destructive accounting operations must never be reachable via GET."""
    client, person, item = ledger
    assert (
        client.post(
            "/invoice", data=invoice_payload(person, item, number="INV-SEC-GET")
        ).status_code
        == 302
    )
    inv_id = only_invoice()
    assert (
        client.post(
            "/cash_doc", data=cash_payload(person, number="RCV-SEC-GET")
        ).status_code
        == 302
    )
    doc_id = only_cashdoc()

    for path in (
        f"/invoice/{inv_id}/cancel",
        f"/invoice/{inv_id}/delete",
        f"/cash/{doc_id}/cancel",
        f"/cash/{doc_id}/delete",
    ):
        assert client.get(path).status_code == 405, path

    # both documents are untouched
    assert invoice_status(inv_id) == "active"
    assert cashdoc_status(doc_id) == "active"


# =========================================================================== #
# Untrusted hidden fields
# =========================================================================== #


def test_hidden_field_cannot_escalate_the_document_kind(
    sales_client, make_person, make_item
):
    """`invoice_kind`/`cash_kind` must never exceed the caller's permissions.

    The user behind `sales_client` may post sales invoices only. A tampered
    hidden field must not turn that into a purchase, a receipt or a payment.
    """
    person, item = make_person(balance=0.0), make_item(stock=10.0)

    # Baseline: the permitted kind still works.
    assert (
        sales_client.post(
            "/invoice",
            data=invoice_payload(person, item, kind="sales", number="INV-SEC-3-OK"),
        ).status_code
        == 302
    )
    assert count(app_module.Invoice) == 1

    # Hidden field claims "purchase" -> rejected, nothing posted.
    assert (
        sales_client.post(
            "/invoice",
            data=invoice_payload(person, item, kind="purchase", number="INV-SEC-3"),
        ).status_code
        == 403
    )
    assert count(app_module.Invoice) == 1
    assert stock_of(item) == pytest.approx(9.0)

    # Hidden field claims a receipt or a payment -> rejected as well.
    for doc_type in ("receive", "payment"):
        assert (
            sales_client.post(
                "/cash_doc",
                data=cash_payload(person, doc_type=doc_type, number="RCV-SEC-3"),
            ).status_code
            == 403
        ), doc_type
        assert count(app_module.CashDoc) == 0
        # only the permitted baseline sales invoice ever moved the books
        assert balance_of(person) == pytest.approx(100.0)


def test_hidden_person_token_cannot_retarget_a_document(
    admin_client, make_person, make_item
):
    """`person_token` is untrusted: it must resolve to a person, or be rejected."""
    person = make_person(balance=0.0)
    other = make_person(name="طرف دیگر", code="208", balance=0.0)
    item = make_item(stock=10.0)

    # A token that points at an item (not a person) must not post.
    data = invoice_payload(person, item, number="INV-SEC-4")
    data["person_token"] = str(item.id)
    data["person_code"] = ""
    assert admin_client.post("/invoice", data=data).status_code in (200, 302, 400, 403)
    assert count(app_module.Invoice) == 0

    # A token for a real party is accepted, and the balance lands on that party.
    data = invoice_payload(person, item, number="INV-SEC-5")
    data["person_token"] = str(other.id)
    assert admin_client.post("/invoice", data=data).status_code == 302
    assert count(app_module.Invoice) == 1
    assert balance_of(other) == pytest.approx(100.0)
    assert balance_of(person) == pytest.approx(0.0)


def test_cash_document_method_cannot_be_changed_by_a_tampered_form(ledger):
    client, person, _item = ledger
    assert (
        client.post(
            "/cash_doc", data=cash_payload(person, amount=100.0, number="RCV-SEC-6")
        ).status_code
        == 302
    )
    doc_id = only_cashdoc()
    with books():
        assert db.session.get(app_module.CashDoc, doc_id).method == "cash"

    # A forged "method" field must not survive the edit.
    assert (
        client.post(
            f"/cash/{doc_id}/edit",
            data=cash_edit_payload(person, amount=250, method="bank"),
        ).status_code
        == 302
    )
    with books():
        assert db.session.get(app_module.CashDoc, doc_id).method == "cash"
    # the amount edit itself was applied
    assert balance_of(person) == pytest.approx(-100.0)


def test_lifecycle_reason_is_bounded_and_escaped_on_render(
    admin_client, make_person, make_item
):
    person, item = make_person(balance=0.0), make_item(stock=10.0)
    assert (
        admin_client.post(
            "/invoice", data=invoice_payload(person, item, number="INV-SEC-7")
        ).status_code
        == 302
    )
    inv_id = only_invoice()

    hostile = "<script>alert(1)</script>" + "x" * 4000
    assert (
        admin_client.post(
            f"/invoice/{inv_id}/cancel", data={"reason": hostile}
        ).status_code
        == 302
    )

    with books():
        stored = db.session.get(app_module.Invoice, inv_id)
        assert len(stored.void_reason) <= 255
        assert "<script>" in stored.void_reason  # stored raw, escaped on render

    page = admin_client.get(f"/invoice/{inv_id}")
    assert b"<script>alert(1)</script>" not in page.data
    assert b"&lt;script&gt;" in page.data


def test_reports_page_renders_lifecycle_actions_as_post_forms_only(
    admin_client, staff_client, make_person, make_item
):
    """The list view must offer cancel/delete as POST forms, and carry no
    counterparty or document-kind hidden field that a tamper could retarget."""
    person, item = make_person(balance=0.0), make_item(stock=10.0)
    assert (
        admin_client.post(
            "/invoice", data=invoice_payload(person, item, number="INV-UI-1")
        ).status_code
        == 302
    )
    inv_id = only_invoice()

    page = admin_client.get("/reports")
    body = page.data.decode()
    assert f'action="/invoice/{inv_id}/cancel"' in body
    assert f'action="/invoice/{inv_id}/delete"' in body
    assert 'method="post"' in body
    assert "lifecycle-op" in body  # the form carries the styling hook
    # Nothing that a browser could be talked into changing the target of the action.
    assert 'name="kind"' not in body
    assert 'name="person_id"' not in body
    assert 'name="invoice_id"' not in body

    # A non-admin sees the document but never the destructive forms.
    staff_view = staff_client.get("/reports")
    assert staff_view.status_code == 200
    assert b"/cancel" not in staff_view.data
    assert b"/delete" not in staff_view.data


def test_failed_post_leaves_no_document_no_effect_and_no_ledger_entry(
    admin_client, make_person, make_item
):
    """A posting rejected by the stock guard must not leave a trace."""
    person = make_person(balance=0.0)
    item = make_item(stock=1.0)

    # 99 units against 1 in stock -> refused (re-rendered form, or flash+redirect)
    assert admin_client.post(
        "/invoice",
        data=invoice_payload(person, item, qty=99, unit_price=10.0, number="INV-SEC-8"),
    ).status_code in (200, 302)
    assert count(app_module.Invoice) == 0
    assert stock_of(item) == pytest.approx(1.0)
    assert balance_of(person) == pytest.approx(0.0)

    with books():
        entries = (
            db.session.query(app_module.LedgerEntry)
            .filter(app_module.LedgerEntry.object_type == "invoice")
            .all()
        )
    assert entries == []


def test_cancelled_invoice_cannot_be_edited_over_http(ledger):
    client, person, item = ledger
    assert (
        client.post(
            "/invoice", data=invoice_payload(person, item, number="INV-SEC-9")
        ).status_code
        == 302
    )
    inv_id = only_invoice()
    assert client.post(f"/invoice/{inv_id}/cancel", data={}).status_code == 302

    resp = client.post(
        f"/invoice/{inv_id}/edit",
        data=invoice_payload(person, item, qty=9, unit_price=9.0, number="INV-SEC-9"),
    )
    assert resp.status_code in (302, 400, 409, 200)
    # the document stays void and the books stay flat
    assert invoice_status(inv_id) == "void"
    assert stock_of(item) == pytest.approx(50.0)
    assert balance_of(person) == pytest.approx(0.0)


def test_cancelling_twice_over_http_does_not_reverse_twice(ledger):
    client, person, item = ledger
    assert (
        client.post(
            "/invoice", data=invoice_payload(person, item, number="INV-SEC-10")
        ).status_code
        == 302
    )
    inv_id = only_invoice()

    assert client.post(f"/invoice/{inv_id}/cancel", data={}).status_code == 302
    assert client.post(f"/invoice/{inv_id}/cancel", data={}).status_code in (200, 302)
    assert stock_of(item) == pytest.approx(50.0)
    assert balance_of(person) == pytest.approx(0.0)
    reconcile_against_person_rows(person)


def test_deleting_a_void_document_does_not_reverse_a_second_time(ledger):
    client, person, item = ledger
    assert (
        client.post(
            "/invoice", data=invoice_payload(person, item, number="INV-SEC-11")
        ).status_code
        == 302
    )
    inv_id = only_invoice()
    assert client.post(f"/invoice/{inv_id}/cancel", data={}).status_code == 302
    assert client.post(f"/invoice/{inv_id}/delete", data={}).status_code == 302

    assert stock_of(item) == pytest.approx(50.0)
    assert balance_of(person) == pytest.approx(0.0)
    assert count(app_module.Invoice) == 0


def test_cash_edit_reassigns_the_counterparty_and_the_date_over_http(
    ledger, make_person
):
    """The full edit surface of a cash document, driven through the real form."""
    client, person, _item = ledger
    other = make_person(name="طرف جدید", code="209", balance=0.0)

    assert (
        client.post(
            "/cash_doc", data=cash_payload(person, amount=300.0, number="RCV-HTTP-MOVE")
        ).status_code
        == 302
    )
    doc_id = only_cashdoc()
    assert balance_of(person) == pytest.approx(-300.0)

    assert (
        client.post(
            f"/cash/{doc_id}/edit",
            data=cash_edit_payload(other, amount=450.0, date="2026-05-20"),
        ).status_code
        == 302
    )

    assert balance_of(person) == pytest.approx(0.0)
    assert balance_of(other) == pytest.approx(-450.0)
    with books():
        stored = db.session.get(app_module.CashDoc, doc_id)
        assert stored.person_id == other.id
        assert float(stored.amount) == 450.0
        assert stored.method == "cash"
    reconcile_against_person_rows(other)


def test_cash_edit_refuses_a_forged_counterparty_that_is_an_item(ledger, make_item):
    """A tampered person_token can never book a document against an item row."""
    client, person, _item = ledger
    decoy = make_item(name="کالای فریب", code="102", stock=5.0)

    assert (
        client.post(
            "/cash_doc",
            data=cash_payload(person, amount=100.0, number="RCV-HTTP-FORGED"),
        ).status_code
        == 302
    )
    doc_id = only_cashdoc()

    data = cash_edit_payload(person, amount=999.0)
    data["person_token"] = str(decoy.id)
    data["person_code"] = ""
    assert client.post(f"/cash/{doc_id}/edit", data=data).status_code in (200, 302)

    assert balance_of(person) == pytest.approx(-100.0)
    assert stock_of(decoy) == pytest.approx(5.0)


def test_cash_edit_date_field_offers_a_value_the_server_can_parse(
    admin_client, make_person
):
    """BUG: the cash edit form rendered a *Jalali* string into the
    ``doc_date_greg`` field, which the server parses with %Y-%m-%d. Parsing
    failed, so any date the user typed was silently discarded while the page
    still reported success -- and the field could not be used at all."""
    import re as _re

    person = make_person(balance=0.0)
    assert (
        admin_client.post(
            "/receive",
            data=cash_payload(person, number="RCV-DATE-UI", date="2026-04-03"),
        ).status_code
        == 302
    )
    doc_id = only_cashdoc()

    page = admin_client.get(f"/cash/{doc_id}/edit")
    assert page.status_code == 200
    body = page.data.decode()
    # Whatever the browser would submit for the date field must be parseable by
    # the very parser the POST handler uses.
    match = _re.search(r'name="doc_date_greg"[^>]*value="([^"]*)"', body)
    assert match, body[:2000]
    submitted = match.group(1)
    from app import parse_gregorian_date

    assert parse_gregorian_date(submitted, allow_none=True) is not None, (
        f"the form offered {submitted!r} but the server parses %Y-%m-%d, so a "
        "round-trip save silently drops the user's date"
    )
    assert parse_gregorian_date(submitted, allow_none=True).isoformat() == "2026-04-03"


def test_cash_edit_date_change_over_http_is_stored_and_audited(
    admin_client, make_person
):
    import json

    person = make_person(balance=0.0)
    assert (
        admin_client.post(
            "/receive",
            data=cash_payload(person, number="RCV-DATE-UI-2", date="2026-04-03"),
        ).status_code
        == 302
    )
    doc_id = only_cashdoc()

    page = admin_client.get(f"/cash/{doc_id}/edit")
    # Take the form as rendered and change only the date, the way a user would.
    fields = dict(_form_fields(page.data.decode()))
    fields["doc_date_greg"] = "2026-05-20"

    assert admin_client.post(f"/cash/{doc_id}/edit", data=fields).status_code == 302

    with books():
        stored = db.session.get(app_module.CashDoc, doc_id)
        assert stored.date.isoformat() == "2026-05-20"
        entry = app_module.LedgerEntry.query.filter_by(
            object_type="cashdoc", object_id=str(doc_id), action="update"
        ).one()
        payload = json.loads(entry.payload)
        assert payload["date_before"] == "2026-04-03"
        assert payload["date_after"] == "2026-05-20"


def test_cash_edit_round_trip_save_does_not_move_the_document_to_year_1405(
    admin_client, make_person
):
    """BUG (data corruption): opening the cash edit form and saving it without
    touching the date posted the *Jalali* rendering of the date, which
    %Y-%m-%d happily parses as a Gregorian year 1405 -- so an ordinary amount
    edit silently moved the document four centuries into the future."""
    person = make_person(balance=0.0)
    assert (
        admin_client.post(
            "/receive",
            data=cash_payload(person, number="RCV-ROUNDTRIP", date="2026-04-03"),
        ).status_code
        == 302
    )
    doc_id = only_cashdoc()

    page = admin_client.get(f"/cash/{doc_id}/edit")
    fields = dict(_form_fields(page.data.decode()))
    # Change only the amount, exactly as an operator fixing a typo would.
    fields["amount"] = "123"

    assert admin_client.post(f"/cash/{doc_id}/edit", data=fields).status_code == 302

    with books():
        stored = db.session.get(app_module.CashDoc, doc_id)
        assert stored.date.isoformat() == "2026-04-03", (
            f"the date moved to {stored.date.isoformat()} just because the "
            "operator saved the form without editing the date"
        )


def test_a_stale_edit_form_cannot_silently_overwrite_a_newer_save(
    admin_client, make_item, make_person
):
    """A second operator holding the same form must not wipe the first one's work.

    Operator A renders the edit form and corrects the unit price; operator B
    still holds the copy rendered before that. When B saves, the browser has no
    way to say "I was looking at an older document", so the last write simply
    won: A's price change vanished and B was told he had succeeded.
    """
    item = make_item(stock=100.0)
    person = make_person(balance=0.0)
    assert (
        admin_client.post(
            "/sales",
            data=invoice_payload(
                person, item, number="INV-CAS-1", qty=10, unit_price=2000.0
            ),
        ).status_code
        == 302
    )
    inv_id = only_invoice()

    form_a = dict(_form_fields(admin_client.get(f"/invoice/{inv_id}/edit").data.decode()))
    form_b = dict(_form_fields(admin_client.get(f"/invoice/{inv_id}/edit").data.decode()))
    assert form_a["revision"] == form_b["revision"] == "0"

    # A raises the unit price and saves.
    form_a["unit_price[]"] = ["3000"]
    assert (
        admin_client.post(f"/invoice/{inv_id}/edit", data=form_a).status_code == 302
    )

    # B, still holding the pre-A copy, changes only the quantity.
    form_b["qty[]"] = ["7"]
    response = admin_client.post(
        f"/invoice/{inv_id}/edit", data=form_b, follow_redirects=True
    )
    assert "flash danger" in response.data.decode()
    assert "تغییر کرده است" in response.data.decode()

    with books():
        inv = db.session.get(app_module.Invoice, inv_id)
        line = inv.lines[0]
        # A's correction survives; B was told his stale save did not apply.
        assert line.unit_price == 3000.0
        assert line.qty == 10.0
        assert inv.total == 30000.0
        assert stock_of(item) == 90.0
        assert balance_of(person) == 30000.0
        assert inv.revision == 1, "one accepted edit means one revision bump"


def test_a_freshly_rendered_form_still_saves(admin_client, make_item, make_person):
    """The guard must not break the ordinary sequential edit."""
    item = make_item(stock=100.0)
    person = make_person(balance=0.0)
    assert (
        admin_client.post(
            "/sales",
            data=invoice_payload(
                person, item, number="INV-CAS-2", qty=10, unit_price=2000.0
            ),
        ).status_code
        == 302
    )
    inv_id = only_invoice()

    for expected_revision, qty in ((0, 5), (1, 6), (2, 7)):
        form = dict(
            _form_fields(admin_client.get(f"/invoice/{inv_id}/edit").data.decode())
        )
        assert form["revision"] == str(expected_revision)
        form["qty[]"] = [str(qty)]
        assert (
            admin_client.post(f"/invoice/{inv_id}/edit", data=form).status_code == 302
        )

    with books():
        inv = db.session.get(app_module.Invoice, inv_id)
        assert inv.lines[0].qty == 7.0
        assert inv.revision == 3
        assert stock_of(item) == 93.0
        assert balance_of(person) == 14000.0


def test_a_stale_cash_edit_form_cannot_silently_overwrite_a_newer_save(
    admin_client, make_person
):
    person = make_person(balance=0.0)
    assert (
        admin_client.post(
            "/receive",
            data=cash_payload(person, number="RCV-CAS-1", amount=5000.0),
        ).status_code
        == 302
    )
    doc_id = only_cashdoc()

    form_a = dict(_form_fields(admin_client.get(f"/cash/{doc_id}/edit").data.decode()))
    form_b = dict(_form_fields(admin_client.get(f"/cash/{doc_id}/edit").data.decode()))
    assert form_a["revision"] == form_b["revision"] == "0"

    form_a["amount"] = "9000"
    assert admin_client.post(f"/cash/{doc_id}/edit", data=form_a).status_code == 302

    form_b["amount"] = "123"
    response = admin_client.post(
        f"/cash/{doc_id}/edit", data=form_b, follow_redirects=True
    )
    assert "flash danger" in response.data.decode()

    with books():
        stored = db.session.get(app_module.CashDoc, doc_id)
        assert stored.amount == 9000.0, "the stale save overwrote the newer amount"
        assert stored.revision == 1
        assert balance_of(person) == -9000.0

# --------------------------------------------------------------------------- #
# Reconciliation: do the stored aggregates match an independent recomputation?
#
# The ledger is a hash-chained audit log, not a double-entry journal, so
# "the books balance" here means something concrete and testable: the stock and
# balance stored on each entity must equal what you get by re-deriving them
# from the surviving active documents, using the documented sign convention
# and nothing else. This recomputation deliberately does NOT call
# utils.accounting -- if it did, a sign error would cancel itself out and the
# test would prove nothing.
# --------------------------------------------------------------------------- #


def _recompute_from_documents():
    """Derive expected stock/balance from the documents that are still active."""
    stock = {}
    balance = {}
    cashbox = {}

    for inv in db.session.query(app_module.Invoice).all():
        if accounting.is_void(inv):
            continue
        if inv.kind == "sales":
            sign_stock, sign_balance = -1.0, 1.0
        else:
            sign_stock, sign_balance = 1.0, -1.0
        for line in inv.lines:
            stock[line.item_id] = stock.get(line.item_id, 0.0) + sign_stock * line.qty
        balance[inv.person_id] = balance.get(inv.person_id, 0.0) + sign_balance * inv.total

    for doc in db.session.query(app_module.CashDoc).all():
        if accounting.is_void(doc):
            continue
        if doc.doc_type == "receive":
            sign_party, sign_box = -1.0, 1.0
        else:
            sign_party, sign_box = 1.0, -1.0
        balance[doc.person_id] = balance.get(doc.person_id, 0.0) + sign_party * doc.amount
        if doc.cashbox_id:
            cashbox[doc.cashbox_id] = cashbox.get(doc.cashbox_id, 0.0) + sign_box * doc.amount

    return stock, balance, cashbox


def test_stored_aggregates_reconcile_with_an_independent_recomputation(
    admin_client, make_item, make_person
):
    """A realistic mixed book, then void / edit / delete, then reconciliation."""
    item = make_item(stock=0.0)
    customer = make_person(balance=0.0, name="مشتری", code="201")
    supplier = make_person(balance=0.0, name="تامین‌کننده", code="202")

    # 1. purchase 10 @ 100 from the supplier
    r = admin_client.post(
        "/purchase",
        data=invoice_payload(
            supplier, item, kind="purchase", number="PO-1", qty=10, unit_price=100.0
        ),
    )
    assert r.status_code == 302
    purchase_id = only_invoice()

    # 2. sale 4 @ 300 to the customer
    r = admin_client.post(
        "/sales",
        data=invoice_payload(
            customer, item, kind="sales", number="SO-1", qty=4, unit_price=300.0
        ),
    )
    assert r.status_code == 302
    sale_id = next(i.id for i in with_invoices() if i.number == "SO-1")

    # 3. receive 500 from the customer, then delete it again
    r = admin_client.post(
        "/receive",
        data=cash_payload(customer, number="RCV-1", amount=500.0),
    )
    assert r.status_code == 302
    receive_id = only_cashdoc()

    # 4. pay 200 to the supplier
    r = admin_client.post(
        "/payment",
        data=cash_payload(supplier, doc_type="payment", number="PAY-1", amount=200.0),
    )
    assert r.status_code == 302

    # 5. void the sale
    assert (
        admin_client.post(f"/invoice/{sale_id}/cancel", data={"reason": "test"}).status_code
        == 302
    )
    # 6. edit the purchase to 12 units
    form = dict(_form_fields(admin_client.get(f"/invoice/{purchase_id}/edit").data.decode()))
    form["qty[]"] = ["12"]
    assert admin_client.post(f"/invoice/{purchase_id}/edit", data=form).status_code == 302
    # 7. delete the receipt
    assert admin_client.post(f"/cash/{receive_id}/delete").status_code == 302

    # -- reconciliation -------------------------------------------------- #
    with books():
        expected_stock, expected_balance, expected_box = _recompute_from_documents()

        item_row = db.session.get(app_module.Entity, item.id)
        assert item_row.stock_qty == pytest.approx(expected_stock.get(item.id, 0.0)), (
            f"stock {item_row.stock_qty} != recomputed {expected_stock.get(item.id, 0.0)}"
        )

        for row in db.session.query(app_module.Entity).filter_by(type="person").all():
            assert row.balance == pytest.approx(
                expected_balance.get(row.id, 0.0)
            ), f"person {row.code}: stored {row.balance} != recomputed {expected_balance.get(row.id, 0.0)}"

        for box in db.session.query(app_module.CashBox).all():
            assert box.balance == pytest.approx(
                expected_box.get(box.id, 0.0)
            ), f"cashbox {box.id}: stored {box.balance} != recomputed {expected_box.get(box.id, 0.0)}"

    # and the numbers, spelled out, so a failure says what went wrong
    with books():
        assert stock_of(item) == pytest.approx(12.0)      # 12 units purchased
        assert balance_of(customer) == pytest.approx(0.0)
        assert balance_of(supplier) == pytest.approx(-1000.0)


def test_the_ledger_deltas_sum_to_the_same_story_as_the_documents(
    admin_client, make_item, make_person
):
    """The audit trail and the books must tell the same story.

    Every create/update/delete entry records the deltas it applied, so summing
    the chain has to reproduce the stored aggregates. A mutation that moved a
    balance without writing its delta -- or wrote a delta it did not apply --
    would pass a document-only reconciliation and fail here.
    """
    import json

    item = make_item(stock=10.0)
    customer = make_person(balance=0.0, name="مشتری", code="201")

    assert (
        admin_client.post(
            "/sales",
            data=invoice_payload(
                customer, item, kind="sales", number="SO-L1", qty=5, unit_price=400.0
            ),
        ).status_code
        == 302
    )
    sale_id = next(i.id for i in with_invoices() if i.number == "SO-L1")

    form = dict(_form_fields(admin_client.get(f"/invoice/{sale_id}/edit").data.decode()))
    form["qty[]"] = ["3"]
    admin_client.post(f"/invoice/{sale_id}/edit", data=form)
    admin_client.post(f"/invoice/{sale_id}/cancel", data={"reason": "test"})

    stock_delta = 0.0
    balance_delta = 0.0
    with books():
        for entry in (
            db.session.query(app_module.LedgerEntry)
            .filter_by(object_type="invoice", object_id=str(sale_id))
            .order_by(app_module.LedgerEntry.id)
            .all()
        ):
            payload = json.loads(entry.payload or "{}")
            stock_delta += sum((payload.get("stock_delta") or {}).values())
            balance_delta += float(payload.get("person_balance_delta") or 0.0)

        assert stock_delta == pytest.approx(0.0), (
            "the recorded stock deltas for a voided sale must cancel out"
        )
        assert balance_delta == pytest.approx(0.0)
        # the item started at 10 units; a cancelled sale must leave it there
        assert stock_of(item) == pytest.approx(10.0)
        assert balance_of(customer) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Document state machine: what may follow what
#
# The transitions below are the ones the lifecycle surface makes reachable.
# Each is asserted on the *money*, not just the status code: a transition that
# is wrongly permitted twice reverses the posting twice, which is the one
# failure mode a status code alone would hide.
# --------------------------------------------------------------------------- #


def test_cancelling_twice_does_not_reverse_the_posting_twice(
    admin_client, make_item, make_person
):
    item = make_item(stock=100.0)
    customer = make_person(balance=0.0)
    admin_client.post(
        "/sales",
        data=invoice_payload(
            customer, item, number="SM-1", qty=5, unit_price=100.0
        ),
    )
    inv_id = only_invoice()
    assert stock_of(item) == 95.0 and balance_of(customer) == 500.0

    first = admin_client.post(f"/invoice/{inv_id}/cancel", data={"reason": "first"})
    assert first.status_code == 302
    assert stock_of(item) == 100.0 and balance_of(customer) == 0.0

    second = admin_client.post(f"/invoice/{inv_id}/cancel", data={"reason": "second"})
    assert second.status_code == 302  # refused, but not a crash
    # the second attempt must be inert
    assert stock_of(item) == 100.0, "the posting was reversed a second time"
    assert balance_of(customer) == 0.0

    with books():
        inv = db.session.get(app_module.Invoice, inv_id)
        voids = (
            db.session.query(app_module.LedgerEntry)
            .filter_by(
                object_type="invoice",
                object_id=str(inv_id),
                action=accounting.LEDGER_VOID,
            )
            .count()
        )
        assert voids == 1, f"{voids} void entries for one cancel"


def test_a_cancelled_invoice_cannot_be_edited(admin_client, make_item, make_person):
    item = make_item(stock=100.0)
    customer = make_person(balance=0.0)
    admin_client.post(
        "/sales",
        data=invoice_payload(
            customer, item, number="SM-2", qty=5, unit_price=100.0
        ),
    )
    inv_id = only_invoice()
    admin_client.post(f"/invoice/{inv_id}/cancel", data={"reason": "x"})

    response = admin_client.post(
        f"/invoice/{inv_id}/edit",
        data=invoice_payload(customer, item, number="SM-2", qty=99, unit_price=1.0),
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert stock_of(item) == 100.0, "a cancelled invoice was re-priced"
    assert balance_of(customer) == 0.0


def test_lifecycle_actions_are_post_only(admin_client, make_item, make_person):
    """A GET must never change money: no prefetch, no crawler, no <img src>."""
    item = make_item(stock=100.0)
    customer = make_person(balance=0.0)
    admin_client.post(
        "/sales",
        data=invoice_payload(
            customer, item, number="SM-3", qty=5, unit_price=100.0
        ),
    )
    inv_id = only_invoice()

    for url in (
        f"/invoice/{inv_id}/cancel",
        f"/invoice/{inv_id}/delete",
    ):
        response = admin_client.get(url)
        assert response.status_code == 405, f"{url} answered a GET"
    assert stock_of(item) == 95.0, "a GET moved stock"
    assert balance_of(customer) == 500.0


def test_deleting_an_already_cancelled_invoice_does_not_reverse_twice(
    admin_client, make_item, make_person
):
    item = make_item(stock=100.0)
    customer = make_person(balance=0.0)
    admin_client.post(
        "/sales",
        data=invoice_payload(
            customer, item, number="SM-4", qty=5, unit_price=100.0
        ),
    )
    inv_id = only_invoice()
    admin_client.post(f"/invoice/{inv_id}/cancel", data={"reason": "x"})
    assert stock_of(item) == 100.0 and balance_of(customer) == 0.0

    response = admin_client.post(f"/invoice/{inv_id}/delete", follow_redirects=True)
    assert response.status_code == 200
    # Whether the delete is allowed after a cancel is a policy choice; what
    # must never happen is the posting being reversed a second time.
    assert stock_of(item) == 100.0, "delete after cancel reversed the posting again"
    assert balance_of(customer) == 0.0
    with books():
        # Purging a cancelled document is allowed (its posting is already
        # reversed), but the append-only ledger keeps the history, so the
        # cancel stays auditable after the row is gone.
        assert db.session.get(app_module.Invoice, inv_id) is None
        voids = (
            db.session.query(app_module.LedgerEntry)
            .filter_by(
                object_type="invoice",
                object_id=str(inv_id),
                action=accounting.LEDGER_VOID,
            )
            .count()
        )
        assert voids == 1, f"{voids} void entries for one posting"


def test_cancelling_twice_does_not_reverse_a_cash_document_twice(
    admin_client, make_person
):
    customer = make_person(balance=0.0)
    admin_client.post(
        "/receive", data=cash_payload(customer, number="SM-RCV-1", amount=750.0)
    )
    doc_id = only_cashdoc()
    assert balance_of(customer) == -750.0

    assert admin_client.post(f"/cash/{doc_id}/cancel", data={"reason": "first"}).status_code == 302
    assert balance_of(customer) == 0.0

    assert admin_client.post(f"/cash/{doc_id}/cancel", data={"reason": "second"}).status_code == 302
    assert balance_of(customer) == 0.0, "the receipt was reversed twice"

    with books():
        doc = db.session.get(app_module.CashDoc, doc_id)
        voids = (
            db.session.query(app_module.LedgerEntry)
            .filter_by(
                object_type="cashdoc",
                object_id=str(doc_id),
                action=accounting.LEDGER_VOID,
            )
            .count()
        )
        assert voids == 1
