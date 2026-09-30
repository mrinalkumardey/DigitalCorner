import os
from flask import Flask, request, jsonify, render_template
from supabase import create_client, Client
from dotenv import load_dotenv

# Load environment variables (SUPABASE_URL, SUPABASE_KEY)
load_dotenv()

app = Flask(__name__)

# Initialize Supabase Client
url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(url, key)

@app.route('/')
def index():
    return render_template('index.html')


# -----------------------------------------------------------
# 1. DASHBOARD DATA (Optimized with RPC to bypass 1k row limit)
# -----------------------------------------------------------
@app.route('/api/data', methods=['GET'])
def get_initial_data():
    client_date = request.args.get('client_date')
    current_month = client_date[:7] if client_date else ""

    try:
        # 1. Let Postgres do the math (Bypasses the 1,000 row limit!)
        stats_response = supabase.rpc('get_monthly_stats', {'month_prefix': current_month}).execute()
        monthly_stats = stats_response.data

        # 2. ONLY fetch today's transactions for the dashboard table (Saves massive bandwidth)
        today_tx_response = supabase.table('transactions').select('*').eq('date', client_date).order('created_at', desc=True).execute()
        dashboard_transactions = today_tx_response.data

        # 3. Fetch current fund balances
        funds_response = supabase.table('funds').select('*').execute()
        funds = funds_response.data

        return jsonify({
            "transactions": dashboard_transactions,
            "funds": funds,
            "monthly_stats": monthly_stats
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -----------------------------------------------------------
# 2. PAGINATED TRANSACTIONS & SEARCH
# -----------------------------------------------------------
@app.route('/api/transactions', methods=['GET'])
def get_transactions():
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))
    search_query = request.args.get('search', '').strip()
    linked_id = request.args.get('linked_id', '').strip()
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    category = request.args.get('category', '')
    fund = request.args.get('fund', '')

    try:
        query = supabase.table('transactions').select('*', count='exact')

        # If frontend requests a specific linked_id, filter by it directly
        if linked_id:
            query = query.eq('linked_id', linked_id)
        elif search_query:
            # Otherwise, do the normal text search
            query = query.or_(f"description.ilike.%{search_query}%,heading.ilike.%{search_query}%,id.ilike.%{search_query}%")

        # Apply specific filters if they exist
        if start_date:
            query = query.gte('date', start_date)
        if end_date:
            query = query.lte('date', end_date)
        if category:
            query = query.eq('heading', category)
        if fund:
            # Match either source or destination fund
            query = query.or_(f"fund_account.eq.{fund},to_fund_account.eq.{fund}")

        # Calculate PostgREST pagination range
        offset = (page - 1) * limit
        query = query.range(offset, offset + limit - 1).order('date', desc=True).order('created_at', desc=True)
        
        response = query.execute()
        
        return jsonify({
            "transactions": response.data,
            "total_count": response.count
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -----------------------------------------------------------
# 3. CREATE TRANSACTIONS (Batch Mode for Invoices/Double-Entry)
# -----------------------------------------------------------
@app.route('/api/transaction', methods=['POST'])
def create_transaction():
    data = request.json
    transactions = data.get('transactions', [])
    
    if not transactions:
        return jsonify({"error": "No transactions provided"}), 400

    try:
        response = supabase.table('transactions').insert(transactions).execute()
        return jsonify({"message": "Successfully created", "data": response.data}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -----------------------------------------------------------
# 4. ATOMIC EDIT TRANSACTION (Using Supabase SQL RPC)
# -----------------------------------------------------------
@app.route('/api/transaction/edit', methods=['POST'])
def edit_transaction_atomic():
    data = request.json
    old_id = data.get('old_id')
    new_transactions = data.get('transactions')
    
    if not old_id or not new_transactions:
        return jsonify({"error": "Missing old_id or transaction data"}), 400

    try:
        # Call the Postgres function to do the safe, atomic swap
        supabase.rpc('atomic_edit_transaction', {
            'p_old_id': old_id,
            'p_new_transactions': new_transactions
        }).execute()
        
        return jsonify({"message": "Transaction edited atomically successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -----------------------------------------------------------
# 5. DELETE TRANSACTION
# -----------------------------------------------------------
@app.route('/api/transaction/<id>', methods=['DELETE'])
def delete_transaction(id):
    try:
        # First, find the transaction to see if it belongs to a linked cluster (like an invoice + portal cost)
        tx_response = supabase.table('transactions').select('linked_id').eq('id', id).execute()
        
        if not tx_response.data:
            return jsonify({"error": "Transaction not found"}), 404
            
        linked_id = tx_response.data[0].get('linked_id')
        
        if linked_id:
            # Delete all transactions with this linked_id simultaneously
            supabase.table('transactions').delete().eq('linked_id', linked_id).execute()
        else:
            # Fallback for legacy transactions without a linked_id
            supabase.table('transactions').delete().eq('id', id).execute()
            
        return jsonify({"message": "Successfully deleted"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    # Ensure templates folder exists for development
    app.run(debug=True, port=5000)