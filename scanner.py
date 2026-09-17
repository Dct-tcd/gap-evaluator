import os
import sys
import time
import math
import json
from datetime import datetime, timezone

import pandas as pd

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    print("❌ curl_cffi is not installed.")
    print("Add curl_cffi to the GitHub Actions pip install command.")
    sys.exit(1)


# ============================================================
# CONFIG
# ============================================================

NSE_HOME = "https://www.nseindia.com"
NSE_OPTION_CHAIN_PAGE = f"{NSE_HOME}/option-chain"

NSE_CONTRACT_INFO = (
    f"{NSE_HOME}/api/option-chain-contract-info"
)

NSE_OPTION_CHAIN_V3 = (
    f"{NSE_HOME}/api/option-chain-v3"
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

INDEX_SYMBOLS = {
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
}


# ============================================================
# NSE SESSION
# ============================================================

def create_nse_session():
    """
    NSE is protected by Akamai and can reject normal Python
    requests. curl_cffi impersonates a real Chrome TLS fingerprint.
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
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Referer": NSE_OPTION_CHAIN_PAGE,
    })

    return session


def warm_nse_session(session):
    """
    Establish NSE cookies before hitting the API.
    """

    urls = [
        NSE_OPTION_CHAIN_PAGE,
        f"{NSE_HOME}/api/allIndices",
    ]

    for url in urls:
        try:
            response = session.get(
                url,
                timeout=20,
            )

            print(
                f"🌐 NSE warm-up: "
                f"{response.status_code} {url}"
            )

            time.sleep(1)

        except Exception as exc:
            print(
                f"⚠️ NSE warm-up warning: "
                f"{type(exc).__name__}: {exc}"
            )


def nse_get(session, url, params=None, retries=3):
    """
    GET helper with retry logic.
    """

    last_error = None

    for attempt in range(1, retries + 1):

        try:
            response = session.get(
                url,
                params=params,
                timeout=25,
            )

            if response.status_code == 200:
                return response.json()

            last_error = (
                f"HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )

            print(
                f"⚠️ NSE request attempt "
                f"{attempt}/{retries}: {last_error}"
            )

        except Exception as exc:
            last_error = str(exc)

            print(
                f"⚠️ NSE request attempt "
                f"{attempt}/{retries} failed: {exc}"
            )

        if attempt < retries:
            time.sleep(2 * attempt)

    raise RuntimeError(
        f"NSE request failed after {retries} attempts: "
        f"{last_error}"
    )


# ============================================================
# EXPIRY
# ============================================================

def get_nearest_expiry(session, symbol):
    """
    NSE v3 flow:
        option-chain-contract-info
            ↓
        expiryDates[0]
    """

    print(f"📅 Fetching expiry information for {symbol}...")

    data = nse_get(
        session,
        NSE_CONTRACT_INFO,
        params={
            "symbol": symbol
        },
    )

    expiry_dates = data.get("expiryDates", [])

    if not expiry_dates:

        # Some NSE responses can expose the information
        # in slightly different locations.
        records = data.get("records", {})

        expiry_dates = records.get(
            "expiryDates",
            []
        )

    if not expiry_dates:
        raise RuntimeError(
            f"No expiry dates returned by NSE for {symbol}."
        )

    # Parse and sort defensively.
    parsed = []

    for expiry in expiry_dates:

        try:
            dt = datetime.strptime(
                expiry,
                "%d-%b-%Y"
            )

            parsed.append(
                (dt, expiry)
            )

        except ValueError:
            continue

    if not parsed:
        raise RuntimeError(
            f"Could not parse NSE expiry dates: "
            f"{expiry_dates}"
        )

    parsed.sort(key=lambda x: x[0])

    nearest_expiry = parsed[0][1]

    print(
        f"✅ Nearest expiry for {symbol}: "
        f"{nearest_expiry}"
    )

    return nearest_expiry


# ============================================================
# OPTION CHAIN FETCH
# ============================================================

def fetch_nse_option_chain(symbol, mode):
    """
    Fetch current NSE option chain.

    mode:
        "indices"  -> type=Indices
        "equities" -> type=Equity
    """

    symbol = symbol.upper().strip()

    nse_type = (
        "Indices"
        if mode == "indices"
        else "Equity"
    )

    print(
        f"🔎 Fetching NSE option chain: "
        f"{symbol} ({nse_type})"
    )

    session = create_nse_session()

    warm_nse_session(session)

    expiry = get_nearest_expiry(
        session,
        symbol
    )

    print(
        f"📡 Fetching v3 option chain "
        f"for {symbol} / {expiry}..."
    )

    data = nse_get(
        session,
        NSE_OPTION_CHAIN_V3,
        params={
            "type": nse_type,
            "symbol": symbol,
            "expiry": expiry,
        },
    )

    if not isinstance(data, dict):
        raise RuntimeError(
            "NSE returned an unexpected response."
        )

    records = data.get("records", {})

    rows = records.get("data", [])

    if not rows:
        # v3 responses should normally expose records.data.
        # Keep a fallback for alternate response structures.
        filtered = data.get("filtered", {})
        rows = filtered.get("data", [])

    if not rows:
        raise RuntimeError(
            f"NSE returned no option-chain rows "
            f"for {symbol}."
        )

    print(
        f"✅ Received {len(rows)} NSE option-chain rows."
    )

    return data, expiry


# ============================================================
# NUMERIC HELPERS
# ============================================================

def safe_float(value, default=0.0):

    try:

        if value is None:
            return default

        if isinstance(value, str):
            value = value.replace(",", "").strip()

            if value == "":
                return default

        result = float(value)

        if math.isnan(result) or math.isinf(result):
            return default

        return result

    except (ValueError, TypeError):
        return default


def safe_int(value, default=0):
    return int(safe_float(value, default))


# ============================================================
# BUILD NORMALIZED DATAFRAME
# ============================================================

def build_option_dataframe(raw_data, expiry):
    """
    Convert NSE records.data into a clean dataframe.

    Each row represents one strike.
    """

    records = raw_data.get("records", {})
    rows = records.get("data", [])

    if not rows:
        rows = (
            raw_data
            .get("filtered", {})
            .get("data", [])
        )

    normalized = []

    for item in rows:

        strike = safe_float(
            item.get("strikePrice")
        )

        if strike <= 0:
            continue

        ce = item.get("CE") or {}
        pe = item.get("PE") or {}

        # Some responses can contain multiple expiries.
        # Keep the requested expiry when available.
        ce_expiry = ce.get("expiryDate")
        pe_expiry = pe.get("expiryDate")

        if ce_expiry and ce_expiry != expiry:
            continue

        if pe_expiry and pe_expiry != expiry:
            continue

        normalized.append({
            "Strike": strike,

            "CE_Volume": safe_int(
                ce.get("totalTradedVolume")
                if "totalTradedVolume" in ce
                else ce.get("volume")
            ),

            "PE_Volume": safe_int(
                pe.get("totalTradedVolume")
                if "totalTradedVolume" in pe
                else pe.get("volume")
            ),

            "CE_OI": safe_int(
                ce.get("openInterest")
            ),

            "PE_OI": safe_int(
                pe.get("openInterest")
            ),

            "CE_LTP": safe_float(
                ce.get("lastPrice")
            ),

            "PE_LTP": safe_float(
                pe.get("lastPrice")
            ),

            "CE_Change_OI": safe_int(
                ce.get("changeinOpenInterest")
            ),

            "PE_Change_OI": safe_int(
                pe.get("changeinOpenInterest")
            ),
        })

    df = pd.DataFrame(normalized)

    if df.empty:
        raise RuntimeError(
            "Could not construct option-chain dataframe."
        )

    df = (
        df
        .drop_duplicates(subset=["Strike"])
        .sort_values("Strike")
        .reset_index(drop=True)
    )

    return df


# ============================================================
# SPOT PRICE
# ============================================================

def get_spot_price(raw_data, df):
    """
    Prefer NSE's underlyingValue from CE/PE.
    Fall back to the nearest non-zero underlying value.
    """

    records = raw_data.get("records", {})

    possible_values = [
        records.get("underlyingValue"),
        records.get("underlyingValue"),
    ]

    for value in possible_values:

        value = safe_float(value)

        if value > 0:
            return value

    rows = records.get("data", [])

    for item in rows:

        ce = item.get("CE") or {}
        pe = item.get("PE") or {}

        for side in (ce, pe):

            value = safe_float(
                side.get("underlyingValue")
            )

            if value > 0:
                return value

    # Last-resort estimate from the strike grid.
    # This should rarely be reached.
    if not df.empty:
        return float(df["Strike"].median())

    return 0.0


# ============================================================
# SINGULARITY MATRIX
# ============================================================

def process_singularity_matrix(
    symbol,
    mode,
):
    """
    Core scanner logic.

    Finds:
      - nearest expiry
      - spot
      - ATM
      - ATM +/- 5 strike zone
      - CE volume resistance
      - CE OI resistance
      - PE volume support
      - PE OI support
      - PCR volume
      - PCR OI
      - EOR
      - EOS
    """

    raw_data, expiry = fetch_nse_option_chain(
        symbol,
        mode
    )

    df = build_option_dataframe(
        raw_data,
        expiry
    )

    spot = get_spot_price(
        raw_data,
        df
    )

    if spot <= 0:
        raise RuntimeError(
            "Unable to determine underlying spot price."
        )

    # --------------------------------------------------------
    # Strike step
    # --------------------------------------------------------

    strikes = sorted(
        float(x)
        for x in df["Strike"].unique()
    )

    differences = [
        strikes[i + 1] - strikes[i]
        for i in range(len(strikes) - 1)
        if strikes[i + 1] > strikes[i]
    ]

    step = (
        min(differences)
        if differences
        else 0
    )

    # --------------------------------------------------------
    # ATM
    # --------------------------------------------------------

    atm_index = (
        df["Strike"] - spot
    ).abs().idxmin()

    atm = float(
        df.loc[atm_index, "Strike"]
    )

    # --------------------------------------------------------
    # ATM zone: 5 strikes above + 5 below
    # --------------------------------------------------------

    atm_position = (
        df.index[
            df["Strike"] == atm
        ][0]
    )

    start = max(
        0,
        atm_position - 5
    )

    end = min(
        len(df),
        atm_position + 6
    )

    zone = df.iloc[
        start:end
    ].copy()

    if zone.empty:
        raise RuntimeError(
            "ATM analysis zone is empty."
        )

    # --------------------------------------------------------
    # Resistance
    # --------------------------------------------------------

    ce_volume_row = zone.loc[
        zone["CE_Volume"].idxmax()
    ]

    ce_oi_row = zone.loc[
        zone["CE_OI"].idxmax()
    ]

    resistance_volume = float(
        ce_volume_row["Strike"]
    )

    resistance_oi = float(
        ce_oi_row["Strike"]
    )

    # Primary resistance follows the strongest CE OI
    # with volume used as confirmation.
    resistance = resistance_oi

    resistance_ce_ltp = safe_float(
        ce_oi_row["CE_LTP"]
    )

    # --------------------------------------------------------
    # Support
    # --------------------------------------------------------

    pe_volume_row = zone.loc[
        zone["PE_Volume"].idxmax()
    ]

    pe_oi_row = zone.loc[
        zone["PE_OI"].idxmax()
    ]

    support_volume = float(
        pe_volume_row["Strike"]
    )

    support_oi = float(
        pe_oi_row["Strike"]
    )

    # Primary support follows the strongest PE OI
    # with volume used as confirmation.
    support = support_oi

    support_pe_ltp = safe_float(
        pe_oi_row["PE_LTP"]
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
        else 0
    )

    pcr_oi = (
        total_pe_oi / total_ce_oi
        if total_ce_oi > 0
        else 0
    )

    # --------------------------------------------------------
    # EOR / EOS
    # --------------------------------------------------------

    eor = resistance + resistance_ce_ltp

    eos = support - support_pe_ltp

    # --------------------------------------------------------
    # Strength / confirmation
    # --------------------------------------------------------

    max_ce_volume = float(
        zone["CE_Volume"].max()
    )

    max_pe_volume = float(
        zone["PE_Volume"].max()
    )

    max_ce_oi = float(
        zone["CE_OI"].max()
    )

    max_pe_oi = float(
        zone["PE_OI"].max()
    )

    resistance_status = (
        "STRONG"
        if max_ce_oi > 0 and max_ce_volume > 0
        else "WEAK"
    )

    support_status = (
        "STRONG"
        if max_pe_oi > 0 and max_pe_volume > 0
        else "WEAK"
    )

    # Volume confirmation.
    if (
        pcr_volume > 1
        and total_pe_volume > total_ce_volume
    ):
        eor_type = "CE RESISTANCE"
        eor_status = "PRESSURE ABOVE"
    else:
        eor_type = "CE RESISTANCE"
        eor_status = "POTENTIAL BREAKOUT ZONE"

    if (
        pcr_volume > 1
        and total_pe_volume > total_ce_volume
    ):
        eos_type = "PE SUPPORT"
        eos_status = "BUYING SUPPORT"
    else:
        eos_type = "PE SUPPORT"
        eos_status = "SUPPORT UNDER TEST"

    # --------------------------------------------------------
    # Return metrics
    # --------------------------------------------------------

    return {
        "Asset": symbol,
        "Spot": round(spot, 2),
        "Expiry": expiry,

        "ATM": round(atm, 2),
        "Strike_Step": round(step, 2),

        "Zone_Low": round(
            float(zone["Strike"].min()),
            2
        ),

        "Zone_High": round(
            float(zone["Strike"].max()),
            2
        ),

        "Resistance": round(
            resistance,
            2
        ),

        "Resistance_By_Volume": round(
            resistance_volume,
            2
        ),

        "Resistance_By_OI": round(
            resistance_oi,
            2
        ),

        "Support": round(
            support,
            2
        ),

        "Support_By_Volume": round(
            support_volume,
            2
        ),

        "Support_By_OI": round(
            support_oi,
            2
        ),

        "CE_Max_Volume": int(
            max_ce_volume
        ),

        "PE_Max_Volume": int(
            max_pe_volume
        ),

        "CE_Max_OI": int(
            max_ce_oi
        ),

        "PE_Max_OI": int(
            max_pe_oi
        ),

        "PCR_Vol": round(
            pcr_volume,
            3
        ),

        "PCR_OI": round(
            pcr_oi,
            3
        ),

        "EOR": round(
            eor,
            2
        ),

        "EOR_Type": eor_type,
        "EOR_Status": eor_status,

        "EOS": round(
            eos,
            2
        ),

        "EOS_Type": eos_type,
        "EOS_Status": eos_status,

        "Resistance_Strength":
            resistance_status,

        "Support_Strength":
            support_status,
    }


# ============================================================
# GEMINI ANALYSIS
# ============================================================

def get_gemini_analysis(metrics):
    """
    Gemini combines quantitative option-chain information
    with current web-grounded information.
    """

    if not GEMINI_API_KEY:
        return (
            "⚠️ Gemini analysis skipped: "
            "GEMINI_API_KEY is missing."
        )

    try:
        from google import genai

    except ImportError:
        return (
            "⚠️ Gemini analysis unavailable: "
            "google-genai is not installed."
        )

    print("🤖 Sending scanner data to Gemini...")

    client = genai.Client(
        api_key=GEMINI_API_KEY
    )

    asset = metrics["Asset"]

    prompt = f"""
You are an Indian equity derivatives market-analysis assistant.

Analyze {asset} using BOTH:

1. The quantitative NSE option-chain data supplied below.
2. Current web information found using Google Search.

Do NOT invent news, company developments, regulatory events,
geopolitical events, prices, or market facts.

CURRENT NSE OPTION-CHAIN DATA
-----------------------------

{json.dumps(metrics, indent=2)}

ANALYSIS REQUIREMENTS
---------------------

A. OPTIONS STRUCTURE
- Explain the current ATM and surrounding strike zone.
- Interpret CE/PE OI.
- Interpret CE/PE volume.
- Interpret PCR by volume and OI.
- Explain the strongest apparent support and resistance.
- Explain EOR and EOS.
- Identify whether volume confirms or contradicts OI.

B. COMPANY / INDEX CONTEXT
For an individual company:
- Search for recent material company developments.
- Results, management commentary, major projects,
  acquisitions, partnerships, product/business developments,
  regulatory developments and sector-specific events.

For an index:
- Search for current macroeconomic and market factors
  relevant to that index.

C. CURRENT NEWS
Search for genuinely recent developments that could materially
affect the asset.

D. GEOPOLITICAL / MACRO CONTEXT
Consider only developments that have a plausible transmission
channel to the company/index, such as:
- crude oil
- trade restrictions
- tariffs
- sanctions
- supply-chain disruption
- currency movements
- interest rates
- major geopolitical conflicts
- government/regulatory changes

Do not force geopolitical explanations when they are irrelevant.

E. SYNTHESIS
Separate:
- What the option-chain data says
- What current external information says
- Where the two agree
- Where they conflict

F. SCENARIOS
Give:
- Bullish scenario
- Bearish scenario
- Key invalidation/risk factors

Do NOT present this as guaranteed financial advice.
Do not claim certainty or guaranteed price targets.

Use concise, Telegram-friendly formatting.

End with:

"Key takeaway:"
followed by 2-4 sentences summarizing the evidence,
uncertainties and major levels to watch.

Also include a short "Sources:" section with the most relevant
web sources used.
"""

    try:

        interaction = client.interactions.create(
            model="gemini-3.8-flash",
            input=prompt,
            tools=[
                {
                    "type": "google_search"
                }
            ],
        )

        analysis = interaction.output_text

        if not analysis:
            return (
                "⚠️ Gemini returned an empty analysis."
            )

        print("✅ Gemini analysis generated.")

        # Extract grounded URLs from Gemini annotations.
        sources = []

        try:
            for step in interaction.steps:

                if getattr(step, "type", None) != "model_output":
                    continue

                for block in getattr(
                    step,
                    "content",
                    []
                ):

                    annotations = getattr(
                        block,
                        "annotations",
                        None
                    )

                    if not annotations:
                        continue

                    for annotation in annotations:

                        if (
                            getattr(
                                annotation,
                                "type",
                                None
                            )
                            == "url_citation"
                        ):

                            url = getattr(
                                annotation,
                                "url",
                                None
                            )

                            title = getattr(
                                annotation,
                                "title",
                                None
                            )

                            if url and url not in [
                                x[0] for x in sources
                            ]:
                                sources.append(
                                    (
                                        url,
                                        title or url
                                    )
                                )

        except Exception as citation_error:
            print(
                f"⚠️ Citation extraction warning: "
                f"{citation_error}"
            )

        # Add source URLs only if Gemini returned them.
        if sources:

            source_lines = [
                "\n\n<b>Sources:</b>"
            ]

            for index, (url, title) in enumerate(
                sources[:6],
                start=1
            ):
                source_lines.append(
                    f'{index}. <a href="{url}">'
                    f"{title}</a>"
                )

            analysis += "\n".join(
                source_lines
            )

        return analysis

    except Exception as exc:

        print(
            f"❌ Gemini analysis failed: "
            f"{type(exc).__name__}: {exc}"
        )

        return (
            "⚠️ Gemini analysis failed.\n"
            f"Reason: {str(exc)[:500]}"
        )


# ============================================================
# TELEGRAM
# ============================================================

def telegram_request(method, payload):
    """
    Telegram Bot API helper.
    """

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing."
        )

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/{method}"
    )

    response = curl_requests.post(
        url,
        json=payload,
        timeout=30,
        impersonate="chrome",
    )

    if response.status_code != 200:

        raise RuntimeError(
            f"Telegram HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API error: "
            f"{result}"
        )

    return result


def send_telegram_message(
    text,
    parse_mode="HTML"
):
    """
    Telegram has a ~4096 character limit.
    Split long Gemini responses safely.
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

        if split_at <= 0:
            split_at = max_length

        chunks.append(
            text[:split_at]
        )

        text = text[split_at:]

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
            },
        )

        time.sleep(0.5)


