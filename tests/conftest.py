"""Wspólne fixtures: konfiguracja na katalogach tymczasowych, baza,
fabryka klienta Flaska i sztuczny odczyt AI (żadnego prawdziwego Gemini)."""

import dataclasses
import os
import sys

import pytest
from werkzeug.security import generate_password_hash

# repozytorium (moduły płaskie: baza.py, panel.py...) na ścieżce importu:
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import baza  # noqa: E402
import konfiguracja  # noqa: E402
import panel  # noqa: E402
import weryfikator  # noqa: E402


def _faktura(**zmiany) -> dict:
    """Poprawny słownik danych faktury (jak z modelu), z nadpisanymi polami."""
    dane = {
        "numer": "FV/1/2026",
        "nazwa_sprzedawcy": "Sprzedawca Sp. z o.o.",
        "nip_sprzedawcy": "5260250274",   # poprawna suma kontrolna
        "nazwa_nabywcy": "Nabywca S.A.",
        "nip_nabywcy": "1234563218",
        "data_wystawienia": "2026-11-01",
        "termin_platnosci": "2026-11-15",
        "netto": 100.0,
        "vat": 23.0,
        "brutto": 123.0,
        "pozycje": [{"nazwa": "Usługa", "ilosc": 1.0,
                     "cena_netto": 100.0, "wartosc_netto": 100.0}],
    }
    dane.update(zmiany)
    return dane


@pytest.fixture
def faktura():
    return _faktura


@pytest.fixture
def konf(tmp_path):
    k = konfiguracja.Konfiguracja(
        katalog_danych=str(tmp_path / "data"),
        katalog_eksportu=str(tmp_path / "przetworzone"),
        katalog_logow=str(tmp_path / "logi"),
    )
    for katalog in (k.katalog_danych, k.katalog_eksportu, k.katalog_logow):
        os.makedirs(katalog)
    return k


@pytest.fixture
def con(konf):
    polaczenie = baza.polacz(konf.sciezka_bazy)
    yield polaczenie
    baza.zamknij(polaczenie)


HASLO_TESTOWE = "tajne-haslo"


@pytest.fixture
def klient(konf):
    """Fabryka: klient(**zmiany) -> test_client() nowej aplikacji.

    Domyślnie tryb deweloperski (bez kont). Przykład logowania:
    klient(konta={"ania": hash}, secret_key="x" * 32).
    """
    def fabryka(**zmiany):
        k = dataclasses.replace(konf, **zmiany)
        return panel.utworz_app(k).test_client()
    return fabryka


@pytest.fixture
def konta_testowe():
    """Konto 'ania' z hashem werkzeug (hasło: HASLO_TESTOWE)."""
    return {"ania": generate_password_hash(HASLO_TESTOWE)}


@pytest.fixture
def wstaw(con, konf, faktura):
    """wstaw(plik, **zmiany_danych) -> id faktury po ekstrakcji (PDF-em jest
    sztuczny plik z unikalną treścią, żeby odciski się nie zderzały)."""
    def _wstaw(plik, bledy=None, **zmiany):
        tresc = f"%PDF-sztuczny {plik}".encode()
        nazwa, powod = baza.dodaj_do_kolejki(
            con, plik, baza.odcisk_bajtow(tresc), konf.katalog_danych,
            lambda n: open(os.path.join(konf.katalog_danych, n), "wb")
            .write(tresc))
        assert powod is None
        fid = con.execute("SELECT id FROM faktury WHERE plik = ?",
                          (nazwa,)).fetchone()[0]
        baza.zapisz_wynik_ekstrakcji(con, fid, faktura(**zmiany),
                                     bledy or [])
        return fid
    return _wstaw


@pytest.fixture
def sztuczny_odczyt(monkeypatch):
    """Podmienia odczyt AI: zwraca słownik {nazwa_pliku: dane | Exception}.

    Test wypełnia słownik; wartość-wyjątek jest rzucana z wyodrebnij_dane.
    """
    wyniki: dict = {}

    def renderuj(sciezka):
        return b"PNG:" + os.path.basename(sciezka).encode()

    def wyodrebnij(klient, png):
        wynik = wyniki[png[4:].decode()]
        if isinstance(wynik, Exception):
            raise wynik
        return wynik

    monkeypatch.setattr(weryfikator, "renderuj_pdf_do_png", renderuj)
    monkeypatch.setattr(weryfikator, "wyodrebnij_dane", wyodrebnij)
    monkeypatch.setattr(weryfikator, "PRZERWA", 0)
    return wyniki
