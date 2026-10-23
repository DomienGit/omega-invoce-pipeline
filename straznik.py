"""Strażnik dzienny — raport z bazy + alerty (warstwa 3 monitoringu).

Uruchamiany raz dziennie (cron na serwerze o 20:00 wywołuje
`docker compose run --rm straznik`). Robi trzy rzeczy:

  1. RAPORT — jeden rzut oka: ile przetworzono dzisiaj, ile czeka
     weryfikacji księgowej, ile wyeksportowano (łącznie), ile w kolejce.
  2. ALERTY — cztery sygnały z notatek o monitoringu (patrz funkcje
     alert_*). Aktywne alerty pingują adres Healthchecks z dopiskiem
     "/fail" — Healthchecks nie czeka na harmonogram i od razu wysyła
     maila. Zasada anty-zmęczenie: tylko te 4 sytuacje budzą maila,
     cała reszta jest w raporcie.
  3. ZNAK ŻYCIA — sam raport pinguje drugą checkę w Healthchecks
     (cron 20:00, grace 30 min). Brak pingu do 20:30 = strażnik nie
     przebiegł = alarm (drugi dead man's switch obok monitor.py).

Raport zapisuje też do logi/straznik_RRRR-MM-DD.txt.

Uruchomienie:  python straznik.py
Zmienne środowiskowe (opcjonalne):
  HEALTHCHECKS_STRAZNIK_URL  adres pingu checki z healthchecks.io;
                             puste = tryb lokalny (raport tylko na ekran)
"""

import os
import urllib.request
from datetime import date, datetime, timedelta

import baza

URL_HEALTHCHECKS = os.environ.get("HEALTHCHECKS_STRAZNIK_URL", "")
FOLDER_LOGOW = "logi"
PROG_WIEKU_KOLEJKI = timedelta(hours=4)
SPADEK_SKUTECZNOSCI = 15     # punkty procentowe vs dotychczasowa średnia
MIN_FAKTUR_W_OKNIE = 10      # mniejsze okno 7-dniowe nic jeszcze nie mówi
PROG_BLEDOW_API = 2          # ile czekających z błędem API budzi alert
KODY_API = ("429", "500", "503")


def stan_dnia(con) -> dict:
    """Liczniki do nagłówka raportu."""
    dzis = f"{date.today().isoformat()}T%"
    (przetworzone, do_sprawdzenia,
     wyeksportowane, kolejka) = con.execute(
        "SELECT SUM(przetworzone_at LIKE ?), SUM(status = 'do_weryfikacji'),"
        "       SUM(status = 'wyeksportowane'),"
        "       SUM(status = 'do_przetworzenia') FROM faktury",
        (dzis,)).fetchone()
    return {"przetworzone_dzis": przetworzone or 0,
            "do_sprawdzenia": do_sprawdzenia or 0,
            "wyeksportowane": wyeksportowane or 0,
            "kolejka": kolejka or 0}


def alert_kolejka_stoi(con) -> str | None:
    """Najstarsza faktura w kolejce czeka dłużej niż 4 h.

    Świeży upload to normalne oczekiwanie na Gemini; 4 h ciszy oznacza,
    że nikt nie przetwarza (panel zawieszony, klucz wygasł, API leży).
    Wiersze bez znaku czasu (sprzed migracji) pomijamy.
    """
    czeka = con.execute(
        "SELECT plik, wgrane_at FROM faktury WHERE status = 'do_przetworzenia'"
        " AND wgrane_at IS NOT NULL ORDER BY wgrane_at LIMIT 1").fetchone()
    if not czeka:
        return None
    plik, wgrane_at = czeka
    wiek = datetime.now() - datetime.fromisoformat(wgrane_at)
    if wiek <= PROG_WIEKU_KOLEJKI:
        return None
    godziny = wiek.total_seconds() / 3600
    return (f"faktura {plik} czeka w kolejce {godziny:.0f} h "
            f"(od {wgrane_at}; próg {PROG_WIEKU_KOLEJKI}) — sprawdź, "
            f"czy panel przetwarza")


def alert_trend_bledow(con) -> str | None:
    """Faktury z błędami ekstrakcji przybywają dzień po dniu.

    Stan chwilowy nic nie znaczy (trudny skan), ale rosnący trend
    przez 2 dni z rzędu to zwykle zmiana formatu faktur u dostawcy
    albo problem z modelem. Liczymy wpisy, które SKOŃCZYLY się błędem
    walidacji (do_weryfikacji/dot. zatwierdzone po poprawkach).
    """
    dniowo = dict(con.execute(
        "SELECT substr(przetworzone_at, 1, 10), COUNT(*) FROM faktury "
        "WHERE bledy != '[]' AND status != 'do_przetworzenia' "
        "AND przetworzone_at IS NOT NULL GROUP BY 1"))
    dzisiaj = date.today()
    # [przedwczoraj, wczoraj, dzisiaj] — każdy kolejny dzień więcej
    # błędów niż poprzedni, dzisiaj co najmniej 2 (1 dziennie to szum):
    tryby = [dniowo.get((dzisiaj - timedelta(days=d)).isoformat(), 0)
             for d in (2, 1, 0)]
    if tryby[2] > tryby[1] > tryby[0] and tryby[2] >= 2:
        return ("liczba faktur z błędami rośnie dzień po dniu: "
                f"{tryby[0]} -> {tryby[1]} -> {tryby[2]} — zwykle zmiana "
                f"formatu faktur albo problem z modelem")


