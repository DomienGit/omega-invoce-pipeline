"""Panel dla księgowej — lokalna aplikacja webowa (Flask).

Uruchomienie:  python panel.py

Przebieg:
  1. Przy starcie: przetworzenie kolejki 'do_przetworzenia' z bazy
     (wznowienie po przerwaniu — patrz niżej). Bez GEMINI_API_KEY krok
     pomijany (kolejka czeka).
  2. Serwer Flask pod http://127.0.0.1:5000 + otwarcie w przeglądarce.
     Panel działa, dopóki skrypt jest uruchomiony (Ctrl+C kończy).

Możliwości panelu (dla księgowej):
  - wgrywanie faktur: przycisk "Wybierz faktury…" (pliki) albo
    "Wybierz folder…" — każdy wskazany PDF dostaje wiersz o statusie
    'do przetworzenia' i jest przetwarzany w tle; lista odświeża się
    sama w trakcie. Ta sama treść (odcisk SHA-256) nie jest czytana
    drugi raz ("już przetworzona" / "już w kolejce"),
  - zakładki: Wszystkie / Do przetworzenia / Do zatwierdzenia / Błędy /
    Zatwierdzone; przycisk "Przetwórz ponownie" dla czekających
    (nieudanych) faktur — też automatycznie przy każdym starcie,
  - podgląd: wyekstrahowane dane + pozycje obok oryginalnego PDF
    (tylko odczyt — bez możliwości korygowania),
  - zatwierdzenie faktury bez błędów — status 'zatwierdzone' jest
    chroniony przed nadpisaniem,
  - eksport zatwierdzonych faktur do XML + PDF-y (eksport.py), po czym
    faktury dostają status 'wyeksportowane' i znikają z panelu.

Endpointy:
  GET  /                             panel (templates/panel.html)
  GET  /api/faktury                  lista faktur z pozycjami (JSON)
  POST /api/faktury/wgraj            przyjęcie PDF-ów (multipart -> kolejka)
  POST /api/przetwarzaj              ręczne przetworzenie kolejki (przycisk)
  GET  /api/przetwarzanie            czy batch w tle właśnie działa
  GET  /pdf/<plik>                   oryginalny PDF (tylko pliki z bazy)
  POST /api/faktury/<id>/zatwierdz   zatwierdzenie faktury
  POST /api/eksport                  eksport zatwierdzonych do XML
  GET  /eksport                      pobranie najnowszego XML
"""

import glob
import os
import threading
import webbrowser
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file, send_from_directory
from google import genai

import baza
import eksport
import weryfikator

# Na hoście panel jest lokalny (127.0.0.1); w kontenerze compose ustawia
# PANEL_HOST=0.0.0.0, żeby dosięgła go nakładka/przeglądarka z sieci:
HOST = os.environ.get("PANEL_HOST", "127.0.0.1")
PORT = int(os.environ.get("PANEL_PORT", "5000"))

app = Flask(__name__)
# PDF-y bywają ciężkie (skany) — hojny limit jednorazowej wysyłki:
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024 * 1024

# --- wgrywanie faktur (batch w tle) -----------------------------------------

_PRZETWARZANIE = threading.Event()   # czy batch właśnie leci (status dla UI)
_ZAMEK = threading.Lock()            # wzajemne wykluczanie przebiegów batcha


def _batch_w_tle() -> None:
    """Wątek tła: przetwarza kolejkę 'do_przetworzenia' z bazy (logika
    w weryfikator.przetworz_kolejke), a po przebiegu sprawdza jeszcze raz
    — pliki wgrane w trakcie też zostaną obsłużone. Bez klucza API nie
    startuje (kolejka czeka)."""
    with _ZAMEK:
        _PRZETWARZANIE.set()
        try:
            if not os.environ.get("GEMINI_API_KEY"):
                return
            klient = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
            while True:
                con = baza.polacz()
                try:
                    n = weryfikator.przetworz_kolejke(klient, con)
                finally:
                    baza.zamknij(con)
                if n == 0:
                    break
        finally:
            _PRZETWARZANIE.clear()


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


