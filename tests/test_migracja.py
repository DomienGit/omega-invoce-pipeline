"""Migracja bazy ze schematu sprzed zapisu zatwierdzającego.

STARY_SCHEMAT to DDL z baza.py z commitu a21440d (przed kolumnami
zatwierdzil / zatwierdzone_at) — wklejony jako tekst SQL.
"""

import sqlite3

import baza

STATUSY = ("'do_przetworzenia', 'do_weryfikacji', 'do_zatwierdzenia', "
           "'zatwierdzone', 'wyeksportowane'")

STARY_SCHEMAT = f"""
CREATE TABLE IF NOT EXISTS faktury (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    plik              TEXT,
    odcisk_pliku      TEXT,
    blad_przetworzenia TEXT,
    wgrane_at         TEXT,
    przetworzone_at   TEXT,
    numer             TEXT,
    nazwa_sprzedawcy  TEXT,
    nip_sprzedawcy    TEXT,
    nazwa_nabywcy     TEXT,
    nip_nabywcy       TEXT,
    data_wystawienia  TEXT,
    termin_platnosci  TEXT,
    netto             REAL,
    vat               REAL,
    brutto            REAL,
    bledy             TEXT NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL CHECK (status IN ({STATUSY}))
);
CREATE TABLE IF NOT EXISTS pozycje (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    faktura_id    INTEGER NOT NULL REFERENCES faktury(id) ON DELETE CASCADE,
    nazwa         TEXT,
    ilosc         REAL,
    cena_netto    REAL,
    wartosc_netto REAL,
    status        TEXT NOT NULL CHECK (status IN ({STATUSY}))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_aktywny_plik
    ON faktury(plik) WHERE status != 'wyeksportowane';
"""


def _stara_baza(sciezka):
    s = sqlite3.connect(sciezka)
    s.executescript(STARY_SCHEMAT)
    s.execute("INSERT INTO faktury (plik, numer, nip_sprzedawcy, netto, vat, "
              "brutto, status) VALUES ('a.pdf', 'FV/1', '5260250274', "
              "100, 23, 123, 'zatwierdzone')")
    s.execute("INSERT INTO pozycje (faktura_id, nazwa, ilosc, cena_netto, "
              "wartosc_netto, status) VALUES (1, 'Usługa', 1, 100, 100, "
              "'zatwierdzone')")
    s.commit()
    s.close()


def test_stara_baza_zachowuje_dane_i_dostaje_nowe_kolumny(tmp_path):
    sciezka = str(tmp_path / "stara.db")
    _stara_baza(sciezka)
    przed = sqlite3.connect(sciezka)
    kolumny_przed = {w[1] for w in przed.execute("PRAGMA table_info(faktury)")}
    przed.close()  # otwarte połączenie blokowałoby przebudowę tabel
    assert "zatwierdzil" not in kolumny_przed

    con = baza.polacz(sciezka)
    try:
        kolumny = {w[1] for w in con.execute("PRAGMA table_info(faktury)")}
        assert {"zatwierdzil", "zatwierdzone_at"} <= kolumny
        assert con.execute(
            "SELECT plik, numer, status, zatwierdzil, zatwierdzone_at "
            "FROM faktury").fetchall() == [
            ("a.pdf", "FV/1", "zatwierdzone", None, None)]
        assert con.execute(
            "SELECT nazwa, status FROM pozycje").fetchall() == [
            ("Usługa", "zatwierdzone")]
        f = baza.pobierz_szczegoly(con, 1)
        assert f["zatwierdzil"] is None and len(f["pozycje"]) == 1
    finally:
        baza.zamknij(con)


def test_ponowne_otwarcie_zmigrowanej_bazy_dziala(tmp_path):
    sciezka = str(tmp_path / "stara.db")
    _stara_baza(sciezka)
    baza.zamknij(baza.polacz(sciezka))
    con = baza.polacz(sciezka)
    try:
        assert con.execute("SELECT COUNT(*) FROM faktury").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM pozycje").fetchone()[0] == 1
    finally:
        baza.zamknij(con)


def test_zmigrowana_baza_przyjmuje_nowe_zatwierdzenia(tmp_path, faktura):
    sciezka = str(tmp_path / "stara.db")
    _stara_baza(sciezka)
    con = baza.polacz(sciezka)
    try:
        baza.zapisz_wynik_ekstrakcji(
            con, 1, faktura(), [])   # faktura 'zatwierdzone' -> ekstrakcja
        con.execute("UPDATE faktury SET status = 'do_zatwierdzenia'")
        con.commit()
        assert baza.zatwierdz_fakture(con, 1, "ania")
        assert con.execute("SELECT zatwierdzil FROM faktury").fetchone() == (
            "ania",)
    finally:
        baza.zamknij(con)
