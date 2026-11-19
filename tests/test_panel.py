import io
import os

import pytest

import baza
import panel
from conftest import HASLO_TESTOWE


@pytest.fixture
def zalogowany(klient, konta_testowe):
    """Klient z włączonym logowaniem, już zalogowany jako 'ania'."""
    c = klient(konta=konta_testowe, secret_key="x" * 32)
    odp = c.post("/login", data={"login": "ania", "haslo": HASLO_TESTOWE})
    assert odp.status_code == 302
    return c


def _wgraj(c, *pliki):
    return c.post("/api/faktury/wgraj", content_type="multipart/form-data",
                  data={"pliki": [(io.BytesIO(b), n) for n, b in pliki]})


# --- dostęp i logowanie ----------------------------------------------------

def test_bez_sesji_api_pdf_i_eksport_daja_401_a_strona_przekierowuje(
        klient, konta_testowe):
    c = klient(konta=konta_testowe, secret_key="x" * 32)
    for sciezka in ("/api/faktury", "/pdf/x.pdf", "/eksport"):
        assert c.get(sciezka).status_code == 401
    odp = c.get("/")
    assert odp.status_code == 302 and odp.headers["Location"].endswith("/login")


def test_healthz_dziala_bez_logowania(klient, konta_testowe):
    c = klient(konta=konta_testowe, secret_key="x" * 32)
    assert c.get("/healthz").json == {"ok": True}


def test_logowanie_poprawne_otwiera_dostep(zalogowany):
    assert zalogowany.get("/api/faktury").status_code == 200


def test_logowanie_bledne_daje_401(klient, konta_testowe, monkeypatch):
    monkeypatch.setattr(panel.time, "sleep", lambda s: None)
    c = klient(konta=konta_testowe, secret_key="x" * 32)
    odp = c.post("/login", data={"login": "ania", "haslo": "zle"})
    assert odp.status_code == 401
    assert c.get("/api/faktury").status_code == 401


def test_szosta_proba_po_pieciu_blednych_daje_429(klient, konta_testowe,
                                                  monkeypatch):
    monkeypatch.setattr(panel.time, "sleep", lambda s: None)
    c = klient(konta=konta_testowe, secret_key="x" * 32)
    for _ in range(5):
        assert c.post("/login", data={"login": "ania",
                                      "haslo": "zle"}).status_code == 401
    # blokada obejmuje też poprawne hasło:
    odp = c.post("/login", data={"login": "ania", "haslo": HASLO_TESTOWE})
    assert odp.status_code == 429


def test_dwie_aplikacje_maja_niezalezne_liczniki_prob(klient, konta_testowe,
                                                      monkeypatch):
    monkeypatch.setattr(panel.time, "sleep", lambda s: None)
    c1 = klient(konta=konta_testowe, secret_key="x" * 32)
    c2 = klient(konta=konta_testowe, secret_key="x" * 32)
    for _ in range(5):
        c1.post("/login", data={"login": "ania", "haslo": "zle"})
    assert c1.post("/login", data={"login": "ania",
                                   "haslo": HASLO_TESTOWE}).status_code == 429
    assert c2.post("/login", data={"login": "ania",
                                   "haslo": HASLO_TESTOWE}).status_code == 302


def test_token_monitora_przepuszcza_odczyt_api(klient, konta_testowe):
    c = klient(konta=konta_testowe, secret_key="x" * 32, panel_token="tok")
    assert c.get("/api/faktury").status_code == 401
    odp = c.get("/api/faktury", headers={"Authorization": "Bearer tok"})
    assert odp.status_code == 200
    odp = c.get("/api/faktury", headers={"Authorization": "Bearer zly"})
    assert odp.status_code == 401


# --- wgrywanie -------------------------------------------------------------

def test_dwa_rozne_pliki_o_tej_samej_nazwie_w_jednym_zadaniu(klient, konf):
    c = klient()
    odp = _wgraj(c, ("faktura.pdf", b"AAA"), ("faktura.pdf", b"BBB"))
    assert odp.json["zapisane"] == 2
    assert odp.json["zmienione_nazwy"] == ["faktura.pdf → faktura_1.pdf"]
    pdfy = sorted(f for f in os.listdir(konf.katalog_danych)
                  if f.endswith(".pdf"))
    assert pdfy == ["faktura.pdf", "faktura_1.pdf"]
    assert len(c.get("/api/faktury").json["faktury"]) == 2


def test_plik_nie_pdf_jest_pominiety_z_powodem(klient, konf):
    odp = _wgraj(klient(), ("notatki.txt", b"x"))
    assert odp.json["zapisane"] == 0
    assert odp.json["pominiete"] == ["notatki.txt: to nie jest PDF"]


def test_nazwa_ze_sciezka_laduje_w_katalogu_danych(klient, konf, tmp_path):
    odp = _wgraj(klient(), ("../x.pdf", b"AAA"))
    assert odp.json["zapisane"] == 1
    assert os.path.exists(os.path.join(konf.katalog_danych, "x.pdf"))
    assert not os.path.exists(tmp_path / "x.pdf")


def test_ta_sama_tresc_nie_jest_przyjmowana_drugi_raz(klient):
    c = klient()
    _wgraj(c, ("a.pdf", b"AAA"))
    odp = _wgraj(c, ("b.pdf", b"AAA"))
    assert odp.json["zapisane"] == 0
    assert odp.json["pominiete"] == ["b.pdf: już w kolejce"]


# --- zatwierdzanie ---------------------------------------------------------

def test_zatwierdzanie_nieistniejacej_faktury_daje_404(klient):
    assert klient().post("/api/faktury/999/zatwierdz").status_code == 404


