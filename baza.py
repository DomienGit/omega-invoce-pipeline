"""Warstwa SQLite — spis faktur i ich pozycji (data/faktury.db).

Baza jest JEDYNYM magazynem wyników (dawniej był też
data/wyniki_weryfikacji.json — usunięty).

  faktury  — nagłówki: numer, strony (nazwy + NIP-y), daty, kwoty
             netto/VAT/brutto, błędy walidacji (JSON), status workflow,
             nazwa pliku PDF (plik) + jego odcisk (odcisk_pliku — SHA-256
             zawartości) oraz blad_przetworzenia (niepusty = próba
             odczytu nieudana: zepsuty PDF / API; taka faktura jest
             ponawiana przy kolejnym przebiegu).
  pozycje  — wiersze tabeli faktury: nazwa, ilosc, cena_netto,
             wartosc_netto (to, co wyciąga model w weryfikatorze)
             + status. Klucz obcy do faktury (ON DELETE CASCADE).

Statusy: do_weryfikacji / do_zatwierdzenia / zatwierdzone /
wyeksportowane. Wyeksportowane to HISTORIA: znikają z panelu i nie są
nadpisywane, ale też nie blokują przyjęcia nowego pliku — unikalność
(plik oraz numer + NIP sprzedawcy) obowiązuje tylko wśród faktur
aktywnych (częściowe indeksy), więc historia może powtarzać nazwy
i numery.

Odcisk pliku rozstrzyga, czy plik o znanej nazwie to "ta sama faktura"
(pomijamy — już przetworzona) czy "nowa treść pod starą nazwą"
(przetwarzamy). Dzięki temu pliki wyeksportowane, które nadal leżą
w data/, NIE są przetwarzane w kółko przy kolejnych uruchomieniach.

Zapis jest idempotentny: ponowny zapis tej samej faktury (ten sam plik
albo ten sam numer + NIP sprzedawcy, wśród aktywnych) zastępuje poprzedni
wpis i jego pozycje — z wyjątkiem statusów 'zatwierdzone' (ludzka
akceptacja) i 'wyeksportowane' (historia), których re-run nie cofa.
Jeżeli zapisywana faktura powtarza numer + NIP faktury już
wyeksportowanej, dostaje błąd "duplikat faktury wcześniej wyeksportowanej"
— do decyzji księgowej.

Migracje w polacz(): stare tabele (tabelowy UNIQUE, CHECK bez
'wyeksportowane', brak kolumn odcisk_pliku/blad_przetworzenia) są
przebudowywane; unikalność przenoszona do indeksów częściowych.

Użycie z weryfikatora / panelu:
  con = baza.polacz()
  baza.zapisz_fakture(con, rekord)
  faktury = baza.pobierz_faktury(con)          # lista do panelu
  sz = baza.pobierz_szczegoly(con, faktura_id) # + pozycje
  baza.zamknij(con)
"""

import hashlib
import json
import re
import sqlite3

SCIEZKA_BAZY = "data/faktury.db"

STATUSY = ("'do_weryfikacji', 'do_zatwierdzenia', 'zatwierdzone', "
           "'wyeksportowane'")

DDL_FAKTURY = f"""
CREATE TABLE IF NOT EXISTS faktury (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    plik              TEXT,
    odcisk_pliku      TEXT,
    blad_przetworzenia TEXT,
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
"""

DDL_POZYCJE = f"""
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

DDL = DDL_FAKTURY + DDL_POZYCJE

# Unikalność tylko wśród faktur aktywnych — wyeksportowane (historia)
# mogą powtarzać nazwy plików i numery faktur:
DDL_INDEKSY = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_aktywny_plik
    ON faktury(plik) WHERE status != 'wyeksportowane';
CREATE UNIQUE INDEX IF NOT EXISTS uq_aktywny_numer
    ON faktury(numer, nip_sprzedawcy) WHERE status != 'wyeksportowane';
"""

# kolumny kopiowane przy przebudowie tabeli (stare tabele mogą nie mieć
# wszystkich — brakujące dostają NULL):
KOLUMNY_MIGRACJI = (
    "id", "plik", "odcisk_pliku", "blad_przetworzenia", "numer",
    "nazwa_sprzedawcy", "nip_sprzedawcy", "nazwa_nabywcy", "nip_nabywcy",
    "data_wystawienia", "termin_platnosci", "netto", "vat", "brutto",
    "bledy", "status",
)