def alert_spadek_skutecznosci(con) -> str | None:
    """Skuteczność z ostatnich 7 dni vs dotychczasowa średnia.

    Podobny cel co trend błędów, ale patrzymy na proporcje, nie liczby
    — łapie sytuację "przybyło faktur, ale prawie żadna nie przechodzi".
    Okno działa dopiero od 10 faktur (2 trudne skany z 3 to nie trend).
    """
    granica = (datetime.now() - timedelta(days=7)
               ).isoformat(timespec="seconds")
    n_okno, ok_okno = con.execute(
        "SELECT COUNT(*), SUM(bledy = '[]') FROM faktury "
        "WHERE status != 'do_przetworzenia' AND przetworzone_at >= ?",
        (granica,)).fetchone()
    if not n_okno or n_okno < MIN_FAKTUR_W_OKNIE:
        return None
    n_all, ok_all = con.execute(
        "SELECT COUNT(*), SUM(bledy = '[]') FROM faktury "
        "WHERE status != 'do_przetworzenia'").fetchone()
    okno = 100 * (ok_okno or 0) / n_okno
    srednia = 100 * (ok_all or 0) / n_all
    if srednia - okno >= SPADEK_SKUTECZNOSCI:
        return (f"skuteczność ekstrakcji spadła: ostatnie 7 dni "
                f"{okno:.0f}% vs dotychczasowa średnia {srednia:.0f}% "
                f"(próg: -{SPADEK_SKUTECZNOSCI} p.p.)")


def alert_bledy_api(con) -> str | None:
    """Kilka faktur w kolejce z błędem 429/500/503 = problem z Geminim.

    Pojedynczy 429 to limit na darmowym tierze (próba sama się ponowi);
    kilka naraz wygląda jak awaria dostawcy albo wygasły klucz.
    """
    z_bledem = con.execute(
        "SELECT plik, blad_przetworzenia FROM faktury "
        "WHERE status = 'do_przetworzenia' "
        "AND blad_przetworzenia IS NOT NULL").fetchall()
    api = [(plik, blad) for plik, blad in z_bledem
           if any(kod in blad for kod in KODY_API)]
    if len(api) >= PROG_BLEDOW_API:
        return ("błędy API w kolejce (Gemini/limity): "
                + "; ".join(f"{plik}" for plik, _ in api))


def ping(url: str, tresc: str) -> None:
    """POST z treścią raportu — Healthchecks zapisuje body w historii."""
    zadanie = urllib.request.Request(url, data=tresc.encode("utf-8"),
                                     method="POST")
    urllib.request.urlopen(zadanie, timeout=10)


def main() -> None:
    con = baza.polacz()
    try:
        stan = stan_dnia(con)
        alerty = [a for a in (alert_kolejka_stoi(con),
                              alert_trend_bledow(con),
                              alert_spadek_skutecznosci(con),
                              alert_bledy_api(con)) if a]
    finally:
        baza.zamknij(con)

    dzisiaj = date.today().isoformat()
    raport = (
        f"Omega — raport dzienny {dzisiaj}\n"
        f"przetworzone dzisiaj: {stan['przetworzone_dzis']}, "
        f"do sprawdzenia: {stan['do_sprawdzenia']}, "
        f"wyeksportowane (łącznie): {stan['wyeksportowane']}, "
        f"kolejka: {stan['kolejka']}\n"
        + ("ALERTY:\n- " + "\n- ".join(alerty) if alerty else "ALERTY: brak")
    )
    print(raport)

    # raport jako plik — "czy mail/raport powstał" widać na dysku:
    os.makedirs(FOLDER_LOGOW, exist_ok=True)
    with open(os.path.join(FOLDER_LOGOW, f"straznik_{dzisiaj}.txt"), "w",
              encoding="utf-8") as plik:
        plik.write(raport + "\n")

    if URL_HEALTHCHECKS:
        # aktywny alarm = /fail (natychmiastowy mail), spokojny dzień =
        # zwykły ping (potwierdzenie, że strażnik przebiegł do 20:30):
        url = URL_HEALTHCHECKS.rstrip("/") + ("/fail" if alerty else "")
        ping(url, raport)
        print(f"ping do Healthchecks: {'/fail (alert!)' if alerty else 'ok'}")


if __name__ == "__main__":
    main()
