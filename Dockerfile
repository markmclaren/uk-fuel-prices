FROM python:3.9-slim

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Set the working directory
WORKDIR /app

# Copy the script into the container
COPY uff.py .

# Set the entry point
# --dump: refresh cache then write all stations with fresh prices to
# prices_<date>.json in the work dir (mounted to ./config/.storage/uk_fuel_finder/).
# --max-price-age-days 1 keeps only prices updated in the last 24 hours.
ENTRYPOINT ["python", "uff.py", "--debug", "--dump", "--max-price-age-days", "7"]