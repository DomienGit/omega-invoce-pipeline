Projekt próbny automatyzujacy procesy w firmie zajmujaca sie rachunkowosciami "Omega"
Cel: przecwiczyc budowanie automatyzacji w procesach biznesowych

## Jak uruchomić (Docker)

Wymagania: zainstalowany Docker z wtyczką compose + plik `.env`
z kluczem `GEMINI_API_KEY` w katalogu projektu (klucz nigdy nie trafia
do obrazu — przekazuje go compose przy starcie).

```bash
docker compose up -d        # start serwera (panel + baza + caddy)
```

- **Panel:** `https://localhost` (z innego komputera: `https://IP-serwera`).
  Port 443, ruch kończy Caddy (HTTPS). Panel wymaga zalogowania — patrz
  sekcja „Logowanie i HTTPS" niżej. Na starcie serwer sam dokańcza kolejkę
  faktur przerwanych przy restarcie.
- **Faktury:** księgowa wgrywa PDF-y w panelu („Wybierz faktury…" /
  „Wybierz folder…") — każdy plik od razu dostaje wiersz w kolejce
  (zakładka „Do przetworzenia") i jest przetwarzany w tle. Folder
  `./data/` to tylko magazyn plików, nie skanujemy go w poszukiwaniu
  nowych faktur.
- **Logi:** `docker compose logs -f panel`
- **Stop:** `docker compose down` (dane w `./data/` i `./przetworzone/`
  zostają na dysku — to volume, nie część obrazu)

## Logowanie i HTTPS

Panel stoi za Caddy, który jako **jedyne okno na świat** kończy TLS
certyfikatem samopisanym (nie mamy domeny — bez niej Let's Encrypt
nie wystawi certyfikatu). Skutki praktyczne:

- cały ruch (hasło, faktury, PDF-y) jest szyfrowany tak samo jak
  przy „prawdziwym" certyfikacie,
- przy pierwszym wejściu przeglądarka pokaże ostrzeżenie o nieznanym
  wystawcy — kliknij „Zaawansowane → Przejdź na stronę" (raz na
  urządzenie; jeśli kiedyś doszedłbyś do domeny, wymiana na darmowy
  certyfikat to jedna linia w `Caddyfile`),
- port 5000 panelu nie istnieje z zewnątrz — jedyny otwarty port to 443.

Konta (login + **hash** hasła, nigdy jawne hasło) i pozostałe sekrety
mieszkają w `.env` (szablon: `.env.example`). Trzy generatory:

```bash
python -c "import secrets; print(secrets.token_hex(32))"    # SECRET_KEY (sesje)
python -c "import secrets; print(secrets.token_urlsafe(32))" # PANEL_TOKEN (monitor)
python -c "from werkzeug.security import generate_password_hash as g; print('ksiegowa=' + g(input('Hasło: ')))"
# ^ uruchom dla każdego konta, wyniki sklej średnikiem w PANEL_KONTA
```

Efekt w `.env` wygląda tak (hasze przykładowe):

```
SECRET_KEY=64 znaki hex...
PANEL_TOKEN=43 znaki urlsafe...
PANEL_KONTA='ksiegowa=scrypt:32768:8:1$abc...$def...;wlasciciel=scrypt:32768:8:1$...'
ADRES_VPS=203.0.113.10
```

`PANEL_KONTA` musi być w **pojedynczych** cudzysłowach — hash werkzeuga
zawiera `$`, a docker compose w wartościach bez cudzysłowów i w podwójnych
cudzysłowach traktuje `$coś` jako zmienną i zamienia ją na pusty ciąg
(hash się psuje i logowanie odrzuca dobre hasło). Pojedyncze cudzysłowy
są dla compose i python-dotenv wartością dosłowną.

`ADRES_VPS` to IP, pod którym panel jest osiągalny: na VPS-ie jego
publiczne IP, lokalnie `127.0.0.1`. Uwaga na testy „na własnym
komputerze z własnym publicznym IP": większość domowych łączy (CGNAT)
nie pozwala wrócić do własnej maszyny przez ten adres — lokalnie
zawsze `127.0.0.1`, a w przeglądarce wchodź na `https://127.0.0.1`
dokładnie (nie na `https://localhost`, bo caddy dopasowuje adres).

Zasady bezpieczeństwa wbudowane w kod: 5 błędnych logowań z jednego
IP = 15 minut blokady; sesja wygasa po 8 h; ciasteczko HttpOnly
i SameSite=Lax; każdy endpoint (także API i PDF-y) przechodzi przez
tę samą bramę — wylogowany nie dobierze się do danych nawet `curl`-em.
Monitor loguje się tokenem (`PANEL_TOKEN`), HEALTHCHECK Dockera używa
publicznego `/healthz` (sama informacja „żyje", bez danych).

Puste `PANEL_KONTA` = logowanie wyłączone (tryb deweloperski —
`python desktop.py` lokalnie działa bez hasła). Na serwerze zawsze
ustawione.

## Monitoring (czy aplikacja żyje na serwerze)

Trzy warstwy (szczegóły i przepisy reakcji: `RUNBOOK.md`):

1. **Auto-restart** — compose (`restart: unless-stopped`) + healthcheck
   w obrazie. Padnięty proces wstaje sam; tego nie trzeba konfigurować.
2. **Monitor** — kontener `monitor` odpytuje panel co 15 min tak jak
   przeglądarka księgowej. Po udanej odpowiedzi wysyła ping do
   [Healthchecks.io](https://healthchecks.io). Zawieszony panel, padnięty
   serwer czy restartująca się w pętlę maszyna = brak pingów = mail
   z Healthchecks po upływie czasu grace.
3. **Strażnik dzienny** — raport o 20:00 (przetworzone / do sprawdzenia /
   wyeksportowane / kolejka) + 4 alerty progowe (kolejka stoi >4 h,
   błędy rosną dzień po dniu, spadek skuteczności o 15 p.p., seria błędów
   API). Aktywny alarm pinguje `/fail` — natychmiastowy mail.

Konfiguracja jednorazowa (~5 minut):

1. W Healthchecks.io utwórz 2 checky:
   - `monitor` — Schedule: Period 15 minutes, Grace 45 minutes,
   - `strażnik` — Schedule: Cron `0 20 * * *`, Grace 30 minutes
     (w Integration/Notification ustaw swój mail — domyślnie konto ma
     już podstawowy).
2. Adresy pingu obu check (URL w stylu `https://hc-ping.com/<uuid>`)
   dopisz do `.env`:

   ```
   HEALTHCHECKS_URL=https://hc-ping.com/...        # checka "monitor"
   HEALTHCHECKS_STRAZNIK_URL=https://hc-ping.com/...  # checka "strażnik"
   ```

3. Na serwerze dodaj do crontaba (`crontab -e`) uruchomienie strażnika:

   ```
   0 20 * * * cd /ścieżka/do/omega-invoce-pipeline && docker compose run --rm straznik
   ```

4. `docker compose up -d --build` — od tej chwili w Healthchecks powinny
   pojawiać się pingi co 15 min (treść z licznikami widać w historii
   pingu po kliknięciu w checkę).

Raporty strażnika lądują też w `./logi/straznik_RRRR-MM-DD.txt`.

Wszystkie zmienne są opcjonalne: bez nich monitor i strażnik działają,
ale nie wysyłają nic na zewnątrz (raport tylko na ekran/do logów).
