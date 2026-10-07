"""Datenschutzerklärung unter /datenschutz: öffentlich, aus .env befüllt."""

from app.config import settings


def test_privacy_page_is_public(client):
    resp = client.get("/datenschutz", follow_redirects=False)
    assert resp.status_code == 200
    assert "Datenschutzerklärung" in resp.text
    assert resp.headers["cache-control"] == "no-store"


def test_privacy_page_shows_controller_and_retention(client, monkeypatch):
    monkeypatch.setattr(settings, "privacy_name", "Erika Mustermann")
    monkeypatch.setattr(settings, "privacy_address", "Musterweg 1, 12345 Musterstadt")
    monkeypatch.setattr(settings, "privacy_email", "datenschutz@example.org")
    monkeypatch.setattr(settings, "privacy_hosting", "Hoster GmbH")
    monkeypatch.setattr(settings, "data_retention_months", 12)
    html = client.get("/datenschutz").text
    assert "Erika Mustermann" in html
    assert "Musterweg 1, 12345 Musterstadt" in html
    assert 'href="mailto:datenschutz@example.org"' in html
    assert "Hoster GmbH" in html
    assert "12 Monate nach Ende der" in html
    assert "PRIVACY_NAME" not in html


def test_privacy_page_warns_without_controller(client, monkeypatch):
    monkeypatch.setattr(settings, "privacy_name", "")
    monkeypatch.setattr(settings, "privacy_hosting", "")
    html = client.get("/datenschutz").text
    assert "noch keine" in html and "PRIVACY_NAME" in html
    assert "einem Hosting-Dienstleister" in html


def test_footer_links_privacy_on_public_pages(client, seed):
    """Login und Gast-Buchungsseite verlinken die Erklärung."""
    for url in ("/member/login", f"/g/{seed['event'].public_token}"):
        assert 'href="/datenschutz"' in client.get(url).text, url
