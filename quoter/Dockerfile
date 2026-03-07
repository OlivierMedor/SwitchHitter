FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
COPY quoter/requirements.txt ./quoter/

RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir -r quoter/requirements.txt

COPY quoter/ ./quoter/

# Command to run the quoter (usually runs as a batch job, but can be scheduled)
CMD ["python", "-m", "quoter.main"]
