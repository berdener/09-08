import os, sqlite3, secrets
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, g
import requests
from dotenv import load_dotenv
import sqlite3

# sale_date sütunu yoksa ekle
conn = sqlite3.connect('database.db')  # burada database.db senin veritabanı dosya adın
cursor = conn.cursor()

# Sütun kontrolü
cursor.execute("PRAGMA table_info(sales)")
columns = [col[1] for col in cursor.fetchall()]
if 'sale_date' not in columns:
    cursor.execute("ALTER TABLE sales ADD COLUMN sale_date TEXT")
    conn.commit()

conn.close()

load_dotenv()

APP_SECRET = os.environ.get("APP_SECRET", secrets.token_hex(16))
DB_PATH = os.path.join(os.path.dirname(__file__), "panel.db")
API_VERSION = "2024-07"

ENV_STORE = os.environ.get("STORE", "").strip()
ENV_TOKEN = os.environ.get("TOKEN", "").strip()
ENV_LOCATION = os.environ.get("LOCATION_ID", "").strip()

# Vergi oranı; ör. 0.18
TAX_RATE = float(os.environ.get("TAX_RATE") or 0.0)

app = Flask(__name__)
app.secret_key = APP_SECRET


# ----------------------- DB -----------------------
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
    if db is not None:
        db.close()

def init_db():
    db = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL;')
    db.execute('PRAGMA busy_timeout=15000;')
    db.execute('PRAGMA synchronous=NORMAL;')
    db.executescript("""
CREATE TABLE IF NOT EXISTS auth (
  id INTEGER PRIMARY KEY CHECK (id=1),
  admin_password TEXT
);
INSERT OR IGNORE INTO auth (id, admin_password) VALUES (1, NULL);

CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT,
  phone TEXT,
  email TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS sales (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT DEFAULT (datetime('now','localtime')),
  customer_id INTEGER,
  subtotal REAL,
  tax REAL,
  total REAL,
  payment_method TEXT
);

CREATE TABLE IF NOT EXISTS sale_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sale_id INTEGER,
  variant_id INTEGER,
  inventory_item_id INTEGER,
  sku TEXT,
  barcode TEXT,
  title TEXT,
  qty INTEGER,
  unit_price REAL
);

-- İADE / DEĞİŞİM (yeni)
CREATE TABLE IF NOT EXISTS returns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT DEFAULT (datetime('now','localtime')),
  sale_id INTEGER,
  refund REAL,            -- müşteriye iade tutarı (pozitif)
  additional_charge REAL, -- müşteriden tahsil tutarı (pozitif)
  net REAL,               -- net = additional_charge - refund (pozitifse tahsil, negatifse iade)
  payment_method TEXT,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS return_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  return_id INTEGER,
  sale_item_id INTEGER,   -- hangi satış satırından iade edildi
  qty INTEGER,            -- iade edilen adet
  unit_price REAL         -- referans birim fiyat
);
""")
    db.commit()
    db.close()


# ----------------------- Auth helpers -----------------------
def get_admin_password_raw():
    r = get_db().execute('SELECT admin_password FROM auth WHERE id=1').fetchone()
    return r["admin_password"] if r else None

def set_admin_password_plain(pw):
    get_db().execute('UPDATE auth SET admin_password=? WHERE id=1', (f'plain:{pw}',))
    get_db().commit()

def verify_password(input_pw, stored):
    if not stored:
        return False
    if stored.startswith('plain:'):
        return input_pw == stored.split('plain:', 1)[1]
    return False

def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def w(*a, **k):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return fn(*a, **k)
    return w


# ----------------------- Shopify helpers -----------------------
def shopify_headers(token):
    return {'X-Shopify-Access-Token': token, 'Content-Type': 'application/json', 'Accept': 'application/json'}

def sget(store, token, path, params=None):
    return requests.get(f'https://{store}/admin/api/{API_VERSION}/{path}',
                        headers=shopify_headers(token), params=params, timeout=60)

def spost(store, token, path, payload):
    return requests.post(f'https://{store}/admin/api/{API_VERSION}/{path}',
                         headers=shopify_headers(token), json=payload, timeout=60)

