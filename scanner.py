import os
import sys
import json
import numpy as np
import pandas as pd
import requests
from datetime import datetime

from google import genai


# ============================================================
# NSE OPTION CHAIN
# ============================================================

def fetch_nse_option_chain(symbol, mode):
    """Fetch raw option-chain data from NSE India."""

    headers = {
        'user-agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        ),
        'accept-encoding': 'gzip, deflate, br',
        'accept-language': 'en-US,en;q=0.9'
    }

    session = requests.Session()

    try:
        # Populate NSE cookies first
        session.get(
            "https://www.nseindia.com",
            headers=headers,
            timeout=5
        )

        url = (
            f"https://www.nseindia.com/api/option-chain/"
            f"{mode}?symbol={symbol}"
        )

        response = session.get(
            url,
            headers=headers,
            cookies=dict(session.cookies),
            timeout=10
        )

        response.raise_for_status()

        res_json = response.json()

        records = res_json.get('records', {})

        expiries = records.get('expiryDates', [])
        spot_price = records.get('underlyingValue')

        return (
            records.get('data', []),
            expiries,
            spot_price
        )

    except Exception as e:
        print(f"❌ Core API Sync Failure for {symbol}: {e}")

        return None, None, None


# ============================================================
# OPTIONS MATRIX CALCULATOR
# ============================================================

