# Zadanie: zapakowanie pipeline'u w Docker

> Brief dla agenta AI. Przeczytaj CAŁOŚĆ przed rozpoczęciem pracy, potem działaj krok po kroku wg sekcji "Zadania". Wątpliwości rozstrzygaj wg sekcji "Pułapki" i "Zasady". Na koniec odhacz "Definition of Done" — dopiero wtedy zadanie jest skończone.

## Kontekst — co to za projekt

Pipeline przetwarzania faktur dla fikcyjnego biura rachunkowego "Omega" (projekt naukowy/portfolio):

- `weryfikator.py` — batch: czyta PDF-y z `data/`, renderuje pierwszą stronę do PNG (pymupdf), wysyła do Gemini API (klucz `GEMINI_API_KEY` z `.env`), waliduje dane (NIP, kwoty, daty, duplikaty), zapisuje wyniki do `data/wyniki_weryfikacji.json` i SQLite `data/faktury.db` (moduł `baza.py`). Ma wznawianie: faktury już obecne w JSON są pomijane.
- `panel.py` — robi to samo co weryfikator dla nowych PDF-ów, po czym generuje `data/panel.html` (szablon `templates/panel.html` + dane z bazy) i otwiera przeglądarkę. Panel jest statyczny, podglądowy.
- `eksport.py` — CLI: eksportuje faktury o zadanym statusie (domyślnie `zatwierdzone`) do `data/eksport.xml`.
- `baza.py` — warstwa SQLite. Wszystkie skrypty działają na ścieżkach względnych (`data/`, `templates/`) względem CWD.
- `data/` — PDF-y wejściowe, `faktury.db`, `wyniki_weryfikacji.json`, `ground_truth.json` (wzorce do testów), `panel.html`, `eksport.xml`. Ten katalog jest INTERFEJSEM człowieka: tu wrzuca się PDF-y i stąd pobiera eksport.
- Historia pracy w `WORKLOG.md` — konwencja projektu: po skończonej pracy dopisujemy wpis z datą.

Docelowa architektura (ustalona z właścicielem projektu): aplikacja działa **cały czas na jednej maszynie** (docelowo serwer u klienta). Docker ma ją sklejać w całość, która po restarcie maszyny wstaje sama.

## Cel

Zamknąć pipeline w Dockerze tak, żeby:

1. PDF wrzucony do `./data/` (z poziomu hosta) był automatycznie przetwarzany cyklicznie (co 5 minut) — bez uruchamiania czegokolwiek ręcznie.
2. Panel był dostępny w przeglądarce pod `http://localhost:8000/panel.html` (serwowanie katalogu `data/`).
3. Eksport dało się odpalić jedną komendą `docker compose run --rm cykl python eksport.py`.
4. Dane (`data/`) przeżywały usunięcie/konstrukcję kontenerów (volume z hosta).
5. Całość wstawała sama po `docker compose up -d` i restarcie maszyny (`restart: unless-stopped`).

## Zadania (w tej kolejności)

### Krok 0 — porządki w zależnościach (OBOWIĄZKOWO przed Dockerfile)

`pyproject.toml` jest nieaktualny: zadeklarowana `flask` jest nieużywana, a faktyczne importy (`google-genai`, `pymupdf`, `python-dotenv`) nie są zadeklarowane. Popraw listę `dependencies` do stanu faktycznego (te trzy pakietu; flask usuń). Bez tego obraz zbuduje się bez bibliotek i padnie w runtime.

### Krok 1 — `.dockerignore`

Wyklucz z build contextu: `.venv/`, `__pycache__/`, `.git/`, `data/`, `.env`, `generator.py`, `*.pyc`. Katalog `data/` i klucz API NIGDY nie mogą trafić do obrazu — obraz to tylko kod.

### Krok 2 — `Dockerfile`

- Baza: `python:3.12-slim` — **konkretnie 3.12**, nie nowsze (python 3.14 w tym środowisku nie ma modułu `sqlite3` — wpis w WORKLOG z 14-10-2026).
- `WORKDIR /app`.
- Instalacja zależności z `pyproject.toml` (`pip install .` po skopiowaniu kodu — najpierw sam plik konfiguracyjny w osobnej warstwie, żeby cache działał, potem reszta kodu).
- `COPY` tylko: `*.py`, `templates/`, `pyproject.toml`, `README.md`.
- Zainstaluj `tzdata` i ustaw `ENV TZ=Europe/Warsaw` (skrypty używają `date.today()` — daty w raportach i eksporcie mają być po lokalnu, nie UTC).
- Użytkownik nie-root (`useradd`) — dobra praktyka, przy okazzeniu pliki w volume nie będą roota.
- Domyślny `CMD` może odpalać `panel.py`.

### Krok 3 — drobna zmiana w `panel.py`

Otwieranie przeglądarki (`webbrowser.open`) w kontenerze nie ma sensu. Nie zmieniaj żadnej logiki przetwarzania — tylko ten jeden fragment: pomijaj otwieranie przeglądarki, gdy ustawiona jest zmienna środowiskowa `BEZ_PRZEGLADARKI=1` (w compose ustawiaj ją zawsze). W konsoli zamiast tego wypisz pełny URL panelu.

