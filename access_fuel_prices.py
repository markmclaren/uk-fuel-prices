import json
import os
import sys
import requests
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

BASE_URL = "https://www.fuel-finder.service.gov.uk"
TOKEN_URL = f"{BASE_URL}/api/v1/oauth/generate_access_token"
PRICES_URL = f"{BASE_URL}/api/v1/pfs/fuel-prices"

# Extract OAuth credentials
client_id = os.getenv("UFF_CLIENT_ID")
client_secret = os.getenv("UFF_CLIENT_SECRET")

def get_access_token(cid: str | None = None, csec: str | None = None) -> tuple[str, str]:
    """Obtain an OAuth access token and refresh token from the Fuel Finder API."""
    cid = cid or client_id
    csec = csec or client_secret

    if not cid or not csec:
        raise ValueError("UFF_CLIENT_ID and UFF_CLIENT_SECRET must be set in the environment or .env file.")

    # The Fuel Finder API expects JSON payload: {"client_id": "...", "client_secret": "..."}
    payload = {"client_id": cid, "client_secret": csec}
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    response = requests.post(TOKEN_URL, json=payload, headers=headers)
    
    # If 401, check if credentials might be inverted (client_id is 32 chars, client_secret is 64 chars)
    if response.status_code == 401 and len(cid) == 64 and len(csec) == 32:
        print("[INFO] Retrying with swapped credentials (client_id <-> client_secret)...")
        payload = {"client_id": csec, "client_secret": cid}
        response = requests.post(TOKEN_URL, json=payload, headers=headers)

    response.raise_for_status()
    res_data = response.json()
    token_info = res_data.get("data", res_data)

    access_token = token_info.get("access_token")
    refresh_token = token_info.get("refresh_token")
    return access_token, refresh_token

def get_fuel_prices(access_token: str, batch_number: int = 1) -> list[dict]:
    """Fetch fuel prices for a specific batch number."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    params = {"batch-number": batch_number}
    response = requests.get(PRICES_URL, headers=headers, params=params)
    response.raise_for_status()
    return response.json()

if __name__ == "__main__":
    try:
        print(f"Requesting access token from {TOKEN_URL}...")
        access_token, refresh_token = get_access_token()
        print(f"[SUCCESS] OAuth Access Token obtained (valid for 1 hour).")
        print(f"Token preview: {access_token[:30]}...")

        print(f"\nFetching fuel prices from {PRICES_URL}?batch-number=1...")
        stations = get_fuel_prices(access_token, batch_number=1)
        print(f"[SUCCESS] Successfully retrieved {len(stations)} fuel stations!")

        if stations:
            sample = stations[0]
            print("\nSample station data:")
            print(json.dumps(sample, indent=2))

    except requests.RequestException as e:
        print(f"[ERROR] Request failed: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"Status Code: {e.response.status_code}")
            print(f"Response Body: {e.response.text}")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
