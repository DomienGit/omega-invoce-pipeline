"""Warstwa SQLite — spis faktur i ich pozycji (data/faktury.db).

Baza jest JEDYNYM magazynem wyników i KOLEJKĄ przetwarzania: PDF wgrany
przez panel dostaje wiersz o statusie 'do_przetworzenia', a agent
(weryfikator / panel) bierze takie wiersze po kolei i po ekstrakcji
AKTUALIZUJE je w miejscu (zapisz_wynik_ekstrakcji).

  faktury  — nagłówki: numer, strony (nazwy + NIP-y), daty, kwoty
             netto/VAT/brutto, błędy walidacji (JSON), status workflow,
             nazwa pliku PDF (plik) + odcisk treści (SHA-256 — wykrywa
             ponowne wgranie tej samej faktury), zatwierdzil +
             zatwierdzone_at (kto i kiedy zatwierdził) oraz blad_przetworzenia
             (niepusty = ostatnia próba nieudana: zepsuty PDF / API;
             wiersz zostaje w 'do_przetworzenia' i czeka na ponowienie).
  pozycje  — wiersze tabeli faktury: nazwa, ilosc, cena_netto,
             wartosc_netto (to, co wyciąga model w weryfikatorze)
             + status. Klucz obcy do faktury (ON DELETE CASCADE).

Statusy: do_przetworzenia (czeka na ekstrakcję) / do_weryfikacji /
do_zatwierdzenia / zatwierdzone / wyeksportowane. Wyeksportowane to
HISTORIA: znikają z panelu i nie są nadpisywane. Zatwierdzić można
tylko fakturę 'do_zatwierdzenia' (warunek w samym UPDATE). Wgranie PDF-a
nigdy niczego nie usuwa ani nie nadpisuje: gdy nazwa pliku jest zajęta
(wiersz w bazie w dowolnym statusie albo plik w data/), nowa faktura
dostaje wolną nazwę z dopiskiem (faktura_1.pdf). Powtórka numeru + NIP
sprzedawcy względem historii dostaje błąd "duplikat faktury wcześniej
wyeksportowanej"; powtórka tej samej pary wśród aktywnych — błąd
"duplikat numeru faktury" (oznacz_duplikaty) — oba do decyzji księgowej,
bo duplikat musi być WIDOCZNY, a nie cicho zastąpiony.

Migracje w polacz(): stare schematy (tabelowy UNIQUE, CHECK bez
'do_przetworzenia', brak kolumn odcisk_pliku/blad_przetworzenia) są
przebudowywane, a brakujące kolumny zatwierdzil/zatwierdzone_at
dodawane przez ALTER TABLE; nazwa pliku jest unikalna wśród aktywnych
(indeks częściowy), numery mogą się powtarzać (patrz wyżej).

Użycie z weryfikatora / panelu:
  con = baza.polacz(konf.sciezka_bazy)      # ścieżka zawsze jawna
  nazwa, powod = baza.dodaj_do_kolejki(con, plik, odcisk, konf.katalog_danych, zapisz)
  for f in baza.pobierz_kolejke(con): ...    # agent: ekstrakcja
  baza.zapisz_wynik_ekstrakcji(con, id, dane, bledy)
  faktury = baza.pobierz_faktury(con)        # lista do panelu
  baza.zamknij(con)
"""

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime

STATUSY = ("'do_przetworzenia', 'do_weryfikacji', 'do_zatwierdzenia', "
           "'zatwierdzone', 'wyeksportowane'")

DDL_FAKTURY = f"""
CREATE TABLE IF NOT EXISTS faktury (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    plik              TEXT,
    odcisk_pliku      TEXT,
    blad_przetworzenia TEXT,
    wgrane_at         TEXT,     -- kiedy PDF wpadł do kolejki (monitoring)
    przetworzone_at   TEXT,     -- kiedy agent skończył ekstrakcję
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
    status            TEXT NOT NULL CHECK (status IN ({STATUSY})),
    zatwierdzil       TEXT,     -- login osoby, która zatwierdziła fakturę
    zatwierdzone_at   TEXT      -- kiedy (ISO, do sekundy)
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

# Nazwa pliku unikalna wśród faktur aktywnych — wyeksportowane
# (historia) mogą powtarzać nazwy. Numerów NIE indeksujemy: duplikat
# ma być widoczny jako błąd (oznacz_duplikaty), nie cicho zastąpiony:
DDL_INDEKSY = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_aktywny_plik
    ON faktury(plik) WHERE status != 'wyeksportowane';
"""