def process_singularity_matrix(symbol, mode):
    """
    Processes NSE option-chain data and calculates:

    - PCR based on volume
    - PCR based on open interest
    - Expected resistance
    - Expected support
    - Structural strength / weakness
    """

    all_market_data, expiry_list, spot_price = fetch_nse_option_chain(
        symbol,
        mode
    )

    if not tragedy_guard(
        all_market_data,
        expiry_list,
        spot_price
    ):
        return None

    selected_expiry = expiry_list[0]

    filtered_list = [
        d for d in all_market_data
        if d.get('expiryDate') == selected_expiry
    ]

    data_list = []

    total_ce_oi = 0
    total_pe_oi = 0
    total_ce_vol = 0
    total_pe_vol = 0

    for data in filtered_list:

        if 'CE' in data and 'PE' in data:

            strike = data['strikePrice']

            ce_oi = data['CE'].get('openInterest', 0)
            pe_oi = data['PE'].get('openInterest', 0)

            ce_vol = data['CE'].get('totalTradedVolume', 0)
            pe_vol = data['PE'].get('totalTradedVolume', 0)

            total_ce_oi += ce_oi
            total_pe_oi += pe_oi

            total_ce_vol += ce_vol
            total_pe_vol += pe_vol

            data_list.append({
                'Strike': strike,

                'CE_Vol': ce_vol,
                'CE_OI': ce_oi,
                'CE_LTP': data['CE'].get(
                    'lastPrice',
                    0
                ),

                'PE_Vol': pe_vol,
                'PE_OI': pe_oi,
                'PE_LTP': data['PE'].get(
                    'lastPrice',
                    0
                )
            })

    df = pd.DataFrame(data_list)

    if df.empty:
        return None

    # --------------------------------------------------------
    # Strike step
    # --------------------------------------------------------

    sorted_strikes = df.sort_values(
        by='Strike'
    )['Strike']

    differences = sorted_strikes.diff().dropna()

    step = (
        int(differences.iloc[0])
        if not differences.empty
        else 50
    )

    # --------------------------------------------------------
    # ATM
    # --------------------------------------------------------

    atm_strike = round(
        spot_price / step
    ) * step

    # Keep 5 strikes on either side of ATM
    zone_df = df[
        (df['Strike'] >= atm_strike - (step * 5)) &
        (df['Strike'] <= atm_strike + (step * 5))
    ].copy()

    if zone_df.empty:
        return None

    # --------------------------------------------------------
    # Resistance
    # --------------------------------------------------------

    ce_vol_max = zone_df.sort_values(
        by='CE_Vol',
        ascending=False
    ).iloc[0]

    ce_oi_max = zone_df.sort_values(
        by='CE_OI',
        ascending=False
    ).iloc[0]

    # --------------------------------------------------------
    # Support
    # --------------------------------------------------------

    pe_vol_max = zone_df.sort_values(
        by='PE_Vol',
        ascending=False
    ).iloc[0]

    pe_oi_max = zone_df.sort_values(
        by='PE_OI',
        ascending=False
    ).iloc[0]

    # --------------------------------------------------------
    # Resistance selection
    # --------------------------------------------------------

    res_strike = ce_vol_max['Strike']
    res_type = "Vol"
    max_val_ce = ce_vol_max['CE_Vol']

    if ce_oi_max['CE_OI'] > max_val_ce:

        res_strike = ce_oi_max['Strike']
        res_type = "OI"
        max_val_ce = ce_oi_max['CE_OI']

    # --------------------------------------------------------
    # Support selection
    # --------------------------------------------------------

    sup_strike = pe_vol_max['Strike']
    sup_type = "Vol"
    max_val_pe = pe_vol_max['PE_Vol']

    if pe_oi_max['PE_OI'] > max_val_pe:

        sup_strike = pe_oi_max['Strike']
        sup_type = "OI"
        max_val_pe = pe_oi_max['PE_OI']

    # --------------------------------------------------------
    # Second strongest levels
    # --------------------------------------------------------

    ce_metric = (
        'CE_Vol'
        if res_type == "Vol"
        else 'CE_OI'
    )

    pe_metric = (
        'PE_Vol'
        if sup_type == "Vol"
        else 'PE_OI'
    )

    ce_2nd = zone_df[
        zone_df['Strike'] != res_strike
    ].sort_values(
        by=ce_metric,
        ascending=False
    )

    pe_2nd = zone_df[
        zone_df['Strike'] != sup_strike
    ].sort_values(
        by=pe_metric,
        ascending=False
    )

    ce_weakness = (
        ce_2nd.iloc[0][ce_metric] /
        max_val_ce
    ) * 100 if max_val_ce > 0 else 0

    pe_weakness = (
        pe_2nd.iloc[0][pe_metric] /
        max_val_pe
    ) * 100 if max_val_pe > 0 else 0

    # --------------------------------------------------------
    # Resistance status
    # --------------------------------------------------------

    if ce_weakness <= 75:

        res_status = "STRONG"

    else:

        res_status = (
            f"WTT {ce_weakness:.0f}%"
            if ce_2nd.iloc[0]['Strike'] > res_strike
            else f"WTB {ce_weakness:.0f}%"
        )

    # --------------------------------------------------------
    # Support status
    # --------------------------------------------------------

    if pe_weakness <= 75:

        sup_status = "STRONG"

    else:

        sup_status = (
            f"WTT {pe_weakness:.0f}%"
            if pe_2nd.iloc[0]['Strike'] > sup_strike
            else f"WTB {pe_weakness:.0f}%"
        )

    # --------------------------------------------------------
    # Expected boundaries
    # --------------------------------------------------------

    res_row = zone_df[
        zone_df['Strike'] == res_strike
    ]

    sup_row = zone_df[
        zone_df['Strike'] == sup_strike
    ]

    EOR = (
        res_strike +
        res_row['CE_LTP'].values[0]
        if not res_row.empty
        else res_strike
    )

    EOS = (
        sup_strike -
        sup_row['PE_LTP'].values[0]
        if not sup_row.empty
        else sup_strike
    )

    # --------------------------------------------------------
    # PCR
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Final metrics
    # --------------------------------------------------------

    return {

        "Asset": symbol,

        "Spot": round(
            float(spot_price),
            2
        ),

        "Expiry": selected_expiry,

        "ATM": atm_strike,

        "PCR_Vol": round(
            float(pcr_vol),
            4
        ),

        "PCR_OI": round(
            float(pcr_oi),
            4
        ),

        "EOR": round(
            float(EOR),
            2
        ),

        "EOR_Type": res_type,

        "EOR_Status": res_status,

        "EOS": round(
            float(EOS),
            2
        ),

        "EOS_Type": sup_type,

        "EOS_Status": sup_status,

        "CE_Total_OI": int(total_ce_oi),

        "PE_Total_OI": int(total_pe_oi),

        "CE_Total_Volume": int(total_ce_vol),

        "PE_Total_Volume": int(total_pe_vol),

        "Generated_At": datetime.utcnow().isoformat()
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
            (int, float)
        )
    )


