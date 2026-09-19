import os
import sys
import time
import math
import html
from datetime import datetime, timezone

import pandas as pd
import requests
from curl_cffi import requests as curl_requests


# ============================================================
# CONFIGURATION
# ============================================================

NSE_HOME = "https://www.nseindia.com"
NSE_OPTION_CHAIN_PAGE = "https://www.nseindia.com/option-chain"

NSE_CONTRACT_INFO = (
    "https://www.nseindia.com/api/option-chain-contract-info"
)

NSE_OPTION_CHAIN_V3 = (
    "https://www.nseindia.com/api/option-chain-v3"
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

GEMINI_MODEL = "gemini-3.8-flash"

INDEX_SYMBOLS = {
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
}


# ============================================================
# GENERIC HELPERS
# ============================================================

def safe_float(value, default=0.0):
    try:
        if value is None:
            return default

        if isinstance(value, str):
            value = value.replace(",", "").strip()

        result = float(value)

        if math.isnan(result) or math.isinf(result):
            return default

        return result

    except (ValueError, TypeError):
        return default


def safe_int(value, default=0):
    try:
        if value is None:
            return default

        if isinstance(value, str):
            value = value.replace(",", "").strip()

        return int(float(value))

    except (ValueError, TypeError):
        return default


def clean_ticker(value):
    if not value:
        return ""

    return (
        str(value)
        .strip()
        .upper()
        .replace(" ", "")
    )


def format_number(value):
    try:
        return f"{float(value):,.2f}"
    except Exception:
        return str(value)


def format_integer(value):
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value)


# ============================================================
# NSE SESSION
# ============================================================

def create_nse_session():
    """
    NSE is much more reliable when accessed through a browser-like
    session with cookies and realistic headers.
    """

    session = curl_requests.Session(
        impersonate="chrome"
    )

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,image/avif,image/webp,"
            "image/apng,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })

    return session


def warm_nse_session(session):
    """
    Establish NSE cookies before hitting API endpoints.
    """

    print("Warming NSE session...")

    try:
        response = session.get(
            NSE_HOME,
            timeout=20,
            allow_redirects=True,
        )

        print(
            f"NSE home: {response.status_code} "
            f"{response.url}"
        )

    except Exception as exc:
        print(f"NSE home warm-up warning: {exc}")

    try:
        response = session.get(
            NSE_OPTION_CHAIN_PAGE,
            timeout=20,
            allow_redirects=True,
        )

        print(
            f"NSE option-chain page: "
            f"{response.status_code}"
        )

    except Exception as exc:
        print(
            f"NSE option-chain warm-up warning: "
            f"{exc}"
        )

    time.sleep(0.8)


