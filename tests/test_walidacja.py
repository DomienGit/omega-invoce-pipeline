import pytest

from weryfikator import waliduj


def test_poprawna_faktura_nie_ma_bledow(faktura):
    assert waliduj(faktura()) == []


@pytest.mark.parametrize("nip", [None, "", "brak"])
def test_brak_nipu_sprzedawcy(faktura, nip):
    assert waliduj(faktura(nip_sprzedawcy=nip)) == ["brak NIP sprzedawcy"]


def test_nip_o_zlej_dlugosci(faktura):
    bledy = waliduj(faktura(nip_sprzedawcy="12345"))
    assert len(bledy) == 1 and "10 cyfr" in bledy[0]


def test_nip_ze_zla_suma_kontrolna(faktura):
    bledy = waliduj(faktura(nip_sprzedawcy="5260250275"))
    assert len(bledy) == 1 and "sumy kontrolnej" in bledy[0]


def test_nip_z_myslnikami_i_spacjami_jest_normalizowany(faktura):
    assert waliduj(faktura(nip_sprzedawcy="526-025-02-74")) == []


def test_netto_plus_vat_rozne_od_brutto(faktura):
    bledy = waliduj(faktura(brutto=130.0))
    assert len(bledy) == 1 and "kwoty niezgodne" in bledy[0]


@pytest.mark.xfail(reason="błąd aplikacji: porównanie zmiennoprzecinkowe "
                   "abs(netto + vat - brutto) > 0.01 daje 0.0100000000000051 "
                   "dla 100 + 23 vs 123.01, więc równo 1 grosz jest odrzucany "
                   "mimo deklarowanej tolerancji (weryfikator.waliduj, pkt c)",
                   strict=True)
def test_roznica_jednego_grosza_miesci_sie_w_tolerancji(faktura):
    assert waliduj(faktura(brutto=123.01)) == []


def test_roznica_pol_grosza_miesci_sie_w_tolerancji(faktura):
    assert waliduj(faktura(brutto=123.005)) == []


def test_roznica_dwoch_groszy_przekracza_tolerancje(faktura):
    bledy = waliduj(faktura(brutto=123.02))
    assert len(bledy) == 1 and "kwoty niezgodne" in bledy[0]


def test_brakujace_kwoty(faktura):
    assert waliduj(faktura(vat=None)) == [
        "nie udało się odczytać wszystkich kwot"]


def test_suma_pozycji_rozna_od_netto(faktura):
    pozycje = [{"nazwa": "A", "ilosc": 1, "cena_netto": 90.0,
                "wartosc_netto": 90.0}]
    bledy = waliduj(faktura(pozycje=pozycje))
    assert len(bledy) == 1 and "suma pozycji" in bledy[0]


def test_brak_pozycji(faktura):
    assert waliduj(faktura(pozycje=[])) == [
        "nie udało się odczytać pozycji faktury"]


def test_pozycja_bez_wartosci(faktura):
    pozycje = [{"nazwa": "A", "ilosc": 1, "cena_netto": 100.0,
                "wartosc_netto": None}]
    assert waliduj(faktura(pozycje=pozycje)) == [
        "nie udało się odczytać wartości wszystkich pozycji"]


def test_termin_platnosci_przed_data_wystawienia(faktura):
    bledy = waliduj(faktura(termin_platnosci="2026-10-01"))
    assert len(bledy) == 1 and "przed datą wystawienia" in bledy[0]


def test_termin_w_dniu_wystawienia_jest_poprawny(faktura):
    assert waliduj(faktura(termin_platnosci="2026-11-01")) == []


@pytest.mark.parametrize("zmiany", [
    {"data_wystawienia": "01.11.2026"},
    {"termin_platnosci": "jutro"},
    {"data_wystawienia": None},
    {"termin_platnosci": ""},
])
def test_nieczytelne_daty(faktura, zmiany):
    assert waliduj(faktura(**zmiany)) == [
        "nie udało się odczytać dat w formacie RRRR-MM-DD"]
