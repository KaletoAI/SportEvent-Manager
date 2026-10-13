"""Einheitliches Mail-Layout: jede Mail hat Klartext und HTML im
gemeinsamen Design, Inhalte werden escaped."""

import pytest

from app import emailer
from app.emailer import Mail
from app.maildoc import BRAND, MailContent

ALL_MAILS = [
    emailer.login_link_email("Anna", [("", "https://x/login/t/abc")], "123456"),
    emailer.cancel_reminder_email(
        "Anna", "06.10.2026", "04.10.2026 18:00", "4711", ["Anna", "Sina (+1 Gast)"]
    ),
    emailer.minimum_participants_email("Anna", "06.10.2026", "18:00", 3, 4),
    emailer.waitlist_promoted_email("Anna", "06.10.2026", "18:00"),
    emailer.cancel_request_email("Sina", "Anna", "06.10.2026"),
    emailer.settlement_email(
        "Anna", "06.10.2026", "8,00 €", "-8,00 €", "pay@example.com", None,
        open_items=[("Teilnahme 06.10.2026", "8,00 €")], debt="8,00 €",
    ),
    emailer.payment_request_email(
        "Anna", "Beach", "8,00 €", [("Teilnahme 06.10.2026", "8,00 €")],
        "pay@example.com",
    ),
    emailer.guest_settlement_email("Gustav", "06.10.2026", 2, "10,00 €", "pay@example.com"),
    emailer.guest_request_email("Sina", "Gustav", 2, "06.10.2026"),
    emailer.guest_confirmed_email(
        "Gustav", "06.10.2026", "18:00", 2, "12,00 €", "pay@example.com", door_code="4711"
    ),
    emailer.guest_rejected_email("Gustav", "06.10.2026"),
    emailer.guest_reminder_email("Gustav", "06.10.2026", "18:00", 2, "4711", ["Anna"]),
]


@pytest.mark.parametrize("content", ALL_MAILS)
def test_every_mail_uses_common_layout(content):
    assert isinstance(content, MailContent)
    assert content.html.startswith("<!DOCTYPE html>")
    assert BRAND in content.html
    assert "Sportliche Grüße" in content.html
    assert content.text.endswith(f"Sportliche Grüße\n{BRAND}")
    # Inline-Styles only: no <style>/<script> blocks
    assert "<style" not in content.html and "<script" not in content.html


def test_mail_takes_text_and_html_from_content():
    content = emailer.guest_rejected_email("Gustav", "06.10.2026")
    mail = Mail("g@example.com", "Betreff", content)
    assert mail.body == content.text
    assert mail.html == content.html


def test_html_shows_same_facts_as_text():
    content = emailer.payment_request_email(
        "Anna", "Beach", "15,00 €",
        [("Teilnahme 01.10.2026 (Rest)", "5,00 €"), ("Teilnahme 08.10.2026", "10,00 €")],
        "sina@pay.me", "Sina",
    )
    for fragment in ("Teilnahme 01.10.2026 (Rest)", "5,00 €", "Offen gesamt",
                     "15,00 €", "sina@pay.me", "Bitte zahle 15,00 € an Sina per PayPal:"):
        assert fragment in content.text
        assert fragment in content.html


def test_html_escapes_user_content():
    content = emailer.guest_request_email("Sina", "<b>Eve</b>", 1, "06.10.2026")
    assert "<b>Eve</b>" not in content.html
    assert "&lt;b&gt;Eve&lt;/b&gt;" in content.html


def test_paypal_me_link_gets_button():
    content = emailer.guest_settlement_email(
        "Gustav", "06.10.2026", 1, "5,00 €", "paypal.me/sina"
    )
    assert 'href="https://paypal.me/sina"' in content.html
    plain = emailer.guest_settlement_email(
        "Gustav", "06.10.2026", 1, "5,00 €", "sina@pay.me"
    )
    assert "href=" not in plain.html
