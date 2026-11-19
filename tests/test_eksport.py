import os
import xml.etree.ElementTree as ET

import baza
import eksport


def _zatwierdz(con, *ids, login="ania"):
    for fid in ids:
        assert baza.zatwierdz_fakture(con, fid, login)


def _xml(sciezka):
    return ET.parse(sciezka).getroot()


def test_xml_zawiera_tylko_faktury_z_listy_ids(con, konf, wstaw):
    a, b = wstaw("a.pdf", numer="A/1"), wstaw("b.pdf", numer="B/1")
    _zatwierdz(con, a, b)
    n, sciezka = eksport.eksportuj_xml(con, konf.katalog_eksportu, ids=[a])
    assert n == 1
    numery = [e.text for e in _xml(sciezka).iter("NumerFaktury")]
    assert numery == ["A/1"]


def test_xml_ma_poprawne_wartosci_i_atrybuty_zatwierdzenia(con, konf, wstaw):
    a = wstaw("a.pdf", numer="A/1", netto=1234.5, vat=283.94, brutto=1518.44,
              pozycje=[{"nazwa": "Towar", "ilosc": 4, "cena_netto": 308.625,
                        "wartosc_netto": 1234.5}])
    _zatwierdz(con, a, login="jan")
    zatw_at = con.execute("SELECT zatwierdzone_at FROM faktury").fetchone()[0]
    _, sciezka = eksport.eksportuj_xml(con, konf.katalog_eksportu)
    f = _xml(sciezka).find("Faktura")
    assert f.findtext("Naglowek/NumerFaktury") == "A/1"
    assert f.findtext("Sprzedawca/NIP") == "5260250274"
    assert f.findtext("Nabywca/NIP") == "1234563218"
    assert f.findtext("Podsumowanie/Netto") == "1234.50"
    assert f.findtext("Podsumowanie/VAT") == "283.94"
    assert f.findtext("Podsumowanie/Brutto") == "1518.44"
    assert f.findtext("Pozycje/Pozycja/Ilosc") == "4"
    dekretacja = f.find("Dekretacja")
    assert dekretacja.get("zatwierdzil") == "jan"
    assert dekretacja.get("dataZatwierdzenia") == zatw_at


def test_kolejny_eksport_tworzy_nowy_plik_zamiast_nadpisywac(con, konf, wstaw):
    _zatwierdz(con, wstaw("a.pdf"))
    _, p1 = eksport.eksportuj_xml(con, konf.katalog_eksportu)
    _, p2 = eksport.eksportuj_xml(con, konf.katalog_eksportu)
    assert os.path.basename(p1) == "eksport.xml"
    assert os.path.basename(p2) == "eksport_1.xml"
    assert os.path.exists(p1) and os.path.exists(p2)


def test_kopie_pdf_o_tej_samej_nazwie_dostaja_dopisek(con, konf, wstaw):
    # ta sama data, numer i sprzedawca -> ta sama docelowa nazwa kopii:
    a = wstaw("a.pdf", numer="X/1", nazwa_sprzedawcy="Firma")
    b = wstaw("b.pdf", numer="X/1", nazwa_sprzedawcy="Firma")
    _zatwierdz(con, a, b)
    assert eksport.zapisz_pdf_do_przetworzonych(
        con, konf.katalog_danych, konf.katalog_eksportu) == 2
    assert sorted(os.listdir(konf.katalog_eksportu)) == [
        "2026-11-01_X-1_Firma.pdf", "2026-11-01_X-1_Firma_1.pdf"]


def test_eksport_pustej_listy_niczego_nie_zapisuje(con, konf):
    assert eksport.eksportuj_zatwierdzone(
        con, konf.katalog_danych, konf.katalog_eksportu) == (0, "", 0)
    assert os.listdir(konf.katalog_eksportu) == []


def test_eksportuj_zatwierdzone_robi_xml_pdf_i_zmienia_status(con, konf, wstaw):
    a = wstaw("a.pdf")
    _zatwierdz(con, a)
    n, sciezka, pdfy = eksport.eksportuj_zatwierdzone(
        con, konf.katalog_danych, konf.katalog_eksportu)
    assert (n, pdfy) == (1, 1) and os.path.exists(sciezka)
    assert con.execute("SELECT status FROM faktury").fetchone() == (
        "wyeksportowane",)


def test_faktura_zatwierdzona_po_ustaleniu_listy_zostaje_zatwierdzona(
        con, konf, wstaw):
    a, b = wstaw("a.pdf", numer="A/1"), wstaw("b.pdf", numer="B/1")
    _zatwierdz(con, a)
    ids = eksport.pobierz_ids_do_eksportu(con)      # lista ustalona raz
    _zatwierdz(con, b)                              # "w trakcie eksportu"
    _, sciezka = eksport.eksportuj_xml(con, konf.katalog_eksportu, ids=ids)
    eksport.zapisz_pdf_do_przetworzonych(
        con, konf.katalog_danych, konf.katalog_eksportu, ids=ids)
    baza.oznacz_wyeksportowane(con, ids)
    statusy = dict(con.execute("SELECT numer, status FROM faktury"))
    assert statusy == {"A/1": "wyeksportowane", "B/1": "zatwierdzone"}
    assert [e.text for e in _xml(sciezka).iter("NumerFaktury")] == ["A/1"]