def nse_get(session, url, params=None):
    """
    GET request against NSE with browser-like headers.
    """

    headers = {
        "Accept": "application/json,text/plain,*/*",
        "Referer": NSE_OPTION_CHAIN_PAGE,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    response = session.get(
        url,
        params=params,
        headers=headers,
        timeout=30,
    )

    print(
        f"NSE API: {response.status_code} "
        f"{response.url}"
    )

    response.raise_for_status()

    try:
        return response.json()

    except Exception as exc:
        print("NSE returned non-JSON response.")
        print(response.text[:1000])
        raise RuntimeError(
            f"Unable to decode NSE JSON: {exc}"
        )


# ============================================================
# NSE EXPIRY
# ============================================================

def extract_expiry_dates(raw):
    """
    NSE has changed the nesting of expiry information across
    endpoint versions. Search several known structures.
    """

    candidates = []

    def collect(obj):
        if isinstance(obj, dict):

            for key in (
                "expiryDates",
                "expiryDate",
                "expiries",
            ):
                value = obj.get(key)

                if isinstance(value, list):
                    candidates.extend(value)

                elif isinstance(value, str):
                    candidates.append(value)

            for value in obj.values():
                if isinstance(value, (dict, list)):
                    collect(value)

        elif isinstance(obj, list):
            for item in obj:
                collect(item)

    collect(raw)

    cleaned = []

    for item in candidates:

        if not isinstance(item, str):
            continue

        item = item.strip()

        if item and item not in cleaned:
            cleaned.append(item)

    return cleaned


def get_nearest_expiry(session, symbol, mode):
    """
    Fetch available expiries from NSE contract-info endpoint.
    """

    print(
        f"Fetching expiry information for "
        f"{symbol}..."
    )

    params = {
        "symbol": symbol,
        "type": mode,
    }

    raw = nse_get(
        session,
        NSE_CONTRACT_INFO,
        params=params,
    )

    expiry_dates = extract_expiry_dates(raw)

    if not expiry_dates:

        # Fallback to older records-style response.
        records = raw.get("records", {})

        expiry_dates = records.get(
            "expiryDates",
            []
        )

    if not expiry_dates:
        raise RuntimeError(
            "NSE did not return any expiry dates."
        )

    expiry = expiry_dates[0]

    print(
        f"Nearest expiry for {symbol}: "
        f"{expiry}"
    )

    return expiry


# ============================================================
# NSE OPTION CHAIN
# ============================================================

def fetch_nse_option_chain(symbol, mode):
    """
    Fetch current NSE option-chain-v3 data.
    """

    session = create_nse_session()

    warm_nse_session(session)

    expiry = get_nearest_expiry(
        session,
        symbol,
        mode,
    )

    print(
        f"Fetching v3 option chain for "
        f"{symbol} / {expiry}..."
    )

    params = {
        "type": mode,
        "symbol": symbol,
        "expiry": expiry,
    }

    raw = nse_get(
        session,
        NSE_OPTION_CHAIN_V3,
        params=params,
    )

    # Count rows for diagnostics.
    rows = []

    if isinstance(raw.get("filtered"), dict):
        rows = raw["filtered"].get("data", [])

    if not rows and isinstance(raw.get("records"), dict):
        rows = raw["records"].get("data", [])

    print(
        f"Received {len(rows)} NSE "
        f"option-chain rows."
    )

    if not rows:
        raise RuntimeError(
            "NSE returned no option-chain rows."
        )

    return raw, expiry


# ============================================================
# OPTION CHAIN PARSER
# ============================================================

def build_option_dataframe(raw_data, expiry):
    """
    Convert NSE option-chain-v3 JSON into a normalized dataframe.

    Supports both:

        filtered.data

    and:

        records.data

    NSE's v3 endpoint already receives the requested expiry,
    so we intentionally do NOT reject rows merely because
    expiry fields inside CE/PE are missing.
    """

    filtered = raw_data.get("filtered", {})
    records = raw_data.get("records", {})

    rows = []

    if isinstance(filtered, dict):
        rows = filtered.get("data", [])

    if not rows and isinstance(records, dict):
        rows = records.get("data", [])

    if not rows:
        raise RuntimeError(
            "NSE returned no option-chain data rows."
        )

    print(
        f"Parsing {len(rows)} option-chain rows..."
    )

    normalized = []

    for item in rows:

        if not isinstance(item, dict):
            continue

        strike = safe_float(
            item.get("strikePrice")
        )

        if strike <= 0:
            continue

        ce = item.get("CE")

        if not isinstance(ce, dict):
            ce = {}

        pe = item.get("PE")

        if not isinstance(pe, dict):
            pe = {}

        normalized.append({
            "Strike": strike,

            "CE_Volume": safe_int(
                ce.get("totalTradedVolume", 0)
            ),

            "PE_Volume": safe_int(
                pe.get("totalTradedVolume", 0)
            ),

            "CE_OI": safe_int(
                ce.get("openInterest", 0)
            ),

            "PE_OI": safe_int(
                pe.get("openInterest", 0)
            ),

            "CE_LTP": safe_float(
                ce.get("lastPrice", 0)
            ),

            "PE_LTP": safe_float(
                pe.get("lastPrice", 0)
            ),

            "CE_Change_OI": safe_int(
                ce.get("changeinOpenInterest", 0)
            ),

            "PE_Change_OI": safe_int(
                pe.get("changeinOpenInterest", 0)
            ),

            "CE_IV": safe_float(
                ce.get("impliedVolatility", 0)
            ),

            "PE_IV": safe_float(
                pe.get("impliedVolatility", 0)
            ),
        })

    if not normalized:

        # Very useful diagnostic if NSE changes the schema again.
        first_row = rows[0]

        print(
            "First NSE row keys:",
            list(first_row.keys())
        )

        print(
            "First NSE row sample:",
            str(first_row)[:2500]
        )

        raise RuntimeError(
            "NSE returned option-chain rows, "
            "but none contained a valid strikePrice."
        )

    df = pd.DataFrame(normalized)

    df = (
        df
        .drop_duplicates(
            subset=["Strike"]
        )
        .sort_values("Strike")
        .reset_index(drop=True)
    )

    if df.empty:
        raise RuntimeError(
            "Option dataframe is empty after parsing."
        )

    print(
        f"Successfully parsed "
        f"{len(df)} strikes."
    )

    print(
        f"Strike range: "
        f"{df['Strike'].min()} "
        f"→ "
        f"{df['Strike'].max()}"
    )

    print(
        f"CE OI: "
        f"{int(df['CE_OI'].sum()):,}"
    )

    print(
        f"PE OI: "
        f"{int(df['PE_OI'].sum()):,}"
    )

    print(
        f"CE Volume: "
        f"{int(df['CE_Volume'].sum()):,}"
    )

    print(
        f"PE Volume: "
        f"{int(df['PE_Volume'].sum()):,}"
    )

    return df


# ============================================================
# SPOT PRICE
# ============================================================

def get_spot_price(raw_data, df):
    """
    Extract underlying spot price from NSE response.
    """

    records = raw_data.get("records", {})

    if isinstance(records, dict):

        value = safe_float(
            records.get(
                "underlyingValue",
                0
            )
        )

        if value > 0:
            return value

    filtered = raw_data.get("filtered", {})

    if isinstance(filtered, dict):

        for item in filtered.get("data", []):

            if not isinstance(item, dict):
                continue

            ce = item.get("CE") or {}
            pe = item.get("PE") or {}

            for side in (ce, pe):

                value = safe_float(
                    side.get(
                        "underlyingValue",
                        0
                    )
                )

                if value > 0:
                    return value

    if isinstance(records, dict):

        for item in records.get("data", []):

            if not isinstance(item, dict):
                continue

            ce = item.get("CE") or {}
            pe = item.get("PE") or {}

            for side in (ce, pe):

                value = safe_float(
                    side.get(
                        "underlyingValue",
                        0
                    )
                )

                if value > 0:
                    return value

    # Last-resort fallback.
    if not df.empty:
        return float(
            df["Strike"].median()
        )

    return 0.0


# ============================================================
# STRIKE STEP
# ============================================================

def calculate_strike_step(df):
    strikes = sorted(
        set(
            safe_float(x)
            for x in df["Strike"].tolist()
        )
    )

    differences = []

    for i in range(1, len(strikes)):

        diff = strikes[i] - strikes[i - 1]

        if diff > 0:
            differences.append(diff)

    if not differences:
        return 1.0

    # Most common / smallest sensible increment.
    differences.sort()

    return float(
        differences[len(differences) // 2]
    )


# ============================================================
# SINGULARITY MATRIX
# ============================================================

def process_singularity_matrix(symbol, mode):
    """
    Main quantitative option-chain engine.
    """

    print("")
    print("=" * 70)
    print(f"SCANNING {symbol}")
    print("=" * 70)

    raw_data, expiry = fetch_nse_option_chain(
        symbol,
        mode,
    )

    df = build_option_dataframe(
        raw_data,
        expiry,
    )

    spot = get_spot_price(
        raw_data,
        df,
    )

    if spot <= 0:
        raise RuntimeError(
            "Could not determine underlying spot price."
        )

    strike_step = calculate_strike_step(
        df
    )

    # Nearest ATM strike.
    atm_row = (
        df.iloc[
            (
                df["Strike"] - spot
            ).abs().argsort()[:1]
        ]
    )

    atm = float(
        atm_row.iloc[0]["Strike"]
    )

    # 5 strikes above and below ATM.
    zone_low = atm - (
        strike_step * 5
    )

    zone_high = atm + (
        strike_step * 5
    )

    zone = df[
        (df["Strike"] >= zone_low)
        &
        (df["Strike"] <= zone_high)
    ].copy()

    if zone.empty:
        zone = df.copy()

    # --------------------------------------------------------
    # RESISTANCE
    # --------------------------------------------------------

    resistance_by_oi = zone.loc[
        zone["CE_OI"].idxmax()
    ]

    resistance_by_volume = zone.loc[
        zone["CE_Volume"].idxmax()
    ]

    # Primary resistance = strongest CE OI.
    resistance = float(
        resistance_by_oi["Strike"]
    )

    # --------------------------------------------------------
    # SUPPORT
    # --------------------------------------------------------

    support_by_oi = zone.loc[
        zone["PE_OI"].idxmax()
    ]

    support_by_volume = zone.loc[
        zone["PE_Volume"].idxmax()
    ]

    # Primary support = strongest PE OI.
    support = float(
        support_by_oi["Strike"]
    )

    # --------------------------------------------------------
    # PCR
    # --------------------------------------------------------

    total_ce_volume = float(
        zone["CE_Volume"].sum()
    )

    total_pe_volume = float(
        zone["PE_Volume"].sum()
    )

    total_ce_oi = float(
        zone["CE_OI"].sum()
    )

    total_pe_oi = float(
        zone["PE_OI"].sum()
    )

    pcr_volume = (
        total_pe_volume / total_ce_volume
        if total_ce_volume > 0
        else 0.0
    )

    pcr_oi = (
        total_pe_oi / total_ce_oi
        if total_ce_oi > 0
        else 0.0
    )

    # --------------------------------------------------------
    # EOR / EOS
    # --------------------------------------------------------

    resistance_ce_ltp = safe_float(
        resistance_by_oi["CE_LTP"]
    )

    support_pe_ltp = safe_float(
        support_by_oi["PE_LTP"]
    )

    eor = resistance + resistance_ce_ltp

    eos = support - support_pe_ltp

    # --------------------------------------------------------
    # STRENGTH
    # --------------------------------------------------------

    resistance_oi = safe_float(
        resistance_by_oi["CE_OI"]
    )

    support_oi = safe_float(
        support_by_oi["PE_OI"]
    )

    resistance_volume = safe_int(
        resistance_by_volume["CE_Volume"]
    )

    support_volume = safe_int(
        support_by_volume["PE_Volume"]
    )

    if pcr_volume > 1.0:
        eor_status = "VOL BREAKOUT SUPPORTED"
    else:
        eor_status = "LIQUIDITY RESISTANCE BLOCKED"

    if pcr_oi > 1.0:
        eos_status = "PUT OI SUPPORTIVE"
    else:
        eos_status = "CALL OI DOMINANT"

    resistance_strength = (
        "STRONG"
        if resistance_oi >= support_oi
        else "MODERATE"
    )

    support_strength = (
        "STRONG"
        if support_oi >= resistance_oi
        else "MODERATE"
    )

    metrics = {
        "Asset": symbol,
        "Spot": spot,
        "Expiry": expiry,
        "ATM": atm,
        "Strike_Step": strike_step,

        "Zone_Low": zone_low,
        "Zone_High": zone_high,

        "Resistance": resistance,
        "Resistance_By_Volume": float(
            resistance_by_volume["Strike"]
        ),
        "Resistance_By_OI": float(
            resistance_by_oi["Strike"]
        ),

        "Support": support,
        "Support_By_Volume": float(
            support_by_volume["Strike"]
        ),
        "Support_By_OI": float(
            support_by_oi["Strike"]
        ),

        "CE_Max_Volume": resistance_volume,
        "CE_Max_OI": resistance_oi,

        "PE_Max_Volume": support_volume,
        "PE_Max_OI": support_oi,

        "PCR_Vol": pcr_volume,
        "PCR_OI": pcr_oi,

        "EOR": eor,
        "EOR_Type": "CE OI + CE LTP",
        "EOR_Status": eor_status,

        "EOS": eos,
        "EOS_Type": "PE OI - PE LTP",
        "EOS_Status": eos_status,

        "Resistance_Strength": resistance_strength,
        "Support_Strength": support_strength,

        "Timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    # --------------------------------------------------------
    # PRINT MATRIX
    # --------------------------------------------------------

    print("")
    print("=" * 70)
    print("MATRIX RESULT")
    print("=" * 70)

    print(
        f"Asset              : {symbol}"
    )

    print(
        f"Spot               : "
        f"{format_number(spot)}"
    )

    print(
        f"Expiry             : {expiry}"
    )

    print(
        f"ATM                : "
        f"{format_number(atm)}"
    )

    print(
        f"Strike Step        : "
        f"{format_number(strike_step)}"
    )

    print(
        f"Resistance         : "
        f"{format_number(resistance)}"
    )

    print(
        f"Support            : "
        f"{format_number(support)}"
    )

    print(
        f"PCR Volume         : "
        f"{pcr_volume:.3f}"
    )

    print(
        f"PCR OI             : "
        f"{pcr_oi:.3f}"
    )

    print(
        f"EOR                : "
        f"{format_number(eor)}"
    )

    print(
        f"EOS                : "
        f"{format_number(eos)}"
    )

    print(
        f"EOR Status         : "
        f"{eor_status}"
    )

    print(
        f"EOS Status         : "
        f"{eos_status}"
    )

    print("=" * 70)

    return metrics


# ============================================================
# GEMINI ANALYSIS
# ============================================================

def build_gemini_prompt(metrics):
    """
    Prompt Gemini to combine the quantitative scanner output
    with current web information.

    The model is explicitly asked to separate facts from
    interpretation and avoid inventing news.
    """

    return f"""
You are the research and analysis layer of an Indian market
option-chain scanner.

Analyze the following quantitative NSE option-chain output
for {metrics['Asset']}.

IMPORTANT:
- This is research/analysis, not personalized financial advice.
- Do not invent news or company developments.
- Use current Google Search results for recent information.
- Prefer primary sources, company filings, official government/
  regulator sources, major financial publications, and established
  news organizations.
- Clearly distinguish confirmed facts from interpretation.
- Consider the latest relevant geopolitical developments,
  government/regulatory actions, macroeconomic conditions,
  company policies, business developments, sector developments,
  commodity/energy exposure, currency effects, trade restrictions,
  sanctions, wars/conflicts, and supply-chain implications where
  relevant to this asset.
- Only discuss geopolitical factors that actually have a plausible
  connection to this asset.
- Do not treat a single news headline as proof of a market outcome.
- Do not fabricate a target price.
- Explain uncertainty.

QUANTITATIVE SCANNER DATA
-------------------------
Asset: {metrics['Asset']}
Spot: {metrics['Spot']}
Expiry: {metrics['Expiry']}
ATM: {metrics['ATM']}
Strike Step: {metrics['Strike_Step']}

Zone:
Low: {metrics['Zone_Low']}
High: {metrics['Zone_High']}

Resistance:
Primary: {metrics['Resistance']}
Volume-based: {metrics['Resistance_By_Volume']}
OI-based: {metrics['Resistance_By_OI']}
CE Max Volume: {metrics['CE_Max_Volume']}
CE Max OI: {metrics['CE_Max_OI']}
Strength: {metrics['Resistance_Strength']}

Support:
Primary: {metrics['Support']}
Volume-based: {metrics['Support_By_Volume']}
OI-based: {metrics['Support_By_OI']}
PE Max Volume: {metrics['PE_Max_Volume']}
PE Max OI: {metrics['PE_Max_OI']}
Strength: {metrics['Support_Strength']}

PCR Volume: {metrics['PCR_Vol']}
PCR OI: {metrics['PCR_OI']}

EOR: {metrics['EOR']}
EOR Type: {metrics['EOR_Type']}
EOR Status: {metrics['EOR_Status']}

EOS: {metrics['EOS']}
EOS Type: {metrics['EOS_Type']}
EOS Status: {metrics['EOS_Status']}

TASK
----

Produce a concise research report with exactly these sections:

1. OPTION-CHAIN READ
Explain what the OI, volume, PCR, support, resistance, EOR
and EOS are indicating.

2. CURRENT COMPANY / BUSINESS DEVELOPMENTS
Find relevant recent developments affecting the company or
underlying asset.

3. MACRO + GEOPOLITICAL CONTEXT
Find only relevant current geopolitical, regulatory,
macroeconomic, trade, commodity, currency or supply-chain
developments.

4. BULLISH FACTORS
List the strongest factors supporting upside.

5. BEARISH FACTORS
List the strongest factors creating downside/risk.

6. KEY LEVELS
State the scanner's support, resistance, EOR and EOS levels
and explain their meaning. Do not invent additional levels.

7. SYNTHESIS
Give a balanced interpretation of how the quantitative data
and current information interact.

8. WHAT WOULD INVALIDATE THIS VIEW
List concrete events or market conditions that would make
the current interpretation less useful.

Keep the response Telegram-friendly and reasonably concise.
Use INR/Indian-market terminology where appropriate.
"""


def get_gemini_analysis(metrics):
    """
    Use Gemini + Google Search grounding for current information.
    """

    if not GEMINI_API_KEY:
        print(
            "GEMINI_API_KEY is not configured. "
            "Skipping Gemini analysis."
        )
        return ""

    try:
        from google import genai
        from google.genai import types

    except ImportError:
        print(
            "google-genai is not installed. "
            "Skipping Gemini analysis."
        )
        return ""

    print("")
    print("=" * 70)
    print("RUNNING GEMINI RESEARCH")
    print("=" * 70)

    try:

        client = genai.Client(
            api_key=GEMINI_API_KEY
        )

        prompt = build_gemini_prompt(
            metrics
        )

        grounding_tool = types.Tool(
            google_search=types.GoogleSearch()
        )

        config = types.GenerateContentConfig(
            tools=[
                grounding_tool
            ]
        )

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=config,
        )

        text = getattr(
            response,
            "text",
            None
        )

        if not text:
            print(
                "Gemini returned an empty response."
            )
            return ""

        print(
            "Gemini analysis generated successfully."
        )

        return text.strip()

    except Exception as exc:

        print(
            "Gemini analysis failed:"
        )

        print(
            f"{type(exc).__name__}: {exc}"
        )

        return ""


# ============================================================
# TELEGRAM
# ============================================================

def telegram_request(method, payload):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing."
        )

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/{method}"
    )

    response = requests.post(
        url,
        json=payload,
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {data}"
        )

    return data


