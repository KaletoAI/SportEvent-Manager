"""Zentrale Nutzerablage: Sync bei Anlage/Änderung, Übernahme in andere Abos."""

from conftest import admin_login

from app.models.models import Member, Person


def _create_member(client, csrf, sub_id, name="Tom", email="tom@example.com"):
    return client.post(
        f"/admin/subscription/{sub_id}/members/new",
        data={
            "name": name,
            "email": email,
            "paypal_address": "tom@pay.me",
            "credit": "0",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )


def test_member_creation_populates_directory(client, db, seed):
    csrf = admin_login(client)
    resp = _create_member(client, csrf, seed["sub"].id)
    assert resp.status_code == 302
    person = db.query(Person).filter(Person.email == "tom@example.com").one()
    assert person.name == "Tom"
    assert person.paypal_address == "tom@pay.me"


def test_member_edit_updates_directory(client, db, seed):
    csrf = admin_login(client)
    _create_member(client, csrf, seed["sub"].id)
    member = db.query(Member).filter(Member.email == "tom@example.com").one()
    client.post(
        f"/admin/member/{member.id}/edit",
        data={
            "name": "Thomas",
            "email": "tom@example.com",
            "paypal_address": "",
            "is_active": "true",
            "csrf_token": csrf,
        },
    )
    db.expire_all()
    person = db.query(Person).filter(Person.email == "tom@example.com").one()
    assert person.name == "Thomas"
    assert person.paypal_address == "tom@pay.me"  # bleibt (leer überschreibt nicht)


def test_add_existing_person_to_other_subscription(client, db, seed):
    csrf = admin_login(client)
    _create_member(client, csrf, seed["sub"].id)
    person = db.query(Person).filter(Person.email == "tom@example.com").one()

    # Auswahl erscheint auf der Anlage-Seite des anderen Abos
    resp = client.get(f"/admin/subscription/{seed['other_sub'].id}/members/new")
    assert "Aus Nutzerablage übernehmen" in resp.text
    assert "tom@example.com" in resp.text

    resp = client.post(
        f"/admin/subscription/{seed['other_sub'].id}/members/add-existing",
        data={"person_id": person.id, "credit": "100.00", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert "%C3%BCbernommen" in resp.headers["location"]
    member = (
        db.query(Member)
        .filter(
            Member.subscription_id == seed["other_sub"].id,
            Member.email == "tom@example.com",
        )
        .one()
    )
    assert member.name == "Tom"
    assert member.paypal_address == "tom@pay.me"
    assert str(member.credit) == "100.00"
    # Nur EIN Verzeichniseintrag
    assert db.query(Person).filter(Person.email == "tom@example.com").count() == 1


def test_add_existing_rejects_duplicate(client, db, seed):
    csrf = admin_login(client)
    _create_member(client, csrf, seed["sub"].id)
    person = db.query(Person).filter(Person.email == "tom@example.com").one()
    resp = client.post(
        f"/admin/subscription/{seed['sub'].id}/members/add-existing",
        data={"person_id": person.id, "credit": "0", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert "bereits%20Mitglied" in resp.headers["location"]
    assert (
        db.query(Member).filter(Member.email == "tom@example.com").count() == 1
    )


def test_member_edit_duplicate_email_rejected(client, db, seed):
    """E-Mail-Änderung auf eine im Abo vergebene Adresse → Fehlermeldung, kein 500."""
    csrf = admin_login(client)
    member = seed["member"]  # Anna
    resp = client.post(
        f"/admin/member/{member.id}/edit",
        data={
            "name": member.name,
            "email": "sina@example.com",  # gehört Sina im selben Abo
            "paypal_address": "",
            "is_active": "true",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "bereits" in resp.headers["location"]
    db.expire_all()
    assert db.get(Member, member.id).email == "anna@example.com"  # unverändert


def test_delete_member_without_history(client, db, seed):
    csrf = admin_login(client)
    _create_member(client, csrf, seed["sub"].id)  # Tom, keine Buchungen
    member = db.query(Member).filter(Member.email == "tom@example.com").one()

    resp = client.post(
        f"/admin/member/{member.id}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "gel%C3%B6scht" in resp.headers["location"]
    db.expire_all()
    assert db.query(Member).filter(Member.email == "tom@example.com").count() == 0
    # Nutzerablage behält den Eintrag für spätere Wiederverwendung
    assert db.query(Person).filter(Person.email == "tom@example.com").count() == 1


def test_delete_member_with_history_refused(client, db, seed):
    from app.models.models import Booking

    csrf = admin_login(client)
    member = seed["member"]  # Anna, credit 20 → hat aber noch keine Payments
    db.add(Booking(event_id=seed["event"].id, member_id=member.id))
    db.commit()
    resp = client.post(
        f"/admin/member/{member.id}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert "deaktivieren" in resp.headers["location"]
    db.expire_all()
    assert db.query(Member).filter(Member.id == member.id).count() == 1
