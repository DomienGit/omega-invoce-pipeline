"""Generator danych testowych — 20 fikcyjnych faktur PDF + ground truth.

Rozkład: 12 poprawnych + 8 z celowymi defektami:
  - defekty ODCZYTU (dane poprawne, dokument trudny do odczytania):
      "obrocony"           - obrócony o 90/270 stopni (jak zdjęcie z telefonu)
      "skan_slaba_jakosc"  - szum, rozmycie, lekkie przekrzywienie, artefakty JPEG
  - defekty DANYCH (dokument czytelny, ale dane na nim są złe):
      "brak_nip_sprzedawcy" - faktura bez NIP-u sprzedawcy
      "kwoty_niezgodne"     - netto + VAT != brutto na fakturze
      "termin_przed_wystawieniem" - termin płatności wcześniejszy niż data wystawienia

Zasada ground truth: zapisujemy to, co jest WYDRUKOWANE na dokumencie
(to jest cel ekstrakcji), plus defekt i oczekiwany wynik walidacji.

Uruchomienie:  python generator.py
Wynik:         data/faktura_01..20.pdf, data/ground_truth.json
Zależności:    reportlab, pymupdf, pillow
"""

import io
import json
import os
import random
from datetime import date, timedelta

import pymupdf  # renderowanie strony PDF do obrazu
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

