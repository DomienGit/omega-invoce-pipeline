"""Weryfikator faktur — odczyt danych z PDF (Gemini) + walidacja + raport.

Kolejką przetwarzania jest BAZA: panel po wgraniu PDF-ów tworzy wiersze
o statusie 'do_przetworzenia' (moduł baza.py), a weryfikator bierze je
po kolei i dla każdego pliku data/<plik>:
  1. RENDEROWANIE  — pierwsza strona PDF zamieniana na obraz PNG (pymupdf),
     bo Gemini czyta obrazy, a nie "surowy" PDF.
  2. EKSTRAKCJA    — Gemini analizuje obraz i zwraca dane jako JSON
     (numer, NIP sprzedawcy, daty, kwoty). Model sam radzi sobie
     z obróconymi dokumentami i słabą jakością skanu.
  3. WALIDACJA     — reguły poprawności danych:
       a) NIP sprzedawcy jest obecny na fakturze,
       b) NIP składa się z dokładnie 10 cyfr,
       c) netto + VAT == brutto (tolerancja 0.01 zł na zaokrąglenia),
       d) suma wartości netto pozycji == "Razem netto" faktury,
       e) termin płatności nie jest wcześniejszy niż data wystawienia.
       Dodatkowo między fakturami: żaden numer faktury nie powtarza się.
  4. ZAPIS         — wiersz faktury aktualizowany W MIEJSCU (status
     do_zatwierdzenia / do_weryfikacji). Nieudana ekstrakcja (zepsuty
     PDF, limity API) zostawia wiersz w 'do_przetworzenia' z opisem
     błędu — dobierze go kolejny przebieg.
  5. RAPORT        — wiersz na fakturę + podsumowanie (w tym suma netto),
     czytane z bazy po przetworzeniu kolejki.
  6. PORÓWNANIE    — jeśli istnieje data/ground_truth.json: trafność
     wyekstrahowanych danych w % (pole po polu) + zapis raportu do
     data/raport_dokladnosci.json.

Chwilowe błędy API (429/500/503) są ponawiane w trakcie próby (MAX_PROB
× 30 s). Przerwany batch wznawia się sam: przy następnym uruchomieniu
(panel, weryfikator, serwis cykl) lecą wszystkie faktury o statusie
'do_przetworzenia' — nieprzetworzone widać też w panelu z przyciskiem
"Przetwórz ponownie".

Uruchomienie:  python weryfikator.py   (przetwarza kolejkę z bazy)
Zależności:    google-genai, pymupdf, python-dotenv
Klucz API:     GEMINI_API_KEY w pliku .env
"""

import json
import os
import sys
import time
from datetime import date

import pymupdf
from dotenv import load_dotenv
from google import genai
from google.genai import types

import baza

MODEL = "gemini-3.5-flash-lite"
DPI = 150                   # rozdzielczość renderowania PDF -> PNG
TOLERANCJA_KWOT = 0.01      # zł — grosze mogą się różnić zaokrągleniem
PRZERWA = 4                 # s między zapytaniami — free tier ma limit RPM
MAX_PROB = 5                # próby przy chwilowych problemach API (429/500/503)
WAGI_NIP = (6, 5, 7, 2, 3, 4, 5, 6, 7)  # wagi sumy kontrolnej NIP (9 cyfr)

PROMPT = """Przeanalizuj obraz faktury VAT i zwróć z niej dane.

Zasady:
- nazwa_sprzedawcy / nazwa_nabywcy: pełne nazwy sprzedawcy i nabywcy.
- nip_sprzedawcy: NIP podany przy SPRZEDAWCY (nie przy nabywcy!),
  same cyfry bez spacji i myślników. null, jeśli sprzedawca nie ma NIP-u.
- nip_nabywcy: NIP podany przy NABYWCY, same cyfry. null, jeśli brak.
- Daty w formacie RRRR-MM-DD.
- Kwoty jako liczby w złotych, bez symbolu waluty.
- pozycje: kolejne wiersze tabeli na fakturze jako obiekty
  {nazwa, ilosc, cena_netto, wartosc_netto}, w tej samej kolejności.
- Dokument może być obrócony o 90/270 stopni albo słabej jakości —
  mimo to odczytaj dane dokładnie.

Zwróć wyłącznie JSON zgodny ze schematem."""