# ============================================================
# TELEGRAM MATRIX ALERT
# ============================================================

def send_matrix_telegram_alert(metrics):
    """
    Sends the quantitative scanner result followed by
    Gemini's contextual analysis.
    """

    pcr_vol = metrics["PCR_Vol"]

    if pcr_vol > 1:
        signal = (
            "🟢 VOL BREAKOUT SUPPORTED"
        )
    else:
        signal = (
            "🔴 LIQUIDITY RESISTANCE BLOCKED"
        )

    message = f"""
<b>⚡ SINGULARITY MATRIX</b>

<b>Asset:</b> {metrics["Asset"]}
<b>Spot:</b> {metrics["Spot"]}
<b>Expiry:</b> {metrics["Expiry"]}

<b>ATM:</b> {metrics["ATM"]}
<b>Zone:</b> {metrics["Zone_Low"]} → {metrics["Zone_High"]}

<b>Resistance:</b> {metrics["Resistance"]}
<b>Resistance by Volume:</b> {metrics["Resistance_By_Volume"]}
<b>Resistance by OI:</b> {metrics["Resistance_By_OI"]}

<b>Support:</b> {metrics["Support"]}
<b>Support by Volume:</b> {metrics["Support_By_Volume"]}
<b>Support by OI:</b> {metrics["Support_By_OI"]}

<b>PCR Volume:</b> {metrics["PCR_Vol"]}
<b>PCR OI:</b> {metrics["PCR_OI"]}

<b>EOR:</b> {metrics["EOR"]}
<b>EOS:</b> {metrics["EOS"]}

<b>Resistance Strength:</b>
{metrics["Resistance_Strength"]}

<b>Support Strength:</b>
{metrics["Support_Strength"]}

<b>Signal:</b>
{signal}
""".strip()

    send_telegram_message(
        message
    )

    # Gemini contextual analysis
    gemini_analysis = get_gemini_analysis(
        metrics
    )

    send_telegram_message(
        "<b>🤖 GEMINI CONTEXTUAL ANALYSIS</b>\n\n"
        + gemini_analysis
    )