### Krok 4 — `docker-compose.yml` (dwa serwisy z jednego obrazu)

```yaml
services:
  cykl:
    build: .
    restart: unless-stopped
    env_file: .env
    environment:
      - BEZ_PRZEGLADARKI=1
    volumes:
      - ./data:/app/data
      - ./templates:/app/templates:ro
    command: >-          # pętla: co 5 min przetwórz nowe PDF-y + przegeneruj panel
      sh -c "while true; do python panel.py; sleep 300; done"
    logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}
  panel-serwer:
    build: .
    restart: unless-stopped
    volumes:
      - ./data:/app/data:ro
    command: python -m http.server 8000 --directory /app/data --bind 0.0.0.0
    ports:
      - "8000:8000"
    logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}
```

Uwagi:
- Pętla w `cykl` jest celowa — to tymczasowy zamiennik kolektora (folder watch powstanie później jako osobny program). `panel.py` sam pomija już przetworzone pliki, więc pętla niczego nie dubluje.
- Log-rotation przez logging driver (json-file z limitami) — na MVP zamiast rotacji plików.
- Do `panel-serwer` dodaj `HEALTHCHECK` (wget/curl na `http://localhost:8000/panel.html`).
- `weryfikator.py` (pełny re-run) i `eksport.py` odpala się przez `docker compose run --rm cykl python <skrypt>.py` — nie potrzeba osobnych serwisów.

### Krok 5 — README: sekcja "Jak uruchomić (Docker)"

Krótko i po ludzku, dla kogoś kto widzi projekt pierwszy raz: wymagania (Docker + compose), `docker compose up -d`, gdzie wrzucać PDF-y (`./data/`), gdzie jest panel (`http://localhost:8000/panel.html`), jak wywołać eksport, jak zobaczyć logi (`docker compose logs -f cykl`), jak zatrzymać.

### Krok 6 — wpis w WORKLOG.md

Wpis z datą, w stylu istniejących: co powstało (Dockerfile, compose, .dockerignore, poprawka pyproject i panel.py), jak testowano (lista testów z DoD), co przyszło naprawić po drodze.

## Pułapki (sprawdzone fakty o tym projekcie)

1. **Python 3.12, nie wyżej** — brak `sqlite3` w 3.14 (WORKLOG 14-10-2026).
2. **Ścieżki względne** — wszystkie skrypty zakładają CWD=`/app`. Workdir w obrazie załatwia sprawę; nie "naprawiaj" ścieżek w kodzie.
3. **`.env` tylko przez env_file** — klucz API nigdy w obrazie, nigdy w ARG/ENV obrazu, nigdy w repo.
4. **`data/` to volume, nie warstwa obrazu** — baza i wyniki muszą przetrwać `docker compose down`.
5. **pymupdf** na slimie działa z wheel — nie trzeba systemowych zależności; jeśli jednak build krzyknie, doinstaluj minimum przez apt i opisz w WORKLOG.
6. **Nie modyfikuj logiki** weryfikatora/bazy/eksportu. Jedyne dozwolone zmiany w kodzie: `panel.py` (przeglądarka, krok 3) i `pyproject.toml` (zależności, krok 0). Wszystko inne to nowe pliki.
7. commity po polsku, w stylu istniejących ("Dodano konteneryzację Docker").

## Definition of Done (odhacz po kolei, uruchamiając naprawdę)

- [ ] `docker compose build` przechodzi bez błędów
- [ ] `docker compose up -d` — oba serwisy `Up` (`docker compose ps`)
- [ ] Nowy PDF wrzucony do `./data/` znika z "nieprzetworzonych" i pojawia się w panelu w ciągu ~5 minut (`http://localhost:8000/panel.html`)
- [ ] PDF osadzony w panelu otwiera się z poziomu `http://` (użyj linku "otwórz w nowej karcie" — osadzanie z http działa lepiej niż z file://)
- [ ] `docker compose run --rm cykl python eksport.py` tworzy `data/eksport.xml`
- [ ] `docker compose down && docker compose up -d` — faktury.db i wyniki nietknięte (volume działa)
- [ ] `docker inspect` na obrazie: brak `GEMINI_API_KEY` w env/konfiguracji; `.env` nie leży w obrazie
- [ ] `docker compose logs cykl` pokazuje przebiegi pętli; daty w logach/raportach po czasie warszawskim
- [ ] Drugi przebieg pętli na tych samych PDF-ach nie tworzy duplikatów w bazie (idempotencja zachowana)
- [ ] README ma sekcję uruchomienia; WORKLOG ma wpis

## Poza zakresem tego zadania

- Tailscale i systemd na hoście (konfiguracja maszyny, nie obrazu)
- Pingi do Healthchecks.io i alerty (osobne zadanie wg notatki "Monitoring" w vault projektu)
- Kolektor folder-watch, serwer panelu z zatwierdzaniem, IMAP — przyszłe etapy
- Faktyczna maszyna u klienta — tu tylko lokalne uruchomienie
