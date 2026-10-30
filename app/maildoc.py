"""Einheitliches Layout für alle E-Mails.

Jede Mail wird aus wenigen Bausteinen zusammengesetzt (Absatz, Fakten-
tabelle, Hinweis, Zahlungsbox, Code, Button, Liste, Kleingedrucktes) und
daraus zweimal gerendert: als Klartext und als HTML im gemeinsamen Design.
So bleiben beide Fassungen inhaltsgleich, und jede Mail sieht gleich aus.

Builder übergeben nur Klartext — escaped wird beim HTML-Rendern. Alle
Styles stehen inline (Mailprogramme ignorieren <style> oft), das Gerüst
ist tabellenbasiert, damit es auch in Outlook trägt.
"""

import html
from dataclasses import dataclass
from typing import Optional

BRAND = "SportAbo Manager"

# Farben wie in app/static/style.css (helles Theme)
_PRIMARY = "#2563eb"
_TEXT = "#1e293b"
_MUTED = "#64748b"
_BORDER = "#e2e8f0"
_PAGE = "#f1f5f9"
_PANEL = "#f8fafc"
_FONT = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, "
    "Arial, sans-serif"
)
_TONES = {
    # Hintergrund, Rahmen, Text
    "info": ("#eff6ff", "#bfdbfe", "#1e40af"),
    "success": ("#dcfce7", "#bbf7d0", "#166534"),
    "warning": ("#fef3c7", "#fde68a", "#92400e"),
}


@dataclass
class MailContent:
    """Fertig gerenderte Mail: Klartext und HTML."""

    text: str
    html: str


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _paypal_url(paypal: str) -> Optional[str]:
    """PayPal.me-Links bekommen einen Button; reine Adressen nicht."""
    lower = paypal.lower()
    if lower.startswith(("https://", "http://")):
        return paypal
    if lower.startswith(("paypal.me/", "www.paypal.me/")):
        return "https://" + paypal
    return None


def _button_html(label: str, url: str) -> str:
    return (
        f'<a href="{_e(url)}" style="display:inline-block; background:{_PRIMARY}; '
        "color:#ffffff; padding:11px 22px; border-radius:8px; "
        f'text-decoration:none; font-weight:700;">{_e(label)}</a>'
    )