# kolumny kopiowane przy przebudowie tabeli (stare tabele mogą nie mieć
# wszystkich — brakujące dostają NULL):
KOLUMNY_MIGRACJI = (
    "id", "plik", "odcisk_pliku", "blad_przetworzenia", "wgrane_at",
    "przetworzone_at", "numer", "nazwa_sprzedawcy", "nip_sprzedawcy",
    "nazwa_nabywcy", "nip_nabywcy", "data_wystawienia", "termin_platnosci",
    "netto", "vat", "brutto", "bledy", "status",
    "zatwierdzil", "zatwierdzone_at",
)

# kolumny dodane później — stara baza dostaje je przez ALTER TABLE
# (bez przebudowy tabeli i bez utraty danych):
KOLUMNY_DODAWANE = ("zatwierdzil", "zatwierdzone_at")


def normalizuj_nip(nip: str | None) -> str | None:
    """Zwraca NIP jako same cyfry; None/pusty/string bez cyfr -> None."""
    if not nip:
        return None
    cyfry = re.sub(r"\D", "", nip)
    return cyfry or None


def odcisk_bajtow(dane: bytes) -> str:
    """SHA-256 zawartości — identyfikator "tej samej" treści PDF."""
    return hashlib.sha256(dane).hexdigest()


def _migruj_schemat(con: sqlite3.Connection) -> None:
    """Przebudowuje starą tabelę faktury do aktualnego schematu.

    SQLite nie potrafi zmieniać constraintów w miejscu, więc stara
    tabela jest przemianowywana, powstaje nowa (aktualny DDL), dane
    są kopiowane (brakujące kolumny dostają NULL), stare tabele
    kasowane. Nowe bazy — bez zmian.

    Przebudowa, gdy: CHECK nie zna 'do_przetworzenia', jest tabelowy
    UNIQUE (numer, nip_sprzedawcy) — dziś numery nie są unikalne,
    duplikaty oznacza błędem oznacz_duplikaty — albo brakuje kolumn
    odcisk_pliku / blad_przetworzenia / wgrane_at / przetworzone_at.
    """
    sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='faktury'"
    ).fetchone()
    if sql is None:  # świeża baza — tabele dopiero co powstały z DDL
        return
    kolumny = {w[1] for w in con.execute("PRAGMA table_info(faktury)")}
    przestarzala = (
        "do_przetworzenia" not in sql[0]
        or "UNIQUE" in sql[0]
        or not {"odcisk_pliku", "blad_przetworzenia",
                "wgrane_at", "przetworzone_at"} <= kolumny
    )
    if not przestarzala:
        return

    stare = set(kolumny)
    selecty = ", ".join(c if c in stare else f"NULL AS {c}"
                        for c in KOLUMNY_MIGRACJI)

    # DROP TABLE kasuje wpis w sqlite_sequence, a sam odtworzony z MAX(id)
    # ZEZWALA na ponowne użycie id raz użytego przez ręcznie skasowany
    # wiersz — jego osierocone pozycje (DELETE bez PRAGMA foreign_keys)
    # przylgnęłyby do świeżej faktury. Zachowujemy więc najwyższe
    # kiedykolwiek nadane id:
    stare_sekwencje = {}
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
        stare_sekwencje = dict(con.execute(
            "SELECT name, seq FROM sqlite_sequence "
            "WHERE name IN ('faktury', 'pozycje')").fetchall())

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
            for tabela in ("faktury", "pozycje"):
                najstarsze_id = stare_sekwencje.get(tabela, 0)
                con.execute(
                    "INSERT INTO sqlite_sequence (name, seq) "
                    "SELECT ?, ? WHERE NOT EXISTS "
                    "(SELECT 1 FROM sqlite_sequence WHERE name = ?)",
                    (tabela, najstarsze_id, tabela))
                con.execute(
                    "UPDATE sqlite_sequence SET seq = ? "
                    "WHERE name = ? AND seq < ?",
                    (najstarsze_id, tabela, najstarsze_id))
    finally:
        con.execute("PRAGMA foreign_keys = ON")


