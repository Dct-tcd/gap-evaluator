import os
import sys
import numpy as np
import yfinance as yf
import pandas as pd
import requests

def predict_stock_range(tickers):
    range_data = []
    
    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            
            # Fetch last 21 days of history to compute 20 days of log returns
            hist = stock.history(period="21d")
            if len(hist) < 5:
                continue
                
            last_session = hist.iloc[-1]
            close = last_session['Close']
            
            # Calculate historical daily volatility using Log Returns
            hist['Log_Returns'] = np.log(hist['Close'] / hist['Close'].shift(1))
            daily_volatility = hist['Log_Returns'].std()
            
            if np.isnan(daily_volatility) or daily_volatility == 0:
                daily_volatility = 0.015
                
            info = stock.info
            live_price = info.get('preMarketPrice')
            
            if live_price is None or live_price == 0:
                live_price = info.get('regularMarketPrice', close)
            if live_price is None or live_price == 0:
                live_price = close

            expected_high = live_price * (1 + daily_volatility)
            expected_low = live_price * (1 - daily_volatility)
            expected_move_pct = daily_volatility * 100
            
            range_data.append({
                "Ticker": ticker.replace(".NS", ""),
                "Base": round(live_price, 2),
                "Low": round(expected_low, 2),
                "High": round(expected_high, 2),
                "Swing": round(expected_move_pct, 2)
            })
            
        except Exception as e:
            print(f"Error processing {ticker}: {e}")
            
    return pd.DataFrame(range_data)

def send_telegram_alert(df):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    
    if not bot_token or not chat_id:
        print("⚠️ Missing Telegram environment configuration. Skipping alert.")
        return

    # --- 🛰️ LIVE DIAGNOSTIC PING BLOCK ---
    print("\n📡 Initiating API Diagnostic Ping...")
    ping_url = f"https://api.telegram.org/bot{bot_token}/getMe"
    try:
        ping_response = requests.get(ping_url)
        if ping_response.status_code == 200:
            bot_info = ping_response.json()
            print(f"✅ Connection Stable! Authenticated as Bot: @{bot_info['result']['username']}")
        elif ping_response.status_code == 404:
            print("❌ Diagnostic Failed: HTTP 404 Not Found.")
            print("👉 CRITICAL: Your TELEGRAM_BOT_TOKEN is invalid. Check for typos or extra spaces in your GitHub Secrets.")
            return
        else:
            print(f"⚠️ Unexpected Ping Response ({ping_response.status_code}): {ping_response.text}")
    except Exception as e:
        print(f"❌ Network Level Failure connecting to Telegram: {e}")
        return
    # -------------------------------------

    # Build a clean plain text table using HTML pre-formatting tag
    message = "🎯 <b>INTRADAY RANGE FORECAST (68% Prob)</b>\n"
    message += "===================================\n"
    message += f"{'Ticker':<10} | {'Low':<8} | {'High':<8} | {'Swing'}\n"
    message += "-----------------------------------\n"
    
    for _, row in df.iterrows():
        message += f"{row['Ticker']:<10} | {int(row['Low']):<8,} | {int(row['High']):<8,} | ±{row['Swing']}%\n"
        
    message += "===================================\n"
    message += "<i>MAPPED VIA LOG RETURNS ALGORITHMIC SCAN</i>"

    # Wrap the entire string in HTML <pre> tags to lock monospace spacing without breaking characters
    formatted_text = f"<pre>{message}</pre>"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": formatted_text,
        "parse_mode": "HTML"
    }
    
    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            print("⚡ Telegram range notification pinged successfully.")
        else:
            print(f"\n❌ Core Alert Transmission Failed (Status Code: {response.status_code})")
            print(f"Response Payload: {response.text}")
            print("👉 If the connection ping passed above but this failed, your TELEGRAM_CHAT_ID is wrong, or you forgot to hit /start in the chat with your bot.")
    except Exception as e:
        print(f"❌ HTTP request to Telegram failed: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].strip() != '':
        target_ticker = sys.argv[1].upper()
        if not target_ticker.endswith(".NS") and not target_ticker.endswith(".BO"):
            target_ticker += ".NS"
        ticker_universe = [target_ticker]
    else:
        ticker_universe = [
            "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
            "SBIN.NS", "BHARTIARTL.NS", "ITC.NS", "LTIM.NS", "HINDUNILVR.NS",
            "LT.NS", "AXISBANK.NS", "KOTAKBANK.NS", "BAJFINANCE.NS", "M&M.NS",
            "MARUTI.NS", "SUNPHARMA.NS", "ADANIENT.NS", "TATAMOTORS.NS", "WIPRO.NS"
        ]
        
    print(f"--- Processing Range Model Across {len(ticker_universe)} Elements ---")
    df_results = predict_stock_range(ticker_universe)
    
    if df_results.empty:
        print("❌ No matrix generated.")
        sys.exit()

    # Sort sequentially by expected volatility magnitude
    df_results = df_results.sort_values(by="Swing", ascending=False)
    
    # Deliver directly to your chat window
    send_telegram_alert(df_results)
