# RUNBOOK — co robić, gdy przyjdzie alert

Przepisy reakcji na alerty monitoringu. Każdy alert ma format:
co się stało → jak sprawdzić → typowa przyczyna → co zrobić.
Logi panelu: `docker compose logs -f panel` (ostatnie 100 linii:
`docker compose logs --tail 100 panel`). Stan bazy na żywo:

```bash
sqlite3 data/faktury.db "SELECT plik, status, wgrane_at, blad_przetworzenia FROM faktury"
```

Zasada: alerty przychodzą mailem z Healthchecks.io. Mail o braku pinga
to cisza z maszyny; mail „check has failed" zaraz po 20:00 to `/fail`
od strażnika (treść alarmu jest w treści pingu — kliknij checkę
w panelu Healthchecks i zobacz historię).

## Alert: brak pingów „monitor" (cisza > 45 min)

**Co się stało:** panel przestał odpowiadać albo cała maszyna padła
(monitor wysyła ping tylko po udanej odpowiedzi panelu).

**Jak sprawdzić:**
1. Czy serwer żyje: `ping IP-serwera` / ssh na maszynę.
2. `docker compose ps` — czy `panel` i `monitor` wstają (restart loop?).
3. `docker compose logs --tail 100 panel` i `... monitor`.

**Typowe przyczyny:** restart maszyny z wiszącym Dockera; kontener
panelu restartuje się w pętlę (baza zablokowana? brak klucza?);
zapchany dysk (`df -h`).

**Co zrobić:** zwykle wystarczy `docker compose up -d --force-recreate`.
Jeśli dysk pełny — wyczyść `docker system prune` i stare `eksport_*.xml`
z `przetworzone/`. Jeśli restartuje się w pętlę — czytaj logi; baza
SQLite „database is locked" = dwa procesy walczą o plik (zabierz
ręcznie odpalone `python panel.py` obok kontenera).

## Alert: brak pingów „strażnik" (po 20:30 nie ma raportu)

**Co się stało:** crontab nie odpalił strażnika albo kontener nie wstał.

**Jak sprawdzić:** `docker compose run --rm straznik` ręcznie — jeśli
działa, problem w cronie (ścieżka? crond żyje? `grep CRON /var/log/syslog`).

**Typowe przyczyny:** wpis crontab ze złą ścieżką projektu; zatrzymany
daemon crona po aktualizacji serwera.

## Alert strażnika: „faktura … czeka w kolejce … h"

Kolejka `do_przetworzenia` nie drgnęła ponad 4 h — nikt nie przetwarza.

**Jak sprawdzić:** `docker compose logs --tail 100 panel` — szukaj
linii `odczyt...` i błędów API. Panel odpowiada (monitor pinguje),
ale batch nie leci?

**Typowe przyczyny:** wygasły/brak `GEMINI_API_KEY` w `.env` (batch
pomijany bez klucza); Gemini zwraca 429/500 dłużej niż retry; plik PDF
nieczytelny (wiersz ma `blad_przetworzenia` z opisem — czeka na przycisk
„Przetwórz ponownie").

**Co zrobić:** w panelu zakładka „Do przetworzenia" → „Przetwórz
wszystkie". Jeśli brak klucza — ustaw w `.env` i `docker compose up -d`.
Przy trwającej awarii Gemini faktury bezpiecznie czekają w kolejce
(przy najbliższym przebiegu zostaną wzięte).

## Alert strażnika: „liczba faktur z błędami rośnie dzień po dniu"

Kilka dni z rzędu więcej faktur kończy z błędami walidacji.

**Jak sprawdzić:** w panelu zakładka „Błędy" — czy błędy są tego samego
rodzaju (np. wszystkie „nie składa się z 10 cyfr")?

**Typowe przyczyny:** dostawca zmienił szablon faktury (model czyta
inaczej — np. NIP w nowym miejscu); skany gorszej jakości.

**Co zrobić:** obejrzyj 2–3 PDF-y z błędami obok wyekstrahowanych danych;
jeśli to systemowa zmiana, dołóż przykład do promptu w `weryfikator.py`
albo popraw regułę walidacji. Pojedyncze trudne skany = ręczna decyzja
księgowej w panelu.

## Alert strażnika: „skuteczność ekstrakcji spadła"

Ostatnie 7 dni radzą sobie o ≥15 p.p. gorzej niż dotychczasowa średnia.

Jak wyżej — to wariant „proporcjonalny" tego samego problemu; patrz
przepis wyżej. Jeśli okno ma mało faktur, alert sam się nie pojawi
(próg minimalny 10 faktur).

## Alert strażnika: „błędy API w kolejce (Gemini/limity)"

≥ 2 faktury w kolejce z błędem 429/500/503.

**Typowe przyczyny:** darmowy limit RPM wyczerpany (duży upload
naraz — weryfikator robi przerwę 4 s między zapytaniami, ale limity
bywają mniejsze); awaria Gemini; wygasłe konto API.

**Co zrobić:** zwykle wystarczy „Przetwórz wszystkie" po kilkunastu
minutach (retry w kodzie wyczerpał próby w trakcie awarii). Jeśli
429 powraca — sprawdź limity w Google AI Studio.
