FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000
EXPOSE 8000

# WEB_CONCURRENCY defaults to 1 (today's behavior). Raising it shares one SQLite file
# across processes — safe under WAL mode (see main.py db()) — but each worker keeps its
# own in-memory response cache and model-fallback cooldown; see README.md.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY:-1}"]
