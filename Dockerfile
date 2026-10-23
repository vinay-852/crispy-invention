FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Bot runs on long polling; only the PayPal webhook needs the public URL.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]