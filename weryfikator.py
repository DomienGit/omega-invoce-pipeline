"""Weryfikator faktur — odczyt danych z PDF (Gemini) + walidacja + raport.

Pipeline dla każdego pliku data/faktura_*.pdf:
  1. RENDEROWANIE  — pierwsza strona PDF zamieniana na obraz PNG (pymupdf),
     bo Gemini czyta obrazy, a nie "surowy" PDF.
  2. EKSTRAKCJA    — Gemini analizuje obraz i zwraca dane jako JSON
     (numer, NIP sprzedawcy, daty, kwoty). Model sam radzi sobie
     z obróconymi dokumentami i słabą jakością skanu.
  3. WALIDACJA     — reguły poprawności danych:
       a) NIP sprzedawcy jest obecny na fakturze,
       b) NIP ma dokładnie 10 cyfr,
       c) netto + VAT == brutto (tolerancja 0.01 zł na zaokrąglenia),
       d) termin płatności nie jest wcześniejszy niż data wystawienia.
  4. RAPORT        — wiersz na fakturę + podsumowanie + JSON z wynikami.

Uruchomienie:  python weryfikator.py
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

MODEL = "gemini-3.5-flash"  # starsza generacja = mniejsze obciążenie serwerów
DPI = 150                   # rozdzielczość renderowania PDF -> PNG
TOLERANCJA_KWOT = 0.01      # zł — grosze mogą się różnić zaokrągleniem
PRZERWA = 4                 # s między zapytaniami — free tier ma limit RPM
MAX_PROB = 5                # próby przy chwilowych problemach API (429/500/503)

PROMPT = """Przeanalizuj obraz faktury VAT i zwróć z niej dane.

Zasady:
- nip_sprzedawcy: NIP podany przy SPRZEDAWCY (nie przy nabywcy!),
  same cyfry bez spacji i myślników. null, jeśli sprzedawca nie ma NIP-u.
- Daty w formacie RRRR-MM-DD.
- Kwoty jako liczby w złotych, bez symbolu waluty.
- Dokument może być obrócony o 90/270 stopni albo słabej jakości —
  mimo to odczytaj dane dokładnie.

Zwróć wyłącznie JSON zgodny ze schematem."""

# Ustrukturyzowana odpowiedź — Gemini zwraca dokładnie takie pole, dzięki
# temu nie trzeba parsować dowolnego tekstu odpisy modelu.
SCHEMA = {
    "type": "object",
    "properties": {
        "numer": {"type": "string"},
        "nip_sprzedawcy": {"type": "string", "nullable": True},
        "data_wystawienia": {"type": "string"},
        "termin_platnosci": {"type": "string"},
        "netto": {"type": "number"},
        "vat": {"type": "number"},
        "brutto": {"type": "number"},
    },
    "required": ["numer", "nip_sprzedawcy", "data_wystawienia",
                 "termin_platnosci", "netto", "vat", "brutto"],
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

    # --- a) i b) NIP sprzedawcy: obecność i długość -------------------------
    nip = (f.get("nip_sprzedawcy") or "").replace(" ", "").replace("-", "")
    if not nip:
        bledy.append("brak NIP sprzedawcy")
    elif len(nip) != 10 or not nip.isdigit():
        bledy.append(f"nieprawidłowa długość NIP sprzedawcy: '{nip}'")

    # --- c) netto + VAT == brutto -------------------------------------------
    netto, vat, brutto = f.get("netto"), f.get("vat"), f.get("brutto")
    if None in (netto, vat, brutto):
        bledy.append("nie udało się odczytać wszystkich kwot")
    elif abs(netto + vat - brutto) > TOLERANCJA_KWOT:
        bledy.append(f"kwoty niezgodne: netto {netto:.2f} + VAT {vat:.2f} "
                     f"!= brutto {brutto:.2f}")

    # --- d) termin płatności >= data wystawienia ----------------------------
    try:
        wystawiona = date.fromisoformat(f.get("data_wystawienia") or "")
        termin = date.fromisoformat(f.get("termin_platnosci") or "")
        if termin < wystawiona:
            bledy.append(f"termin płatności ({termin}) przed datą wystawienia ({wystawiona})")
    except ValueError:
        bledy.append("nie udało się odczytać dat w formacie RRRR-MM-DD")

    return bledy


def main():
    load_dotenv()
    klucz = os.environ.get("GEMINI_API_KEY")
    if not klucz:
        sys.exit("Błąd: brak GEMINI_API_KEY w pliku .env")

    klient = genai.Client(api_key=klucz)

    pliki = sorted(p for p in os.listdir("data") if p.endswith(".pdf"))
    if not pliki:
        sys.exit("Błąd: brak plików PDF w katalogu data/")

    print(f"Weryfikacja {len(pliki)} faktur (model: {MODEL})\n")

    wyniki = []
    for nazwa in pliki:
        print(f"{nazwa}: odczyt...", flush=True)
        png = renderuj_pdf_do_png(os.path.join("data", nazwa))
        dane = wyodrebnij_dane(klient, png)
        bledy = waliduj(dane)

        status = "OK" if not bledy else "BŁĄD: " + "; ".join(bledy)
        print(f"  {dane.get('numer', '?'):20} -> {status}")
        wyniki.append({"plik": nazwa, **dane, "bledy": bledy})
        time.sleep(PRZERWA)

    # --- raport końcowy -------------------------------------------------------
    poprawne = [w for w in wyniki if not w["bledy"]]
    zle = [w for w in wyniki if w["bledy"]]

    print("\n=== PODSUMOWANIE ===")
    print(f"Poprawne:    {len(poprawne)}/{len(wyniki)}")
    print(f"Niepoprawne: {len(zle)}/{len(wyniki)}")
    for w in zle:
        print(f"  {w['plik']}: {'; '.join(w['bledy'])}")

    sciezka_wyniki = os.path.join("data", "wyniki_weryfikacji.json")
    with open(sciezka_wyniki, "w", encoding="utf-8") as plik:
        json.dump(wyniki, plik, ensure_ascii=False, indent=2)
    print(f"\nSzczegółowe wyniki: {sciezka_wyniki}")


if __name__ == "__main__":
    main()
