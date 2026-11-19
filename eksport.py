"""Eksport faktur z bazy SQLite do XML (format OmegaProcessor).

Szablon: <Dokumenty> → <Faktura> (Naglowek, Sprzedawca, Nabywca,
Podsumowanie, Pozycje, Dekretacja, PlikPDF). Docelowo plik trafia do
importu w Comarch Optima.

Eksport jest ATOMOWY względem listy faktur: eksportuj_zatwierdzone()
ustala identyfikatory zatwierdzonych faktur jeden raz na początku,
a zapis XML, kopie PDF i zmiana statusu dotyczą wyłącznie tej listy —
faktura zatwierdzona w trakcie eksportu zostaje 'zatwierdzone' do
następnego razu, zamiast dostać status 'wyeksportowane' bez wpisu
w pliku.

Wyniki lądują w katalogu eksportu (domyślnie przetworzone/, patrz
konfiguracja.py — katalogi są przekazywane jawnie, nie zależą od katalogu
roboczego):
  - eksport.xml — faktury o zadanym statusie; kolejne eksporty NIE
    nadpisują pliku — dostają numer: eksport_1.xml, eksport_2.xml...,
  - kopie oryginalnych PDF-ów zatwierdzonych faktur, nazwane
    [data wystawienia]_[numer faktury]_[kontrahent].pdf, gdzie
    kontrahent = sprzedawca (rejestr zakupu). Znaki niedozwolone
    w nazwach plików (m.in. '/' z numeru faktury) → '-'. Istniejący
    plik nie jest nadpisywany — kolejny dostaje dopisek (_1, _2...).

Ustalenia dla pól, których nie ma na fakturach:
  - DataSprzedazy = DataWystawienia (faktury nie mają osobnej daty sprzedaży),
  - StawkaVAT = round(100 * vat / netto) — liczona z kwot nagłówka,
  - Jednostka = pusta (faktury nie podają jednostek przy pozycjach),
  - Kategoria = parametr (domyślnie "Inne" — docelowo klasyfikacja AI),
  - Dekretacja: atrybuty zatwierdzil / dataZatwierdzenia z danych samej
    faktury (kto i kiedy ją zatwierdził); brak danych = brak atrybutu,
  - PlikPDF = pusty (baza nie trzyma nazw plików źródłowych).

Uruchomienie samodzielne:
  python eksport.py [status]     # domyślnie "zatwierdzone"
(load_dotenv() i konfiguracja z_srodowiska() tylko przy uruchomieniu
z linii poleceń — import modułu niczego nie czyta ze środowiska.)
"""

import os
import re
import shutil
import xml.etree.ElementTree as ET
from datetime import date

import baza

SYSTEM = "OmegaProcessor"
WERSJA = "1.0"


def _bezpieczna_nazwa(tekst: str | None) -> str:
    """Zamienia znaki niedozwolne w nazwach plików na '-'."""
    return re.sub(r'[\\/:*?"<>|]+', "-", (tekst or "").strip())


def _tekst(rodzic: ET.Element, znacznik: str, wartosc) -> None:
    """Dodaje podelement; None -> pusty znacznik."""
    el = ET.SubElement(rodzic, znacznik)
    el.text = "" if wartosc is None else str(wartosc)


def _kwota(x) -> str:
    """Kwota w formacie 1234.56; None -> pusty string."""
    return "" if x is None else f"{x:.2f}"


def _unikalna_sciezka(sciezka: str) -> str:
    """Wolna ścieżka do zapisu: gdy plik istnieje, dopisuje _1, _2, ...

    Kolejne eksporty nie kasują poprzednich: eksport.xml, eksport_1.xml,
    eksport_2.xml...
    """
    if not os.path.exists(sciezka):
        return sciezka
    korzen, rozszerzenie = os.path.splitext(sciezka)
    numer = 1
    while os.path.exists(f"{korzen}_{numer}{rozszerzenie}"):
        numer += 1
    return f"{korzen}_{numer}{rozszerzenie}"


def _ilosc(x) -> str:
    """Ilość: całkowita bez miejsc po przecinku (4), inna z dwoma (89.20)."""
    if x is None:
        return ""
    return f"{x:.2f}".rstrip("0").rstrip(".") if x != int(x) else str(int(x))