# Ustrukturyzowana odpowiedź — Gemini zwraca dokładnie takie pole, dzięki
# temu nie trzeba parsować dowolnego tekstu odpisy modelu.
SCHEMA = {
    "type": "object",
    "properties": {
        "numer": {"type": "string"},
        "nazwa_sprzedawcy": {"type": "string", "nullable": True},
        "nip_sprzedawcy": {"type": "string", "nullable": True},
        "nazwa_nabywcy": {"type": "string", "nullable": True},
        "nip_nabywcy": {"type": "string", "nullable": True},
        "data_wystawienia": {"type": "string"},
        "termin_platnosci": {"type": "string"},
        "netto": {"type": "number"},
        "vat": {"type": "number"},
        "brutto": {"type": "number"},
        "pozycje": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nazwa": {"type": "string"},
                    "ilosc": {"type": "number"},
                    "cena_netto": {"type": "number"},
                    "wartosc_netto": {"type": "number"},
                },
                "required": ["nazwa", "ilosc", "cena_netto", "wartosc_netto"],
            },
        },
    },
    "required": ["numer", "nazwa_sprzedawcy", "nip_sprzedawcy",
                 "nazwa_nabywcy", "nip_nabywcy", "data_wystawienia",
                 "termin_platnosci", "netto", "vat", "brutto", "pozycje"],
}


def renderuj_pdf_do_png(sciezka: str) -> bytes:
    """Zamienia pierwszą stronę PDF na bajty PNG."""
    doc = pymupdf.open(sciezka)
    try:
        pix = doc[0].get_pixmap(dpi=DPI)
        return pix.tobytes("png")
    finally:
        doc.close()


def wyodrebnij_dane(klient: genai.Client, png: bytes) -> dict:
    """Wysyła obraz do Geminia i zwraca sparsowany JSON z danymi faktury."""
    for probe in range(1, MAX_PROB + 1):
        try:
            odp = klient.models.generate_content(
                model=MODEL,
                contents=[
                    PROMPT,
                    types.Part.from_bytes(data=png, mime_type="image/png"),
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=SCHEMA,
                    temperature=0,  # odczyt ma być deterministyczny, nie "kreatywny"
                ),
            )
            return json.loads(odp.text)
        except Exception as e:
            # Błędy chwilowe, które mogą same przejść po odczekaniu:
            # 429 = limit zapytań/min (free tier), 503/500 = przeciążenie serwera
            chwilowy = any(kod in str(e) for kod in ("429", "500", "503"))
            if chwilowy and probe < MAX_PROB:
                print(f"  ... chwilowy problem API, czekam 30 s "
                      f"(próba {probe}/{MAX_PROB})", flush=True)
                time.sleep(30)
            else:
                raise
    return {}  # nieosiągalne, ale pylint nie protestuje


