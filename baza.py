"""Warstwa SQLite — spis faktur i ich pozycji (data/faktury.db).

Tabele (dokładnie wg notatki "Baza danych faktur"):
  faktury  — nagłówki: numer, strony (nazwy + NIP-y), daty, kwoty
             netto/VAT/brutto, błędy walidacji (JSON), status workflow,
             nazwa pliku PDF (plik — powiązanie z oryginałem w panelu).
             UNIQUE (numer, nip_sprzedawcy) — wykrywanie duplikatów
             (sama detekcja zostaje w weryfikatorze, na wynikach),
             UNIQUE (plik) — jedna faktura na plik.
  pozycje  — wiersze tabeli faktury: nazwa, ilosc, cena_netto,
             wartosc_netto (to, co wyciąga model w weryfikatorze)
             + status. Klucz obcy do faktury (ON DELETE CASCADE).

Zapis jest idempotentny: ponowny zapis tej samej faktury (ten sam
plik albo ten sam numer + NIP sprzedawcy) zastępuje poprzedni wpis
i jego pozycje, z jednym wyjątkiem — status 'zatwierdzone' (ludzka
akceptacja) nie jest nadpisywany przez re-run.

Migracja: bazy sprzed kolumny `plik` są uzupełniane w polacz() —
ALTER TABLE + backfill numerów/NIP-ów z data/wyniki_weryfikacji.json.

Użycie z weryfikatora / panelu:
  con = baza.polacz()
  baza.zapisz_fakture(con, rekord)
  faktury = baza.pobierz_faktury(con)          # lista do panelu
  sz = baza.pobierz_szczegoly(con, faktura_id) # + pozycje
  baza.zamknij(con)
"""

import json
import os
import re
import sqlite3

SCIEZKA_BAZY = "data/faktury.db"

STATUSY = ("'do_weryfikacji', 'do_zatwierdzenia', 'zatwierdzone'")

DDL = f"""
CREATE TABLE IF NOT EXISTS faktury (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    plik             TEXT,
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
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    faktura_id    INTEGER NOT NULL REFERENCES faktury(id) ON DELETE CASCADE,
    nazwa         TEXT,
    ilosc         REAL,
    cena_netto    REAL,
    wartosc_netto REAL,
    status        TEXT NOT NULL CHECK (status IN ({STATUSY}))
);
"""


def normalizuj_nip(nip: str | None) -> str | None:
    """Zwraca NIP jako same cyfry; None/pusty/string bez cyfr -> None."""
    if not nip:
        return None
    cyfry = re.sub(r"\D", "", nip)
    return cyfry or None


def _migruj_plik(con: sqlite3.Connection) -> None:
    """Doprowadza starszą bazę (bez kolumny plik) do aktualnego schematu.

    ALTER TABLE dodaje kolumnę, po czym wiersze bez pliku są uzupełniane
    z data/wyniki_weryfikacji.json (dopasowanie po numerze + NIP-ie
    sprzedawcy, znormalizowanym tak jak przy zapisie). Samo UPDATE —
    nie narusza unikalności indeksu na plik.
    """
    kolumny = {w[1] for w in con.execute("PRAGMA table_info(faktury)")}
    sciezka_json = os.path.join("data", "wyniki_weryfikacji.json")
    if "plik" not in kolumny and os.path.exists(sciezka_json):
        with open(sciezka_json, encoding="utf-8") as plik:
            wyniki = [w for w in json.load(plik) if "pozycje" in w]
    else:
        wyniki = []

    with con:
        if "plik" not in kolumny:
            con.execute("ALTER TABLE faktury ADD COLUMN plik TEXT")
        for w in wyniki:
            numer = (w.get("numer") or "").strip() or None
            nip = normalizuj_nip(w.get("nip_sprzedawcy"))
            # ... AND plik IS NULL — re-run nigdy nie nadpisuje istniejącej
            # nazwy pliku (piszemy tylko w puste miejsca):
            con.execute(
                "UPDATE faktury SET plik = ? "
                "WHERE numer = ? AND nip_sprzedawcy IS ? AND plik IS NULL",
                (w.get("plik"), numer, nip),
            )


def polacz(sciezka: str = SCIEZKA_BAZY) -> sqlite3.Connection:
    """Otwiera bazę, tworzy tabele i migruje stary schemat (bez plik)."""
    con = sqlite3.connect(sciezka)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(DDL)
    _migruj_plik(con)
    # indeks dopiero po migracji — na starszej bazie kolumna plik
    # powstaje w _migruj_plik, więc CREATE INDEX tutaj ma ją już zawsze:
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_faktury_plik ON faktury(plik)")
    return con


def zamknij(con: sqlite3.Connection) -> None:
    con.close()


def zatwierdz_fakture(con: sqlite3.Connection, faktura_id: int) -> bool:
    """Ustawia status 'zatwierdzone' fakturze i jej pozycjom.

    Zwraca False, gdy faktura nie istnieje. Decyzja o dopuszczalności
    zatwierdzenia (np. faktura bez błędów) należy do wywołującego.
    """
    with con:
        cur = con.execute(
            "UPDATE faktury SET status = 'zatwierdzone' WHERE id = ?",
            (faktura_id,),
        )
        con.execute(
            "UPDATE pozycje SET status = 'zatwierdzone' WHERE faktura_id = ?",
            (faktura_id,),
        )
    return cur.rowcount > 0


