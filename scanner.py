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
                prediction = "GAP UP"
                confidence = "High" if expected_gap_pct > 0.8 else "Medium"
            elif expected_gap_pct < -0.4 or (close_position < 0.15 and rel_volume > 1.3):
                prediction = "GAP DOWN"
                confidence = "High" if expected_gap_pct < -0.8 else "Medium"
                
            gap_data.append({
                "Ticker": ticker.replace(".NS", ""),
                "Prev Close": round(close, 2),
                "Pre-Mkt": round(live_price, 2),
                "Gap %": round(expected_gap_pct, 2),
                "Rel Vol": round(rel_volume, 2),
                "Direction": prediction,
                "Confidence": confidence
            })
            
        except Exception as e:
            print(f"Error processing {ticker}: {e}")
            
    return pd.DataFrame(gap_data)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].strip() != '':
        target_ticker = sys.argv[1].upper()
        if not target_ticker.endswith(".NS") and not target_ticker.endswith(".BO"):
            target_ticker += ".NS"
        ticker_universe = [target_ticker]
        print(f"--- 🎯 Targeted Scan Activated For: {target_ticker} ---")
    else:
        ticker_universe = [
            "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
            "SBIN.NS", "BHARTIARTL.NS", "ITC.NS", "LTIM.NS", "HINDUNILVR.NS",
            "LT.NS", "AXISBANK.NS", "KOTAKBANK.NS", "BAJFINANCE.NS", "M&M.NS",
            "MARUTI.NS", "SUNPHARMA.NS", "ADANIENT.NS", "TATAMOTORS.NS", "WIPRO.NS"
        ]
        print(f"--- 📊 Running Bulk Pre-Market Scan ({len(ticker_universe)} Items) ---")
    
    df_results = analyze_gap_candidates(ticker_universe)
    
    if df_results.empty:
        print("❌ No market numbers retrieved.")
        sys.exit()

    # Sort sequentially by raw gap edge magnitude
    df_results = df_results.sort_values(by="Gap %", ascending=False)
    
    # Segment data for grouped rendering
    gap_ups = df_results[df_results['Direction'] == 'GAP UP']
    gap_downs = df_results[df_results['Direction'] == 'GAP DOWN']
    neutrals = df_results[df_results['Direction'] == 'Neutral']
    
    # --- Clean Display Presentation Block ---
    print("\n" + "="*65)
    print("      🟢 PRE-MARKET OPENING MOMENTUM RADAR (8:45 AM IST)      ")
    print("="*65)
    
    if not gap_ups.empty:
        print("\n🔥 [POTENTIAL GAP UP CANDIDATES]")
        for _, row in gap_ups.iterrows():
            print(f"  ▲ {row['Ticker']:<12} | Est. Gap: {row['Gap %']:>+6}% | Vol Momentum: {row['Rel Vol']}x | Confidence: {row['Confidence']}")
            
    if not gap_downs.empty:
        print("\n🚨 [POTENTIAL GAP DOWN CANDIDATES]")
        for _, row in gap_downs.iterrows():
            print(f"  ▼ {row['Ticker']:<12} | Est. Gap: {row['Gap %']:>+6}% | Vol Momentum: {row['Rel Vol']}x | Confidence: {row['Confidence']}")
            
    if not neutrals.empty:
        print("\n💤 [NEUTRAL STABILITY ZONE]")
        for _, row in neutrals.iterrows():
            print(f"  • {row['Ticker']:<12} | Est. Gap: {row['Gap %']:>+6}% | Prev Close: {row['Prev Close']}")
            
    print("\n" + "="*65)
