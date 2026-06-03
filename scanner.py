import sys
import numpy as np
import yfinance as yf
import pandas as pd

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
            
            # 1. Calculate historical daily volatility using Log Returns
            hist['Log_Returns'] = np.log(hist['Close'] / hist['Close'].shift(1))
            daily_volatility = hist['Log_Returns'].std()
            
            # Handle edge cases for zero variance or lack of data
            if np.isnan(daily_volatility) or daily_volatility == 0:
                daily_volatility = 0.015 # Fallback to a default 1.5% daily variation baseline
                
            # 2. Extract explicit Pre-Market ticks or fallback to latest close
            info = stock.info
            live_price = info.get('preMarketPrice')
            
            if live_price is None or live_price == 0:
                live_price = info.get('regularMarketPrice', close)
            if live_price is None or live_price == 0:
                live_price = close

            # 3. Calculate 1-Standard Deviation Expected High & Low Boundaries
            # Statistically, price action contains itself within this range 68.2% of the session
            expected_high = live_price * (1 + daily_volatility)
            expected_low = live_price * (1 - daily_volatility)
            
            # Expected maximum intraday move bandwidth in percentage terms
            expected_move_pct = daily_volatility * 100
            
            range_data.append({
                "Ticker": ticker.replace(".NS", ""),
                "Current/Pre-Mkt": round(live_price, 2),
                "Expected Low": round(expected_low, 2),
                "Expected High": round(expected_high, 2),
                "Expected Move %": round(expected_move_pct, 2)
            })
            
        except Exception as e:
            print(f"Error processing {ticker}: {e}")
            
    return pd.DataFrame(range_data)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].strip() != '':
        target_ticker = sys.argv[1].upper()
        if not target_ticker.endswith(".NS") and not target_ticker.endswith(".BO"):
            target_ticker += ".NS"
        ticker_universe = [target_ticker]
        print(f"--- 🎯 Targeted Range Prediction Activated For: {target_ticker} ---")
    else:
        ticker_universe = [
            "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
            "SBIN.NS", "BHARTIARTL.NS", "ITC.NS", "LTIM.NS", "HINDUNILVR.NS",
            "LT.NS", "AXISBANK.NS", "KOTAKBANK.NS", "BAJFINANCE.NS", "M&M.NS",
            "MARUTI.NS", "SUNPHARMA.NS", "ADANIENT.NS", "TATAMOTORS.NS", "WIPRO.NS"
        ]
        print(f"--- 📊 Running Bulk Intraday Range Forecasting ({len(ticker_universe)} Items) ---")
    
    df_results = predict_stock_range(ticker_universe)
    
    if df_results.empty:
        print("❌ No market data could be compiled.")
        sys.exit()

    # Sort sequentially by expected volatility magnitude
    df_results = df_results.sort_values(by="Expected Move %", ascending=False)
    
    # --- Clean Display Presentation Block ---
    print("\n" + "="*72)
    print("      🎯 MATHEMATICAL 1-STANDARD DEVIATION RANGE FORECAST (68% Probability)      ")
    print("="*72)
    print(f"{'Ticker':<12} | {'Base Price':<12} | {'Expected Low':<14} | {'Expected High':<14} | {'Est. Swing %':<10}")
    print("-"*72)
    
    for _, row in df_results.iterrows():
        print(f"{row['Ticker']:<12} | {row['Current/Pre-Mkt']:<12,2f} | {row['Expected Low']:<14,2f} | {row['Expected High']:<14,2f} | ±{row['Expected Move %']}%")
        
    print("="*72)
    print("Statistically, the security price is expected to stay inside this range tomorrow.")
