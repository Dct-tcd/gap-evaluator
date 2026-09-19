import os
import sys
import math
import html
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from google import genai
from google.genai import types


# ============================================================
# CONFIG
# ============================================================

GEMINI_MODEL = "gemini-3.8-flash"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Yahoo Finance symbols for Indian equities.
# User can enter RELIANCE, TCS, INFY, HDFCBANK, etc.
# We try NSE first and BSE second.
BENCHMARK = "^NSEI"       # NIFTY 50
VOLATILITY_INDEX = "^INDIAVIX"


# ============================================================
# GENERIC HELPERS
# ============================================================

def safe_float(value, default=None):
    try:
        if value is None:
            return default
        value = float(value)
        if not math.isfinite(value):
            return default
        return value
    except (TypeError, ValueError):
        return default


def fmt_num(value, decimals=2):
    value = safe_float(value)
    if value is None:
        return "N/A"
    return f"{value:,.{decimals}f}"


def fmt_pct(value, decimals=2):
    value = safe_float(value)
    if value is None:
        return "N/A"
    return f"{value:+.{decimals}f}%"


def fmt_money(value):
    value = safe_float(value)
    if value is None:
        return "N/A"

    if abs(value) >= 1e12:
        return f"₹{value / 1e12:.2f}T"
    if abs(value) >= 1e9:
        return f"₹{value / 1e9:.2f}B"
    if abs(value) >= 1e7:
        return f"₹{value / 1e7:.2f}Cr"
    if abs(value) >= 1e5:
        return f"₹{value / 1e5:.2f}L"
    return f"₹{value:,.2f}"


def clean_ticker(raw):
    ticker = (raw or "").strip().upper()

    for suffix in (".NS", ".BO"):
        if ticker.endswith(suffix):
            ticker = ticker[:-3]

    ticker = ticker.replace(" ", "")

    if not ticker:
        return "RELIANCE"

    return ticker


def yahoo_candidates(ticker):
    # Allow direct Yahoo symbols such as ^NSEI.
    if ticker.startswith("^"):
        return [ticker]

    return [f"{ticker}.NS", f"{ticker}.BO"]


def first_not_none(*values):
    for value in values:
        if value is not None:
            return value
    return None


# ============================================================
# YAHOO FINANCE
# ============================================================

def resolve_yahoo_ticker(ticker):
    """
    Resolve an Indian equity to NSE first, then BSE.
    """
    last_error = None

    for symbol in yahoo_candidates(ticker):
        try:
            stock = yf.Ticker(symbol)
            hist = stock.history(period="5d", auto_adjust=False)

            if hist is not None and not hist.empty:
                print(f"Yahoo symbol resolved: {symbol}")
                return symbol, stock
        except Exception as exc:
            last_error = exc

    raise RuntimeError(
        f"Could not resolve {ticker} on Yahoo Finance. "
        f"Tried {yahoo_candidates(ticker)}. Error: {last_error}"
    )


def clean_dataframe(df):
    if df is None or df.empty:
        return None

    result = df.copy()

    # yfinance can occasionally return timezone-aware indexes.
    try:
        result.index = pd.to_datetime(result.index)
    except Exception:
        pass

    return result


def calculate_rsi(close, period=14):
    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    return rsi


def calculate_atr(df, period=14):
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    previous_close = close.shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return true_range.rolling(period).mean()


