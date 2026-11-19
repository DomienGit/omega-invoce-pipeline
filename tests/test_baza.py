import json
import os
import sqlite3

import pytest

import baza
import weryfikator


def _zapis(katalog, tresc=b"x"):
    def zapisz(nazwa):
        with open(os.path.join(katalog, nazwa), "wb") as f:
            f.write(tresc)
    return zapisz


def _dodaj(con, konf, plik, tresc):
    return baza.dodaj_do_kolejki(con, plik, baza.odcisk_bajtow(tresc),
                                 konf.katalog_danych,
                                 _zapis(konf.katalog_danych, tresc))


def _status(con, fid):
    return con.execute("SELECT status FROM faktury WHERE id = ?",
                       (fid,)).fetchone()[0]


# --- kolejka ---------------------------------------------------------------

def test_identyczna_tresc_w_kolejce_jest_odrzucona(con, konf):
    assert _dodaj(con, konf, "a.pdf", b"AAA") == ("a.pdf", None)
    assert _dodaj(con, konf, "b.pdf", b"AAA") == (None, "już w kolejce")


def test_identyczna_tresc_juz_przetworzona_jest_odrzucona(con, konf, wstaw):
    wstaw("a.pdf")
    tresc = b"%PDF-sztuczny a.pdf"
    assert _dodaj(con, konf, "inna.pdf", tresc) == (None, "już przetworzona")


def test_kolizja_nazwy_daje_wolna_nazwe_z_dopiskiem(con, konf):
    assert _dodaj(con, konf, "faktura.pdf", b"1")[0] == "faktura.pdf"
    assert _dodaj(con, konf, "faktura.pdf", b"2")[0] == "faktura_1.pdf"
    assert _dodaj(con, konf, "faktura.pdf", b"3")[0] == "faktura_2.pdf"
    assert sorted(os.listdir(konf.katalog_danych)) == [
        "faktura.pdf", "faktura_1.pdf", "faktura_2.pdf", "faktury.db"]


def test_kolizja_z_faktura_wyeksportowana_nie_nadpisuje(con, konf, wstaw):
    fid = wstaw("faktura.pdf")
    con.execute("UPDATE faktury SET status = 'wyeksportowane'")
    con.commit()
    os.remove(os.path.join(konf.katalog_danych, "faktura.pdf"))  # sam wiersz
    assert _dodaj(con, konf, "faktura.pdf", b"nowa")[0] == "faktura_1.pdf"
    assert con.execute("SELECT COUNT(*) FROM faktury WHERE id = ?",
                       (fid,)).fetchone()[0] == 1


def test_kolizja_z_plikiem_istniejacym_na_dysku(con, konf):
    with open(os.path.join(konf.katalog_danych, "sierota.pdf"), "wb") as f:
        f.write(b"stary")
    assert _dodaj(con, konf, "sierota.pdf", b"nowy")[0] == "sierota_1.pdf"
    with open(os.path.join(konf.katalog_danych, "sierota.pdf"), "rb") as f:
        assert f.read() == b"stary"


def test_nieudany_zapis_pliku_nie_zostawia_wiersza(con, konf):
    def zapisz(nazwa):
        raise OSError("dysk pełny")
    with pytest.raises(OSError):
        baza.dodaj_do_kolejki(con, "a.pdf", "h", konf.katalog_danych, zapisz)
    assert con.execute("SELECT COUNT(*) FROM faktury").fetchone()[0] == 0


def test_nieudane_wstawienie_wiersza_usuwa_zapisany_plik(con, konf):
    con.execute("CREATE TRIGGER blokada BEFORE INSERT ON faktury "
                "BEGIN SELECT RAISE(ABORT, 'odmowa'); END")
    con.commit()
    with pytest.raises(sqlite3.DatabaseError):
        _dodaj(con, konf, "a.pdf", b"x")
    assert not os.path.exists(os.path.join(konf.katalog_danych, "a.pdf"))
    assert con.execute("SELECT COUNT(*) FROM faktury").fetchone()[0] == 0


# --- duplikaty -------------------------------------------------------------

def _bledy(con, fid):
    return json.loads(con.execute("SELECT bledy FROM faktury WHERE id = ?",
                                  (fid,)).fetchone()[0])


def test_ten_sam_numer_i_nip_daje_blad_na_drugiej_fakturze(con, wstaw):
    a = wstaw("a.pdf", numer="1/11/2026")
    b = wstaw("b.pdf", numer="1/11/2026")
    baza.oznacz_duplikaty(con)
    assert _bledy(con, a) == []
    assert _bledy(con, b)[0].startswith("duplikat numeru faktury")
    assert _status(con, b) == "do_weryfikacji"


def test_ten_sam_numer_u_innego_sprzedawcy_to_nie_duplikat(con, wstaw):
    a = wstaw("a.pdf", numer="1/11/2026", nip_sprzedawcy="5260250274")
    b = wstaw("b.pdf", numer="1/11/2026", nip_sprzedawcy="1234563218")
    baza.oznacz_duplikaty(con)
    assert _bledy(con, a) == [] and _bledy(con, b) == []
    assert _status(con, b) == "do_zatwierdzenia"


def test_dwie_faktury_bez_nipu_z_tym_samym_numerem_to_duplikat(con, wstaw):
    wstaw("a.pdf", numer="7", nip_sprzedawcy=None)
    b = wstaw("b.pdf", numer="7", nip_sprzedawcy=None)
    baza.oznacz_duplikaty(con)
    assert _bledy(con, b)[0].startswith("duplikat numeru faktury")


