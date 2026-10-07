"""Automatische Löschung verwaister Einträge im Personenverzeichnis."""

from datetime import date
import logging

import pytest
from sqlalchemy import event

from app import clock, scheduler, services
from app.config import settings
from app.models.models import Member, Person
from app.schema_upgrade import upgrade


@pytest.fixture(autouse=True)
def retention_settings(monkeypatch):
    monkeypatch.setattr(settings, "data_retention_months", 12)


def test_membership_tracks_latest_end_even_when_inactive(db, seed):
    person = services.upsert_person(db, "Anna", seed["member"].email)
    seed["sub"].end_date = date(2024, 8, 31)
    seed["other_sub"].end_date = date(2024, 10, 31)
    db.add(Member(subscription_id=seed["other_sub"].id,
                  email="ANNA@EXAMPLE.COM", name="Anna", is_active=False))
    db.commit()
    clock.set_override(db, date(2026, 1, 1))

    assert services.purge_stale_persons(db) == 0
    db.refresh(person)
    assert person.last_season_end == date(2024, 10, 31)
    seed["other_sub"].end_date = date(2024, 7, 31)
    db.commit()
    assert services.purge_stale_persons(db) == 0
    db.refresh(person)
    assert person.last_season_end == date(2024, 8, 31)


def test_untracked_person_starts_retention_today(db):
    person = services.upsert_person(db, "Altbestand", "old@example.com")
    db.commit()
    clock.set_override(db, date(2026, 2, 15))
    assert services.purge_stale_persons(db) == 0
    db.refresh(person)
    assert person.last_season_end == date(2026, 2, 15)
    clock.set_override(db, date(2026, 3, 15))
    assert services.purge_stale_persons(db) == 0
    db.refresh(person)
    assert person.last_season_end == date(2026, 2, 15)


@pytest.mark.parametrize("today, deleted", [
    (date(2026, 8, 30), 0),
    (date(2026, 8, 31), 1),
    (date(2026, 9, 1), 1),
])
def test_orphan_retention_boundary(db, today, deleted):
    person = Person(email="orphan@example.com", name="Orphan",
                    last_season_end=date(2025, 8, 31),
                    paypal_address="pay@example.com", calendar_token="secret")
    db.add(person)
    db.commit()
    person_id = person.id
    clock.set_override(db, today)
    assert services.purge_stale_persons(db) == deleted
    db.expire_all()
    assert (db.get(Person, person_id) is None) == bool(deleted)
    assert services.purge_stale_persons(db) == 0


@pytest.mark.parametrize("season_end, months, deadline", [
    (date(2023, 1, 31), 1, date(2023, 2, 28)),
    (date(2024, 1, 31), 1, date(2024, 2, 29)),
    (date(2024, 2, 29), 12, date(2025, 2, 28)),
    (date(2024, 12, 31), 2, date(2025, 2, 28)),
])
def test_month_addition_clamps_to_last_day(db, monkeypatch, season_end, months, deadline):
    from datetime import timedelta

    monkeypatch.setattr(settings, "data_retention_months", months)
    db.add(Person(email="month@example.com", name="Month",
                  last_season_end=season_end))
    db.commit()
    clock.set_override(db, deadline - timedelta(days=1))
    assert services.purge_stale_persons(db) == 0
    clock.set_override(db, deadline)
    assert services.purge_stale_persons(db) == 1


@pytest.mark.parametrize("run_before_delete", [False, True])
def test_deleted_subscription_keeps_person_until_deadline(db, seed, run_before_delete):
    person = services.upsert_person(db, "Anna", seed["member"].email)
    seed["sub"].end_date = date(2025, 8, 31)
    db.commit()
    person_id = person.id
    clock.set_override(db, date(2025, 9, 1))
    if run_before_delete:
        services.purge_stale_persons(db)
    services.delete_subscription(db, seed["sub"])
    db.refresh(person)
    assert person.last_season_end == date(2025, 8, 31)
    assert services.purge_stale_persons(db) == 0
    clock.set_override(db, date(2026, 8, 31))
    assert services.purge_stale_persons(db) == 1
    assert db.get(Person, person_id) is None


def test_email_comparison_and_batched_queries(db, seed):
    seed["member"].email = " ANNA@EXAMPLE.COM "
    db.add(Person(email="Anna@Example.Com", name="Anna"))
    db.add_all([Person(email=f"orphan{i}@example.com", name="Orphan")
                for i in range(20)])
    db.commit()
    clock.set_override(db, date(2026, 1, 1))
    selects = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(db.bind, "before_cursor_execute", record)
    try:
        assert services.purge_stale_persons(db) == 0
    finally:
        event.remove(db.bind, "before_cursor_execute", record)
    assert len(selects) <= 3  # clock, grouped memberships, directory
    person = db.query(Person).filter(Person.email == "Anna@Example.Com").one()
    assert person.last_season_end == seed["sub"].end_date


@pytest.mark.anyio
async def test_scheduler_purges_and_logs_only_count(db, caplog):
    db.add(Person(email="private@example.com", name="Private",
                  last_season_end=date(2024, 1, 1)))
    db.commit()
    with caplog.at_level(logging.INFO, logger="app.scheduler"):
        await scheduler.run_jobs()
    db.expire_all()
    assert db.query(Person).count() == 0
    assert "1 persons deleted" in caplog.text
    assert "private@example.com" not in caplog.text


@pytest.mark.anyio
async def test_scheduler_skips_purge_on_test_date(db):
    """Ein Test-Datum in der Zukunft darf keine echten Einträge löschen."""
    db.add(Person(email="private@example.com", name="Private",
                  last_season_end=date(2024, 1, 1)))
    db.commit()
    clock.set_override(db, date(2030, 1, 1))
    await scheduler.run_jobs()
    db.expire_all()
    assert db.query(Person).count() == 1


def test_schema_upgrade_adds_nullable_retention_column_idempotently(db):
    engine = db.bind
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE persons DROP COLUMN last_season_end")
        conn.exec_driver_sql(
            "INSERT INTO persons (id, email, name, created_at) "
            "VALUES ('legacy', 'old@example.com', 'Old', datetime('now'))"
        )
    upgrade(engine)
    upgrade(engine)
    db.expire_all()
    assert db.get(Person, "legacy").last_season_end is None
