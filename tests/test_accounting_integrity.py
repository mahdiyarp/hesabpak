from types import SimpleNamespace

import app as app_module


def test_cash_balance_delta_for_receive():
    person = SimpleNamespace(balance=1000.0)
    app_module._adjust_cash_person_balance("receive", person, 200.0, 350.0)
    assert person.balance == 850.0
    app_module._adjust_cash_person_balance("receive", person, 350.0, 250.0)
    assert person.balance == 950.0


def test_cash_balance_delta_for_payment():
    person = SimpleNamespace(balance=1000.0)
    app_module._adjust_cash_person_balance("payment", person, 200.0, 350.0)
    assert person.balance == 1150.0
    app_module._adjust_cash_person_balance("payment", person, 350.0, 250.0)
    assert person.balance == 1050.0
