"""Best-effort email sending via aiosmtplib.

Per-subscription SMTP settings override the global ones from config.
Without any configured SMTP host, sending is silently skipped (returns
0/False) so the app works fully without a mail server.

Several mails for one occasion (settlement, reminders, waitlist) go out as
one batch over a single SMTP connection — one TLS handshake instead of
one per recipient, so e.g. "Abrechnen" doesn't keep the phone waiting.
"""

import logging
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Optional, Sequence

import aiosmtplib

from app.config import settings
from app.maildoc import MailContent, MailDoc
from app.models.models import Subscription

logger = logging.getLogger(__name__)


@dataclass
class Mail:
    to: str
    subject: str
    body: str
    html: Optional[str] = None

    def __post_init__(self):
        # Builder liefern MailContent: Klartext wird body, HTML die Alternative
        if isinstance(self.body, MailContent):
            self.body, self.html = self.body.text, self.body.html


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


# ── Mail-Inhalte ───────────────────────────────────────────────────────────
# Jeder Builder liefert MailContent (Klartext + HTML im gemeinsamen Layout,
# siehe app/maildoc.py); Mail() übernimmt beides.


def _target(payee_name: Optional[str]) -> str:
    return f"an {payee_name} per PayPal" if payee_name else "per PayPal"


def _link(doc: MailDoc, label: str, url: str) -> None:
    """Direktlink als Button — fehlt ohne BASE_URL (siehe web.app_url).
    Links öffnen nur Seiten; Aktionen bleiben POST + Bestätigung, weil
    Mail-Scanner Links automatisch aufrufen."""
    if url:
        doc.button(label, url)


def _door_code(doc: MailDoc, door_code: str) -> None:
    if door_code:
        doc.code("Türcode", door_code)


def _open_debt(
    doc: MailDoc,
    items: list[tuple[str, str]],
    debt: str,
    paypal: Optional[str],
    payee_name: Optional[str],
) -> None:
    """Offene Termine + Zahlungsbox — gleich in Abrechnungs- und
    Sammelmail."""
    doc.facts(items, title="Offene Termine", total=("Offen gesamt", debt), itemized=True)
    if paypal:
        doc.pay(
            f"Bitte zahle {debt} {_target(payee_name)}:",
            paypal,
            "Bitte deinen Namen im Verwendungszweck angeben.",
        )
    else:
        doc.p(f"Bitte gleiche {debt} beim Organisator aus.")


def login_link_email(
    member_name: str, links: list[tuple[str, str]], code: str
) -> MailContent:
    """`links`: (Abo-Name, URL); der Name bleibt leer bei nur einem Abo."""
    doc = MailDoc("Dein Anmelde-Link", member_name)
    doc.p("mit einem Klick bist du angemeldet:")
    for label, url in links:
        doc.button(f"Anmelden: {label}" if label else "Jetzt anmelden", url)
    doc.code("Oder diesen Code auf der Login-Seite eingeben", code)
    doc.small(
        "Link und Code sind 15 Minuten gültig. Wenn du keine Anmeldung "
        "angefordert hast, ignoriere diese E-Mail."
    )
    return doc.render()


def cancel_reminder_email(
    member_name: str, event_date: str, deadline: str, door_code: str = "",
    roster: Sequence[str] = (), url: str = "",
) -> MailContent:
    doc = MailDoc(f"Erinnerung: Termin {event_date}", member_name)
    doc.p(f"du bist für den Termin am {event_date} angemeldet.")
    doc.facts([("Kostenlos abmelden bis", f"{deadline} Uhr")])
    _link(doc, "Zum Termin (abmelden)", url)
    if roster:
        doc.bullets("Mit dabei:", list(roster))
    _door_code(doc, door_code)
    return doc.render()