def pobierz_ids_do_eksportu(con) -> list[int]:
    """Identyfikatory faktur 'zatwierdzone' w tej chwili (kolejność po numerze)."""
    return [w[0] for w in con.execute(
        "SELECT id FROM faktury WHERE status = 'zatwierdzone' "
        "ORDER BY numer")]


def _warunek_ids(ids: list[int] | None) -> str:
    return "" if ids is None else f" AND id IN ({', '.join('?' * len(ids))})"


def eksportuj_xml(con, katalog_eksportu: str, status: str = "zatwierdzone",
                  kategoria: str = "Inne",
                  ids: list[int] | None = None) -> tuple[int, str]:
    """Zapisuje faktury o danym statusie do XML (nowy plik, bez nadpisywania).

    Plik: <katalog_eksportu>/eksport.xml. Zwraca (liczba faktur, faktyczna
    ścieżka zapisanego pliku — z numerem, gdy eksport.xml już istniał). ids — ogranicza eksport do wskazanych
    faktur (lista ustalona raz na początku całego eksportu); None = wszystkie
    o danym statusie. Atrybuty <Dekretacja> zatwierdzil / dataZatwierdzenia
    pochodzą z danych każdej faktury.
    """
    faktury = con.execute(
        "SELECT id, numer, nazwa_sprzedawcy, nip_sprzedawcy, nazwa_nabywcy, "
        "nip_nabywcy, data_wystawienia, termin_platnosci, netto, vat, brutto, "
        "zatwierdzil, zatwierdzone_at "
        "FROM faktury WHERE status = ?" + _warunek_ids(ids) + " ORDER BY numer",
        (status, *(ids or ())) if ids is not None else (status,)
    ).fetchall()

    korzen = ET.Element("Dokumenty", {
        "dataGeneracji": date.today().isoformat(),
        "system": SYSTEM,
        "wersja": WERSJA,
    })

    for (fid, numer, nazwa_s, nip_s, nazwa_n, nip_n, wystawiona,
         termin, netto, vat, brutto, zatwierdzil, zatwierdzone_at) in faktury:
        f = ET.SubElement(korzen, "Faktura")

        naglowek = ET.SubElement(f, "Naglowek")
        _tekst(naglowek, "NumerFaktury", numer)
        _tekst(naglowek, "DataWystawienia", wystawiona)
        _tekst(naglowek, "DataSprzedazy", wystawiona)  # brak na fakturach
        _tekst(naglowek, "TerminPlatnosci", termin)

        sprzedawca = ET.SubElement(f, "Sprzedawca")
        _tekst(sprzedawca, "NIP", nip_s)
        _tekst(sprzedawca, "Nazwa", nazwa_s)

        nabywca = ET.SubElement(f, "Nabywca")
        _tekst(nabywca, "NIP", nip_n)
        _tekst(nabywca, "Nazwa", nazwa_n)

        podsumowanie = ET.SubElement(f, "Podsumowanie", {"waluta": "PLN"})
        _tekst(podsumowanie, "Netto", _kwota(netto))
        _tekst(podsumowanie, "VAT", _kwota(vat))
        _tekst(podsumowanie, "Brutto", _kwota(brutto))

        stawka = None
        if netto:
            stawka = round(100 * vat / netto)
        pozycje_el = ET.SubElement(f, "Pozycje")
        for (_, nazwa, ilosc, _, wartosc_netto) in con.execute(
                "SELECT id, nazwa, ilosc, cena_netto, wartosc_netto "
                "FROM pozycje WHERE faktura_id = ? ORDER BY id", (fid,)):
            p = ET.SubElement(pozycje_el, "Pozycja")
            _tekst(p, "Opis", nazwa)
            _tekst(p, "Ilosc", _ilosc(ilosc))
            _tekst(p, "Jednostka", "")           # brak na fakturach
            _tekst(p, "WartoscNetto", _kwota(wartosc_netto))
            _tekst(p, "StawkaVAT", stawka)

        dekretacja = ET.SubElement(f, "Dekretacja", {"zrodlo": "AI"})
        if zatwierdzil:
            dekretacja.set("zatwierdzil", zatwierdzil)
        if zatwierdzone_at:
            dekretacja.set("dataZatwierdzenia", zatwierdzone_at)
        _tekst(dekretacja, "Kategoria", kategoria)
        _tekst(dekretacja, "RejestrVAT", "Zakup")

        _tekst(f, "PlikPDF", "")           

    ET.indent(korzen, space="  ")
    sciezka = _unikalna_sciezka(  # każdy eksport = nowy plik
        os.path.join(katalog_eksportu, "eksport.xml"))
    os.makedirs(os.path.dirname(sciezka) or ".", exist_ok=True)
    with open(sciezka, "w", encoding="utf-8") as plik:
        plik.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        plik.write(ET.tostring(korzen, encoding="unicode"))

    return len(faktury), sciezka


