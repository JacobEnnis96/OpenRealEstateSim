# Comprehensive Real Estate Engine

A local FastAPI app that simulates a housing market and lets you interrogate the
results with an AI analyst. Tweak macro levers (rates, demographics, supply
constraints) and watch prices, inventory, closed sales, and affordability evolve
month over month.

## What it does

- **Monte Carlo housing simulation** — seeds a log-normally distributed stock of
  properties, then mutates them monthly using a lock-in / distress / supply-demand
  model.
- **Interactive dashboard** — a browser UI with sliders for every parameter, four
  live charts (Chart.js), and a property ledger table.
- **AI analyst chat** — a chat panel that summarizes the simulation results and
  answers questions about the market, powered by OpenRouter.

## Requirements

- Python 3.10+
- The Python packages in use: `fastapi`, `uvicorn`, `pydantic`, `requests`

Install them with:

```bash
pip install fastapi uvicorn pydantic requests
```

## Setup

1. Create a `.env` file from the example (nosecrets.envexample), or rename nosecrets.envexample to .env and edit:

   ```bash
   cp nosecrets.envexample .env
   ```

2. Add your OpenRouter API key to `.env`:

   ```
   OPENROUTER_API_KEY=sk-or-v1-yourkey
   ```

   You can get a free key at https://openrouter.ai/keys. The models used here are free and no payment info is required to be added to your account to create an API key or use this app.

## Running

```bash
python app.py
```

Then open http://127.0.0.1:8000/ in your browser.

## How the simulation works

The engine seeds a set of homes (default 500) with log-normal prices, then for
each simulated month it:

- **Adds/exits** homes via new construction, commercial conversion, and demolition.
- **Lists homes for sale** based on unemployment, ARM resets, divorce/death, and a
  lock-in effect (owners with low locked rates stay put).
- **Prices and clears deals** by comparing buyer budgets to asking prices, driven
  by credit conditions, DTI, and affordability.
- **Drifts values** each month based on demand vs. supply pressure.

Prices can crash when inventory piles up, there is intentionally no floor.

### Population & demand

The model tracks a literal population (`initial_population`, default 1250 —
aproximately 2.5 people per home) and evolves it each month: it grows via
`pop_growth` and `net_migration` and shrinks via `death_rate`. Demand pressure is
then computed by comparing that population against the housing stock's capacity.

- `share_young_adults` boosts household formation and buyer demand.
- `share_retirees` adds a steady stream of listings from downsizing/aging.
- `existing_homeownership` inversely scales the baseline listing rate — higher
  ownership means a tighter market with fewer active listings.
- `death_rate` both shrinks the population and forces estate liquidations.

## API

### `POST /api/simulate`

Takes a `SimulationParams` body and returns `history` (months, avg_price,
inventory, transactions, affordability) and `properties` (the final ledger).

### `POST /api/chat`

Takes `{ "messages": [...] }` and returns `{ "content": ..., "reasoning_details": ... }`.

It tries the models in order, falling back if one fails:

1. `nvidia/nemotron-3-ultra-550b-a55b:free` (primary)
2. `nvidia/nemotron-3-super-120b-a12b:free` (fallback)

Both models run with reasoning enabled.

## Project layout

| File          | Purpose                                    |
| ------------- | ------------------------------------------ |
| `app.py`      | Main app: API, simulation engine, and UI.  |
| `.env`        | Local environment (API key). Not committed.|
| `.envexample` | Template for `.env`.                       |
