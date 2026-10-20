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
  `http://IP-serwera:5000`). Na starcie serwer sam przetwarza nowe PDF-y.
- **Faktury:** wrzuć PDF-y do folderu `./data/` i po chwili uruchom
  `docker compose restart panel` — albo od razu batch poniżej.
- **Batch + eksport XML** (jednorazowo, bez dotykania działającego panelu):

```bash
docker compose run --rm cykl    # przetwarza nowe PDF-y + pisze przetworzone/eksport.xml
```

- **Logi:** `docker compose logs -f panel`
- **Stop:** `docker compose down` (dane w `./data/` i `./przetworzone/`
  zostają na dysku — to volume, nie część obrazu)

**Uwaga (VPS):** panel nie ma jeszcze logowania — wystawiony na publiczny
IP jest dostępny dla każdego, kto zna adres. Do czasu dodania autoryzacji
ogranicz dostęp (VPN typu Tailscale albo reguła firewall na porcie 5000).
