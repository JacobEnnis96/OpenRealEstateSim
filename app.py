import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import random
import math
import os
import json
import requests


# quick and dirty env loader so I don't need python-dotenv as a dep
def load_env(path=".env"):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())
    except FileNotFoundError:
        pass


load_env()

app = FastAPI(title="Comprehensive Real Estate Engine")


# pydantic model for the API input. all the knobs you can turn in the UI
class SimulationParams(BaseModel):
    num_properties: int = 500
    initial_population: int = 1250
    simulation_months: int = 24

    # demographics / preferences
    pop_growth: float = 1.0
    household_formation: float = 1.5
    share_young_adults: float = 30.0
    share_retirees: float = 20.0
    net_migration: float = 0.5
    divorce_rate: float = 2.5
    death_rate: float = 0.8
    first_time_buyers: float = 33.0
    geographic_preference_shift: float = 50.0   # 0 = all suburb, 100 = all urban
    size_preference_shift: float = 50.0         # 0 = small homes, 100 = large

    # income / wealth
    unemployment_rate: float = 5.0
    income_growth: float = 3.0
    wealth_savings_index: float = 50.0
    income_inequality: float = 50.0
    existing_homeownership: float = 65.0

    # credit conditions
    mortgage_rate: float = 6.5
    mortgage_term: int = 30
    down_payment_req: float = 10.0
    credit_strictness: float = 50.0
    bank_lending_standards: float = 50.0
    arm_share: float = 10.0
    dti_limit: float = 43.0
    refinance_rate: float = 2.0

    # supply side
    zoning_strictness: float = 50.0
    building_regulations: float = 50.0
    density_limits: float = 50.0
    build_costs_index: float = 50.0
    builder_capacity: float = 50.0
    land_scarcity: float = 50.0
    str_share: float = 2.0
    institutional_share: float = 5.0
    demolition_rate: float = 0.2
    commercial_conversion_rate: float = 0.5

    # carrying costs / expectations
    expected_appreciation: float = 4.0
    buyer_willingness: float = 98.0
    seller_reservation: float = 95.0


# individual property object that gets mutated over the sim months
class Property:
    def __init__(self, id_num, base_value, tier):
        self.id = id_num
        self.value = base_value
        self.tier = tier
        self.location = random.choices(["urban", "suburban"], weights=[0.5, 0.5])[0]
        self.size = random.choices(["small", "medium", "large"], weights=[0.3, 0.5, 0.2])[0]
        self.owner_income = base_value / random.uniform(3, 5)
        self.owner_type = "resident"
        self.owner_age = random.choices(["young", "mid", "old"], weights=[0.2, 0.5, 0.3])[0]
        self.has_arm = False
        # starting rate for existing mortgages; this is what creates the lock-in effect
        self.mortgage_rate = 4.0
        self.on_market = False
        self.is_distressed = False
        self.asking_price = 0
        self.months_on_market = 0
        self.transactions = 0