def send_telegram_message(
    text,
    parse_mode="HTML"
):
    """
    Telegram has a message size limit.
    Split long Gemini reports safely.
    """

    if not TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is missing."
        )

    max_length = 3900

    chunks = []

    while len(text) > max_length:

        split_at = text.rfind(
            "\n",
            0,
            max_length
        )

        if split_at < 1000:
            split_at = max_length

        chunks.append(
            text[:split_at]
        )

        text = text[split_at:].lstrip()

    if text:
        chunks.append(text)

    for chunk in chunks:

        telegram_request(
            "sendMessage",
            {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
        )

        time.sleep(0.5)


# ============================================================
# TELEGRAM MATRIX REPORT
# ============================================================

def send_matrix_telegram_alert(metrics):
    """
    Send the quantitative scanner result first.
    Gemini research is sent separately afterwards.
    """

    signal = (
        "🟢 VOL BREAKOUT SUPPORTED"
        if metrics["PCR_Vol"] > 1
        else
        "🔴 LIQUIDITY RESISTANCE BLOCKED"
    )

    message = f"""
<b>OPTION CHAIN MATRIX</b>

<b>Asset:</b> {html.escape(metrics["Asset"])}
<b>Spot:</b> {format_number(metrics["Spot"])}
<b>Expiry:</b> {html.escape(str(metrics["Expiry"]))}

<b>ATM:</b> {format_number(metrics["ATM"])}

<b>Resistance:</b> {format_number(metrics["Resistance"])}
<b>Support:</b> {format_number(metrics["Support"])}

<b>Resistance by Volume:</b>
{format_number(metrics["Resistance_By_Volume"])}

<b>Resistance by OI:</b>
{format_number(metrics["Resistance_By_OI"])}

<b>Support by Volume:</b>
{format_number(metrics["Support_By_Volume"])}

<b>Support by OI:</b>
{format_number(metrics["Support_By_OI"])}

<b>PCR Volume:</b> {metrics["PCR_Vol"]:.3f}
<b>PCR OI:</b> {metrics["PCR_OI"]:.3f}

<b>EOR:</b> {format_number(metrics["EOR"])}
<b>EOS:</b> {format_number(metrics["EOS"])}

<b>EOR Status:</b>
{html.escape(metrics["EOR_Status"])}

<b>EOS Status:</b>
{html.escape(metrics["EOS_Status"])}

<b>Scanner Signal:</b>
{signal}

<i>Quantitative NSE option-chain analysis.</i>
"""

    send_telegram_message(
        message.strip()
    )


# ============================================================
# TRAGEDY GUARD
# ============================================================

def tragedy_guard(metrics):
    """
    Basic sanity checks before sending the result.
    """

    required = [
        "Spot",
        "Resistance",
        "Support",
        "PCR_Vol",
        "PCR_OI",
        "EOR",
        "EOS",
    ]

    for key in required:

        value = metrics.get(key)

        if value is None:
            raise RuntimeError(
                f"Missing metric: {key}"
            )

        if isinstance(value, float):

            if math.isnan(value) or math.isinf(value):
                raise RuntimeError(
                    f"Invalid metric: {key}"
                )

    if metrics["Spot"] <= 0:
        raise RuntimeError(
            "Invalid spot price."
        )

    if metrics["Resistance"] <= 0:
        raise RuntimeError(
            "Invalid resistance."
        )

    if metrics["Support"] <= 0:
        raise RuntimeError(
            "Invalid support."
        )


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # Determine ticker
    # --------------------------------------------------------

    ticker = ""

    if len(sys.argv) > 1:
        ticker = sys.argv[1]

    if not ticker:
        ticker = os.getenv(
            "TELEGRAM_INPUT_TICKER",
            ""
        )

    ticker = clean_ticker(ticker)

    if not ticker:
        ticker = "NIFTY"

    print("")
    print("=" * 70)
    print("ALGORITHMIC SCANNER")
    print("=" * 70)

    print(
        f"Ticker: {ticker}"
    )

    # --------------------------------------------------------
    # Select NSE API mode
    # --------------------------------------------------------

    if ticker in INDEX_SYMBOLS:

        mode = "Indices"

        print(
            "Index detected. "
            "Using NSE Indices option chain."
        )

    else:

        mode = "Equity"

        print(
            "Equity detected. "
            "Using NSE Equity option chain."
        )

    # --------------------------------------------------------
    # Run matrix
    # --------------------------------------------------------

    try:

        metrics = process_singularity_matrix(
            ticker,
            mode,
        )

        tragedy_guard(
            metrics
        )

    except Exception as exc:

        print("")
        print("=" * 70)
        print("CORE API SYNC FAILURE")
        print("=" * 70)

        print(
            f"{type(exc).__name__}: {exc}"
        )

        print(
            "Matrix processing halted."
        )

        raise

    # --------------------------------------------------------
    # Send quantitative report
    # --------------------------------------------------------

    try:

        print(
            "Sending Telegram matrix report..."
        )

        send_matrix_telegram_alert(
            metrics
        )

        print(
            "Telegram matrix report sent."
        )

    except Exception as exc:

        print(
            "Telegram matrix report failed:"
        )

        print(
            f"{type(exc).__name__}: {exc}"
        )

    # --------------------------------------------------------
    # Gemini research
    # --------------------------------------------------------

    gemini_analysis = get_gemini_analysis(
        metrics
    )

    if gemini_analysis:

        print("")
        print("=" * 70)
        print("GEMINI ANALYSIS")
        print("=" * 70)

        print(
            gemini_analysis
        )

        print("=" * 70)

        try:

            gemini_message = (
                "<b>GEMINI MARKET RESEARCH</b>\n\n"
                +
                html.escape(
                    gemini_analysis
                )
                +
                "\n\n"
                "<i>Research generated using "
                "current web-grounded information. "
                "Not financial advice.</i>"
            )

            send_telegram_message(
                gemini_message
            )

            print(
                "Gemini analysis sent to Telegram."
            )

        except Exception as exc:

            print(
                "Failed to send Gemini analysis:"
            )

            print(
                f"{type(exc).__name__}: {exc}"
            )

    else:

        print(
            "No Gemini analysis available."
        )

    print("")
    print("=" * 70)
    print("SCAN COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()
