FROM python:3.11-slim

WORKDIR /app

COPY scripts/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run mounts the GCS volume at /data. SimpleHTTPRequestHandler serves
# files relative to WORKDIR (=/app), so static fetches like
# /data/tournament_sim.json look in /app/data. Symlink /app/data → /data so
# both paths resolve to the same GCS-backed location. Self-host is unaffected
# because Docker isn't involved there.
RUN rm -rf /app/data && ln -s /data /app/data

ENV PORT=8080 DATA_DIR=/data
EXPOSE 8080

CMD ["python", "scripts/serve.py"]
