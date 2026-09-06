FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY odoo_client.py server.py .

# Railway injects PORT at runtime; server.py reads it directly.
CMD ["python", "server.py"]