class MailDoc:
    """Baukasten für eine Mail. Methoden lassen sich verketten:

        MailDoc("Abrechnung", "Anna").p("…").facts([...]).render()
    """

    def __init__(self, title: str, name: str):
        self.title = title
        self.name = name
        self._blocks: list[tuple[str, str]] = []  # (text, html)

    def _add(self, text: str, markup: str) -> "MailDoc":
        self._blocks.append((text, markup))
        return self

    def p(self, text: str) -> "MailDoc":
        return self._add(text, f'<p style="margin:0 0 16px;">{_e(text)}</p>')

    def facts(
        self,
        rows: list[tuple[str, str]],
        title: Optional[str] = None,
        total: Optional[tuple[str, str]] = None,
        itemized: bool = False,
    ) -> "MailDoc":
        """Zweispaltige Tabelle „Bezeichnung … Wert“, optional mit
        Überschrift und Summenzeile. `itemized` = Aufzählung im Klartext."""
        prefix = "  – " if itemized else ""
        lines = [title] if title else []
        lines += [f"{prefix}{label}: {value}" for label, value in rows]
        if total:
            lines.append(f"{total[0]}: {total[1]}")

        cell = "padding:7px 0; vertical-align:top;"
        body = "".join(
            f'<tr><td style="{cell} color:{_MUTED};">{_e(label)}</td>'
            f'<td align="right" style="{cell} padding-left:16px; '
            f'font-weight:600; white-space:nowrap;">{_e(value)}</td></tr>'
            for label, value in rows
        )
        if total:
            total_cell = f"padding:10px 0 2px; border-top:2px solid {_BORDER};"
            body += (
                f'<tr><td style="{total_cell} font-weight:700;">{_e(total[0])}</td>'
                f'<td align="right" style="{total_cell} padding-left:16px; '
                f'font-weight:700; font-size:17px; white-space:nowrap;">'
                f"{_e(total[1])}</td></tr>"
            )
        heading = (
            f'<div style="font-weight:700; margin:0 0 6px;">{_e(title)}</div>'
            if title
            else ""
        )
        markup = (
            f'<div style="background:{_PANEL}; border:1px solid {_BORDER}; '
            f'border-radius:10px; padding:12px 16px; margin:0 0 16px;">'
            f"{heading}"
            '<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" style="font-size:15px;">{body}</table></div>'
        )
        return self._add("\n".join(lines), markup)

    def notice(self, text: str, tone: str = "info") -> "MailDoc":
        bg, border, color = _TONES[tone]
        return self._add(
            text,
            f'<div style="background:{bg}; border:1px solid {border}; '
            f"color:{color}; border-radius:10px; padding:12px 16px; "
            f'margin:0 0 16px;">{_e(text)}</div>',
        )

    def pay(
        self, lead: str, paypal: str, hint: str = "", pay_url: str = ""
    ) -> "MailDoc":
        """Hervorgehobene Zahlungsbox: Aufforderung, PayPal-Ziel, Hinweis.
        `pay_url` (PayPal-Link des Empfängers) wird zum Zahl-Button."""
        url = pay_url or _paypal_url(paypal)
        lines = [lead] + ([paypal] if paypal else []) + ([url] if pay_url else [])
        lines += [hint] if hint else []
        button = (
            f'<div style="margin:12px 0 4px;">{_button_html("Mit PayPal zahlen", url)}</div>'
            if url
            else ""
        )
        hint_html = (
            f'<div style="color:{_MUTED}; font-size:13px; margin-top:8px;">{_e(hint)}</div>'
            if hint
            else ""
        )
        return self._add(
            "\n".join(lines),
            f'<div style="background:#eff6ff; border:1px solid #bfdbfe; '
            f"border-left:4px solid {_PRIMARY}; border-radius:10px; "
            f'padding:14px 16px; margin:0 0 16px;">'
            f"<div>{_e(lead)}</div>"
            + (
                f'<div style="font-size:17px; font-weight:700; margin-top:6px; '
                f'word-break:break-all;">{_e(paypal)}</div>'
                if paypal
                else ""
            )
            + f"{button}{hint_html}</div>",
        )

    def code(self, label: str, value: str) -> "MailDoc":
        """Gut lesbarer Code (Login-Code, Türcode)."""
        return self._add(
            f"{label}: {value}",
            f'<div style="background:{_PANEL}; border:1px solid {_BORDER}; '
            f'border-radius:10px; padding:12px 16px; margin:0 0 16px; text-align:center;">'
            f'<div style="color:{_MUTED}; font-size:13px;">{_e(label)}</div>'
            f'<div style="font-size:28px; font-weight:700; letter-spacing:6px; '
            f'margin-top:4px;">{_e(value)}</div></div>',
        )

    def button(self, label: str, url: str) -> "MailDoc":
        return self._add(
            f"{label}:\n{url}",
            f'<div style="margin:0 0 16px;">{_button_html(label, url)}'
            f'<div style="color:{_MUTED}; font-size:12px; margin-top:6px; '
            f'word-break:break-all;">Falls der Button nicht funktioniert: '
            f"{_e(url)}</div></div>",
        )

    def bullets(self, title: str, entries: list[str]) -> "MailDoc":
        items = "".join(f'<li style="margin:2px 0;">{_e(e)}</li>' for e in entries)
        return self._add(
            "\n".join([title] + [f"- {e}" for e in entries]),
            f'<div style="margin:0 0 16px;"><div style="font-weight:700; '
            f'margin-bottom:4px;">{_e(title)}</div>'
            f'<ul style="margin:0; padding-left:20px;">{items}</ul></div>',
        )

    def small(self, text: str) -> "MailDoc":
        return self._add(
            text,
            f'<p style="margin:0 0 16px; color:{_MUTED}; font-size:13px;">{_e(text)}</p>',
        )

    def render(self) -> MailContent:
        greeting = f"Hallo {self.name},"
        closing = ("Sportliche Grüße", BRAND)
        text = "\n\n".join(
            [greeting] + [t for t, _ in self._blocks] + ["\n".join(closing)]
        )
        blocks = "".join(m for _, m in self._blocks)
        markup = f"""\
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>{_e(self.title)}</title>
</head>
<body style="margin:0; padding:0; background:{_PAGE};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_PAGE};">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px; background:#ffffff; border:1px solid {_BORDER}; border-radius:14px; border-collapse:separate; overflow:hidden;">
<tr><td style="background:{_PRIMARY}; padding:18px 24px; font-family:{_FONT}; color:#ffffff;">
<div style="font-size:12px; letter-spacing:1px; text-transform:uppercase; opacity:0.85;">{_e(BRAND)}</div>
<div style="font-size:21px; font-weight:700; margin-top:4px;">{_e(self.title)}</div>
</td></tr>
<tr><td style="padding:24px; font-family:{_FONT}; font-size:15px; line-height:1.55; color:{_TEXT};">
<p style="margin:0 0 16px;">{_e(greeting)}</p>
{blocks}
<p style="margin:24px 0 0;">{_e(closing[0])}<br>{_e(closing[1])}</p>
</td></tr>
</table>
<div style="font-family:{_FONT}; font-size:12px; color:#94a3b8; padding:14px 12px;">Diese E-Mail wurde automatisch vom {_e(BRAND)} verschickt.</div>
</td></tr>
</table>
</body>
</html>"""
        return MailContent(text=text, html=markup)
