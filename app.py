from flask import Flask, render_template, request, redirect, url_for, send_file, session, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, inspect, delete
from datetime import datetime, timedelta
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from werkzeug.security import generate_password_hash, check_password_hash
from flask_socketio import SocketIO  # <<< realtime
import os, re, math

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "tajny_klic_pro_session")

# ---------- DB konfigurace (Postgres/SQLite) ----------
db_url = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(os.getcwd(), 'akce.db')}")
if db_url.startswith("postgres://"):  # Heroku starý formát
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")  # <<< realtime

# ---------- MODELY ----------
class Akce(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nazev = db.Column(db.String(200), nullable=False)
    datum = db.Column(db.String(50), nullable=False)     # 'YYYY-MM-DD'
    cas = db.Column(db.String(50), nullable=False, default="")  # historická kompatibilita – NOT NULL
    cas_od = db.Column(db.String(50), nullable=True)     # 'HH:MM'
    cas_do = db.Column(db.String(50), nullable=True)     # 'HH:MM'
    misto = db.Column(db.String(200), nullable=False)
    poznamka = db.Column(db.String(500), nullable=True)
    vytvoreno = db.Column(db.DateTime, default=datetime.now)

class Produkt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nazev = db.Column(db.String(200), nullable=False)
    jednotka = db.Column(db.String(50), nullable=False, default="ks")
    skupina = db.Column(db.String(50), nullable=True)   # kabeláž/monitory/světla/repro/nářadí
    vytvoreno = db.Column(db.DateTime, default=datetime.now)

class Sklad(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    produkt_id = db.Column(db.Integer, db.ForeignKey("produkt.id"), nullable=False)
    akce_id = db.Column(db.Integer, db.ForeignKey("akce.id"), nullable=True)
    typ = db.Column(db.String(50), nullable=False)       # naskladneni / vyskladneni
    mnozstvi = db.Column(db.Integer, nullable=False)     # celé kusy
    datum = db.Column(db.DateTime, default=datetime.now)

class AkceProdukt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    akce_id = db.Column(db.Integer, db.ForeignKey("akce.id"), nullable=False)
    produkt_id = db.Column(db.Integer, db.ForeignKey("produkt.id"), nullable=False)
    mnozstvi = db.Column(db.Integer, nullable=False)     # celé kusy
    nalozeno = db.Column(db.Integer, nullable=False, default=0)
    hotovo = db.Column(db.Integer, nullable=False, default=0)
    akce = db.relationship("Akce", backref="produkty")
    produkt = db.relationship("Produkt")

class Zamestnanec(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    jmeno = db.Column(db.String(150), nullable=False)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Integer, default=0)  # 1=admin (admin + Roman), 0=zaměstnanec
    vytvoreno = db.Column(db.DateTime, default=datetime.now)

class AkceZamestnanec(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    akce_id = db.Column(db.Integer, db.ForeignKey("akce.id"), nullable=False)
    zamestnanec_id = db.Column(db.Integer, db.ForeignKey("zamestnanec.id"), nullable=False)
    akce = db.relationship("Akce", backref="pracovnici")
    zamestnanec = db.relationship("Zamestnanec")

class Timesheet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    zamestnanec_id = db.Column(db.Integer, db.ForeignKey("zamestnanec.id"), nullable=False)
    akce_id = db.Column(db.Integer, db.ForeignKey("akce.id"), nullable=False)
    start = db.Column(db.DateTime, nullable=False, default=datetime.now)
    end = db.Column(db.DateTime, nullable=True)
    duration_hours = db.Column(db.Float, nullable=False, default=0.0)
    zamestnanec = db.relationship("Zamestnanec")
    akce = db.relationship("Akce")

# ---------- INIT & SEED ----------
DEFAULT_ZAMESTNANCI = [
    "Roman Labaj", "Lukáš Vodrada", "Pavel Lach", "Václav Fiksek", "Václav Janeček",
    "Petr Lach", "Rostislav Staroň", "Štěpán Turoň", "Michal Pyszko",
    "Jakub Lipowski", "Ondřej Hanisch", "David Kantor", "Robert Zaremba"
]
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")  # změň v produkci

def strip_diacritics(s: str) -> str:
    repl = {"á":"a","č":"c","ď":"d","é":"e","ě":"e","í":"i","ň":"n","ó":"o","ř":"r",
            "š":"s","ť":"t","ú":"u","ů":"u","ý":"y","ž":"z","Á":"A","Č":"C","Ď":"D",
            "É":"E","Ě":"E","Í":"I","Ň":"N","Ó":"O","Ř":"R","Š":"S","Ť":"T","Ú":"U",
            "Ů":"U","Ý":"Y","Ž":"Z"}
    return "".join(repl.get(ch, ch) for ch in s)

def make_username(jmeno: str) -> str:
    base = strip_diacritics(jmeno).lower()
    base = re.sub(r"[^a-z0-9]+", ".", base).strip(".") or "user"
    uname, i = base, 1
    while Zamestnanec.query.filter_by(username=uname).first():
        i += 1; uname = f"{base}{i}"
    return uname

def ensure_schema_and_seed():
    insp = inspect(db.engine)

    # Akce: přidat cas_od/cas_do, synchronizovat cas (NOT NULL)
    if "akce" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("akce")]
        if "cas_od" not in cols:
            db.session.execute(text("ALTER TABLE akce ADD COLUMN cas_od TEXT"))
        if "cas_do" not in cols:
            db.session.execute(text("ALTER TABLE akce ADD COLUMN cas_do TEXT"))
        if "cas" in cols:
            db.session.execute(text("""
                UPDATE akce
                   SET cas_od = COALESCE(cas_od, cas),
                       cas_do = COALESCE(cas_do, cas)
            """))
            db.session.execute(text("""
                UPDATE akce
                   SET cas = COALESCE(cas, cas_od, '')
                 WHERE cas IS NULL
            """))
        db.session.commit()

    # Produkt: přidat skupina/jednotka
    if "produkt" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("produkt")]
        if "skupina" not in cols:
            db.session.execute(text("ALTER TABLE produkt ADD COLUMN skupina TEXT"))
        if "jednotka" not in cols:
            db.session.execute(text("ALTER TABLE produkt ADD COLUMN jednotka TEXT DEFAULT 'ks'"))
        db.session.commit()

    # AkceProdukt: checklist flagy
    if "akce_produkt" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("akce_produkt")]
        if "nalozeno" not in cols:
            db.session.execute(text("ALTER TABLE akce_produkt ADD COLUMN nalozeno INTEGER DEFAULT 0"))
        if "hotovo" not in cols:
            db.session.execute(text("ALTER TABLE akce_produkt ADD COLUMN hotovo INTEGER DEFAULT 0"))
        db.session.commit()

    # Zamestnanec: login + role
    if "zamestnanec" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("zamestnanec")]
        if "username" not in cols:
            db.session.execute(text("ALTER TABLE zamestnanec ADD COLUMN username TEXT"))
        if "password_hash" not in cols:
            db.session.execute(text("ALTER TABLE zamestnanec ADD COLUMN password_hash TEXT"))
        if "is_admin" not in cols:
            db.session.execute(text("ALTER TABLE zamestnanec ADD COLUMN is_admin INTEGER DEFAULT 0"))
        db.session.commit()
        # doplnit prázdné hodnoty
        for z in Zamestnanec.query.all():
            changed = False
            if not z.username:
                z.username = make_username(z.jmeno); changed = True
            if not z.password_hash:
                z.password_hash = generate_password_hash("start123"); changed = True
            if changed:
                db.session.add(z)
        db.session.commit()

    # Seed admin + zaměstnanci
    if not Zamestnanec.query.filter_by(username=ADMIN_USERNAME).first():
        db.session.add(Zamestnanec(
            jmeno="Administrátor",
            username=ADMIN_USERNAME,
            password_hash=generate_password_hash(ADMIN_PASSWORD),
            is_admin=1
        ))
        db.session.commit()

    exist_jmena = {z.jmeno for z in Zamestnanec.query.all()}
    for j in DEFAULT_ZAMESTNANCI:
        if j not in exist_jmena:
            db.session.add(Zamestnanec(
                jmeno=j,
                username=make_username(j),
                password_hash=generate_password_hash("start123"),
                is_admin=1 if j == "Roman Labaj" else 0
            ))
    db.session.commit()