def zapisz_pdf_do_przetworzonych(con, katalog_danych: str,
                                 katalog_eksportu: str,
                                 ids: list[int] | None = None) -> int:
    """Kopiuje PDF-y zatwierdzonych faktur z katalogu danych do katalogu eksportu.

    Nazwa pliku: [data wystawienia]_[numer]_[kontrahent].pdf
    (kontrahent = sprzedawca); istniejący plik nie jest nadpisywany —
    kopia dostaje dopisek (_1, _2...). ids — tylko wskazane faktury
    (None = wszystkie zatwierdzone). Zwraca liczbę skopiowanych plików;
    faktury bez pliku PDF w bazie lub bez pliku na dysku są pomijane.
    """
    faktury = con.execute(
        "SELECT plik, numer, nazwa_sprzedawcy, data_wystawienia "
        "FROM faktury WHERE status = 'zatwierdzone' AND plik IS NOT NULL"
        + _warunek_ids(ids), tuple(ids or ()) if ids is not None else ()
    ).fetchall()

    os.makedirs(katalog_eksportu, exist_ok=True)
    skopiowane = 0
    for (plik, numer, kontrahent, data) in faktury:
        zrodlo = os.path.join(katalog_danych, plik)
        if not os.path.exists(zrodlo):
            continue
        nazwa = "_".join(_bezpieczna_nazwa(x) for x in (
            data or "bez-daty", numer or "bez-numeru",
            kontrahent or "bez-kontrahenta")).rstrip(".")
        shutil.copy2(zrodlo, _unikalna_sciezka(
            os.path.join(katalog_eksportu, f"{nazwa}.pdf")))
        skopiowane += 1
    return skopiowane


def eksportuj_zatwierdzone(con, katalog_danych: str,
                           katalog_eksportu: str) -> tuple[int, str, int]:
    """Pełny eksport zatwierdzonych: XML + PDF-y + zmiana statusu.

    Lista faktur ustalana jest raz, na początku; wszystkie trzy kroki
    dotyczą tylko jej. Zwraca (liczba faktur, ścieżka XML, liczba PDF-ów);
    przy pustej liście nic nie zapisuje (liczba 0, ścieżka pusta).
    """
    ids = pobierz_ids_do_eksportu(con)
    if not ids:
        return 0, "", 0
    n, sciezka = eksportuj_xml(con, katalog_eksportu, ids=ids)
    pdfy = zapisz_pdf_do_przetworzonych(con, katalog_danych,
                                        katalog_eksportu, ids=ids)
    baza.oznacz_wyeksportowane(con, ids)  # znikną z panelu
    return n, sciezka, pdfy


def main() -> None:
    import sys

    from dotenv import load_dotenv

    import konfiguracja

    load_dotenv()
    konf = konfiguracja.z_srodowiska()
    if not os.path.exists(konf.sciezka_bazy):
        sys.exit(f"Błąd: brak bazy {konf.sciezka_bazy} — uruchom najpierw "
                 f"weryfikator.py")
    status = sys.argv[1] if len(sys.argv) > 1 else "zatwierdzone"
    con = baza.polacz(konf.sciezka_bazy)
    if status == "zatwierdzone":
        n, sciezka, pdfy = eksportuj_zatwierdzone(
            con, konf.katalog_danych, konf.katalog_eksportu)
    else:
        n, sciezka = eksportuj_xml(con, konf.katalog_eksportu, status=status)
        pdfy = 0
    baza.zamknij(con)
    if status == "zatwierdzone" and n == 0:
        sys.exit("Brak zatwierdzonych faktur do eksportu.")
    print(f"Wyeksportowano {n} faktur (status: {status}) do {sciezka}")
    if pdfy:
        print(f"Skopiowano {pdfy} PDF-ów do {konf.katalog_eksportu}/")


if __name__ == "__main__":
    main()
