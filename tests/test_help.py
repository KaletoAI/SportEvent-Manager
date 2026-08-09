"""Hilfe-Seiten unter /hilfe: Kurzreferenz öffentlich, Handbuch geschützt."""

from tests.conftest import admin_login, member_login


def test_help_index_is_public(client):
    resp = client.get("/hilfe")
    assert resp.status_code == 200
    assert "Kurzreferenz" in resp.text


def test_quickref_is_public(client):
    """Gäste haben kein Konto — die Kurzreferenz muss ohne Login gehen."""
    resp = client.get("/hilfe/kurzreferenz", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Kurzreferenz" in resp.text


def test_manual_requires_login(client):
    resp = client.get("/hilfe/handbuch", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/member/login"


def test_manual_for_member(client, seed):
    member_login(client)
    resp = client.get("/hilfe/handbuch", follow_redirects=False)
    assert resp.status_code == 200
    assert "Handbuch" in resp.text


def test_manual_for_admin(client, seed):
    admin_login(client)
    resp = client.get("/hilfe/handbuch", follow_redirects=False)
    assert resp.status_code == 200


def test_index_hides_manual_link_when_logged_out(client):
    """Ohne Anmeldung wird auf den Login verwiesen statt aufs Handbuch."""
    resp = client.get("/hilfe")
    assert "/hilfe/handbuch" not in resp.text
    assert "/hilfe/kurzreferenz" in resp.text


def test_index_shows_manual_link_when_logged_in(client, seed):
    member_login(client)
    resp = client.get("/hilfe")
    assert "/hilfe/handbuch" in resp.text


def test_help_link_in_nav(client):
    resp = client.get("/member/login")
    assert 'href="/hilfe"' in resp.text


def test_guest_page_links_to_quickref(client, seed):
    event = seed["event"]
    resp = client.get(f"/g/{event.public_token}")
    assert resp.status_code == 200
    assert "/hilfe/kurzreferenz" in resp.text
