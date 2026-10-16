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

