import os
import logging
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Config from environment variables ---
TV_USERNAME     = os.environ.get('TV_USERNAME', 'APEX_493686')
TV_PASSWORD     = os.environ.get('TV_PASSWORD', '')
TV_ACCOUNT_SPEC = os.environ.get('TV_ACCOUNT_SPEC', 'APEX4936860000001')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'UTBot2026')
BASE_URL        = os.environ.get('TV_BASE_URL', 'https://demo.tradovateapi.com/v1')

_access_token = None

def get_access_token():
    global _access_token
    try:
        resp = requests.post(
            f'{BASE_URL}/auth/accesstokenrequest',
            json={
                'name': TV_USERNAME,
                'password': TV_PASSWORD,
                'appId': 'Sample App',
                'appVersion': '1.0',
                'cid': 0,
                'sec': ''
            },
            timeout=10
        )
        data = resp.json()
        logger.info(f'Auth response: {data}')
        token = data.get('accessToken')
        if token:
            _access_token = token
            return token
        logger.error(f'Auth failed: {data}')
        return None
    except Exception as e:
        logger.error(f'Auth exception: {e}')
        return None

def get_account_id():
    token = get_access_token()
    if not token:
        return None, None
    try:
        resp = requests.get(
            f'{BASE_URL}/account/list',
            headers={'Authorization': f'Bearer {token}'},
            timeout=10
        )
        accounts = resp.json()
        logger.info(f'Accounts: {accounts}')
        for acct in accounts:
            if acct.get('name') == TV_ACCOUNT_SPEC:
                return token, acct.get('id')
        # fallback: return first account
        if accounts:
            return token, accounts[0].get('id')
        return token, None
    except Exception as e:
        logger.error(f'Account lookup exception: {e}')
        return token, None

def place_order(action, symbol, qty=1):
    token, account_id = get_account_id()
    if not token or not account_id:
        return {'error': 'Auth or account lookup failed'}
    tv_action = 'Buy' if action.lower() in ['buy', 'long'] else 'Sell'
    payload = {
        'accountSpec': TV_ACCOUNT_SPEC,
        'accountId': account_id,
        'action': tv_action,
        'symbol': symbol,
        'orderQty': qty,
        'orderType': 'Market',
        'isAutomated': True
    }
    logger.info(f'Placing order: {payload}')
    try:
        resp = requests.post(
            f'{BASE_URL}/order/placeorder',
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json'
            },
            json=payload,
            timeout=10
        )
        result = resp.json()
        logger.info(f'Order result: {result}')
        return result
    except Exception as e:
        logger.error(f'Order exception: {e}')
        return {'error': str(e)}

def close_position(symbol):
    token, account_id = get_account_id()
    if not token or not account_id:
        return {'error': 'Auth or account lookup failed'}
    payload = {
        'accountId': account_id,
        'symbol': symbol,
        'isAutomated': True
    }
    logger.info(f'Closing position: {payload}')
    try:
        resp = requests.post(
            f'{BASE_URL}/order/liquidateposition',
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json'
            },
            json=payload,
            timeout=10
        )
        result = resp.json()
        logger.info(f'Close result: {result}')
        return result
    except Exception as e:
        logger.error(f'Close exception: {e}')
        return {'error': str(e)}

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400

    # Security check
    if data.get('secret') != WEBHOOK_SECRET:
        logger.warning(f'Unauthorized webhook attempt')
        return jsonify({'error': 'Unauthorized'}), 401

    action = data.get('action', '').lower()
    symbol = data.get('symbol', '').upper()
    qty    = int(data.get('qty', 1))

    logger.info(f'Webhook: action={action} symbol={symbol} qty={qty}')

    if not symbol:
        return jsonify({'error': 'symbol required'}), 400

    if action in ['buy', 'long', 'sell', 'short']:
        result = place_order(action, symbol, qty)
    elif action in ['exit', 'exitlong', 'exitshort', 'close', 'flat']:
        result = close_position(symbol)
    else:
        return jsonify({'error': f'Unknown action: {action}'}), 400

    return jsonify(result), 200

@app.route('/', methods=['GET'])
def health():
    return 'UTBot Tradovate Webhook Server - Running', 200

@app.route('/test-auth', methods=['GET'])
def test_auth():
    token = get_access_token()
    if token:
        return jsonify({'status': 'ok', 'token_preview': token[:20] + '...'}), 200
    return jsonify({'status': 'failed', 'error': 'Could not authenticate'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
