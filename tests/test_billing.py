"""Billing: settlement, ledger, credit, member deactivation."""

from decimal import Decimal

from conftest import admin_login, member_login

from app.models.models import Booking, Event, Member, Payment


def _seed_booking(db, seed, guest_count=0, past=True):
    event = seed["past_event"] if past else seed["event"]
    db.add(
        Booking(
            event_id=event.id,
            member_id=seed["member"].id,
            guest_count=guest_count,
        )
    )
    db.commit()
    return event


def test_settle_event_charges_members(client, db, seed):
    event = _seed_booking(db, seed, guest_count=1)
    csrf = admin_login(client)

    resp = client.post(
        f"/admin/event/{event.id}/settle",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    db.expire_all()
    member = db.query(Member).filter(Member.id == seed["member"].id).one()
    # 2 Teilnehmer (Mitglied + 1 Gast): Mitglieder-Anteil 8/2 = 4,00,
    # Gäste-Anteil 10/2 = 5,00 → 20,00 Start − 9,00 = 11,00
    assert member.credit == Decimal("11.00")
    charge = db.query(Payment).filter(Payment.type == Payment.TYPE_CHARGE).one()
    assert charge.amount == Decimal("-9.00")
    assert charge.event_id == event.id
    assert db.get(Event, event.id).settled_at is not None


def test_settle_is_idempotent(client, db, seed):
    event = _seed_booking(db, seed)
    csrf = admin_login(client)
    client.post(f"/admin/event/{event.id}/settle", data={"csrf_token": csrf})
    resp = client.post(
        f"/admin/event/{event.id}/settle",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "bereits%20abgerechnet" in resp.headers["location"]
    db.expire_all()
    assert db.query(Payment).filter(Payment.type == Payment.TYPE_CHARGE).count() == 1
    member = db.query(Member).filter(Member.id == seed["member"].id).one()
    # Allein am Termin: voller Abo-Anteil 8,00 — nur einmal abgezogen
    assert member.credit == Decimal("12.00")


def test_settle_refuses_future_and_cancelled(client, db, seed):
    csrf = admin_login(client)
    future = seed["event"]
    resp = client.post(
        f"/admin/event/{future.id}/settle",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "Zukunft" in resp.headers["location"]

    past = seed["past_event"]
    past.is_cancelled = True
    db.commit()
    resp = client.post(
        f"/admin/event/{past.id}/settle",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "Abgesagte" in resp.headers["location"]
    assert db.query(Payment).count() == 0


def test_unbook_blocked_after_settlement(client, db, seed):
    event = _seed_booking(db, seed, past=False)
    from app.models.models import utcnow

    event.settled_at = utcnow()
    db.commit()

    csrf = member_login(client)
    resp = client.post(
        f"/member/event/{event.id}/unbook",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "abgerechnet" in resp.headers["location"]
    assert db.query(Booking).count() == 1


def test_add_credit_creates_ledger_entry(client, db, seed):
    csrf = admin_login(client)
    member = seed["member"]
    resp = client.post(
        f"/admin/member/{member.id}/add-credit",
        data={"amount": "25.50", "note": "Überweisung", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    db.expire_all()
    m = db.query(Member).filter(Member.id == member.id).one()
    assert m.credit == Decimal("45.50")
    deposit = (
        db.query(Payment)
        .filter(Payment.type == Payment.TYPE_DEPOSIT)
        .order_by(Payment.created_at.desc())
        .first()
    )
    assert deposit.amount == Decimal("25.50")


def test_member_deactivation_via_checkbox(client, db, seed):
    """B1-Fix: fehlende Checkbox im Formular muss deaktivieren."""
    csrf = admin_login(client)
    member = seed["member"]
    resp = client.post(
        f"/admin/member/{member.id}/edit",
        data={
            "name": member.name,
            "email": member.email,
            "password": "",
            "csrf_token": csrf,
            # is_active fehlt = Checkbox nicht angehakt
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    db.expire_all()
    assert db.query(Member).filter(Member.id == member.id).one().is_active is False

    # Deaktiviertes Mitglied bekommt keinen Login-Link
    from conftest import get_csrf

    csrf2 = get_csrf(client, "/member/login")
    resp = client.post(
        "/member/login",
        data={"email": member.email, "csrf_token": csrf2},
    )
    assert "/member/login/t/" not in resp.text


def test_stats_page_shows_spending(client, db, seed):
    event = _seed_booking(db, seed, guest_count=1)
    csrf = admin_login(client)
    client.post(f"/admin/event/{event.id}/settle", data={"csrf_token": csrf})
    resp = client.get(f"/admin/subscription/{seed['sub'].id}/stats")
    assert resp.status_code == 200
    assert "9,00" in resp.text  # Ausgaben des Mitglieds / Umsatz des Termins


def test_member_dashboard_shows_balance_and_ledger(client, db, seed):
    event = _seed_booking(db, seed)
    admin_csrf = admin_login(client)
    client.post(f"/admin/event/{event.id}/settle", data={"csrf_token": admin_csrf})

    member_login(client)
    resp = client.get("/member/dashboard")
    assert resp.status_code == 200
    assert "12,00" in resp.text  # Guthaben 20 − 8
    assert "Teilnahme" in resp.text  # Ledger-Eintrag sichtbar


def test_recompute_budgets_spreads_totals(db, seed):
    """Budgets = Gesamtpreise gleichmäßig über offene reguläre Termine."""
    from app import services

    sub = seed["sub"]
    services.recompute_budgets(db, sub)
    db.commit()
    assert seed["event"].abo_budget == Decimal("8.00")  # 16 / 2
    assert seed["event"].normal_budget == Decimal("10.00")  # 20 / 2


def test_cancel_with_price_reduction_keeps_other_budgets(db, seed):
    """Absagen + Preis reduzieren: Gesamtpreis sinkt, Rest-Budgets bleiben."""
    from app import services

    services.cancel_event(db, seed["past_event"], reduce_price=True)
    db.refresh(seed["sub"])
    assert seed["sub"].abo_price == Decimal("8.00")  # 16 − 8
    assert seed["sub"].default_price == Decimal("10.00")  # 20 − 10
    assert seed["event"].abo_budget == Decimal("8.00")  # unverändert


def test_cancel_with_redistribution_raises_budgets(db, seed):
    """Absagen + umlegen: Gesamtpreis bleibt, Rest-Termine werden teurer."""
    from app import services

    services.cancel_event(db, seed["past_event"], reduce_price=False)
    db.refresh(seed["sub"])
    assert seed["sub"].abo_price == Decimal("16.00")  # unverändert
    assert seed["event"].abo_budget == Decimal("16.00")  # ganzes Budget
    assert seed["event"].normal_budget == Decimal("20.00")


def test_settle_refuses_below_minimum(client, db, seed):
    """Termin unter Mindestteilnehmerzahl kann nicht abgerechnet werden."""
    event = _seed_booking(db, seed)  # 1 Teilnehmer
    event.min_participants = 3
    db.commit()
    csrf = admin_login(client)
    resp = client.post(
        f"/admin/event/{event.id}/settle",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "Mindestteilnehmerzahl" in resp.headers["location"]
    assert db.query(Payment).count() == 0


def test_shares_split_budget_among_participants(client, db, seed):
    """Anteil pro Person = Termin-Budget ÷ Teilnehmerzahl."""
    from app import services

    event = seed["event"]
    # Mitglied mit 2 Gästen + Link-Gast mit 1 Person = 4 Teilnehmer
    db.add(Booking(event_id=event.id, member_id=seed["member"].id, guest_count=2))
    from app.models.models import GuestBooking

    db.add(GuestBooking(event_id=event.id, name="G", email="g@x.de", count=1))
    db.commit()

    shares = services.event_shares(db, event)
    assert shares["participants"] == 4
    assert shares["member_share"] == Decimal("2.00")  # 8 / 4
    assert shares["guest_share"] == Decimal("2.50")  # 10 / 4


def test_redistribution_skips_settled_events(db, seed):
    """Umlegen trifft nur offene Termine — abgerechnete bleiben eingefroren."""
    from datetime import timedelta
    from app import services
    from app.models.models import Event as Ev

    sub = seed["sub"]
    # Drittes Event; Gesamtpreis 24 → je 8 Budget
    third = Ev(
        subscription_id=sub.id,
        date=seed["event"].date + timedelta(days=7),
        start_time=seed["event"].start_time,
        end_time=seed["event"].end_time,
        max_participants=4,
        min_participants=1,
        abo_budget=Decimal("8.00"),
        normal_budget=Decimal("10.00"),
    )
    sub.abo_price = Decimal("24.00")
    sub.default_price = Decimal("30.00")
    db.add(third)
    db.commit()

    # Vergangenen Termin abrechnen → Budget 8 eingefroren
    db.refresh(seed["past_event"])
    services.settle_event(db, seed["past_event"])

    # Mittleren Termin absagen + umlegen
    db.refresh(seed["event"])
    services.cancel_event(db, seed["event"], reduce_price=False)

    db.expire_all()
    assert db.get(Ev, seed["past_event"].id).abo_budget == Decimal("8.00")  # eingefroren
    assert db.get(Ev, third.id).abo_budget == Decimal("16.00")  # 24 − 8 auf 1 offenen
    assert db.get(Ev, third.id).normal_budget == Decimal("20.00")  # 30 − 10
