import sqlite3
import os

# app.py ile aynı klasördeki database.db dosyasını bul
db_path = os.path.join(os.path.dirname(__file__), "database.db")

if not os.path.exists(db_path):
    print("❌ database.db bulunamadı. Bu dosyayı app.py ile aynı klasöre koy.")
else:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # sale_date kolonu ekle
        cursor.execute("ALTER TABLE sales ADD COLUMN sale_date TEXT")
        conn.commit()
        print("✅ 'sale_date' kolonu başarıyla eklendi.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e).lower():
            print("ℹ️ 'sale_date' kolonu zaten mevcut.")
        else:
            print(f"❌ Hata: {e}")
    finally:
        conn.close()
