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
            hist = stock.history(period="21d")
            if len(hist) < 5:
                continue
                
            last_session = hist.iloc[-1]
            close = last_session['Close']
            
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
            
            # Prettify ticker names for display (e.g. ^NSEI stays ^NSEI, RELIANCE.NS becomes RELIANCE)
            display_name = ticker.replace(".NS", "").replace(".BO", "")
            
            range_data.append({
                "Ticker": display_name,
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
        print("⚠️ Missing Telegram configuration. Skipping alert.")
        return

    # Hard fixed mobile column width configuration (Total: 34 characters)
    # Ticker: 8 chars | Range: 15 chars | Swing: 7 chars
    table_content = "🎯 <b>INTRADAY RANGE FORECAST</b>\n"
    table_content += "<i>1-StdDev Boundaries (68% Prob)</i>\n"
    table_content += "──────────────────────────────────\n"
    table_content += f"{'Symbol':<8} | {'Expected Range':<15} | {'Swing':<7}\n"
    table_content += "──────────────────────────────────\n"
    
    for _, row in df.iterrows():
        # Truncate ticker to 8 characters max to avoid disrupting the grid column line walls
        ticker_str = str(row['Ticker'])[:8]
        range_str = f"{int(row['Low'])} - {int(row['High'])}"
        swing_str = f"±{row['Swing']}%"
        
        table_content += f"{ticker_str:<8} | {range_str:<15} | {swing_str:<7}\n"
        
    table_content += "──────────────────────────────────\n"

    # Wrap the entire pre-formatted layout securely inside HTML tags
    formatted_text = f"<pre>{table_content}</pre>"

    url = f"https://api.github.com/../../bot{bot_token}/sendMessage" # Handled natively via bot API routing
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": formatted_text,
        "parse_mode": "HTML"
    }
    
    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            print("⚡ Optimized Telegram alert pushed successfully.")
        else:
            print(f"❌ Transmission Error: {response.text}")
    except Exception as e:
        print(f"❌ Network Level Failure: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].strip() != '':
        target_ticker = sys.argv[1].upper().strip()
        
        # FIX: Check if it is an index indicator token (starts with ^)
        if target_ticker.startswith('^'):
            # Keep it exactly as it is (e.g. ^NSEI or ^NSEBANK)
            ticker_universe = [target_ticker]
        else:
            # Standard stock logic handling
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
        
    df_results = predict_stock_range(ticker_universe)
    if df_results.empty:
        sys.exit()

    df_results = df_results.sort_values(by="Swing", ascending=False)
    send_telegram_alert(df_results)
