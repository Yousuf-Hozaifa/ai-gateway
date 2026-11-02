FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd -m -u 1000 appuser
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gateway ./gateway
COPY tools ./tools
USER appuser

EXPOSE 8000
# Hosts such as Render tell the container which port to use through $PORT (default 8000 locally).
ENV PORT=8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://localhost:%s/health' % os.environ['PORT'])"

# One worker on purpose: rate-limit buckets and dashboard stats live in process memory.
CMD ["sh", "-c", "exec uvicorn gateway.main:app --host 0.0.0.0 --port ${PORT}"]
