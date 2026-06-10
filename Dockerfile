FROM python:3.11-slim

WORKDIR /app

COPY scripts/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080 DATA_DIR=/data
EXPOSE 8080

CMD ["python", "scripts/serve.py"]
