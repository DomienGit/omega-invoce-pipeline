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