def calculate_technical_snapshot(hist):
    hist = clean_dataframe(hist)

    if hist is None or len(hist) < 30:
        raise RuntimeError(
            "Not enough historical price data to calculate the technical snapshot."
        )

    close = hist["Close"].dropna()
    volume = hist["Volume"].fillna(0)

    latest_price = safe_float(close.iloc[-1])
    previous_close = safe_float(close.iloc[-2])

    sma20 = safe_float(close.rolling(20).mean().iloc[-1])
    sma50 = safe_float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
    sma200 = safe_float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None

    rsi_series = calculate_rsi(close)
    rsi14 = safe_float(rsi_series.iloc[-1])

    atr_series = calculate_atr(hist)
    atr14 = safe_float(atr_series.iloc[-1])

    avg_volume20 = safe_float(volume.rolling(20).mean().iloc[-1])
    latest_volume = safe_float(volume.iloc[-1])

    volume_ratio = None
    if avg_volume20 and avg_volume20 > 0:
        volume_ratio = latest_volume / avg_volume20

    def period_return(days):
        if len(close) <= days:
            return None
        old = safe_float(close.iloc[-days - 1])
        if old in (None, 0):
            return None
        return ((latest_price / old) - 1) * 100

    recent_20 = close.tail(20)
    recent_60 = close.tail(min(60, len(close)))

    high20 = safe_float(recent_20.max())
    low20 = safe_float(recent_20.min())

    high60 = safe_float(recent_60.max())
    low60 = safe_float(recent_60.min())

    distance_from_high20 = (
        ((latest_price / high20) - 1) * 100
        if latest_price is not None and high20
        else None
    )

    distance_from_low20 = (
        ((latest_price / low20) - 1) * 100
        if latest_price is not None and low20
        else None
    )

    return {
        "latest_price": latest_price,
        "previous_close": previous_close,
        "return_1d": period_return(1),
        "return_5d": period_return(5),
        "return_20d": period_return(20),
        "return_60d": period_return(60),
        "sma20": sma20,
        "sma50": sma50,
        "sma200": sma200,
        "rsi14": rsi14,
        "atr14": atr14,
        "atr_pct": (atr14 / latest_price * 100)
        if atr14 is not None and latest_price
        else None,
        "latest_volume": latest_volume,
        "avg_volume20": avg_volume20,
        "volume_ratio": volume_ratio,
        "high20": high20,
        "low20": low20,
        "high60": high60,
        "low60": low60,
        "distance_from_high20": distance_from_high20,
        "distance_from_low20": distance_from_low20,
        "above_sma20": latest_price > sma20 if latest_price and sma20 else None,
        "above_sma50": latest_price > sma50 if latest_price and sma50 else None,
        "above_sma200": latest_price > sma200 if latest_price and sma200 else None,
    }


def calculate_benchmark_snapshot():
    try:
        benchmark = yf.Ticker(BENCHMARK)
        hist = benchmark.history(period="6mo", auto_adjust=False)

        if hist is None or hist.empty:
            return {}

        close = hist["Close"].dropna()

        latest = safe_float(close.iloc[-1])
        return {
            "nifty_price": latest,
            "nifty_1d": (
                ((latest / safe_float(close.iloc[-2])) - 1) * 100
                if len(close) >= 2 and safe_float(close.iloc[-2])
                else None
            ),
            "nifty_5d": (
                ((latest / safe_float(close.iloc[-6])) - 1) * 100
                if len(close) >= 6 and safe_float(close.iloc[-6])
                else None
            ),
            "nifty_20d": (
                ((latest / safe_float(close.iloc[-21])) - 1) * 100
                if len(close) >= 21 and safe_float(close.iloc[-21])
                else None
            ),
        }
    except Exception as exc:
        print(f"Benchmark data unavailable: {exc}")
        return {}


def calculate_vix():
    try:
        vix = yf.Ticker(VOLATILITY_INDEX)
        hist = vix.history(period="10d", auto_adjust=False)

        if hist is None or hist.empty:
            return None

        return safe_float(hist["Close"].dropna().iloc[-1])
    except Exception as exc:
        print(f"India VIX unavailable: {exc}")
        return None


# ============================================================
# FUNDAMENTALS
# ============================================================

def extract_info(stock):
    try:
        info = stock.get_info()
        if isinstance(info, dict):
            return info
    except Exception as exc:
        print(f"Yahoo info unavailable: {exc}")

    try:
        info = stock.info
        if isinstance(info, dict):
            return info
    except Exception as exc:
        print(f"Yahoo fallback info unavailable: {exc}")

    return {}


