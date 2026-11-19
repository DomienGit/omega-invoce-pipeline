"""Launcher desktopowy — panel Omega Faktury w natywnym oknie (pywebview).

Działanie:
  1. Jeśli port panelu jest wolny: przygotowanie danych (jak panel.py —
     nowe PDF-y, walidacja, baza) i start serwera Flask w WĄTKU DAEMON
     (wątek daemon ginie automatycznie razem z procesem).
  2. Jeśli port zajęty (panel.py już działa w terminalu): pomijamy
     przygotowanie danych i otwieramy tylko okno.
  3. Natywne okno pywebview z panelem. Zamknięcie okna (X) kończy cały
     program — bez wiszącego serwera w tle.

Uruchomienie:  python desktop.py  (albo skrót "Omega Faktury" z menu)
Zależności:    pywebview + PyGObject/WebKitGTK (backend GTK)
"""

import socket
import threading
import time

import webview
from dotenv import load_dotenv

import konfiguracja
import panel


def port_odpowiada(host: str, port: int, timeout: float = 0.5) -> bool:
    """True, gdy port przyjmuje połączenia (serwer już nasłuchuje)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def czekaj_na_serwer(host: str, port: int, timeout_s: float = 15) -> bool:
    """Blokuje, aż serwer Flask zacznie nasłuchiwać (start trwa ~1 s)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if port_odpowiada(host, port):
            return True
        time.sleep(0.2)
    return False


def main():
    # .env tylko w punkcie wejścia (import panel.py niczego nie wczytuje);
    # konfiguracja i aplikacja budowane jawnie, jak w panel.main():
    load_dotenv()
    konf = konfiguracja.z_srodowiska()
    host, port = konf.host, konf.port
    url = f"http://{host}:{port}"

    if port_odpowiada(host, port):
        print("Panel już działa (port zajęty) — otwieram samo okno.")
    else:
        try:
            app = panel.utworz_app(konf)
        except ValueError as e:
            raise SystemExit(str(e))
        panel.przygotuj_dane(konf)
        watek = threading.Thread(
            target=app.run, kwargs={"host": host, "port": port},
            daemon=True)  # daemon=True: ginie, gdy zamknie się okno
        watek.start()
        if not czekaj_na_serwer(host, port):
            raise SystemExit("Serwer panelu nie wystartował w 15 s.")

    print(f"Panel: {url}  (zamknięcie okna kończy program)")
    webview.create_window("Omega Faktury", url, width=1100, height=750)
    webview.start()


if __name__ == "__main__":
    main()