# --- czcionka z polskimi znakami (Helvetica je nie ma) ----------------------
# Jeśli ścieżka nie istnieje: sudo apt install fonts-dejavu-core
pdfmetrics.registerFont(TTFont("DejaVu", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
pdfmetrics.registerFont(TTFont("DejaVu-Bold", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))

# To samo "losowanie" przy każdym uruchomieniu — PDF-y i ground truth zawsze
# będą do siebie pasować, także po ponownym wygenerowaniu.
random.seed(42)

WAGI_NIP = [6, 5, 7, 2, 3, 4, 5, 6, 7]


def wygeneruj_nip() -> str:
    """10-cyfrowy NIP przechodzący oficjalną sumę kontrolną."""
    while True:
        cyfry = [random.randint(0, 9) for _ in range(9)]
        suma = sum(w * c for w, c in zip(WAGI_NIP, cyfry))
        kontrolna = suma % 11
        if kontrolna != 10:  # 10 = z definicji niepoprawny, losujemy dalej
            return "".join(map(str, cyfry)) + str(kontrolna)


# --- pule danych (wszystko fikcyjne) ----------------------------------------
SPRZEDAWCY = [
    "Hurtownia Mąki Krakowskiej sp. z o.o.",
    "Opon-Pol Hurtownia Opon sp. z o.o.",
    "Chem-Bud Materiały Budowlane sp. z o.o.",
    "Megabajt IT Solutions sp. z o.o.",
    "Fresh Food Distribution sp. z o.o.",
    "Pol-Serwis Autoryzowany Serwis sp. z o.o.",
    "Papier i Biuro Hurtownia sp. z o.o.",
    "Trans-Logistyka Polska sp. z o.o.",
    "Green Energy OZE sp. z o.o.",
    "Stalmax Konstrukcje Stalowe sp. z o.o.",
]

NABYWCY = [
    "Piekarnia Słodka Bułka",
    "Warsztat Samochodowy u Zbyszka",
    "Kancelaria Radcy Prawnego Kowalski",
    "Salon Fryzjerski Blondynka",
    "Firma Budowlana Dom-Styl",
    "Pizza Napoletana sp. z o.o.",
    "Gabinet Stomatologiczny Biały Ząb",
    "Sklep Spożywczy Pod Lipą",
    "Autokomis Auto-Centrum",
    "Pracownia Graficzna Pixel",
]

# (nazwa, cena netto za sztukę) — celowo różne kategorie kosztów,
# przydadzą się później przy dekretacji
POZYCJE = [
    ("Mąka pszenna typ 500, 25 kg", 89.99),
    ("Drożdże piekarskie świeże, 1 kg", 12.50),
    ("Opona zimowa 205/55 R16", 320.00),
    ("Diesel B7, 1 l", 5.89),
    ("Zestaw kluczy nasadowych 1/2 cala", 189.00),
    ("Papier ksero A4, 500 arkuszy", 24.90),
    ("Toner do drukarki laserowej", 149.00),
    ("Kawa ziarnista 1 kg", 68.00),
    ("Abonament Microsoft 365, 1 mies.", 23.80),
    ("Usługa sprzątania pomieszczeń, 1 godz.", 45.00),
    ("Energia elektryczna, 1 kWh", 0.95),
    ("Cegła ceramiczna poryzowana, szt.", 3.20),
    ("Zestaw pędzli malarskich", 35.00),
    ("Mrożonki warzywne mix, 1 kg", 7.30),
    ("Ser mozzarella tarta, 1 kg", 22.10),
]

# Przydział defektów wg numeru faktury (jawny, nie losowy — łatwiej sterować)
DEFECTY = {
    4: "obrocony",
    9: "obrocony",
    6: "skan_slaba_jakosc",
    13: "skan_slaba_jakosc",
    16: "brak_nip_sprzedawcy",
    18: "kwoty_niezgodne",
    20: "kwoty_niezgodne",
    11: "termin_przed_wystawieniem",
}

OCZEKIWANA_WALIDACJA = {
    None: "ok",
    "obrocony": "ok",                      # trudny odczyt, ale dane poprawne
    "skan_slaba_jakosc": "ok",             # jw.
    "brak_nip_sprzedawcy": "blad: brak NIP sprzedawcy",
    "kwoty_niezgodne": "blad: netto + VAT != brutto",
    "termin_przed_wystawieniem": "blad: termin platnosci przed data wystawienia",
}


def policz_kwoty(pozycje):
    """Netto, VAT 23% i brutto liczone z pozycji."""
    netto = round(sum(ilosc * cena for _, ilosc, cena in pozycje), 2)
    vat = round(netto * 0.23, 2)
    return netto, vat, round(netto + vat, 2)


def zbuduj_fakture(nr: int) -> dict:
    """Składa jedną fakturę z pul danych."""
    pozycje = random.sample(POZYCJE, k=random.randint(2, 4))
    pozycje = [(nazwa, random.randint(1, 15), cena) for nazwa, cena in pozycje]
    dzien = random.randint(1, 28)
    wystawiona = date(2026, 10, dzien)
    return {
        "numer": f"FV/2026/10/{nr:03d}",
        "sprzedawca": random.choice(SPRZEDAWCY),
        "nip_sprzedawcy": wygeneruj_nip(),
        "nabywca": random.choice(NABYWCY),
        "nip_nabywcy": wygeneruj_nip(),
        "data_wystawienia": wystawiona.isoformat(),
        "termin_platnosci": (wystawiona + timedelta(days=random.choice([7, 14, 21]))).isoformat(),
        "pozycje": pozycje,
    }


def zapisz_fakture(f: dict, sciezka: str, pomin_nip_sprzedawcy=False, brutto_delta=0.0):
    """Rysuje fakturę. brutto_delta celowo psuje kwotę 'Do zapłaty'."""
    os.makedirs(os.path.dirname(sciezka), exist_ok=True)
    netto, vat, brutto = policz_kwoty(f["pozycje"])
    do_zaplaty = round(brutto + brutto_delta, 2)

    c = canvas.Canvas(sciezka, pagesize=A4)
    x = 20 * mm
    y = 272 * mm

    c.setFont("DejaVu-Bold", 14)
    c.drawString(x, y, f"Faktura VAT nr {f['numer']}")
    y -= 12 * mm

    c.setFont("DejaVu", 9)
    c.drawString(x, y, f"Sprzedawca: {f['sprzedawca']}")
    y -= 5 * mm
    if not pomin_nip_sprzedawcy:
        c.drawString(x, y, f"NIP: {f['nip_sprzedawcy']}")
        y -= 9 * mm
    else:
        y -= 9 * mm
    c.drawString(x, y, f"Nabywca: {f['nabywca']}")
    y -= 5 * mm
    c.drawString(x, y, f"NIP: {f['nip_nabywcy']}")
    y -= 9 * mm

    c.drawString(x, y, f"Data wystawienia: {f['data_wystawienia']}")
    y -= 5 * mm
    c.drawString(x, y, f"Termin płatności: {f['termin_platnosci']}")
    y -= 14 * mm

    kolumny = [0, 15, 95, 120, 145]
    c.setFont("DejaVu-Bold", 9)
    for tytul, dx in zip(["Lp.", "Nazwa", "Ilość", "Cena netto", "Wartość netto"], kolumny):
        c.drawString(x + dx * mm, y, tytul)
    y -= 7 * mm

    c.setFont("DejaVu", 9)
    for lp, (nazwa, ilosc, cena) in enumerate(f["pozycje"], start=1):
        wartosc = ilosc * cena
        komorki = [str(lp), nazwa, str(ilosc), f"{cena:.2f}", f"{wartosc:.2f}"]
        for tekst, dx in zip(komorki, kolumny):
            c.drawString(x + dx * mm, y, tekst)
        y -= 7 * mm

    y -= 8 * mm
    c.setFont("DejaVu-Bold", 10)
    c.drawString(x, y, f"Razem netto: {netto:.2f} zł")
    y -= 6 * mm
    c.drawString(x, y, f"VAT 23%: {vat:.2f} zł")
    y -= 6 * mm
    c.setFont("DejaVu-Bold", 12)
    c.drawString(x, y, f"Do zapłaty: {do_zaplaty:.2f} zł")

    c.save()


# --- psucie dokumentów (defekty odczytu) -------------------------------------

def renderuj_do_obrazu(sciezka: str, dpi: int) -> Image.Image:
    """Zamienia pierwszą stronę PDF na obraz PIL (dalsza obróbka w Pillow)."""
    doc = pymupdf.open(sciezka)
    pix = doc[0].get_pixmap(dpi=dpi)
    doc.close()
    return Image.open(io.BytesIO(pix.tobytes("png")))


def zrob_obrocony(sciezka: str):
    """Obraca stronę o 90/270 stopni — jak zdjęcie zrobione 'bokiem'."""
    obraz = renderuj_do_obrazu(sciezka, dpi=150)
    kat = random.choice([90, 270])
    obraz = obraz.rotate(kat, expand=True, fillcolor=(255, 255, 255))
    obraz.save(sciezka, "PDF", resolution=150.0)


def zrob_skan_slabej_jakosci(sciezka: str):
    """Degraduje jakość: przekrzywienie, artefakty JPEG, rozmycie, szum."""
    obraz = renderuj_do_obrazu(sciezka, dpi=120)
    obraz = obraz.rotate(random.uniform(-2.5, 2.5), expand=True, fillcolor=(255, 255, 255))
    obraz = obraz.convert("L")  # skan w odcieniach szarości

    # przepuszczenie przez JPEG o złej jakości odtwarza artefakty skanów
    bufor = io.BytesIO()
    obraz.save(bufor, "JPEG", quality=25)
    obraz = Image.open(bufor)

    obraz = obraz.filter(ImageFilter.GaussianBlur(0.6))
    szum = Image.effect_noise(obraz.size, 50)
    obraz = Image.blend(obraz, szum, 0.18)

    obraz.save(sciezka, "PDF", resolution=120.0)


# --- główna pętla ------------------------------------------------------------

def main():
    os.makedirs("data", exist_ok=True)
    ground_truth = []

    for nr in range(1, 21):
        faktura = zbuduj_fakture(nr)
        defekt = DEFECTY.get(nr)

        if defekt == "termin_przed_wystawieniem":
            wystawiona = date.fromisoformat(faktura["data_wystawienia"])
            faktura["termin_platnosci"] = (wystawiona - timedelta(days=random.randint(3, 10))).isoformat()

        brutto_delta = round(random.uniform(5, 60), 2) if defekt == "kwoty_niezgodne" else 0.0
        zapisz_fakture(
            faktura,
            f"data/faktura_{nr:02d}.pdf",
            pomin_nip_sprzedawcy=(defekt == "brak_nip_sprzedawcy"),
            brutto_delta=brutto_delta,
        )

        if defekt == "obrocony":
            zrob_obrocony(f"data/faktura_{nr:02d}.pdf")
        elif defekt == "skan_slaba_jakosc":
            zrob_skan_slabej_jakosci(f"data/faktura_{nr:02d}.pdf")

        # --- ground truth: to, co jest WYDRUKOWANE (cel ekstrakcji) ---
        netto, vat, brutto = policz_kwoty(faktura["pozycje"])
        ground_truth.append({
            "plik": f"faktura_{nr:02d}.pdf",
            "numer": faktura["numer"],
            "sprzedawca": faktura["sprzedawca"],
            "nip_sprzedawcy": None if defekt == "brak_nip_sprzedawcy" else faktura["nip_sprzedawcy"],
            "nabywca": faktura["nabywca"],
            "nip_nabywcy": faktura["nip_nabywcy"],
            "data_wystawienia": faktura["data_wystawienia"],
            "termin_platnosci": faktura["termin_platnosci"],
            "pozycje": [
                {
                    "nazwa": nazwa,
                    "ilosc": ilosc,
                    "cena_netto": cena,
                    "wartosc_netto": round(ilosc * cena, 2),
                }
                for nazwa, ilosc, cena in faktura["pozycje"]
            ],
            "netto": netto,
            "vat": vat,
            "brutto": round(brutto + brutto_delta, 2),
            "defekty": [defekt] if defekt else [],
            "oczekiwana_walidacja": OCZEKIWANA_WALIDACJA[defekt],
        })

    with open(os.path.join("data", "ground_truth.json"), "w", encoding="utf-8") as plik:
        json.dump(ground_truth, plik, ensure_ascii=False, indent=2)

    print("Wygenerowano 20 faktur w data/")
    print(f"Poprawne: {20 - len(DEFECTY)}, z defektami: {len(DEFECTY)}")
    print("Ground truth: data/ground_truth.json")


if __name__ == "__main__":
    main()