def get_financial_statement_snapshot(stock):
    """
    Pull a compact set of financial metrics from Yahoo.
    Missing fields are kept as N/A rather than guessed.
    """
    result = {
        "revenue": None,
        "net_income": None,
        "operating_cashflow": None,
        "free_cashflow": None,
        "total_debt": None,
        "cash": None,
        "equity": None,
    }

    try:
        income = stock.quarterly_income_stmt
        if income is not None and not income.empty:
            col = income.columns[0]

            def row_value(names):
                for name in names:
                    if name in income.index:
                        return safe_float(income.loc[name, col])
                return None

            result["revenue"] = row_value(["Total Revenue", "Operating Revenue"])
            result["net_income"] = row_value(["Net Income", "Net Income Common Stockholders"])
    except Exception as exc:
        print(f"Income statement unavailable: {exc}")

    try:
        cashflow = stock.quarterly_cashflow
        if cashflow is not None and not cashflow.empty:
            col = cashflow.columns[0]

            def row_value(names):
                for name in names:
                    if name in cashflow.index:
                        return safe_float(cashflow.loc[name, col])
                return None

            result["operating_cashflow"] = row_value(
                ["Operating Cash Flow", "Total Cash From Operating Activities"]
            )
            result["free_cashflow"] = row_value(["Free Cash Flow"])
    except Exception as exc:
        print(f"Cash-flow statement unavailable: {exc}")

    try:
        balance = stock.quarterly_balance_sheet
        if balance is not None and not balance.empty:
            col = balance.columns[0]

            def row_value(names):
                for name in names:
                    if name in balance.index:
                        return safe_float(balance.loc[name, col])
                return None

            result["total_debt"] = row_value(
                ["Total Debt", "Total Debt And Capital Lease Obligation"]
            )
            result["cash"] = row_value(
                ["Cash Cash Equivalents And Short Term Investments",
                 "Cash And Cash Equivalents"]
            )
            result["equity"] = row_value(
                ["Stockholders Equity", "Common Stock Equity"]
            )
    except Exception as exc:
        print(f"Balance sheet unavailable: {exc}")

    return result


def calculate_fundamental_snapshot(info, statements):
    return {
        "company_name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "website": info.get("website"),
        "market_cap": info.get("marketCap"),
        "trailing_pe": first_not_none(
            info.get("trailingPE"),
            info.get("forwardPE"),
        ),
        "forward_pe": info.get("forwardPE"),
        "price_to_book": info.get("priceToBook"),
        "enterprise_to_ebitda": info.get("enterpriseToEbitda"),
        "peg_ratio": info.get("pegRatio"),
        "dividend_yield": info.get("dividendYield"),
        "return_on_equity": info.get("returnOnEquity"),
        "return_on_assets": info.get("returnOnAssets"),
        "profit_margin": info.get("profitMargins"),
        "operating_margin": info.get("operatingMargins"),
        "revenue_growth": info.get("revenueGrowth"),
        "earnings_growth": info.get("earningsGrowth"),
        "debt_to_equity": info.get("debtToEquity"),
        "current_ratio": info.get("currentRatio"),
        "beta": info.get("beta"),
        **statements,
    }


def get_calendar_snapshot(stock):
    try:
        calendar = stock.calendar

        if calendar is None:
            return {}

        if isinstance(calendar, dict):
            return calendar

        # Newer yfinance versions can expose a DataFrame.
        if isinstance(calendar, pd.DataFrame):
            return calendar.to_dict()

    except Exception as exc:
        print(f"Calendar unavailable: {exc}")

    return {}


def get_yahoo_news(stock):
    try:
        news = stock.get_news(count=10, tab="all")

        if not news:
            return []

        cleaned = []

        for item in news[:10]:
            content = item.get("content", item)

            title = content.get("title")
            publisher = content.get("provider", {}).get("displayName")
            url = (
                content.get("canonicalUrl", {}).get("url")
                or content.get("clickThroughUrl", {}).get("url")
            )

            if title:
                cleaned.append({
                    "title": title,
                    "publisher": publisher,
                    "url": url,
                })

        return cleaned

    except Exception as exc:
        print(f"Yahoo news unavailable: {exc}")
        return []


# ============================================================
# SERIALIZATION FOR GEMINI
# ============================================================

