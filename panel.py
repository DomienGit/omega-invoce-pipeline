"""Panel dla księgowej — lokalna aplikacja webowa (Flask).

Uruchomienie:  python panel.py

Aplikację buduje funkcja utworz_app(konf) z konfiguracja.Konfiguracja —
bez stanu na poziomie modułu (blokady, liczniki prób logowania i znacznik
przetwarzania należą do konkretnej aplikacji), dzięki czemu testy tworzą
niezależne aplikacje na katalogach tymczasowych. .env wczytuje dopiero
main() (nigdy import).

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
  - zatwierdzenie faktury 'do zatwierdzenia' (bez błędów); zapisuje się,
    kto i kiedy ją zatwierdził (login z sesji),
  - eksport zatwierdzonych faktur do XML + PDF-y (eksport.py), po czym
    faktury dostają status 'wyeksportowane' i znikają z panelu; eksport
    działa pod blokadą — drugi równoczesny dostaje 409.

Wgrywanie nigdy nie nadpisuje istniejących faktur ani plików: zajęta
nazwa pliku zamieniana jest na wolną (faktura_1.pdf). Każda odpowiedź
dostaje nagłówki bezpieczeństwa (CSP, nosniff, DENY...), a za proxy
(PANEL_ZA_PROXY=1) ciasteczko sesji ma flagę Secure i jest HSTS.

Endpointy:
  GET  /                             panel (templates/panel.html)
  GET/POST /login                    logowanie (gdy ustawione PANEL_KONTA)
  POST /api/wyloguj                  koniec sesji
  GET  /healthz                      "żywy" bez danych (HEALTHCHECK Dockera)
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
import secrets
import threading
import time
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta

from dotenv import load_dotenv
from flask import (Flask, jsonify, redirect, render_template, request,
                   send_file, send_from_directory, session, url_for)
from google import genai
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

import baza
import eksport
import konfiguracja
import weryfikator

LIMIT_PROB = 5
BLOKADA_S = 15 * 60
_PUBLICZNE = {"login", "login_post", "wyloguj", "healthz"}  # nazwy endpointów

# Szablony mają skrypty i style wbudowane w stronę (stąd 'unsafe-inline'),
# ale nic nie ładują z zewnątrz; object-src 'self' dla podglądu PDF (embed):
_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; object-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class Stan:
    """Stan jednej aplikacji (nie modułu): blokady, znacznik batcha,
    nieudane logowania w pamięci procesu (restart czyści — akceptowalne)."""

    def __init__(self) -> None:
        self.przetwarzanie = threading.Event()  # czy batch właśnie leci (UI)
        self.zamek_batcha = threading.Lock()    # wykluczanie przebiegów batcha
        self.zamek_wgrywania = threading.Lock()  # jedno wgranie plików naraz
        self.zamek_eksportu = threading.Lock()   # jeden eksport naraz
        self.proby: dict[str, list[float]] = defaultdict(list)
        self.zamek_prob = threading.Lock()

    def zablokowane(self, ip: str) -> bool:
        granica = time.monotonic() - BLOKADA_S
        with self.zamek_prob:
            czasy = [t for t in self.proby[ip] if t > granica]
            self.proby[ip] = czasy
            return len(czasy) >= LIMIT_PROB

    def blad_logowania(self, ip: str) -> None:
        with self.zamek_prob:
            self.proby[ip].append(time.monotonic())


def _batch_w_tle(konf: konfiguracja.Konfiguracja, stan: Stan) -> None:
    """Wątek tła: przetwarza kolejkę 'do_przetworzenia' z bazy (logika
    w weryfikator.przetworz_kolejke), a po przebiegu sprawdza jeszcze raz
    — pliki wgrane w trakcie też zostaną obsłużone. Bez klucza API nie
    startuje (kolejka czeka)."""
    with stan.zamek_batcha:
        stan.przetwarzanie.set()
        try:
            if not konf.gemini_api_key:
                return
            klient = genai.Client(api_key=konf.gemini_api_key)
            while True:
                con = baza.polacz(konf.sciezka_bazy)
                try:
                    n = weryfikator.przetworz_kolejke(
                        klient, con, konf.katalog_danych)
                finally:
                    baza.zamknij(con)
                if n == 0:
                    break
        finally:
            stan.przetwarzanie.clear()


def utworz_app(konf: konfiguracja.Konfiguracja) -> Flask:
    """Buduje aplikację Flask z podanej konfiguracji (bez stanu w module).

    Rzuca ValueError, gdy są konta, a brak secret_key (main() zamienia to
    na SystemExit). Stan aplikacji: app.extensions["omega"] (obiekt Stan).
    """
    if konf.auth_wlaczona and not konf.secret_key:
        raise ValueError(
            "PANEL_KONTA ustawione, ale brak SECRET_KEY — wygeneruj: "
            'python -c "import secrets; print(secrets.token_hex(32))"')

    # root_path = katalog modułu, więc szablony nie zależą od katalogu
    # roboczego procesu:
    app = Flask(__name__)
    stan = Stan()
    app.extensions["omega"] = stan
    # PDF-y bywają ciężkie (skany) — hojny limit jednorazowej wysyłki:
    app.config["MAX_CONTENT_LENGTH"] = 128 * 1024 * 1024

    if konf.auth_wlaczona:
        app.config.update(
            SECRET_KEY=konf.secret_key,
            SESSION_COOKIE_NAME="omega_panel_sesja",
            SESSION_COOKIE_HTTPONLY=True,   # JS nie widzi ciasteczka (ochrona XSS)
            SESSION_COOKIE_SAMESITE="Lax",  # ciasteczko nie leci w cross-site POST (CSRF)
            # za Caddy panel stoi pod HTTPS — ciasteczko tylko po szyfrowanym
            # łączu; lokalnie po HTTP flaga musi być wyłączona, inaczej
            # przeglądarka odrzuci ciasteczko i logowanie nie zadziała:
            SESSION_COOKIE_SECURE=konf.za_proxy,
            PERMANENT_SESSION_LIFETIME=timedelta(hours=8),  # dzień pracy księgowej
        )

    # Za Caddy remote_addr to zawsze IP kontenera proxy — ProxyFix bierze IP
    # klienta z X-Forwarded-For. Włączone tylko za proxy (PANEL_ZA_PROXY=1):
    # bez proxy klient mógłby podrobić nagłówek i resetować licznik prób.
    if konf.za_proxy:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

    @app.after_request
    def naglowki_bezpieczenstwa(odp):
        """Nagłówki chroniące przed ramkami, zgadywaniem typu i obcymi skryptami.

        Wyjątek: PDF-y (/pdf/) wolno osadzić we własnym panelu — Chrome
        pokazuje <embed> z PDF-em tylko, gdy dokument nie ma X-Frame-Options:
        DENY / frame-ancestors 'none', więc dla nich ramka z własnego źródła
        jest dozwolona (SAMEORIGIN / 'self'); obce strony nadal nie mogą.
        """
        wlasny_pdf = request.path.startswith("/pdf/")
        odp.headers["X-Content-Type-Options"] = "nosniff"
        odp.headers["X-Frame-Options"] = "SAMEORIGIN" if wlasny_pdf else "DENY"
        odp.headers["Referrer-Policy"] = "same-origin"
        odp.headers["Content-Security-Policy"] = (
            _CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
            if wlasny_pdf else _CSP)
        if konf.za_proxy:
            odp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return odp


    @app.before_request
    def pilnuj_dostepu():
        """Jedna brama dla wszystkich endpointów (strona, API, PDF-y, eksport).

        Przepuszcza: wyłączone logowanie, endpointy publiczne, żądania
        monitora z tokenem (porównanie stałoczasowe), zalogowaną sesję.
        API/PDF odpowiadają 401 JSON-em (łapie to JS panelu), strony
        przekierowują na formularz logowania.
        """
        if (not konf.auth_wlaczona or request.endpoint in _PUBLICZNE
                or request.endpoint is None):   # brak trasy = 404, nic nie wycieka
            return None
        if konf.panel_token and secrets.compare_digest(
                request.headers.get("Authorization", ""), f"Bearer {konf.panel_token}"):
            return None
        if session.get("zalogowany"):
            return None
        if request.path.startswith(("/api/", "/pdf/")) or request.path == "/eksport":
            return jsonify({"blad": "wymagane logowanie"}), 401
        return redirect(url_for("login"))


    @app.get("/login")
    def login():
        if not konf.auth_wlaczona or session.get("zalogowany"):
            return redirect(url_for("strona"))
        return render_template("login.html")


    @app.post("/login")
    def login_post():
        ip = request.remote_addr or "?"
        if stan.zablokowane(ip):
            return render_template(
                "login.html",
                blad="Za dużo nieudanych prób — odczekaj ok. 15 minut."), 429
        login = request.form.get("login", "").strip()
        haslo = request.form.get("haslo", "")
        haslo_hash = konf.konta.get(login)
        if haslo_hash and check_password_hash(haslo_hash, haslo):
            session.clear()             # obrót sesji przy zmianie uprawnień
            session["zalogowany"] = True
            session["login"] = login
            session.permanent = True    # bez tego brak Expires — wygaśnięcie 8 h nie działa
            with stan.zamek_prob:
                stan.proby.pop(ip, None)
            return redirect(url_for("strona"))
        stan.blad_logowania(ip)
        time.sleep(0.3)                 # lekkie spowolnienie zgadywania hasła
        # jeden komunikat dla złego loginu i hasła — nie zdradzamy, co się nie zgadza:
        return render_template("login.html", blad="Błędny login lub hasło."), 401


    @app.post("/api/wyloguj")
    def wyloguj():
        session.clear()
        return redirect(url_for("login"))


    @app.get("/healthz")
    def healthz():
        """Żywy proces bez żadnych danych — dla HEALTHCHECK Dockera."""
        return jsonify({"ok": True})

    @app.get("/")
    def strona():
        return send_from_directory("templates", "panel.html")


    @app.get("/api/faktury")
    def api_faktury():
        con = baza.polacz(konf.sciezka_bazy)
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
        przebiegu — patrz _batch_w_tle(konf, stan)). Zajęta nazwa pliku nie powoduje
        nadpisania: faktura dostaje wolną nazwę (faktura_1.pdf), a odpowiedź
        wymienia zmienione nazwy w 'zmienione_nazwy'.
        """
        zapisane: list[str] = []
        pominiete: list[str] = []
        zmienione: list[str] = []
        con = baza.polacz(konf.sciezka_bazy)
        try:
            # jedno wgranie naraz — wybór wolnej nazwy i zapis pliku nie mogą
            # się pokrywać z równoległym żądaniem:
            with stan.zamek_wgrywania:
                for plik in request.files.getlist("pliki"):
                    # basename neutralizuje ewentualne ścieżki w nazwie
                    # (przejście poza data/ jest niemożliwe):
                    nazwa = os.path.basename(plik.filename or "")
                    if not nazwa.lower().endswith(".pdf"):
                        pominiete.append(f"{nazwa}: to nie jest PDF")
                        continue
                    bajty = plik.read()

                    def zapisz(wolna_nazwa: str, bajty=bajty) -> None:
                        with open(os.path.join(konf.katalog_danych, wolna_nazwa),
                                  "wb") as docelowy:
                            docelowy.write(bajty)

                    # plik ląduje na dysku PRZED wierszem w bazie (wątek tła nie
                    # znajdzie w kolejce faktury bez PDF-a):
                    zarejestrowana, powod = baza.dodaj_do_kolejki(
                        con, nazwa, baza.odcisk_bajtow(bajty), konf.katalog_danych, zapisz)
                    if powod:
                        pominiete.append(f"{nazwa}: {powod}")
                    else:
                        zapisane.append(zarejestrowana)
                        if zarejestrowana != nazwa:
                            zmienione.append(f"{nazwa} → {zarejestrowana}")
        finally:
            baza.zamknij(con)

        if zapisane and not stan.przetwarzanie.is_set():
            threading.Thread(target=_batch_w_tle, args=(konf, stan), daemon=True).start()
        return jsonify({"zapisane": len(zapisane), "pominiete": pominiete,
                        "zmienione_nazwy": zmienione})


    @app.post("/api/przetwarzaj")
    def api_przetwarzaj():
        """Ręczne uruchomienie przetwarzania kolejki (przycisk w panelu)."""
        con = baza.polacz(konf.sciezka_bazy)
        try:
            czeka = con.execute(
                "SELECT 1 FROM faktury WHERE status = 'do_przetworzenia' LIMIT 1"
            ).fetchone()
        finally:
            baza.zamknij(con)
        if not czeka:
            return jsonify({"blad": "kolejka jest pusta"}), 409
        if not konf.gemini_api_key:
            return jsonify({"blad": "brak klucza GEMINI_API_KEY na serwerze"}), 503
        if not stan.przetwarzanie.is_set():
            threading.Thread(target=_batch_w_tle, args=(konf, stan), daemon=True).start()
        return jsonify({"ok": True})


    @app.get("/api/przetwarzanie")
    def api_przetwarzanie():
        return jsonify({"aktywny": stan.przetwarzanie.is_set()})


    @app.get("/pdf/<plik>")
    def pdf(plik: str):
        # serwujemy wyłącznie pliki przypisane do faktur w bazie:
        con = baza.polacz(konf.sciezka_bazy)
        try:
            istnieje = con.execute(
                "SELECT 1 FROM faktury WHERE plik = ?", (plik,)).fetchone()
        finally:
            baza.zamknij(con)
        if not istnieje:
            return jsonify({"blad": "nie ma takiego pliku w bazie"}), 404
        return send_from_directory(os.path.abspath(konf.katalog_danych), plik)


    def _kto_zatwierdza() -> str:
        """Login do zapisu przy zatwierdzeniu: sesja, token monitora albo tryb dev."""
        if not konf.auth_wlaczona:
            return "tryb-deweloperski"
        if session.get("zalogowany"):
            return session.get("login") or "nieznany"
        return "token-monitora"   # inaczej pilnuj_dostepu by nie przepuścił


    _NAZWY_STATUSOW = {
        "do_przetworzenia": "czeka na odczyt",
        "do_weryfikacji": "ma błędy do weryfikacji",
        "zatwierdzone": "jest już zatwierdzona",
        "wyeksportowane": "została już wyeksportowana",
    }


    @app.post("/api/faktury/<int:faktura_id>/zatwierdz")
    def api_zatwierdz(faktura_id: int):
        con = baza.polacz(konf.sciezka_bazy)
        try:
            f = baza.pobierz_szczegoly(con, faktura_id)
            if f is None:
                return jsonify({"blad": "nie ma takiej faktury"}), 404
            if f["status"] != "do_zatwierdzenia":
                return jsonify({"blad": "tej faktury nie można zatwierdzić — "
                                f"{_NAZWY_STATUSOW.get(f['status'], f['status'])}"
                                }), 409
            if f["bledy"]:
                return jsonify({"blad": "faktura ma błędy walidacji — "
                                         "nie może zostać zatwierdzona"}), 409
            if not baza.zatwierdz_fakture(con, faktura_id, _kto_zatwierdza()):
                # status zmienił się między sprawdzeniem a zapisem:
                return jsonify({"blad": "status faktury zmienił się w międzyczasie "
                                         "— odśwież listę"}), 409
        finally:
            baza.zamknij(con)
        return jsonify({"ok": True})


    @app.post("/api/eksport")
    def api_eksport():
        # drugi równoczesny eksport nie czeka, tylko dostaje 409 — inaczej
        # księgowa klikająca dwa razy dostałaby dwa pliki:
        if not stan.zamek_eksportu.acquire(blocking=False):
            return jsonify({"blad": "eksport już trwa — poczekaj na jego "
                                    "zakończenie"}), 409
        try:
            con = baza.polacz(konf.sciezka_bazy)
            try:
                n, plik_xml, pdfy = eksport.eksportuj_zatwierdzone(
                    con, konf.katalog_danych, konf.katalog_eksportu)
            finally:
                baza.zamknij(con)
        finally:
            stan.zamek_eksportu.release()
        if n == 0:
            return jsonify({"blad": "brak zatwierdzonych faktur do eksportu"}), 409
        return jsonify({"liczba": n, "pdfy": pdfy,
                        "folder": konf.katalog_eksportu,
                        "plik": os.path.basename(plik_xml)})


    @app.get("/eksport")
    def pobierz_eksport():
        # każdy eksport to nowy plik (eksport.xml, eksport_1.xml...) — pobieramy
        # najnowszy, czyli o największej dacie modyfikacji:
        pliki = sorted(
            glob.glob(os.path.join(konf.katalog_eksportu, "eksport*.xml")),
            key=os.path.getmtime)
        if not pliki:
            return jsonify({"blad": "plik eksportu jeszcze nie powstał"}), 404
        return send_file(pliki[-1], as_attachment=True)


    return app


