"""Warstwa SQLite — spis faktur i ich pozycji (data/faktury.db).

Tabele (dokładnie wg notatki "Baza danych faktur"):
  faktury  — nagłówki: numer, strony (nazwy + NIP-y), daty, kwoty
             netto/VAT/brutto, błędy walidacji (JSON), status workflow.
             UNIQUE (numer, nip_sprzedawcy) — wykrywanie duplikatów
             (sama detekcja zostaje w weryfikatorze, na wynikach).
  pozycje  — po jednym wierszu na pozycję faktury: id, faktura_id
             (klucz obcy, ON DELETE CASCADE) i status.

Zapis jest idempotentny: ponowny zapis tej samej faktury (ten sam
numer + NIP sprzedawcy) zastępuje poprzedni wpis i jego pozycje,
z jednym wyjątkiem — status 'zatwierdzone' (ludzka akceptacja)
nie jest nadpisywany przez re-run.

Użycie z weryfikatora:
  con = baza.polacz()
  baza.zapisz_fakture(con, rekord)
  baza.zamknij(con)
"""

import json
import re
import sqlite3

SCIEZKA_BAZY = "data/faktury.db"

STATUSY = ("'do_weryfikacji', 'do_zatwierdzenia', 'zatwierdzone'")

DDL = f"""
CREATE TABLE IF NOT EXISTS faktury (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    numer            TEXT,
    nazwa_sprzedawcy TEXT,
    nip_sprzedawcy   TEXT,
    nazwa_nabywcy    TEXT,
    nip_nabywcy      TEXT,
    data_wystawienia TEXT,
    termin_platnosci TEXT,
    netto            REAL,
    vat              REAL,
    brutto           REAL,
    bledy            TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL CHECK (status IN ({STATUSY})),
    UNIQUE (numer, nip_sprzedawcy)
);

CREATE TABLE IF NOT EXISTS pozycje (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    faktura_id INTEGER NOT NULL REFERENCES faktury(id) ON DELETE CASCADE,
    status     TEXT NOT NULL CHECK (status IN ({STATUSY}))
);
"""


def normalizuj_nip(nip: str | None) -> str | None:
    """Zwraca NIP jako same cyfry; None/pusty/string bez cyfr -> None."""
    if not nip:
        return None
    cyfry = re.sub(r"\D", "", nip)
    return cyfry or None


def polacz(sciezka: str = SCIEZKA_BAZY) -> sqlite3.Connection:
    """Otwiera bazę i tworzy tabele, jeśli jeszcze ich nie ma."""
    con = sqlite3.connect(sciezka)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(DDL)
    return con


def zamknij(con: sqlite3.Connection) -> None:
    con.close()


def zapisz_fakture(con: sqlite3.Connection, rekord: dict) -> None:
    """Zapisuje/aktualizuje fakturę i jej pozycje.

    rekord = jeden wpis z wyników weryfikacji (klucze jak kolumny tabeli;
    dodatkowe klucze jak 'plik' są ignorowane, 'bledy' to lista,
    'pozycje' decyduje o liczbie wierszy w tabeli pozycje).
    Ten sam numer + NIP sprzedawcy = ta sama faktura: poprzedni wpis
    (i jego pozycje) jest zastępowany. Faktura bez numeru nie jest
    zastępowana (brak tożsamości) — po prostu dostaje nowy wiersz.
    """
    numer = (rekord.get("numer") or "").strip() or None
    nip_sprzedawcy = normalizuj_nip(rekord.get("nip_sprzedawcy"))
    nip_nabywcy = normalizuj_nip(rekord.get("nip_nabywcy"))
    bledy = rekord.get("bledy") or []
    status = "do_zatwierdzenia" if not bledy else "do_weryfikacji"

    with con:  # jedna transakcja: nagłówek + pozycje
        if numer is not None:
            # re-run nie nadpisze ludzkiej akceptacji:
            wiersz = con.execute(
                "SELECT status FROM faktury "
                "WHERE numer = ? AND nip_sprzedawcy IS ?",
                (numer, nip_sprzedawcy),
            ).fetchone()
            if wiersz and wiersz[0] == "zatwierdzone":
                status = "zatwierdzone"
            # zastąp poprzedni wpis tej samej faktury (kasuje też pozycje
            # przez ON DELETE CASCADE):
            con.execute(
                "DELETE FROM faktury WHERE numer = ? AND nip_sprzedawcy IS ?",
                (numer, nip_sprzedawcy),
            )
        cur = con.execute(
            "INSERT INTO faktury (numer, nazwa_sprzedawcy, nip_sprzedawcy, "
            "nazwa_nabywcy, nip_nabywcy, data_wystawienia, termin_platnosci, "
            "netto, vat, brutto, bledy, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (numer, rekord.get("nazwa_sprzedawcy"), nip_sprzedawcy,
             rekord.get("nazwa_nabywcy"), nip_nabywcy,
             rekord.get("data_wystawienia"), rekord.get("termin_platnosci"),
             rekord.get("netto"), rekord.get("vat"), rekord.get("brutto"),
             json.dumps(bledy, ensure_ascii=False), status),
        )
        faktura_id = cur.lastrowid  # zwykły INSERT — lastrowid jest wiarygodny
        con.executemany(
            "INSERT INTO pozycje (faktura_id, status) VALUES (?, ?)",
            [(faktura_id, status)] * len(rekord.get("pozycje") or []),
        )
