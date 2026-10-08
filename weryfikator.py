"""Weryfikator faktur — odczyt danych z PDF (Gemini) + walidacja + raport.

Pipeline dla każdego pliku data/faktura_*.pdf:
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
  4. RAPORT        — wiersz na fakturę + podsumowanie (w tym suma netto)
     + JSON z wynikami.
  5. PORÓWNANIE    — jeśli istnieje data/ground_truth.json: trafność
     wyekstrahowanych danych w % (pole po polu) + zapis raportu do
     data/raport_dokladnosci.json.

Wznawianie: wyniki zapisywane są po KAŻDEJ fakturze do
data/wyniki_weryfikacji.json. Przy ponownym uruchomieniu faktury już
obecne w tym pliku są pomijane (przydatne przy limitach API lub przerwaniu).

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

MODEL = "gemini-3.7-flash"
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
- wartosci_netto_pozycji: wartości netto ("Wartość netto") kolejnych
  pozycji z tabeli na fakturze, w tej samej kolejności.
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
        "wartosci_netto_pozycji": {
            "type": "array",
            "items": {"type": "number"},
        },
    },
    "required": ["numer", "nip_sprzedawcy", "data_wystawienia",
                 "termin_platnosci", "netto", "vat", "brutto",
                 "wartosci_netto_pozycji"],
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
        bledy.append(f"NIP sprzedawcy nie składa się z 10 cyfr: '{nip}'")

    # --- c) netto + VAT == brutto -------------------------------------------
    netto, vat, brutto = f.get("netto"), f.get("vat"), f.get("brutto")
    if None in (netto, vat, brutto):
        bledy.append("nie udało się odczytać wszystkich kwot")
    elif abs(netto + vat - brutto) > TOLERANCJA_KWOT:
        bledy.append(f"kwoty niezgodne: netto {netto:.2f} + VAT {vat:.2f} "
                     f"!= brutto {brutto:.2f}")

    # --- d) suma wartości netto pozycji == "Razem netto" --------------------
    pozycje = f.get("wartosci_netto_pozycji") or []
    if netto is None:
        pass  # brak netto już zgłoszony w punkcie c)
    elif not pozycje:
        bledy.append("nie udało się odczytać pozycji faktury")
    elif abs(sum(pozycje) - netto) > TOLERANCJA_KWOT:
        bledy.append(f"suma pozycji netto ({sum(pozycje):.2f}) "
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


def zapisz_wyniki(sciezka: str, wyniki: list[dict]):
    """Zapisuje wyniki do JSON (posortowane po nazwie pliku)."""
    with open(sciezka, "w", encoding="utf-8") as plik:
        json.dump(sorted(wyniki, key=lambda w: w["plik"]),
                  plik, ensure_ascii=False, indent=2)


def porownaj_z_ground_truth(wyniki: list[dict]) -> dict | None:
    """Porównuje wyekstrahowane dane z data/ground_truth.json.

    Zwraca {"trafnosc_danych_procent": ..., "trafnosc_klasyfikacji_procent": ...}
    lub None, gdy plik ground truth nie istnieje. Dodatkowo dopisuje do
    każdego wyniku pole "poprawnosc_danych_procent" (trafność tej faktury).
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
        w["poprawnosc_danych_procent"] = round(100 * lokalne / n_porownan, 1)

        # klasyfikacja: czy walidacja zgadza się z oczekiwaną (błąd vs ok)
        spodziewany_blad = oczekiwane.get("oczekiwana_walidacja") != "ok"
        zgodne += bool(w["bledy"]) == spodziewany_blad

    n_faktur = len([w for w in wyniki if w["plik"] in gt])
    return {
        "trafnosc_danych_procent": round(100 * trafienia / porownania, 1),
        "trafnosc_klasyfikacji_procent": round(100 * zgodne / n_faktur, 1),
    }


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

    # --- wznawianie: wczytaj wyniki poprzedniego przebiegu -------------------
    # Wyniki bez "wartosci_netto_pozycji" pochodzą ze starego schematu
    # ekstrakcji — przeliczamy je od nowa (reguła d wymaga pozycji tabeli).
    sciezka_wyniki = os.path.join("data", "wyniki_weryfikacji.json")
    wyniki = []
    if os.path.exists(sciezka_wyniki):
        with open(sciezka_wyniki, encoding="utf-8") as plik:
            wyniki = [w for w in json.load(plik)
                      if "wartosci_netto_pozycji" in w]
    zrobione = {w["plik"] for w in wyniki}

    for nazwa in pliki:
        if nazwa in zrobione:
            print(f"{nazwa}: już sprawdzona wcześniej, pomijam")
            continue

        print(f"{nazwa}: odczyt...", flush=True)
        png = renderuj_pdf_do_png(os.path.join("data", nazwa))
        dane = wyodrebnij_dane(klient, png)
        bledy = waliduj(dane)

        status = "OK" if not bledy else "BŁĄD: " + "; ".join(bledy)
        print(f"  {dane.get('numer', '?'):20} -> {status}")
        wyniki.append({"plik": nazwa, **dane, "bledy": bledy})
        zapisz_wyniki(sciezka_wyniki, wyniki)  # zapis po każdej fakturze
        time.sleep(PRZERWA)

    # --- duplikaty: ten sam numer faktury w dwóch plikach --------------------
    widziane = {}
    for w in sorted(wyniki, key=lambda x: x["plik"]):
        numer = w.get("numer")
        if numer in widziane:
            opis = (f"duplikat numeru faktury {numer} "
                    f"(wcześniejsze wystąpienie: {widziane[numer]})")
            if opis not in w["bledy"]:  # zabezpieczenie przed powtórką przy wznowieniu
                w["bledy"].append(opis)
        else:
            widziane[numer] = w["plik"]
    zapisz_wyniki(sciezka_wyniki, wyniki)

    # --- porównanie z ground truth ---------------------------------------------
    raport = porownaj_z_ground_truth(wyniki)
    if raport:
        zapisz_wyniki(sciezka_wyniki, wyniki)  # z per-faktura trafnością w %
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
    for w in zle:
        print(f"  {w['plik']}: {'; '.join(w['bledy'])}")
    print(f"Suma netto wszystkich faktur: {suma_netto:.2f} zł")
    print(f"Szczegółowe wyniki: {sciezka_wyniki}")


if __name__ == "__main__":
    main()
