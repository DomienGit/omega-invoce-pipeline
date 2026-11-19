import os
import subprocess
import sys

import pytest

import konfiguracja
import panel

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_domyslne_katalogi_jak_przed_refaktoryzacja():
    k = konfiguracja.z_srodowiska({})
    assert (k.katalog_danych, k.katalog_eksportu, k.katalog_logow) == (
        "data", "przetworzone", "logi")
    assert k.sciezka_bazy == os.path.join("data", "faktury.db")
    assert k.port == 5000 and k.host == "127.0.0.1"
    assert not k.auth_wlaczona


def test_zmienne_omega_nadpisuja_katalogi():
    k = konfiguracja.z_srodowiska({
        "OMEGA_KATALOG_DANYCH": "/x/d", "OMEGA_KATALOG_EKSPORTU": "/x/e",
        "OMEGA_KATALOG_LOGOW": "/x/l"})
    assert (k.katalog_danych, k.katalog_eksportu, k.katalog_logow) == (
        "/x/d", "/x/e", "/x/l")
    assert k.sciezka_bazy == os.path.join("/x/d", "faktury.db")


def test_pozostale_zmienne_srodowiska():
    k = konfiguracja.z_srodowiska({
        "GEMINI_API_KEY": "k", "SECRET_KEY": "s", "PANEL_TOKEN": "t",
        "PANEL_ZA_PROXY": "1", "PANEL_HOST": "0.0.0.0", "PANEL_PORT": "5077",
        "BEZ_PRZEGLADARKI": "1"})
    assert (k.gemini_api_key, k.secret_key, k.panel_token) == ("k", "s", "t")
    assert k.za_proxy and k.bez_przegladarki
    assert (k.host, k.port) == ("0.0.0.0", 5077)


def test_parsowanie_kont_z_hashami_z_dwukropkami_i_dolarami():
    hash1 = "scrypt:32768:8:1$sol1$abc=="
    hash2 = "pbkdf2:sha256:600000$sol2$def"
    konta = konfiguracja.z_srodowiska(
        {"PANEL_KONTA": f"ania={hash1}; jan={hash2}"}).konta
    # pierwszy "=" rozdziela login od hasha, kolejne zostają w hashu:
    assert konta == {"ania": hash1, "jan": hash2}


def test_puste_konta_to_tryb_deweloperski():
    assert konfiguracja.z_srodowiska({"PANEL_KONTA": ""}).konta == {}
    assert konfiguracja.z_srodowiska({"PANEL_KONTA": " ; "}).konta == {}


def test_import_modulow_nie_zmienia_srodowiska():
    # w podprocesie, żeby moduły nie były już zaimportowane:
    kod = (
        "import os; przed = dict(os.environ); "
        "import panel, weryfikator, eksport, baza, straznik, konfiguracja; "
        "assert dict(os.environ) == przed, 'import zmienił os.environ'")
    odp = subprocess.run([sys.executable, "-c", kod], cwd=REPO,
                         capture_output=True, text=True)
    assert odp.returncode == 0, odp.stderr


def test_konta_bez_secret_key_daja_valueerror(konf):
    import dataclasses
    z_kontami = dataclasses.replace(konf, konta={"ania": "hash"})
    with pytest.raises(ValueError, match="brak SECRET_KEY"):
        panel.utworz_app(z_kontami)
