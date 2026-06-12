import os
import sys
import numpy as np
import pandas as pd
import requests
from datetime import datetime

def fetch_nse_option_chain(symbol, mode):
    """Fetches raw structural JSON data from NSE India securely."""
    headers = {
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36',
        'accept-encoding': 'gzip, deflate, br', 
        'accept-language': 'en-US,en;q=0.9'
    }
    session = requests.Session()
    try:
        # Pre-hit main page to populate cookie parameters
        session.get("https://www.nseindia.com", headers=headers, timeout=5)
        url = f"https://www.nseindia.com/api/option-chain/{mode}?symbol={symbol}"
        response = session.get(url, headers=headers, cookies=dict(session.cookies), timeout=10)
        res_json = response.json()
        
        records = res_json.get('records', {})
        expiries = records.get('expiryDates', [])
        spot_price = records.get('underlyingValue')
        
        return records.get('data', []), expiries, spot_price
    except Exception as e:
        print(f"❌ Core API Sync Failure for {symbol}: {e}")
        return None, None, None

def process_singularity_matrix(symbol, mode):
    """Processes the option chain array to detect shifts and structural matrix boundaries."""
    all_market_data, expiry_list, spot_price = fetch_nse_option_chain(symbol, mode)
    
    if not tragedy_guard(all_market_data, expiry_list, spot_price):
        return None

    selected_expiry = expiry_list[0]
    filtered_list = [d for d in all_market_data if d.get('expiryDate') == selected_expiry]
    
    data_list = []
    total_ce_oi, total_pe_oi = 0, 0
    total_ce_vol, total_pe_vol = 0, 0
    
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
                'Strike': strike, 'CE_Vol': ce_vol, 'CE_OI': ce_oi, 'CE_LTP': data['CE'].get('lastPrice', 0),
                'PE_Vol': pe_vol, 'PE_OI': pe_oi, 'PE_LTP': data['PE'].get('lastPrice', 0)
            })
            
    df = pd.DataFrame(data_list)
    if df.empty:
        return None

    # Step size configuration
    step = int(df.sort_values(by='Strike')['Strike'].diff().iloc[1]) if len(df) > 1 else 50
    atm_strike = round(spot_price / step) * step
    zone_df = df[(df['Strike'] >= atm_strike - (step*5)) & (df['Strike'] <= atm_strike + (step*5))].copy()

    # Hybrid Support / Resistance and Volume Profile detection 
    ce_vol_max = zone_df.sort_values(by='CE_Vol', ascending=False).iloc[0]
    ce_oi_max = zone_df.sort_values(by='CE_OI', ascending=False).iloc[0]
    pe_vol_max = zone_df.sort_values(by='PE_Vol', ascending=False).iloc[0]
    pe_oi_max = zone_df.sort_values(by='PE_OI', ascending=False).iloc[0]

    res_strike = ce_vol_max['Strike']
    res_type = "Vol"
    max_val_ce = ce_vol_max['CE_Vol']
    if ce_oi_max['CE_OI'] > max_val_ce:
        res_strike = ce_oi_max['Strike']
        res_type = "OI"
        max_val_ce = ce_oi_max['CE_OI']

    sup_strike = pe_vol_max['Strike']
    sup_type = "Vol"
    max_val_pe = pe_vol_max['PE_Vol']
    if pe_oi_max['PE_OI'] > max_val_pe:
        sup_strike = pe_oi_max['Strike']
        sup_type = "OI"
        max_val_pe = pe_oi_max['PE_OI']

    # Shift status calculation mechanics
    ce_2nd = zone_df[zone_df['Strike'] != res_strike].sort_values(by='CE_Vol' if res_type == "Vol" else 'CE_OI', ascending=False)
    pe_2nd = zone_df[zone_df['Strike'] != sup_strike].sort_values(by='PE_Vol' if sup_type == "Vol" else 'PE_OI', ascending=False)

    ce_weakness = (ce_2nd.iloc[0]['CE_Vol' if res_type == "Vol" else 'CE_OI'] / max_val_ce) * 100 if max_val_ce > 0 else 0
    pe_weakness = (pe_2nd.iloc[0]['PE_Vol' if sup_type == "Vol" else 'PE_OI'] / max_val_pe) * 100 if max_val_pe > 0 else 0

    res_status = "STRONG" if ce_weakness <= 75 else (f"WTT {ce_weakness:.0f}%" if ce_2nd.iloc[0]['Strike'] > res_strike else f"WTB {ce_weakness:.0f}%")
    sup_status = "STRONG" if pe_weakness <= 75 else (f"WTT {pe_weakness:.0f}%" if pe_2nd.iloc[0]['Strike'] > sup_strike else f"WTB {pe_weakness:.0f}%")

    EOR = res_strike + zone_df[zone_df['Strike'] == res_strike]['CE_LTP'].values[0] if not zone_df[zone_df['Strike'] == res_strike].empty else res_strike
    EOS = sup_strike - zone_df[zone_df['Strike'] == sup_strike]['PE_LTP'].values[0] if not zone_df[zone_df['Strike'] == sup_strike].empty else sup_strike

    pcr_oi = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 1.0
    pcr_vol = total_pe_vol / total_ce_vol if total_ce_vol > 0 else 1.0

    return {
        "Asset": symbol, "Spot": spot_price, "Expiry": selected_expiry,
        "PCR_Vol": pcr_vol, "PCR_OI": pcr_oi,
        "EOR": EOR, "EOR_Type": res_type, "EOR_Status": res_status,
        "EOS": EOS, "EOS_Type": sup_type, "EOS_Status": sup_status
    }