with app.app_context():
    db.create_all()
    ensure_schema_and_seed()

# ---------- PRÁVA & POMOCNÉ ----------
SKUPINY = ['kabeláž', 'monitory', 'světla', 'repro', 'nářadí']

def is_roman() -> bool:
    return (session.get("display_name") or "").lower() == "roman labaj" or (session.get("username") or "") == "roman.labaj"

def is_privileged_session() -> bool:
    return bool(session.get("is_admin")) or is_roman()

def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*a, **k):
        if not session.get("user_id"): return redirect(url_for("login"))
        return fn(*a, **k)
    return wrapper

def privileged_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*a, **k):
        if not is_privileged_session():
            flash("Pouze administrátor nebo Roman Labaj.", "error")
            return redirect(url_for("index"))
        return fn(*a, **k)
    return wrapper

def admin_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*a, **k):
        if not session.get("is_admin"):
            flash("Pouze administrátor.", "error")
            return redirect(url_for("index"))
        return fn(*a, **k)
    return wrapper

def stav_skladu_for(pid: int) -> int:
    n = db.session.query(db.func.sum(Sklad.mnozstvi)).filter_by(produkt_id=pid, typ="naskladneni").scalar() or 0
    v = db.session.query(db.func.sum(Sklad.mnozstvi)).filter_by(produkt_id=pid, typ="vyskladneni").scalar() or 0
    return int(n - v)

