"""Super-Member: Termine absagen (mit Preis-Frage), Zusatztermine, Abrechnen."""

from datetime import date, timedelta
from decimal import Decimal

from conftest import member_login

from app.models.models import Booking, Event, Payment, Subscription


def super_login(client):
    return member_login(client, email="sina@example.com")


def test_normal_member_cannot_use_super_routes(client, db, seed):
    csrf = member_login(client)  # Anna, kein Super
    event = seed["event"]
    for url in (
        f"/member/event/{event.id}/cancel",
        f"/member/event/{event.id}/settle",
    ):
        resp = client.post(url, data={"csrf_token": csrf})
        assert resp.status_code == 403
    # Teilnehmerliste ist bewusst für ALLE Mitglieder des Abos sichtbar,
    # aber ohne Gast-E-Mails (nur Super sieht sie)
    resp = client.get(f"/member/event/{event.id}/participants")
    assert resp.status_code == 200


def test_super_cancel_with_price_reduction(client, db, seed):
    csrf = super_login(client)
    event = seed["event"]
    resp = client.post(
        f"/member/event/{event.id}/cancel",
        data={"reduce_price": "yes", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "reduziert" in resp.headers["location"]
    db.expire_all()
    assert db.get(Event, event.id).is_cancelled is True
    sub = db.get(Subscription, seed["sub"].id)
    assert sub.abo_price == Decimal("8.00")  # 16 − 8


def test_super_cannot_touch_foreign_subscription(client, db, seed):
    """Super-Rechte gelten nur im eigenen Abo."""
    sub2 = seed["other_sub"]
    foreign_event = Event(
        subscription_id=sub2.id,
        date=date.today() + timedelta(days=2),
        start_time=seed["event"].start_time,
        end_time=seed["event"].end_time,
        max_participants=10,
        min_participants=1,
    )
    db.add(foreign_event)
    db.commit()

    csrf = super_login(client)
    resp = client.post(
        f"/member/event/{foreign_event.id}/cancel",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "nicht%20gefunden" in resp.headers["location"]
    db.expire_all()
    assert db.get(Event, foreign_event.id).is_cancelled is False


def test_super_creates_extra_event_outside_totals(client, db, seed):
    csrf = super_login(client)
    extra_date = (date.today() + timedelta(days=10)).isoformat()
    resp = client.post(
        "/member/extra-event",
        data={
            "event_date": extra_date,
            "start_hour": "19",
            "start_minute": "0",
            "duration_minutes": "90",
            "budget": "50.00",
            "min_participants": "4",
            "max_participants": "10",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "Zusatztermin" in resp.headers["location"]

    extra = db.query(Event).filter(Event.is_extra == True).one()  # noqa: E712
    assert extra.abo_budget == Decimal("50.00")
    assert extra.normal_budget == Decimal("50.00")
    # Abo-Gesamtpreis unverändert, reguläre Budgets unverändert
    db.refresh(seed["sub"])
    assert seed["sub"].abo_price == Decimal("16.00")
    assert seed["event"].abo_budget == Decimal("8.00")


def test_super_settles_event_and_everyone_pays_same_share_on_extra(client, db, seed):
    """Zusatztermin: Mitglied + 1 Gast → beide zahlen 50/2 = 25."""
    from app import services

    extra = services.create_extra_event(
        db, seed["sub"], date.today() - timedelta(days=1),
        seed["event"].start_time, 90, Decimal("50.00"), 10, 1,
    )
    db.add(Booking(event_id=extra.id, member_id=seed["member"].id, guest_count=1))
    db.commit()

    csrf = super_login(client)
    resp = client.post(
        f"/member/event/{extra.id}/settle",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    db.expire_all()
    charge = db.query(Payment).filter(Payment.type == Payment.TYPE_CHARGE).one()
    assert charge.amount == Decimal("-50.00")  # 25 selbst + 25 Gast
    assert "Zusatztermin" in charge.note


def test_super_sees_participants(client, db, seed):
    db.add(Booking(event_id=seed["event"].id, member_id=seed["member"].id))
    db.commit()
    super_login(client)
    resp = client.get(f"/member/event/{seed['event'].id}/participants")
    assert resp.status_code == 200
    assert "Anna" in resp.text
    assert "Preisstaffel" in resp.text


def test_price_tiers_ladder(db, seed):
    """Staffel: Preis bei min, min+1, … bis max."""
    from app import services

    event = seed["event"]
    event.min_participants = 2
    db.commit()
    tiers = services.price_tiers(event)
    assert [t["n"] for t in tiers] == [2, 3, 4]
    assert tiers[0]["member"] == Decimal("4.00")  # 8 / 2
    assert tiers[0]["guest"] == Decimal("5.00")  # 10 / 2
    assert tiers[2]["member"] == Decimal("2.00")  # 8 / 4


def test_participants_price_text_by_event_state(client, db, seed):
    """Preis-Text: zukünftig=Maximalpreis, vorbei=ausstehend, abgerechnet=final."""
    from app import services

    member_login(client)  # Anna, kein Super

    resp = client.get(f"/member/event/{seed['event'].id}/participants")
    assert "Maximal" in resp.text  # zukünftiger Termin

    resp = client.get(f"/member/event/{seed['past_event'].id}/participants")
    assert "Abrechnung steht noch aus" in resp.text
    assert "Maximal" not in resp.text

    db.add(Booking(event_id=seed["past_event"].id, member_id=seed["member"].id))
    db.commit()
    services.settle_event(db, seed["past_event"])
    resp = client.get(f"/member/event/{seed['past_event'].id}/participants")
    assert "Abgerechnet mit" in resp.text
    assert "Abrechnung steht noch aus" not in resp.text


def test_guest_link_visible_only_for_super(client, db, seed):
    event = seed["event"]

    super_login(client)
    resp = client.get(f"/member/event/{event.id}/participants")
    assert f"/g/{event.public_token}" in resp.text

    client.get("/member/logout")
    member_login(client)  # Anna, kein Super
    resp = client.get(f"/member/event/{event.id}/participants")
    assert f"/g/{event.public_token}" not in resp.text
