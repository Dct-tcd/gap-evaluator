import sys
import yfinance as yf
import pandas as pd

def analyze_gap_candidates(tickers):
    gap_data = []
    
    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            
            # Fetch last 5 days of history for momentum baseline
            hist = stock.history(period="5d")
            if len(hist) < 2:
                continue
                
            last_session = hist.iloc[-1]
            close = last_session['Close']
            high = last_session['High']
            low = last_session['Low']
            volume = last_session['Volume']
            avg_volume = hist['Volume'].mean()
            
            day_range = high - low
            if day_range == 0: 
                continue
            
            # Historical momentum: close proximity to daily extremes
            close_position = (close - low) / day_range 
            rel_volume = volume / avg_volume if avg_volume > 0 else 1
            
            # Target explicit Pre-Market ticks via the .info payload
            info = stock.info
            live_price = info.get('preMarketPrice')
            
            # Fallback patterns if pre-market trading hasn't registered a tick yet
            if live_price is None or live_price == 0:
                live_price = info.get('regularMarketPrice', close)
            if live_price is None or live_price == 0:
                live_price = close
                
            # Calculate mathematical expected gap
            expected_gap_pct = ((live_price - close) / close) * 100
            
            prediction = "Neutral"
            confidence = "Low"
            
            # Logic boundaries for identifying Gap Ups / Gap Downs
            if expected_gap_pct > 0.4 or (close_position > 0.85 and rel_volume > 1.3):
                prediction = "Potential GAP UP"
                confidence = "High" if expected_gap_pct > 0.8 else "Medium"
            elif expected_gap_pct < -0.4 or (close_position < 0.15 and rel_volume > 1.3):
                prediction = "Potential GAP DOWN"
                confidence = "High" if expected_gap_pct < -0.8 else "Medium"
                
            gap_data.append({
                "Ticker": ticker.replace(".NS", ""),
                "Prev Close": round(close, 2),
                "Pre-Mkt Price": round(live_price, 2),
                "Expected Gap %": round(expected_gap_pct, 2),
                "Rel Vol": round(rel_volume, 2),
                "Prediction": prediction,
                "Confidence": confidence
            })
            
        except Exception as e:
            print(f"Error processing {ticker}: {e}")
            
    return pd.DataFrame(gap_data)

if __name__ == "__main__":
    # Check if a specific ticker argument was passed via command line
    # Usage: python scanner.py WIPRO.NS
    if len(sys.argv) > 1:
        target_ticker = sys.argv[1].upper()
        # Append .NS automatically if you forget to provide it
        if not target_ticker.endswith(".NS") and not target_ticker.endswith(".BO"):
            target_ticker += ".NS"
        ticker_universe = [target_ticker]
        print(f"--- Running Targeted Scan For: {target_ticker} ---")
    else:
        # Default Expanded Universe: 20 Blue-chip Indian Market Movers
        ticker_universe = [
            "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
            "SBIN.NS", "BHARTIARTL.NS", "ITC.NS", "LTIM.NS", "HINDUNILVR.NS",
            "LT.NS", "AXISBANK.NS", "KOTAKBANK.NS", "BAJFINANCE.NS", "M&M.NS",
            "MARUTI.NS", "SUNPHARMA.NS", "ADANIENT.NS", "TATAMOTORS.NS", "WIPRO.NS"
        ]
        print(f"--- Running Bulk Indian Market Pre-Market Scan ({len(ticker_universe)} tickers) ---")
    
    results = analyze_gap_candidates(ticker_universe)
    
    # Sort results by the size of the expected gap edge
    if not results.empty:
        results = results.sort_values(by="Expected Gap %", ascending=False)
        print("\n[SCAN RESULTS]")
        print(results.to_string(index=False))
    else:
        print("\nNo data retrieved.")