@app.post("/api/faktury/wgraj")
def api_wgraj():
    """Przyjmuje PDF-y od księgowej (multipart): każdy trafia do kolejki.

    Nie-PDF-y są pomijane z powodem. Treść już znaną bazie (odcisk
    SHA-256) pomijamy ("już przetworzona" / "już w kolejce"), zanim plik
    cokolwiek kosztuje. Przyjęte PDF-y lądują w data/ ze statusem
    'do przetworzenia' i są przetwarzane w tle (od razu albo po bieżącym
    przebiegu — patrz _batch_w_tle).
    """
    zapisane: list[str] = []
    pominiete: list[str] = []
    con = baza.polacz()
    try:
        for plik in request.files.getlist("pliki"):
            # basename neutralizuje ewentualne ścieżki w nazwie (przejście
            # poza data/ jest niemożliwe):
            nazwa = os.path.basename(plik.filename or "")
            if not nazwa.lower().endswith(".pdf"):
                pominiete.append(f"{nazwa}: to nie jest PDF")
                continue
            bajty = plik.read()
            powod = baza.dodaj_do_kolejki(con, nazwa,
                                          baza.odcisk_bajtow(bajty))
            if powod:
                pominiete.append(f"{nazwa}: {powod}")
            else:
                with open(os.path.join("data", nazwa), "wb") as docelowy:
                    docelowy.write(bajty)
                zapisane.append(nazwa)
    finally:
        baza.zamknij(con)

    if zapisane and not _PRZETWARZANIE.is_set():
        threading.Thread(target=_batch_w_tle, daemon=True).start()
    return jsonify({"zapisane": len(zapisane), "pominiete": pominiete})


@app.post("/api/przetwarzaj")
def api_przetwarzaj():
    """Ręczne uruchomienie przetwarzania kolejki (przycisk w panelu)."""
    con = baza.polacz()
    try:
        czeka = con.execute(
            "SELECT 1 FROM faktury WHERE status = 'do_przetworzenia' LIMIT 1"
        ).fetchone()
    finally:
        baza.zamknij(con)
    if not czeka:
        return jsonify({"blad": "kolejka jest pusta"}), 409
    if not os.environ.get("GEMINI_API_KEY"):
        return jsonify({"blad": "brak klucza GEMINI_API_KEY na serwerze"}), 503
    if not _PRZETWARZANIE.is_set():
        threading.Thread(target=_batch_w_tle, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/przetwarzanie")
def api_przetwarzanie():
    return jsonify({"aktywny": _PRZETWARZANIE.is_set()})


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
        n, plik_xml = eksport.eksportuj_xml(con)  # status: zatwierdzone
        pdfy = eksport.zapisz_pdf_do_przetworzonych(con)
        if n:
            baza.oznacz_wyeksportowane(con)  # znikają z panelu
    finally:
        baza.zamknij(con)
    if n == 0:
        return jsonify({"blad": "brak zatwierdzonych faktur do eksportu"}), 409
    return jsonify({"liczba": n, "pdfy": pdfy,
                    "folder": eksport.FOLDER_PRZETWORZONE,
                    "plik": os.path.basename(plik_xml)})


@app.get("/eksport")
def pobierz_eksport():
    # każdy eksport to nowy plik (eksport.xml, eksport_1.xml...) — pobieramy
    # najnowszy, czyli o największej dacie modyfikacji:
    pliki = sorted(
        glob.glob(os.path.join(eksport.FOLDER_PRZETWORZONE, "eksport*.xml")),
        key=os.path.getmtime)
    if not pliki:
        return jsonify({"blad": "plik eksportu jeszcze nie powstał"}), 404
    return send_file(pliki[-1], as_attachment=True)


# --- start ----------------------------------------------------------------


def przygotuj_dane() -> int:
    """Przetwarza kolejkę 'do_przetworzenia' (wznowienie po restarcie).

    Wydzielone z main(), żeby launcher desktopowy (desktop.py) mógł
    wykonać ten sam krok bez kopiowania kodu. Zwraca liczbę prób.
    """
    load_dotenv()
    klucz = os.environ.get("GEMINI_API_KEY")
    if not klucz:
        print("Brak GEMINI_API_KEY — kolejka 'do przetworzenia' czeka.")
        return 0
    klient = genai.Client(api_key=klucz)

    con = baza.polacz()
    try:
        n = weryfikator.przetworz_kolejke(klient, con)
        aktywne, z_bledem = con.execute(
            "SELECT COUNT(*), SUM(bledy != '[]') FROM faktury "
            "WHERE status NOT IN ('wyeksportowane', 'do_przetworzenia')"
        ).fetchone()
    finally:
        baza.zamknij(con)

    print(f"\nKolejka: {n} prób, aktywnych faktur: {aktywne or 0} "
          f"(z błędami: {z_bledem or 0})")
    return n


def main():
    # batch startowy pod tym samym zamkiem co wątek wgrywania — nowy
    # upload w trakcie startu nie przetworzy niczego dwa razy:
    with _ZAMEK:
        _PRZETWARZANIE.set()
        try:
            przygotuj_dane()
        finally:
            _PRZETWARZANIE.clear()
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
