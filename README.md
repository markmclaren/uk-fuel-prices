# UK True Cost Fuel Finder ⛽

A self-contained web app and data pipeline to answer the question:

> **"Where is the cheapest place to fill up, accounting for the fuel I'll burn driving there and back?"**

A station 10 miles away charging **135.0p/L** may cost you *more* overall than a local station charging **140.0p/L** once you factor in the round-trip detour fuel burn. This tool computes the **True Cost** (effective price per litre and total tank cost) for every nearby station.

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
- 🟢 **Worth It** (`status-worth-it`): Net saving $\Delta S_{\text{GBP}} > +£0.10$.
- 🟡 **Break-Even** (`status-breakeven`): Net saving within $\pm £0.10$.
- 🔴 **Not Worth It** (`status-not-worth`): Net saving $\Delta S_{\text{GBP}} < -£0.10$.

---

## ✨ Features

- 🗺️ **Interactive Dark-Mode Map**: Vector map powered by MapLibre GL JS and OpenFreeMap tiles with dynamic radius circle overlay.
- 📊 **Table Layout**: High-density sidebar displaying **Station**, **Pump Price**, **True Cost**, and **Distance**.
- 💷 **Full Tank (£) / p/L Display Toggle**: Switch primary display units between total tank cost in pounds and pence per litre.
- 🚫 **Costco & Motorway Exclusion**: Exclude membership-only stations (Costco) and expensive motorway service stations via single-click toggles.
- 🎯 **Interactive Radius Overrides**: Live range slider, preset pills (`5k`, `10k`, `15k`, `25k`, `50k`), and map-click location origin picker.
- ↕️ **Interactive Column Sorting**: Click any table header or quick-sort button to sort by True Cost, Pump Price, Distance, or Station Name.
- 📍 **Geolocation**: One-click geolocation using standard browser location APIs.

---

## 🚀 Quick Start

### 1. Download UK Fuel Price Data
Run `uff.py` or the Docker container to generate a JSON dump (`prices_YYYY-MM-DD.json`):

```bash
python3 uff.py
```

### 2. Launch the Finder App
Serve the directory locally using Node or Python:

```bash
# Using Node
npx serve .

# Or using Python
python3 -m http.server 3000
```

Open `http://localhost:3000/fuel-finder.html` in your web browser, upload your `prices_YYYY-MM-DD.json` file, and click **Find Cheapest Stations**.

---

## 🛠️ Tech Stack

- **Frontend**: HTML5, Vanilla JavaScript, CSS Grid & Flexbox.
- **Mapping**: [MapLibre GL JS](https://maplibre.org/) with [OpenFreeMap](https://openfreemap.org/) dark tiles.
- **Data Scraping & Extraction**: Python 3 (`uff.py`).
