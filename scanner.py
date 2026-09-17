import os
import sys
import json
import pandas as pd
import requests
from datetime import datetime, timezone

from google import genai
from google.genai import types


# ============================================================
# NSE OPTION CHAIN
# ============================================================

def fetch_nse_option_chain(symbol, mode):
    """
    Fetch raw option-chain data from NSE India.
    """

    headers = {
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "accept-encoding": "gzip, deflate, br",
        "accept-language": "en-US,en;q=0.9",
    }

    session = requests.Session()

    try:
        # First request establishes NSE cookies.
        session.get(
            "https://www.nseindia.com",
            headers=headers,
            timeout=5,
        )

        url = (
            f"https://www.nseindia.com/api/option-chain/"
            f"{mode}?symbol={symbol}"
        )

        response = session.get(
            url,
            headers=headers,
            cookies=dict(session.cookies),
            timeout=10,
        )

        response.raise_for_status()

        res_json = response.json()

        records = res_json.get("records", {})

        expiries = records.get("expiryDates", [])
        spot_price = records.get("underlyingValue")

        return (
            records.get("data", []),
            expiries,
            spot_price,
        )

    except Exception as e:
        print(
            f"❌ Core API Sync Failure for {symbol}: {e}"
        )

        return None, None, None


# ============================================================
# OPTIONS MATRIX CALCULATOR
# ============================================================