def normalizuj_nip(nip: str | None) -> str | None:
    """Zwraca NIP jako same cyfry; None/pusty/string bez cyfr -> None."""
    if not nip:
        return None
    cyfry = re.sub(r"\D", "", nip)
    return cyfry or None


def odcisk_pliku(sciezka: str) -> str:
    """SHA-256 zawartości pliku — identyfikator "tej samej" treści."""
    sha = hashlib.sha256()
    with open(sciezka, "rb") as plik:
        for porcja in iter(lambda: plik.read(65536), b""):
            sha.update(porcja)
    return sha.hexdigest()


def _migruj_schemat(con: sqlite3.Connection) -> None:
    """Przebudowuje starą tabelę faktury do aktualnego schematu.

    SQLite nie potrafi zmieniać constraintów w miejscu, więc stara
    tabela jest przemianowywana, powstaje nowa (aktualny DDL), dane
    są kopiowane (brakujące kolumny dostają NULL), stare tabele
    kasowane. Nowe bazy — bez zmian.

    Przebudowa, gdy: CHECK nie zna 'wyeksportowane', jest tabelowy
    UNIQUE (numer, nip_sprzedawcy) — dziś unikalność tylko wśród
    aktywnych, indeksami częściowymi — albo brakuje kolumn
    odcisk_pliku / blad_przetworzenia.
    """
    sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='faktury'"
    ).fetchone()
    if sql is None:  # świeża baza — tabele dopiero co powstały z DDL
        return
    kolumny = {w[1] for w in con.execute("PRAGMA table_info(faktury)")}
    przestarzala = (
        "wyeksportowane" not in sql[0]
        or "UNIQUE" in sql[0]
        or not {"odcisk_pliku", "blad_przetworzenia"} <= kolumny
    )
    if not przestarzala:
        return

    stare = set(kolumny)
    selecty = ", ".join(c if c in stare else f"NULL AS {c}"
                        for c in KOLUMNY_MIGRACJI)

    con.execute("PRAGMA foreign_keys = OFF")  # przebudowa tabeli rodzica
    try:
        with con:
            con.execute("ALTER TABLE faktury RENAME TO faktury_stara")
            con.execute("ALTER TABLE pozycje RENAME TO pozycje_stare")
            con.execute(DDL_FAKTURY.strip())
            con.execute(DDL_POZYCJE.strip())
            con.execute(
                f"INSERT INTO faktury ({', '.join(KOLUMNY_MIGRACJI)}) "
                f"SELECT {selecty} FROM faktury_stara")
            con.execute(
                "INSERT INTO pozycje (id, faktura_id, nazwa, ilosc, "
                "cena_netto, wartosc_netto, status) "
                "SELECT id, faktura_id, nazwa, ilosc, cena_netto, "
                "wartosc_netto, status FROM pozycje_stare")
            con.execute("DROP TABLE pozycje_stare")
            con.execute("DROP TABLE faktury_stara")
    finally:
        con.execute("PRAGMA foreign_keys = ON")


def polacz(sciezka: str = SCIEZKA_BAZY) -> sqlite3.Connection:
    """Otwiera bazę, tworzy tabele i migruje stare schematy."""
    con = sqlite3.connect(sciezka)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(DDL)
    _migruj_schemat(con)
    # stary, pełny indeks na plik (z czasów, gdy historia nie mogła
    # powtarzać nazw) — zastąpiony częściowym:
    con.execute("DROP INDEX IF EXISTS uq_faktury_plik")
    con.executescript(DDL_INDEKSY)
    return con


def zamknij(con: sqlite3.Connection) -> None:
    con.close()


def uzupelnij_odciski(con: sqlite3.Connection, odciski: dict[str, str]) -> None:
    """Jednorazowy backfill odcisków po migracji schematu.

    odciski = {nazwa pliku: SHA-256}. Wypełniamy tylko puste pola
    wierszy bez błędu przetwarzania — wiersze z błędem i tak są
    ponawiane i dostaną odcisk po udanej próbie.
    """
    with con:
        for plik, odcisk in odciski.items():
            con.execute(
                "UPDATE faktury SET odcisk_pliku = ? "
                "WHERE plik = ? AND odcisk_pliku IS NULL "
                "AND blad_przetworzenia IS NULL",
                (odcisk, plik),
            )


