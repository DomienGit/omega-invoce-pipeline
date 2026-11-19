"""Konfiguracja aplikacji — jedno miejsce, jawnie przekazywane dalej.

Żaden moduł nie czyta ścieżek ani zmiennych środowiskowych sam z siebie:
punkt wejścia (panel.py, weryfikator.py, eksport.py, straznik.py,
desktop.py) woła load_dotenv(), buduje Konfiguracja przez z_srodowiska()
i podaje ją (albo jej pola) funkcjom. Dzięki temu moduły nie zależą od
katalogu, z którego je uruchomiono, import nie wczytuje .env, a testy
budują Konfiguracja z katalogami tymczasowymi, bez podmieniania katalogu
roboczego i bez ponownego importowania modułów.

Domyślne ścieżki (data/, przetworzone/, logi/ względem katalogu
roboczego) są takie jak przed refaktoryzacją, więc Docker (WORKDIR /app,
volume ./data i ./przetworzone) działa bez zmian. Zmienne OMEGA_KATALOG_*
są opcjonalne i służą tylko do przeniesienia katalogów.
"""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Konfiguracja:
    katalog_danych: str = "data"          # PDF-y + baza faktury.db
    katalog_eksportu: str = "przetworzone"  # eksport XML + kopie PDF
    katalog_logow: str = "logi"           # raporty strażnika
    gemini_api_key: str = ""
    konta: dict[str, str] = field(default_factory=dict)  # login -> hash
    secret_key: str = ""
    panel_token: str = ""
    za_proxy: bool = False
    host: str = "127.0.0.1"
    port: int = 5000
    bez_przegladarki: bool = False

    @property
    def sciezka_bazy(self) -> str:
        return os.path.join(self.katalog_danych, "faktury.db")

    @property
    def auth_wlaczona(self) -> bool:
        """Puste konta = tryb deweloperski (bez logowania)."""
        return bool(self.konta)


def wczytaj_konta(tekst: str | None) -> dict[str, str]:
    """Parsuje PANEL_KONTA: "login=hash;login2=hash2".

    Hash z werkzeug (generate_password_hash), nigdy jawne hasło. Hash
    zawiera ":" i "$", więc separatorem wpisów jest ";", a klucz od
    wartości oddziela pierwszy "=".
    """
    konta: dict[str, str] = {}
    for wpis in (tekst or "").split(";"):
        login, _, haslo_hash = wpis.strip().partition("=")
        if login:
            konta[login] = haslo_hash
    return konta


def z_srodowiska(env=None) -> Konfiguracja:
    """Buduje Konfiguracja ze zmiennych środowiskowych (os.environ).

    Wołać PO load_dotenv() w punkcie wejścia. Nazwy zmiennych jak dotąd:
    GEMINI_API_KEY, PANEL_KONTA, SECRET_KEY, PANEL_TOKEN, PANEL_ZA_PROXY,
    PANEL_HOST, PANEL_PORT, BEZ_PRZEGLADARKI; nowe, opcjonalne:
    OMEGA_KATALOG_DANYCH / _EKSPORTU / _LOGOW.
    """
    env = os.environ if env is None else env
    return Konfiguracja(
        katalog_danych=env.get("OMEGA_KATALOG_DANYCH") or "data",
        katalog_eksportu=env.get("OMEGA_KATALOG_EKSPORTU") or "przetworzone",
        katalog_logow=env.get("OMEGA_KATALOG_LOGOW") or "logi",
        gemini_api_key=env.get("GEMINI_API_KEY") or "",
        konta=wczytaj_konta(env.get("PANEL_KONTA")),
        secret_key=env.get("SECRET_KEY") or "",
        panel_token=env.get("PANEL_TOKEN") or "",
        za_proxy=env.get("PANEL_ZA_PROXY") == "1",
        host=env.get("PANEL_HOST", "127.0.0.1"),
        port=int(env.get("PANEL_PORT", "5000")),
        bez_przegladarki=env.get("BEZ_PRZEGLADARKI") == "1",
    )