def waliduj(f: dict) -> list[str]:
    """Zwraca listę błędów faktury. Pusta lista = faktura poprawna."""
    bledy = []

    # --- a) i b) NIP sprzedawcy: obecność, długość, suma kontrolna ----------
    nip = baza.normalizuj_nip(f.get("nip_sprzedawcy"))
    if not nip:
        bledy.append("brak NIP sprzedawcy")
    elif len(nip) != 10 or not nip.isdigit():
        bledy.append(f"NIP sprzedawcy nie składa się z 10 cyfr: '{nip}'")
    elif sum(int(c) * w for c, w in zip(nip, WAGI_NIP)) % 11 != int(nip[9]):
        bledy.append(f"NIP sprzedawcy nie przechodzi sumy kontrolnej: '{nip}'")

    # --- c) netto + VAT == brutto -------------------------------------------
    netto, vat, brutto = f.get("netto"), f.get("vat"), f.get("brutto")
    if None in (netto, vat, brutto):
        bledy.append("nie udało się odczytać wszystkich kwot")
    elif abs(netto + vat - brutto) > TOLERANCJA_KWOT:
        bledy.append(f"kwoty niezgodne: netto {netto:.2f} + VAT {vat:.2f} "
                     f"!= brutto {brutto:.2f}")

    # --- d) suma wartości netto pozycji == "Razem netto" --------------------
    pozycje = f.get("pozycje") or []
    if netto is None:
        pass  # brak netto już zgłoszony w punkcie c)
    elif not pozycje:
        bledy.append("nie udało się odczytać pozycji faktury")
    elif not all(isinstance(p.get("wartosc_netto"), (int, float))
                 for p in pozycje):
        bledy.append("nie udało się odczytać wartości wszystkich pozycji")
    else:
        suma = sum(p["wartosc_netto"] for p in pozycje)
        if abs(suma - netto) > TOLERANCJA_KWOT:
            bledy.append(f"suma pozycji netto ({suma:.2f}) "
                         f"!= razem netto ({netto:.2f})")

    # --- e) termin płatności >= data wystawienia ----------------------------
    try:
        wystawiona = date.fromisoformat(f.get("data_wystawienia") or "")
        termin = date.fromisoformat(f.get("termin_platnosci") or "")
        if termin < wystawiona:
            bledy.append(f"termin płatności ({termin}) przed datą wystawienia ({wystawiona})")
    except ValueError:
        bledy.append("nie udało się odczytać dat w formacie RRRR-MM-DD")

    return bledy


def porownaj_z_ground_truth(wyniki: list[dict]) -> dict | None:
    """Porównuje dane z bazy z data/ground_truth.json.

    wyniki = lista z baza.pobierz_faktury() (klucze jak kolumny tabeli).
    Zwraca {"trafnosc_danych_procent": ..., "trafnosc_klasyfikacji_procent": ...}
    lub None, gdy plik ground truth nie istnieje.
    """
    sciezka_gt = os.path.join("data", "ground_truth.json")
    if not os.path.exists(sciezka_gt):
        return None
    with open(sciezka_gt, encoding="utf-8") as plik:
        gt = {w["plik"]: w for w in json.load(plik)}

    pola_tekstowe = ["numer", "nip_sprzedawcy",
                     "data_wystawienia", "termin_platnosci"]
    pola_kwotowe = ["netto", "vat", "brutto"]

    trafienia, porownania, zgodne = 0, 0, 0
    for w in wyniki:
        oczekiwane = gt.get(w["plik"])
        if oczekiwane is None:
            continue  # plik nie występuje w ground truth — pomijamy

        lokalne = 0
        for pole in pola_tekstowe:
            nasz = (w.get(pole) or "").replace(" ", "").replace("-", "")
            prawda = (oczekiwane.get(pole) or "").replace(" ", "").replace("-", "")
            porownania += 1
            lokalne += nasz == prawda
        for pole in pola_kwotowe:
            porownania += 1
            lokalne += (w.get(pole) is not None and oczekiwane.get(pole) is not None
                        and abs(w[pole] - oczekiwane[pole]) <= TOLERANCJA_KWOT)
        trafienia += lokalne

        n_porownan = len(pola_tekstowe) + len(pola_kwotowe)

        # klasyfikacja: czy walidacja zgadza się z oczekiwaną (błąd vs ok)
        spodziewany_blad = oczekiwane.get("oczekiwana_walidacja") != "ok"
        zgodne += bool(w["bledy"]) == spodziewany_blad

    n_faktur = len([w for w in wyniki if w["plik"] in gt])
    return {
        "trafnosc_danych_procent": round(100 * trafienia / porownania, 1),
        "trafnosc_klasyfikacji_procent": round(100 * zgodne / n_faktur, 1),
    }