def przetworzone_pliki(con: sqlite3.Connection,
                       odciski: dict[str, str]) -> set[str]:
    """Których PDF-ów NIE przetwarzać ponownie.

    odciski = {nazwa pliku w data/: aktualny SHA-256}. Pomijamy plik,
    gdy w bazie jest wiersz (aktywny albo wyeksportowany) o tej nazwie,
    bez błędu przetwarzania i o tym samym odcinku — czyli tę samą
    treść już kiedyś pomyślnie odczytano. Nowa treść pod starą nazwą
    (także po eksporcie) oraz nieudane próby — do przetworzenia.
    """
    znane = {(plik, odcisk) for (plik, odcisk) in con.execute(
        "SELECT plik, odcisk_pliku FROM faktury "
        "WHERE plik IS NOT NULL AND odcisk_pliku IS NOT NULL "
        "AND blad_przetworzenia IS NULL")}
    return {plik for plik, odcisk in odciski.items()
            if (plik, odcisk) in znane}


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


def oznacz_wyeksportowane(con: sqlite3.Connection) -> int:
    """Przenosi faktury 'zatwierdzone' do statusu 'wyeksportowane'.

    Wywoływane po udanym eksporcie — wyeksportowane faktury znikają
    z panelu i (jak 'zatwierdzone') nie są cofane przez re-run, ale
    pozostają w bazie jako historia. Zwraca liczbę zmienionych faktur.
    """
    with con:
        cur = con.execute(
            "UPDATE faktury SET status = 'wyeksportowane' "
            "WHERE status = 'zatwierdzone'")
        con.execute(
            "UPDATE pozycje SET status = 'wyeksportowane' "
            "WHERE status != 'wyeksportowane' AND faktura_id IN "
            "(SELECT id FROM faktury WHERE status = 'wyeksportowane')")
    return cur.rowcount