def tragedy_guard(data, expiries, spot):
    return data and expiries and isinstance(spot, (int, float))

def send_matrix_telegram_alert(metrics):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id or not metrics:
        return

    signal = "🟢 VOL BREAKOUT SUPPORTED" if metrics['PCR_Vol'] > 1.0 else "🔴 LIQUIDITY RESISTANCE BLOCKED"
    
    # Pre-formatted structural block optimized for clear mobile layouts
    msg = (
        f"👑 <b>LTP CALCULATOR PRO — ALPHA GRID</b>\n"
        f"<i>Asset Alpha Matrix Dashboard Context</i>\n"
        f"──────────────────────────────────\n"
        f"<b>Instrument:</b> {metrics['Asset']} ({metrics['Expiry']})\n"
        f"<b>Spot Price:</b> ₹{metrics['Spot']:.2f}\n"
        f"──────────────────────────────────\n"
        f"📊 <b>PCR Vol:</b> {metrics['PCR_Vol']:.2f} | <b>PCR OI:</b> {metrics['PCR_OI']:.2f}\n"
        f"🛑 <b>EoR ({metrics['EOR_Type']}):</b> {metrics['EOR']:.2f}\n"
        f"   └ Status: <code>{metrics['EOR_Status']}</code>\n"
        f"🟢 <b>EoS ({metrics['EOS_Type']}):</b> {metrics['EOS']:.2f}\n"
        f"   └ Status: <code>{metrics['EOS_Status']}</code>\n"
        f"──────────────────────────────────\n"
        f"💡 <b>Signal:</b> <code>{signal}</code>"
    )

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Network error routing alert: {e}")

if __name__ == "__main__":
    raw_input = os.environ.get("TELEGRAM_INPUT_TICKER", "").strip().upper()
    if not raw_input and len(sys.argv) > 1:
        raw_input = sys.argv[1].strip().upper()

    # Default fallback to NIFTY index processing if string parameter is blank
    target_asset = raw_input if raw_input else "NIFTY"
    api_mode = "indices" if target_asset in ["NIFTY", "BANKNIFTY", "FINNIFTY"] else "equities"

    matrix_metrics = process_singularity_matrix(target_asset, api_mode)
    if matrix_metrics:
        send_matrix_telegram_alert(matrix_metrics)
    else:
        print("⚠️ Matrix processing halted. Target instrument lacks option structures.")