def polacz(sciezka: str) -> sqlite3.Connection:
    """Otwiera bazę pod jawną ścieżką, tworzy tabele i migruje stare schematy.

    Ścieżkę podaje wywołujący (Konfiguracja.sciezka_bazy) — moduł nie
    zakłada katalogu roboczego.
    """
    con = sqlite3.connect(sciezka)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(DDL)
    _migruj_schemat(con)
    istniejace = {w[1] for w in con.execute("PRAGMA table_info(faktury)")}
    for kolumna in KOLUMNY_DODAWANE:
        if kolumna not in istniejace:
            con.execute(f"ALTER TABLE faktury ADD COLUMN {kolumna} TEXT")
    con.commit()
    # pozostałości starych schematów — zastąpione częściowym uq_aktywny_plik:
    con.execute("DROP INDEX IF EXISTS uq_faktury_plik")
    con.execute("DROP INDEX IF EXISTS uq_aktywny_numer")
    con.executescript(DDL_INDEKSY)
    return con


def zamknij(con: sqlite3.Connection) -> None:
    con.close()


def zatwierdz_fakture(con: sqlite3.Connection, faktura_id: int,
                      login: str) -> bool:
    """Ustawia status 'zatwierdzone' fakturze i jej pozycjom.

    Zatwierdzić można TYLKO fakturę 'do_zatwierdzenia' bez błędów —
    warunek siedzi w samym UPDATE, więc jest odporny na wyścig między
    sprawdzeniem w endpoincie a zapisem (np. faktura wyeksportowana
    w międzyczasie nie wróci do eksportu). Zapisuje, kto (login)
    i kiedy zatwierdził. Zwraca False, gdy nic się nie zmieniło
    (brak faktury albo inny status) — pozycje wtedy też zostają.
    """
    with con:
        cur = con.execute(
            "UPDATE faktury SET status = 'zatwierdzone', zatwierdzil = ?, "
            "zatwierdzone_at = ? "
            "WHERE id = ? AND status = 'do_zatwierdzenia' AND bledy = '[]'",
            (login, datetime.now().isoformat(timespec="seconds"),
             faktura_id),
        )
        if cur.rowcount:
            con.execute(
                "UPDATE pozycje SET status = 'zatwierdzone' "
                "WHERE faktura_id = ?", (faktura_id,))
    return cur.rowcount > 0


def oznacz_wyeksportowane(con: sqlite3.Connection,
                          ids: list[int] | None = None) -> int:
    """Przenosi faktury 'zatwierdzone' do statusu 'wyeksportowane'.

    ids — identyfikatory faktur faktycznie zapisanych w eksporcie:
    zmieniamy tylko te z nich, które nadal są 'zatwierdzone' (faktura
    zatwierdzona już PO ustaleniu listy nie trafiła do pliku, więc
    zostaje do następnego eksportu). ids=None -> wszystkie zatwierdzone.
    Wyeksportowane znikają z panelu, ale zostają w bazie jako historia.
    Zwraca liczbę zmienionych faktur.
    """
    warunek, parametry = "", ()
    if ids is not None:
        if not ids:
            return 0
        warunek = f" AND id IN ({', '.join('?' * len(ids))})"
        parametry = tuple(ids)
    with con:
        cur = con.execute(
            "UPDATE faktury SET status = 'wyeksportowane' "
            "WHERE status = 'zatwierdzone'" + warunek, parametry)
        con.execute(
            "UPDATE pozycje SET status = 'wyeksportowane' "
            "WHERE status != 'wyeksportowane' AND faktura_id IN "
            "(SELECT id FROM faktury WHERE status = 'wyeksportowane')")
    return cur.rowcount


def _wolna_nazwa(con: sqlite3.Connection, plik: str, folder: str) -> str:
    """Pierwsza nazwa pliku niezajęta ani w bazie, ani na dysku.

    Zajęta = jakikolwiek wiersz faktury (także wyeksportowany — jego PDF
    to historia) albo plik w folderze. Dopisek: faktura_1.pdf, _2, ...
    """
    korzen, rozszerzenie = os.path.splitext(plik)
    kandydat, numer = plik, 0
    while (con.execute("SELECT 1 FROM faktury WHERE plik = ?",
                       (kandydat,)).fetchone()
           or os.path.exists(os.path.join(folder, kandydat))):
        numer += 1
        kandydat = f"{korzen}_{numer}{rozszerzenie}"
    return kandydat