# ============================================================
# GEMINI INTELLIGENCE ENGINE
# ============================================================

def run_gemini_analysis(metrics):

    api_key = os.environ.get(
        "GEMINI_API_KEY"
    )

    if not api_key:

        print(
            "⚠️ GEMINI_API_KEY not configured."
        )

        return None

    if not metrics:
        return None

    try:

        client = genai.Client(
            api_key=api_key
        )

        metrics_json = json.dumps(
            metrics,
            indent=2,
            default=str
        )

        asset = metrics["Asset"]

        prompt = f"""
You are an advanced market-research and financial-analysis engine.

Analyze {asset} using the REAL-TIME NSE options-chain data supplied below.

==================================================
QUANTITATIVE DATA
==================================================

{metrics_json}

==================================================
YOUR JOB
==================================================

Do NOT simply say whether the market will rise or fall.

You must combine:

1. The supplied quantitative options-chain data
2. Current company/business developments
3. Current market developments
4. Current macroeconomic conditions
5. Current regulatory developments
6. Current geopolitical developments

Use Google Search to research CURRENT information.

==================================================
RESEARCH REQUIREMENTS
==================================================

Search for information relevant to {asset}.

Depending on whether the asset is a company or an index, investigate:

COMPANY / BUSINESS:

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
- Capital allocation
- Regulatory developments
- Product/business changes
- Sector developments

MACRO:

- RBI policy
- Interest rates
- Inflation
- INR/USD
- US economic conditions
- Global market conditions
- Commodity prices where relevant

GEOPOLITICAL:

- Wars/conflicts
- Sanctions
- Tariffs
- Trade restrictions
- Diplomatic developments
- Supply-chain disruption
- Energy/geopolitical shocks
- Country-specific risks

IMPORTANT:

Only include geopolitical developments if there is a
reasonable economic or business mechanism connecting them
to the asset.

For example:

Geopolitical event
        ↓
trade/supply chain/currency/demand/regulation
        ↓
company/sector
        ↓
possible market impact

Do NOT simply mention geopolitical news because it is currently
in the headlines.

==================================================
QUANTITATIVE INTERPRETATION
==================================================

Interpret:

PCR_Vol
PCR_OI
EOR
EOS
EOR_Status
EOS_Status
ATM
Spot

Explain what the options structure is actually indicating.

Do not invent technical indicators that were not supplied.

Do not calculate RSI, MACD, moving averages or other indicators
unless they are explicitly provided.

==================================================
EVIDENCE RULES
==================================================

Separate:

FACT
from
INTERPRETATION
from
POSSIBLE MARKET IMPACT.

Do not present speculation as fact.

Do not manufacture information.

If reliable current information cannot be found, explicitly say so.

Recent information should receive greater weight than old information.

Do not assume that a positive headline automatically means a positive
stock-price reaction.

Explain the causal mechanism.

==================================================
OUTPUT
==================================================

Return the analysis using this exact structure:

📊 QUANTITATIVE READ

Explain the options-chain structure.

📰 CURRENT DEVELOPMENTS

Give the most important recent developments.

🌍 GEOPOLITICAL & MACRO

Explain relevant geopolitical, economic and regulatory factors.

🏢 BUSINESS IMPACT

Explain how those developments could affect the company/index.

⚖️ UPSIDE FACTORS

List factors that could support upward movement.

⚠️ DOWNSIDE FACTORS

List factors that could create downward pressure.

🎯 OPTIONS + NEWS SYNTHESIS

Combine the quantitative options structure with the
current information environment.

🔎 KEY RISK

Identify the most important uncertainty or event that could
invalidate the current interpretation.

📌 CONCLUSION

Give a concise evidence-based market interpretation.

Do NOT provide guaranteed predictions.

Do NOT claim certainty.

Use citations/sources from Google Search wherever possible.
"""

        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt,
            config={
                "tools": [
                    {
                        "google_search": {}
                    }
                ],
                "thinking_config": {
                    "thinking_level": "medium"
                }
            }
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

    # Telegram message limit safety
    chunks = [
        text[i:i + 3900]
        for i in range(
            0,
            len(text),
            3900
        )
    ]

    for chunk in chunks:

        try:

            response = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": chunk
                },
                timeout=10
            )

            if not response.ok:

                print(
                    "Telegram error:",
                    response.text
                )

        except Exception as e:

            print(
                f"❌ Telegram network error: {e}"
            )


