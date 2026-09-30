# -*- coding: utf-8 -*-
"""The reconciliation engine must detect corruption, not merely agree with it.

A checker built from the same helpers as the code it checks will happily
confirm a wrong answer. These tests therefore do two things: they plant real
damage and require a specific finding, and they mutate the *poster* to prove
the checker disagrees with it.
"""

import contextlib
import json
import os
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import text

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="hesabpak-recon-")
os.environ.setdefault("DATA_DIR", _TEST_DATA_DIR)

import app as app_module  # noqa: E402
from extensions import db  # noqa: E402
from models.accounting_models import Entity  # noqa: E402
from models.accounting_models import (CashBox, CashDoc, Invoice, InvoiceLine,
                                      LedgerEntry)
from utils import accounting, reconciliation  # noqa: E402


@pytest.fixture()
def books():
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


def seed(item_stock=100.0, person_balance=0.0):
    """An item, a party and a cashbox, created outside the ledger on purpose."""
    with ctx():
        item = Entity(
            type="item",
            code="101",
            name="کالا",
            unit="عدد",
            level=1,
            stock_qty=item_stock,
        )
        person = Entity(
            type="person",
            code="201",
            name="مشتری",
            unit="شرکت",
            level=1,
            balance=person_balance,
        )
        box = CashBox(name="صندوق", kind="cash", is_active=True, account_no="IR-1")
        db.session.add_all([item, person, box])
        db.session.commit()
        return item.id, person.id, box.id


def codes(report):
    return {f.code for f in report.findings}


def run_check():
    with ctx():
        return reconciliation.run()


def post_sale(item_id, person_id, qty=5.0, price=100.0, number=None):
    """Post a sales invoice; returns the invoice ID."""
    with ctx():
        item = db.session.get(Entity, item_id)
        person = db.session.get(Entity, person_id)
        inv = accounting.post_invoice(
            kind="sales",
            number=number or "R-1",
            doc_date=app_module.datetime.utcnow().date(),
            person=person,
            lines=[{"item": item, "qty": qty, "unit_price": price}],
        )
        return inv.id


def post_receipt(person_id, box_id, amount=500.0, number="C-1"):
    """Post a cash receipt; returns the document ID."""
    with ctx():
        person = db.session.get(Entity, person_id)
        box = db.session.get(CashBox, box_id)
        doc = accounting.post_cashdoc(
            doc_type="receive",
            number=number,
            doc_date=app_module.datetime.utcnow().date(),
            person=person,
            amount=amount,
            method="cash",
            cashbox=box,
        )
        return doc.id


# --------------------------------------------------------------------------- #
# A healthy book must be quiet
# --------------------------------------------------------------------------- #


def test_a_freshly_posted_book_reconciles_clean(books):
    item_id, person_id, box_id = seed(item_stock=100.0)
    post_sale(item_id, person_id, qty=5.0, price=100.0)
    post_receipt(person_id, box_id, amount=500.0)

    report = run_check()
    assert report.ok, [f.as_dict() for f in report.findings]
    assert report.counts["critical"] == 0


def test_a_voided_document_leaves_a_clean_book(books):
    item_id, person_id, _box = seed(item_stock=100.0)
    inv_id = post_sale(item_id, person_id, qty=5.0, price=100.0)
    with ctx():
        inv = db.session.get(Invoice, inv_id)
        accounting.void_invoice(inv, reason="تست")
    report = run_check()
    assert report.ok, [f.as_dict() for f in report.findings]


def test_the_checker_labels_an_opening_balance_as_expected_not_corruption(books):
    """Seeded stock has no documents behind it, and that is not damage."""
    item_id, _person_id, _box = seed(item_stock=42.0)
    report = run_check()
    stock_findings = [f for f in report.findings if f.object_type == "entity"]
    assert stock_findings, "the seeded stock produced no finding at all"
    for finding in stock_findings:
        assert finding.code == "opening_balance.unverifiable"
        assert finding.classification == reconciliation.CLASS_EXPECTED
        assert finding.severity == reconciliation.SEV_INFO
    assert report.ok, "an opening balance must not make the run fail"


# --------------------------------------------------------------------------- #
# Corruption must be detected
# --------------------------------------------------------------------------- #


def test_it_detects_stock_drift(books):
    item_id, person_id, _box = seed(item_stock=100.0)
    post_sale(item_id, person_id, qty=5.0, price=100.0)
    with ctx():
        item = db.session.get(Entity, item_id)
        item.stock_qty = 999.0
        db.session.commit()

    report = run_check()
    drift = [f for f in report.findings if f.code == "stock.drift"]
    assert len(drift) == 1, [f.as_dict() for f in report.findings]
    assert drift[0].severity == reconciliation.SEV_CRITICAL
    assert drift[0].classification == reconciliation.CLASS_CORRUPTION
    assert drift[0].expected == pytest.approx(95.0)
    assert drift[0].actual == pytest.approx(999.0)
    assert not report.ok


