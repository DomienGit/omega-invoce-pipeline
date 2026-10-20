# Omega Faktury — obraz serwera: panel (Flask) + pipeline batch
# (weryfikator, baza, eksport). Docelowo staje na VPS; łączą się z nim
# nakładki desktopowe / przeglądarki z innych komputerów w sieci.
# desktop.py i generator.py zostają poza obrazem (GUI / narzędzie testowe).
FROM python:3.12-slim

# tzdata + strefa Warszawa: date.today() (raporty, "dataGeneracji" w XML)
# ma zwracać czas lokalny, nie UTC. Od razu użytkownik nie-root — uid 1000
# odpowiada kontu na hoście/VPS, więc pliki tworzone w volume (faktury.db,
# wyniki, XML) nie będą należeć do roota:
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 appuser
ENV TZ=Europe/Warsaw

WORKDIR /app

# Najpierw sama konfiguracja zależności — dopóki pyproject.toml się nie
# zmieni, kosztowna warstwa z pip install jest brana z cache (kod może
# się zmieniać i niepsuć cache'a):
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir .

# Kod programu. Wszystkie ścieżki w skryptach są względne (data/,
# przetworzone/, templates/) — załatwia je WORKDIR /app:
COPY weryfikator.py baza.py eksport.py panel.py ./
COPY templates/ templates/

USER appuser

# Domyślnie serwer panelu (batch nowych PDF-ów robi na starcie).
# Jednorazowy batch/eksport bez panelu — serwis "cykl" w compose
# albo: docker run ... <obraz> python weryfikator.py
CMD ["python", "panel.py"]

# Zdrowie = panel odpowiada na API. start-period dłuższy, bo przy
# starcie z nowymi PDF-ami panel najpierw robi batch (wolne API).
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/faktury', timeout=3)" || exit 1