def dodaj_do_kolejki(con: sqlite3.Connection, plik: str, odcisk: str,
                     folder: str,
                     zapisz_plik=None) -> tuple[str | None, str | None]:
    """Rejestruje wgrany PDF: wiersz o statusie 'do_przetworzenia'.

    Zwraca (nazwa, powód): przyjęto -> (nazwa pod jaką zarejestrowano,
    None); pominięto -> (None, 'już przetworzona' / 'już w kolejce').
    Identyczna treść (odcisk SHA-256) ma pierwszeństwo przed nazwą.
    NIC nie jest usuwane ani nadpisywane: zajętą nazwę zastępuje wolna
    z dopiskiem (patrz _wolna_nazwa), więc faktura i jej PDF z historii
    są bezpieczne.

    folder — katalog danych z PDF-ami (jawny, np. Konfiguracja.katalog_danych).
    zapisz_plik(nazwa) — wywołanie zapisujące PDF na dysku; idzie PRZED
    wstawieniem wiersza, żeby wątek przetwarzania nie znalazł w kolejce
    wiersza bez pliku. Gdy wstawienie się nie uda, świeżo zapisany plik
    jest usuwany. Całość pod BEGIN IMMEDIATE — wybór nazwy i wstawienie
    nie rozjadą się z równoległym wgraniem.
    """
    con.execute("BEGIN IMMEDIATE")
    zapisany = None
    try:
        z_tym_odciskiem = con.execute(
            "SELECT status FROM faktury WHERE odcisk_pliku = ?", (odcisk,)
        ).fetchone()
        if z_tym_odciskiem:
            con.rollback()
            return None, ("już w kolejce"
                          if z_tym_odciskiem[0] == "do_przetworzenia"
                          else "już przetworzona")

        nazwa = _wolna_nazwa(con, plik, folder)
        if zapisz_plik is not None:
            zapisany = os.path.join(folder, nazwa)
            zapisz_plik(nazwa)
        con.execute(
            "INSERT INTO faktury (plik, odcisk_pliku, wgrane_at, status) "
            "VALUES (?, ?, ?, 'do_przetworzenia')",
            (nazwa, odcisk, datetime.now().isoformat(timespec="seconds")))
        con.commit()
        return nazwa, None
    except BaseException:
        con.rollback()
        if zapisany and os.path.exists(zapisany):
            os.remove(zapisany)   # nie zostawiamy pliku bez wiersza
        raise


def pobierz_kolejke(con: sqlite3.Connection) -> list[dict]:
    """Faktury czekające na przetworzenie (po kolei wg kolejności wgrania)."""
    return [
        {"id": fid, "plik": plik, "blad_przetworzenia": blad}
        for (fid, plik, blad) in con.execute(
            "SELECT id, plik, blad_przetworzenia FROM faktury "
            "WHERE status = 'do_przetworzenia' ORDER BY id")
    ]