def test_it_detects_party_balance_drift(books):
    item_id, person_id, _box = seed(item_stock=100.0)
    post_sale(item_id, person_id, qty=5.0, price=100.0)
    with ctx():
        person = db.session.get(Entity, person_id)
        person.balance = 1.0
        db.session.commit()

    report = run_check()
    drift = [f for f in report.findings if f.code == "balance.drift"]
    assert len(drift) == 1
    assert drift[0].expected == pytest.approx(500.0)
    assert drift[0].actual == pytest.approx(1.0)


def test_it_detects_a_document_with_no_ledger_entry(books):
    item_id, person_id, _box = seed(item_stock=100.0)
    # First touch captures the opening baseline and creates ledger entries.
    post_sale(item_id, person_id, qty=5.0, price=100.0)
    with ctx():
        # A document inserted behind the poster's back: real money moved in
        # the aggregate columns with no audit trail whatsoever.
        item = db.session.get(Entity, item_id)
        person = db.session.get(Entity, person_id)
        item.stock_qty = 40.0
        person.balance = 700.0
        inv = Invoice(
            number="GHOST-1",
            kind="sales",
            date=app_module.datetime.utcnow().date(),
            person_id=person.id,
            total=700.0,
            status="active",
        )
        db.session.add(inv)
        db.session.flush()
        db.session.add(
            InvoiceLine(
                invoice_id=inv.id,
                item_id=item.id,
                qty=60.0,
                unit_price=100.0,
                line_total=6000.0,
            )
        )
        db.session.commit()

    report = run_check()
    assert "ledger.missing_entry" in codes(report)
    missing = [f for f in report.findings if f.code == "ledger.missing_entry"][0]
    assert missing.severity == reconciliation.SEV_CRITICAL
    assert missing.classification == reconciliation.CLASS_CORRUPTION


def test_it_labels_a_pre_ledger_document_as_historical_import(books):
    """With no ledger at all, missing entries are expected, not corruption.

    The ledger was introduced after the documents, so a database that has
    never written an entry must not be reported as corrupt.
    """
    with ctx():
        person = Entity(type="person", code="201", name="قدیمی", unit="شرکت", level=1)
        item = Entity(
            type="item", code="101", name="قدیمی", unit="عدد", level=1, stock_qty=10.0
        )
        db.session.add_all([person, item])
        db.session.flush()
        inv = Invoice(
            number="OLD-1",
            kind="sales",
            date=app_module.datetime.utcnow().date(),
            person_id=person.id,
            total=100.0,
            status="active",
        )
        db.session.add(inv)
        db.session.flush()
        db.session.add(
            InvoiceLine(
                invoice_id=inv.id,
                item_id=item.id,
                qty=1.0,
                unit_price=100.0,
                line_total=100.0,
            )
        )
        db.session.commit()
        assert db.session.query(LedgerEntry).count() == 0

    report = run_check()
    missing = [f for f in report.findings if f.code == "ledger.missing_entry"]
    assert missing
    for finding in missing:
        assert finding.classification == reconciliation.CLASS_HISTORICAL
        assert finding.severity == reconciliation.SEV_WARNING
    assert not any(f.code == "ledger.chain_broken" for f in report.findings)


def test_it_detects_a_tampered_ledger_payload(books):
    """Editing a recorded delta is tampering, and must not pass unnoticed."""
    item_id, person_id, _box = seed(item_stock=100.0)
    post_sale(item_id, person_id, qty=5.0, price=100.0)
    with ctx():
        entry = (
            db.session.query(LedgerEntry)
            .filter_by(object_type="invoice", action="create")
            .first()
        )
        payload = json.loads(entry.payload)
        payload["person_balance_delta"] = 99999.0
        entry.payload = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        db.session.commit()

    report = run_check()
    assert "ledger.party_effect_mismatch" in codes(report)
    # Tampering with the payload also breaks the hash it was signed with.
    assert "ledger.chain_broken" in codes(report)
    assert not report.ok


def test_it_detects_a_broken_hash_chain(books):
    item_id, person_id, _box = seed(item_stock=100.0)
    post_sale(item_id, person_id, qty=5.0, price=100.0)
    post_sale(item_id, person_id, qty=1.0, price=100.0, number="R-2")
    with ctx():
        entry = db.session.query(LedgerEntry).order_by(LedgerEntry.id).first()
        entry.payload = json.dumps({"tampered": True})
        db.session.commit()

    report = run_check()
    broken = [f for f in report.findings if f.code == "ledger.chain_broken"]
    assert len(broken) == 1
    assert broken[0].severity == reconciliation.SEV_CRITICAL