def find_variant_by_barcode(store, token, barcode):
    code = (str(barcode) or '').strip()
    since_id = None
    for _ in range(20):
        params = {'limit': 250}
        if since_id:
            params['since_id'] = since_id
        r = sget(store, token, 'variants.json', params=params)
        js = r.json()
        arr = js.get('variants', [])
        if not arr:
            break
        for v in arr:
            if ((v.get('barcode') or '').strip() == code):
                return v
        since_id = arr[-1].get('id')
    return None


# ----------------------- Routes: Auth -----------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    pw_stored = get_admin_password_raw()
    if pw_stored is None or pw_stored == '' or pw_stored == 'plain:':
        if request.method == 'POST':
            newpw = (request.form.get('password') or '').strip()
            if len(newpw) < 4:
                flash('Şifre en az 4 karakter olmalı.', 'error')
            else:
                set_admin_password_plain(newpw)
                session['logged_in'] = True
                return redirect(url_for('customer'))
        return render_template('first_login.html')
    else:
        if request.method == 'POST':
            if verify_password(request.form.get('password', ''), pw_stored):
                session['logged_in'] = True
                return redirect(url_for('customer'))
            flash('Şifre hatalı.', 'error')
        return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route('/whoami')
def whoami():
    return jsonify({'logged_in': bool(session.get('logged_in')),
                    'customer_id': session.get('customer_id')})


# ----------------------- Routes: Müşteri & POS -----------------------
@app.route('/customer', methods=['GET', 'POST'])
def customer():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    db = get_db()
    if request.method == 'POST':
        if request.form.get('action') == 'create':
            name = (request.form.get('name') or '').strip()
            phone = (request.form.get('phone') or '').strip()
            email = (request.form.get('email') or '').strip()
            db.execute('INSERT INTO customers(name,phone,email) VALUES (?,?,?)', (name, phone, email))
            db.commit()
            cid = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            session['customer_id'] = cid
            return redirect(url_for('pos'))
        else:
            cid = int(request.form.get('customer_id') or 0)
            if cid:
                session['customer_id'] = cid
                return redirect(url_for('pos'))
    rows = db.execute('SELECT id,name,phone,email FROM customers ORDER BY id DESC LIMIT 200').fetchall()
    return render_template('customer.html', rows=rows)

@app.route('/pos')
def pos():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    if not session.get('customer_id'):
        return redirect(url_for('customer'))
    r = get_db().execute('SELECT id,name,phone FROM customers WHERE id=?', (session['customer_id'],)).fetchone()
    return render_template('pos.html', tax_rate=TAX_RATE, customer=r)

@app.route('/pos/change_customer')
def pos_change_customer():
    session.pop('customer_id', None)
    return redirect(url_for('customer'))

@app.route('/api/scan')
def api_scan():
    if not session.get('logged_in'):
        return jsonify({'ok': False, 'error': 'Auth'}), 401
    if not session.get('customer_id'):
        return jsonify({'ok': False, 'error': 'Müşteri seçilmedi'}), 400
    code = (request.args.get('code') or '').strip()
    if not code:
        return jsonify({'ok': False, 'error': 'code gerekli'}), 400
    v = find_variant_by_barcode(ENV_STORE, ENV_TOKEN, code)
    if not v:
        return jsonify({'ok': False, 'error': 'BARKOD bulunamadı'}), 404
    price = float(v.get('price') or 0.0)
    product_title = None
    pid = v.get('product_id')
    if pid:
        product_title = sget(ENV_STORE, ENV_TOKEN, f'products/{pid}.json').json().get('product', {}).get('title')
    return jsonify({'ok': True, 'variant': {
        'id': v.get('id'),
        'title': v.get('title'),
        'product_title': product_title,
        'sku': v.get('sku'),
        'barcode': v.get('barcode'),
        'inventory_item_id': v.get('inventory_item_id'),
        'price': price
    }})