def is_user_assigned_to_akce(akce_id: int, user_id: int) -> bool:
    return AkceZamestnanec.query.filter_by(akce_id=akce_id, zamestnanec_id=user_id).first() is not None

def has_open_timesheet(user_id: int):
    return Timesheet.query.filter_by(zamestnanec_id=user_id, end=None).first()

# ---------- AUTH ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        user = Zamestnanec.query.filter_by(username=username).first()
        if not user or not check_password_hash(user.password_hash, password):
            flash("Neplatné přihlašovací údaje.", "error")
            return render_template("login.html")
        session.clear()
        session["user_id"] = user.id
        session["username"] = user.username
        session["display_name"] = user.jmeno
        session["is_admin"] = bool(user.is_admin)
        flash(f"Přihlášen: {user.jmeno}", "success")
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("Byl jste odhlášen.", "success")
    return redirect(url_for("login"))

# ---------- AKCE – PŘEHLED ----------
@app.route("/")
@login_required
def index():
    uid = session["user_id"]
    privileged = is_privileged_session()

    moje_ids = [r.akce_id for r in AkceZamestnanec.query.filter_by(zamestnanec_id=uid).all()]
    akce_moje = Akce.query.filter(Akce.id.in_(moje_ids)).order_by(Akce.vytvoreno.desc()).all() if moje_ids else []

    dnes = datetime.now().date().isoformat()
    akce_dnes = [a for a in akce_moje if a.datum == dnes]

    open_ts = has_open_timesheet(uid)
    open_akce_id = open_ts.akce_id if open_ts else None

    akce_all = Akce.query.order_by(Akce.vytvoreno.desc()).all() if privileged else []

    return render_template("index.html",
                           akce_all=akce_all,
                           akce_moje=akce_moje,
                           akce_dnes=akce_dnes,
                           open_akce_id=open_akce_id,
                           privileged=privileged)