def minimum_participants_email(
    recipient_name: str, event_date: str, start_time: str,
    participants: int, minimum: int, guest_url: str = "",
) -> MailContent:
    doc = MailDoc("Mitspieler gesucht", recipient_name)
    doc.p(
        f"nach einer Abmeldung sind für den Termin am {event_date} "
        f"um {start_time} Uhr noch {participants} Personen angemeldet."
    )
    doc.facts(
        [
            ("Angemeldet", str(participants)),
            ("Mindestens nötig", str(minimum)),
        ],
        total=("Es fehlen noch", str(minimum - participants)),
    )
    doc.p(
        "Frag bitte in deinem Bekanntenkreis nach, ob jemand mitspielen "
        "möchte, damit der Termin stattfinden kann."
    )
    if guest_url:
        doc.p("Mit diesem Gastlink können sie sich direkt anmelden, ohne Konto:")
        _link(doc, "Gastlink zum Weitergeben", guest_url)
    return doc.render()


def waitlist_promoted_email(
    member_name: str, event_date: str, start_time: str, url: str = ""
) -> MailContent:
    doc = MailDoc("Du bist nachgerückt", member_name)
    doc.notice(
        f"Gute Nachricht: Für den Termin am {event_date} um {start_time} Uhr "
        "ist ein Platz frei geworden — du bist von der Warteliste "
        "nachgerückt und jetzt fest angemeldet.",
        "success",
    )
    doc.p("Wenn du nicht kannst, melde dich bitte im Dashboard wieder ab.")
    _link(doc, "Zum Termin", url)
    return doc.render()


def cancel_request_email(
    super_name: str, member_name: str, event_date: str, url: str = ""
) -> MailContent:
    doc = MailDoc("Abmelde-Anfrage", super_name)
    doc.p(f"{member_name} möchte sich vom Termin am {event_date} abmelden.")
    doc.notice(
        "Die freie Abmeldefrist ist bereits abgelaufen — bitte gib die "
        "Anfrage im Dashboard frei oder lehne sie ab.",
        "warning",
    )
    _link(doc, "Anfrage ansehen", url)
    return doc.render()


def settlement_email(
    member_name: str,
    event_date: str,
    amount: str,
    balance: str,
    paypal: Optional[str],
    payee_name: Optional[str] = None,
    is_payee: bool = False,
    open_items: Sequence[tuple[str, str]] = (),
    debt: str = "",
    url: str = "",
) -> MailContent:
    """Abrechnungsmail. Im Minus (`open_items`, `debt`) mit denselben
    offenen Terminen und derselben Zahlungsbox wie die Sammelmail."""
    doc = MailDoc(f"Abrechnung {event_date}", member_name)
    doc.p(f"der Termin am {event_date} ist abgerechnet.")
    doc.facts([("Dein Betrag", amount), ("Dein Guthaben jetzt", balance)])
    if is_payee:
        doc.notice(
            "Du bist aktuell der Zahlungsempfänger — die anderen zahlen "
            "ihre Beträge an dich."
        )
    elif open_items:
        _open_debt(doc, list(open_items), debt, paypal, payee_name)
    else:
        doc.notice("Dein Guthaben deckt den Betrag — du musst nichts zahlen.", "success")
    _link(doc, "Kontoauszug ansehen", url)
    return doc.render()


def payment_request_email(
    member_name: str,
    subscription_name: str,
    debt: str,
    items: list[tuple[str, str]],
    paypal: Optional[str],
    payee_name: Optional[str] = None,
    url: str = "",
) -> MailContent:
    """Gesammelte Zahlungsaufforderung: offener Betrag + offene Posten
    (Notiz, Betrag), ältester zuerst."""
    doc = MailDoc("Zahlungsaufforderung", member_name)
    doc.p(f"dein Guthaben im Abo „{subscription_name}“ ist im Minus.")
    _open_debt(doc, items, debt, paypal, payee_name)
    _link(doc, "Kontoauszug ansehen", url)
    return doc.render()


def guest_settlement_email(
    guest_name: str,
    event_date: str,
    count: int,
    amount: str,
    paypal: Optional[str],
    payee_name: Optional[str] = None,
) -> MailContent:
    persons = f" ({count} Personen)" if count > 1 else ""
    doc = MailDoc(f"Abrechnung {event_date}", guest_name)
    doc.p(f"danke für deine Teilnahme als Gast am {event_date}{persons}!")
    doc.facts([("Dein Anteil", amount)])
    if paypal:
        doc.pay(
            f"Bitte zahle {amount} {_target(payee_name)}:",
            paypal,
            "Bitte Name und Termin im Verwendungszweck angeben.",
        )
    return doc.render()