# ---------- Satış tamamla (İNDİRİM destekli) ----------
@app.route('/api/checkout', methods=['POST'])
def api_checkout():
    if not session.get('logged_in'):
        return jsonify({'ok': False, 'error': 'Auth'}), 401
    if not session.get('customer_id'):
        return jsonify({'ok': False, 'error': 'Müşteri seçilmedi'}), 400

    body = request.get_json(silent=True) or {}
    cart = body.get('cart', [])
    payment = body.get('payment_method') or 'cash'
    if not cart:
        return jsonify({'ok': False, 'error': 'Sepet boş'}), 400

    # 1) Ara Toplam
    subtotal = sum(float(i.get('price') or 0) * int(i.get('qty') or 1) for i in cart)

    # 2) İndirim (opsiyonel; öncelik: percent > amount)
    discount_type = body.get('discount_type')
    try:
        discount_value = float(body.get('discount_value') or 0.0)
    except Exception:
        discount_value = 0.0

    discount = 0.0
    if discount_type == 'percent' and discount_value > 0:
        discount = subtotal * (discount_value / 100.0)
    elif discount_type == 'amount' and discount_value > 0:
        discount = min(discount_value, subtotal)
    discount = round(discount, 2)

    # 3) Vergi (indirimin ardından)
    taxable_base = max(subtotal - discount, 0.0)
    tax = round(taxable_base * TAX_RATE, 2)
    total = round(taxable_base + tax, 2)

    db = get_db()
    cust_id = int(session['customer_id'])

    # 4) Satış başlık kaydı
    db.execute(
        'INSERT INTO sales(customer_id, subtotal, tax, total, payment_method) VALUES (?,?,?,?,?)',
        (cust_id, subtotal, tax, total, payment)
    )
    db.commit()
    sale_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

    # 5) Satırları yaz
    for it in cart:
        db.execute(
            'INSERT INTO sale_items(sale_id, variant_id, inventory_item_id, sku, barcode, title, qty, unit_price) VALUES (?,?,?,?,?,?,?,?)',
            (
                sale_id,
                it['id'],
                it.get('inventory_item_id'),
                it.get('sku'),
                it.get('barcode'),
                it.get('title'),
                int(it.get('qty') or 1),
                float(it.get('price') or 0)
            )
        )
    db.commit()

    # 6) Shopify stok düş
    for it in cart:
        try:
            spost(
                ENV_STORE, ENV_TOKEN,
                'inventory_levels/adjust.json',
                {
                    'inventory_item_id': int(it['inventory_item_id']),
                    'location_id': int(ENV_LOCATION or 0),
                    'available_adjustment': -int(it.get('qty') or 1)
                }
            )
        except Exception:
            pass

    return jsonify({
        'ok': True,
        'sale_id': sale_id,
        'total': total,
        'tax': tax,
        'subtotal': subtotal,
        'discount': discount,
        'discount_type': discount_type,
        'discount_value': discount_value
    })


# --------- Yardımcı: satış ve satırlarını çek (iade/değişim için) ---------
def get_sale_with_items(sale_id: int):
    db = get_db()
    sale = db.execute('SELECT id, ts, customer_id, subtotal, tax, total FROM sales WHERE id=?', (sale_id,)).fetchone()
    if not sale:
        return None, []
    items = db.execute(
        'SELECT id, variant_id, inventory_item_id, sku, barcode, title, qty, unit_price '
        'FROM sale_items WHERE sale_id=?',
        (sale_id,)
    ).fetchall()
    return sale, items


# ----------------------- Stok, Rapor, Liste -----------------------
def fetch_all_variants(store, token, page_size=250, limit_pages=8):
    results = []
    since_id = None
    for _ in range(limit_pages):
        params = {'limit': page_size}
        if since_id:
            params['since_id'] = since_id
        js = sget(store, token, 'variants.json', params=params).json()
        arr = js.get('variants', [])
        if not arr:
            break
        results.extend(arr)
        since_id = arr[-1].get('id')
    return results

@app.route('/inventory')
def inventory():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    try:
        variants = fetch_all_variants(ENV_STORE, ENV_TOKEN, 250, 8)
    except Exception:
        variants = []
    return render_template('inventory.html', variants=variants)

