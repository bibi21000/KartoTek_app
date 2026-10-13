FROM python:3.12-slim

# Pas de root à l'exécution
RUN groupadd -g 10001 kartotek \
    && useradd -u 10001 -g kartotek -M -s /usr/sbin/nologin kartotek

WORKDIR /app

COPY pyproject.toml .
COPY src/toolspostcards/app ./src/app
COPY src/toolspostcards/scripts ./scripts

RUN pip install --no-cache-dir .[tools]

RUN mkdir -p /data && chown -R kartotek:kartotek /app /data
USER kartotek

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
