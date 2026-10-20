"""Panel dla księgowej — lokalna aplikacja webowa (Flask).

Uruchomienie:  python panel.py

Przebieg:
  1. Batch nowych PDF-ów z data/ (render -> Gemini -> walidacja -> zapis
     do JSON i bazy), wznawianie jak w weryfikatorze; zepsuty plik =
     rekord z "błąd przetwarzania"; bez GEMINI_API_KEY krok pomijany.
  2. Serwer Flask pod http://127.0.0.1:5000 + otwarcie w przeglądarce.
     Panel działa, dopóki skrypt jest uruchomiony (Ctrl+C kończy).

Możliwości panelu (dla księgowej):
  - zakładki: Wszystkie / Do zatwierdzenia / Błędy / Zatwierdzone,
  - podgląd: wyekstrahowane dane + pozycje obok oryginalnego PDF
    (tylko odczyt — bez możliwości korygowania),
  - zatwierdzenie faktury bez błędów — status 'zatwierdzone' jest
    chroniony przed nadpisaniem przez re-run,
  - eksport zatwierdzonych faktur do XML + PDF-y (eksport.py), po czym
    faktury dostają status 'wyeksportowane' i znikają z panelu.

Endpointy:
  GET  /                             panel (templates/panel.html)
  GET  /api/faktury                  lista faktur z pozycjami (JSON)
  GET  /pdf/<plik>                   oryginalny PDF (tylko pliki z bazy)
  POST /api/faktury/<id>/zatwierdz   zatwierdzenie faktury
  POST /api/eksport                  eksport zatwierdzonych do XML
  GET  /eksport                      pobranie wygenerowanego XML
"""

import json
import os
import time
import webbrowser
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, send_file, send_from_directory
from google import genai

import baza
import eksport
from weryfikator import (PRZERWA, renderuj_pdf_do_png, waliduj,
                         wyodrebnij_dane, zapisz_wyniki)

SCIEZKA_WYNIKOW = os.path.join("data", "wyniki_weryfikacji.json")
# Na hoście panel jest lokalny (127.0.0.1); w kontenerze compose ustawia
# PANEL_HOST=0.0.0.0, żeby dosięgła go nakładka/przeglądarka z sieci:
HOST = os.environ.get("PANEL_HOST", "127.0.0.1")
PORT = int(os.environ.get("PANEL_PORT", "5000"))

app = Flask(__name__)


# --- przetwarzanie batcha (bez zmian względem wersji statycznej) ----------


def wczytaj_wyniki() -> list[dict]:
    """Aktualne rekordy z poprzedniego przebiegu (jak w weryfikatorze)."""
    if not os.path.exists(SCIEZKA_WYNIKOW):
        return []
    with open(SCIEZKA_WYNIKOW, encoding="utf-8") as plik:
        return [w for w in json.load(plik) if "pozycje" in w]


def przetworz_nowe_pdfy(klient, wyniki: list[dict], con) -> int:
    """Wyciąga dane z PDF-ów nieobecnych jeszcze w wynikach.

    Zwraca liczbę przetworzonych plików. Po każdym pliku wyniki są
    zapisywane (JSON + baza), więc przerwany batch można wznowić.
    """
    if klient is None:
        print("Brak GEMINI_API_KEY — pomijam przetwarzanie nowych PDF-ów "
              "(panel powstanie z danych w bazie).")
        return 0

    zrobione = {w["plik"] for w in wyniki}
    pliki = sorted(p for p in os.listdir("data") if p.endswith(".pdf"))
    nowe = [p for p in pliki if p not in zrobione]
    if not nowe:
        print("Brak nowych PDF-ów do przetworzenia.")
        return 0

    print(f"Przetwarzam {len(nowe)} nowych PDF-ów...\n")
    for nazwa in nowe:
        print(f"{nazwa}: odczyt...", flush=True)
        try:
            png = renderuj_pdf_do_png(os.path.join("data", nazwa))
            dane = wyodrebnij_dane(klient, png)
            rekord = {"plik": nazwa, **dane, "bledy": waliduj(dane)}
        except Exception as e:
            # zepsuty PDF albo API po wyczerpaniu prób — bez kosztu
            # wywołania w pierwszym przypadku; rekord bez "pozycje",
            # więc kolejny run ponowi próbę:
            rekord = {"plik": nazwa, "bledy": [f"błąd przetwarzania: {e}"]}
            print(f"  BŁĄD: {e}")
        else:
            status = "OK" if not rekord["bledy"] else "BŁĄD: " + "; ".join(rekord["bledy"])
            print(f"  {rekord.get('numer', '?'):20} -> {status}")
            time.sleep(PRZERWA)  # przerywa tylko po wywołaniu API

        wyniki.append(rekord)
        zapisz_wyniki(SCIEZKA_WYNIKOW, wyniki)
        baza.zapisz_fakture(con, rekord)
    return len(nowe)


