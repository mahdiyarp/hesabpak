from utils.date_utils import parse_gregorian_date


def test_invalid_invoice_date_is_not_silently_replaced_with_today():
    assert parse_gregorian_date("", allow_none=True) is None
    assert parse_gregorian_date("not-a-date", allow_none=True) is None
    assert parse_gregorian_date("2026-09-29", allow_none=True).isoformat() == "2026-09-29"
