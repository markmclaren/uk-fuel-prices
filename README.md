# UK True Cost Fuel Finder ⛽

A self-contained web application and automated data collection pipeline to answer the essential question:

> **"Where is the cheapest place to fill up, accounting for the fuel I'll burn driving there and back?"**

A petrol station 10 miles away charging **135.0p/L** may cost you *more* overall than a local station charging **140.0p/L** once you factor in the round-trip detour fuel burn. This tool computes the **True Cost** (effective price per litre and total tank cost) for every nearby station.

---

## 🔗 Project Origin & Attribution

This project is built as a streamlined, dependency-free enhancement inspired by the original [UK-Fuel-Finder](https://github.com/ismjml/UK-Fuel-Finder) project. It expands upon the original concepts by providing automated GitHub Actions scraping, client-side map visualization, zero-dependency Python script execution, and GitHub Pages deployment.

---

## 📐 The Mathematical Formula

The algorithm evaluates every station within your chosen search radius by computing the round-trip detour cost and adding it to your total fill expense.

### 1. Variables & Inputs

| Symbol | Parameter | Unit | Default |
|---|---|---|---|
| $P_{\text{pump}}$ | Station Pump Price | pence / litre | (From data) |
| $d_{\text{one-way}}$ | Distance to Station | miles | (Haversine formula) |
| $d_{\text{round-trip}}$ | Round-trip Detour Distance ($2 \times d_{\text{one-way}}$) | miles | — |
| $\text{MPG}$ | Vehicle Fuel Economy | UK Imperial MPG | 40.0 mpg |
| $V_{\text{tank}}$ | Tank Fill Amount | litres | 40 L |
| $C_{\text{gal}}$ | UK Gallon Constant | litres / gallon | 4.54609 L |

---

### 2. Fuel Consumption & Detour Cost

First, determine the total fuel burned during the round trip:

$$\text{Litres Burned } (L_{\text{burn}}) = d_{\text{round-trip}} \times \left( \frac{C_{\text{gal}}}{\text{MPG}} \right)$$

The monetary cost of burning that fuel (evaluated at the target station's price) is:

$$\text{Detour Cost (pence) } (C_{\text{detour\_p}}) = L_{\text{burn}} \times P_{\text{pump}}$$

---

### 3. Effective True Price & Total Tank Cost

The **Effective Price** ($P_{\text{true\_p}}$, in pence per litre) bakes the detour cost into every litre of fuel bought:

$$P_{\text{true\_p}} = P_{\text{pump}} + \left( \frac{C_{\text{detour\_p}}}{V_{\text{tank}}} \right)$$

The **Total True Tank Cost** ($T_{\text{true\_GBP}}$, in pounds) is the complete out-of-pocket cost for the trip and fill:

$$T_{\text{true\_GBP}} = \frac{P_{\text{true\_p}} \times V_{\text{tank}}}{100} = \frac{(P_{\text{pump}} \times V_{\text{tank}}) + C_{\text{detour\_p}}}{100}$$

---

### 4. Savings & Verdict Classification

Comparing a candidate station ($P_{\text{true\_target}}$) against the nearest available station ($P_{\text{true\_nearest}}$):

$$\text{Net Saving (£) } (\Delta S_{\text{GBP}}) = \frac{(P_{\text{true\_nearest}} - P_{\text{true\_target}}) \times V_{\text{tank}}}{100}$$

Stations are classified into four visual tiers:
- 🔵 **Nearest**: The geographically closest station (baseline reference).
- 🟢 **Worth It**: Net saving $\Delta S_{\text{GBP}} > +£0.10$.
- 🟡 **Break-Even**: Net saving within $\pm £0.10$.
- 🔴 **Not Worth It**: Net saving $\Delta S_{\text{GBP}} < -£0.10$.

---

## ✨ Features

- 🗺️ **Interactive Vector Map**: Powered by MapLibre GL JS and OpenFreeMap dark tiles with dynamic radius circle overlays.
- ⚡ **GitHub Pages Ready**: The web app in `/docs` auto-loads `prices_latest.json` on startup.
- 🐍 **Zero External Dependencies**: `uff.py` uses 100% standard library modules (`urllib`, `json`, `fcntl`) with no `pip install` required.
- ⚙️ **Automated GitHub Actions**: Scheduled workflow runs every 2 hours to pull fresh prices from the UK Government Fuel Finder API.
- 📊 **Table & Map Integration**: Instant search filtering, distance radius sliders, Costco/Motorway exclusion toggles, and geolocation support.

---

## 🚀 Deployment & Usage

### 🌐 Deploying to GitHub Pages

1. Push this repository to GitHub.
2. Go to **Settings > Pages** in your repository.
3. Select **Deploy from a branch** -> Branch `main` -> Folder `/docs`.
4. Your live app will be published at `https://<your-username>.github.io/<repo-name>/`.

### 🔄 Automated Data Updates (GitHub Actions)

Add your UK Fuel Finder API credentials to GitHub Secrets:
- `UFF_CLIENT_ID`
- `UFF_CLIENT_SECRET`

The GitHub Actions workflow in `.github/workflows/fuel_finder.yml` will automatically fetch data, update local state caches, and write minified JSON output to `docs/prices_latest.json` every 2 hours.

---

## 💻 Local Development

### Running the Python Collector

```bash
python3 uff.py --debug --dump --compact --output-dir docs
```

### Running via Docker Compose

```bash
docker compose up --build
```

---

## 🛠️ Tech Stack

- **Frontend**: HTML5, Vanilla JavaScript, CSS Grid & Flexbox (Located in `/docs`).
- **Mapping**: [MapLibre GL JS](https://maplibre.org/) with [OpenFreeMap](https://openfreemap.org/) dark tiles.
- **Data Scraping & Extraction**: Python 3 (`uff.py`, standard library only).
- **Automation**: GitHub Actions (`.github/workflows/fuel_finder.yml`).