# ---------- AKCE – NOVÁ ----------
@app.route("/akce/nova", methods=["GET", "POST"])
@privileged_required
def akce_nova():
    produkty = Produkt.query.order_by(Produkt.skupina, Produkt.nazev).all()
    zam = Zamestnanec.query.order_by(Zamestnanec.jmeno).all()

    if request.method == "POST":
        cas_od = request.form.get("cas_od") or ""
        cas_do = request.form.get("cas_do") or ""
        a = Akce(
            nazev=request.form["nazev"],
            datum=request.form["datum"],
            cas=cas_od or "",  # kvůli NOT NULL
            cas_od=cas_od,
            cas_do=cas_do,
            misto=request.form["misto"],
            poznamka=request.form.get("poznamka", "")
        )
        db.session.add(a)
        db.session.flush()  # a.id

        # produkty -> vyskladnit (celá čísla)
        for p in produkty:
            key = f"produkt_{p.id}"
            if key in request.form and request.form[key]:
                try:
                    qty = int(float(request.form[key]))
                except ValueError:
                    qty = 0
                if qty > 0:
                    db.session.add(AkceProdukt(akce_id=a.id, produkt_id=p.id, mnozstvi=qty))
                    db.session.add(Sklad(produkt_id=p.id, akce_id=a.id, typ="vyskladneni", mnozstvi=qty))

        # zaměstnanci
        for zid in request.form.getlist("zamestnanci[]"):
            try:
                db.session.add(AkceZamestnanec(akce_id=a.id, zamestnanec_id=int(zid)))
            except Exception:
                pass

        db.session.commit()
        socketio.emit("akce_updated", {"id": a.id}, broadcast=True)  # <<< realtime
        return redirect(url_for("index"))

    return render_template("akce_nova.html", produkty=produkty, zamestnanci=zam, skupiny=SKUPINY)

# ---------- AKCE – UPRAVIT ----------
@app.route("/akce/upravit/<int:id>", methods=["GET", "POST"])
@privileged_required
def akce_upravit(id):
    a = Akce.query.get_or_404(id)
    produkty = Produkt.query.order_by(Produkt.skupina, Produkt.nazev).all()
    zam = Zamestnanec.query.order_by(Zamestnanec.jmeno).all()

    if request.method == "POST":
        a.nazev = request.form["nazev"]
        a.datum = request.form["datum"]
        a.cas_od = request.form.get("cas_od") or ""
        a.cas_do = request.form.get("cas_do") or ""
        a.cas = a.cas_od or ""
        a.misto = request.form["misto"]
        a.poznamka = request.form.get("poznamka", "")

        # vrátit staré produkty do skladu, smazat vazby
        for ap in a.produkty:
            db.session.add(Sklad(produkt_id=ap.produkt_id, akce_id=None, typ="naskladneni", mnozstvi=ap.mnozstvi))
        AkceProdukt.query.filter_by(akce_id=a.id).delete()

        # vložit nové produkty + vyskladnit
        for p in produkty:
            key = f"produkt_{p.id}"
            if key in request.form and request.form[key]:
                try:
                    qty = int(float(request.form[key]))
                except ValueError:
                    qty = 0
                if qty > 0:
                    db.session.add(AkceProdukt(akce_id=a.id, produkt_id=p.id, mnozstvi=qty))
                    db.session.add(Sklad(produkt_id=p.id, akce_id=a.id, typ="vyskladneni", mnozstvi=qty))

        # zaměstnanci
        AkceZamestnanec.query.filter_by(akce_id=a.id).delete()
        for zid in request.form.getlist("zamestnanci[]"):
            try:
                db.session.add(AkceZamestnanec(akce_id=a.id, zamestnanec_id=int(zid)))
            except Exception:
                pass

        db.session.commit()
        socketio.emit("akce_updated", {"id": a.id}, broadcast=True)  # <<< realtime
        return redirect(url_for("akce_detail", id=a.id))

    prirazeni_ids = {rel.zamestnanec_id for rel in a.pracovnici}
    stav = {ap.produkt_id: ap.mnozstvi for ap in a.produkty}
    return render_template("akce_upravit.html",
                           akce=a, produkty=produkty, zamestnanci=zam,
                           prirazeni_ids=prirazeni_ids, stav=stav, skupiny=SKUPINY)

# ---------- AKCE – DETAIL ----------
@app.route("/akce/detail/<int:id>")
@login_required
def akce_detail(id):
    a = Akce.query.get_or_404(id)
    uid = session["user_id"]
    privileged = is_privileged_session()
    if not privileged and not is_user_assigned_to_akce(id, uid):
        flash("K této akci nemáte přístup.", "error")
        return redirect(url_for("index"))

    jmena_zam = [rel.zamestnanec.jmeno for rel in a.pracovnici]
    open_ts = has_open_timesheet(uid)
    open_akce_id = open_ts.akce_id if open_ts else None
    return render_template("akce_detail.html", akce=a, jmena_zam=jmena_zam, open_akce_id=open_akce_id)