def process_singularity_matrix(symbol, mode):
    """
    Processes the NSE option chain and calculates:

    - PCR based on volume
    - PCR based on open interest
    - Expected resistance
    - Expected support
    - Resistance strength
    - Support strength
    """

    all_market_data, expiry_list, spot_price = (
        fetch_nse_option_chain(symbol, mode)
    )

    if not tragedy_guard(
        all_market_data,
        expiry_list,
        spot_price,
    ):
        return None

    selected_expiry = expiry_list[0]

    filtered_list = [
        d
        for d in all_market_data
        if d.get("expiryDate") == selected_expiry
    ]

    data_list = []

    total_ce_oi = 0
    total_pe_oi = 0
    total_ce_vol = 0
    total_pe_vol = 0

    for data in filtered_list:

        if "CE" not in data or "PE" not in data:
            continue

        strike = data["strikePrice"]

        ce_oi = data["CE"].get(
            "openInterest",
            0,
        )

        pe_oi = data["PE"].get(
            "openInterest",
            0,
        )

        ce_vol = data["CE"].get(
            "totalTradedVolume",
            0,
        )

        pe_vol = data["PE"].get(
            "totalTradedVolume",
            0,
        )

        total_ce_oi += ce_oi
        total_pe_oi += pe_oi

        total_ce_vol += ce_vol
        total_pe_vol += pe_vol

        data_list.append(
            {
                "Strike": strike,

                "CE_Vol": ce_vol,
                "CE_OI": ce_oi,
                "CE_LTP": data["CE"].get(
                    "lastPrice",
                    0,
                ),

                "PE_Vol": pe_vol,
                "PE_OI": pe_oi,
                "PE_LTP": data["PE"].get(
                    "lastPrice",
                    0,
                ),
            }
        )

    df = pd.DataFrame(data_list)

    if df.empty:
        return None

    # ========================================================
    # STRIKE STEP
    # ========================================================

    sorted_strikes = (
        df.sort_values("Strike")["Strike"]
    )

    differences = (
        sorted_strikes.diff()
        .dropna()
    )

    step = (
        int(differences.iloc[0])
        if not differences.empty
        else 50
    )

    if step <= 0:
        step = 50

    # ========================================================
    # ATM
    # ========================================================

    atm_strike = (
        round(spot_price / step) * step
    )

    # Five strikes on either side of ATM.
    zone_df = df[
        (df["Strike"] >= atm_strike - step * 5)
        &
        (df["Strike"] <= atm_strike + step * 5)
    ].copy()

    if zone_df.empty:
        return None

    # ========================================================
    # RESISTANCE
    # ========================================================

    ce_vol_max = (
        zone_df
        .sort_values("CE_Vol", ascending=False)
        .iloc[0]
    )

    ce_oi_max = (
        zone_df
        .sort_values("CE_OI", ascending=False)
        .iloc[0]
    )

    res_strike = ce_vol_max["Strike"]
    res_type = "Vol"
    max_val_ce = ce_vol_max["CE_Vol"]

    if ce_oi_max["CE_OI"] > max_val_ce:
        res_strike = ce_oi_max["Strike"]
        res_type = "OI"
        max_val_ce = ce_oi_max["CE_OI"]

    # ========================================================
    # SUPPORT
    # ========================================================

    pe_vol_max = (
        zone_df
        .sort_values("PE_Vol", ascending=False)
        .iloc[0]
    )

    pe_oi_max = (
        zone_df
        .sort_values("PE_OI", ascending=False)
        .iloc[0]
    )

    sup_strike = pe_vol_max["Strike"]
    sup_type = "Vol"
    max_val_pe = pe_vol_max["PE_Vol"]

    if pe_oi_max["PE_OI"] > max_val_pe:
        sup_strike = pe_oi_max["Strike"]
        sup_type = "OI"
        max_val_pe = pe_oi_max["PE_OI"]

    # ========================================================
    # SECOND LEVELS
    # ========================================================

    ce_metric = (
        "CE_Vol"
        if res_type == "Vol"
        else "CE_OI"
    )

    pe_metric = (
        "PE_Vol"
        if sup_type == "Vol"
        else "PE_OI"
    )

    ce_2nd = (
        zone_df[
            zone_df["Strike"] != res_strike
        ]
        .sort_values(
            ce_metric,
            ascending=False,
        )
    )

    pe_2nd = (
        zone_df[
            zone_df["Strike"] != sup_strike
        ]
        .sort_values(
            pe_metric,
            ascending=False,
        )
    )

    # ========================================================
    # STRENGTH
    # ========================================================

    ce_weakness = (
        (
            ce_2nd.iloc[0][ce_metric]
            / max_val_ce
        )
        * 100
        if max_val_ce > 0 and not ce_2nd.empty
        else 0
    )

    pe_weakness = (
        (
            pe_2nd.iloc[0][pe_metric]
            / max_val_pe
        )
        * 100
        if max_val_pe > 0 and not pe_2nd.empty
        else 0
    )

    # ========================================================
    # RESISTANCE STATUS
    # ========================================================

    if ce_weakness <= 75:
        res_status = "STRONG"
    else:
        res_status = (
            f"WTT {ce_weakness:.0f}%"
            if ce_2nd.iloc[0]["Strike"] > res_strike
            else f"WTB {ce_weakness:.0f}%"
        )

    # ========================================================
    # SUPPORT STATUS
    # ========================================================

    if pe_weakness <= 75:
        sup_status = "STRONG"
    else:
        sup_status = (
            f"WTT {pe_weakness:.0f}%"
            if pe_2nd.iloc[0]["Strike"] > sup_strike
            else f"WTB {pe_weakness:.0f}%"
        )

    # ========================================================
    # EOR / EOS
    # ========================================================

    res_row = zone_df[
        zone_df["Strike"] == res_strike
    ]

    sup_row = zone_df[
        zone_df["Strike"] == sup_strike
    ]

    if not res_row.empty:
        eor = (
            res_strike
            + res_row["CE_LTP"].values[0]
        )
    else:
        eor = res_strike

    if not sup_row.empty:
        eos = (
            sup_strike
            - sup_row["PE_LTP"].values[0]
        )
    else:
        eos = sup_strike

    # ========================================================
    # PCR
    # ========================================================

    pcr_oi = (
        total_pe_oi / total_ce_oi
        if total_ce_oi > 0
        else 1.0
    )

    pcr_vol = (
        total_pe_vol / total_ce_vol
        if total_ce_vol > 0
        else 1.0
    )

    # ========================================================
    # FINAL METRICS
    # ========================================================

    return {
        "Asset": symbol,

        "Spot": round(
            float(spot_price),
            2,
        ),

        "Expiry": selected_expiry,

        "ATM": int(atm_strike),

        "PCR_Vol": round(
            float(pcr_vol),
            4,
        ),

        "PCR_OI": round(
            float(pcr_oi),
            4,
        ),

        "EOR": round(
            float(eor),
            2,
        ),

        "EOR_Type": res_type,

        "EOR_Status": res_status,

        "EOS": round(
            float(eos),
            2,
        ),

        "EOS_Type": sup_type,

        "EOS_Status": sup_status,

        "CE_Total_OI": int(total_ce_oi),

        "PE_Total_OI": int(total_pe_oi),

        "CE_Total_Volume": int(total_ce_vol),

        "PE_Total_Volume": int(total_pe_vol),

        "Generated_At": datetime.now(
            timezone.utc
        ).isoformat(),
    }


