# -*- coding: utf-8 -*-
"""Shared fixtures for the HesabPak test-suite.

Every test gets a private DATA_DIR and a freshly created schema so that no test
can observe or corrupt another test's books.
"""

import contextlib
import json
import os
import tempfile
from pathlib import Path

import pytest

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="hesabpak-tests-")
os.environ.setdefault("DATA_DIR", _TEST_DATA_DIR)

import app as app_module  # noqa: E402  (must follow the DATA_DIR default)
from extensions import db  # noqa: E402


@contextlib.contextmanager
def app_context_if_needed():
    """Reuse the ambient application context, or open a short-lived one.

    Pure-database tests hold a context open via `ctx`. HTTP tests must *not*:
    Flask reuses an already-pushed application context for the request
    contexts the test clients open, so a leaked context would make `flask.g`
    -- and therefore the Flask-Login user cache -- shared by every request in
    the test. The first identity resolved would then silently win for all
    later requests and authorization tests would prove nothing.
    """
    from flask import has_app_context

    if has_app_context():
        yield
    else:
        with app_module.app.app_context():
            yield


@pytest.fixture()
def fresh_schema():
    """An empty schema, with **no** application context held open.

    Leaving a context open here used to make every test client share one
    `flask.g`, so two clients logged in as different users resolved to the
    same user. The context is opened only for the schema work itself.
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
def ctx(fresh_schema):
    """Database-only tests: hold an application context open for the body."""
    with app_module.app.app_context():
        yield


def _write_users_catalog(entries):
    """Persist *entries* (a {username: fields} mapping) in the on-disk format.

    `app.load_users_catalog()` reads ``{"users": [{"username": ...}, ...]}``, so a
    flat mapping would silently yield an empty catalog -- every non-admin user
    would then resolve to anonymous and authorization tests would pass vacuously.
    """
    users_file = Path(app_module.app.config["DATA_DIR"]) / "users.json"
    payload = {
        "users": [
            dict(fields, username=username) for username, fields in entries.items()
        ]
    }
    users_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


@pytest.fixture()
def users_catalog():
    """Install a users.json with one admin and one permission-scoped staff user."""
    _write_users_catalog(
        {
            "admin": {
                "password": app_module.generate_password_hash("admin-pass"),
                "role": "admin",
                "permissions": app_module.ADMIN_PERMISSIONS,
                "is_active": True,
                "email": "mahdiyarp@gmail.com",
            },
            # Staff user that may post and view, but may not void/delete documents.
            "clerk": {
                "password": app_module.generate_password_hash("clerk-pass"),
                "role": "staff",
                "permissions": [
                    "dashboard",
                    "sales",
                    "purchase",
                    "receive",
                    "payment",
                    "reports",
                    "entities",
                ],
                "is_active": True,
                "email": "",
            },
            # Staff user with reports-only access: used to prove that a direct
            # endpoint call cannot be used to bypass UI permissions.
            "auditor": {
                "password": app_module.generate_password_hash("auditor-pass"),
                "role": "limited",
                "permissions": ["dashboard", "reports"],
                "is_active": True,
                "email": "",
            },
            # Sales-only staff: may post sales invoices but has no purchase,
            # receive or payment rights. Used to prove that a hidden form field
            # cannot escalate the document kind beyond the caller's rights.
            "salesclerk": {
                "password": app_module.generate_password_hash("salesclerk-pass"),
                "role": "limited",
                "permissions": ["dashboard", "sales", "entities"],
                "is_active": True,
                "email": "",
            },
        }
    )
    return {
        "admin": "admin-pass",
        "clerk": "clerk-pass",
        "auditor": "auditor-pass",
        "salesclerk": "salesclerk-pass",
    }


def login(client, username):
    """Put *username* into the Flask-Login session.

    Bypassing the login form keeps the tests focused on authorization outcomes
    rather than on password hashing, which has its own tests.
    """
    with client.session_transaction() as sess:
        sess["_user_id"] = username
        sess["_fresh"] = True
        sess["login_at_utc"] = "2026-01-01T00:00:00"
    return client


@pytest.fixture()
def admin_client(fresh_schema, users_catalog):
    return login(app_module.app.test_client(), "admin")


@pytest.fixture()
def staff_client(fresh_schema, users_catalog):
    return login(app_module.app.test_client(), "clerk")


@pytest.fixture()
def auditor_client(fresh_schema, users_catalog):
    return login(app_module.app.test_client(), "auditor")


@pytest.fixture()
def sales_client(fresh_schema, users_catalog):
    """Sales-only user: no purchase / receive / payment permission."""
    return login(app_module.app.test_client(), "salesclerk")


# --------------------------------------------------------------------------- #
# Entity helpers
# --------------------------------------------------------------------------- #


@pytest.fixture()
def make_person(fresh_schema):
    def _make(name="مشتری تست", code="201", balance=0.0):
        with app_context_if_needed():
            person = app_module.Entity(
                type="person",
                code=code,
                name=name,
                unit="شرکت",
                level=1,
                balance=balance,
            )
            db.session.add(person)
            db.session.commit()
            # A commit expires every attribute; reload them while the session
            # is still open so the detached instance stays usable.
            db.session.refresh(person)
            return person

    return _make


@pytest.fixture()
def make_item(fresh_schema):
    def _make(name="کالای تست", code="101", stock=0.0):
        with app_context_if_needed():
            item = app_module.Entity(
                type="item", code=code, name=name, unit="عدد", level=1, stock_qty=stock
            )
            db.session.add(item)
            db.session.commit()
            db.session.refresh(item)
            return item

    return _make


@pytest.fixture()
def make_cashbox(fresh_schema):
    def _make(name="صندوق تست", kind="cash"):
        with app_context_if_needed():
            box = app_module.CashBox(name=name, kind=kind, is_active=True)
            db.session.add(box)
            db.session.commit()
            db.session.refresh(box)
            return box

    return _make


def reload(entity):
    """Re-read an entity from the database, bypassing the identity map."""
    with app_context_if_needed():
        db.session.expire(entity)
        db.session.refresh(entity)
        return entity


def stock_of(item):
    """Current stock for *item*, read fresh from the database."""
    with app_context_if_needed():
        row = db.session.get(app_module.Entity, item.id)
        return float(row.stock_qty or 0.0)


def balance_of(person):
    """Current balance for *person*, read fresh from the database."""
    with app_context_if_needed():
        row = db.session.get(app_module.Entity, person.id)
        return float(row.balance or 0.0)