@app.route('/reports')
@login_required
def reports():
    db = get_db()

    # Günlük veriler
    today = datetime.now().date()
    daily_sales = db.execute("""
        SELECT SUM(total_amount) 
        FROM sales 
        WHERE DATE(sale_date) = ? AND payment_type != 'Veresiye'
    """, (today,)).fetchone()[0] or 0

    daily_credit = db.execute("""
        SELECT SUM(total_amount) 
        FROM sales 
        WHERE DATE(sale_date) = ? AND payment_type = 'Veresiye'
    """, (today,)).fetchone()[0] or 0

    # Aylık veriler
    first_day_month = today.replace(day=1)
    monthly_sales = db.execute("""
        SELECT SUM(total_amount) 
        FROM sales 
        WHERE DATE(sale_date) >= ? AND payment_type != 'Veresiye'
    """, (first_day_month,)).fetchone()[0] or 0

    monthly_credit = db.execute("""
        SELECT SUM(total_amount) 
        FROM sales 
        WHERE DATE(sale_date) >= ? AND payment_type = 'Veresiye'
    """, (first_day_month,)).fetchone()[0] or 0

    return render_template(
        'reports.html',
        daily_sales=daily_sales,
        daily_credit=daily_credit,
        monthly_sales=monthly_sales,
        monthly_credit=monthly_credit
    )

@app.route('/sales')
def sales_list():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    rows = get_db().execute('SELECT id, ts, total, payment_method FROM sales ORDER BY id DESC LIMIT 200').fetchall()
    return render_template('sales.html', rows=rows)
@app.route('/returns')
def returns_list():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    rows = get_db().execute("""
      SELECT id, ts, sale_id, refund, additional_charge, net, payment_method, notes
      FROM returns
      ORDER BY id DESC
      LIMIT 500
    """).fetchall()
    return render_template('returns.html', rows=rows)

@app.route('/returns.csv')
def returns_csv():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    import csv, io
    db = get_db()
    rows = db.execute("""
      SELECT id, ts, sale_id, refund, additional_charge, net, payment_method, notes
      FROM returns
      ORDER BY id DESC
      LIMIT 10000
    """).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['id','ts','sale_id','refund','additional_charge','net','payment_method','notes'])
    for r in rows:
        w.writerow([r['id'], r['ts'], r['sale_id'], r['refund'], r['additional_charge'], r['net'], r['payment_method'], r['notes']])
    from flask import Response
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=returns.csv'})

@app.route('/customers')
def customers_list():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    rows = get_db().execute('SELECT * FROM customers ORDER BY id DESC LIMIT 200').fetchall()
    return render_template('customers.html', rows=rows)


