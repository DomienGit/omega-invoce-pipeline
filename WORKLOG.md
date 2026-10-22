05-10-2026 {
    Zostaly stworzone 20 przykadowych faktur z wykorzyustaniem skryptu oraz skrypt sprawdzajacy poprawnosc danych na fakturze z wykorzystaniem modelu gemini api
}

14-10-2026 {
    Baza SQLite (data/faktury.db, modul baza.py):
    - tabela faktury: naglowki z ekstrakcji (numer, strony, NIP-y, daty,
      netto/vat/brutto), bledy walidacji jako JSON, status workflow
      (do_weryfikacji / do_zatwierdzenia / zatwierdzone),
      UNIQUE(numer, nip_sprzedawcy)
    - tabela pozycje: nazwa, ilosc, cena_netto, wartosc_netto + status,
      klucz obcy z ON DELETE CASCADE
    - zapis idempotentny (re-run zastepuje wpis), status 'zatwierdzone'
      chroniony przed nadpisaniem, NIP-y normalizowane do samych cyfr

    Weryfikator:
    - rozszerzony schemat ekstrakcji: nazwa_sprzedawcy, nazwa_nabywcy,
      nip_nabywcy, pozycje jako obiekty {nazwa, ilosc, cena_netto,
      wartosc_netto} -> zmiana wymuszyla jednorazowa re-ekstrakcje
      wszystkich faktur (stary format wynikow odfiltrowywany po kluczu
      "pozycje")
    - nowa walidacja: suma kontrolna NIP (wagi mod 11)

    Eksport XML (modul eksport.py):
    - generuje data/eksport.xml wg szablonu OmegaProcessor
      (Naglowek/Sprzedawca/Nabywca/Podsumowanie/Pozycje/Dekretacja)
    - domyslnie eksportuje faktury zatwierdzone; DataSprzedazy =
      DataWystawienia, StawkaVAT liczona z netto/vat, Jednostka pusta
      (brak na fakturach), Kategoria parametrem (dflt. "Inne")

    Srodowisko: venv przebudowany na systemowy Python 3.12
    (poprzedni 3.14 z /usr/local byl bez modulu sqlite3),
    requires-python obnizone do >=3.12

    Testy: warstwa bazy + eksport offline (pamiec :memory:), pelny run
    20 faktur (100% trafnosci vs ground truth, 16/4 statusy, 64 pozycje),
    re-run idempotentny, eksport 3 zatwierdzonych faktur
}