def run_simulation(params: SimulationParams):
    properties = []
    inequality_factor = params.income_inequality / 50.0

    # track population to build real demand pressure against housing stock
    current_population = params.initial_population

    # seed the initial housing stock with a log-normal distribution so we get
    # a realistic spread (lots of mid-range, fewer at the extremes)
    for i in range(params.num_properties):
        val = random.lognormvariate(math.log(350000), 0.6 * inequality_factor)
        val = max(50000, min(val, 10000000))
        tier = "entry" if val < 200000 else "luxury" if val > 800000 else "mid"

        p = Property(i + 1, val, tier)
        if random.random() < (params.arm_share / 100.0):
            p.has_arm = True
        if random.random() < (params.institutional_share / 100.0):
            p.owner_type = "investor"
        elif random.random() < (params.str_share / 100.0):
            p.owner_type = "str"
        properties.append(p)

    history = {"months": [], "avg_price": [], "inventory": [], "transactions": [], "affordability": []}
    current_stock = properties.copy()
    next_id = params.num_properties + 1

    for month in range(1, params.simulation_months + 1):
        transactions_this_month = 0
        market_inventory = 0

        # monthly macro rates derived from annual params
        monthly_pop_growth = (params.pop_growth + params.net_migration) / 100 / 12
        monthly_death_rate = params.death_rate / 100 / 12
        monthly_income_growth = params.income_growth / 100 / 12

        # grow population naturally but subtract deaths to lower absolute demand over time
        current_population *= (1 + monthly_pop_growth - monthly_death_rate)

        # affordability check: can the median buyer actually afford the avg home right now?
        current_avg_price = sum(p.value for p in current_stock) / len(current_stock) if current_stock else 350000
        macro_r = (params.mortgage_rate / 100) / 12
        macro_n = params.mortgage_term * 12
        macro_avg_payment = (current_avg_price * (1 - params.down_payment_req / 100)) * (macro_r * (1 + macro_r) ** macro_n) / (max(0.001, (1 + macro_r) ** macro_n - 1))
        macro_est_income = 85000 * (1 + (month * monthly_income_growth))
        affordability_modifier = macro_est_income / (macro_avg_payment * 12) if macro_avg_payment else 1.0

        # how much regulation is choking new construction this month
        supply_bottleneck = (params.zoning_strictness + params.building_regulations + params.build_costs_index + params.land_scarcity) / 400.0
        density_bonus = params.density_limits / 100.0
        build_rate = (params.builder_capacity / 100.0) * (1.0 - supply_bottleneck) * (1.0 + density_bonus) * 0.05 / 12

        # tighter credit = smaller pool of qualified buyers
        effective_credit_cap = (200 - params.credit_strictness - params.bank_lending_standards) / 100.0

        total_value = 0

        # demolition removes a random unit occasionally
        if random.random() < (params.demolition_rate / 100):
            if current_stock:
                current_stock.pop(random.randint(0, len(current_stock) - 1))

        # new construction + commercial conversions added to the stock
        new_units = int(len(current_stock) * build_rate) + int(len(current_stock) * (params.commercial_conversion_rate / 100 / 12))
        for _ in range(new_units):
            new_val = 300000 * (1 + (params.build_costs_index / 100))
            current_stock.append(Property(next_id, new_val, "mid"))
            next_id += 1

        # main loop: iterate over every property and update its state
        for p in current_stock:
            p.owner_income *= (1 + monthly_income_growth)
            
            # baseline listing probability is inversely scaled by homeownership.
            # high existing homeownership = tighter market, fewer active listings
            list_prob = 0.005 * (65.0 / max(1.0, params.existing_homeownership))
            
            # retirees aging out or downsizing adds a steady, small stream of listings
            list_prob += (params.share_retirees / 100 / 12) * 0.05
            p.is_distressed = False

            # things that push an owner to list their home
            if random.random() < (params.unemployment_rate / 100 * 0.1):
                list_prob += 0.5
                p.is_distressed = True

            # ARM reset: if market rate has jumped well above their locked-in rate, they're in trouble
            if p.has_arm and params.mortgage_rate > (p.mortgage_rate + 2.0):
                list_prob += 0.1
                p.is_distressed = True

            if random.random() < (params.divorce_rate / 100 / 12):
                list_prob += 1.0
            
            # deaths force immediate estate liquidation onto the market
            if random.random() < monthly_death_rate:
                list_prob += 1.0

            # lock-in effect: if rates have gone up a lot owners stay put
            rate_delta = params.mortgage_rate - p.mortgage_rate
            if rate_delta > 1.5 and not p.is_distressed and p.owner_type == "resident":
                list_prob *= 0.2
            elif rate_delta < -1.0 and random.random() < (params.refinance_rate / 100):
                # owner refinances to the new lower rate, breaking the lock-in
                p.mortgage_rate = params.mortgage_rate

            if not p.on_market and random.random() < list_prob:
                p.on_market = True
                p.months_on_market = 0
                expected_premium = 1.0 + (params.expected_appreciation / 100 / 4)
                if p.is_distressed:
                    p.asking_price = p.value * 0.85
                else:
                    p.asking_price = max(p.value * (params.seller_reservation / 100), p.value * expected_premium)

            if p.on_market:
                market_inventory += 1
                p.months_on_market += 1

                # do buyer preferences match this property?
                pref_multiplier = 1.0
                if p.location == "urban" and params.geographic_preference_shift > 60:
                    pref_multiplier *= 1.1
                if p.location == "suburban" and params.geographic_preference_shift < 40:
                    pref_multiplier *= 1.1
                if p.size == "large" and params.size_preference_shift > 60:
                    pref_multiplier *= 1.1
                if p.size == "small" and params.size_preference_shift < 40:
                    pref_multiplier *= 1.1

                # figure out how much a typical buyer can actually spend on this unit
                if p.tier == "entry" and random.random() < (params.institutional_share / 100):
                    # institutional investor just writes a check at asking
                    buyer_budget = p.asking_price
                else:
                    employment_factor = 1.0 - (params.unemployment_rate / 100)
                    base_buyer_income = random.lognormvariate(math.log(p.value / 4), 0.5) * (1 + monthly_pop_growth) * employment_factor
                    max_payment = base_buyer_income * (params.dti_limit / 100) / 12
                    max_loan = max_payment / (((params.mortgage_rate / 100) / 12) + 0.0001)
                    buyer_budget = ((max_loan + (base_buyer_income * (params.wealth_savings_index / 100))) * effective_credit_cap) * pref_multiplier

                    if p.tier == "entry":
                        buyer_budget *= (1 + (params.first_time_buyers / 100))

                # does the deal close?
                if buyer_budget >= (p.asking_price * (params.buyer_willingness / 100)):
                    # buyer pays asking, not some fraction of their budget
                    p.value = min(p.asking_price, buyer_budget)
                    p.on_market = False
                    p.months_on_market = 0
                    p.owner_income = p.value / 4
                    p.mortgage_rate = params.mortgage_rate
                    p.transactions += 1
                    transactions_this_month += 1
                elif p.months_on_market > 2:
                    # stale listing, seller drops price and it becomes the new baseline
                    p.asking_price *= 0.95
                    p.value = p.asking_price

            # monthly value drift based on demand vs supply pressure
            employment_factor = max(0.1, 1.0 - (params.unemployment_rate / 100))
            
            # calculate housing shortage/surplus assuming ~2.5 people per home baseline
            housing_capacity = max(1, len(current_stock)) * 2.5
            population_pressure = (current_population / housing_capacity) - 1.0
            
            # young adults turbocharge household formation, driving demand up
            young_adult_multiplier = max(0.1, params.share_young_adults / 30.0)
            effective_formation = (params.household_formation / 100 / 12) * young_adult_multiplier
            
            demand_pressure = ((affordability_modifier * 0.5) + population_pressure + effective_formation) * employment_factor

            # no cap here on purpose; if inventory piles up without buyers prices should be able to crash
            inventory_ratio = market_inventory / max(1, len(current_stock))
            supply_pressure = inventory_ratio * 40.0

            p.value *= (1 + (demand_pressure - supply_pressure) * 0.005)
            total_value += p.value

        avg_price = total_value / len(current_stock) if current_stock else 0

        # compute the affordability index for this month's data point
        r = (params.mortgage_rate / 100) / 12
        n = params.mortgage_term * 12
        avg_payment = (avg_price * (1 - params.down_payment_req / 100)) * (r * (1 + r) ** n) / (max(0.001, (1 + r) ** n - 1))
        est_avg_income = 85000 * (1 + (month * monthly_income_growth))
        affordability_idx = est_avg_income / (avg_payment * 12) * 100 if avg_payment else 0

        history["months"].append(f"Mo {month}")
        history["avg_price"].append(round(avg_price))
        history["inventory"].append(market_inventory)
        history["transactions"].append(transactions_this_month)
        history["affordability"].append(round(affordability_idx, 1))

    prop_data = [{
        "id": p.id,
        "type": f"{p.size.capitalize()} {p.location.capitalize()} ({p.tier})",
        "rate": f"{p.mortgage_rate:.1f}%",
        "value": round(p.value, 2),
        "on_market": "Yes (Distressed)" if (p.on_market and p.is_distressed) else ("Yes" if p.on_market else "No"),
        "asking_price": round(p.asking_price, 2) if p.on_market else 0,
        "dom": p.months_on_market,
        "sales": p.transactions
    } for p in current_stock]

    return {"history": history, "properties": prop_data}


