# -*- coding: utf-8 -*-
"""Request idempotency for financial creation.

Document-number uniqueness is not idempotency. It only protects a request that
already carries a number, and it protects it by *failing*, which means the
caller cannot tell a duplicate from a transient database error, and nothing
at all stops a retry of an auto-numbered document.

This module implements the usual idempotency-key contract on top of that:

* the caller generates a token before it knows whether the request will mutate
  anything (the create forms render one into a hidden field);
* the token is claimed in the **same transaction** as the money, so a rollback
  frees the key and a commit makes the replay possible;
* the same key with the same material payload replays the original result
  without touching inventory or balances a second time;
* the same key with a *different* payload is refused, because that is a client
  bug, not a retry.

The interesting part is the loser's path. A losing INSERT must not roll back
the caller's outer transaction -- the caller may already have done work -- so
the insert is attempted inside a SAVEPOINT (``begin_nested``). Only the
savepoint is undone on a uniqueness violation, and the winner's committed row
can then be read within the same outer transaction. That is portable: SQLite
and PostgreSQL both implement savepoints, and the code does not depend on any
particular isolation level.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from extensions import db
from models.accounting_models import IdempotencyKey

# Scope namespaces. A key is only ever compared within its own scope.
SCOPE_INVOICE_CREATE = "invoice.create"
SCOPE_CASH_CREATE = "cash.create"

# How much of the request actually matters. Anything outside this set (the
# idempotency key itself, CSRF/session noise, the submit button's name) is
# excluded, so a browser resend still matches while a changed amount does not.
_MATERIAL_FIELDS = (
    "kind",
    "doc_type",
    "number",
    "doc_number",
    "date",
    "doc_date",
    "person_id",
    "person_code",
    "amount",
    "discount",
    "tax",
    "lines",
)


class IdempotencyConflict(Exception):
    """The key was already used for a materially different request."""


class NumberTaken(Exception):
    """A client-chosen document number lost a race to another request.

    The pre-check in ``allocate_number`` can pass and the insert still collide,
    because the other request commits in between. Translating that into a
    domain error is what keeps the driver message -- table and column names,
    timestamps, a documentation URL -- out of the operator's browser.
    """


@dataclass(frozen=True)
class Claim:
    """The outcome of trying to take ownership of a key.

    ``NEW`` means this request owns the key and must perform the mutation and
    then call :func:`complete`. ``REPLAY`` means an identical request already
    succeeded and the caller must return ``location`` without mutating
    anything. ``DISABLED`` means no key was supplied, so the caller proceeds
    exactly as before -- idempotency is opt-in per call site and degrades to
    the previous behaviour rather than failing.
    """

    status: str
    record: Optional[IdempotencyKey] = None
    location: Optional[str] = None

    @property
    def is_new(self) -> bool:
        return self.status == "NEW"

    @property
    def is_replay(self) -> bool:
        return self.status == "REPLAY"


def fingerprint(payload: Dict[str, Any]) -> str:
    """A stable digest of the material part of a request.

    ``json.dumps`` with sorted keys and no insignificant whitespace makes the
    digest independent of dict ordering, which matters because form data
    arrives in whatever order the browser serialised it.
    """
    material = {
        k: payload.get(k) for k in _MATERIAL_FIELDS if payload.get(k) is not None
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_row(row: Any) -> Dict[str, Any]:
    """Normalise a submitted document row for fingerprinting.

    Quantities and prices arrive as strings from a form and as numbers from the
    assistant, so ``"5"`` and ``5`` must produce the same digest: they describe
    the same financial request.
    """

    def _num(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return str(value)

    if isinstance(row, dict):
        item = row.get("item_id", row.get("entity_id", row.get("item")))
        return {
            "item": str(item) if item is not None else None,
            "qty": _num(row.get("qty")),
            "unit_price": _num(row.get("unit_price", row.get("price"))),
        }
    return {"item": str(row), "qty": None, "unit_price": None}


def request_fingerprint(
    *,
    kind: str,
    number: Optional[str],
    doc_date: Optional[str],
    person_id: Optional[Any],
    amount: Optional[Any] = None,
    discount: Optional[Any] = None,
    tax: Optional[Any] = None,
    lines: Optional[Any] = None,
    doc_type: Optional[str] = None,
) -> str:
    """Build the digest for a document-creation request.

    Lines are compared as a *multiset* because their order carries no financial
    meaning: resending the same document with its rows in a different order is
    the same logical request, and must replay rather than be refused.
    """
    rows: list = []
    if lines:
        if isinstance(lines, dict):
            for key in ("item_id[]", "item_code[]", "qty[]", "unit_price[]"):
                if key in lines:
                    rows.append(lines[key])
        else:
            rows = list(lines)
    normalised = sorted((json.dumps(_canonical_row(r), sort_keys=True) for r in rows))

    def _num(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return str(value)

    return fingerprint(
        {
            "kind": kind or doc_type,
            "number": (number or "").strip() or None,
            "date": (
                doc_date.isoformat() if hasattr(doc_date, "isoformat") else doc_date
            ),
            "person_id": str(person_id) if person_id is not None else None,
            "amount": _num(amount),
            "discount": _num(discount),
            "tax": _num(tax),
            "lines": normalised,
        }
    )


def _insert_ignore_conflict(values: Dict[str, Any]):
    """An INSERT that quietly does nothing when the unique key already exists.

    Returning ``None`` means this dialect has no portable upsert-do-nothing and
    the caller must fall back to the exception path.
    """
    dialect = db.session.get_bind().dialect.name
    try:
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as _insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as _insert
        else:
            return None
    except ImportError:  # pragma: no cover - dialects ship with SQLAlchemy
        return None
    return _insert(IdempotencyKey).values(**values).on_conflict_do_nothing(
        index_elements=["scope", "key"]
    )


def claim(
    scope: str, key: Optional[str], request_fingerprint: str, actor: Optional[str]
) -> Claim:
    """Take ownership of *key*, or report that someone else already has it.

    Must be called inside the transaction that will also carry the financial
    mutation, so that the claim and the money share a fate.

    The insert deliberately does **not** use a SAVEPOINT. Under pysqlite,
    releasing a savepoint commits the work done inside it, so a claim taken in
    one would survive a later rollback -- leaving an unreleasable, permanently
    "in progress" key after a failed posting. ``ON CONFLICT DO NOTHING`` asks
    the database to arbitrate the race instead, needs no exception on the happy
    path, and leaves the claim inside the caller's transaction where a rollback
    can undo it.
    """
    if not key or not str(key).strip():
        return Claim(status="DISABLED")

    key = str(key).strip()
    values = {
        "scope": scope,
        "key": key,
        "fingerprint": request_fingerprint,
        "status": "in_progress",
        "actor": (actor or None),
    }

    statement = _insert_ignore_conflict(values)
    if statement is not None:
        result = db.session.execute(statement)
        if result.rowcount:
            return Claim(
                status="NEW",
                record=db.session.query(IdempotencyKey)
                .filter_by(scope=scope, key=key)
                .one(),
            )
        # Lost the race: the winner's row is already committed and visible.
        existing = (
            db.session.query(IdempotencyKey).filter_by(scope=scope, key=key).first()
        )
        if existing is None:
            # Not a uniqueness problem after all; refuse rather than post money
            # without a guard.
            raise RuntimeError("idempotency claim could not be established")
        return _replay_or_conflict(existing, request_fingerprint, actor)

    # Fallback for a dialect without upsert-do-nothing: let the unique
    # constraint raise, and undo only this statement with a savepoint.
    record = IdempotencyKey(**values)
    try:
        with db.session.begin_nested():
            db.session.add(record)
            db.session.flush()
    except Exception:  # noqa: BLE001 - narrowed below by the re-read
        existing = (
            db.session.query(IdempotencyKey).filter_by(scope=scope, key=key).first()
        )
        if existing is None:
            raise
        return _replay_or_conflict(existing, request_fingerprint, actor)
    return Claim(status="NEW", record=record)


def _replay_or_conflict(
    existing: IdempotencyKey, request_fingerprint: str, actor: Optional[str]
) -> Claim:
    """Decide whether an existing claim may be replayed, or must be refused."""
    if existing.fingerprint != request_fingerprint:
        raise IdempotencyConflict(
            "این کلید تکرارپذیری قبلاً برای درخواستی با محتوای متفاوت "
            "استفاده شده است."
        )
    if existing.actor and actor and existing.actor != actor:
        # Never hand one operator the result of another operator's request.
        raise IdempotencyConflict("این کلید تکرارپذیری به کاربر دیگری تعلق دارد.")
    return Claim(status="REPLAY", record=existing, location=existing.result_location)



def complete(
    claim_result: Claim, *, object_type: str, object_id: Any, location: str
) -> None:
    """Record what the guarded mutation produced, inside the same transaction."""
    if claim_result.status != "NEW" or claim_result.record is None:
        return
    claim_result.record.status = "succeeded"
    claim_result.record.object_type = object_type
    claim_result.record.object_id = str(object_id)
    claim_result.record.result_location = location
    claim_result.record.completed_at = datetime.utcnow()  # type: ignore[attr-defined]
    db.session.add(claim_result.record)
    db.session.flush()


def key_from_request(form: Any, headers: Any = None) -> Optional[str]:
    """Read the caller's token, preferring the header over the form field.

    A header is what an API client or a fetch-based retry can keep constant
    across attempts; a hidden form field is what a plain HTML form can carry.
    Both are accepted so the contract does not depend on the client type.
    """
    for source in (headers, form):
        if source is None:
            continue
        try:
            value = source.get("Idempotency-Key") or source.get("idempotency_key")
        except Exception:  # noqa: BLE001 - a missing mapping is not fatal
            value = None
        if value and str(value).strip():
            return str(value).strip()
    return None


def canonical_form_payload(form: Any) -> Dict[str, Any]:
    """Collect the repeated form arrays that describe an invoice's lines."""
    return {
        "item_id[]": list(form.getlist("item_id[]")),
        "item_code[]": list(form.getlist("item_code[]")),
        "qty[]": list(form.getlist("qty[]")),
        "unit_price[]": list(form.getlist("unit_price[]")),
    }