def przetworz_kolejke(klient, con) -> int:
    """Przetwarza faktury o statusie 'do_przetworzenia'.

    Współdzielone z panelem (batch w tle po wgraniu plików). Zwraca
    liczbę podjętych prób — wynik każdego pliku ląduje w bazie od razu,
    więc przerwany przebieg można wznowić (status 'do_przetworzenia'
    zostaje tylko przy nieudanej próbie).
    """
    kolejka = baza.pobierz_kolejke(con)
    for f in kolejka:
        print(f"{f['plik']}: odczyt...", flush=True)
        try:
            png = renderuj_pdf_do_png(os.path.join("data", f["plik"]))
            dane = wyodrebnij_dane(klient, png)
            bledy = waliduj(dane)
        except Exception as e:
            # zepsuty PDF albo API po wyczerpaniu prób — jeden plik nie
            # może wywalić batcha; wiersz zostaje w 'do_przetworzenia'
            # i dobierze go kolejny przebieg:
            opis = f"błąd przetwarzania: {e}"
            baza.oznacz_blad_przetworzenia(con, f["id"], opis)
            print(f"  BŁĄD: {e}")
        else:
            baza.zapisz_wynik_ekstrakcji(con, f["id"], dane, bledy)
            status = "OK" if not bledy else "BŁĄD: " + "; ".join(bledy)
            print(f"  {dane.get('numer', '?'):20} -> {status}")
            time.sleep(PRZERWA)  # przerywa tylko po wywołaniu API

    if kolejka:
        # ten sam numer w dwóch aktywnych fakturach — baza przelicza
        # błędy duplikatów od zera:
        baza.oznacz_duplikaty(con)
    return len(kolejka)


def main():
    load_dotenv()
    klucz = os.environ.get("GEMINI_API_KEY")
    if not klucz:
        sys.exit("Błąd: brak GEMINI_API_KEY w pliku .env")

    klient = genai.Client(api_key=klucz)
    con = baza.polacz()

    kolejka = baza.pobierz_kolejke(con)
    if not kolejka:
        print("Kolejka pusta — brak faktur do przetworzenia.\n")
    else:
        print(f"Weryfikacja {len(kolejka)} faktur (model: {MODEL})\n")
        przetworz_kolejke(klient, con)

    # --- raporty czytamy z bazy (pełny stan, także historia) ------------------
    # Wierszy czekających dalej w 'do_przetworzenia' (nieudane próby)
    # nie liczymy do podsumowania ani trafności — ich dane nie istnieją:
    wszystkie = baza.pobierz_faktury(con)
    nieprzetworzone = [w for w in wszystkie if w["status"] == "do_przetworzenia"]
    wyniki = [w for w in wszystkie if w["status"] != "do_przetworzenia"]

    # --- porównanie z ground truth ---------------------------------------------
    raport = porownaj_z_ground_truth(wyniki)
    if raport:
        sciezka_raport = os.path.join("data", "raport_dokladnosci.json")
        with open(sciezka_raport, "w", encoding="utf-8") as plik:
            json.dump(raport, plik, ensure_ascii=False, indent=2)
        print(f"Trafność danych (vs ground truth): {raport['trafnosc_danych_procent']}%")
        print(f"Trafność klasyfikacji walidacji:    {raport['trafnosc_klasyfikacji_procent']}%")
        print(f"Raport dokładności: {sciezka_raport}")

    # --- raport końcowy -------------------------------------------------------
    poprawne = [w for w in wyniki if not w["bledy"]]
    zle = [w for w in wyniki if w["bledy"]]
    suma_netto = sum(w.get("netto") or 0 for w in wyniki)

    print("\n=== PODSUMOWANIE ===")
    print(f"Poprawne:    {len(poprawne)}/{len(wyniki)}")
    print(f"Niepoprawne: {len(zle)}/{len(wyniki)}")
    if nieprzetworzone:
        print(f"Do przetworzenia (ponowienie przy kolejnym przebiegu): "
              f"{len(nieprzetworzone)}")
        for w in nieprzetworzone:
            print(f"  {w['plik']}: {w.get('blad_przetworzenia') or 'czeka'}")
    for w in zle:
        print(f"  {w['plik']}: {'; '.join(w['bledy'])}")
    print(f"Suma netto wszystkich faktur: {suma_netto:.2f} zł")
    print(f"Baza faktur: {baza.SCIEZKA_BAZY}")

    baza.zamknij(con)


if __name__ == "__main__":
    main()