# ============================================================
# SAFETY / DATA GUARD
# ============================================================

def tragedy_guard(metrics):
    """
    Basic sanity checks before sending results.
    """

    required = [
        "Asset",
        "Spot",
        "Expiry",
        "PCR_Vol",
        "PCR_OI",
        "EOR",
        "EOS",
    ]

    for field in required:

        if field not in metrics:
            raise RuntimeError(
                f"Missing scanner metric: {field}"
            )

    if metrics["Spot"] <= 0:
        raise RuntimeError(
            "Invalid spot price."
        )

    if metrics["EOR"] <= 0:
        raise RuntimeError(
            "Invalid EOR."
        )

    if metrics["EOS"] <= 0:
        raise RuntimeError(
            "Invalid EOS."
        )

    return True


# ============================================================
# MAIN
# ============================================================

def main():

    # GitHub Actions passes:
    # python scanner.py "$TICKER"

    if len(sys.argv) > 1:
        ticker = sys.argv[1]

    else:
        ticker = os.getenv(
            "TELEGRAM_INPUT_TICKER",
            "NIFTY"
        )

    ticker = (
        ticker
        .strip()
        .upper()
    )

    if not ticker:
        ticker = "NIFTY"

    print("=" * 60)
    print("🚀 ALGORITHMIC OPTIONS SCANNER")
    print("=" * 60)

    print(f"Ticker: {ticker}")

    print(
        f"TELEGRAM_BOT_TOKEN: "
        f"{'***' if TELEGRAM_BOT_TOKEN else 'MISSING'}"
    )

    print(
        f"TELEGRAM_CHAT_ID: "
        f"{'***' if TELEGRAM_CHAT_ID else 'MISSING'}"
    )

    print(
        f"GEMINI_API_KEY: "
        f"{'***' if GEMINI_API_KEY else 'MISSING'}"
    )

    if ticker in INDEX_SYMBOLS:
        mode = "indices"

        print(
            "📊 Index detected. "
            "Using NSE Indices option chain."
        )

    else:
        mode = "equities"

        print(
            "📈 Equity detected. "
            "Using NSE Equity option chain."
        )

    try:

        print(
            f"\n🔍 Scanning {ticker}..."
        )

        metrics = process_singularity_matrix(
            ticker,
            mode
        )

        print("\n📊 MATRIX RESULT")
        print(
            json.dumps(
                metrics,
                indent=2
            )
        )

        tragedy_guard(
            metrics
        )

        print(
            "\n📨 Sending Telegram report..."
        )

        send_matrix_telegram_alert(
            metrics
        )

        print(
            "\n✅ COMPLETE"
        )

    except Exception as exc:

        print(
            f"\n❌ Core API Sync Failure "
            f"for {ticker}:"
        )

        print(
            f"{type(exc).__name__}: {exc}"
        )

        print(
            "⚠️ Matrix processing halted."
        )

        # Try to notify Telegram about the failure.
        try:

            send_telegram_message(
                f"<b>❌ Scanner Failed</b>\n\n"
                f"<b>Asset:</b> {ticker}\n"
                f"<b>Error:</b> "
                f"{type(exc).__name__}: "
                f"{str(exc)[:700]}"
            )

        except Exception as telegram_error:

            print(
                "⚠️ Could not send failure "
                f"notification: {telegram_error}"
            )

        sys.exit(1)


if __name__ == "__main__":
    main()