def new_key() -> str:
    """A fresh token for a freshly rendered form."""
    import uuid

    return uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# Guarded posting
# --------------------------------------------------------------------------- #
# A generated number can be claimed by a concurrent request between the moment
# it is chosen and the moment the row is inserted. Rather than guessing, the
# UNIQUE constraint on the document number arbitrates: the loser sees the
# collision, and only a *generated* number is re-allocated and retried. An
# explicitly typed number is never silently swapped for another one.
MAX_NUMBER_ATTEMPTS = 5


@dataclass
class Outcome:
    """What :func:`guard_and_post` did."""

    replayed: bool
    result: Any
    location: Optional[str] = None


def guard_and_post(
    *,
    scope: str,
    key: Optional[str],
    request_fingerprint: str,
    actor: Optional[str],
    allocate_number,
    post,
    object_type: str,
    location_for,
):
    """Claim a key, post the document, and complete the claim atomically.

    ``allocate_number()`` must return ``(number, is_generated)``. A generated
    number that loses a race is re-allocated and the whole unit of work is
    retried, which is safe because the failed attempt rolled back completely.
    """
    from utils import accounting

    last_error: Optional[Exception] = None
    for _attempt in range(MAX_NUMBER_ATTEMPTS):
        # Only a failure *after* a generated number was allocated may be retried;
        # a failure while claiming the key, or with a client-chosen number, is
        # reported rather than papered over.
        generated = False
        try:
            with accounting.atomic():
                claimed = claim(scope, key, request_fingerprint, actor)
                if claimed.is_replay:
                    return Outcome(
                        replayed=True,
                        result=claimed.record,
                        location=claimed.location,
                    )
                number, generated = allocate_number()
                result = post(number)
                location = location_for(result)
                complete(
                    claimed,
                    object_type=object_type,
                    object_id=getattr(result, "id", None),
                    location=location,
                )
            return Outcome(replayed=False, result=result, location=location)
        except IdempotencyConflict:
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised unless retryable
            name = type(exc).__name__
            if "IntegrityError" not in name and "OperationalError" not in name:
                raise
            if not generated:
                # A number the operator typed must never be swapped for another
                # one, so this is reported as a taken number rather than
                # silently re-allocated.
                raise NumberTaken(
                    f"شماره سند «{number}» قبلاً ثبت شده است."
                ) from None
            last_error = exc
    raise last_error if last_error else RuntimeError("unable to post document")


def free_invoice_number(kind: str = "sales") -> str:
    """An invoice number that is not currently in use.

    ``generate_invoice_number`` derives its value from the clock, so a manually
    entered number can already occupy the slot it picked. This walks forward
    from the highest allocated id. It is only the *first* guess: if a
    concurrent request takes it, :func:`guard_and_post` retries.
    """
    from models.accounting_models import Invoice

    highest = (
        db.session.query(db.func.coalesce(db.func.max(Invoice.id), 0)).scalar() or 0
    )
    for offset in range(1, 100000):
        candidate = f"{int(highest) + offset:08d}"
        if not Invoice.query.filter_by(number=candidate).first():
            return candidate
    raise RuntimeError("unable to allocate a free invoice number")