def test_zatwierdzanie_faktury_w_kolejce_daje_409(klient, con, konf):
    c = klient()
    _wgraj(c, ("a.pdf", b"AAA"))
    fid = con.execute("SELECT id FROM faktury").fetchone()[0]
    assert c.post(f"/api/faktury/{fid}/zatwierdz").status_code == 409
    assert con.execute("SELECT status FROM faktury").fetchone() == (
        "do_przetworzenia",)


def test_zatwierdzanie_wyeksportowanej_daje_409(klient, con, wstaw):
    fid = wstaw("a.pdf")
    con.execute("UPDATE faktury SET status = 'wyeksportowane'")
    con.commit()
    assert klient().post(f"/api/faktury/{fid}/zatwierdz").status_code == 409


def test_zatwierdzanie_z_loginem_z_sesji(zalogowany, con, wstaw):
    fid = wstaw("a.pdf")
    assert zalogowany.post(f"/api/faktury/{fid}/zatwierdz").status_code == 200
    assert con.execute("SELECT status, zatwierdzil FROM faktury").fetchone() \
        == ("zatwierdzone", "ania")
    # drugi raz: już zatwierdzona
    assert zalogowany.post(f"/api/faktury/{fid}/zatwierdz").status_code == 409


def test_tryb_deweloperski_zapisuje_zatwierdzajacego(klient, con, wstaw):
    fid = wstaw("a.pdf")
    assert klient().post(f"/api/faktury/{fid}/zatwierdz").status_code == 200
    assert con.execute("SELECT zatwierdzil FROM faktury").fetchone() == (
        "tryb-deweloperski",)


def test_token_monitora_jako_zatwierdzajacy(klient, konta_testowe, con, wstaw):
    c = klient(konta=konta_testowe, secret_key="x" * 32, panel_token="tok")
    fid = wstaw("a.pdf")
    odp = c.post(f"/api/faktury/{fid}/zatwierdz",
                 headers={"Authorization": "Bearer tok"})
    assert odp.status_code == 200
    assert con.execute("SELECT zatwierdzil FROM faktury").fetchone() == (
        "token-monitora",)


# --- eksport ---------------------------------------------------------------

def test_eksport_bez_zatwierdzonych_daje_409(klient):
    assert klient().post("/api/eksport").status_code == 409


def test_eksport_zatwierdzonych_zwraca_plik_i_liczby(klient, con, wstaw):
    c = klient()
    fid = wstaw("a.pdf")
    c.post(f"/api/faktury/{fid}/zatwierdz")
    odp = c.post("/api/eksport")
    assert odp.status_code == 200
    assert (odp.json["liczba"], odp.json["pdfy"], odp.json["plik"]) == (
        1, 1, "eksport.xml")
    assert c.get("/eksport").status_code == 200


def test_zajeta_blokada_eksportu_daje_409(klient, con, wstaw):
    c = klient()
    fid = wstaw("a.pdf")
    c.post(f"/api/faktury/{fid}/zatwierdz")
    zamek = c.application.extensions["omega"].zamek_eksportu
    assert zamek.acquire(blocking=False)
    try:
        odp = c.post("/api/eksport")
        assert odp.status_code == 409 and "trwa" in odp.json["blad"]
    finally:
        zamek.release()
    assert con.execute("SELECT status FROM faktury").fetchone() == (
        "zatwierdzone",)


# --- nagłówki i ciasteczko -------------------------------------------------

def test_naglowki_bezpieczenstwa_na_stronie_glownej(klient):
    h = klient().get("/").headers
    assert h["X-Frame-Options"] == "DENY"
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["Referrer-Policy"] == "same-origin"
    csp = h["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "object-src 'self'" in csp and "base-uri 'none'" in csp


def test_pdf_mozna_osadzic_tylko_we_wlasnym_panelu(klient, wstaw):
    wstaw("a.pdf")
    h = klient().get("/pdf/a.pdf").headers
    assert h["X-Frame-Options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in h["Content-Security-Policy"]
    assert h["X-Content-Type-Options"] == "nosniff"


def test_za_proxy_hsts_i_ciasteczko_secure(klient, konta_testowe):
    c = klient(konta=konta_testowe, secret_key="x" * 32, za_proxy=True)
    assert "Strict-Transport-Security" in c.get("/login").headers
    odp = c.post("/login", data={"login": "ania", "haslo": HASLO_TESTOWE})
    assert "Secure" in odp.headers["Set-Cookie"]


def test_bez_proxy_brak_hsts_i_flagi_secure(klient, konta_testowe):
    c = klient(konta=konta_testowe, secret_key="x" * 32)
    assert "Strict-Transport-Security" not in c.get("/login").headers
    odp = c.post("/login", data={"login": "ania", "haslo": HASLO_TESTOWE})
    assert "Secure" not in odp.headers["Set-Cookie"]
    assert "HttpOnly" in odp.headers["Set-Cookie"]


def test_tryb_deweloperski_daje_dostep_bez_logowania(klient):
    c = klient()
    assert c.get("/api/faktury").status_code == 200
    assert c.get("/").status_code == 200


def test_strona_glowna_dziala_z_innego_katalogu_roboczego(
        klient, tmp_path, monkeypatch):
    c = klient()   # aplikacja zbudowana w katalogu repozytorium
    inny = tmp_path / "inny"
    inny.mkdir()
    monkeypatch.chdir(inny)
    odp = c.get("/")
    assert odp.status_code == 200 and b"Panel faktur" in odp.data
    assert c.get("/login").status_code in (200, 302)
