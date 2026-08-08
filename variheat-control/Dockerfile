FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY variheat_control.py probe_occupancy.py .

CMD ["python", "-u", "variheat_control.py"]
