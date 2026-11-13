Projekt próbny automatyzujacy procesy w firmie zajmujaca sie rachunkowosciami "Omega"
Cel: przecwiczyc budowanie automatyzacji w procesach biznesowych

## Jak uruchomić (Docker)

Wymagania: zainstalowany Docker z wtyczką compose + plik `.env`
z kluczem `GEMINI_API_KEY` w katalogu projektu (klucz nigdy nie trafia
do obrazu — przekazuje go compose przy starcie).

```bash
docker compose up -d        # start serwera (panel + baza + caddy)
```

- **Panel:** `https://` + twoja domena z `.env` (`DOMENA`) + port 8443,
  lokalnie po prostu `https://localhost:8443`. Ruch kończy Caddy (HTTPS).
  Panel wymaga zalogowania — patrz sekcja „Logowanie i HTTPS" niżej.
  Na starcie serwer sam dokańcza kolejkę faktur przerwanych
  przy restarcie.
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
certyfikatem Let's Encrypt. Wyzwanie ACME to DNS-01: Caddy sam zakłada
rekord TXT `_acme-challenge.<domena>` przez API Duck DNS (token
`DUCKDNS_TOKEN` w `.env`) — dzięki temu certyfikat nie wymaga portów
80/443, które na serwerze zajęte są przez inną aplikację. Panel jest
osiągalny na porcie 8443: `https://omega-faktury.duckdns.org:8443`.
Skutki praktyczne:

- cały ruch (hasło, faktury, PDF-y) jest szyfrowany, a przeglądarka
  nie pokazuje ostrzeżeń — certyfikat jest „prawdziwy",
- lokalnie (`DOMENA=localhost`) Caddy wystawia certyfikat ze swojego
  CA — ostrzeżenie o nieznanym wystawcy dotyczy wyłącznie testów
  lokalnych (`DUCKDNS_TOKEN` może być wtedy pusty),
- port 5000 panelu nie istnieje z zewnątrz — otwarty jest tylko 8443
  (TCP; plus UDP dla HTTP/3),
- obraz Caddy budujemy sami (`Dockerfile.caddy` z modułem duckdns),
  bo standardowy `caddy:2` nie wspiera wyzwania DNS-01.

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
DOMENA=omega-faktury.duckdns.org
DUCKDNS_TOKEN=token z duckdns.org
```

`PANEL_KONTA` musi być w **pojedynczych** cudzysłowach — hash werkzeuga
zawiera `$`, a docker compose w wartościach bez cudzysłowów i w podwójnych
cudzysłowach traktuje `$coś` jako zmienną i zamienia ją na pusty ciąg
(hash się psuje i logowanie odrzuca dobre hasło). Pojedyncze cudzysłowy
są dla compose i python-dotenv wartością dosłowną.

`DOMENA` to nazwa, pod którą panel jest osiągalny: na VPS-ie domena
wskazująca na jego publiczne IP (u nas darmowa `omega-faktury.duckdns.org`
z Duck DNS — wpis A ustawiony raz, IP serwera jest stałe), lokalnie
`localhost`. Panel słucha na porcie 8443 — w przeglądarce wchodź
dokładnie na tę nazwę z portem, czyli na serwerze
`https://omega-faktury.duckdns.org:8443`, a w testach lokalnych
`https://localhost:8443` (Caddy dopasowuje adres, także z portem).

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
