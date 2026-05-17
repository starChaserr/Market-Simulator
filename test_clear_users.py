import urllib.request
import json
import time

URL = "http://127.0.0.1:8000/api"

def call_api(path, method="GET", data=None):
    req = urllib.request.Request(f"{URL}{path}", method=method)
    if data:
        req.add_header('Content-Type', 'application/json')
        body = json.dumps(data).encode('utf-8')
    else: body = None
    try:
        with urllib.request.urlopen(req, data=body) as f: 
            return json.loads(f.read().decode('utf-8'))
    except Exception as e:
        print(f"Error calling {path}: {e}")
        return None

def test_clear_users():
    print("Testing clear API users...")
    
    # 1. Create a user
    print("Creating user test-user...")
    call_api("/accounts", "POST", {"user": "test-user", "starting_cash": 1000000})
    
    # 2. Verify user exists
    users = call_api("/users")
    print(f"Users after creation: {users}")
    if not users or not any(u['owner'] == 'test-user' for u in users.get('users', [])):
        print("Failed: User not created")
        return

    # 3. Submit an order
    print("Submitting order for test-user...")
    order = call_api("/order", "POST", {"side": "buy", "quantity": 10, "order_type": "limit", "price": 90, "user": "test-user"})
    print(f"Order: {order}")

    # 4. Clear users
    print("Clearing API users...")
    result = call_api("/users", "DELETE")
    print(f"Clear result: {result}")

    # 5. Verify users are gone
    users = call_api("/users")
    print(f"Users after clear: {users}")
    if users and any(u['owner'] == 'test-user' for u in users.get('users', [])):
        print("Failed: User still exists")
        return

    # 6. Verify orders are gone
    orders = call_api("/orders?include_internal=true")
    print(f"All orders: {orders}")
    if orders and any(o['owner'] == 'test-user' for o in orders.get('orders', [])):
        print("Failed: Order still exists")
        return

    print("Success: API users and their orders cleared.")

if __name__ == "__main__":
    test_clear_users()
