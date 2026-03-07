FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
COPY backtester/requirements.txt ./backtester/

RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir -r backtester/requirements.txt

COPY backtester/ ./backtester/

CMD ["python", "-m", "backtester.main"]
