"""Best-effort email sending via aiosmtplib.

Per-subscription SMTP settings override the global ones from config.
Without any configured SMTP host, sending is silently skipped (returns
0/False) so the app works fully without a mail server.

Several mails for one occasion (settlement, reminders, waitlist) go out as
one batch over a single SMTP connection — one TLS handshake instead of
one per recipient, so e.g. "Abrechnen" doesn't keep the phone waiting.
"""

import html
import logging
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Optional

import aiosmtplib

from app.config import settings
from app.models.models import Subscription

logger = logging.getLogger(__name__)


@dataclass
class Mail:
    to: str
    subject: str
    body: str
    html: Optional[str] = None


def smtp_config_for(subscription: Optional[Subscription]) -> Optional[dict]:
    """Effective SMTP config: subscription overrides, else global settings."""
    if subscription is not None and subscription.smtp_host:
        return {
            "host": subscription.smtp_host,
            "port": subscription.smtp_port or 587,
            "user": subscription.smtp_user or "",
            "password": subscription.smtp_password or "",
            "use_tls": subscription.smtp_use_tls,
            "sender": subscription.email_sender or settings.email_from,
            "sender_name": subscription.email_from_name
            or settings.email_from_name,
        }
    if settings.smtp_host:
        return {
            "host": settings.smtp_host,
            "port": settings.smtp_port,
            "user": settings.smtp_user,
            "password": settings.smtp_password,
            "use_tls": settings.smtp_use_tls,
            "sender": settings.email_from,
            "sender_name": settings.email_from_name,
        }
    return None


def _message(config: dict, mail: Mail) -> EmailMessage:
    message = EmailMessage()
    message["From"] = f"{config['sender_name']} <{config['sender']}>"
    message["To"] = mail.to
    message["Subject"] = mail.subject
    message.set_content(mail.body)
    if mail.html:
        message.add_alternative(mail.html, subtype="html")
    return message


async def send_with_config(config: Optional[dict], mails: list[Mail]) -> int:
    """Send `mails` over one SMTP connection. Returns how many succeeded.
    Takes a resolved config (no ORM access) so it can run as a background
    task after the request's DB session is gone."""
    if not mails:
        return 0
    if config is None:
        for mail in mails:
            logger.info(
                "No SMTP configured, skipping email to %s (%s)", mail.to, mail.subject
            )
        return 0
    # Port 465 = implicit SSL (SMTPS); otherwise STARTTLS per config
    implicit = config["port"] == 465
    smtp = aiosmtplib.SMTP(
        hostname=config["host"],
        port=config["port"],
        use_tls=implicit,
        start_tls=False if implicit else config["use_tls"],
        timeout=30,
    )
    sent = 0
    try:
        async with smtp:
            if config["user"]:
                await smtp.login(config["user"], config["password"])
            for mail in mails:
                try:
                    await smtp.send_message(_message(config, mail))
                    logger.info("Email sent to %s (%s)", mail.to, mail.subject)
                    sent += 1
                except Exception:
                    logger.exception("Email to %s failed", mail.to)
    except Exception:
        for mail in mails[sent:]:
            logger.exception("Email to %s failed", mail.to)
    return sent


async def send_batch(
    subscription: Optional[Subscription], mails: list[Mail]
) -> int:
    """Send several mails with the subscription's SMTP config."""
    return await send_with_config(smtp_config_for(subscription), mails)


async def send_email(
    subscription: Optional[Subscription],
    to: str,
    subject: str,
    body: str,
    html: Optional[str] = None,
) -> bool:
    """Send a single email. Returns True on success, False otherwise."""
    return await send_batch(subscription, [Mail(to, subject, body, html)]) == 1


def login_link_email_body(member_name: str, link: str, code: str) -> str:
    return "\n".join(
        [
            f"Hallo {member_name},",
            "",
            "mit diesem Link kannst du dich anmelden (15 Minuten gültig):",
            link,
            "",
            f"Alternativ gib diesen Code auf der Login-Seite ein: {code}",
            "",
            "Wenn du keine Anmeldung angefordert hast, ignoriere diese E-Mail.",
            "",
            "Sportliche Grüße",
            "SportAbo Manager",
        ]
    )


