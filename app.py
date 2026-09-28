from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from functools import wraps
from supabase import create_client, Client
import datetime
import os
from dotenv import load_dotenv

# Load sensitive variables from the hidden .env file
load_dotenv()

app = Flask(__name__)

# --- CRITICAL SECURITY SETTINGS ---
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'fallback-dev-key-do-not-use-in-prod')
app.permanent_session_lifetime = datetime.timedelta(hours=4)

# --- CREDENTIALS & API KEYS ---
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'User1')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'zxcv')
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')

db: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized. Please log in.'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session.permanent = True 
            session['logged_in'] = True
            return redirect(url_for('home'))
        else:
            return render_template('login.html', error="Invalid username or password.")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def home():
    return render_template('index.html')

@app.route('/api/data', methods=['GET'])
@login_required
def get_data():
    try:
        funds_res = db.table('funds').select('*').execute()
        recent_tx = db.table('transactions').select('*').order('date', desc=True).order('created_at', desc=True).limit(500).execute()
        
        client_date_str = request.args.get('client_date')
        if client_date_str:
            client_date = datetime.datetime.strptime(client_date_str, '%Y-%m-%d').date()
        else:
            client_date = datetime.date.today()
            
        first_day_of_month = client_date.replace(day=1).isoformat()
        monthly_tx_res = db.table('transactions').select('type, amount, heading').gte('date', first_day_of_month).execute()
        
        monthly_inflow = 0
        monthly_outflow = 0
        category_breakdown = {}
        
        for tx in monthly_tx_res.data:
            amt = float(tx['amount'])
            if tx['type'] == 'income':
                monthly_inflow += amt
                cat = tx['heading']
                category_breakdown[cat] = category_breakdown.get(cat, 0) + amt
            elif tx['type'] == 'expense':
                monthly_outflow += amt

        return jsonify({
            'funds': funds_res.data,
            'transactions': recent_tx.data,
            'monthly_stats': {
                'inflow': monthly_inflow,
                'outflow': monthly_outflow,
                'breakdown': category_breakdown
            }
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/transactions', methods=['GET'])
@login_required
def get_paginated_transactions():
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 50))
        search = request.args.get('search', '')
        start_date = request.args.get('start_date', '')
        end_date = request.args.get('end_date', '')
        cat_filter = request.args.get('category', '')
        fund_filter = request.args.get('fund', '')

        start = (page - 1) * limit
        end = start + limit - 1

        query = db.table('transactions').select('*')

        if start_date: query = query.gte('date', start_date)
        if end_date: query = query.lte('date', end_date)
        if cat_filter: query = query.eq('heading', cat_filter)
        if fund_filter: query = query.or_(f"fund_account.eq.{fund_filter},to_fund_account.eq.{fund_filter}")
        
        if search: query = query.or_(f"description.ilike.%{search}%,heading.ilike.%{search}%")

        res = query.order('date', desc=True).order('created_at', desc=True).range(start, end).execute()
        return jsonify({'transactions': res.data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/transaction', methods=['POST'])
@login_required
def add_transaction():
    try:
        data = request.json
        tx_list = data.get('transactions', [])
        
        if not tx_list:
            return jsonify({'error': 'No transaction data provided.'}), 400

        # Hand the entire array to the Supabase Stored Procedure
        db.rpc('process_transaction_batch', {'tx_list': tx_list}).execute()
        
        return jsonify({'success': True})
    except Exception as e:
        # Pass the PostgreSQL error exactly as generated by the RPC RAISE EXCEPTION
        return jsonify({'error': str(e)}), 400

@app.route('/api/transaction/<tx_id>', methods=['DELETE'])
@login_required
def delete_transaction(tx_id):
    try:
        # Hand the UUID to the Supabase Stored Procedure for atomic reversal
        db.rpc('delete_transaction_atomic', {'p_tx_id': tx_id}).execute()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

if __name__ == '__main__':
    app.run(debug=True, port=5000)