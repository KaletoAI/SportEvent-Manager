#!/usr/bin/env python3
"""Mobile-Screenshots für docs/usermanual.html erzeugen.

Startet eine eigene Uvicorn-Instanz gegen die Demo-Datenbank aus
`scripts/demo_seed.py` (Scheduler aus, SMTP leer — es gehen keine Mails
raus) und fotografiert die Seiten im Viewport eines Smartphones.

    source .venv/bin/activate
    python scripts/demo_seed.py
    python scripts/make_screenshots.py

Voraussetzung: `pip install playwright && playwright install chromium`.
Die PNGs landen in `docs/screenshots/`; anschließend baut
`scripts/build_manual.py` sie in das Handbuch ein.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEMO_DB = os.environ.get("DEMO_DB", str(ROOT / "data" / "demo.db"))
DATABASE_URL = f"sqlite:///{DEMO_DB}"
OUT_DIR = Path(os.environ.get("SHOT_DIR", ROOT / "docs" / "screenshots"))
ADMIN_PASSWORD = "demo-admin-passwort"

VIEWPORT = {"width": 390, "height": 844}
SCALE = 2
# Obergrenze für den mitwachsenden Viewport, damit einzelne Bilder nicht
# unlesbar lang werden.
MAX_HEIGHT = 2400
# Adresse, die im Gastlink-Screenshot stehen soll (statt der des lokalen
# Testservers).
PUBLIC_DOMAIN = "https://sportevent.example.com"

os.environ["DATABASE_URL"] = DATABASE_URL


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"Server unter {url} nicht erreichbar")


def start_server(port: int) -> subprocess.Popen:
    env = {
        **os.environ,
        "DATABASE_URL": DATABASE_URL,
        "ENABLE_SCHEDULER": "false",
        "SMTP_HOST": "",  # ohne SMTP verschickt die App nichts
        "APP_ENV": "dev",
        "ADMIN_PASSWORD": ADMIN_PASSWORD,
        "COOKIE_SECURE": "false",
        "BASE_URL": f"http://127.0.0.1:{port}",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    wait_for(f"http://127.0.0.1:{port}/member/login")
    return proc


def demo_context() -> dict:
    """Sessions und IDs, die das Skript zum Navigieren braucht."""
    from app.auth import create_session
    from app.database import SessionLocal
    from app.models.models import Event, Member, Subscription

    db = SessionLocal()
    sub = db.query(Subscription).first()
    if sub is None:
        raise SystemExit("Demo-DB ist leer — erst scripts/demo_seed.py laufen lassen.")

    def member(email: str) -> Member:
        return db.query(Member).filter(Member.email == email).first()

    superm = member("lena.sommer@example.com")
    normal = member("jonas.berg@example.com")
    minus = member("paul.brenner@example.com")

    from datetime import date

    upcoming = (
        db.query(Event)
        .filter(Event.subscription_id == sub.id, Event.date >= date.today())
        .order_by(Event.date)
        .all()
    )
    # Aufsteigend: der älteste vergangene Termin ist abgerechnet und hat
    # den bereits bezahlten Link-Gast — der zeigt die Teilnehmerliste am besten.
    past = (
        db.query(Event)
        .filter(Event.subscription_id == sub.id, Event.date < date.today())
        .order_by(Event.date)
        .all()
    )

    ctx = {
        "sub_id": sub.id,
        "admin_token": create_session(db, is_admin=True).token,
        "super_token": create_session(db, member_id=superm.id).token,
        "member_token": create_session(db, member_id=normal.id).token,
        "minus_token": create_session(db, member_id=minus.id).token,
        "next_event_id": upcoming[0].id,
        "next_event_token": upcoming[0].public_token,
        "full_event_id": upcoming[1].id,
        "settled_event_id": past[0].id,
    }
    db.close()
    return ctx


def main() -> None:
    from playwright.sync_api import sync_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server = start_server(port)
    shots = 0

    try:
        ctx = demo_context()

        with sync_playwright() as p:
            browser = p.chromium.launch()
            bctx = browser.new_context(
                viewport=VIEWPORT,
                device_scale_factor=SCALE,
                locale="de-DE",
                is_mobile=True,
                has_touch=True,
            )
            page = bctx.new_page()

            def login_as(token: str) -> None:
                bctx.clear_cookies()
                bctx.add_cookies([{
                    "name": "session", "value": token,
                    "domain": "127.0.0.1", "path": "/",
                }])

            def capture(name: str) -> None:
                """Bild in Viewport-Größe schreiben.

                Kein `full_page`: Die Tab-Leiste und die Kopfzeile sind
                `position: fixed` und würden sonst mehrfach mitten ins
                Bild gerendert. Stattdessen wächst der Viewport auf die
                Seitenhöhe — das Ergebnis sieht aus wie ein sehr langes
                Handy-Display, mit den Leisten genau einmal am Rand.
                """
                nonlocal shots
                height = page.evaluate("document.documentElement.scrollHeight")
                page.set_viewport_size(
                    {"width": VIEWPORT["width"], "height": min(height, MAX_HEIGHT)}
                )
                time.sleep(0.25)
                page.screenshot(path=str(OUT_DIR / f"{name}.png"))
                page.set_viewport_size(VIEWPORT)
                shots += 1
                print(f"  ✓ {name}.png")

            def shot(name: str, url: str, prepare=None,
                     wait: float = 0.35) -> None:
                page.goto(base + url, wait_until="networkidle")
                if prepare:
                    prepare()
                time.sleep(wait)
                capture(name)

            def shot_clip(name: str, url: str, height: int,
                          prepare=None) -> None:
                """Nur den oberen Ausschnitt einer Seite aufnehmen —
                für Bilder, bei denen der Kopfbereich die Aussage trägt."""
                nonlocal shots
                page.goto(base + url, wait_until="networkidle")
                if prepare:
                    prepare()
                time.sleep(0.35)
                page.set_viewport_size(
                    {"width": VIEWPORT["width"], "height": height}
                )
                time.sleep(0.25)
                page.screenshot(path=str(OUT_DIR / f"{name}.png"))
                page.set_viewport_size(VIEWPORT)
                shots += 1
                print(f"  ✓ {name}.png")

            def shot_element(name: str, url: str, selector: str,
                             prepare=None, index: int = 0) -> None:
                """Ein einzelnes Element fotografieren (z. B. eine Karte)."""
                nonlocal shots
                page.goto(base + url, wait_until="networkidle")
                if prepare:
                    prepare()
                time.sleep(0.35)
                page.locator(selector).nth(index).screenshot(
                    path=str(OUT_DIR / f"{name}.png")
                )
                shots += 1
                print(f"  ✓ {name}.png")

            def shot_split(names: tuple, url: str, prepare=None) -> None:
                """Sehr lange Seiten (Formulare) in zwei Bilder teilen."""
                page.goto(base + url, wait_until="networkidle")
                if prepare:
                    prepare()
                time.sleep(0.35)
                height = page.evaluate("document.documentElement.scrollHeight")
                half = height // 2 + 40
                page.set_viewport_size(
                    {"width": VIEWPORT["width"], "height": min(half, MAX_HEIGHT)}
                )
                time.sleep(0.25)
                for i, name in enumerate(names):
                    page.evaluate(f"window.scrollTo(0, {i * half})")
                    time.sleep(0.25)
                    page.screenshot(path=str(OUT_DIR / f"{name}.png"))
                    print(f"  ✓ {name}.png")
                page.set_viewport_size(VIEWPORT)
                nonlocal shots
                shots += len(names)

            def tab(label: str):
                """Tab per Beschriftung öffnen (Tab-Leiste aus base.html)."""
                def _open():
                    page.get_by_role("button", name=label).first.click()
                    time.sleep(0.2)
                return _open

            # ── Admin ────────────────────────────────────────────────────
            print("Admin:")
            bctx.clear_cookies()
            shot("01-admin-login", "/admin/login")

            login_as(ctx["admin_token"])

            def fill_subscription_form():
                page.fill("#name", "Beachvolleyball Dienstag")
                page.fill("#description",
                          "Feste Platzmiete dienstags auf Platz 3, Sommersaison.")
                page.select_option("#weekday", label="Dienstag")
                page.fill("#start_hour", "18")
                page.fill("#start_minute", "30")
                page.fill("#duration_minutes", "120")
                page.fill("#min_participants", "4")
                page.fill("#max_participants", "8")
                page.fill("#start_date", "2026-04-07")
                page.fill("#end_date", "2026-09-29")
                page.fill("#abo_price", "480")
                page.fill("#default_price", "624")
                page.fill("#cancel_hours_free", "48")
                page.fill("#cancel_hours_approval", "12")
                page.select_option("#payout_mode", value="member")
                page.fill("#paypal_address", "kasse.beachclub@example.com")

            shot_split(("02-admin-abo-formular-1", "02-admin-abo-formular-2"),
                       "/admin/subscription/new", prepare=fill_subscription_form)
            shot_split(("03-admin-abo-detail-1", "03-admin-abo-detail-2"),
                       f"/admin/subscription/{ctx['sub_id']}")
            shot("04-admin-mitglied-formular",
                 f"/admin/subscription/{ctx['sub_id']}/members/new")
            def neutral_guest_link():
                """Der Gastlink enthält die Adresse des Testservers
                (127.0.0.1:<Port>). Fürs Handbuch durch die Produktiv-Domain
                ersetzen — rein kosmetisch, das Feld wird nicht abgeschickt."""
                page.eval_on_selector(
                    "#guest-link",
                    "el => el.value = el.value.replace("
                    f"/^https?:\\/\\/[^/]+/, '{PUBLIC_DOMAIN}')",
                )

            shot_split(("05-admin-termin-detail-1", "05-admin-termin-detail-2"),
                       f"/admin/event/{ctx['next_event_id']}",
                       prepare=neutral_guest_link)

            # ── Mitglied ─────────────────────────────────────────────────
            print("Mitglied:")
            bctx.clear_cookies()
            shot("06-member-login", "/member/login")

            # Code-Eingabe: Login anfordern und die Folgeseite zeigen. Der
            # Dev-Kasten mit den Klartext-Links existiert nur ohne SMTP und
            # wird fürs Handbuch ausgeblendet.
            page.goto(base + "/member/login", wait_until="networkidle")
            page.fill("#email", "jonas.berg@example.com")
            page.get_by_role("button", name="Login-Link anfordern").first.click()
            page.wait_for_load_state("networkidle")
            page.evaluate(
                "document.querySelectorAll('h2').forEach(h => {"
                "  if (h.textContent.includes('Dev-Modus')) h.closest('.card').remove();"
                "})"
            )
            time.sleep(0.35)
            capture("06b-member-code")

            login_as(ctx["member_token"])
            shot_split(("07-member-termine-1", "07-member-termine-2"),
                       "/member/dashboard", prepare=tab("📅 Termine"))
            shot("08-member-meine", "/member/dashboard", prepare=tab("✅ Meine"))
            shot("09-member-konto", "/member/dashboard", prepare=tab("💶 Konto"))

            # Der Kontoauszug scrollt seitlich — die Betragsspalte separat.
            page.get_by_role("button", name="💶 Konto").first.click()
            time.sleep(0.2)
            ledger = page.locator("div.card", has_text="Kontoauszug")
            ledger.locator(".table-wrap").evaluate(
                "el => el.scrollLeft = el.scrollWidth"
            )
            time.sleep(0.3)
            ledger.screenshot(path=str(OUT_DIR / "09b-member-kontoauszug.png"))
            shots += 1
            print("  ✓ 09b-member-kontoauszug.png")

            def open_transfer():
                page.get_by_role("button", name="💶 Konto").first.click()
                time.sleep(0.2)
                page.locator("details summary").first.click()
                time.sleep(0.2)

            shot("10-member-zahlungseingang", "/member/dashboard",
                 prepare=open_transfer)
            shot("11-member-teilnehmer",
                 f"/member/event/{ctx['next_event_id']}/participants")

            login_as(ctx["minus_token"])
            # Banner und Wartelisten-Karte tragen die Aussage — der Rest
            # der Terminliste ist schon in 07 zu sehen.
            shot_clip("12-member-minus-banner", "/member/dashboard", 760,
                      prepare=tab("📅 Termine"))

            # ── Super-Mitglied ───────────────────────────────────────────
            print("Super-Mitglied:")
            login_as(ctx["super_token"])
            # Super-Mitglieder sehen die volle Preisstaffel statt nur des
            # Maximalpreises — dafür reicht eine einzelne Terminkarte.
            shot_element("13-super-preisstaffel", "/member/dashboard",
                         ".event-item", prepare=tab("📅 Termine"))
            shot_split(("14-super-verwaltung-1", "14-super-verwaltung-2"),
                       "/member/dashboard", prepare=tab("⚙️ Verwaltung"))
            shot("15-super-teilnehmer",
                 f"/member/event/{ctx['settled_event_id']}/participants")

            # Die Gästetabelle scrollt auf dem Handy seitlich — für das
            # Handbuch die rechte Hälfte mit der Bezahlt-Spalte zeigen.
            page.goto(
                base + f"/member/event/{ctx['settled_event_id']}/participants",
                wait_until="networkidle",
            )
            guest_card = page.locator("div.card", has_text="Gäste (über Gastlink)")
            guest_card.locator(".table-wrap").evaluate(
                "el => el.scrollLeft = el.scrollWidth"
            )
            time.sleep(0.3)
            guest_card.screenshot(path=str(OUT_DIR / "16-super-gast-bezahlt.png"))
            shots += 1
            print("  ✓ 16-super-gast-bezahlt.png")

            # ── Gast ─────────────────────────────────────────────────────
            # Zuletzt, weil die Buchung die Demo-Daten verändert.
            print("Gast:")
            bctx.clear_cookies()
            shot("17-gast-buchung", f"/g/{ctx['next_event_token']}")

            page.goto(base + f"/g/{ctx['next_event_token']}",
                      wait_until="networkidle")
            page.fill("#name", "Kim Roth")
            page.fill("#email", "kim.roth@example.com")
            page.fill("#count", "2")
            page.get_by_role("button", name="Verbindlich buchen").first.click()
            page.wait_for_load_state("networkidle")
            time.sleep(0.35)
            capture("18-gast-bestaetigung")

            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)

    print(f"\n{shots} Screenshots in {OUT_DIR}")


if __name__ == "__main__":
    main()
