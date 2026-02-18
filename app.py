import os
import logging
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config from environment variables
TRADOVATE_USERNAME = os.environ.get('TRADOVATE_USERNAME')
TRADOVATE_PASSWORD = os.environ.get('TRADOVATE_PASSWORD')
TRADOVATE_DEVICE_ID = os.environ.get('TRADOVATE_DEVICE_ID', 'utbot-device-001')
TRADOVATE_APP_ID    = os.environ.get('TRADOVATE_APP_ID', 'tradovate')
TRADOVATE_APP_VERSION = os.environ.get('TRADOVATE_APP_VERSION', '1.0')
WEBHOOK_SECRET      = os.environ.get('WEBHOOK_SECRET', 'utbot_tradovate_secret_2026')
SYMBOL              = os.environ.get('SYMBOL', 'MNQH5')
CONTRACT_SIZE       = int(os.environ.get('CONTRACT_SIZE', 1))

BASE_URL = "https://demo.tradovateapi.com/v1"

def get_token():
    logger.info("Requesting access token...")
    url = f"{BASE_URL}/auth/accesstokenrequest"
    payload = {
        "name": TRADOVATE_USERNAME,
        "password": TRADOVATE_PASSWORD,
        "appId": TRADOVATE_APP_ID,
        "appVersion": TRADOVATE_APP_VERSION,
        "deviceId": TRADOVATE_DEVICE_ID,
        "cid": 0,
        "sec": ""
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        logger.info("Successfully obtained token.")
        return data.get('accessToken')
    except Exception as e:
        logger.error(f"Failed to get token: {e}")
        return None

def get_account_id(token):
    url = f"{BASE_URL}/account/list"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        accounts = r.json()
        # Usually first account in list for Apex/Tradovate
        if accounts:
            return accounts[0].get('id')
    except Exception as e:
        logger.error(f"Failed to get account ID: {e}")
    return None

def place_order(token, account_id, action):
    url = f"{BASE_URL}/order/placeorder"
    headers = {"Authorization": f"Bearer {token}"}
    
    # Map TradingView actions to Tradovate actions
    # strategy.order.action is usually 'buy' or 'sell'
    tv_action = action.lower()
    tradovate_action = "Buy" if "buy" in tv_action else "Sell"
    
    payload = {
        "account": account_id,
        "symbol": SYMBOL,
        "action": tradovate_action,
        "orderStrategyTypeId": 0,
        "orderQty": CONTRACT_SIZE,
        "orderType": "Market",
        "isCheckOnly": False
    }
    
    logger.info(f"Placing {tradovate_action} order for {SYMBOL}...")
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        r.raise_for_status()
        logger.info(f"Order successful: {r.json()}")
        return True
    except Exception as e:
        logger.error(f"Order failed: {e}")
        return False

def liquidate_all(token, account_id):
    url = f"{BASE_URL}/order/liquidate"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"accountId": account_id, "symbol": SYMBOL}
    logger.info(f"Liquidating position for {SYMBOL}...")
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        r.raise_for_status()
        logger.info("Liquidate successful.")
        return True
    except Exception as e:
        logger.error(f"Liquidate failed: {e}")
        return False

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    logger.info(f"Received webhook: {data}")
    
    # 1. Secret check
    if data.get('secret') != WEBHOOK_SECRET:
        logger.warning("Invalid secret received.")
        return jsonify({"status": "error", "message": "unauthorized"}), 401
    
    # 2. Authenticate
    token = get_token()
    if not token:
        return jsonify({"status": "error", "message": "auth_failed"}), 500
    
    # 3. Get Account
    account_id = get_account_id(token)
    if not account_id:
        return jsonify({"status": "error", "message": "account_not_found"}), 500
    
    # 4. Handle Action
    action = data.get('action', '').lower()
    
    if action in ['buy', 'sell']:
        success = place_order(token, account_id, action)
    elif action == 'exit':
        success = liquidate_all(token, account_id)
    else:
        logger.warning(f"Unknown action: {action}")
        return jsonify({"status": "error", "message": "unknown_action"}), 400
        
    if success:
        return jsonify({"status": "success"}), 200
    else:
        return jsonify({"status": "error", "message": "execution_failed"}), 500

@app.route('/health', methods=['GET'])
def health():
    return "OK", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