def compact_json_safe(obj):
    """
    Convert numpy/pandas/date-like objects into JSON-friendly strings.
    """
    if obj is None:
        return None

    if isinstance(obj, (str, int, float, bool)):
        if isinstance(obj, float) and not math.isfinite(obj):
            return None
        return obj

    if isinstance(obj, (np.integer,)):
        return int(obj)

    if isinstance(obj, (np.floating,)):
        value = float(obj)
        return value if math.isfinite(value) else None

    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()

    if isinstance(obj, dict):
        return {str(k): compact_json_safe(v) for k, v in obj.items()}

    if isinstance(obj, list):
        return [compact_json_safe(v) for v in obj]

    return str(obj)


def build_research_packet(
    ticker,
    yahoo_symbol,
    technical,
    fundamentals,
    benchmark,
    vix,
    calendar,
    yahoo_news,
):
    return {
        "analysis_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "yahoo_symbol": yahoo_symbol,
        "market": "India",
        "benchmark": "NIFTY 50",
        "technical": compact_json_safe(technical),
        "fundamentals": compact_json_safe(fundamentals),
        "market_context": compact_json_safe({
            "benchmark": benchmark,
            "india_vix": vix,
        }),
        "upcoming_events": compact_json_safe(calendar),
        "recent_yahoo_news": compact_json_safe(yahoo_news),
    }


# ============================================================
# GEMINI ANALYSIS
# ============================================================

def build_gemini_prompt(packet):
    return f"""
You are an evidence-driven stock research assistant for an Indian investor.

The investor enters an NSE/BSE stock and wants to understand whether the
CURRENT PRICE looks attractive or whether waiting makes more sense, with
special emphasis on the NEXT 3-7 TRADING DAYS.

This is NOT an options-trading system. Do not use option-chain jargon unless
you independently find a genuinely relevant event or source.

You have been given structured market/fundamental data below. You ALSO have
Google Search grounding available. Use Google Search aggressively for
information that can have changed recently.

RESEARCH REQUIREMENTS

1. PRICE / TECHNICALS
Analyze:
- 1D, 5D, 20D and 60D price performance
- 20D / 50D / 200D moving averages when available
- RSI
- ATR / volatility
- recent 20D and 60D high/low
- volume versus 20D average
- relationship with NIFTY 50

2. FUNDAMENTALS
Analyze the available:
- revenue / earnings growth
- margins
- ROE / ROA
- debt
- cash flow
- valuation
- dividend
- sector / industry
Do NOT invent missing numbers.

3. CURRENT NEWS
Search for important developments from roughly the last 7 days:
- company announcements
- exchange filings
- earnings/results
- management commentary
- major contracts
- acquisitions/divestments
- regulatory developments
- lawsuits or material controversies
- analyst estimate changes when credible
- sector-specific developments

Prefer primary sources such as company/exchange/regulator filings, then
high-quality financial news such as Reuters and other reputable outlets.

4. MACRO / SECTOR
Only include macro or geopolitical developments that are plausibly relevant
to THIS company over the next few days.
Examples can include:
- crude oil
- INR
- interest rates
- global risk sentiment
- commodity prices
- government policy
- sector regulation
Do not dump generic macro news.

5. UPCOMING CATALYSTS
Look for events in the next 1-2 weeks that could materially affect price:
earnings, investor meetings, dividends, ex-dates, regulatory decisions,
major announcements, etc.

6. CONFLICT CHECK
Explicitly identify when:
- fundamentals are positive but short-term momentum is weak
- price momentum is positive but valuation/news risk is high
- market-wide conditions conflict with company-specific conditions

7. SHORT-TERM ASSESSMENT
Based on the evidence, classify the next 3-7 trading day setup as exactly
one of:
- BULLISH
- BEARISH
- MIXED

Then give a confidence level:
- LOW
- MEDIUM
- HIGH

Do NOT claim certainty and do NOT invent a numerical probability.

8. CURRENT-PRICE ASSESSMENT
Classify the current price as exactly one of:
- ATTRACTIVE
- WAIT
- ELEVATED RISK

This is a research classification, not a guarantee of future returns.
Explain the evidence behind it.

9. INVALIDATION
Give 2-4 concrete developments or price conditions that would make the
current assessment less reliable.

10. SOURCES
At the end, list the most important sources with publisher + URL when
available. Do not fabricate URLs.

IMPORTANT:
- Never invent current news.
- Never pretend an old event is recent.
- Separate documented facts from your interpretation.
- If data conflicts, say so.
- If information is missing, say "data unavailable".
- Do not give a fake target price.
- Do not use the word "guaranteed".
- Keep the final answer concise enough for Telegram.

OUTPUT FORMAT

STOCK: <ticker>
COMPANY: <name>
AS OF: <timestamp>

SHORT-TERM (3-7 DAYS): <BULLISH / BEARISH / MIXED>
CONFIDENCE: <LOW / MEDIUM / HIGH>

CURRENT PRICE: ₹...
PRICE ASSESSMENT: <ATTRACTIVE / WAIT / ELEVATED RISK>

WHY:
• ...
• ...
• ...

PRICE & MOMENTUM:
• ...
• ...

FUNDAMENTALS:
• ...
• ...

RECENT NEWS:
POSITIVE:
• ...
NEGATIVE:
• ...

CATALYSTS:
• ...

RISKS:
• ...

INVALIDATION:
• ...

BOTTOM LINE:
2-4 concise sentences explaining the overall evidence.

SOURCES:
• Publisher — URL
• Publisher — URL

STRUCTURED DATA:
{compact_json_safe(packet)}
"""