@app.post("/api/simulate")
def api_simulate(params: SimulationParams):
    return run_simulation(params)


class ChatRequest(BaseModel):
    messages: list


@app.post("/api/chat")
def api_chat(req: ChatRequest):
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return {"content": "Error: OPENROUTER_API_KEY is not set in the .env file.", "reasoning_details": None}
    models = [
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
    ]
    last_error = None
    for model in models:
        try:
            resp = requests.post(
                url="https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": "Bearer " + api_key,
                    "Content-Type": "application/json",
                },
                data=json.dumps({
                    "model": model,
                    "messages": req.messages,
                    "reasoning": {"enabled": True}
                }),
                timeout=120
            )
            resp.raise_for_status()
            message = resp.json()["choices"][0]["message"]
            return {"content": message.get("content"), "reasoning_details": message.get("reasoning_details")}
        except Exception as e:
            last_error = e
    return {"content": "Error: " + str(last_error), "reasoning_details": None}


@app.get("/")
def get_ui():
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Comprehensive Real Estate Engine</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body { font-family: Arial, Helvetica, sans-serif; background: #ffffff; margin: 0; padding: 20px; color: #000000; }
            .layout { display: flex; gap: 20px; max-width: 1800px; margin: 0 auto; align-items: flex-start; }
            .sidebar { background: #ffffff; border: 2px solid #000000; padding: 20px; width: 450px; flex-shrink: 0; max-height: 90vh; overflow-y: auto; }
            .sidebar h2 { margin-top: 0; font-size: 1.2rem; border-bottom: 3px solid #000000; padding-bottom: 10px; position: sticky; top: 0; background: #ffffff; z-index: 100; text-transform: uppercase; letter-spacing: 1px; }
            .category { margin-top: 20px; border: 2px solid #000000; background: #ffffff; }
            .category h3 { margin: 0; padding: 10px 12px; font-size: 0.9rem; color: #ffffff; background: #000000; text-transform: uppercase; letter-spacing: 1px; }
            .form-group { margin-bottom: 12px; padding: 0 12px; }
            .form-group:first-of-type { margin-top: 12px; }
            label { display: flex; justify-content: space-between; font-weight: 700; font-size: 0.85rem; margin-bottom: 5px; }
            small { display: block; font-size: 0.7rem; color: #555555; margin-bottom: 5px; line-height: 1.2; }
            input { width: 100%; padding: 6px; border: 2px solid #000000; border-radius: 0; box-sizing: border-box; font-size: 0.85rem; }
            .main { flex: 1; min-width: 0; }
            .header-panel { background: #000000; color: #ffffff; padding: 20px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }
            .header-panel button { background: #ffffff; color: #000000; border: 2px solid #000000; padding: 12px 24px; border-radius: 0; font-weight: 700; cursor: pointer; text-transform: uppercase; letter-spacing: 1px; }
            .header-panel button:hover { background: #000000; color: #ffffff; }
            .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
            .chart-card { background: #ffffff; border: 2px solid #000000; padding: 15px; }
            .table-card { background: #ffffff; border: 2px solid #000000; padding: 20px; }
            .table-wrapper { max-height: 400px; overflow-y: auto; margin-top: 15px; border-top: 2px solid #000000; }
            table { width: 100%; border-collapse: collapse; text-align: left; }
            th, td { padding: 10px; border-bottom: 1px solid #000000; font-size: 0.8rem; }
            th { background: #000000; color: #ffffff; position: sticky; top: 0; font-weight: 700; }
            .chat-card { background: #ffffff; border: 2px solid #000000; padding: 20px; margin-top: 20px; }
            .chat-messages { max-height: 320px; overflow-y: auto; margin: 15px 0; padding: 12px; background: #ffffff; border: 2px solid #000000; display: flex; flex-direction: column; }
            .chat-msg { margin-bottom: 10px; padding: 8px 12px; border: 1px solid #000000; font-size: 0.85rem; line-height: 1.4; white-space: pre-wrap; word-break: break-word; }
            .chat-user { background: #000000; color: #ffffff; align-self: flex-end; max-width: 80%; }
            .chat-bot { background: #ffffff; color: #000000; align-self: flex-start; max-width: 80%; }
            .chat-input-row { display: flex; gap: 10px; }
            .chat-input-row input { flex: 1; padding: 10px; border: 2px solid #000000; border-radius: 0; }
            .chat-input-row button { background: #000000; color: #ffffff; border: 2px solid #000000; padding: 10px 22px; border-radius: 0; font-weight: 700; cursor: pointer; text-transform: uppercase; }
            .chat-input-row button:disabled { background: #cccccc; color: #666666; border-color: #999999; cursor: not-allowed; }
        </style>
    </head>
    <body>
        <div class="layout">
            <div class="sidebar" id="sidebar">
                <h2>Market Variables</h2>
            </div>

            <div class="main">
                <div class="header-panel">
                    <div>
                        <h1 style="margin: 0 0 5px 0;">Market Outcomes & Interdependencies</h1>
                        <div style="font-size: 0.9rem; color: #cbd5e1;">Lock-ins, geographic shifts, and credit crunches dynamically restrict supply and demand.</div>
                    </div>
                    <button onclick="runSimulation()">Run Simulation</button>
                </div>

                <div class="charts-grid">
                    <div class="chart-card"><canvas id="chartPrice"></canvas></div>
                    <div class="chart-card"><canvas id="chartInv"></canvas></div>
                    <div class="chart-card"><canvas id="chartTrans"></canvas></div>
                    <div class="chart-card"><canvas id="chartAfford"></canvas></div>
                </div>

                <div class="table-card">
                    <h3>Simulated Property Ledger</h3>
                    <div class="table-wrapper">
                        <table>
                            <thead>
                                <tr><th>ID</th><th>Type</th><th>Current Rate</th><th>Value</th><th>On Market</th><th>Ask Price</th><th>DOM</th><th>Sales</th></tr>
                            </thead>
                            <tbody id="tableBody"></tbody>
                        </table>
                    </div>
                </div>

                <div class="chat-card">
                    <h3>Ask the Analyst</h3>
                    <div class="chat-messages" id="chatMessages"></div>
                    <div class="chat-input-row">
                        <input type="text" id="chatInput" placeholder="Ask about the housing market data..." onkeydown="if(event.key==='Enter') askBot()">
                        <button id="chatSendBtn" onclick="askBot()">Send</button>
                    </div>
                </div>
            </div>
        </div>

        <script>
            const formConfig = [
                {
                    category: "Core Scope & Geographics",
                    fields: [
                        { id: "num_properties", label: "Number of Properties", default: 500, desc: "Total housing stock size." },
                        { id: "simulation_months", label: "Sim Length (Months)", default: 24, desc: "Time horizon." },
                        { id: "geographic_preference_shift", label: "Location Pref (0 Sub / 100 Urb)", default: 50.0, desc: "Shifts budget multipliers by location." },
                        { id: "size_preference_shift", label: "Size Pref (0 Small / 100 Large)", default: 50.0, desc: "Shifts demand toward larger footprints." }
                    ]
                },
                {
                    category: "Demographics",
                    fields: [
                        { id: "initial_population", label: "Initial Population", default: 1250, desc: "Baseline population size driving demand against housing stock." },
                        { id: "pop_growth", label: "Population Growth (%)", default: 1.0, desc: "Overall demand baseline." },
                        { id: "share_young_adults", label: "Share of Young Adults (%)", default: 30.0, desc: "Drives household formation and buyer demand." },
                        { id: "share_retirees", label: "Share of Retirees (%)", default: 20.0, desc: "Increases baseline turnover from downsizing/aging." },
                        { id: "existing_homeownership", label: "Existing Homeownership (%)", default: 65.0, desc: "High rates restrict active supply and tighten the market." },
                        { id: "death_rate", label: "Death Rate (%)", default: 0.8, desc: "Reduces population and forces estate liquidations." },
                        { id: "divorce_rate", label: "Divorce Rate (%)", default: 2.5, desc: "Forces property liquidation." },
                        { id: "unemployment_rate", label: "Unemployment Rate (%)", default: 5.0, desc: "Triggers distress sales and shrinks the buyer pool." }
                    ]
                },
                {
                    category: "Credit & Financing",
                    fields: [
                        { id: "mortgage_rate", label: "Current Market Rate (%)", default: 6.5, desc: "Drives lock-in effects and buyer purchasing power." },
                        { id: "refinance_rate", label: "Refinancing Rate (%)", default: 2.0, desc: "How fast owners adopt lower market rates." },
                        { id: "bank_lending_standards", label: "Lending Strictness (1-100)", default: 50.0, desc: "Caps buyer budgets when high." },
                        { id: "arm_share", label: "ARM Share (%)", default: 10.0, desc: "High ARMs + rising rates = defaults." }
                    ]
                },
                {
                    category: "Supply Capacity",
                    fields: [
                        { id: "zoning_strictness", label: "Zoning Strictness (1-100)", default: 50.0, desc: "Bottlenecks new construction." },
                        { id: "density_limits", label: "Density Limits (1-100)", default: 50.0, desc: "Caps units per acre." },
                        { id: "commercial_conversion_rate", label: "Commercial Conversion (%)", default: 0.5, desc: "Adds alternative supply to the market." }
                    ]
                }
            ];

            const sidebar = document.getElementById('sidebar');
            formConfig.forEach(cat => {
                let html = `<div class="category"><h3>${cat.category}</h3>`;
                cat.fields.forEach(f => {
                    html += `
                    <div class="form-group">
                        <label for="${f.id}"><span>${f.label}</span></label>
                        <small>${f.desc}</small>
                        <input type="number" id="${f.id}" step="any" value="${f.default}">
                    </div>`;
                });
                html += `</div>`;
                sidebar.insertAdjacentHTML('beforeend', html);
            });

            let charts = {};
            let simData = null;
            let chatHistory = [];
            let lastInputs = null;
            const MAIN_PROMPT = "You are a macro real-estate explanation bot. Answer all user questions according to the provided data. You only answer questions about real estate, the housing market, etc. Remind the user that your answers are not guaranteed to be accurate. Respond in 6 sentences or less.";

            async function runSimulation() {
                const payload = {};
                formConfig.forEach(cat => cat.fields.forEach(f => payload[f.id] = parseFloat(document.getElementById(f.id).value) || 0));

                lastInputs = payload;
                const res = await fetch('/api/simulate', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
                const data = await res.json();
                simData = data;

                draw('chartPrice', 'Avg Sale Price ($)', data.history.avg_price, data.history.months, '#3b82f6');
                draw('chartInv', 'Active Inventory (Units)', data.history.inventory, data.history.months, '#f59e0b');
                draw('chartTrans', 'Monthly Closed Sales', data.history.transactions, data.history.months, '#10b981');
                draw('chartAfford', 'Affordability Index', data.history.affordability, data.history.months, '#8b5cf6');

                const tbody = document.getElementById('tableBody');
                tbody.innerHTML = data.properties.map(p => `
                    <tr>
                        <td>#${p.id}</td><td>${p.type}</td><td>${p.rate}</td>
                        <td>$${p.value.toLocaleString(undefined, {maximumFractionDigits:0})}</td>
                        <td style="color: ${p.on_market.includes('Distressed') ? '#ef4444' : 'inherit'}">${p.on_market}</td>
                        <td>${p.asking_price > 0 ? '$'+p.asking_price.toLocaleString(undefined, {maximumFractionDigits:0}) : '-'}</td>
                        <td>${p.dom}</td><td>${p.sales}</td>
                    </tr>`).join('');
            }

            // helper to format month-over-month % changes for the prompt
            function pctSeries(arr) {
                return arr.slice(1).map((v, i) => {
                    const prev = arr[i];
                    const pct = prev ? ((v - prev) / prev) * 100 : 0;
                    return (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%';
                }).join(', ');
            }

            function buildDataSummary() {
                if (!simData) return "No simulation data available yet.";
                const h = simData.history;
                const lines = [];
                lines.push("SIMULATION INPUT PARAMETERS: " + JSON.stringify(lastInputs));
                lines.push("MONTH-BY-MONTH % CHANGE IN AVG SALE PRICE (month 2 onward): " + pctSeries(h.avg_price));
                lines.push("MONTH-BY-MONTH % CHANGE IN ACTIVE INVENTORY (month 2 onward): " + pctSeries(h.inventory));
                lines.push("MONTH-BY-MONTH % CHANGE IN CLOSED SALES (month 2 onward): " + pctSeries(h.transactions));
                lines.push("MONTH-BY-MONTH % CHANGE IN AFFORDABILITY INDEX (month 2 onward): " + pctSeries(h.affordability));
                const p0 = h.avg_price[0], pN = h.avg_price[h.avg_price.length - 1];
                const a0 = h.affordability[0], aN = h.affordability[h.affordability.length - 1];
                const totalSales = h.transactions.reduce((s, v) => s + v, 0);
                lines.push("OVERALL (" + h.months.length + " months): Avg sale price went from $" + p0.toLocaleString() + " to $" + pN.toLocaleString() + " (" + (((pN - p0) / p0) * 100).toFixed(1) + "% total). Affordability index went from " + a0 + " to " + aN + ". Total closed sales: " + totalSales + ". Final month inventory: " + h.inventory[h.inventory.length - 1] + " units.");
                return lines.join("\\n");
            }

            function addMsg(text, cls) {
                const box = document.getElementById('chatMessages');
                const div = document.createElement('div');
                div.className = 'chat-msg ' + cls;
                div.textContent = text;
                box.appendChild(div);
                box.scrollTop = box.scrollHeight;
            }

            async function askBot() {
                const input = document.getElementById('chatInput');
                const btn = document.getElementById('chatSendBtn');
                const q = input.value.trim();
                if (!q || btn.disabled) return;
                addMsg(q, 'chat-user');
                input.value = '';
                btn.disabled = true;

                const messages = [{ role: "system", content: MAIN_PROMPT + "\\n\\nMARKET DATA:\\n" + buildDataSummary() }];
                chatHistory.forEach(m => messages.push(m));
                messages.push({ role: "user", content: q });

                try {
                    const res = await fetch('/api/chat', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ messages: messages })
                    });
                    const data = await res.json();
                    const reply = data.content || 'No response received.';
                    addMsg(reply, 'chat-bot');
                    chatHistory.push({ role: "user", content: q });
                    const botMsg = { role: "assistant", content: reply };
                    if (data.reasoning_details) botMsg.reasoning_details = data.reasoning_details;
                    chatHistory.push(botMsg);
                } catch (err) {
                    addMsg('Error: could not reach the AI service. Please try again.', 'chat-bot');
                }
                btn.disabled = false;
            }

            function draw(id, label, dataArr, labelsArr, color) {
                const ctx = document.getElementById(id).getContext('2d');
                if (charts[id]) charts[id].destroy();
                charts[id] = new Chart(ctx, {
                    type: 'line',
                    data: { labels: labelsArr, datasets: [{ label: label, data: dataArr, borderColor: color, backgroundColor: color + '22', fill: true, tension: 0.2 }] },
                    options: { responsive: true, maintainAspectRatio: false }
                });
            }

            // run once on load so the page isn't blank
            runSimulation();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)