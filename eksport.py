"""Eksport faktur z bazy SQLite do XML (format OmegaProcessor).

Szablon: <Dokumenty> → <Faktura> (Naglowek, Sprzedawca, Nabywca,
Podsumowanie, Pozycje, Dekretacja, PlikPDF). Docelowo plik trafia do
importu w Comarch Optima.

Ustalenia dla pól, których nie ma na fakturach:
  - DataSprzedazy = DataWystawienia (faktury nie mają osobnej daty sprzedaży),
  - StawkaVAT = round(100 * vat / netto) — liczona z kwot nagłówka,
  - Jednostka = pusta (faktury nie podają jednostek przy pozycjach),
  - Kategoria = parametr (domyślnie "Inne" — docelowo klasyfikacja AI),
  - PlikPDF = pusty (baza nie trzyma nazw plików źródłowych).

Uruchomienie samodzielne:
  python eksport.py [status]     # domyślnie "zatwierdzone"
"""

import xml.etree.ElementTree as ET
from datetime import date

import baza

SCIEZKA_EKSPORTU = "data/eksport.xml"
SYSTEM = "OmegaProcessor"
WERSJA = "1.0"


def _tekst(rodzic: ET.Element, znacznik: str, wartosc) -> None:
    """Dodaje podelement; None -> pusty znacznik."""
    el = ET.SubElement(rodzic, znacznik)
    el.text = "" if wartosc is None else str(wartosc)


def _kwota(x) -> str:
    """Kwota w formacie 1234.56; None -> pusty string."""
    return "" if x is None else f"{x:.2f}"


def _ilosc(x) -> str:
    """Ilość: całkowita bez miejsc po przecinku (4), inna z dwoma (89.20)."""
    if x is None:
        return ""
    return f"{x:.2f}".rstrip("0").rstrip(".") if x != int(x) else str(int(x))


def eksportuj_xml(con, sciezka: str = SCIEZKA_EKSPORTU, status: str = "zatwierdzone",
                  kategoria: str = "Inne", zatwierdzil: str | None = None,
                  data_zatwierdzenia: str | None = None) -> int:
    """Zapisuje faktury o danym statusie do XML. Zwraca ich liczbę.

    zatwierdzil / data_zatwierdzenia — atrybuty <Dekretacja>; None = pomijamy.
    """
    faktury = con.execute(
        "SELECT id, numer, nazwa_sprzedawcy, nip_sprzedawcy, nazwa_nabywcy, "
        "nip_nabywcy, data_wystawienia, termin_platnosci, netto, vat, brutto "
        "FROM faktury WHERE status = ? ORDER BY numer", (status,)
    ).fetchall()

    korzen = ET.Element("Dokumenty", {
        "dataGeneracji": date.today().isoformat(),
        "system": SYSTEM,
        "wersja": WERSJA,
    })

    for (fid, numer, nazwa_s, nip_s, nazwa_n, nip_n, wystawiona,
         termin, netto, vat, brutto) in faktury:
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
        if data_zatwierdzenia:
            dekretacja.set("dataZatwierdzenia", data_zatwierdzenia)
        _tekst(dekretacja, "Kategoria", kategoria)
        _tekst(dekretacja, "RejestrVAT", "Zakup")

        _tekst(f, "PlikPDF", "")                 # baza nie trzyma nazw plików

    ET.indent(korzen, space="  ")
    with open(sciezka, "w", encoding="utf-8") as plik:
        plik.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        plik.write(ET.tostring(korzen, encoding="unicode"))

    return len(faktury)


if __name__ == "__main__":
    import sys
    import os

    if not os.path.exists(baza.SCIEZKA_BAZY):
        sys.exit(f"Błąd: brak bazy {baza.SCIEZKA_BAZY} — uruchom najpierw "
                 f"weryfikator.py")
    status = sys.argv[1] if len(sys.argv) > 1 else "zatwierdzone"
    con = baza.polacz()
    n = eksportuj_xml(con, status=status)
    baza.zamknij(con)
    print(f"Wyeksportowano {n} faktur (status: {status}) do {SCIEZKA_EKSPORTU}")
