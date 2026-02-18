import os
import logging
import requests
import time
import math
from flask import Flask, jsonify
from threading import Thread

# ── Flask app ──────────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────
USERNAME     = os.environ.get('TRADOVATE_USERNAME', '')
PASSWORD     = os.environ.get('TRADOVATE_PASSWORD', '')
DEVICE_ID    = os.environ.get('TRADOVATE_DEVICE_ID', 'utbot-device-001')
APP_ID       = os.environ.get('TRADOVATE_APP_ID', 'tradovate')
APP_VER      = os.environ.get('TRADOVATE_APP_VERSION', '1.0')
SYMBOL       = os.environ.get('SYMBOL', 'MNQH5')
QTY          = int(os.environ.get('CONTRACT_SIZE', '1'))
POLL_SEC     = int(os.environ.get('POLL_SECONDS', '60'))   # poll every 60 s
KEY_VALUE    = float(os.environ.get('KEY_VALUE', '1'))     # UTBot multiplier
ATR_LEN      = int(os.environ.get('ATR_LEN', '10'))        # UTBot ATR period
BASE         = 'https://demo.tradovateapi.com/v1'

# ── Token cache ────────────────────────────────────────────
_token       = None
_token_ts    = 0
TOKEN_TTL    = 18 * 60   # refresh every 18 min

def get_token():
    global _token, _token_ts
    now = time.time()
    if _token and now - _token_ts < TOKEN_TTL:
        return _token
    logger.info('Requesting Tradovate token...')
    r = requests.post(f'{BASE}/auth/accesstokenrequest', json={
        'name': USERNAME, 'password': PASSWORD,
        'appId': APP_ID, 'appVersion': APP_VER,
        'deviceId': DEVICE_ID, 'cid': 0, 'sec': ''
    }, timeout=15)
    r.raise_for_status()
    data = r.json()
    if 'p-ticket' in data:
        logger.warning('MFA required – cannot auto-auth')
        return None
    _token    = data['accessToken']
    _token_ts = now
    logger.info('Token obtained.')
    return _token

def hdrs():
    return {'Authorization': f'Bearer {get_token()}'}

# ── Account lookup ─────────────────────────────────────────
_acct_id = None

def get_account():
    global _acct_id
    if _acct_id:
        return _acct_id
    r = requests.get(f'{BASE}/account/list', headers=hdrs(), timeout=10)
    r.raise_for_status()
    accounts = r.json()
    if not accounts:
        raise RuntimeError('No accounts found')
    _acct_id = accounts[0]['id']
    logger.info(f'Using account id={_acct_id}')
    return _acct_id

# ── Market data ────────────────────────────────────────────
def fetch_bars(n=50):
    """
    Uses /md/getChart (REST, returns historical bars).
    Returns list of dicts with open/high/low/close.
    """
    r = requests.get(
        f'{BASE}/md/getChart',
        headers=hdrs(),
        params={
            'symbol': SYMBOL,
            'chartDescription.underlyingType': 'Minute',
            'chartDescription.elementSize': 1,
            'chartDescription.elementSizeUnit': 'Minute',
            'timeRange.asMuchAsElements': n,
        },
        timeout=15
    )
    r.raise_for_status()
    payload = r.json()
    # Response shape: {"s":"ok","d":{"bars":[{"timestamp":...,"open":...,"high":...,"low":...,"close":...}]}}
    bars = payload.get('d', {}).get('bars', [])
    if not bars:
        bars = payload.get('bars', [])
    return bars

# ── UTBot logic (pure Python, no pandas/numpy) ─────────────
def compute_atr(bars, period):
    trs = []
    for i in range(1, len(bars)):
        h = bars[i]['high']
        l = bars[i]['low']
        pc = bars[i-1]['close']
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    # RMA (Wilder's) approximation using EMA seed
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    alpha = 1.0 / period
    for tr in trs[period:]:
        atr = alpha * tr + (1 - alpha) * atr
    return atr