def login_link_email_html(member_name: str, link: str, code: str) -> str:
    member_name = html.escape(member_name)
    link = html.escape(link, quote=True)
    code = html.escape(code)
    return f"""\
<div style="font-family:sans-serif; max-width:480px;">
  <p>Hallo {member_name},</p>
  <p>
    <a href="{link}" style="display:inline-block; background:#2563eb; color:#ffffff;
       padding:10px 20px; border-radius:8px; text-decoration:none; font-weight:bold;">
      Jetzt anmelden
    </a>
  </p>
  <p>Alternativ gib diesen Code auf der Login-Seite ein:</p>
  <p style="font-size:28px; font-weight:bold; letter-spacing:6px;">{code}</p>
  <p style="color:#64748b; font-size:13px;">
    Link und Code sind 15 Minuten gültig. Wenn du keine Anmeldung angefordert
    hast, ignoriere diese E-Mail.<br>
    Falls der Button nicht funktioniert: {link}
  </p>
  <p>Sportliche Grüße<br>SportAbo Manager</p>
</div>"""


def cancel_reminder_email_body(
    member_name: str, event_date: str, deadline: str
) -> str:
    return "\n".join(
        [
            f"Hallo {member_name},",
            "",
            f"du bist für den Termin am {event_date} angemeldet.",
            f"Bis {deadline} Uhr kannst du dich noch kostenlos abmelden.",
            "",
            "Sportliche Grüße",
            "SportAbo Manager",
        ]
    )


def waitlist_promoted_email_body(
    member_name: str, event_date: str, start_time: str
) -> str:
    return "\n".join(
        [
            f"Hallo {member_name},",
            "",
            f"gute Nachricht: Für den Termin am {event_date} um {start_time} Uhr "
            "ist ein Platz frei geworden — du bist von der Warteliste "
            "nachgerückt und jetzt fest angemeldet.",
            "",
            "Wenn du nicht kannst, melde dich bitte im Dashboard wieder ab.",
            "",
            "Sportliche Grüße",
            "SportAbo Manager",
        ]
    )


def cancel_request_email_body(
    super_name: str, member_name: str, event_date: str
) -> str:
    return "\n".join(
        [
            f"Hallo {super_name},",
            "",
            f"{member_name} möchte sich vom Termin am {event_date} abmelden.",
            "Die freie Abmeldefrist ist bereits abgelaufen — bitte gib die Anfrage "
            "im Dashboard frei oder lehne sie ab.",
            "",
            "Sportliche Grüße",
            "SportAbo Manager",
        ]
    )


def settlement_email_body(
    member_name: str,
    event_date: str,
    amount: str,
    balance: str,
    paypal: Optional[str],
    payee_name: Optional[str] = None,
    is_payee: bool = False,
) -> str:
    lines = [
        f"Hallo {member_name},",
        "",
        f"für den Termin am {event_date} wurden dir {amount} berechnet.",
        f"Dein aktuelles Guthaben: {balance}",
    ]
    if is_payee:
        lines += [
            "",
            "Du bist aktuell der Zahlungsempfänger — die anderen zahlen "
            "ihre Beträge an dich.",
        ]
    elif paypal:
        target = f"an {payee_name} per PayPal" if payee_name else "per PayPal"
        lines += [
            "",
            f"Bei negativem Guthaben zahle bitte {target}:",
            paypal,
            "Bitte Name und Termin im Verwendungszweck angeben.",
        ]
    lines += ["", "Sportliche Grüße", "SportAbo Manager"]
    return "\n".join(lines)


def guest_settlement_email_body(
    guest_name: str,
    event_date: str,
    count: int,
    amount: str,
    paypal: Optional[str],
    payee_name: Optional[str] = None,
) -> str:
    persons = f" ({count} Personen)" if count > 1 else ""
    lines = [
        f"Hallo {guest_name},",
        "",
        f"danke für deine Teilnahme als Gast am {event_date}{persons}!",
        f"Dein Anteil beträgt {amount}.",
    ]
    if paypal:
        target = f"an {payee_name} per PayPal" if payee_name else "per PayPal"
        lines += [
            "",
            f"Bitte zahle den Betrag {target}:",
            paypal,
            "Bitte Name und Termin im Verwendungszweck angeben.",
        ]
    lines += ["", "Sportliche Grüße", "SportAbo Manager"]
    return "\n".join(lines)


def guest_reminder_email_body(
    guest_name: str, event_date: str, start_time: str, count: int
) -> str:
    persons = f" mit {count} Personen" if count > 1 else ""
    return "\n".join(
        [
            f"Hallo {guest_name},",
            "",
            f"du bist{persons} als Gast für den Termin am {event_date} "
            f"um {start_time} Uhr angemeldet.",
            "Falls du nicht kommen kannst, gib bitte dem Organisator "
            "Bescheid, damit dein Platz frei wird.",
            "",
            "Sportliche Grüße",
            "SportAbo Manager",
        ]
    )
