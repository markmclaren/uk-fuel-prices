FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Copy uff.py (stdlib only, zero pip dependencies)
COPY uff.py .

# Entry point: collect fuel data, update cache, and dump prices JSON
ENTRYPOINT ["python", "uff.py", "--debug", "--dump", "--compact", "--max-price-age-days", "7"]