def guest_request_email(
    super_name: str, guest_name: str, count: int, event_date: str,
    url: str = "",
) -> MailContent:
    persons = f" ({count} Personen)" if count > 1 else ""
    doc = MailDoc("Neue Gast-Anfrage", super_name)
    doc.p(f"{guest_name}{persons} möchte als Gast am {event_date} mitspielen.")
    doc.notice(
        "Die Mindestteilnehmerzahl ist erreicht — bitte bestätige die "
        "Anfrage im Dashboard (Tab „Verwaltung“) oder lehne sie ab.",
        "warning",
    )
    _link(doc, "Anfrage ansehen", url)
    return doc.render()


def guest_confirmed_email(
    guest_name: str,
    event_date: str,
    start_time: str,
    count: int,
    max_amount: str,
    paypal: Optional[str],
    payee_name: Optional[str] = None,
    door_code: str = "",
    url: str = "",
) -> MailContent:
    persons = f" für {count} Personen" if count > 1 else ""
    doc = MailDoc("Gastbuchung bestätigt", guest_name)
    doc.p(f"deine Gastbuchung{persons} ist bestätigt — bis dann!")
    doc.facts(
        [("Termin", f"{event_date}, {start_time} Uhr")]
        + ([("Personen", str(count))] if count > 1 else [])
        + [("Maximal zu zahlen", max_amount)]
    )
    _door_code(doc, door_code)
    doc.small(
        "Der endgültige Betrag steht nach dem Termin fest "
        "(Terminkosten ÷ Teilnehmerzahl)."
    )
    if paypal:
        doc.pay(f"Bitte zahle nach dem Termin {_target(payee_name)}:", paypal)
    doc.p(
        "Falls du nicht kommen kannst, gib bitte dem Organisator Bescheid, "
        "damit dein Platz frei wird."
    )
    _link(doc, "Deine Buchung ansehen", url)
    return doc.render()


def guest_rejected_email(guest_name: str, event_date: str) -> MailContent:
    doc = MailDoc("Gastanfrage nicht bestätigt", guest_name)
    doc.p(
        f"deine Gastanfrage für den Termin am {event_date} konnte leider "
        "nicht bestätigt werden. Die Buchung ist damit hinfällig, es "
        "entstehen dir keine Kosten."
    )
    return doc.render()


def guest_reminder_email(
    guest_name: str, event_date: str, start_time: str, count: int,
    door_code: str = "", roster: Sequence[str] = (), url: str = "",
) -> MailContent:
    persons = f" mit {count} Personen" if count > 1 else ""
    doc = MailDoc(f"Erinnerung: Termin {event_date}", guest_name)
    doc.p(
        f"du bist{persons} als Gast für den Termin am {event_date} "
        f"um {start_time} Uhr angemeldet."
    )
    if roster:
        doc.bullets("Mit dabei:", list(roster))
    _door_code(doc, door_code)
    doc.small(
        "Falls du nicht kommen kannst, gib bitte dem Organisator "
        "Bescheid, damit dein Platz frei wird."
    )
    _link(doc, "Deine Buchung ansehen", url)
    return doc.render()


def booking_removed_email(
    name: str, event_date: str, removed_by: str, url: str = ""
) -> MailContent:
    """Ein Super-Mitglied hat die Anmeldung entfernt (z. B. nicht
    erschienen) — Transparenz für die betroffene Person."""
    doc = MailDoc(f"Abgemeldet: Termin {event_date}", name)
    doc.p(
        f"{removed_by} hat deine Anmeldung für den Termin am {event_date} "
        "entfernt. Für diesen Termin entsteht dir damit kein Anteil."
    )
    doc.small(
        "Falls das ein Versehen war, melde dich bitte bei "
        f"{removed_by} oder beim Organisator."
    )
    _link(doc, "Zum Dashboard", url)
    return doc.render()
