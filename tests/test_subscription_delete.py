"""Abo löschen: Kaskade über alle abhängigen Daten, Namens-Bestätigung."""

from conftest import admin_login

from app.models.models import (
    Booking,
    Event,
    GuestBooking,
    LoginToken,
    Member,
    Payment,
    Person,
    Subscription,
)


def _fill_subscription(db, seed):
    """Buchungen, Gastbuchung, Ledger und Token ans Seed-Abo hängen."""
    from conftest import make_login_token

    db.add(Booking(event_id=seed["event"].id, member_id=seed["member"].id))
    db.add(
        GuestBooking(
            event_id=seed["event"].id, name="G", email="g@x.de", count=1
        )
    )
    db.add(
        Payment(member_id=seed["member"].id, amount=20, note="Einzahlung")
    )
    db.commit()
    make_login_token("anna@example.com")


def test_delete_subscription_cascades(client, db, seed):
    _fill_subscription(db, seed)
    csrf = admin_login(client)
    sub_id, sub_name = seed["sub"].id, seed["sub"].name
    other_id = seed["other_sub"].id

    resp = client.post(
        f"/admin/subscription/{sub_id}/delete",
        data={"confirm_name": sub_name, "csrf_token": csrf},
        follow_redirects=False,
    )
    assert "gel%C3%B6scht" in resp.headers["location"]

    db.expire_all()
    assert db.query(Subscription).filter(Subscription.id == sub_id).count() == 0
    assert db.query(Event).filter(Event.subscription_id == sub_id).count() == 0
    assert db.query(Member).filter(Member.subscription_id == sub_id).count() == 0
    for model in (Booking, GuestBooking, Payment, LoginToken):
        assert db.query(model).count() == 0
    # Anderes Abo und dessen Mitglieder unberührt
    assert db.query(Subscription).filter(Subscription.id == other_id).count() == 1
    assert db.query(Member).filter(Member.subscription_id == other_id).count() == 1


def test_delete_requires_matching_name(client, db, seed):
    csrf = admin_login(client)
    sub = seed["sub"]
    resp = client.post(
        f"/admin/subscription/{sub.id}/delete",
        data={"confirm_name": "falscher name", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert "abgebrochen" in resp.headers["location"]
    db.expire_all()
    assert db.get(Subscription, sub.id) is not None
