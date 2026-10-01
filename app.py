import os
import datetime
import uuid
import re
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from supabase import create_client, Client
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)
# Restored Secret Key for Sessions
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "fallback-secret-key-change-in-prod")

# Restored Admin Credentials
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

# Initialize Supabase Client
url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(url, key)

ALLOWED_FUNDS = ["Cash in Hand", "AEPS Float", "Airtel Payments Bank", "Main Office Bank", "CSC Wallet"]

# -----------------------------------------------------------
# HELPERS & VALIDATORS
# -----------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def is_valid_uuid(val):
    try:
        uuid.UUID(str(val))
        return True
    except ValueError:
        return False

def safe_date(date_str):
    if not date_str:
        return ''
    try:
        # Validates format is exactly YYYY-MM-DD
        datetime.date.fromisoformat(date_str)
        return date_str
    except ValueError:
        raise ValueError(f"Invalid date format")

# -----------------------------------------------------------
# CORE ROUTES
# -----------------------------------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            return "Invalid credentials", 401
    return render_template('login.html')

# SECURED: Changed to POST and uses session.clear()
@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html')

# -----------------------------------------------------------
# 1. DASHBOARD DATA
# -----------------------------------------------------------
@app.route('/api/data', methods=['GET'])
@login_required
def get_initial_data():
    try:
        client_date = safe_date(request.args.get('client_date', ''))
        if not client_date:
            client_date = datetime.date.today().isoformat()
    except ValueError:
         return jsonify({"error": "Invalid client_date format"}), 400
        
    current_month = client_date[:7]

    try:
        stats_response = supabase.rpc('get_monthly_stats', {'month_prefix': current_month}).execute()
        monthly_stats = stats_response.data
        
        # Guard against RPC returning a list instead of a dict
        if isinstance(monthly_stats, list) and len(monthly_stats) > 0:
            monthly_stats = monthly_stats[0]
        elif not monthly_stats:
            monthly_stats = {"inflow": 0, "outflow": 0, "breakdown": {}}

        today_tx_response = supabase.table('transactions').select('*').eq('date', client_date).order('created_at', desc=True).execute()
        dashboard_transactions = today_tx_response.data

        funds_response = supabase.table('funds').select('*').execute()
        funds = funds_response.data

        return jsonify({
            "transactions": dashboard_transactions,
            "funds": funds,
            "monthly_stats": monthly_stats
        }), 200

    except Exception:
        return jsonify({"error": "Failed to fetch dashboard data"}), 500

# -----------------------------------------------------------
# 2. PAGINATED TRANSACTIONS & SEARCH
# -----------------------------------------------------------
@app.route('/api/transactions', methods=['GET'])
@login_required
def get_transactions():
    try:
        page = max(int(request.args.get('page', 1)), 1)
        limit = min(int(request.args.get('limit', 50)), 100000) # Allow large exports
    except ValueError:
        page, limit = 1, 50

    # STRIP INJECTION VECTORS: remove commas and parentheses from search
    raw_search = request.args.get('search', '').strip()
    search_query = re.sub(r'[,()]', '', raw_search)
    linked_id = request.args.get('linked_id', '').strip()
    
    # FUND WHITELIST CHECK
    raw_fund = request.args.get('fund', '').strip()
    fund = raw_fund if raw_fund in ALLOWED_FUNDS else ''
    
    category = request.args.get('category', '').strip()

    try:
        start_date = safe_date(request.args.get('start_date', ''))
        end_date = safe_date(request.args.get('end_date', ''))
    except ValueError:
        return jsonify({"error": "Invalid filter date format"}), 400

    try:
        # OPTIMIZED: Removed count='exact' to save DB execution time
        query = supabase.table('transactions').select('*')

        if linked_id:
            query = query.eq('linked_id', linked_id)
        elif search_query:
            if is_valid_uuid(search_query):
                query = query.eq('id', search_query)
            else:
                query = query.or_(f"description.ilike.%{search_query}%,heading.ilike.%{search_query}%")

        if start_date: query = query.gte('date', start_date)
        if end_date: query = query.lte('date', end_date)
        if category: query = query.eq('heading', category)
        if fund: query = query.or_(f"fund_account.eq.{fund},to_fund_account.eq.{fund}")

        query = query.order('date', desc=True).order('created_at', desc=True)
        
        offset = (page - 1) * limit
        all_transactions = []
        
        # AUTO-CHUNKING: Bypasses Supabase 1,000 row hard-limit for exports
        for i in range(0, limit, 1000):
            chunk_limit = min(1000, limit - i)
            chunk_offset = offset + i
            chunk_res = query.range(chunk_offset, chunk_offset + chunk_limit - 1).execute()
            
            all_transactions.extend(chunk_res.data)
            
            # If we returned fewer than 1000 rows, we've hit the end of the database
            if len(chunk_res.data) < chunk_limit:
                break
        
        return jsonify({
            "transactions": all_transactions
        }), 200

    except Exception:
        return jsonify({"error": "Failed to fetch transactions"}), 500

# -----------------------------------------------------------
# 3. CREATE TRANSACTIONS
# -----------------------------------------------------------
@app.route('/api/transaction', methods=['POST'])
@login_required
def create_transaction():
    data = request.json or {}
    transactions = data.get('transactions', [])
    
    if not isinstance(transactions, list) or len(transactions) == 0:
        return jsonify({"error": "No transactions provided"}), 400
        
    if len(transactions) > 50:
        return jsonify({"error": "Payload exceeds maximum allowed items (50)"}), 400

    try:
        response = supabase.rpc('process_transaction_batch', {'transactions_data': transactions}).execute()
        return jsonify({"message": "Successfully created"}), 201
    except Exception:
        return jsonify({"error": "Failed to log transactions safely"}), 500

# -----------------------------------------------------------
# 4. ATOMIC EDIT TRANSACTION
# -----------------------------------------------------------
@app.route('/api/transaction/edit', methods=['POST'])
@login_required
def edit_transaction_atomic():
    data = request.json or {}
    old_id = data.get('old_id')
    new_transactions = data.get('transactions')
    
    if not is_valid_uuid(old_id):
        return jsonify({"error": "Invalid old_id format"}), 400
        
    if not isinstance(new_transactions, list) or len(new_transactions) > 50:
        return jsonify({"error": "Invalid payload format or size"}), 400

    try:
        supabase.rpc('atomic_edit_transaction', {
            'p_old_id': old_id,
            'p_new_transactions': new_transactions
        }).execute()
        
        return jsonify({"message": "Transaction edited atomically"}), 200
    except Exception:
        return jsonify({"error": "Failed to execute atomic edit"}), 500

# -----------------------------------------------------------
# 5. ATOMIC DELETE TRANSACTION
# -----------------------------------------------------------
@app.route('/api/transaction/<id>', methods=['DELETE'])
@login_required
def delete_transaction(id):
    if not is_valid_uuid(id):
        return jsonify({"error": "Invalid ID format"}), 400
        
    try:
        supabase.rpc('delete_transaction_atomic', {'p_id': id}).execute()
        return jsonify({"message": "Successfully deleted atomically"}), 200
    except Exception:
        return jsonify({"error": "Failed to delete transaction"}), 500

if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(debug=debug_mode, port=5000)