# ============================================================
# VALIDATION
# ============================================================

def tragedy_guard(data, expiries, spot):

    return (
        bool(data)
        and bool(expiries)
        and isinstance(
            spot,
            (int, float),
        )
    )


# ============================================================
# GEMINI INTELLIGENCE
# ============================================================

def run_gemini_analysis(metrics):

    api_key = os.environ.get(
        "GEMINI_API_KEY"
    )

    if not api_key:
        print(
            "⚠️ GEMINI_API_KEY is missing."
        )
        return None

    try:

        client = genai.Client(
            api_key=api_key
        )

        asset = metrics["Asset"]

        metrics_json = json.dumps(
            metrics,
            indent=2,
        )

        prompt = f"""
You are an advanced financial-market research
and analysis engine.

Analyze {asset} using the REAL-TIME NSE
options-chain data provided below.

==================================================
QUANTITATIVE DATA
==================================================

{metrics_json}

==================================================
OBJECTIVE
==================================================

Combine the quantitative options-chain structure
with CURRENT real-world information.

You must research current information using
Google Search.

The objective is to determine:

1. What the options structure is indicating.
2. What is currently happening around the company,
   sector or index.
3. Whether current events strengthen, weaken or
   complicate the quantitative setup.
4. What geopolitical and macroeconomic factors
   could materially affect the asset.

Do NOT simply produce a "BUY" or "SELL" answer.

==================================================
RESEARCH
==================================================

For a COMPANY, investigate where relevant:

- Latest earnings
- Revenue/profit developments
- Management commentary
- Major contracts
- Major customers
- Acquisitions
- Partnerships
- Expansion
- Layoffs
- Company policies
- Strategic decisions
- Regulatory developments
- Sector developments
- Competitor developments

For an INDEX, investigate:

- Major constituent developments
- Sector movements
- RBI policy
- Interest rates
- Inflation
- INR/USD
- US economic developments
- Global markets
- Commodity prices
- Foreign institutional flows where relevant
- Indian regulatory developments

==================================================
GEOPOLITICAL ANALYSIS
==================================================

Consider:

- Wars and conflicts
- Sanctions
- Tariffs
- Trade restrictions
- Diplomatic developments
- Supply-chain disruptions
- Energy shocks
- Country-specific risks
- International regulatory changes

BUT:

Do not mention geopolitical news merely because
it is currently in the headlines.

There must be a plausible mechanism connecting:

Geopolitical event
        ↓
Trade / regulation / supply chain /
currency / demand / costs
        ↓
Company / sector / index
        ↓
Potential market impact

If there is no meaningful connection,
say so explicitly.

==================================================
QUANTITATIVE ANALYSIS
==================================================

Interpret ONLY the indicators supplied.

Important metrics:

PCR_Vol
PCR_OI
EOR
EOS
EOR_Status
EOS_Status
ATM
Spot

Do NOT invent RSI, MACD, SMA, EMA or other
technical indicators.

Do NOT calculate additional indicators unless
the required raw data is explicitly available.

==================================================
EVIDENCE RULES
==================================================

Clearly distinguish:

FACT
INTERPRETATION
POSSIBLE MARKET IMPACT

Do not present speculation as fact.

Do not manufacture news.

Prefer recent information.

If reliable current information is unavailable,
say so.

Do not assume:

positive news = positive stock movement

or:

negative news = negative stock movement.

Explain the mechanism.

==================================================
OUTPUT FORMAT
==================================================

📊 QUANTITATIVE READ

Explain what the options structure indicates.

📰 CURRENT DEVELOPMENTS

Summarize the most relevant recent developments.

🌍 GEOPOLITICAL & MACRO

Explain relevant geopolitical, economic and
regulatory factors.

🏢 BUSINESS IMPACT

Explain how those developments could affect
the company, sector or index.

⚖️ UPSIDE FACTORS

List factors that could support upward movement.

⚠️ DOWNSIDE FACTORS

List factors that could create downward pressure.

🎯 OPTIONS + NEWS SYNTHESIS

Combine the options structure with the
current information environment.

🔎 KEY RISK

Identify the most important uncertainty or
event that could invalidate the interpretation.

📌 CONCLUSION

Give a concise evidence-based interpretation.

Do NOT provide guaranteed predictions.

Do NOT claim certainty.

Use current web sources and citations where
available.
"""

        response = client.models.generate_content(
            model="gemini-3.8-flash",

            contents=prompt,

            config=types.GenerateContentConfig(

                tools=[
                    types.Tool(
                        google_search=types.GoogleSearch()
                    )
                ],

                thinking_config=types.ThinkingConfig(
                    thinking_level="medium"
                ),
            ),
        )

        return response.text

    except Exception as e:

        print(
            f"❌ Gemini analysis failed: {e}"
        )

        return None


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram_message(text):

    bot_token = os.environ.get(
        "TELEGRAM_BOT_TOKEN"
    )

    chat_id = os.environ.get(
        "TELEGRAM_CHAT_ID"
    )

    if not bot_token or not chat_id:

        print(
            "⚠️ Telegram credentials missing."
        )

        return

    url = (
        f"https://api.telegram.org/"
        f"bot{bot_token}/sendMessage"
    )

    # Telegram allows roughly 4096 characters.
    # Keep a safety margin.
    chunks = [
        text[i:i + 3900]
        for i in range(
            0,
            len(text),
            3900,
        )
    ]

    for chunk in chunks:

        try:

            response = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                },
                timeout=10,
            )

            if not response.ok:

                print(
                    "❌ Telegram API error:"
                )

                print(
                    response.text
                )

        except Exception as e:

            print(
                f"❌ Telegram network error: {e}"
            )


