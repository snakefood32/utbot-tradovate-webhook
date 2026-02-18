import os
import logging
import requests
import time
from flask import Flask, jsonify, request
from threading import Thread

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# Config
USERNAME   = os.environ.get('TRADOVATE_USERNAME', '')
PASSWORD   = os.environ.get('TRADOVATE_PASSWORD', '')
DEVICE_ID  = os.environ.get('TRADOVATE_DEVICE_ID', 'utbot-001')
APP_ID     = os.environ.get('TRADOVATE_APP_ID', 'tradovate')
APP_VER    = os.environ.get('TRADOVATE_APP_VERSION', '1.0')
SYMBOL     = os.environ.get('SYMBOL', 'MNQH5')
QTY        = int(os.environ.get('CONTRACT_SIZE', '1'))
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'utbot_tradovate_secret_2026')
BASE       = 'https://demo.tradovateapi.com/v1'

_token     = None
_token_ts  = 0
TOKEN_TTL  = 18 * 60
_p_ticket  = None
_acct_id   = None

def get_token():
    global _token, _token_ts, _p_ticket
    now = time.time()
    if _token and now - _token_ts < TOKEN_TTL:
        return _token
    
    logger.info(f'Authenticating with Tradovate (User: {USERNAME[:4]}...)')
    payload = {
        'name': USERNAME,
        'password': PASSWORD,
        'appId': APP_ID,
        'appVersion': APP_VER,
        'deviceId': DEVICE_ID,
        'cid': 0,
        'sec': ''
    }
    
    try:
        r = requests.post(f'{BASE}/auth/accesstokenrequest', json=payload, timeout=15)
        logger.info(f'Auth response [{r.status_code}]: {r.text[:300]}')
        
        if r.status_code != 200:
            raise RuntimeError(f'Auth failed HTTP {r.status_code}: {r.text}')
            
        data = r.json()
        
        # Check for error in JSON even if HTTP 200
        if 'errorText' in data:
            err = data['errorText']
            logger.error(f'Tradovate Error: {err}')
            raise RuntimeError(f'Tradovate Auth Error: {err}')
            
        if 'p-ticket' in data:
            _p_ticket = data['p-ticket']
            logger.info(f'Device verification required. p-ticket={_p_ticket}')
            raise RuntimeError('Device verification required. Check email for code.')
            
        _p_ticket = None
        _token = data.get('accessToken') or data.get('token')
        
        if not _token:
            raise RuntimeError(f'No token in response: {data}')
            
        _token_ts = now
        logger.info('Authentication successful.')
        return _token
        
    except Exception as e:
        logger.error(f'get_token error: {e}')
        raise

def verify_device(code):
    global _token, _token_ts, _p_ticket
    if not _p_ticket:
        return False, 'No pending verification'
    
    payload = {
        'name': USERNAME,
        'password': PASSWORD,
        'appId': APP_ID,
        'appVersion': APP_VER,
        'deviceId': DEVICE_ID,
        'cid': 0,
        'sec': '',
        'p-ticket': _p_ticket,
        'p-code': code,
        'p-captcha': True
    }
    
    r = requests.post(f'{BASE}/auth/accesstokenrequest', json=payload, timeout=15)
    logger.info(f'Verify response [{r.status_code}]: {r.text[:300]}')
    
    if r.status_code != 200:
        return False, f'Verify failed: {r.text}'
        
    data = r.json()
    if 'errorText' in data:
        return False, data['errorText']
        
    tok = data.get('accessToken') or data.get('token')
    if not tok:
        return False, f'No token in response: {data}'
        
    _p_ticket = None
    _token = tok
    _token_ts = time.time()
    logger.info('Device verified. Token obtained.')
    return True, 'Verified OK'

def hdrs():
    tok = get_token()
    return {'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json'}

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
    logger.info(f'Account id={_acct_id}')
    return _acct_id

def place_order(side):
    try:
        acct = get_account()
        action = 'Buy' if side == 'buy' else 'Sell'
        r = requests.post(
            f'{BASE}/order/placeorder',
            headers=hdrs(),
            json={
                'accountId': acct,
                'action': action,
                'symbol': SYMBOL,
                'orderQty': QTY,
                'orderType': 'Market',
                'isAutomated': True,
            },
            timeout=15
        )
        logger.info(f'Order [{r.status_code}]: {r.text}')
        return r.status_code == 200
    except Exception as e:
        logger.error(f'Order placement failed: {e}')
        return False

def liquidate():
    try:
        acct = get_account()
        r = requests.post(
            f'{BASE}/order/liquidateposition',
            headers=hdrs(),
            json={
                'accountId': acct,
                'symbol': SYMBOL,
                'isAutomated': True
            },
            timeout=15
        )
        logger.info(f'Liquidate [{r.status_code}]: {r.text}')
    except Exception as e:
        logger.error(f'Liquidation failed: {e}')

# State tracker
last_signal = None

@app.route('/')
def index():
    needs_verify = _p_ticket is not None
    return jsonify({
        'service': 'UTBot Webhook Engine',
        'symbol': SYMBOL,
        'authenticated': _token is not None,
        'needs_device_verification': needs_verify,
        'verify_instructions': 'Check your Tradovate email for a code, then visit /verify/CODE' if needs_verify else None,
        'last_signal': last_signal,
    }), 200

@app.route('/webhook', methods=['POST'])
def webhook():
    global last_signal
    data = request.get_json(force=True, silent=True) or {}
    logger.info(f'Webhook received: {data}')

    secret = data.get('secret', '')
    if secret != WEBHOOK_SECRET:
        logger.warning('Webhook secret mismatch')
        return jsonify({'error': 'Unauthorized'}), 401

    action = str(data.get('action', '')).lower().strip()
    if action not in ('buy', 'sell', 'close', 'liquidate'):
        return jsonify({'error': f'Unknown action: {action}'}), 400

    try:
        if action in ('close', 'liquidate'):
            liquidate()
            last_signal = 'closed'
            return jsonify({'status': 'liquidated'}), 200

        if action == 'buy':
            if last_signal == 'sell':
                liquidate()
                time.sleep(1)
            if place_order('buy'):
                last_signal = 'buy'
                return jsonify({'status': 'buy order placed'}), 200
            else:
                return jsonify({'error': 'Order placement failed'}), 500

        if action == 'sell':
            if last_signal == 'buy':
                liquidate()
                time.sleep(1)
            if place_order('sell'):
                last_signal = 'sell'
                return jsonify({'status': 'sell order placed'}), 200
            else:
                return jsonify({'error': 'Order placement failed'}), 500

    except Exception as e:
        logger.error(f'Webhook processing error: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/verify/<code>')
def verify(code):
    ok, msg = verify_device(code)
    return jsonify({'success': ok, 'message': msg}), 200 if ok else 400

@app.route('/health')
def health():
    return jsonify({'ok': True, 'token': _token is not None, 'last_signal': last_signal}), 200

@app.route('/status')
def status():
    needs_verify = _p_ticket is not None
    return jsonify({
        'authenticated': _token is not None,
        'needs_device_verification': needs_verify,
        'last_signal': last_signal,
        'symbol': SYMBOL,
        'qty': QTY,
        'base_url': BASE,
        'username_prefix': USERNAME[:4] if USERNAME else 'N/A'
    }), 200

def init_auth():
    time.sleep(5)
    try:
        get_token()
        get_account()
        logger.info('Startup auth complete.')
    except Exception as e:
        logger.warning(f'Startup auth background task: {e}')

_auth_thread = Thread(target=init_auth, daemon=True, name='init_auth')
_auth_thread.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