def zapisz_wynik_ekstrakcji(con: sqlite3.Connection, faktura_id: int,
                            dane: dict, bledy: list[str]) -> None:
    """Aktualizuje wiersz 'do przetworzenia' danymi z ekstrakcji.

    dane = odpowiedź modelu (numer, strony, daty, kwoty, pozycje),
    bledy = lista błędów walidacji. Status: do_zatwierdzenia (bez
    błędów) albo do_weryfikacji. Pozycje są przepisywane od nowa.
    """
    numer = (dane.get("numer") or "").strip() or None
    nip_sprzedawcy = normalizuj_nip(dane.get("nip_sprzedawcy"))
    nip_nabywcy = normalizuj_nip(dane.get("nip_nabywcy"))
    bledy = list(bledy)

    with con:
        # powtórka numeru + NIP względem wyeksportowanej historii —
        # ostrzeż księgową:
        if numer is not None:
            duplikat = con.execute(
                "SELECT plik FROM faktury "
                "WHERE numer = ? AND nip_sprzedawcy IS ? "
                "AND status = 'wyeksportowane' AND id != ?",
                (numer, nip_sprzedawcy, faktura_id),
            ).fetchone()
            if duplikat:
                bledy.append("duplikat faktury wcześniej wyeksportowanej "
                             f"(poprzednio: {duplikat[0]})")

        status = "do_zatwierdzenia" if not bledy else "do_weryfikacji"
        con.execute(
            "UPDATE faktury SET numer = ?, nazwa_sprzedawcy = ?, "
            "nip_sprzedawcy = ?, nazwa_nabywcy = ?, nip_nabywcy = ?, "
            "data_wystawienia = ?, termin_platnosci = ?, netto = ?, "
            "vat = ?, brutto = ?, bledy = ?, blad_przetworzenia = NULL, "
            "przetworzone_at = ?, status = ? WHERE id = ?",
            (numer, dane.get("nazwa_sprzedawcy"), nip_sprzedawcy,
             dane.get("nazwa_nabywcy"), nip_nabywcy,
             dane.get("data_wystawienia"), dane.get("termin_platnosci"),
             dane.get("netto"), dane.get("vat"), dane.get("brutto"),
             json.dumps(bledy, ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds"), status, faktura_id),
        )
        con.execute("DELETE FROM pozycje WHERE faktura_id = ?", (faktura_id,))
        con.executemany(
            "INSERT INTO pozycje (faktura_id, nazwa, ilosc, cena_netto, "
            "wartosc_netto, status) VALUES (?, ?, ?, ?, ?, ?)",
            [(faktura_id, p.get("nazwa"), p.get("ilosc"), p.get("cena_netto"),
              p.get("wartosc_netto"), status)
             for p in (dane.get("pozycje") or [])],
        )


def oznacz_blad_przetworzenia(con: sqlite3.Connection, faktura_id: int,
                              opis: str) -> None:
    """Nieudana ekstrakcja: wiersz zostaje w 'do_przetworzenia' z opisem.

    Kolejny przebieg (restart panelu, przycisk 'Przetwórz ponownie')
    ponowi próbę — chwilowe błędy API (429/500/503) agent
    i tak najpierw próbuje rozładować sam (patrz weryfikator).
    """
    with con:
        con.execute(
            "UPDATE faktury SET blad_przetworzenia = ?, bledy = ? "
            "WHERE id = ?",
            (opis, json.dumps([opis], ensure_ascii=False), faktura_id),
        )


def oznacz_duplikaty(con: sqlite3.Connection) -> None:
    """Dopisuje/uczy błędu powtórzonej faktury — wśród aktywnych.

    Duplikat = ta sama para (numer, NIP sprzedawcy): numery w rodzaju
    1/11/2026 są częste, więc ten sam numer u innego sprzedawcy to nie
    duplikat. Faktury bez NIP-u porównujemy po samym numerze (para
    z pustym NIP-em) — ostrożnie, w razie wątpliwości księgowa zdecyduje.
    Pierwsze (alfabetycznie po pliku) wystąpienie zostaje czyste, kolejne
    dostają błąd "duplikat numeru faktury ...". Błędy duplikatów są
    przeliczane od zera (zmiana numeru przy re-ekstrakcji usuwa
    przedawniony błąd u pozostałych). Status 'zatwierdzone' chroniony.
    """
    with con:
        widziane = {}
        for fid, plik, numer, nip, bledy_txt in con.execute(
                "SELECT id, plik, numer, nip_sprzedawcy, bledy FROM faktury "
                "WHERE status != 'wyeksportowane' AND numer IS NOT NULL "
                "AND blad_przetworzenia IS NULL ORDER BY plik"):
            klucz = (numer, nip)
            bledy = [b for b in json.loads(bledy_txt or "[]")
                     if not b.startswith("duplikat numeru faktury")]
            if klucz in widziane:
                bledy.append(f"duplikat numeru faktury {numer} "
                             f"(wcześniejsze wystąpienie: {widziane[klucz]})")
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
            widziane.setdefault(klucz, plik)


KOLUMNY_FAKTURY = (
    "id", "plik", "odcisk_pliku", "blad_przetworzenia", "wgrane_at",
    "przetworzone_at", "numer", "nazwa_sprzedawcy", "nip_sprzedawcy",
    "nazwa_nabywcy", "nip_nabywcy", "data_wystawienia", "termin_platnosci",
    "netto", "vat", "brutto", "bledy", "status",
    "zatwierdzil", "zatwierdzone_at",
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