def oznacz_duplikaty(wyniki: list[dict]) -> None:
    """Dopisuje błąd powtórzonego numeru faktury (1:1 jak weryfikator)."""
    widziane = {}
    for w in sorted(wyniki, key=lambda x: x["plik"]):
        numer = w.get("numer")
        if numer in widziane:
            opis = (f"duplikat numeru faktury {numer} "
                    f"(wcześniejsze wystąpienie: {widziane[numer]})")
            if opis not in w["bledy"]:
                w["bledy"].append(opis)
        else:
            widziane[numer] = w["plik"]


# --- endpointy ------------------------------------------------------------


@app.get("/")
def strona():
    return send_from_directory("templates", "panel.html")


@app.get("/api/faktury")
def api_faktury():
    con = baza.polacz()
    try:
        # wyeksportowane nie są już potrzebne księgowej — ukrywamy:
        faktury = [f for f in baza.pobierz_faktury(con)
                   if f["status"] != "wyeksportowane"]
        for f in faktury:
            szczegoly = baza.pobierz_szczegoly(con, f["id"])
            f["pozycje"] = szczegoly["pozycje"] if szczegoly else []
    finally:
        baza.zamknij(con)
    return jsonify({
        "stanNa": datetime.now().isoformat(timespec="seconds"),
        "faktury": faktury,
    })


@app.get("/pdf/<plik>")
def pdf(plik: str):
    # serwujemy wyłącznie pliki przypisane do faktur w bazie:
    con = baza.polacz()
    try:
        istnieje = con.execute(
            "SELECT 1 FROM faktury WHERE plik = ?", (plik,)).fetchone()
    finally:
        baza.zamknij(con)
    if not istnieje:
        return jsonify({"blad": "nie ma takiego pliku w bazie"}), 404
    return send_from_directory("data", plik)


@app.post("/api/faktury/<int:faktura_id>/zatwierdz")
def api_zatwierdz(faktura_id: int):
    con = baza.polacz()
    try:
        f = baza.pobierz_szczegoly(con, faktura_id)
        if f is None:
            return jsonify({"blad": "nie ma takiej faktury"}), 404
        if f["bledy"]:
            return jsonify({"blad": "faktura ma błędy walidacji — "
                                     "nie może zostać zatwierdzona"}), 409
        baza.zatwierdz_fakture(con, faktura_id)
    finally:
        baza.zamknij(con)
    return jsonify({"ok": True})


@app.post("/api/eksport")
def api_eksport():
    con = baza.polacz()
    try:
        n = eksport.eksportuj_xml(con)  # status: zatwierdzone
        pdfy = eksport.zapisz_pdf_do_przetworzonych(con)
        if n:
            baza.oznacz_wyeksportowane(con)  # znikają z panelu
    finally:
        baza.zamknij(con)
    if n == 0:
        return jsonify({"blad": "brak zatwierdzonych faktur do eksportu"}), 409
    return jsonify({"liczba": n, "pdfy": pdfy,
                    "folder": eksport.FOLDER_PRZETWORZONE,
                    "plik": eksport.SCIEZKA_EKSPORTU})


@app.get("/eksport")
def pobierz_eksport():
    if not os.path.exists(eksport.SCIEZKA_EKSPORTU):
        return jsonify({"blad": "plik eksportu jeszcze nie powstał"}), 404
    return send_file(eksport.SCIEZKA_EKSPORTU, as_attachment=True)


# --- start ----------------------------------------------------------------


def przygotuj_dane() -> int:
    """Batch nowych PDF-ów + synchronizacja JSON-u z bazą.

    Wydzielone z main(), żeby launcher desktopowy (desktop.py) mógł
    wykonać ten sam krok bez kopiowania kodu. Zwraca liczbę nowych PDF-ów.
    """
    load_dotenv()
    klucz = os.environ.get("GEMINI_API_KEY")
    klient = genai.Client(api_key=klucz) if klucz else None

    wyniki = wczytaj_wyniki()
    con = baza.polacz()
    nowe = przetworz_nowe_pdfy(klient, wyniki, con)
    oznacz_duplikaty(wyniki)
    zapisz_wyniki(SCIEZKA_WYNIKOW, wyniki)
    # finalna synchronizacja bazy (statusy finalne po checku duplikatów;
    # baza dogania JSON także po przerwaniu albo skasowaniu faktury.db):
    for w in wyniki:
        baza.zapisz_fakture(con, w)
    baza.zamknij(con)

    z_bledem = sum(1 for w in wyniki if w["bledy"])
    print(f"\nNowe PDF-y: {nowe}, łącznie faktur: {len(wyniki)} "
          f"(z błędami: {z_bledem})")
    return nowe


def main():
    przygotuj_dane()
    url = f"http://{HOST}:{PORT}"
    if os.environ.get("BEZ_PRZEGLADARKI") == "1":
        # kontener/VPS: przeglądarki tam nie ma, URL otwiera użytkownik
        # (albo nakładka desktopowa na swoim komputerze):
        print(f"Panel: {url}")
    else:
        print(f"Panel: {url}  (Ctrl+C kończy)")
        webbrowser.open(url)
    app.run(host=HOST, port=PORT)


if __name__ == "__main__":
    main()
