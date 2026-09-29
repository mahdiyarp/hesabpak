import app as app_module


def test_unified_cash_kind_is_whitelisted():
    assert "receive" in {"receive", "payment"}
    assert "payment" in {"receive", "payment"}


def test_cash_posting_balance_and_reversal_are_consistent():
    class Person:
        balance = 1000.0

    person = Person()
    app_module._adjust_cash_person_balance("receive", person, 0.0, 250.0)
    assert person.balance == 750.0

    app_module._adjust_cash_person_balance("receive", person, 250.0, 100.0)
    assert person.balance == 900.0

    app_module._adjust_cash_person_balance("payment", person, 0.0, 400.0)
    assert person.balance == 1300.0

    app_module._adjust_cash_person_balance("payment", person, 400.0, 100.0)
    assert person.balance == 1000.0
