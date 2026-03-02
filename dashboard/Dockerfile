FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Install dependencies
COPY requirements.txt .
RUN python -m pip install --upgrade pip && \
    python -m pip install -r requirements.txt

# Copy source
COPY dashboard/ ./dashboard/

# Expose Streamlit port
EXPOSE 8501

CMD ["python", "-m", "streamlit", "run", "dashboard/main.py", "--server.address=0.0.0.0"]
