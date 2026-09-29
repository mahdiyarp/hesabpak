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

def test_person_balance_delta_helper_updates_sqlalchemy_entity_atomically():
    with app_module.app.app_context():
        app_module.db.drop_all()
        app_module.db.create_all()
        person = app_module.Entity(
            type="person", code="901", name="تست اتمیک", unit="شرکت", level=1, balance=1000.0
        )
        app_module.db.session.add(person)
        app_module.db.session.commit()

        app_module._adjust_person_balance_delta(person, 250.0)
        assert person.balance == 1250.0
        app_module._adjust_person_balance_delta(person, -400.0)
        assert person.balance == 850.0
        app_module.db.session.commit()