# ============================================================
# ORIGINAL QUANTITATIVE TELEGRAM ALERT
# ============================================================

def send_matrix_telegram_alert(metrics):

    if not metrics:
        return

    signal = (
        "🟢 VOL BREAKOUT SUPPORTED"
        if metrics["PCR_Vol"] > 1.0
        else
        "🔴 LIQUIDITY RESISTANCE BLOCKED"
    )

    msg = (

        "👑 LTP CALCULATOR PRO — ALPHA GRID\n"
        "Asset Alpha Matrix Dashboard\n"
        "──────────────────────────────────\n"

        f"Instrument: "
        f"{metrics['Asset']} "
        f"({metrics['Expiry']})\n"

        f"Spot Price: "
        f"₹{metrics['Spot']:.2f}\n"

        f"ATM: "
        f"{metrics['ATM']}\n"

        "──────────────────────────────────\n"

        f"📊 PCR Vol: "
        f"{metrics['PCR_Vol']:.2f}\n"

        f"📊 PCR OI: "
        f"{metrics['PCR_OI']:.2f}\n"

        f"🛑 EoR ({metrics['EOR_Type']}): "
        f"{metrics['EOR']:.2f}\n"

        f"   Status: "
        f"{metrics['EOR_Status']}\n"

        f"🟢 EoS ({metrics['EOS_Type']}): "
        f"{metrics['EOS']:.2f}\n"

        f"   Status: "
        f"{metrics['EOS_Status']}\n"

        "──────────────────────────────────\n"

        f"💡 Signal: {signal}"
    )

    send_telegram_message(msg)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    raw_input = os.environ.get(
        "TELEGRAM_INPUT_TICKER",
        "",
    ).strip().upper()

    if not raw_input and len(sys.argv) > 1:

        raw_input = (
            sys.argv[1]
            .strip()
            .upper()
        )

    # Default to NIFTY.
    target_asset = (
        raw_input
        if raw_input
        else "NIFTY"
    )

    # NSE endpoint selection.
    api_mode = (
        "indices"
        if target_asset in [
            "NIFTY",
            "BANKNIFTY",
            "FINNIFTY",
        ]
        else
        "equities"
    )

    print(
        f"🔎 Scanning {target_asset}..."
    )

    # ========================================================
    # 1. QUANTITATIVE SCAN
    # ========================================================

    matrix_metrics = (
        process_singularity_matrix(
            target_asset,
            api_mode,
        )
    )

    if not matrix_metrics:

        print(
            "⚠️ Matrix processing halted."
        )

        sys.exit(1)

    print(
        "✅ Quantitative scan completed."
    )

    # ========================================================
    # 2. SEND ORIGINAL SCANNER RESULT
    # ========================================================

    send_matrix_telegram_alert(
        matrix_metrics
    )

    # ========================================================
    # 3. GEMINI + CURRENT WEB RESEARCH
    # ========================================================

    print(
        "🤖 Starting Gemini intelligence analysis..."
    )

    ai_analysis = run_gemini_analysis(
        matrix_metrics
    )

    if ai_analysis:

        ai_message = (
            "🤖 GEMINI MARKET INTELLIGENCE\n"
            f"{target_asset}\n"
            "──────────────────────────"