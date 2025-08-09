import sqlite3
db = sqlite3.connect('panel.db')
db.execute("UPDATE auth SET admin_password=NULL WHERE id=1")
db.commit(); db.close()
print('Admin şifresi sıfırlandı. Uygulamayı yeniden başlatın ve yeni şifre belirleyin.')
