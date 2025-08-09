import os, sqlite3, secrets
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, g
import requests
from dotenv import load_dotenv
load_dotenv()
APP_SECRET = os.environ.get("APP_SECRET", secrets.token_hex(16))
DB_PATH = os.path.join(os.path.dirname(__file__), "panel.db")
API_VERSION = "2024-07"
ENV_STORE = os.environ.get("STORE", "").strip()
ENV_TOKEN = os.environ.get("TOKEN", "").strip()
ENV_LOCATION = os.environ.get("LOCATION_ID", "").strip()
TAX_RATE = float(os.environ.get("TAX_RATE") or 0.0)
app = Flask(__name__)
app.secret_key = APP_SECRET
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA journal_mode=WAL;')
        g.db.execute('PRAGMA busy_timeout=15000;')
        g.db.execute('PRAGMA synchronous=NORMAL;')
    return g.db
@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None: db.close()
def init_db():
    db = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL;')
    db.execute('PRAGMA busy_timeout=15000;')
    db.execute('PRAGMA synchronous=NORMAL;')
    db.executescript("""
CREATE TABLE IF NOT EXISTS auth (id INTEGER PRIMARY KEY CHECK (id=1), admin_password TEXT);
INSERT OR IGNORE INTO auth (id, admin_password) VALUES (1, NULL);
CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, phone TEXT, email TEXT, created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS sales (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT (datetime('now','localtime')), customer_id INTEGER, subtotal REAL, tax REAL, total REAL, payment_method TEXT);
CREATE TABLE IF NOT EXISTS sale_items (id INTEGER PRIMARY KEY AUTOINCREMENT, sale_id INTEGER, variant_id INTEGER, inventory_item_id INTEGER, sku TEXT, barcode TEXT, title TEXT, qty INTEGER, unit_price REAL);
""")
    db.commit(); db.close()
def get_admin_password_raw():
    r = get_db().execute('SELECT admin_password FROM auth WHERE id=1').fetchone()
    return r["admin_password"] if r else None
def set_admin_password_plain(pw):
    get_db().execute('UPDATE auth SET admin_password=? WHERE id=1', (f'plain:{pw}',))
    get_db().commit()
def verify_password(input_pw, stored):
    if not stored: return False
    if stored.startswith('plain:'): return input_pw == stored.split('plain:',1)[1]
    return False
def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def w(*a, **k):
        if not session.get('logged_in'): return redirect(url_for('login'))
        return fn(*a, **k)
    return w
@app.route('/login', methods=['GET','POST'])
def login():
    pw_stored = get_admin_password_raw()
    if pw_stored is None or pw_stored == '' or pw_stored == 'plain:':
        if request.method=='POST':
            newpw = (request.form.get('password') or '').strip()
            if len(newpw) < 4: flash('Şifre en az 4 karakter olmalı.', 'error')
            else:
                set_admin_password_plain(newpw)
                session['logged_in'] = True
                return redirect(url_for('customer'))
        return render_template('first_login.html')
    else:
        if request.method=='POST':
            if verify_password(request.form.get('password',''), pw_stored):
                session['logged_in'] = True
                return redirect(url_for('customer'))
            flash('Şifre hatalı.', 'error')
        return render_template('login.html')
@app.route('/logout')
def logout(): session.clear(); return redirect(url_for("login"))
@app.route('/whoami')
def whoami(): return jsonify({'logged_in': bool(session.get('logged_in')), 'customer_id': session.get('customer_id')})
def shopify_headers(token): return {'X-Shopify-Access-Token': token, 'Content-Type':'application/json', 'Accept':'application/json'}
def sget(store, token, path, params=None): return requests.get(f'https://{store}/admin/api/{API_VERSION}/{path}', headers=shopify_headers(token), params=params, timeout=60)
def spost(store, token, path, payload): return requests.post(f'https://{store}/admin/api/{API_VERSION}/{path}', headers=shopify_headers(token), json=payload, timeout=60)
def find_variant_by_barcode(store, token, barcode):
    code = (str(barcode) or '').strip()
    since_id=None
    for _ in range(20):
        params={'limit':250}
        if since_id: params['since_id']=since_id
        r=sget(store, token, 'variants.json', params=params)
        js=r.json(); arr=js.get('variants',[])
        if not arr: break
        for v in arr:
            if ((v.get('barcode') or '').strip() == code): return v
        since_id=arr[-1].get('id')
    return None
@app.route('/customer', methods=['GET','POST'])
def customer():
    if not session.get('logged_in'): return redirect(url_for('login'))
    db=get_db()
    if request.method=='POST':
        if request.form.get('action')=='create':
            name=(request.form.get('name') or '').strip()
            phone=(request.form.get('phone') or '').strip()
            email=(request.form.get('email') or '').strip()
            db.execute('INSERT INTO customers(name,phone,email) VALUES (?,?,?)',(name,phone,email))
            db.commit(); cid=db.execute('SELECT last_insert_rowid()').fetchone()[0]
            session['customer_id']=cid; return redirect(url_for('pos'))
        else:
            cid=int(request.form.get('customer_id') or 0)
            if cid: session['customer_id']=cid; return redirect(url_for('pos'))
    rows=db.execute('SELECT id,name,phone,email FROM customers ORDER BY id DESC LIMIT 200').fetchall()
    return render_template('customer.html', rows=rows)
@app.route('/pos')
def pos():
    if not session.get('logged_in'): return redirect(url_for('login'))
    if not session.get('customer_id'): return redirect(url_for('customer'))
    r=get_db().execute('SELECT id,name,phone FROM customers WHERE id=?',(session['customer_id'],)).fetchone()
    return render_template('pos.html', tax_rate=TAX_RATE, customer=r)
