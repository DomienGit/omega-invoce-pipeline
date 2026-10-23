Projekt próbny automatyzujacy procesy w firmie zajmujaca sie rachunkowosciami "Omega"
Cel: przecwiczyc budowanie automatyzacji w procesach biznesowych

## Jak uruchomić (Docker)

Wymagania: zainstalowany Docker z wtyczką compose + plik `.env`
z kluczem `GEMINI_API_KEY` w katalogu projektu (klucz nigdy nie trafia
do obrazu — przekazuje go compose przy starcie).

```bash
docker compose up -d        # start serwera (panel + baza)
```

- **Panel:** `http://localhost:5000` (z innego komputera w sieci:
  `http://IP-serwera:5000`). Na starcie serwer sam dokańcza kolejkę
  faktur przerwanych przy restarcie.
- **Faktury:** księgowa wgrywa PDF-y w panelu („Wybierz faktury…" /
  „Wybierz folder…") — każdy plik od razu dostaje wiersz w kolejce
  (zakładka „Do przetworzenia") i jest przetwarzany w tle. Folder
  `./data/` to tylko magazyn plików, nie skanujemy go w poszukiwaniu
  nowych faktur.
- **Batch + eksport XML** (jednorazowo, bez dotykania działającego panelu):

```bash
docker compose run --rm cykl    # przetwarza kolejkę z bazy + pisze przetworzone/eksport.xml
```

- **Logi:** `docker compose logs -f panel`
- **Stop:** `docker compose down` (dane w `./data/` i `./przetworzone/`
  zostają na dysku — to volume, nie część obrazu)

**Uwaga (VPS):** panel nie ma jeszcze logowania — wystawiony na publiczny
IP jest dostępny dla każdego, kto zna adres. Do czasu dodania autoryzacji
ogranicz dostęp (VPN typu Tailscale albo reguła firewall na porcie 5000).

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
