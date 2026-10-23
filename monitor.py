"""Monitor — znak życia aplikacji co 15 minut (dead man's switch).

Zasada odwrócona: to aplikacja daje znak życia, a jego brak oznacza
alarm. Monitor w pętli odpytuje panel DOKŁADNIE tak jak przeglądarka
księgowej (GET /api/faktury) i tylko wtedy, gdy panel odpowiedział,
wysyła ping do usługi Healthchecks.io. W Healthchecks ustawiasz:
spodziewam się pingu co 15 min, grace 45 min — brak pingu = mail.

Co ta jedna pętla łapie: zawieszony (żywy, ale milczący) panel,
wyłączoną maszynę, padniętą sieć, kontener restartujący się w pętlę.
Dlatego monitor odpytuje panel OD ZEWNĄTRZ — wątek w samym panelu
pingowałby dalej nawet przy zawieszonym serwowaniu stron.

Ping niesie treść (liczniki z panelu) — Healthchecks archiwizuje ją
w historii pingów checki, więc można przejrzeć, co się działo.

Uruchomienie:  python monitor.py   (w compose: serwis "monitor")
Zmienne środowiskowe (wszystkie opcjonalne):
  MONITOR_URL        adres panelu, domyślnie http://127.0.0.1:5000
  HEALTHCHECKS_URL   adres pingu checki z healthchecks.io; puste =
                     tryb lokalny (tylko logi, nic nie wychodzi)
  MONITOR_CZAS       odstęp między odpytaniami w sekundach, domyślnie 900
"""

import json
import os
import time
import urllib.request
from datetime import datetime

URL_PANELU = os.environ.get("MONITOR_URL", "http://127.0.0.1:5000").rstrip("/")
URL_HEALTHCHECKS = os.environ.get("HEALTHCHECKS_URL", "")
ODSTEP = int(os.environ.get("MONITOR_CZAS", "900"))


def log(tekst: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')}  {tekst}",
          flush=True)


def stan_panelu() -> str:
    """Odpytuje panel jak przeglądarka księgowej; zwraca liczniki do pingu.

    Rzuca wyjątkiem (timeout, kod != 200), gdy panel nie odpowiada —
    wtedy ping NIE wychodzi i to jest właśnie mechanizm alarmu.
    """
    with urllib.request.urlopen(f"{URL_PANELU}/api/faktury",
                                timeout=10) as odp:
        dane = json.loads(odp.read())
    faktury = dane["faktury"]
    przetworzone = sum(1 for f in faktury
                       if f["status"] != "do_przetworzenia")
    w_kolejce = sum(1 for f in faktury if f["status"] == "do_przetworzenia")
    z_bledami = sum(1 for f in faktury
                    if f["bledy"] and f["status"] != "do_przetworzenia")
    do_zatwierdzenia = sum(1 for f in faktury
                           if f["status"] == "do_zatwierdzenia")
    return (f"przetworzone: {przetworzone}, w kolejce: {w_kolejce}, "
            f"z błędami: {z_bledami}, do zatwierdzenia: {do_zatwierdzenia}")


def ping(url: str, tresc: str) -> None:
    """POST z treścią — Healthchecks zapisuje body w historii pingów."""
    zadanie = urllib.request.Request(url, data=tresc.encode("utf-8"),
                                     method="POST")
    urllib.request.urlopen(zadanie, timeout=10)


def main() -> None:
    log(f"start: panel={URL_PANELU}, odstęp={ODSTEP} s, "
        f"Healthchecks={'tak' if URL_HEALTHCHECKS else 'NIE (tryb lokalny)'}")
    while True:
        try:
            tresc = stan_panelu()
            if URL_HEALTHCHECKS:
                ping(URL_HEALTHCHECKS, tresc)
            log(f"OK — {tresc}")
        except Exception as e:  # brak pingu = sygnał dla Healthchecks
            log(f"PANEL NIE ODPOWIADA — ping niewysłany: {e}")
        time.sleep(ODSTEP)


if __name__ == "__main__":
    main()