@app.route('/pos/change_customer')
def pos_change_customer(): session.pop('customer_id', None); return redirect(url_for('customer'))
@app.route('/api/scan')
def api_scan():
    if not session.get('logged_in'): return jsonify({'ok':False,'error':'Auth'}),401
    if not session.get('customer_id'): return jsonify({'ok':False,'error':'Müşteri seçilmedi'}),400
    code=(request.args.get('code') or '').strip()
    if not code: return jsonify({'ok':False,'error':'code gerekli'}),400
    v=find_variant_by_barcode(ENV_STORE, ENV_TOKEN, code)
    if not v: return jsonify({'ok':False,'error':'BARKOD bulunamadı'}),404
    price=float(v.get('price') or 0.0)
    product_title=None; pid=v.get('product_id')
    if pid: product_title=sget(ENV_STORE, ENV_TOKEN, f'products/{pid}.json').json().get('product',{}).get('title')
    return jsonify({'ok':True,'variant':{'id':v.get('id'),'title':v.get('title'),'product_title':product_title,'sku':v.get('sku'),'barcode':v.get('barcode'),'inventory_item_id':v.get('inventory_item_id'),'price':price}})
@app.route('/api/checkout', methods=['POST'])
def api_checkout():
    if not session.get('logged_in'): return jsonify({'ok':False,'error':'Auth'}),401
    if not session.get('customer_id'): return jsonify({'ok':False,'error':'Müşteri seçilmedi'}),400
    body=request.get_json(silent=True) or {}
    cart=body.get('cart', [])
    payment=body.get('payment_method') or 'cash'
    if not cart: return jsonify({'ok':False,'error':'Sepet boş'}),400
    db=get_db(); cust_id=int(session['customer_id'])
    subtotal=sum(float(i.get('price') or 0)*int(i.get('qty') or 1) for i in cart)
    tax=round(subtotal*TAX_RATE,2); total=round(subtotal+tax,2)
    db.execute('INSERT INTO sales(customer_id,subtotal,tax,total,payment_method) VALUES (?,?,?,?,?)',(cust_id,subtotal,tax,total,payment)); db.commit()
    sale_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
    for it in cart: db.execute('INSERT INTO sale_items(sale_id,variant_id,inventory_item_id,sku,barcode,title,qty,unit_price) VALUES (?,?,?,?,?,?,?,?)',(sale_id,it['id'],it.get('inventory_item_id'),it.get('sku'),it.get('barcode'),it.get('title'),int(it.get('qty') or 1),float(it.get('price') or 0)))
    db.commit()
    for it in cart:
        try: spost(ENV_STORE, ENV_TOKEN, 'inventory_levels/adjust.json', {'inventory_item_id': int(it['inventory_item_id']), 'location_id': int(ENV_LOCATION or 0), 'available_adjustment': -int(it.get('qty') or 1)})
        except Exception: pass
    return jsonify({'ok':True,'sale_id':sale_id,'total':total})
def fetch_all_variants(store, token, page_size=250, limit_pages=8):
    results=[]; since_id=None
    for _ in range(limit_pages):
        params={'limit':page_size}
        if since_id: params['since_id']=since_id
        js=sget(store, token, 'variants.json', params=params).json()
        arr=js.get('variants',[])
        if not arr: break
        results.extend(arr); since_id=arr[-1].get('id')
    return results
@app.route('/inventory')
def inventory():
    if not session.get('logged_in'): return redirect(url_for('login'))
    try: variants=fetch_all_variants(ENV_STORE, ENV_TOKEN, 250, 8)
    except Exception: variants=[]
    return render_template('inventory.html', variants=variants)
@app.route('/reports')
def reports():
    if not session.get('logged_in'): return redirect(url_for('login'))
    db=get_db()
    daily=db.execute("SELECT date(ts) d, SUM(total) t FROM sales WHERE date(ts)=date('now','localtime') GROUP BY d").fetchone()
    monthly=db.execute("SELECT strftime('%Y-%m', ts) m, SUM(total) t FROM sales WHERE strftime('%Y-%m', ts)=strftime('%Y-%m','now','localtime') GROUP BY m").fetchone()
    return render_template('reports.html', daily_total=(daily['t'] if daily and daily['t'] else 0), monthly_total=(monthly['t'] if monthly and monthly['t'] else 0))
@app.route('/sales')
def sales_list():
    if not session.get('logged_in'): return redirect(url_for('login'))
    rows=get_db().execute('SELECT id, ts, total, payment_method FROM sales ORDER BY id DESC LIMIT 200').fetchall()
    return render_template('sales.html', rows=rows)
@app.route('/customers')
def customers_list():
    if not session.get('logged_in'): return redirect(url_for('login'))
    rows=get_db().execute('SELECT * FROM customers ORDER BY id DESC LIMIT 200').fetchall()
    return render_template('customers.html', rows=rows)
@app.route('/')
def root(): return redirect(url_for('customer'))
@app.errorhandler(404)
def nf(e):
    return '<div style="font-family:system-ui,Arial;padding:24px"><h3>Sayfa bulunamadı</h3><p>Müşteri: <a href="/customer">/customer</a> | POS: <a href="/pos">/pos</a> | Stok: <a href="/inventory">/inventory</a></p></div>', 404
if __name__ == '__main__':
    init_db(); port=int(os.environ.get('PORT',5010)); app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)