# ============================================================
# ORIGINAL MATRIX TELEGRAM ALERT
# ============================================================

def send_matrix_telegram_alert(metrics):

    if not metrics:
        return

    signal = (
        "🟢 VOL BREAKOUT SUPPORTED"
        if metrics['PCR_Vol'] > 1.0
        else
        "🔴 LIQUIDITY RESISTANCE BLOCKED"
    )

    msg = (

        f"👑 <b>LTP CALCULATOR PRO — ALPHA GRID</b>\n"

        f"<i>Asset Alpha Matrix Dashboard Context</i>\n"

        f"──────────────────────────────────\n"

        f"<b>Instrument:</b> "
        f"{metrics['Asset']} "
        f"({metrics['Expiry']})\n"

        f"<b>Spot Price:</b> "
        f"₹{metrics['Spot']:.2f}\n"

        f"<b>ATM:</b> "
        f"{metrics['ATM']}\n"

        f"──────────────────────────────────\n"

        f"📊 <b>PCR Vol:</b> "
        f"{metrics['PCR_Vol']:.2f} | "

        f"<b>PCR OI:</b> "
        f"{metrics['PCR_OI']:.2f}\n"

        f"🛑 <b>EoR ({metrics['EOR_Type']}):</b> "
        f"{metrics['EOR']:.2f}\n"

        f"   └ Status: "
        f"<code>{metrics['EOR_Status']}</code>\n"

        f"🟢 <b>EoS ({metrics['EOS_Type']}):</b> "
        f"{metrics['EOS']:.2f}\n"

        f"   └ Status: "
        f"<code>{metrics['EOS_Status']}</code>\n"

        f"──────────────────────────────────\n"

        f"💡 <b>Signal:</b> "
        f"<code>{signal}</code>"
    )

    send_telegram_message(msg)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    raw_input = os.environ.get(
        "TELEGRAM_INPUT_TICKER",
        ""
    ).strip().upper()

    if not raw_input and len(sys.argv) > 1:

        raw_input = (
            sys.argv[1]
            .strip()
            .upper()
        )

    # Default
    target_asset = (
        raw_input
        if raw_input
        else "NIFTY"
    )

    api_mode = (
        "indices"
        if target_asset in [
            "NIFTY",
            "BANKNIFTY",
            "FINNIFTY"
        ]
        else
        "equities"
    )

    print(
        f"🔎 Scanning {target_asset}..."
    )

    matrix_metrics = process_singularity_matrix(
        target_asset,
        api_mode
    )

    if matrix_metrics:

        print(
            "✅ Quantitative scan completed."
        )

        # --------------------------------------------
        # 1. Send existing quantitative alert
        # --------------------------------------------

        send_matrix_telegram_alert(
            matrix_metrics
        )

        # --------------------------------------------
        # 2. Gemini intelligence analysis
        # --------------------------------------------

        print(
            "🤖 Starting Gemini intelligence analysis..."
        )

        ai_analysis = run_gemini_analysis(
            matrix_metrics
        )

        if ai_analysis:

            ai_message = (

                f"🤖 GEMINI MARKET INTELLIGENCE\n"
                f"{target_asset}\n"
                f"────────────────────────\n\n"
                f"{ai_analysis}"
            )

            send_telegram_message(
                ai_message
            )

            print(
                "✅ Gemini analysis sent to Telegram."
            )

        else:

            print(
                "⚠️ Gemini analysis unavailable."
            )

    else:

        print(
            "⚠️ Matrix processing halted. "
            "Target instrument lacks option structures."
        )