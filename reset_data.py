"""
Voer dit eenmalig uit om alle testdata te verwijderen.
Behoudt instellingen, kapsels en tijdsloten.
Run: python reset_data.py
"""
import sqlite3, os

DATA_DIR = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
DATABASE = os.path.join(DATA_DIR, 'barber.db')

db = sqlite3.connect(DATABASE)
db.execute("DELETE FROM afspraken")
db.execute("DELETE FROM feedback")
db.execute("DELETE FROM geblokkeerde_dagen")
db.commit()
db.close()
print("Klaar — alle afspraken, feedback en geblokkeerde dagen verwijderd.")
print("Instellingen, kapsels en tijdsloten zijn bewaard.")
