# Cloud Run Job: rulează pipeline-ul complet pentru un singur PDF, apoi iese.
FROM python:3.13-slim

# poppler-utils: pdftoppm, folosit la recitirea țintită a zonelor picate la R0.
# fonts-dejavu-core + fonts-liberation: glifele românești (ș și ț cu virgulă
# dedesubt nu sunt în Latin-1, iar Helvetica nu le are deloc). Drafter-ul
# refuză să genereze scrisoarea dacă niciunul nu e instalat.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY consilium/ ./consilium/
COPY job/ ./job/
COPY config.yaml ./

ENTRYPOINT ["python", "-m", "job.entry"]
