import os
import logging
import requests
import time
from flask import Flask, jsonify
from threading import Thread

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# Config
USERNAME  = os.environ.get('TRADOVATE_USERNAME', '')
PASSWORD  = os.environ.get('TRADOVATE_PASSWORD', '')
DEVICE_ID = os.environ.get('TRADOVATE_DEVICE_ID', 'utbot-001')
APP_ID    = os.environ.get('TRADOVATE_APP_ID', 'tradovate')
APP_VER   = os.environ.get('TRADOVATE_APP_VERSION', '1.0')
SYMBOL    = os.environ.get('SYMBOL', 'MNQH5')
QTY       = int(os.environ.get('CONTRACT_SIZE', '1'))
POLL_SEC  = int(os.environ.get('POLL_SECONDS', '60'))
KEY_VALUE = float(os.environ.get('KEY_VALUE', '1'))
ATR_LEN   = int(os.environ.get('ATR_LEN', '10'))
BASE      = 'https://demo.tradovateapi.com/v1'

_token    = None
_token_ts = 0
TOKEN_TTL = 18 * 60

def get_token():
    global _token, _token_ts
    now = time.time()
    if _token and now - _token_ts < TOKEN_TTL:
        return _token
    logger.info('Requesting Tradovate token...')
    try:
        r = requests.post(f'{BASE}/auth/accesstokenrequest', json={
            'name': USERNAME, 'password': PASSWORD,
            'appId': APP_ID, 'appVersion': APP_VER,
            'deviceId': DEVICE_ID, 'cid': 0, 'sec': ''
        }, timeout=15)
        logger.info(f'Auth status: {r.status_code}')
        data = r.json()
        logger.info(f'Auth response keys: {list(data.keys())}')
        # Handle multiple possible token key names
        token = (data.get('accessToken') or
                 data.get('token') or
                 data.get('access_token'))
        if not token:
            logger.error(f'No token in response. Full response: {data}')
            return None
        _token = token
        _token_ts = now
        logger.info('Token obtained successfully.')
        return _token
    except Exception as e:
        logger.error(f'Auth exception: {e}')
        return None

def hdrs():
    t = get_token()
    if not t:
        raise RuntimeError('No auth token')
    return {'Authorization': f'Bearer {t}'}

_acct_id = None

def get_account():
    global _acct_id
    if _acct_id:
        return _acct_id
    r = requests.get(f'{BASE}/account/list', headers=hdrs(), timeout=10)
    logger.info(f'Account list status: {r.status_code} - {r.text[:200]}')
    r.raise_for_status()
    accounts = r.json()
    if not accounts:
        raise RuntimeError('No accounts found')
    _acct_id = accounts[0]['id']
    logger.info(f'Using account id={_acct_id}')
    return _acct_id

def fetch_bars(n=50):
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
    logger.info(f'getChart status: {r.status_code}')
    if r.status_code != 200:
        logger.error(f'getChart error: {r.text[:300]}')
        return []
    payload = r.json()
    bars = payload.get('d', {}).get('bars', [])
    if not bars:
        bars = payload.get('bars', [])
    logger.info(f'Fetched {len(bars)} bars')
    return bars

def compute_utbot_signal(bars):
    if len(bars) < ATR_LEN + 2:
        logger.warning(f'Not enough bars: {len(bars)}')
        return None
    trs = [max(
        bars[i]['high'] - bars[i]['low'],
        abs(bars[i]['high'] - bars[i-1]['close']),
        abs(bars[i]['low']  - bars[i-1]['close'])
    ) for i in range(1, len(bars))]
    trs = [trs[0]] + trs
    atr = sum(trs[1:ATR_LEN+1]) / ATR_LEN
    atr_list = [None] * ATR_LEN
    atr_list.append(atr)
    alpha = 1.0 / ATR_LEN
    for i in range(ATR_LEN + 1, len(bars)):
        atr = alpha * trs[i] + (1 - alpha) * atr
        atr_list.append(atr)
    stop = [0.0] * len(bars)
    for i in range(ATR_LEN + 1, len(bars)):
        src = bars[i]['close']
        src_prev = bars[i-1]['close']
        n_loss = KEY_VALUE * atr_list[i]
        ps = stop[i-1]
        if src > ps and src_prev > ps:
            stop[i] = max(ps, src - n_loss)
        elif src < ps and src_prev < ps:
            stop[i] = min(ps, src + n_loss)
        elif src > ps:
            stop[i] = src - n_loss
        else:
            stop[i] = src + n_loss
    i, i1 = len(bars)-1, len(bars)-2
    above_now  = bars[i]['close']  > stop[i]
    above_prev = bars[i1]['close'] > stop[i1]
    if above_now and not above_prev:
        return 'buy'
    if not above_now and above_prev:
        return 'sell'
    return None

def place_order(action):
    acct = get_account()
    side = 'Buy' if action == 'buy' else 'Sell'
    logger.info(f'Placing {side} market order {SYMBOL} x{QTY}')
    r = requests.post(f'{BASE}/order/placeorder', headers=hdrs(), json={
        'accountSpec': USERNAME, 'accountId': acct,
        'action': side, 'symbol': SYMBOL,
        'orderQty': QTY, 'orderType': 'Market', 'isAutomated': True,
    }, timeout=15)
    logger.info(f'Order [{r.status_code}]: {r.text}')
    return r.status_code == 200

def liquidate():
    acct = get_account()
    logger.info(f'Liquidating {SYMBOL}')
    r = requests.post(f'{BASE}/order/liquidateposition', headers=hdrs(), json={
        'accountId': acct, 'symbol': SYMBOL, 'isAutomated': True
    }, timeout=15)
    logger.info(f'Liquidate [{r.status_code}]: {r.text}')

def trading_loop():
    logger.info('=== UTBot autonomous loop started ===')
    last_signal = None
    while True:
        try:
            bars = fetch_bars(n=ATR_LEN * 3)
            if bars:
                signal = compute_utbot_signal(bars)
                close = bars[-1]['close']
                logger.info(f'close={close} signal={signal} last={last_signal}')
                if signal == 'buy' and last_signal != 'buy':
                    logger.info('>>> BUY SIGNAL')
                    if last_signal == 'sell':
                        liquidate(); time.sleep(1)
                    place_order('buy')
                    last_signal = 'buy'
                elif signal == 'sell' and last_signal != 'sell':
                    logger.info('>>> SELL SIGNAL')
                    if last_signal == 'buy':
                        liquidate(); time.sleep(1)
                    place_order('sell')
                    last_signal = 'sell'
        except Exception as e:
            logger.error(f'Loop error: {e}')
        time.sleep(POLL_SEC)

# Start at module level so gunicorn picks it up
_bot = Thread(target=trading_loop, daemon=True, name='utbot')
_bot.start()
logger.info('UTBot thread started.')

@app.route('/health')
def health():
    return jsonify({'ok': True, 'symbol': SYMBOL, 'thread': _bot.is_alive()}), 200

@app.route('/')
def index():
    return jsonify({'service': 'UTBot Autonomous Engine', 'symbol': SYMBOL}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