def test_it_detects_a_voided_document_still_counting(books):
    item_id, person_id, _box = seed(item_stock=100.0)
    inv_id = post_sale(item_id, person_id, qty=5.0, price=100.0)
    with ctx():
        inv = db.session.get(Invoice, inv_id)
        accounting.void_invoice(inv, reason="تست")
        # Put the document back to 'active' while leaving the reversal in place.
        row = db.session.get(Invoice, inv_id)
        row.status = "active"
        db.session.commit()

    report = run_check()
    found = codes(report)
    assert (
        "ledger.party_effect_mismatch" in found
    ), "a re-activated voided document leaves the ledger netting to zero"


def test_it_detects_a_duplicate_document_number(books):
    item_id, person_id, _box = seed(item_stock=100.0)
    post_sale(item_id, person_id, qty=5.0, price=100.0, number="DUP-1")
    # Bypass the ORM unique constraint so two documents share one number:
    # rebuild the table without the constraint, re-insert both rows.
    with ctx():
        db.session.execute(
            text("CREATE TABLE invoices_backup AS SELECT * FROM invoices")
        )
        db.session.execute(text("DROP TABLE invoices"))
        db.session.execute(
            text(
                "CREATE TABLE invoices ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "number TEXT NOT NULL, "
                "date DATE NOT NULL, "
                "person_id INTEGER NOT NULL, "
                "kind TEXT NOT NULL DEFAULT 'sales', "
                "discount FLOAT NOT NULL DEFAULT 0, "
                "tax FLOAT NOT NULL DEFAULT 0, "
                "total FLOAT NOT NULL DEFAULT 0, "
                "created_at DATETIME NOT NULL, "
                "revision INTEGER NOT NULL DEFAULT 0, "
                "status TEXT NOT NULL DEFAULT 'active', "
                "voided_at DATETIME, "
                "void_reason TEXT, "
                "FOREIGN KEY (person_id) REFERENCES entities(id)"
                ")"
            )
        )
        db.session.execute(text("INSERT INTO invoices SELECT * FROM invoices_backup"))
        db.session.execute(text("DROP TABLE invoices_backup"))
        db.session.commit()
        db.session.execute(
            text(
                "INSERT INTO invoices "
                "(number, date, person_id, kind, discount, tax, total, "
                "created_at, revision, status) "
                "VALUES (:num, :date, :person, 'sales', 0, 0, 100, "
                ":date, 0, 'active')"
            ).params(
                num="DUP-1",
                date=app_module.datetime.utcnow().date().isoformat(),
                person=person_id,
            )
        )
        db.session.commit()

    report = run_check()
    assert "numbering.duplicate" in codes(report)


def test_it_detects_an_unknown_status(books):
    item_id, person_id, _box = seed(item_stock=100.0)
    inv_id = post_sale(item_id, person_id, qty=5.0, price=100.0)
    with ctx():
        row = db.session.get(Invoice, inv_id)
        row.status = "archived"
        db.session.commit()

    report = run_check()
    assert "state.unknown_status" in codes(report)


def test_it_detects_a_cash_document_with_a_missing_cashbox(books):
    _item, person_id, box_id = seed()
    doc_id = post_receipt(person_id, box_id, amount=500.0)
    with ctx():
        row = db.session.get(CashDoc, doc_id)
        row.cashbox_id = 99999
        db.session.commit()

    report = run_check()
    assert "ref.cashbox_missing" in codes(report)


def test_it_detects_an_invoice_total_that_disagrees_with_its_lines(books):
    item_id, person_id, _box = seed(item_stock=100.0)
    inv_id = post_sale(item_id, person_id, qty=5.0, price=100.0)
    with ctx():
        row = db.session.get(Invoice, inv_id)
        row.total = 777.0
        db.session.commit()

    report = run_check()
    assert "state.total_mismatch" in codes(report)


# --------------------------------------------------------------------------- #
# The checker must not be fooled by a wrong poster
# --------------------------------------------------------------------------- #


def test_the_checker_disagrees_with_a_wrong_poster(books, monkeypatch):
    """Flip the poster's sale sign: the checker must report mismatch.

    This is the whole justification for restating the sign table instead of
    calling the poster's helpers. If the checker used them, this mutation would
    produce a clean report and the bug would ship.
    """
    item_id, person_id, _box = seed(item_stock=100.0)
    post_sale(item_id, person_id, qty=5.0, price=100.0)

    assert run_check().ok, "the book is clean before the mutation"

    monkeypatch.setattr(accounting, "invoice_stock_sign", lambda kind: +1.0)
    post_sale(item_id, person_id, qty=2.0, price=100.0, number="R-9")

    report = run_check()
    assert "ledger.stock_effect_mismatch" in codes(
        report
    ), "a poster that adds stock on a sale went unnoticed"