def get_gemini_analysis(packet):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    client = genai.Client(api_key=GEMINI_API_KEY)

    grounding_tool = types.Tool(
        google_search=types.GoogleSearch()
    )

    config = types.GenerateContentConfig(
        tools=[grounding_tool],
        temperature=0.2,
    )

    prompt = build_gemini_prompt(packet)

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=config,
    )

    text = getattr(response, "text", None)

    if not text:
        raise RuntimeError("Gemini returned an empty analysis.")

    return text.strip()


# ============================================================
# TELEGRAM
# ============================================================

def telegram_request(method, payload):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"

    response = requests.post(
        url,
        json=payload,
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")

    return data


def send_telegram_message(message):
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID is not configured.")

    # Telegram message limit is 4096 characters.
    # Keep a little room below the hard limit.
    chunks = [
        message[i:i + 3900]
        for i in range(0, len(message), 3900)
    ]

    for chunk in chunks:
        telegram_request(
            "sendMessage",
            {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "disable_web_page_preview": True,
            },
        )


def build_quant_snapshot(ticker, company_name, technical, fundamentals, benchmark, vix):
    def yes_no(value):
        if value is True:
            return "YES"
        if value is False:
            return "NO"
        return "N/A"

    return f"""
INVESTOR DATA — {ticker}
{company_name or ""}

Price: ₹{fmt_num(technical.get("latest_price"))}
1D: {fmt_pct(technical.get("return_1d"))}
5D: {fmt_pct(technical.get("return_5d"))}
20D: {fmt_pct(technical.get("return_20d"))}
60D: {fmt_pct(technical.get("return_60d"))}

20D MA: ₹{fmt_num(technical.get("sma20"))}
50D MA: ₹{fmt_num(technical.get("sma50"))}
200D MA: ₹{fmt_num(technical.get("sma200"))}

RSI(14): {fmt_num(technical.get("rsi14"))}
ATR(14): ₹{fmt_num(technical.get("atr14"))}
ATR %: {fmt_pct(technical.get("atr_pct"))}

Volume / 20D avg: {fmt_num(technical.get("volume_ratio"))}x
20D high: ₹{fmt_num(technical.get("high20"))}
20D low: ₹{fmt_num(technical.get("low20"))}

Above 20D MA: {yes_no(technical.get("above_sma20"))}
Above 50D MA: {yes_no(technical.get("above_sma50"))}
Above 200D MA: {yes_no(technical.get("above_sma200"))}

Sector: {fundamentals.get("sector") or "N/A"}
Industry: {fundamentals.get("industry") or "N/A"}
Market cap: {fmt_money(fundamentals.get("market_cap"))}
P/E: {fmt_num(fundamentals.get("trailing_pe"))}
Forward P/E: {fmt_num(fundamentals.get("forward_pe"))}
P/B: {fmt_num(fundamentals.get("price_to_book"))}
EV/EBITDA: {fmt_num(fundamentals.get("enterprise_to_ebitda"))}
ROE: {fmt_pct(
    fundamentals.get("return_on_equity") * 100
    if fundamentals.get("return_on_equity") is not None
    and abs(fundamentals.get("return_on_equity")) < 2
    else fundamentals.get("return_on_equity")
)}
Revenue growth: {fmt_pct(
    fundamentals.get("revenue_growth") * 100
    if fundamentals.get("revenue_growth") is not None
    and abs(fundamentals.get("revenue_growth")) < 2
    else fundamentals.get("revenue_growth")
)}
Earnings growth: {fmt_pct(
    fundamentals.get("earnings_growth") * 100
    if fundamentals.get("earnings_growth") is not None
    and abs(fundamentals.get("earnings_growth")) < 2
    else fundamentals.get("earnings_growth")
)}
Debt/Equity: {fmt_num(fundamentals.get("debt_to_equity"))}

NIFTY 1D: {fmt_pct(benchmark.get("nifty_1d")) if benchmark else "N/A"}
NIFTY 5D: {fmt_pct(benchmark.get("nifty_5d")) if benchmark else "N/A"}
NIFTY 20D: {fmt_pct(benchmark.get("nifty_20d")) if benchmark else "N/A"}
INDIA VIX: {fmt_num(vix)}
""".strip()


# ============================================================
# MAIN
# ============================================================

def main():
    raw_ticker = (
        os.getenv("TELEGRAM_INPUT_TICKER")
        or (sys.argv[1] if len(sys.argv) > 1 else "")
        or "RELIANCE"
    )

    ticker = clean_ticker(raw_ticker)

    print("=" * 70)
    print(f"INVESTOR ENGINE — {ticker}")
    print("=" * 70)

    print("Resolving market symbol...")
    yahoo_symbol, stock = resolve_yahoo_ticker(ticker)

    print("Downloading price history...")
    hist = stock.history(
        period="1y",
        interval="1d",
        auto_adjust=False,
    )

    technical = calculate_technical_snapshot(hist)

    print("Downloading company information...")
    info = extract_info(stock)

    print("Downloading financial statements...")
    statements = get_financial_statement_snapshot(stock)

    fundamentals = calculate_fundamental_snapshot(
        info,
        statements,
    )

    print("Checking upcoming events...")
    calendar = get_calendar_snapshot(stock)

    print("Collecting recent Yahoo news...")
    yahoo_news = get_yahoo_news(stock)

    print("Checking NIFTY 50...")
    benchmark = calculate_benchmark_snapshot()

    print("Checking India VIX...")
    vix = calculate_vix()

    packet = build_research_packet(
        ticker=ticker,
        yahoo_symbol=yahoo_symbol,
        technical=technical,
        fundamentals=fundamentals,
        benchmark=benchmark,
        vix=vix,
        calendar=calendar,
        yahoo_news=yahoo_news,
    )

    # Send the raw quantitative snapshot first.
    quantitative = build_quant_snapshot(
        ticker=ticker,
        company_name=fundamentals.get("company_name"),
        technical=technical,
        fundamentals=fundamentals,
        benchmark=benchmark,
        vix=vix,
    )

    print("\n" + quantitative + "\n")

    try:
        send_telegram_message(quantitative)
    except Exception as exc:
        print(f"Telegram quantitative report failed: {exc}")

    # Gemini handles the actual research synthesis.
    print("Running Gemini + Google Search research...")
    try:
        analysis = get_gemini_analysis(packet)

        print("\n" + "=" * 70)
        print("GEMINI INVESTOR ANALYSIS")
        print("=" * 70)
        print(analysis)

        send_telegram_message(
            "GEMINI INVESTOR ANALYSIS\n\n" + analysis
        )

    except Exception as exc:
        print(f"Gemini analysis failed: {type(exc).__name__}: {exc}")
        # Do not hide the useful quantitative report if Gemini is temporarily
        # unavailable. The workflow can still complete successfully.


if __name__ == "__main__":
    main()
