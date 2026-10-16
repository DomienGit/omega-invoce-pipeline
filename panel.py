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
  - porównanie: wyekstrahowane dane + pozycje obok oryginalnego PDF,
  - ręczna poprawa danych (nagłówek + pozycje) — po zapisie walidacja
    liczy się od nowa; poprawki trafiają też do
    data/wyniki_weryfikacji.json, żeby re-run nie przywrócił starych
    wartości,
  - zatwierdzenie faktury bez błędów — status 'zatwierdzone' jest
    chroniony przed nadpisaniem przez re-run,
  - eksport zatwierdzonych faktur do data/eksport.xml (funkcja
    eksport.eksportuj_xml) + pobranie pliku z panelu.

Endpointy:
  GET  /                             panel (templates/panel.html)
  GET  /api/faktury                  lista faktur z pozycjami (JSON)
  GET  /pdf/<plik>                   oryginalny PDF (tylko pliki z bazy)
  POST /api/faktury/<id>             poprawa danych (nagłówek + pozycje)
  POST /api/faktury/<id>/zatwierdz   zatwierdzenie faktury
  POST /api/eksport                  eksport zatwierdzonych do XML
  GET  /eksport                      pobranie wygenerowanego XML
"""

import json
import os
import sqlite3
import time
import webbrowser
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file, send_from_directory
from google import genai

import baza
import eksport
from weryfikator import (PRZERWA, renderuj_pdf_do_png, waliduj,
                         wyodrebnij_dane, zapisz_wyniki)

SCIEZKA_WYNIKOW = os.path.join("data", "wyniki_weryfikacji.json")
HOST = "127.0.0.1"
PORT = 5000

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


def zaktualizuj_wyniki_json(rekord: dict) -> None:
    """Podmienia rekord faktury w data/wyniki_weryfikacji.json.

    Ręczne poprawki muszą trafić też do cache'u ekstrakcji — inaczej
    kolejny przebieg (panel/weryfikator) przywróciłby stare wartości,
    kasując korekty księgowej.
    """
    wyniki = []
    if os.path.exists(SCIEZKA_WYNIKOW):
        with open(SCIEZKA_WYNIKOW, encoding="utf-8") as plik:
            wyniki = json.load(plik)
    wyniki = [w for w in wyniki if w.get("plik") != rekord["plik"]]
    wyniki.append(rekord)
    zapisz_wyniki(SCIEZKA_WYNIKOW, wyniki)


# --- endpointy ------------------------------------------------------------


@app.get("/")
def strona():
    return send_from_directory("templates", "panel.html")


@app.get("/api/faktury")
def api_faktury():
    con = baza.polacz()
    try:
        faktury = baza.pobierz_faktury(con)
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
                                     "najpierw popraw dane"}), 409
        baza.zatwierdz_fakture(con, faktura_id)
    finally:
        baza.zamknij(con)
    return jsonify({"ok": True})


@app.post("/api/faktury/<int:faktura_id>")
def api_popraw(faktura_id: int):
    """Zapisuje ręczne poprawki i przelicza walidację od nowa."""
    dane = request.get_json(silent=True) or {}
    con = baza.polacz()
    try:
        f = baza.pobierz_szczegoly(con, faktura_id)
        if f is None:
            return jsonify({"blad": "nie ma takiej faktury"}), 404

        rekord = {
            "plik": f["plik"],
            "numer": dane.get("numer"),
            "nazwa_sprzedawcy": dane.get("nazwa_sprzedawcy"),
            "nip_sprzedawcy": dane.get("nip_sprzedawcy"),
            "nazwa_nabywcy": dane.get("nazwa_nabywcy"),
            "nip_nabywcy": dane.get("nip_nabywcy"),
            "data_wystawienia": dane.get("data_wystawienia"),
            "termin_platnosci": dane.get("termin_platnosci"),
            "netto": dane.get("netto"),
            "vat": dane.get("vat"),
            "brutto": dane.get("brutto"),
            "pozycje": dane.get("pozycje") or [],
        }
        rekord["bledy"] = waliduj(rekord)
        zaktualizuj_wyniki_json(rekord)
        try:
            baza.zapisz_fakture(con, rekord)
        except sqlite3.IntegrityError:
            # np. numer zmieniony na taki, jaki ma już inna faktura
            # tego samego sprzedawcy:
            return jsonify({"blad": "inna faktura ma już ten numer "
                                     "i NIP sprzedawcy"}), 409
    finally:
        baza.zamknij(con)
    return jsonify({"bledy": rekord["bledy"]})


@app.post("/api/eksport")
def api_eksport():
    con = baza.polacz()
    try:
        n = eksport.eksportuj_xml(con)  # status: zatwierdzone
        pdfy = eksport.zapisz_pdf_do_przetworzonych(con)
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


def main():
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
    url = f"http://{HOST}:{PORT}"
    print(f"Panel: {url}  (Ctrl+C kończy)")
    webbrowser.open(url)
    app.run(host=HOST, port=PORT)


if __name__ == "__main__":
    main()