def zapisz_fakture(con: sqlite3.Connection, rekord: dict) -> None:
    """Zapisuje/aktualizuje fakturę i jej pozycje.

    rekord = jeden wpis z wyników weryfikacji (klucze jak kolumny tabeli;
    'bledy' to lista, 'pozycje' decyduje o liczbie wierszy w tabeli
    pozycje).
    Ta sama faktura = ten sam plik albo ten sam numer + NIP sprzedawcy:
    poprzedni wpis (i jego pozycje) jest zastępowany. Faktura bez pliku
    i bez numeru nie jest zastępowana (brak tożsamości) — dostaje nowy
    wiersz.
    """
    plik = (rekord.get("plik") or "").strip() or None
    numer = (rekord.get("numer") or "").strip() or None
    nip_sprzedawcy = normalizuj_nip(rekord.get("nip_sprzedawcy"))
    nip_nabywcy = normalizuj_nip(rekord.get("nip_nabywcy"))
    bledy = rekord.get("bledy") or []
    status = "do_zatwierdzenia" if not bledy else "do_weryfikacji"

    with con:  # jedna transakcja: nagłówek + pozycje
        # plik = ? z NULL niczego nie dopasowuje, więc rekord bez pliku
        # szuka tożsamości tylko po numerze + NIP-ie (jak dotychczas):
        tozsamosc = (plik, numer, nip_sprzedawcy)
        if plik is not None or numer is not None:
            # re-run nie nadpisze ludzkiej akceptacji:
            wiersz = con.execute(
                "SELECT status FROM faktury "
                "WHERE plik = ? OR (numer = ? AND nip_sprzedawcy IS ?)",
                tozsamosc,
            ).fetchone()
            if wiersz and wiersz[0] == "zatwierdzone":
                status = "zatwierdzone"
            # zastąp poprzedni wpis tej samej faktury (kasuje też pozycje
            # przez ON DELETE CASCADE):
            con.execute(
                "DELETE FROM faktury "
                "WHERE plik = ? OR (numer = ? AND nip_sprzedawcy IS ?)",
                tozsamosc,
            )
        cur = con.execute(
            "INSERT INTO faktury (plik, numer, nazwa_sprzedawcy, "
            "nip_sprzedawcy, nazwa_nabywcy, nip_nabywcy, data_wystawienia, "
            "termin_platnosci, netto, vat, brutto, bledy, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plik, numer, rekord.get("nazwa_sprzedawcy"), nip_sprzedawcy,
             rekord.get("nazwa_nabywcy"), nip_nabywcy,
             rekord.get("data_wystawienia"), rekord.get("termin_platnosci"),
             rekord.get("netto"), rekord.get("vat"), rekord.get("brutto"),
             json.dumps(bledy, ensure_ascii=False), status),
        )
        faktura_id = cur.lastrowid  # zwykły INSERT — lastrowid jest wiarygodny
        con.executemany(
            "INSERT INTO pozycje (faktura_id, nazwa, ilosc, cena_netto, "
            "wartosc_netto, status) VALUES (?, ?, ?, ?, ?, ?)",
            [(faktura_id, p.get("nazwa"), p.get("ilosc"), p.get("cena_netto"),
              p.get("wartosc_netto"), status)
             for p in (rekord.get("pozycje") or [])],
        )


KOLUMNY_FAKTURY = (
    "id", "plik", "numer", "nazwa_sprzedawcy", "nip_sprzedawcy",
    "nazwa_nabywcy", "nip_nabywcy", "data_wystawienia", "termin_platnosci",
    "netto", "vat", "brutto", "bledy", "status",
)


def pobierz_faktury(con: sqlite3.Connection,
                    status: str | None = None) -> list[dict]:
    """Lista faktur do panelu (bez pozycji, z ich liczbą).

    status=None -> wszystkie; sortowanie po numerze faktury.
    'bledy' są zwracane jako lista (sparsowane z JSON).
    """
    zapytanie = (
        f"SELECT {', '.join('f.' + k for k in KOLUMNY_FAKTURY)}, "
        "(SELECT COUNT(*) FROM pozycje p WHERE p.faktura_id = f.id) "
        "AS liczba_pozycji FROM faktury f"
    )
    parametry: tuple = ()
    if status is not None:
        zapytanie += " WHERE f.status = ?"
        parametry = (status,)
    zapytanie += " ORDER BY f.numer"

    faktury = []
    for wiersz in con.execute(zapytanie, parametry):
        f = dict(zip(KOLUMNY_FAKTURY + ("liczba_pozycji",), wiersz))
        f["bledy"] = json.loads(f["bledy"] or "[]")
        faktury.append(f)
    return faktury


def pobierz_szczegoly(con: sqlite3.Connection, faktura_id: int) -> dict | None:
    """Jedna faktura + jej pozycje (nazwa, ilosc, cena_netto, wartosc_netto).

    None, gdy nie ma faktury o tym id.
    """
    wiersz = con.execute(
        f"SELECT {', '.join(KOLUMNY_FAKTURY)} FROM faktury WHERE id = ?",
        (faktura_id,),
    ).fetchone()
    if wiersz is None:
        return None

    f = dict(zip(KOLUMNY_FAKTURY, wiersz))
    f["bledy"] = json.loads(f["bledy"] or "[]")
    f["pozycje"] = [
        {"nazwa": nazwa, "ilosc": ilosc, "cena_netto": cena,
         "wartosc_netto": wartosc}
        for (nazwa, ilosc, cena, wartosc) in con.execute(
            "SELECT nazwa, ilosc, cena_netto, wartosc_netto "
            "FROM pozycje WHERE faktura_id = ? ORDER BY id", (faktura_id,))
    ]
    return f