def compute_utbot_signal(bars):
    """
    Returns 'buy', 'sell', or None based on the last two candles.
    Matches the Pine Script UTBot trailing stop logic.
    """
    if len(bars) < ATR_LEN + 2:
        return None

    # Compute trailing stop for each bar from scratch (small dataset)
    atr_values = []
    # Calculate per-bar Wilder ATR
    trs = [max(
        bars[i]['high'] - bars[i]['low'],
        abs(bars[i]['high'] - bars[i-1]['close']),
        abs(bars[i]['low']  - bars[i-1]['close'])
    ) for i in range(1, len(bars))]
    trs = [trs[0]] + trs  # prepend first so index aligns with bars

    # Wilder's smoothing
    atr = sum(trs[1:ATR_LEN+1]) / ATR_LEN
    atr_list = [None] * (ATR_LEN)
    atr_list.append(atr)
    alpha = 1.0 / ATR_LEN
    for i in range(ATR_LEN + 1, len(bars)):
        atr = alpha * trs[i] + (1 - alpha) * atr
        atr_list.append(atr)

    # Compute trailing stop
    stop = [0.0] * len(bars)
    for i in range(ATR_LEN + 1, len(bars)):
        src      = bars[i]['close']
        src_prev = bars[i-1]['close']
        n_loss   = KEY_VALUE * atr_list[i]
        prev_stop = stop[i-1]

        if src > prev_stop and src_prev > prev_stop:
            stop[i] = max(prev_stop, src - n_loss)
        elif src < prev_stop and src_prev < prev_stop:
            stop[i] = min(prev_stop, src + n_loss)
        elif src > prev_stop:
            stop[i] = src - n_loss
        else:
            stop[i] = src + n_loss

    # Signal on last complete bar vs second-to-last
    i   = len(bars) - 1
    i_1 = len(bars) - 2

    above_now  = bars[i]['close']   > stop[i]
    above_prev = bars[i_1]['close'] > stop[i_1]

    buy_signal  = above_now  and not above_prev
    sell_signal = not above_now and above_prev

    if buy_signal:
        return 'buy'
    if sell_signal:
        return 'sell'
    return None

# ── Order execution ────────────────────────────────────────
def place_order(action):
    acct = get_account()
    side = 'Buy' if action == 'buy' else 'Sell'
    logger.info(f'Placing {side} market order for {SYMBOL} x{QTY}')
    r = requests.post(f'{BASE}/order/placeorder', headers=hdrs(), json={
        'accountSpec':  USERNAME,
        'accountId':    acct,
        'action':       side,
        'symbol':       SYMBOL,
        'orderQty':     QTY,
        'orderType':    'Market',
        'isAutomated':  True,
    }, timeout=15)
    logger.info(f'Order response [{r.status_code}]: {r.text}')
    return r.status_code == 200

def liquidate():
    acct = get_account()
    logger.info(f'Liquidating {SYMBOL}')
    r = requests.post(f'{BASE}/order/liquidateposition', headers=hdrs(), json={
        'accountId': acct, 'symbol': SYMBOL, 'isAutomated': True
    }, timeout=15)
    logger.info(f'Liquidate response [{r.status_code}]: {r.text}')

# ── Autonomous trading loop ────────────────────────────────
def trading_loop():
    logger.info('=== UTBot autonomous trading loop started ===')
    last_signal = None

    while True:
        try:
            bars = fetch_bars(n=ATR_LEN * 3)
            if not bars:
                logger.warning('No bars returned – skipping')
            else:
                signal = compute_utbot_signal(bars)
                last_close = bars[-1]['close'] if bars else 0
                logger.info(f'Poll | close={last_close} | signal={signal} | last={last_signal}')

                if signal == 'buy' and last_signal != 'buy':
                    logger.info('>>> BUY SIGNAL – executing long entry')
                    if last_signal == 'sell':
                        liquidate()
                        time.sleep(1)
                    place_order('buy')
                    last_signal = 'buy'

                elif signal == 'sell' and last_signal != 'sell':
                    logger.info('>>> SELL SIGNAL – executing short entry')
                    if last_signal == 'buy':
                        liquidate()
                        time.sleep(1)
                    place_order('sell')
                    last_signal = 'sell'

        except Exception as e:
            logger.error(f'Loop error: {e}')

        time.sleep(POLL_SEC)

# Start background thread at module level (works with gunicorn)
_bot_thread = Thread(target=trading_loop, daemon=True, name='utbot-loop')
_bot_thread.start()
logger.info('UTBot background thread started.')

# ── Flask routes ───────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'symbol': SYMBOL, 'thread_alive': _bot_thread.is_alive()}), 200

@app.route('/')
def index():
    return jsonify({'service': 'UTBot Tradovate Autonomous Engine', 'symbol': SYMBOL}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
