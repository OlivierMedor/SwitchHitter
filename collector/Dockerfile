FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY collector/ ./collector/

CMD ["python", "collector/main.py"]