def test_duplikat_wzgledem_historii_wyeksportowanych(con, wstaw):
    stara = wstaw("stara.pdf", numer="9/2026")
    con.execute("UPDATE faktury SET status = 'wyeksportowane' WHERE id = ?",
                (stara,))
    con.commit()
    nowa = wstaw("nowa.pdf", numer="9/2026")
    assert _bledy(con, nowa)[0].startswith(
        "duplikat faktury wcześniej wyeksportowanej")
    assert _status(con, nowa) == "do_weryfikacji"


def test_inny_nip_wzgledem_historii_nie_jest_duplikatem(con, wstaw):
    stara = wstaw("stara.pdf", numer="9/2026")
    con.execute("UPDATE faktury SET status = 'wyeksportowane' WHERE id = ?",
                (stara,))
    con.commit()
    nowa = wstaw("nowa.pdf", numer="9/2026", nip_sprzedawcy="1234563218")
    assert _bledy(con, nowa) == []


# --- zatwierdzanie ---------------------------------------------------------

def test_zatwierdzenie_zapisuje_login_czas_i_status_pozycji(con, wstaw):
    fid = wstaw("a.pdf")
    assert baza.zatwierdz_fakture(con, fid, "ania") is True
    status, kto, kiedy = con.execute(
        "SELECT status, zatwierdzil, zatwierdzone_at FROM faktury "
        "WHERE id = ?", (fid,)).fetchone()
    assert (status, kto) == ("zatwierdzone", "ania") and kiedy
    assert [s for (s,) in con.execute(
        "SELECT status FROM pozycje WHERE faktura_id = ?", (fid,))
    ] == ["zatwierdzone"]


@pytest.mark.parametrize("poczatkowy", [
    "do_przetworzenia", "do_weryfikacji", "zatwierdzone", "wyeksportowane"])
def test_zatwierdzenie_odmowa_w_innym_statusie(con, wstaw, poczatkowy):
    fid = wstaw("a.pdf")
    con.execute("UPDATE faktury SET status = ? WHERE id = ?",
                (poczatkowy, fid))
    con.commit()
    assert baza.zatwierdz_fakture(con, fid, "ania") is False
    assert _status(con, fid) == poczatkowy
    # pozycje zostają w swoim statusie (z ekstrakcji: do_zatwierdzenia):
    assert [s for (s,) in con.execute(
        "SELECT status FROM pozycje WHERE faktura_id = ?", (fid,))
    ] == ["do_zatwierdzenia"]


def test_zatwierdzenie_odmowa_dla_faktury_z_bledami(con, wstaw):
    fid = wstaw("a.pdf", bledy=["coś nie tak"])
    assert _status(con, fid) == "do_weryfikacji"
    assert baza.zatwierdz_fakture(con, fid, "ania") is False


def test_zatwierdzenie_nieistniejacej_faktury(con):
    assert baza.zatwierdz_fakture(con, 999, "ania") is False


def test_oznacz_wyeksportowane_zmienia_tylko_wskazane(con, wstaw):
    a, b, c = (wstaw(f"{n}.pdf", numer=n) for n in "abc")
    for fid in (a, b, c):
        baza.zatwierdz_fakture(con, fid, "ania")
    assert baza.oznacz_wyeksportowane(con, [a, b]) == 2
    assert [_status(con, f) for f in (a, b, c)] == [
        "wyeksportowane", "wyeksportowane", "zatwierdzone"]
    assert baza.oznacz_wyeksportowane(con, []) == 0


def test_oznacz_wyeksportowane_pomija_niezatwierdzone_z_listy(con, wstaw):
    a = wstaw("a.pdf")   # do_zatwierdzenia
    assert baza.oznacz_wyeksportowane(con, [a]) == 0
    assert _status(con, a) == "do_zatwierdzenia"


# --- przetwarzanie kolejki -------------------------------------------------

def test_przetworzenie_kolejki_poprawna_i_bledna(con, konf, faktura,
                                                 sztuczny_odczyt):
    _dodaj(con, konf, "ok.pdf", b"1")
    _dodaj(con, konf, "zle.pdf", b"2")
    sztuczny_odczyt["ok.pdf"] = faktura(numer="A/1")
    sztuczny_odczyt["zle.pdf"] = faktura(numer="B/1", brutto=999.0,
                                         nip_sprzedawcy="1234563218")
    assert weryfikator.przetworz_kolejke(None, con,
                                         konf.katalog_danych) == 2
    statusy = dict(con.execute("SELECT plik, status FROM faktury"))
    assert statusy == {"ok.pdf": "do_zatwierdzenia",
                       "zle.pdf": "do_weryfikacji"}


def test_wyjatek_odczytu_zostawia_fakture_w_kolejce_z_opisem(
        con, konf, sztuczny_odczyt):
    _dodaj(con, konf, "zepsuty.pdf", b"1")
    sztuczny_odczyt["zepsuty.pdf"] = RuntimeError("API 503")
    weryfikator.przetworz_kolejke(None, con, konf.katalog_danych)
    status, blad = con.execute(
        "SELECT status, blad_przetworzenia FROM faktury").fetchone()
    assert status == "do_przetworzenia" and "API 503" in blad