# --- start ----------------------------------------------------------------


def przygotuj_dane(konf: konfiguracja.Konfiguracja) -> int:
    """Przetwarza kolejkę 'do_przetworzenia' (wznowienie po restarcie).

    Wydzielone z main(), żeby launcher desktopowy (desktop.py) mógł
    wykonać ten sam krok bez kopiowania kodu. Zwraca liczbę prób.
    """
    if not konf.gemini_api_key:
        print("Brak GEMINI_API_KEY — kolejka 'do przetworzenia' czeka.")
        return 0
    klient = genai.Client(api_key=konf.gemini_api_key)

    con = baza.polacz(konf.sciezka_bazy)
    try:
        n = weryfikator.przetworz_kolejke(klient, con, konf.katalog_danych)
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
    # .env tylko tutaj (punkt wejścia) — import modułu niczego nie wczytuje:
    load_dotenv()
    konf = konfiguracja.z_srodowiska()
    try:
        app = utworz_app(konf)
    except ValueError as e:
        raise SystemExit(str(e))
    stan = app.extensions["omega"]
    # batch startowy pod tym samym zamkiem co wątek wgrywania — nowy
    # upload w trakcie startu nie przetworzy niczego dwa razy:
    with stan.zamek_batcha:
        stan.przetwarzanie.set()
        try:
            przygotuj_dane(konf)
        finally:
            stan.przetwarzanie.clear()
    url = f"http://{konf.host}:{konf.port}"
    if konf.bez_przegladarki:
        # kontener/VPS: przeglądarki tam nie ma, URL otwiera użytkownik
        # (albo nakładka desktopowa na swoim komputerze):
        print(f"Panel: {url}")
    else:
        print(f"Panel: {url}  (Ctrl+C kończy)")
        webbrowser.open(url)
    app.run(host=konf.host, port=konf.port)


if __name__ == "__main__":
    main()