# ---------- AKCE – CHECKLIST ----------
@app.route("/akce/<int:id>/checklist", methods=["GET", "POST"])
@login_required
def akce_checklist(id):
    a = Akce.query.get_or_404(id)
    uid = session["user_id"]
    privileged = is_privileged_session()
    if not privileged and not is_user_assigned_to_akce(id, uid):
        flash("K této akci nemáte přístup.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        for ap in a.produkty:
            ap.nalozeno = 1 if request.form.get(f"nalozeno_{ap.id}") == "on" else 0
            ap.hotovo = 1 if request.form.get(f"hotovo_{ap.id}") == "on" else 0
            db.session.add(ap)
        db.session.commit()
        socketio.emit("checklist_updated", {"akce_id": id}, broadcast=True)  # <<< realtime
        flash("Checklist uložen.", "success")
        return redirect(url_for("akce_checklist", id=id))

    return render_template("akce_checklist.html", akce=a)

@app.route("/akce/<int:id>/checklist_pdf")
@login_required
def akce_checklist_pdf(id):
    a = Akce.query.get_or_404(id)
    uid = session["user_id"]
    privileged = is_privileged_session()
    if not privileged and not is_user_assigned_to_akce(id, uid):
        flash("K této akci nemáte přístup.", "error")
        return redirect(url_for("index"))

    fname = f"checklist_{id}.pdf"
    try:
        pdfmetrics.registerFont(TTFont("DejaVuSans", "DejaVuSans.ttf"))
        font_name = "DejaVuSans"
    except Exception:
        font_name = "Helvetica"

    c = canvas.Canvas(fname, pagesize=A4)
    width, height = A4
    y = height - 20*mm
    c.setFont(font_name, 16)
    c.drawString(20*mm, y, f"Checklist – {a.nazev}")
    y -= 8*mm
    c.setFont(font_name, 11)
    c.drawString(20*mm, y, f"Datum: {a.datum}   Čas: {(a.cas_od or '')}–{(a.cas_do or '')}   Místo: {a.misto}")
    y -= 10*mm
    c.setFont(font_name, 11)
    c.drawString(20*mm, y, "☐ = naloženo   ☐ = hotovo (zaškrtněte ručně)")
    y -= 8*mm
    c.setFont(font_name, 12)
    for ap in a.produkty:
        if y < 20*mm:
            c.showPage(); y = height - 20*mm; c.setFont(font_name, 12)
        c.drawString(20*mm, y, f"■ {ap.produkt.skupina} – {ap.produkt.nazev}  × {int(ap.mnozstvi)} {ap.produkt.jednotka or 'ks'}")
        y -= 7*mm
        c.drawString(25*mm, y, "☐ Naloženo    ☐ Hotovo")
        y -= 7*mm
    c.showPage(); c.save()
    return send_file(fname, as_attachment=True)

# ---------- AKCE – SMAZAT ----------
@app.route("/akce/smazat/<int:id>")
@privileged_required
def akce_smazat(id):
    a = Akce.query.get_or_404(id)
    # vrátit produkty do skladu
    for ap in a.produkty:
        db.session.add(Sklad(produkt_id=ap.produkt_id, akce_id=None, typ="naskladneni", mnozstvi=ap.mnozstvi))
    AkceProdukt.query.filter_by(akce_id=id).delete()
    AkceZamestnanec.query.filter_by(akce_id=id).delete()
    db.session.delete(a)
    db.session.commit()
    socketio.emit("akce_deleted", {"id": id}, broadcast=True)  # <<< realtime
    flash("Akce smazána. Technika naskladněna zpět.", "success")
    return redirect(url_for("index"))

# ---------- EXPORT – Přehled akcí PDF ----------
@app.route("/export_pdf")
@privileged_required
def export_pdf():
    akce = Akce.query.order_by(Akce.vytvoreno.desc()).all()
    fname = "prehled_akci.pdf"
    try:
        pdfmetrics.registerFont(TTFont("DejaVuSans", "DejaVuSans.ttf"))
        font_name = "DejaVuSans"
    except Exception:
        font_name = "Helvetica"
    c = canvas.Canvas(fname, pagesize=A4)
    width, height = A4
    y = height - 25*mm
    c.setFont(font_name, 18)
    c.drawString(20*mm, y, "Přehled akcí – Ozvučení")
    y -= 12*mm
    c.setFont(font_name, 11)
    for a in akce:
        if y < 25*mm:
            c.showPage(); y = height - 20*mm; c.setFont(font_name, 11)
        c.drawString(20*mm, y, f"{a.datum}  {a.cas_od or ''}–{a.cas_do or ''}  {a.nazev}  @ {a.misto}")
        y -= 7*mm
        if a.poznamka:
            c.drawString(25*mm, y, f"Pozn.: {a.poznamka}")
            y -= 7*mm
    c.save()
    return send_file(fname, as_attachment=True)

# ---------- PRODUKTY ----------
@app.route("/produkty")
@privileged_required
def produkty():
    produkty_list = Produkt.query.order_by(Produkt.skupina, Produkt.nazev).all()
    return render_template("produkty.html", produkty=produkty_list, skupiny=SKUPINY)

@app.route("/produkt/edit/<int:id>", methods=["GET", "POST"])
@privileged_required
def edit_produkt(id):
    produkt = Produkt.query.get(id) if id != 0 else Produkt(nazev="", jednotka="ks", skupina="")
    if request.method == "POST":
        produkt.nazev = request.form["nazev"]
        produkt.jednotka = request.form.get("jednotka") or "ks"
        produkt.skupina = request.form.get("skupina") or ""
        if id == 0: db.session.add(produkt)
        db.session.commit()
        socketio.emit("product_updated", {"id": produkt.id}, broadcast=True)  # <<< realtime
        flash("Produkt uložen.", "success")
        return redirect(url_for("produkty"))
    return render_template("edit_produkt.html", produkt=produkt, skupiny=SKUPINY)

@app.route("/produkt/delete/<int:id>")
@privileged_required
def delete_produkt(id):
    p = Produkt.query.get_or_404(id)
    db.session.delete(p)
    db.session.commit()
    socketio.emit("product_updated", {"id": id}, broadcast=True)  # <<< realtime
    flash("Produkt smazán.", "success")
    return redirect(url_for("produkty"))

# ---------- SKLAD ----------
def sklad_privileged():
    return session.get("is_admin") or is_roman()

@app.route("/sklad")
@login_required
def sklad():
    if not sklad_privileged():
        flash("Do skladu má přístup jen administrátor nebo Roman Labaj.", "error")
        return redirect(url_for("index"))
    produkty_list = Produkt.query.order_by(Produkt.skupina, Produkt.nazev).all()
    stav = {p.id: stav_skladu_for(p.id) for p in produkty_list}
    return render_template("sklad.html", produkty=produkty_list, stav=stav)

@app.route("/naskladnit", methods=["GET", "POST"])
@login_required
def naskladnit():
    if not sklad_privileged():
        flash("Do skladu má přístup jen administrátor nebo Roman Labaj.", "error")
        return redirect(url_for("index"))
    produkty = Produkt.query.order_by(Produkt.skupina, Produkt.nazev).all()
    if request.method == "POST":
        produkt_id = int(request.form["produkt_id"])
        mnozstvi = int(float(request.form["mnozstvi"]))
        if mnozstvi <= 0:
            flash("Množství musí být kladné celé číslo.", "error")
            return redirect(url_for("naskladnit"))
        db.session.add(Sklad(produkt_id=produkt_id, typ="naskladneni", mnozstvi=mnozstvi))
        db.session.commit()
        socketio.emit("inventory_updated", {}, broadcast=True)  # <<< realtime
        flash("Naskladněno.", "success")
        return redirect(url_for("sklad"))
    return render_template("naskladnit.html", produkty=produkty)

@app.route("/vyskladnit", methods=["GET", "POST"])
@login_required
def vyskladnit():
    if not sklad_privileged():
        flash("Do skladu má přístup jen administrátor nebo Roman Labaj.", "error")
        return redirect(url_for("index"))
    produkty = Produkt.query.order_by(Produkt.skupina, Produkt.nazev).all()
    akce_list = Akce.query.order_by(Akce.vytvoreno.desc()).all()
    if request.method == "POST":
        produkt_id = int(request.form["produkt_id"])
        akce_id = int(request.form.get("akce_id") or 0)
        mnozstvi = int(float(request.form["mnozstvi"]))
        if mnozstvi <= 0:
            flash("Množství musí být kladné celé číslo.", "error")
            return redirect(url_for("vyskladnit"))
        db.session.add(Sklad(
            produkt_id=produkt_id,
            akce_id=akce_id if akce_id else None,
            typ="vyskladneni",
            mnozstvi=mnozstvi
        ))
        db.session.commit()
        socketio.emit("inventory_updated", {}, broadcast=True)  # <<< realtime
        flash("Vyskladněno.", "success")
        return redirect(url_for("sklad"))
    return render_template("vyskladnit.html", produkty=produkty, akce_list=akce_list)

# ---------- ZAMĚSTNANCI (admin: změna hesel) ----------
@app.route("/zamestnanci")
@admin_required
def zamestnanci():
    zams = Zamestnanec.query.order_by(Zamestnanec.jmeno).all()
    return render_template("zamestnanci.html", zamestnanci=zams)

@app.route("/zamestnanci/<int:id>/set_password", methods=["POST"])
@admin_required
def zamestnanec_set_password(id):
    z = Zamestnanec.query.get_or_404(id)
    newpass = (request.form.get("new_password") or "").strip()
    if len(newpass) < 4:
        flash("Heslo musí mít alespoň 4 znaky.", "error")
        return redirect(url_for("zamestnanci"))
    z.password_hash = generate_password_hash(newpass)
    db.session.commit()
    flash(f"Heslo pro {z.jmeno} změněno.", "success")
    return redirect(url_for("zamestnanci"))

# ---------- DOCHÁZKA (jen v den akce, zaokrouhlení na 0.5h) ----------
def can_start_today(a: Akce) -> bool:
    try:
        y, m, d = [int(x) for x in (a.datum or "").split("-")]
        event_date = datetime(y, m, d).date()
    except Exception:
        return False
    return datetime.now().date() == event_date

@app.route("/akce/<int:akce_id>/start", methods=["POST"])
@login_required
def ts_start(akce_id):
    uid = session["user_id"]
    a = Akce.query.get_or_404(akce_id)
    if not is_user_assigned_to_akce(akce_id, uid):
        flash("Nejste přiřazen k této akci.", "error"); return redirect(url_for("index"))
    if has_open_timesheet(uid):
        flash("Už máte běžící záznam. Nejdřív ho ukončete.", "error"); return redirect(url_for("index"))
    if not can_start_today(a):
        flash(f"Docházku lze spustit pouze v den akce ({a.datum}).", "error"); return redirect(url_for("index"))

    try:
        hh, mm = [int(x) for x in (a.cas_od or "00:00").split(":")[:2]]
    except Exception:
        hh, mm = 0, 0
    event_start = datetime.strptime(a.datum, "%Y-%m-%d").replace(hour=hh, minute=mm)
    effective = max(event_start, datetime.now())
    rounded = effective.replace(second=0, microsecond=0)
    if rounded.minute % 30 != 0:
        rounded += timedelta(minutes=(30 - rounded.minute % 30))

    db.session.add(Timesheet(zamestnanec_id=uid, akce_id=akce_id, start=rounded))
    db.session.commit()
    socketio.emit("timesheet_updated", {"akce_id": akce_id}, broadcast=True)  # <<< realtime
    flash(f"Docházka spuštěna od {rounded.strftime('%H:%M')} (zaokrouhleno na půlhodinu).", "success")
    return redirect(request.referrer or url_for("index"))

@app.route("/akce/<int:akce_id>/stop", methods=["POST"])
@login_required
def ts_stop(akce_id):
    uid = session["user_id"]
    ts = Timesheet.query.filter_by(zamestnanec_id=uid, akce_id=akce_id, end=None).first()
    if not ts:
        flash("Neběží žádný záznam docházky pro tuto akci.", "error"); return redirect(url_for("index"))

    now = datetime.now()
    ts.end = now
    minutes = max(0, (ts.end - ts.start).total_seconds() / 60.0)
    ts.duration_hours = math.ceil(minutes / 30.0) / 2.0
    db.session.commit()
    socketio.emit("timesheet_updated", {"akce_id": akce_id}, broadcast=True)  # <<< realtime
    flash(f"Docházka ukončena. Délka: {ts.duration_hours:.1f} h.", "success")
    return redirect(request.referrer or url_for("index"))

# ---------- HODINY – přehled + mazání ----------
@app.route("/hodiny")
@login_required
def hodiny():
    if not (session.get("is_admin") or is_roman()):
        flash("Sekci Hodiny může zobrazit jen administrátor nebo Roman Labaj.", "error")
        return redirect(url_for("index"))

    tot_rows = db.session.query(
        Timesheet.zamestnanec_id, Zamestnanec.jmeno, db.func.sum(Timesheet.duration_hours)
    ).join(Zamestnanec, Zamestnanec.id == Timesheet.zamestnanec_id
    ).group_by(Timesheet.zamestnanec_id, Zamestnanec.jmeno).all()

    det_rows = db.session.query(
        Timesheet.zamestnanec_id, Timesheet.akce_id, Akce.nazev, db.func.sum(Timesheet.duration_hours)
    ).join(Akce, Akce.id == Timesheet.akce_id
    ).group_by(Timesheet.zamestnanec_id, Timesheet.akce_id, Akce.nazev).all()

    action_rows = db.session.query(
        Timesheet.akce_id, Akce.nazev, Akce.datum, db.func.sum(Timesheet.duration_hours)
    ).join(Akce, Akce.id == Timesheet.akce_id
    ).group_by(Timesheet.akce_id, Akce.nazev, Akce.datum).all()

    running = Timesheet.query.filter(Timesheet.end.is_(None)).all()

    data = {}
    for zid, jmeno, total in tot_rows:
        data[zid] = {"jmeno": jmeno, "total": float(total or 0), "akce": []}
    for zid, aid, aname, subtotal in det_rows:
        data.setdefault(zid, {"jmeno": "??", "total": 0.0, "akce": []})
        data[zid]["akce"].append({"akce_id": aid, "nazev": aname, "hours": float(subtotal or 0)})

    actions = [{"akce_id": aid, "nazev": name, "datum": datum, "hours": float(total or 0)}
               for (aid, name, datum, total) in action_rows]

    return render_template("hodiny.html", data=data, running=running, actions=actions)

@app.route("/hodiny/delete/all", methods=["POST"])
@login_required
def hodiny_delete_all():
    if not (session.get("is_admin") or is_roman()):
        flash("Mazání hodin je povoleno jen administrátorovi nebo Romanu Labajovi.", "error")
        return redirect(url_for("hodiny"))
    db.session.execute(delete(Timesheet))
    db.session.commit()
    socketio.emit("timesheet_updated", {}, broadcast=True)  # <<< realtime
    flash("Všechny hodinové záznamy byly smazány.", "success")
    return redirect(url_for("hodiny"))

@app.route("/hodiny/delete/akce/<int:akce_id>", methods=["POST"])
@login_required
def hodiny_delete_by_akce(akce_id):
    if not (session.get("is_admin") or is_roman()):
        flash("Mazání hodin je povoleno jen administrátorovi nebo Romanu Labajovi.", "error")
        return redirect(url_for("hodiny"))
    a = Akce.query.get(akce_id)
    if not a:
        flash("Akce nenalezena.", "error")
        return redirect(url_for("hodiny"))
    db.session.execute(delete(Timesheet).where(Timesheet.akce_id == akce_id))
    db.session.commit()
    socketio.emit("timesheet_updated", {"akce_id": akce_id}, broadcast=True)  # <<< realtime
    flash(f"Hodinové záznamy pro akci „{a.nazev}“ byly smazány.", "success")
    return redirect(url_for("hodiny"))

# ---------- RUN ----------
if __name__ == "__main__":
    try:
        pdfmetrics.registerFont(TTFont("DejaVuSans", "DejaVuSans.ttf"))
    except Exception:
        pass
    socketio.run(app, debug=True)  # <<< realtime server
