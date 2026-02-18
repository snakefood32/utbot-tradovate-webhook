import os
import logging
import requests
import time
import pandas as pd
import numpy as np
from flask import Flask, jsonify
from threading import Thread

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config from environment variables
TRADOVATE_USERNAME = os.environ.get('TRADOVATE_USERNAME')
TRADOVATE_PASSWORD = os.environ.get('TRADOVATE_PASSWORD')
TRADOVATE_DEVICE_ID = os.environ.get('TRADOVATE_DEVICE_ID', 'utbot-autonomous-001')
TRADOVATE_APP_ID    = os.environ.get('TRADOVATE_APP_ID', 'tradovate')
TRADOVATE_APP_VERSION = os.environ.get('TRADOVATE_APP_VERSION', '1.0')
SYMBOL              = os.environ.get('SYMBOL', 'MNQH5')
CONTRACT_SIZE       = int(os.environ.get('CONTRACT_SIZE', 1))

# UTBot Hyperparameters (Match your v6 Pine Script)
KEY_PASS = 1
ATR_PERIOD = 10

BASE_URL = "https://demo.tradovateapi.com/v1"

def get_token():
    url = f"{BASE_URL}/auth/accesstokenrequest"
    payload = {
        "name": TRADOVATE_USERNAME,
        "password": TRADOVATE_PASSWORD,
        "appId": TRADOVATE_APP_ID,
        "appVersion": TRADOVATE_APP_VERSION,
        "deviceId": TRADOVATE_DEVICE_ID,
        "cid": 0, "sec": ""
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return r.json().get('accessToken')
    except Exception as e:
        logger.error(f"Auth failed: {e}")
        return None

def get_ohlcv(token):
    # Fetch recent candles for MNQ
    url = f"{BASE_URL}/md/getchart"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "symbol": SYMBOL,
        "chartDescription": {
            "underlyingType": "Tick",
            "elementSize": 1,
            "elementSizeUnit": "Minute",
            "withHistogram": False
        },
        "timeRange": {"closestTimestamp": pd.Timestamp.now().isoformat(), "asMuchAs": 100}
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        r.raise_for_status()
        bars = r.json().get('bars', [])
        df = pd.DataFrame(bars)
        # Standard columns: open, high, low, close
        return df
    except Exception as e:
        logger.error(f"Failed to fetch data: {e}")
        return None

def calculate_utbot(df):
    # Simplified UTBot Logic (Trailing Stop based on ATR)
    # xATR = atr(10)
    # nLoss = 1 * xATR
    # src = close
    # xATRTrailingStop = 0.0
    # xATRTrailingStop := src > nz(xATRTrailingStop[1], 0) and src[1] > nz(xATRTrailingStop[1], 0) ? max(nz(xATRTrailingStop[1], 0), src - nLoss) : 
    #                   src < nz(xATRTrailingStop[1], 0) and src[1] < nz(xATRTrailingStop[1], 0) ? min(nz(xATRTrailingStop[1], 0), src + nLoss) : 
    #                   src > nz(xATRTrailingStop[1], 0) ? src - nLoss : src + nLoss
    
    df['hl2'] = (df['high'] + df['low']) / 2
    # Simple ATR
    df['tr'] = np.maximum(df['high'] - df['low'], 
                          np.maximum(abs(df['high'] - df['close'].shift(1)), 
                                     abs(df['low'] - df['close'].shift(1))))
    df['atr'] = df['tr'].rolling(ATR_PERIOD).mean()
    
    nLoss = KEY_PASS * df['atr']
    src = df['close']
    trailing_stop = np.zeros(len(df))
    
    for i in range(1, len(df)):
        prev_stop = trailing_stop[i-1]
        curr_src = src[i]
        prev_src = src[i-1]
        
        if curr_src > prev_stop and prev_src > prev_stop:
            trailing_stop[i] = max(prev_stop, curr_src - nLoss[i])
        elif curr_src < prev_stop and prev_src < prev_stop:
            trailing_stop[i] = min(prev_stop, curr_src + nLoss[i])
        elif curr_src > prev_stop:
            trailing_stop[i] = curr_src - nLoss[i]
        else:
            trailing_stop[i] = curr_src + nLoss[i]
            
    df['stop'] = trailing_stop
    df['long'] = src > df['stop']
    df['short'] = src < df['stop']
    
    # Signal: Change in direction
    df['buy_signal'] = df['long'] & (~df['long'].shift(1).fillna(False))
    df['sell_signal'] = df['short'] & (~df['short'].shift(1).fillna(False))
    
    return df.iloc[-1]

def autonomous_loop():
    logger.info("Autonomous UTBot loop started.")
    last_signal = None
    
    while True:
        try:
            token = get_token()
            if not token:
                time.sleep(60); continue
                
            df = get_ohlcv(token)
            if df is None or len(df) < ATR_PERIOD:
                time.sleep(30); continue
                
            latest = calculate_utbot(df)
            
            # Execute trades
            if latest['buy_signal'] and last_signal != 'buy':
                logger.info("UTBot BUY Signal detected!")
                # Logic to liquidate shorts and buy
                last_signal = 'buy'
            elif latest['sell_signal'] and last_signal != 'sell':
                logger.info("UTBot SELL Signal detected!")
                # Logic to liquidate longs and sell
                last_signal = 'sell'
                
        except Exception as e:
            logger.error(f"Loop error: {e}")
        
        time.sleep(10) # Poll every 10 seconds

@app.route('/health')
def health(): return "OK", 200

if __name__ == '__main__':
    # Start bot thread
    Thread(target=autonomous_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