16-10-2026 {
    Panel podgladowy dla ksiegowej (statyczny HTML, bez serwera):
    - panel.py: po uruchomieniu batch nowych PDF-ow z data/ (render ->
      Gemini -> walidacja -> zapis, wznawianie jak w weryfikatorze),
      potem generacja data/panel.html i otwarcie w przegladarce.
      Zepsuty PDF / wyczerpane proby API = rekord z "blad przetwarzania"
      (zakladka Bledy, kolejny run ponawia, bo rekord nie ma "pozycje").
      Bez GEMINI_API_KEY krok batch jest pomijany (panel z bazy).
    - templates/panel.html: zakladki Wszystkie / Do zatwierdzenia / Bledy,
      tabela faktur, szczegoly = wyekstrahowane dane + pozycje OBOK
      oryginalnego PDF (embed + link fallback dla Chrome/file://).
      Panel podgladowy — bez zatwierdzania.

    Baza (baza.py):
    - kolumna plik w tabeli faktury + UNIQUE(plik); migracja w polacz():
      ALTER TABLE + backfill (numer, nip_sprzedawcy) z
      data/wyniki_weryfikacji.json (016 z NULL NIP przez IS NULL)
    - zapisz_fakture: tozsamosc = plik ALBO (numer, nip_sprzedawcy)
      (rekord bez pliku zachowuje sie jak dawniej), ochrona zatwierdzone
      i ON DELETE CASCADE bez zmian
    - helpery odczytu: pobierz_faktury (liczba_pozycji, bledy z JSON),
      pobierz_szczegoly (z pozycjami)

    Testy: migracja na kopii (20/20 backfill, statusy 3/13/4 nietkniete),
    idempotencja po pliku (re-run = replace, zatwierdzone chronione,
    zmiana numeru przy tym samym pliku = podmiana), walidacja
    osadzonego JSON w panel.html, sciezka bledu (psuty PDF przed API,
    koszt zero) + sprzatanie, regresja weryfikator (20 pominietych,
    suma netto 30389.39 bez zmian) i eksport (3 zatwierdzone).
    Forsowanie re-ekstrakcji faktury = usuniecie rekordu z JSON
    (filtr "pozycje") + wiersza z DB po pliku.
}

16-10-2026 (2) {
    Panel przepisany na aplikacje webowa Flask (docelowo nakladka
    desktopowa na web) — panel.py po starcie: batch nowych PDF-ow
    (bez zmian), potem serwer http://127.0.0.1:5000 + auto-otwarcie.
    Serwer dziala dopoki skrypt dziala (Ctrl+C konczy).

    Akcje dla ksiegowej (templates/panel.html, dane z /api/faktury):
    - zatwierdzenie faktury (tylko bez bledow walidacji; 409 gdy
      sa bledy) -> status 'zatwierdzone' + zakladka Zatwierdzone
    - reczna poprawa danych (naglowek + pozycje: edycja/dodanie/
      usuniecie wierszy) — po zapisie walidacja liczy sie od nowa;
      poprawki zapisywane takze do data/wyniki_weryfikacji.json,
      zeby re-run nie przywrocil starych wartosci
    - eksport zatwierdzonych do XML (POST /api/eksport woła
      eksport.eksportuj_xml) + pobranie pliku (GET /eksport)

    Endpointy: GET /, /api/faktury, /pdf/<plik> (tylko pliki z bazy),
    POST /api/faktury/<id>, POST /api/faktury/<id>/zatwierdz,
    POST /api/eksport, GET /eksport. baza.py: + zatwierdz_fakture()
    (status faktury i pozycji). Nowa zaleznosc: flask (uv add).

    Testy: curl/urllib wszystkich endpointow (409 przy bledach,
      poprawka 018 brutto -> walidacja czysta -> zatwierdzenie,
      eksport, PDF, blokada plikow spoza bazy) + test "na zywo"
      z przegladarki (zatwierdzenia 04/05/06/09, poprawka 009 —
      spojna miedzy baza a cache'em JSON). Status 'zatwierdzone'
      nadal chroniony przed nadpisaniem przez re-run.
}

16-10-2026 (3) {
    Eksport do folderu przetworzone/ (eksport.py):
    - eksport.xml przeniesiony z data/ do przetworzone/
    - zapisz_pdf_do_przetworzonych(): kopie oryginalnych PDF-ow
      zatwierdzonych faktur, nazwa [data wystawienia]_[numer]_
      [kontrahent].pdf, kontrahent = sprzedawca (rejestr zakupu);
      znaki niedozwolone w nazwach plikow (m.in. '/' z numeru
      faktury) zamieniane na '-', braki pol -> bez-daty/bez-numeru/
      bez-kontrahenta; CLI i przycisk w panelie robia oba (XML+PDF)

    Panel (templates/panel.html):
    - przycisk "Nastepna ->" w szczegolach faktury: przechodzi do
      kolejnej faktury biezacej zakladki; po zatwierdzeniu pamietana
      jest pozycja na liscie, wiec trafia sie na fakture, ktora
      weszla na miejsce zatwierdzonej; disabled na ostatniej

    Testy: POST /api/eksport -> 9 faktur + 9 PDF-ow, nazwy spojne
      (sanityzacja '/' -> '-'), CLI eksport.py bez zmian zachowania,
      node --check na JS szablonu, panel odswiezony na nowym kodzie.
}


20-10-2026 {
    Status "wyeksportowane" (baza.py, panel, eksport):
    - po eksporcie faktury zatwierdzone dostaja status 'wyeksportowane'
      i znikaja z panelu (api_faktury filtruje), nie cofa ich re-run
      (ochrona w zapisz_fakture jak dla 'zatwierdzone')
    - oznacz_wyeksportowane() w baza.py; woła ja panel po udanym
      eksporcie i eksport.py z CLI (przy statusie zatwierdzone)
    - CHECK statusow nie da sie zmienic w miejscu -> migracja
      _migruj_status(): przebudowa tabel (rename, nowe DDL, kopia 1:1,
      drop) przy pierwszym polacz(); test na kopii bazy przeszedl
      (10 zatwierdzonych -> wyeksportowane, re-run nie cofa statusu)
    - panel.html: po eksporcie najpierw odswiezenie listy, potem
      komunikat (renderTabela buduje pasek od nowa)

    Konteneryzacja (Dockerfile, docker-compose.yml, .dockerignore):
    - obraz python:3.12-slim (3.14 nie ma sqlite3), tzdata +
      TZ=Europe/Warsaw (lokalny czas w raportach/dataGeneracji),
      uzytkownik nie-root uid 1000 (pliki w volume nie roota)
    - zaleznosci z pyproject.toml (pip install ., warstwa cache'owana
      przed kopiowaniem kodu); w obrazie weryfikator, baza, eksport,
      panel + templates/; generator/desktop/main poza obrazem
    - compose: serwis panel (restart unless-stopped, port 5000,
      PANEL_HOST=0.0.0.0, BEZ_PRZEGLADARKI=1, env_file .env, volumes
      ./data i ./przetworzone, rotacja logow 10m x3) + serwis cykl
      (profiles, uruchamiany recznie: weryfikator + eksport
      do_zatwierdzenia) — przygotowane pod VPS
    - panel.py: HOST/PORT czytane z env (PANEL_HOST/PANEL_PORT,
      domyslnie 127.0.0.1:5000 — desktop.py bez zmian); BEZ_PRZEGLADARKI=1
      pomija webbrowser.open w kontenerze
    - README: sekcja "Jak uruchomic (Docker)" + ostrazenie o braku
      autoryzacji przy wystawieniu na publiczny IP

    Testy: py_compile wszystkich modulow, migracja + oznaczanie +
      ochrona statusu na kopii bazy, budowanie paczki z pyproject
      (uv build) pod katem pip install . w obrazie. Build i uruchomienie
      kontenera — po instalacji Dockera na hoscie (byl nieobecny).
}


21-10-2026 {
    Wyniki_weryfikacji.json usuniety — baza jest jedynym stanem:
    - odcisk_pliku (SHA-256) + blad_przetworzenia w tabeli faktury;
      resume = wiersz o tej nazwie i tym samym odcisku bez bledu
      przetwarzania. Nowa tresc pod znana nazwa -> przetwarzana,
      nieudane proby (zepsuty PDF / limity API) -> ponawiane,
      wyeksportowane pliki w data/ -> pomijane (bez petli w kolko)
    - unikalnosc (plik oraz numer+NIP) tylko wsrod aktywnych —
      czesciowe indeksy uq_aktywny_plik / uq_aktywny_numer
      (WHERE status != 'wyeksportowane'); historia moze powtarzac
      nazwy i numery; powtorka numeru faktury wyeksportowanej dostaje
      blad "duplikat faktury wczesniej wyeksportowanej"
    - migracja _migruj_schemat(): przebudowa tabel przy braku nowych
      kolumn / tabelowym UNIQUE / CHECK bez 'wyeksportowane';
      uzupelnij_odciski() backfill odciskow z plikow w data/
    - oznacz_duplikaty(con) liczy bledy duplikatow numerow z bazy
      i przelicza je od zera (zmiana numeru przy re-ekstrakcji zdejmuje
      przedawniony blad u pozostalych); weryfikator i panel wspolnie
      korzystaja z baza.zapisz_fakture/oznacz_duplikaty
    - weryfikator.py: zapis po kazdej fakturze do bazy (nie JSON),
      raport koncowy i porownanie z ground truth czytane z bazy
      (pelny stan, tez historia); panel.py: batch/upload bez JSON,
      przyjecie pliku o znanej tresci -> "juz przetworzona" bez API
    - testy: migracja na kopii, podmiana aktywnej / ochrona
      zatwierdzonych / duplikat wyeksportowanej / blad przetwarzania
      ponawiany; migracja zywej bazy (backup w /tmp), weryfikator
      pominal 20/20 bez wywolan API, panel: API 2 aktywne, upload
      faktura_01.pdf -> pominieta, batch nie startuje
}


22-10-2026 {
    Kolejka przetwarzania = statusy w bazie (koniec skanowania data/):
    - wgranie PDF-a w panelu tworzy od razu wiersz 'do_przetworzenia'
      (dodaj_do_kolejki: SHA-256 tresci -> "juz w kolejce" / "juz
      przetworzona" / kolizja nazwy z zatwierdzona; aktywny wiersz
      o tej nazwie z inna trescia = podmiana pliku), plik laduje
      w data/ jako magazyn — zaden skrypt nie szuka tam nowych plikow
    - weryfikator.przetworz_kolejke(klient, con) wspolna funkcja dla
      CLI i panelu: pobierz_kolejke -> ekstrakcja -> zapis_wynik_
      ekstrakcji (UPDATE w miejscu, statusy do_zatwierdzenia /
      do_weryfikacji); nieudana proba = blad_przetworzenia, wiersz
      zostaje w kolejce i jest ponawiany przy kolejnym przebiegu
      (start panelu, weryfikator, serwis cykl) albo recznie
    - panel: batch w tle po wgraniu (threading.Event + Lock, petla
      az do opuszczenia kolejki — pliki wgrane w trakcie tez zlapane),
      POST /api/przetwarzaj (409 pusta kolejka / 503 brak klucza),
      zakladka "Do przetworzenia" + przyciski "Przetworz wszystkie" /
      "Przetworz ponownie" (w podgladzie), pasek "Przetwarzam..."
      + auto-odswiezanie co 4 s w trakcie batcha
    - baza: uq_aktywny_numer SKASOWANY — dwa wiersze o tym samym
      numerze maja wspolistniec widocznie z bledem "duplikat numeru
      faktury" (oznacz_duplikaty), przy update w miejscu indeks
      nie pozwolilby wpisac drugiej faktury o tym samym numerze
    - migracja _migruj_schemat() odtwarza wpisy sqlite_sequence
      z wartosci sprzed przebudowy: DROP TABLE kasowal sekwencje,
      kolejny INSERT dostawal ponownie uzyte id, a osierocone
      pozycje (po rekach z CLI bez PRAGMA foreign_keys) przylgaly
      do swiezej faktury jako dziedziczone pozycje (wykryte na
      zywej bazie; test regresyjny: id nad max-ever, zero sierot)

    Testy: py_compile, migracja na kopii (7 scenariuszy kolejki),
    panel na zywej bazie z pustym GEMINI_API_KEY (upload -> wiersz
    w kolejce, powtorka -> "juz w kolejce", nie-PDF pominiety,
    /api/przetwarzaj -> 503, /pdf tylko z bazy), weryfikator na
    zywej bazie (kolejka pusta, raport GT odtworzony), sprzatanie
    smieci testowych + osieroconych pozycji. Backup zywej bazy
    przed migracja: /tmp/faktury_backup_przed_migracja_kolejka.db

    UWAGA: kontener panel (localhost:5000) dalej dziala na starym
    obrazie — po tych zmianach wymagany `docker compose build panel`.
}