# ----------------------- İade / Değişim -----------------------
@app.route('/return/<int:sale_id>')
def return_exchange_page(sale_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    sale, items = get_sale_with_items(sale_id)
    if not sale:
        flash('Satış bulunamadı.', 'error')
        return redirect(url_for('sales_list'))
    return render_template('return.html', sale=sale, items=items, tax_rate=TAX_RATE)

@app.route('/api/return', methods=['POST'])
def api_return():
    if not session.get('logged_in'):
        return jsonify({'ok': False, 'error': 'Auth'}), 401

    body = request.get_json(silent=True) or {}
    sale_id = int(body.get('sale_id') or 0)
    return_lines = body.get('return_lines', [])   # [{sale_item_id, qty}]
    exchange_cart = body.get('exchange_cart', []) # [{id, inventory_item_id, sku, barcode, title, qty, price}]
    payment_method = body.get('payment_method') or 'cash'
    notes = (body.get('notes') or '').strip()

    if not sale_id:
        return jsonify({'ok': False, 'error': 'sale_id gerekli'}), 400

    sale, original_items = get_sale_with_items(sale_id)
    if not sale:
        return jsonify({'ok': False, 'error': 'Satış bulunamadı'}), 404

    db = get_db()
    # sqlite3.Row => dict benzeri; .get yok, köşeli parantez kullanacağız
    orig_map = {row['id']: row for row in original_items}

    # 1) İade toplamı
    return_subtotal = 0.0
    for rl in return_lines:
        sid = int(rl.get('sale_item_id') or 0)
        qty = int(rl.get('qty') or 0)
        if sid and qty > 0 and sid in orig_map:
            unit_price = float(orig_map[sid]['unit_price'] or 0)
            return_subtotal += unit_price * qty

    # 2) Değişim toplamı
    def _f(x): 
        try: return float(x)
        except: return 0.0
    def _i(x): 
        try: return int(x)
        except: return 0
    exchange_subtotal = sum(_f(it.get('price')) * max(1, _i(it.get('qty'))) for it in exchange_cart)

    # 3) Vergi yalnız değişim kısmına; iade negatif etki
    taxable_base = max(exchange_subtotal - return_subtotal, 0.0)
    tax = round(taxable_base * TAX_RATE, 2)
    total = round(taxable_base + tax, 2)

    # Net: + tahsil / - iade
    net = round(exchange_subtotal - return_subtotal, 2)
    refund = max(0.0, round(-net, 2))           # müşteriye iade
    additional_charge = max(0.0, round(net, 2)) # müşteriden tahsil

    # 4) returns kaydı
    db.execute(
        'INSERT INTO returns(sale_id, refund, additional_charge, net, payment_method, notes) VALUES (?,?,?,?,?,?)',
        (sale_id, refund, additional_charge, net, payment_method, notes)
    )
    db.commit()
    return_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

    # 5) return_items kaydı + iade için stok geri ekleme
    for rl in return_lines:
        sid = int(rl.get('sale_item_id') or 0)
        qty = int(rl.get('qty') or 0)
        if sid and qty > 0 and sid in orig_map:
            unit_price = float(orig_map[sid]['unit_price'] or 0)
            db.execute(
                'INSERT INTO return_items(return_id, sale_item_id, qty, unit_price) VALUES (?,?,?,?)',
                (return_id, sid, qty, unit_price)
            )
            # !!! DÜZELTME: sqlite3.Row için .get yerine ['inventory_item_id'] kullan
            inv_id = orig_map[sid]['inventory_item_id']
            if inv_id is not None:
                try:
                    spost(
                        ENV_STORE, ENV_TOKEN,
                        'inventory_levels/adjust.json',
                        {
                            'inventory_item_id': int(inv_id),
                            'location_id': int(ENV_LOCATION or 0),
                            'available_adjustment': int(qty)  # iade: stok +
                        }
                    )
                except Exception:
                    pass
    db.commit()

    # 6) Değişim ürünleri için stok düş
    for it in exchange_cart:
        try:
            spost(
                ENV_STORE, ENV_TOKEN,
                'inventory_levels/adjust.json',
                {
                    'inventory_item_id': int(it['inventory_item_id']),
                    'location_id': int(ENV_LOCATION or 0),
                    'available_adjustment': -int(it.get('qty') or 1)  # değişim: stok -
                }
            )
        except Exception:
            pass

    return jsonify({
        'ok': True,
        'return_id': return_id,
        'summary': {
            'return_subtotal': round(return_subtotal, 2),
            'exchange_subtotal': round(exchange_subtotal, 2),
            'tax': tax,
            'total': total,
            'net': net,
            'refund': refund,
            'additional_charge': additional_charge
        }
    })

# ----------------------- Root & Errors -----------------------
@app.route('/')
def root():
    return redirect(url_for('customer'))

@app.errorhandler(404)
def nf(e):
    return '<div style="font-family:system-ui,Arial;padding:24px"><h3>Sayfa bulunamadı</h3><p>Müşteri: <a href="/customer">/customer</a> | POS: <a href="/pos">/pos</a> | Stok: <a href="/inventory">/inventory</a></p></div>', 404
@app.route('/customer/<int:customer_id>/history')
@login_required
def customer_history(customer_id):
    db = get_db()

    # Müşteri bilgisi
    customer = db.execute("""
        SELECT * FROM customers WHERE id = ?
    """, (customer_id,)).fetchone()

    if not customer:
        flash("Müşteri bulunamadı.", "danger")
        return redirect(url_for('customers'))

    # Müşterinin satışları
    sales = db.execute("""
        SELECT * FROM sales
        WHERE customer_id = ?
        ORDER BY sale_date DESC
    """, (customer_id,)).fetchall()

    # Müşterinin iadeleri
    returns = db.execute("""
        SELECT * FROM returns
        WHERE customer_id = ?
        ORDER BY return_date DESC
    """, (customer_id,)).fetchall()

    # Müşterinin toplam açık veresiye borcu
    open_credit = db.execute("""
        SELECT SUM(total_amount) 
        FROM sales 
        WHERE customer_id = ? 
          AND payment_type = 'Veresiye' 
          AND is_paid = 0
    """, (customer_id,)).fetchone()[0] or 0

    return render_template(
        "customer_history.html",
        customer=customer,
        sales=sales,
        returns=returns,
        open_credit=open_credit
    )

@app.route('/customer/<int:cid>/panel')
@login_required
def customer_panel(cid):
    # Paneli history sayfasına yönlendirme
    return redirect(url_for('customer_history', customer_id=cid))



# ----------------------- Main -----------------------
if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5010))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
@app.route('/customer/<int:cid>/panel')
@login_required
def customer_panel(cid):
    db = get_db()
    cust = db.execute("SELECT id, name, phone, email, created_at FROM customers WHERE id=?", (cid,)).fetchone()
    if not cust:
        flash('Müşteri bulunamadı.', 'error')
        return redirect(url_for('customers_list'))

    # SON 12 AY
    sales = db.execute("""
      SELECT id, ts, total, payment_method
      FROM sales
      WHERE customer_id=? AND ts >= datetime('now','localtime','-12 months')
      ORDER BY id DESC
    """, (cid,)).fetchall()

    sale_items = db.execute("""
      SELECT si.sale_id, si.title, si.qty, si.unit_price
      FROM sale_items si
      JOIN sales s ON s.id = si.sale_id
      WHERE s.customer_id=? AND s.ts >= datetime('now','localtime','-12 months')
      ORDER BY si.id DESC
    """, (cid,)).fetchall()

    returns = db.execute("""
      SELECT r.id, r.ts, r.sale_id, r.refund, r.additional_charge, r.net, r.payment_method, r.notes
      FROM returns r
      JOIN sales s ON s.id = r.sale_id
      WHERE s.customer_id=? AND r.ts >= datetime('now','localtime','-12 months')
      ORDER BY r.id DESC
    """, (cid,)).fetchall()

    veresiyeler = db.execute("""
      SELECT id, ts, total
      FROM sales
      WHERE customer_id=? 
        AND ts >= datetime('now','localtime','-12 months') 
        AND LOWER(COALESCE(payment_method,''))='veresiye'
      ORDER BY id DESC
    """, (cid,)).fetchall()

    summary_sales = db.execute("""
      SELECT COUNT(*) AS n_sales, COALESCE(SUM(total),0) AS total_sum
      FROM sales
      WHERE customer_id=? AND ts >= datetime('now','localtime','-12 months')
    """, (cid,)).fetchone()

    summary_returns = db.execute("""
      SELECT
        COALESCE(SUM(refund),0)            AS refund_sum,
        COALESCE(SUM(additional_charge),0) AS additional_sum,
        COALESCE(SUM(net),0)               AS net_sum
      FROM returns r
      JOIN sales s ON s.id = r.sale_id
      WHERE s.customer_id=? AND r.ts >= datetime('now','localtime','-12 months')
    """, (cid,)).fetchone()

    summary_veresiye = db.execute("""
      SELECT COALESCE(SUM(total),0) AS veresiye_sum, COUNT(*) AS veresiye_count
      FROM sales
      WHERE customer_id=? 
        AND ts >= datetime('now','localtime','-12 months') 
        AND LOWER(COALESCE(payment_method,''))='veresiye'
    """, (cid,)).fetchone()

    return render_template(
        'customer_panel.html',
        customer=cust,
        sales=sales,
        sale_items=sale_items,
        returns=returns,
        veresiyeler=veresiyeler,
        summary_sales=summary_sales,
        summary_returns=summary_returns,
        summary_veresiye=summary_veresiye
    )
