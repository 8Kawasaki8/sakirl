"""
Kapper Reservatiesysteem v2
Run:  python Reservatie.py
Deps: pip install flask apscheduler werkzeug
"""
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
import sqlite3, os, secrets, smtplib, atexit, json, re
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.security import generate_password_hash, check_password_hash

# ── App ────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'barber.db')

# ── DB ─────────────────────────────────────────────────────────────────────────
def get_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS afspraken (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            naam                  TEXT    NOT NULL,
            telefoon              TEXT    NOT NULL,
            email                 TEXT,
            kapsel_id             INTEGER,
            kapsel_naam           TEXT    NOT NULL,
            kapsel_duur           INTEGER NOT NULL DEFAULT 30,
            aangepast_kapsel_naam TEXT,
            datum                 TEXT    NOT NULL,
            tijdslot              TEXT    NOT NULL,
            status                TEXT    DEFAULT 'gepland',
            feedback_token        TEXT,
            herinnering_verstuurd INTEGER DEFAULT 0,
            aangemaakt_op         TEXT    DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS kapsel_types (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            naam         TEXT    NOT NULL,
            duur         INTEGER NOT NULL DEFAULT 30,
            actief       INTEGER DEFAULT 1,
            aangemaakt_op TEXT   DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tijdsloten (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            tijdslot TEXT    NOT NULL UNIQUE,
            actief   INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS geblokkeerde_dagen (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            datum TEXT    NOT NULL UNIQUE,
            reden TEXT
        );

        CREATE TABLE IF NOT EXISTS feedback (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            afspraak_id   INTEGER NOT NULL,
            beoordeling   INTEGER,
            bericht       TEXT,
            fooi_bedrag   REAL    DEFAULT 0,
            aangemaakt_op TEXT    DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (afspraak_id) REFERENCES afspraken(id)
        );

        CREATE TABLE IF NOT EXISTS instellingen (
            sleutel TEXT PRIMARY KEY,
            waarde  TEXT
        );
    """)

    # Default settings
    defaults = [
        ('kapper_naam',      'Mijn Kapper'),
        ('smtp_email',       ''),
        ('smtp_wachtwoord',  ''),
        ('wachtwoord_hash',  generate_password_hash('barber2024')),
    ]
    for k, v in defaults:
        db.execute("INSERT OR IGNORE INTO instellingen VALUES (?, ?)", (k, v))

    # Default kapsel types (only if table is empty)
    count = db.execute("SELECT COUNT(*) FROM kapsel_types").fetchone()[0]
    if count == 0:
        for naam, duur in [
            ('Taper', 30), ('Taper + Baard', 45),
            ('Degrade', 30), ('Degrade + Baard', 45),
        ]:
            db.execute("INSERT INTO kapsel_types (naam, duur) VALUES (?, ?)", (naam, duur))

    db.commit()
    db.close()

# ── Helpers ────────────────────────────────────────────────────────────────────
def get_instelling(k):
    db = get_db()
    r = db.execute("SELECT waarde FROM instellingen WHERE sleutel=?", (k,)).fetchone()
    db.close()
    return r['waarde'] if r else None

def set_instelling(k, v):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO instellingen VALUES (?, ?)", (k, v))
    db.commit()
    db.close()

def slot_min(s):
    h, m = s.split(':')
    return int(h) * 60 + int(m)

def get_kapsel_types():
    db = get_db()
    rows = db.execute("SELECT * FROM kapsel_types WHERE actief=1 ORDER BY id").fetchall()
    db.close()
    return rows

def get_tijdsloten():
    db = get_db()
    rows = db.execute("SELECT tijdslot FROM tijdsloten WHERE actief=1 ORDER BY tijdslot").fetchall()
    db.close()
    return [r['tijdslot'] for r in rows]

def beschikbare_slots(datum_str, duur):
    db = get_db()
    if db.execute("SELECT id FROM geblokkeerde_dagen WHERE datum=?", (datum_str,)).fetchone():
        db.close()
        return []
    afspraken = db.execute(
        "SELECT tijdslot, kapsel_duur FROM afspraken WHERE datum=? AND status!='geannuleerd'",
        (datum_str,)
    ).fetchall()
    slots_db = db.execute("SELECT tijdslot FROM tijdsloten WHERE actief=1 ORDER BY tijdslot").fetchall()
    db.close()

    alle_slots = [r['tijdslot'] for r in slots_db]
    vrij = []
    for slot in alle_slots:
        start = slot_min(slot)
        einde = start + duur
        conflict = any(
            start < slot_min(a['tijdslot']) + a['kapsel_duur'] and
            einde > slot_min(a['tijdslot'])
            for a in afspraken
        )
        if not conflict:
            vrij.append(slot)
    return vrij

def smtp_voor_email(email):
    domain = email.split('@')[-1].lower() if '@' in email else ''
    tabel = {
        'gmail.com': ('smtp.gmail.com', 587),
        'googlemail.com': ('smtp.gmail.com', 587),
        'outlook.com': ('smtp-mail.outlook.com', 587),
        'hotmail.com': ('smtp-mail.outlook.com', 587),
        'live.com': ('smtp-mail.outlook.com', 587),
        'yahoo.com': ('smtp.mail.yahoo.com', 587),
        'icloud.com': ('smtp.mail.me.com', 587),
    }
    return tabel.get(domain, ('smtp.' + domain, 587))

def stuur_email(aan, onderwerp, html_body, tekst_body=''):
    van      = get_instelling('smtp_email')
    wacht    = get_instelling('smtp_wachtwoord')
    if not van or not wacht:
        return False
    server, poort = smtp_voor_email(van)
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = onderwerp
        msg['From'] = van
        msg['To']   = aan
        if tekst_body:
            msg.attach(MIMEText(tekst_body, 'plain'))
        msg.attach(MIMEText(html_body, 'html'))
        with smtplib.SMTP(server, poort) as s:
            s.starttls()
            s.login(van, wacht)
            s.sendmail(van, aan, msg.as_string())
        return True
    except Exception as e:
        print(f"[E-mail fout] {e}")
        return False

def mail_herinnering(naam, email, kapsel, datum, tijdslot, kapper_naam):
    d = datetime.strptime(datum, '%Y-%m-%d')
    dag_nl = ['maandag','dinsdag','woensdag','donderdag','vrijdag','zaterdag','zondag']
    mnd_nl = ['januari','februari','maart','april','mei','juni','juli',
               'augustus','september','oktober','november','december']
    datum_nl = f"{dag_nl[d.weekday()]} {d.day} {mnd_nl[d.month-1]} {d.year}"
    html = f"""
<div style="font-family:Georgia,serif;background:#faf8f5;padding:32px;">
  <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;
              padding:32px;border-top:4px solid #c9a84c;box-shadow:0 4px 20px rgba(0,0,0,0.06);">
    <h2 style="color:#c9a84c;margin-bottom:4px;font-size:1.3rem;">✂ Herinnering Afspraak</h2>
    <p style="color:#78716c;">Hallo <strong style="color:#292524;">{naam}</strong>,</p>
    <p style="color:#57534e;">Vergeet je afspraak van morgen niet!</p>
    <div style="background:#faf8f5;border-radius:8px;padding:18px;margin:18px 0;border-left:3px solid #c9a84c;">
      <p style="margin:5px 0;color:#292524;"><strong>✂ Kapsel:</strong> {kapsel}</p>
      <p style="margin:5px 0;color:#292524;"><strong>📅 Datum:</strong> {datum_nl}</p>
      <p style="margin:5px 0;color:#292524;"><strong>🕐 Tijd:</strong> {tijdslot}</p>
    </div>
    <p style="color:#78716c;font-size:0.9rem;">Tot morgen! — {kapper_naam}</p>
  </div>
</div>"""
    return stuur_email(email, f"Herinnering: morgen afspraak bij {kapper_naam}", html)

def mail_feedback(naam, email, feedback_link, kapper_naam):
    html = f"""
<div style="font-family:Georgia,serif;background:#faf8f5;padding:32px;">
  <div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;
              padding:32px;border-top:4px solid #c9a84c;box-shadow:0 4px 20px rgba(0,0,0,0.06);">
    <h2 style="color:#c9a84c;margin-bottom:4px;">Bedankt, {naam}!</h2>
    <p style="color:#57534e;">Was je tevreden met je kapsel? Laat het ons weten!</p>
    <a href="{feedback_link}" style="display:inline-block;margin:18px 0;background:#c9a84c;
       color:#fff;padding:13px 26px;border-radius:8px;text-decoration:none;font-weight:bold;">
      ⭐ Geef je beoordeling
    </a>
    <p style="color:#78716c;font-size:0.85rem;">Tot de volgende keer! — {kapper_naam}</p>
  </div>
</div>"""
    return stuur_email(email, f"Bedankt voor je bezoek bij {kapper_naam}!", html)

def dagelijkse_herinneringen():
    morgen = (date.today() + timedelta(days=1)).strftime('%Y-%m-%d')
    kapper_naam = get_instelling('kapper_naam') or 'Uw Kapper'
    db = get_db()
    rows = db.execute(
        "SELECT * FROM afspraken WHERE datum=? AND email!='' AND email IS NOT NULL "
        "AND herinnering_verstuurd=0 AND status='gepland'", (morgen,)
    ).fetchall()
    for a in rows:
        ok = mail_herinnering(a['naam'], a['email'], a['kapsel_naam'], a['datum'], a['tijdslot'], kapper_naam)
        if ok:
            db.execute("UPDATE afspraken SET herinnering_verstuurd=1 WHERE id=?", (a['id'],))
    db.commit()
    db.close()

def eigenaar_vereist(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('eigenaar_ingelogd'):
            return redirect(url_for('eigenaar_login'))
        return f(*args, **kwargs)
    return wrapper

def datum_nl_format(datum_str):
    d = datetime.strptime(datum_str, '%Y-%m-%d')
    dag = ['maandag','dinsdag','woensdag','donderdag','vrijdag','zaterdag','zondag']
    mnd = ['januari','februari','maart','april','mei','juni','juli',
           'augustus','september','oktober','november','december']
    return f"{dag[d.weekday()]} {d.day} {mnd[d.month-1]} {d.year}"

# ── Client routes ───────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html', kapper_naam=get_instelling('kapper_naam') or 'Uw Kapper')

@app.route('/boeken')
def boeken():
    kapsels = get_kapsel_types()
    return render_template('boeken.html', kapsels=kapsels,
                           kapper_naam=get_instelling('kapper_naam') or 'Uw Kapper')

@app.route('/api/slots')
def api_slots():
    datum  = request.args.get('datum', '')
    kid    = request.args.get('kapsel_id', '')
    duur   = int(request.args.get('duur', 30))
    if not datum:
        return jsonify({'slots': []})
    try:
        if date.fromisoformat(datum) < date.today():
            return jsonify({'slots': []})
    except ValueError:
        return jsonify({'slots': []})
    return jsonify({'slots': beschikbare_slots(datum, duur)})

@app.route('/api/geblokkeerde-dagen')
def api_geblokkeerde_dagen():
    db = get_db()
    rows = db.execute("SELECT datum FROM geblokkeerde_dagen").fetchall()
    db.close()
    return jsonify({'geblokkeerd': [r['datum'] for r in rows]})

@app.route('/api/feedback-status')
def api_feedback_status():
    token = request.args.get('token', '')
    if not token:
        return jsonify({'beschikbaar': False, 'al_gegeven': False})
    db = get_db()
    a = db.execute("SELECT id, status FROM afspraken WHERE feedback_token=?", (token,)).fetchone()
    if not a:
        db.close()
        return jsonify({'beschikbaar': False, 'al_gegeven': False})
    al_gegeven = db.execute("SELECT id FROM feedback WHERE afspraak_id=?", (a['id'],)).fetchone()
    db.close()
    return jsonify({
        'beschikbaar': a['status'] == 'gedaan' and not al_gegeven,
        'al_gegeven':  bool(al_gegeven)
    })

@app.route('/api/bezette-dagen')
def api_bezette_dagen():
    maand = request.args.get('maand', '')
    if not maand:
        return jsonify({'bezet': []})
    db = get_db()
    rows = db.execute(
        "SELECT DISTINCT datum FROM afspraken WHERE datum LIKE ? AND status!='geannuleerd'",
        (maand + '%',)
    ).fetchall()
    db.close()
    return jsonify({'bezet': [r['datum'] for r in rows]})

@app.route('/api/alle-slots')
def api_alle_slots():
    datum = request.args.get('datum', '')
    duur  = int(request.args.get('duur', 30))
    if not datum:
        return jsonify({'slots': []})
    db = get_db()
    afspraken_dag = db.execute(
        "SELECT tijdslot, kapsel_duur FROM afspraken WHERE datum=? AND status!='geannuleerd'",
        (datum,)
    ).fetchall()
    alle = db.execute("SELECT tijdslot FROM tijdsloten WHERE actief=1 ORDER BY tijdslot").fetchall()
    db.close()
    result = []
    for s in alle:
        start  = slot_min(s['tijdslot'])
        einde  = start + duur
        bezet  = any(
            start < slot_min(a['tijdslot']) + a['kapsel_duur'] and einde > slot_min(a['tijdslot'])
            for a in afspraken_dag
        )
        result.append({'tijdslot': s['tijdslot'], 'bezet': bezet})
    return jsonify({'slots': result})

@app.route('/api/kapsels')
def api_kapsels():
    kapsels = get_kapsel_types()
    return jsonify({'kapsels': [{'id': k['id'], 'naam': k['naam'], 'duur': k['duur']} for k in kapsels]})

@app.route('/boeken', methods=['POST'])
def boeken_post():
    naam       = request.form.get('naam', '').strip()
    telefoon   = request.form.get('telefoon', '').strip()
    email      = request.form.get('email', '').strip() or None
    kapsel_id  = request.form.get('kapsel_id', '')
    datum      = request.form.get('datum', '')
    tijdslot   = request.form.get('tijdslot', '')
    aangepast  = request.form.get('aangepast_naam', '').strip()

    if not all([naam, telefoon, datum, tijdslot]):
        flash('Vul alle verplichte velden in.', 'fout')
        return redirect(url_for('boeken'))

    # Custom kapsel
    if kapsel_id == 'aangepast':
        if not aangepast:
            flash('Vul de naam van je gewenste kapsel in.', 'fout')
            return redirect(url_for('boeken'))
        kapsel_naam = aangepast
        kapsel_duur = 30  # default; barber discusses in person
        kapsel_id_val = None
    else:
        db = get_db()
        k = db.execute("SELECT * FROM kapsel_types WHERE id=? AND actief=1", (kapsel_id,)).fetchone()
        db.close()
        if not k:
            flash('Ongeldig kapsel gekozen.', 'fout')
            return redirect(url_for('boeken'))
        kapsel_naam  = k['naam']
        kapsel_duur  = k['duur']
        kapsel_id_val = k['id']

    vrij = beschikbare_slots(datum, kapsel_duur)
    if tijdslot not in vrij:
        flash('Dit tijdslot is niet meer beschikbaar. Kies een ander tijdslot.', 'fout')
        return redirect(url_for('boeken'))

    token = secrets.token_urlsafe(16)
    db = get_db()
    cur = db.execute(
        """INSERT INTO afspraken
           (naam, telefoon, email, kapsel_id, kapsel_naam, kapsel_duur, aangepast_kapsel_naam,
            datum, tijdslot, feedback_token)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (naam, telefoon, email, kapsel_id_val, kapsel_naam, kapsel_duur,
         aangepast if kapsel_id == 'aangepast' else None,
         datum, tijdslot, token)
    )
    db.commit()
    db.close()
    return redirect(url_for('bevestiging', token=token))

@app.route('/bevestiging/<token>')
def bevestiging(token):
    db = get_db()
    a = db.execute("SELECT * FROM afspraken WHERE feedback_token=?", (token,)).fetchone()
    db.close()
    if not a:
        return redirect(url_for('index'))
    datum_nl = datum_nl_format(a['datum'])
    return render_template('bevestiging.html', afspraak=a, datum_nl=datum_nl,
                           kapper_naam=get_instelling('kapper_naam') or 'Uw Kapper')

@app.route('/feedback/<token>', methods=['GET', 'POST'])
def feedback(token):
    db = get_db()
    a = db.execute("SELECT * FROM afspraken WHERE feedback_token=?", (token,)).fetchone()
    if not a:
        db.close()
        return redirect(url_for('index'))
    bestaand = db.execute("SELECT id FROM feedback WHERE afspraak_id=?", (a['id'],)).fetchone()
    if request.method == 'POST' and not bestaand:
        beoordeling = int(request.form.get('beoordeling', 0))
        bericht     = request.form.get('bericht', '').strip()
        try:
            fooi = float(request.form.get('fooi', '0').replace(',', '.'))
        except ValueError:
            fooi = 0.0
        db.execute("INSERT INTO feedback (afspraak_id, beoordeling, bericht, fooi_bedrag) VALUES (?,?,?,?)",
                   (a['id'], beoordeling, bericht, fooi))
        db.commit()
        db.close()
        return redirect(url_for('feedback_bedankt'))
    db.close()
    return render_template('feedback.html', afspraak=a, al_gegeven=bool(bestaand),
                           kapper_naam=get_instelling('kapper_naam') or 'Uw Kapper')

@app.route('/feedback/bedankt')
def feedback_bedankt():
    return render_template('feedback_bedankt.html', kapper_naam=get_instelling('kapper_naam') or 'Uw Kapper')

@app.route('/mijn-afspraken', methods=['GET', 'POST'])
def mijn_afspraken():
    afspraken = None
    telefoon  = ''
    if request.method == 'POST':
        telefoon = request.form.get('telefoon', '').strip()
        # Normalize: keep only digits
        tel_clean = re.sub(r'\D', '', telefoon)
        if tel_clean:
            db = get_db()
            # Match by stripping non-digits in DB too
            rows = db.execute(
                "SELECT * FROM afspraken WHERE replace(replace(replace(telefoon,' ',''),'-',''),'/','')=? "
                "ORDER BY datum DESC, tijdslot DESC", (tel_clean,)
            ).fetchall()
            db.close()
            afspraken = []
            vandaag = date.today().strftime('%Y-%m-%d')
            for r in rows:
                fb = None
                dbf = get_db()
                fb = dbf.execute("SELECT beoordeling, fooi_bedrag FROM feedback WHERE afspraak_id=?", (r['id'],)).fetchone()
                dbf.close()
                afspraken.append({
                    'r': r,
                    'datum_nl': datum_nl_format(r['datum']),
                    'verleden': r['datum'] < vandaag,
                    'feedback': fb
                })
    return render_template('mijn_afspraken.html',
                           afspraken=afspraken, telefoon=telefoon,
                           kapper_naam=get_instelling('kapper_naam') or 'Uw Kapper')

# ── Owner routes ────────────────────────────────────────────────────────────────
@app.route('/eigenaar/login', methods=['GET', 'POST'])
def eigenaar_login():
    if session.get('eigenaar_ingelogd'):
        return redirect(url_for('eigenaar_dashboard'))
    fout = None
    if request.method == 'POST':
        h = get_instelling('wachtwoord_hash')
        if h and check_password_hash(h, request.form.get('wachtwoord', '')):
            session['eigenaar_ingelogd'] = True
            return redirect(url_for('eigenaar_dashboard'))
        fout = 'Fout wachtwoord. Probeer opnieuw.'
    return render_template('eigenaar/login.html', fout=fout,
                           kapper_naam=get_instelling('kapper_naam') or 'Mijn Kapper')

@app.route('/eigenaar/uitloggen')
def eigenaar_uitloggen():
    session.pop('eigenaar_ingelogd', None)
    return redirect(url_for('eigenaar_login'))

@app.route('/eigenaar/')
@app.route('/eigenaar/dashboard')
@eigenaar_vereist
def eigenaar_dashboard():
    vandaag    = date.today().strftime('%Y-%m-%d')
    morgen     = (date.today() + timedelta(days=1)).strftime('%Y-%m-%d')
    db = get_db()
    vandaag_af = db.execute(
        "SELECT * FROM afspraken WHERE datum=? AND status!='geannuleerd' ORDER BY tijdslot", (vandaag,)
    ).fetchall()
    aankomend = db.execute(
        "SELECT * FROM afspraken WHERE datum>=? AND status='gepland' ORDER BY datum, tijdslot LIMIT 20", (morgen,)
    ).fetchall()
    geschiedenis = db.execute(
        "SELECT a.*, f.beoordeling, f.fooi_bedrag FROM afspraken a "
        "LEFT JOIN feedback f ON f.afspraak_id=a.id "
        "WHERE a.datum<? ORDER BY a.datum DESC, a.tijdslot DESC LIMIT 40", (vandaag,)
    ).fetchall()
    totaal_vandaag = len(vandaag_af)
    totaal_week    = db.execute(
        "SELECT COUNT(*) FROM afspraken WHERE datum>=? AND datum<=? AND status!='geannuleerd'",
        (vandaag, (date.today()+timedelta(days=7)).strftime('%Y-%m-%d'))
    ).fetchone()[0]
    totaal_fooi = db.execute("SELECT COALESCE(SUM(fooi_bedrag),0) FROM feedback").fetchone()[0]
    db.close()
    return render_template('eigenaar/dashboard.html',
        vandaag_af=vandaag_af, aankomend=aankomend, geschiedenis=geschiedenis,
        totaal_vandaag=totaal_vandaag, totaal_week=totaal_week, totaal_fooi=totaal_fooi,
        kapper_naam=get_instelling('kapper_naam') or 'Mijn Kapper',
        vandaag=vandaag
    )

@app.route('/eigenaar/afspraken')
@eigenaar_vereist
def eigenaar_afspraken():
    fd = request.args.get('datum', '')
    fs = request.args.get('status', '')
    q  = "SELECT a.*, f.beoordeling, f.fooi_bedrag, f.bericht FROM afspraken a LEFT JOIN feedback f ON f.afspraak_id=a.id WHERE 1=1"
    p  = []
    if fd: q += " AND a.datum=?"; p.append(fd)
    if fs: q += " AND a.status=?"; p.append(fs)
    q += " ORDER BY a.datum DESC, a.tijdslot DESC"
    db = get_db()
    afspraken = db.execute(q, p).fetchall()
    db.close()
    return render_template('eigenaar/afspraken.html', afspraken=afspraken,
                           filter_datum=fd, filter_status=fs,
                           kapper_naam=get_instelling('kapper_naam') or 'Mijn Kapper')

@app.route('/eigenaar/afspraken/<int:aid>/status', methods=['POST'])
@eigenaar_vereist
def eigenaar_status(aid):
    status = request.form.get('status', '')
    if status not in ('gepland', 'gedaan', 'geannuleerd'):
        return jsonify({'ok': False}), 400
    db = get_db()
    a = db.execute("SELECT * FROM afspraken WHERE id=?", (aid,)).fetchone()
    if a:
        db.execute("UPDATE afspraken SET status=? WHERE id=?", (status, aid))
        db.commit()
        if status == 'gedaan' and a['email']:
            link = request.host_url + 'feedback/' + a['feedback_token']
            mail_feedback(a['naam'], a['email'], link, get_instelling('kapper_naam') or 'Uw Kapper')
    db.close()
    return jsonify({'ok': True})

@app.route('/eigenaar/afspraken/<int:aid>/verwijder', methods=['POST'])
@eigenaar_vereist
def eigenaar_verwijder(aid):
    db = get_db()
    db.execute("DELETE FROM feedback WHERE afspraak_id=?", (aid,))
    db.execute("DELETE FROM afspraken WHERE id=?", (aid,))
    db.commit()
    db.close()
    return redirect(url_for('eigenaar_afspraken'))

@app.route('/eigenaar/kalender')
@eigenaar_vereist
def eigenaar_kalender():
    db = get_db()
    geblokkeerd = db.execute("SELECT * FROM geblokkeerde_dagen ORDER BY datum").fetchall()
    vandaag = date.today()
    tot     = (vandaag + timedelta(days=60)).strftime('%Y-%m-%d')
    counts  = {r['datum']: r['n'] for r in db.execute(
        "SELECT datum, COUNT(*) as n FROM afspraken WHERE datum>=? AND datum<=? AND status!='geannuleerd' GROUP BY datum",
        (vandaag.strftime('%Y-%m-%d'), tot)
    ).fetchall()}
    db.close()
    return render_template('eigenaar/kalender.html',
        geblokkeerde_dagen=[r['datum'] for r in geblokkeerd],
        geblokkeerd_details={r['datum']: r['reden'] for r in geblokkeerd},
        afspraak_counts=counts,
        kapper_naam=get_instelling('kapper_naam') or 'Mijn Kapper'
    )

@app.route('/eigenaar/kalender/blokkeer', methods=['POST'])
@eigenaar_vereist
def eigenaar_blokkeer():
    datum = request.form.get('datum', '')
    reden = request.form.get('reden', '').strip()
    if not datum: return jsonify({'ok': False}), 400
    db = get_db()
    db.execute("INSERT OR IGNORE INTO geblokkeerde_dagen (datum, reden) VALUES (?,?)", (datum, reden))
    db.commit(); db.close()
    return jsonify({'ok': True})

@app.route('/eigenaar/kalender/deblokkeer', methods=['POST'])
@eigenaar_vereist
def eigenaar_deblokkeer():
    datum = request.form.get('datum', '')
    db = get_db()
    db.execute("DELETE FROM geblokkeerde_dagen WHERE datum=?", (datum,))
    db.commit(); db.close()
    return jsonify({'ok': True})

@app.route('/eigenaar/feedback')
@eigenaar_vereist
def eigenaar_feedback():
    db = get_db()
    lijst = db.execute(
        "SELECT f.*, a.naam, a.kapsel_naam, a.datum, a.tijdslot "
        "FROM feedback f JOIN afspraken a ON a.id=f.afspraak_id ORDER BY f.aangemaakt_op DESC"
    ).fetchall()
    gem     = db.execute("SELECT AVG(beoordeling) FROM feedback WHERE beoordeling>0").fetchone()[0]
    fooi    = db.execute("SELECT COALESCE(SUM(fooi_bedrag),0) FROM feedback").fetchone()[0]
    db.close()
    return render_template('eigenaar/feedback.html', feedback_lijst=lijst,
                           gemiddeld=round(gem, 1) if gem else 0, totaal_fooi=fooi,
                           kapper_naam=get_instelling('kapper_naam') or 'Mijn Kapper')

# ── Kapsel management ──────────────────────────────────────────────────────────
@app.route('/eigenaar/kapsels/add', methods=['POST'])
@eigenaar_vereist
def eigenaar_kapsel_add():
    naam = request.form.get('naam', '').strip()
    duur = request.form.get('duur', '30')
    if not naam: return jsonify({'ok': False, 'fout': 'Naam vereist'}), 400
    try: duur = max(5, int(duur))
    except: duur = 30
    db = get_db()
    cur = db.execute("INSERT INTO kapsel_types (naam, duur) VALUES (?,?)", (naam, duur))
    db.commit()
    kid = cur.lastrowid
    db.close()
    return jsonify({'ok': True, 'id': kid, 'naam': naam, 'duur': duur})

@app.route('/eigenaar/kapsels/<int:kid>/verwijder', methods=['POST'])
@eigenaar_vereist
def eigenaar_kapsel_delete(kid):
    db = get_db()
    db.execute("UPDATE kapsel_types SET actief=0 WHERE id=?", (kid,))
    db.commit(); db.close()
    return jsonify({'ok': True})

@app.route('/eigenaar/kapsels/<int:kid>/bewerk', methods=['POST'])
@eigenaar_vereist
def eigenaar_kapsel_bewerk(kid):
    naam = request.form.get('naam', '').strip()
    duur = request.form.get('duur', '30')
    try: duur = max(5, int(duur))
    except: duur = 30
    if not naam: return jsonify({'ok': False}), 400
    db = get_db()
    db.execute("UPDATE kapsel_types SET naam=?, duur=? WHERE id=?", (naam, duur, kid))
    db.commit(); db.close()
    return jsonify({'ok': True})

# ── Tijdslot management ─────────────────────────────────────────────────────────
@app.route('/eigenaar/tijdsloten/add', methods=['POST'])
@eigenaar_vereist
def eigenaar_slot_add():
    slot = request.form.get('tijdslot', '').strip()
    if not re.match(r'^\d{1,2}:\d{2}$', slot):
        return jsonify({'ok': False, 'fout': 'Ongeldig formaat (gebruik UU:MM)'}), 400
    # Normalize to HH:MM
    h, m = slot.split(':')
    slot = f"{int(h):02d}:{m}"
    db = get_db()
    try:
        db.execute("INSERT INTO tijdsloten (tijdslot) VALUES (?)", (slot,))
        db.commit()
        ok = True
    except:
        ok = False
    db.close()
    return jsonify({'ok': ok, 'tijdslot': slot})

@app.route('/eigenaar/tijdsloten/<int:sid>/verwijder', methods=['POST'])
@eigenaar_vereist
def eigenaar_slot_delete(sid):
    db = get_db()
    db.execute("DELETE FROM tijdsloten WHERE id=?", (sid,))
    db.commit(); db.close()
    return jsonify({'ok': True})

# ── Instellingen ────────────────────────────────────────────────────────────────
@app.route('/eigenaar/instellingen', methods=['GET', 'POST'])
@eigenaar_vereist
def eigenaar_instellingen():
    bericht = None
    if request.method == 'POST':
        actie = request.form.get('actie', '')
        if actie == 'algemeen':
            set_instelling('kapper_naam', request.form.get('kapper_naam', '').strip())
            bericht = ('succes', 'Naam opgeslagen.')
        elif actie == 'email':
            email = request.form.get('smtp_email', '').strip()
            wacht = request.form.get('smtp_wachtwoord', '').strip()
            set_instelling('smtp_email', email)
            if wacht:
                set_instelling('smtp_wachtwoord', wacht)
            bericht = ('succes', 'E-mailinstellingen opgeslagen.')

    db = get_db()
    kapsels   = db.execute("SELECT * FROM kapsel_types WHERE actief=1 ORDER BY id").fetchall()
    tijdsloten = db.execute("SELECT * FROM tijdsloten ORDER BY tijdslot").fetchall()
    db.close()
    return render_template('eigenaar/instellingen.html',
        kapper_naam=get_instelling('kapper_naam') or 'Mijn Kapper',
        smtp_email=get_instelling('smtp_email') or '',
        kapsels=kapsels, tijdsloten=tijdsloten, bericht=bericht
    )

# ── Beheer (kapsels + tijdsloten) ──────────────────────────────────────────────
@app.route('/eigenaar/beheer')
@eigenaar_vereist
def eigenaar_beheer():
    db = get_db()
    kapsels    = db.execute("SELECT * FROM kapsel_types WHERE actief=1 ORDER BY id").fetchall()
    tijdsloten = db.execute("SELECT * FROM tijdsloten ORDER BY tijdslot").fetchall()
    db.close()
    return render_template('eigenaar/beheer.html',
        kapsels=kapsels, tijdsloten=tijdsloten,
        kapper_naam=get_instelling('kapper_naam') or 'Mijn Kapper'
    )

# ── Test e-mail ─────────────────────────────────────────────────────────────────
@app.route('/eigenaar/test-email', methods=['POST'])
@eigenaar_vereist
def eigenaar_test_email():
    email = get_instelling('smtp_email')
    if not email:
        return jsonify({'ok': False, 'fout': 'Geen e-mailadres ingesteld.'})
    kapper_naam = get_instelling('kapper_naam') or 'Uw Kapper'
    html = f"""
<div style="font-family:sans-serif;background:#141414;padding:32px;">
  <div style="max-width:480px;margin:0 auto;background:#1e1e1e;border-radius:12px;
              padding:32px;border-top:4px solid #c9a84c;">
    <h2 style="color:#c9a84c;">✂ Test e-mail</h2>
    <p style="color:#ccc;">Dit is een test-e-mail van {kapper_naam}.</p>
    <p style="color:#999;font-size:0.85rem;">Als je dit ontvangt, werkt je e-mailinstelling correct!</p>
  </div>
</div>"""
    ok = stuur_email(email, f"Test e-mail — {kapper_naam}", html)
    if ok:
        return jsonify({'ok': True, 'bericht': f'Test e-mail verstuurd naar {email}!'})
    return jsonify({'ok': False, 'fout': 'Kon e-mail niet versturen. Controleer je App-wachtwoord.'})

# ── PWA manifest ────────────────────────────────────────────────────────────────
@app.route('/eigenaar/manifest.json')
def eigenaar_manifest():
    naam = get_instelling('kapper_naam') or 'Mijn Kapper'
    from flask import Response
    return Response(json.dumps({
        "name": f"{naam} — Beheer", "short_name": "Kapper Beheer",
        "start_url": "/eigenaar/dashboard", "display": "standalone",
        "background_color": "#141414", "theme_color": "#c9a84c",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}
        ]
    }), mimetype='application/json')

# ── Scheduler & Run ─────────────────────────────────────────────────────────────
scheduler = BackgroundScheduler()
scheduler.add_job(dagelijkse_herinneringen, 'cron', hour=9, minute=0)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

if __name__ == '__main__':
    init_db()
    print("=" * 52)
    print("  Kapper Reservatiesysteem v2")
    print("  Client:   http://127.0.0.1:5000")
    print("  Eigenaar: http://127.0.0.1:5000/eigenaar/login")
    print("  Wachtwoord: barber2024")
    print("=" * 52)
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