def test_the_checker_disagrees_with_a_wrong_party_sign(books, monkeypatch):
    item_id, person_id, _box = seed(item_stock=100.0)
    post_sale(item_id, person_id, qty=5.0, price=100.0)
    assert run_check().ok

    monkeypatch.setattr(accounting, "invoice_balance_sign", lambda kind: -1.0)
    post_sale(item_id, person_id, qty=2.0, price=100.0, number="R-9")

    report = run_check()
    assert "ledger.party_effect_mismatch" in codes(report)


def test_the_checker_disagrees_with_a_wrong_cash_sign(books, monkeypatch):
    _item, person_id, box_id = seed()
    post_receipt(person_id, box_id, amount=500.0)
    assert run_check().ok

    # Wrong party sign for receives: poster says receive *increases*
    # the party balance, but the checker expects the opposite.
    monkeypatch.setattr(accounting, "cash_balance_sign", lambda doc_type: +1.0)
    post_receipt(person_id, box_id, amount=100.0, number="C-9")

    report = run_check()
    found = codes(report)
    assert "ledger.party_effect_mismatch" in found


# --------------------------------------------------------------------------- #
# Read-only guarantee
# --------------------------------------------------------------------------- #


def test_running_the_check_changes_nothing(books):
    """A checker that mutates the database is worse than no checker."""
    item_id, person_id, box_id = seed(item_stock=100.0)
    post_sale(item_id, person_id, qty=5.0, price=100.0)
    post_receipt(person_id, box_id, amount=500.0)

    with ctx():
        before = _snapshot()
    run_check()
    run_check()
    with ctx():
        after = _snapshot()

    assert before == after, "the reconciliation run modified the database"


def _snapshot():
    """A fingerprint of every business row the checker reads."""
    return {
        "invoices": [
            (i.id, i.number, i.kind, round(float(i.total or 0), 6), i.status)
            for i in db.session.query(Invoice).order_by(Invoice.id).all()
        ],
        "lines": [
            (l.id, l.invoice_id, l.item_id, round(float(l.qty or 0), 6))
            for l in db.session.query(InvoiceLine).order_by(InvoiceLine.id).all()
        ],
        "cash_docs": [
            (d.id, d.number, d.doc_type, round(float(d.amount or 0), 6), d.status)
            for d in db.session.query(CashDoc).order_by(CashDoc.id).all()
        ],
        "entities": [
            (e.id, round(float(e.stock_qty or 0), 6), round(float(e.balance or 0), 6))
            for e in db.session.query(Entity).order_by(Entity.id).all()
        ],
        "ledger": [
            (e.id, e.action, e.hash)
            for e in db.session.query(LedgerEntry).order_by(LedgerEntry.id).all()
        ],
    }


# --------------------------------------------------------------------------- #
# CLI contract
# --------------------------------------------------------------------------- #


def test_the_cli_exits_zero_on_a_clean_book(books, capsys):
    from utils import reconcile

    item_id, person_id, box_id = seed(item_stock=100.0)
    post_sale(item_id, person_id, qty=5.0, price=100.0)
    post_receipt(person_id, box_id, amount=500.0)

    code = reconcile.main(["--quiet"])
    assert code == reconcile.EXIT_OK


def test_the_cli_exits_nonzero_when_something_is_wrong(books):
    from utils import reconcile

    item_id, person_id, _box = seed(item_stock=100.0)
    post_sale(item_id, person_id, qty=5.0, price=100.0)
    with ctx():
        db.session.get(Entity, item_id).stock_qty = 1.0
        db.session.commit()

    assert reconcile.main(["--quiet"]) == reconcile.EXIT_FINDINGS


def test_the_cli_emits_valid_json(books, capsys):
    from utils import reconcile

    item_id, person_id, _box = seed(item_stock=100.0)
    post_sale(item_id, person_id, qty=5.0, price=100.0)
    with ctx():
        db.session.get(Entity, item_id).stock_qty = 1.0
        db.session.commit()

    reconcile.main(["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["counts"]["critical"] >= 1
    assert any(f["code"] == "stock.drift" for f in payload["findings"])
    assert "checked" in payload


def test_the_text_report_names_the_finding(books, capsys):
    from utils import reconcile

    item_id, person_id, _box = seed(item_stock=100.0)
    post_sale(item_id, person_id, qty=5.0, price=100.0)
    with ctx():
        db.session.get(Entity, item_id).stock_qty = 1.0
        db.session.commit()

    reconcile.main([])
    out = capsys.readouterr().out
    assert "stock.drift" in out
    assert "critical" in out
