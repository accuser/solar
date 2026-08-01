FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY weather_collector.py .

CMD ["python", "-u", "weather_collector.py"]