def zapisz_fakture(con: sqlite3.Connection, rekord: dict) -> None:
    """Zapisuje/aktualizuje fakturę i jej pozycje.

    rekord = jeden wpis z wyników weryfikacji (klucze jak kolumny tabeli;
    'bledy' to lista, 'pozycje' decyduje o liczbie wierszy w tabeli
    pozycje, 'odcisk_pliku' = SHA-256 pliku, 'blad_przetworzenia' =
    niepusty, gdy odczyt się nie udał — taka faktura jest ponawiana).
    Tożsamość (ten sam plik albo ten sam numer + NIP sprzedawcy) działa
    tylko wśród faktur AKTYWNYCH: wyeksportowane to historia — nie są
    zastępowane ani nie blokują nowego wiersza. Powtórka numeru + NIP
    faktury wyeksportowanej dostaje błąd "duplikat faktury wcześniej
    wyeksportowanej".
    """
    plik = (rekord.get("plik") or "").strip() or None
    numer = (rekord.get("numer") or "").strip() or None
    nip_sprzedawcy = normalizuj_nip(rekord.get("nip_sprzedawcy"))
    nip_nabywcy = normalizuj_nip(rekord.get("nip_nabywcy"))
    bledy = list(rekord.get("bledy") or [])
    blad_przetwarzania = (rekord.get("blad_przetwarzania") or "").strip() or None

    with con:  # jedna transakcja: nagłówek + pozycje
        # plik = ? z NULL niczego nie dopasowuje, więc rekord bez pliku
        # szuka tożsamości tylko po numerze + NIP-ie:
        tozsamosc = (plik, numer, nip_sprzedawcy)
        if plik is not None or numer is not None:
            wiersz = con.execute(
                "SELECT status FROM faktury "
                "WHERE (plik = ? OR (numer = ? AND nip_sprzedawcy IS ?)) "
                "AND status != 'wyeksportowane'",
                tozsamosc,
            ).fetchone()
            chroniony = wiersz and wiersz[0] == "zatwierdzone"
            # zastąp poprzedni aktywny wpis tej samej faktury (kasuje też
            # pozycje przez ON DELETE CASCADE); historia nietknięta:
            con.execute(
                "DELETE FROM faktury "
                "WHERE (plik = ? OR (numer = ? AND nip_sprzedawcy IS ?)) "
                "AND status != 'wyeksportowane'",
                tozsamosc,
            )
        else:
            chroniony = False

        # powtórka numeru + NIP względem historii — ostrzeż księgową:
        if numer is not None and not blad_przetwarzania:
            duplikat = con.execute(
                "SELECT plik FROM faktury "
                "WHERE numer = ? AND nip_sprzedawcy IS ? "
                "AND status = 'wyeksportowane'",
                (numer, nip_sprzedawcy),
            ).fetchone()
            if duplikat:
                opis = ("duplikat faktury wcześniej wyeksportowanej "
                        f"(poprzednio: {duplikat[0]})")
                if opis not in bledy:
                    bledy.append(opis)

        if chroniony:
            status = "zatwierdzone"  # akceptacja — nie cofamy re-runem
        else:
            status = "do_zatwierdzenia" if not bledy else "do_weryfikacji"

        cur = con.execute(
            "INSERT INTO faktury (plik, odcisk_pliku, blad_przetworzenia, "
            "numer, nazwa_sprzedawcy, nip_sprzedawcy, nazwa_nabywcy, "
            "nip_nabywcy, data_wystawienia, termin_platnosci, netto, vat, "
            "brutto, bledy, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plik, rekord.get("odcisk_pliku"), blad_przetwarzania,
             numer, rekord.get("nazwa_sprzedawcy"), nip_sprzedawcy,
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


def oznacz_duplikaty(con: sqlite3.Connection) -> None:
    """Dopisuje/uczy błędu powtórzonego numeru faktury — wśród aktywnych.

    Pierwsze (alfabetycznie po pliku) wystąpienie numeru zostaje czyste,
    kolejne dostają błąd "duplikat numeru faktury ...". Błędy duplikatów
    są przeliczane od zera (zmiana numeru przy re-ekstrakcji usuwa
    przedawniony błąd u pozostałych). Status 'zatwierdzone' chroniony.
    """
    with con:
        widziane = {}
        for fid, plik, numer, bledy_txt in con.execute(
                "SELECT id, plik, numer, bledy FROM faktury "
                "WHERE status != 'wyeksportowane' AND numer IS NOT NULL "
                "AND blad_przetworzenia IS NULL ORDER BY plik"):
            bledy = [b for b in json.loads(bledy_txt or "[]")
                     if not b.startswith("duplikat numeru faktury")]
            if numer in widziane:
                bledy.append(f"duplikat numeru faktury {numer} "
                             f"(wcześniejsze wystąpienie: {widziane[numer]})")
            if bledy != json.loads(bledy_txt or "[]"):
                status = "do_weryfikacji" if bledy else "do_zatwierdzenia"
                wiersz = con.execute(
                    "SELECT status FROM faktury WHERE id = ?", (fid,)
                ).fetchone()
                if wiersz and wiersz[0] == "zatwierdzone":
                    status = "zatwierdzone"  # akceptacja — nie cofamy
                con.execute(
                    "UPDATE faktury SET bledy = ?, status = ? WHERE id = ?",
                    (json.dumps(bledy, ensure_ascii=False), status, fid),
                )
                con.execute(
                    "UPDATE pozycje SET status = ? WHERE faktura_id = ?",
                    (status, fid),
                )
            widziane.setdefault(numer, plik)


KOLUMNY_FAKTURY = (
    "id", "plik", "odcisk_pliku", "blad_przetworzenia", "numer",
    "nazwa_sprzedawcy", "nip_sprzedawcy", "nazwa_nabywcy", "nip_nabywcy",
    "data_wystawienia", "termin_platnosci", "netto", "vat", "brutto",
    "bledy", "status",
)


def pobierz_faktury(con: sqlite3.Connection,
                    status: str | None = None) -> list[dict]:
    """Lista faktur do panelu (bez pozycji, z ich liczbą).

    status=None -> wszystkie (w tym wyeksportowane — historia);
    sortowanie po numerze faktury. 'bledy' są zwracane jako lista
    (sparsowane z JSON).
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
