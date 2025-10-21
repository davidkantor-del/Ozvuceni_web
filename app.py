import os
from datetime import datetime, timedelta, date, time
from flask import Flask, render_template, request, redirect, url_for, send_file, session, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm

# -----------------------------------------------------------------------------
# APLIKACE & DB
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "tajny_klic_pro_session")

# Podpora Render/Postgres + lokální SQLite
db_url = os.environ.get("DATABASE_URL", "")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
if not db_url:
    db_url = f"sqlite:///{os.path.join(os.getcwd(), 'akce.db')}"
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# Kategorie techniky
SKUPINY = ["kabeláž", "monitory", "světla", "repro", "nářadí"]

# -----------------------------------------------------------------------------
# MODELY
# -----------------------------------------------------------------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="staff")  # admin / manager / staff
    active = db.Column(db.Boolean, default=True)

    def set_password(self, pw: str):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)


class Akce(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nazev = db.Column(db.String(200), nullable=False)
    datum = db.Column(db.String(50), nullable=False)           # YYYY-MM-DD
    cas_od = db.Column(db.String(10), nullable=True)           # HH:MM
    cas_do = db.Column(db.String(10), nullable=True)           # HH:MM
    misto = db.Column(db.String(200), nullable=False)
    poznamka = db.Column(db.Text, nullable=True)
    vytvoreno = db.Column(db.DateTime, default=datetime.now)


class Produkt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nazev = db.Column(db.String(200), nullable=False)
    jednotka = db.Column(db.String(50), nullable=False, default="ks")
    skupina = db.Column(db.String(50), nullable=True)
    vytvoreno = db.Column(db.DateTime, default=datetime.now)


class Sklad(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    produkt_id = db.Column(db.Integer, db.ForeignKey("produkt.id"), nullable=False)
    akce_id = db.Column(db.Integer, db.ForeignKey("akce.id"), nullable=True)
    typ = db.Column(db.String(50), nullable=False)  # naskladneni / vyskladneni
    mnozstvi = db.Column(db.Float, nullable=False)
    datum = db.Column(db.DateTime, default=datetime.now)


class AkceProdukt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    akce_id = db.Column(db.Integer, db.ForeignKey("akce.id"), nullable=False)
    produkt_id = db.Column(db.Integer, db.ForeignKey("produkt.id"), nullable=False)
    mnozstvi = db.Column(db.Float, nullable=False)


class AkceZamestnanec(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    akce_id = db.Column(db.Integer, db.ForeignKey("akce.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)


class Hodiny(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    akce_id = db.Column(db.Integer, db.ForeignKey("akce.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    start = db.Column(db.DateTime, nullable=True)
    end = db.Column(db.DateTime, nullable=True)
    minuty = db.Column(db.Integer, default=0)

# -----------------------------------------------------------------------------
# INIT DB + výchozí uživatelé
# -----------------------------------------------------------------------------
DEFAULT_USERS = [
    ("admin", "admin", "admin123"),            # admin – plná práva
    ("roman", "manager", "roman123"),          # Roman Labaj – i sklad a přiřazování
    ("lukas", "staff", "123456"),
    ("pavel", "staff", "123456"),
    ("vaclav_f", "staff", "123456"),
    ("vaclav_j", "staff", "123456"),
    ("petr_l", "staff", "123456"),
    ("rostislav", "staff", "123456"),
    ("stepan", "staff", "123456"),
    ("michal", "staff", "123456"),
    ("jakub", "staff", "123456"),
    ("ondrej", "staff", "123456"),
    ("david", "staff", "123456"),
    ("robert", "staff", "123456"),
]
with app.app_context():
    db.create_all()
    for uname, role, pwd in DEFAULT_USERS:
        if not User.query.filter_by(username=uname).first():
            u = User(username=uname, role=role)
            u.set_password(pwd)
            db.session.add(u)
    db.session.commit()

# -----------------------------------------------------------------------------
# HELPERY
# -----------------------------------------------------------------------------
def current_user():
    uid = session.get("user_id")
    return User.query.get(uid) if uid else None

def login_required(fn):
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper

def require_role(*roles):
    def deco(fn):
        def wrapper(*args, **kwargs):
            u = current_user()
            if not u or u.role not in roles:
                flash("Nemáš oprávnění pro tuto akci.", "error")
                return redirect(url_for("index"))
            return fn(*args, **kwargs)
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco

def stav_skladu(produkt_id: int) -> float:
    n = db.session.query(db.func.sum(Sklad.mnozstvi)).filter_by(produkt_id=produkt_id, typ="naskladneni").scalar() or 0
    v = db.session.query(db.func.sum(Sklad.mnozstvi)).filter_by(produkt_id=produkt_id, typ="vyskladneni").scalar() or 0
    return float(n - v)

def round_to_half_hours(minutes: int) -> int:
    q, r = divmod(minutes, 30)
    return (q + (1 if r else 0)) * 30

def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def parse_time(s: str) -> time:
    return datetime.strptime(s, "%H:%M").time()

def vrat_produkty_a_smaz_vazby(akce: Akce):
    for ap in AkceProdukt.query.filter_by(akce_id=akce.id).all():
        db.session.add(Sklad(produkt_id=ap.produkt_id, akce_id=None, typ="naskladneni", mnozstvi=ap.mnozstvi))
    AkceProdukt.query.filter_by(akce_id=akce.id).delete()

def uloz_produkty_k_akci(akce: Akce, form, produkty):
    for p in produkty:
        key = f"produkt_{p.id}"
        if key in form and form[key]:
            try:
                qty = float(form[key])
            except ValueError:
                qty = 0.0
            if qty > 0:
                db.session.add(AkceProdukt(akce_id=akce.id, produkt_id=p.id, mnozstvi=qty))
                db.session.add(Sklad(produkt_id=p.id, akce_id=akce.id, typ="vyskladneni", mnozstvi=qty))

def uloz_zamestnance_k_akci(akce: Akce, form):
    AkceZamestnanec.query.filter_by(akce_id=akce.id).delete()
    for u in User.query.filter(User.active==True).all():
        if form.get(f"zam_{u.id}") == "on":
            db.session.add(AkceZamestnanec(akce_id=akce.id, user_id=u.id))

# -----------------------------------------------------------------------------
# LOGIN / LOGOUT
# -----------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        uname = request.form.get("username", "").strip()
        pw = request.form.get("password", "")
        u = User.query.filter_by(username=uname, active=True).first()
        if u and u.check_password(pw):
            session["user_id"] = u.id
            flash("Přihlášení OK.", "success")
            return redirect(url_for("index"))
        flash("Neplatné přihlašovací údaje.", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("Byl jsi odhlášen.", "success")
    return redirect(url_for("login"))

# -----------------------------------------------------------------------------
# DASHBOARD
# -----------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    akce = Akce.query.order_by(Akce.vytvoreno.desc()).all()
    return render_template("index.html", akce=akce, user=current_user())

# -----------------------------------------------------------------------------
# AKCE – CRUD
# -----------------------------------------------------------------------------
@app.route("/akce/nova", methods=["GET", "POST"])
@login_required
@require_role("admin", "manager")
def akce_nova():
    produkty = Produkt.query.order_by(Produkt.skupina, Produkt.nazev).all()
    users = User.query.filter_by(active=True).order_by(User.username).all()
    if request.method == "POST":
        a = Akce(
            nazev=request.form["nazev"],
            datum=request.form["datum"],
            cas_od=request.form.get("cas_od") or "",
            cas_do=request.form.get("cas_do") or "",
            misto=request.form["misto"],
            poznamka=request.form.get("poznamka", "")
        )
        db.session.add(a); db.session.flush()
        uloz_produkty_k_akci(a, request.form, produkty)
        uloz_zamestnance_k_akci(a, request.form)
        db.session.commit()
        flash("Akce vytvořena a položky vyskladněny.", "success")
        return redirect(url_for("index"))
    return render_template("akce_nova.html", produkty=produkty, users=users, skupiny=SKUPINY)

@app.route("/akce/upravit/<int:id>", methods=["GET", "POST"])
@login_required
@require_role("admin", "manager")
def akce_upravit(id):
    a = Akce.query.get_or_404(id)
    produkty = Produkt.query.order_by(Produkt.skupina, Produkt.nazev).all()
    users = User.query.filter_by(active=True).order_by(User.username).all()
    prirazeni_ids = {z.user_id for z in AkceZamestnanec.query.filter_by(akce_id=a.id).all()}

    if request.method == "POST":
        a.nazev = request.form["nazev"]
        a.datum = request.form["datum"]
        a.cas_od = request.form.get("cas_od") or ""
        a.cas_do = request.form.get("cas_do") or ""
        a.misto = request.form["misto"]
        a.poznamka = request.form.get("poznamka", "")

        vrat_produkty_a_smaz_vazby(a)
        uloz_produkty_k_akci(a, request.form, produkty)
        uloz_zamestnance_k_akci(a, request.form)

        db.session.commit()
        flash("Akce upravena.", "success")
        return redirect(url_for("index"))

    return render_template("akce_upravit.html", akce=a, produkty=produkty, users=users, prirazeni_ids=prirazeni_ids, skupiny=SKUPINY)

@app.route("/akce/detail/<int:id>")
@login_required
def akce_detail(id):
    a = Akce.query.get_or_404(id)
    prirazeni = db.session.query(User).join(AkceZamestnanec, User.id==AkceZamestnanec.user_id)\
                .filter(AkceZamestnanec.akce_id==a.id).all()
    polozky = db.session.query(AkceProdukt, Produkt).join(Produkt, AkceProdukt.produkt_id==Produkt.id)\
                .filter(AkceProdukt.akce_id==a.id).all()
    u = current_user()
    moje = Hodiny.query.filter_by(akce_id=a.id, user_id=u.id).order_by(Hodiny.id.desc()).all()
    return render_template("akce_detail.html", akce=a, prirazeni=prirazeni, polozky=polozky, moje_hodiny=moje, user=u)

@app.route("/akce/smazat/<int:id>")
@login_required
@require_role("admin", "manager")
def akce_smazat(id):
    a = Akce.query.get_or_404(id)
    vrat_produkty_a_smaz_vazby(a)
    AkceZamestnanec.query.filter_by(akce_id=a.id).delete()
    Hodiny.query.filter_by(akce_id=a.id).delete()
    db.session.delete(a); db.session.commit()
    flash("Akce smazána (položky vráceny, hodiny smazány).", "success")
    return redirect(url_for("index"))

# -----------------------------------------------------------------------------
# HODINY (přihlášení jen v den akce; start korigovaný)
# -----------------------------------------------------------------------------
def can_check_today(a: Akce) -> bool:
    try:
        return parse_date(a.datum) == date.today()
    except Exception:
        return False

def compute_start_from_rules(a: Akce, clicked: datetime) -> datetime:
    if not a.cas_od:
        return clicked
    start_nominal = datetime.combine(parse_date(a.datum), parse_time(a.cas_od))
    if clicked <= start_nominal:
        return start_nominal
    # další půlhodina
    minute = (clicked.minute // 30 + 1) * 30
    hour = clicked.hour + (1 if minute == 60 else 0)
    minute = 0 if minute == 60 else minute
    candidate = clicked.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate < clicked:
        candidate += timedelta(minutes=30)
    return candidate

@app.route("/akce/<int:id>/checkin", methods=["POST"])
@login_required
def akce_checkin(id):
    a = Akce.query.get_or_404(id)
    u = current_user()
    if not can_check_today(a):
        flash("Na akci se lze přihlásit jen v den konání.", "error")
        return redirect(url_for("akce_detail", id=a.id))
    if Hodiny.query.filter_by(akce_id=a.id, user_id=u.id, end=None).first():
        flash("Už máš běžící záznam.", "error")
        return redirect(url_for("akce_detail", id=a.id))
    start_calc = compute_start_from_rules(a, datetime.now())
    db.session.add(Hodiny(akce_id=a.id, user_id=u.id, start=start_calc))
    db.session.commit()
    flash(f"Započítáno od {start_calc.strftime('%H:%M')}.", "success")
    return redirect(url_for("akce_detail", id=a.id))

@app.route("/akce/<int:id>/checkout", methods=["POST"])
@login_required
def akce_checkout(id):
    a = Akce.query.get_or_404(id)
    u = current_user()
    rec = Hodiny.query.filter_by(akce_id=a.id, user_id=u.id, end=None).first()
    if not rec:
        flash("Nemáš běžící záznam.", "error")
        return redirect(url_for("akce_detail", id=a.id))
    now = datetime.now()
    minutes = int((now - rec.start).total_seconds() // 60)
    rounded = round_to_half_hours(minutes)
    rec.end = rec.start + timedelta(minutes=rounded)
    rec.minuty = rounded
    db.session.commit()
    flash(f"Ukončeno. Započítáno {rounded} min.", "success")
    return redirect(url_for("akce_detail", id=a.id))

@app.route("/hodiny")
@login_required
def hodiny_overview():
    u = current_user()
    q = db.session.query(Hodiny, User, Akce)\
        .join(User, Hodiny.user_id==User.id)\
        .join(Akce, Hodiny.akce_id==Akce.id)
    if u.role not in ("admin", "manager"):
        q = q.filter(User.id==u.id)
    zaznamy = q.order_by(Hodiny.id.desc()).all()
    return render_template("hodiny.html", zaznamy=zaznamy, user=u)

@app.route("/hodiny/reset-akce/<int:akce_id>", methods=["POST"])
@login_required
@require_role("admin", "manager")
def hodiny_reset_akce(akce_id):
    Hodiny.query.filter_by(akce_id=akce_id).delete()
    db.session.commit()
    flash("Hodiny pro akci smazány.", "success")
    return redirect(url_for("hodiny_overview"))

@app.route("/hodiny/reset-vse", methods=["POST"])
@login_required
@require_role("admin")
def hodiny_reset_vse():
    Hodiny.query.delete()
    db.session.commit()
    flash("Všechny hodiny smazány.", "success")
    return redirect(url_for("hodiny_overview"))

# -----------------------------------------------------------------------------
# CHECKLIST PDF (s “okýnky”)
# -----------------------------------------------------------------------------
@app.route("/akce/<int:id>/checklist.pdf")
@login_required
def akce_checklist_pdf(id):
    a = Akce.query.get_or_404(id)
    pdf_filename = f"checklist_akce_{a.id}.pdf"
    c = canvas.Canvas(pdf_filename, pagesize=A4)
    w, h = A4

    # logo
    logo = os.path.join(os.getcwd(), "static", "logo.png")
    if os.path.exists(logo):
        c.drawImage(logo, 20*mm, h-35*mm, width=45*mm, preserveAspectRatio=True, mask="auto")

    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(w/2, h-30*mm, "Nakládací checklist")

    c.setFont("Helvetica", 11)
    c.drawString(20*mm, h-45*mm, f"Název: {a.nazev}")
    c.drawString(20*mm, h-50*mm, f"Datum: {a.datum}   Čas: {(a.cas_od or '')}-{(a.cas_do or '')}")
    c.drawString(20*mm, h-55*mm, f"Místo: {a.misto}")

    y = h - 72*mm
    c.setLineWidth(0.6)
    c.line(20*mm, y, w-20*mm, y); y -= 6*mm
    c.setFont("Helvetica-Bold", 11)
    c.drawString(22*mm, y, "☐")               # “okýnko” (tiskem to vypadá jako čtvereček)
    c.drawString(30*mm, y, "Produkt")
    c.drawRightString(w-70*mm, y, "Požad.")
    c.drawRightString(w-40*mm, y, "Nab.")
    c.drawRightString(w-20*mm, y, "Zbývá")
    y -= 3*mm
    c.line(20*mm, y, w-20*mm, y); y -= 5*mm
    c.setFont("Helvetica", 11)

    polozky = db.session.query(AkceProdukt, Produkt).join(Produkt, AkceProdukt.produkt_id==Produkt.id)\
             .filter(AkceProdukt.akce_id==a.id).all()
    for ap, p in polozky:
        if y < 30*mm:
            c.showPage(); y = h - 20*mm; c.setFont("Helvetica", 11)
        # “okýnko”
        c.rect(22*mm, y-3*mm, 4*mm, 4*mm, stroke=1, fill=0)
        c.drawString(30*mm, y, p.nazev.upper())
        c.drawRightString(w-70*mm, y, f"{int(ap.mnozstvi)} {p.jednotka}")
        c.drawRightString(w-40*mm, y, "0")
        c.drawRightString(w-20*mm, y, f"{int(ap.mnozstvi)}")
        y -= 6*mm

    y -= 10*mm
    c.line(25*mm, y, 95*mm, y); c.drawString(25*mm, y-5*mm, "Zodpovědná osoba – podpis")
    c.line(115*mm, y, w-25*mm, y); c.drawString(115*mm, y-5*mm, "Kontrola – podpis")

    c.save()
    return send_file(pdf_filename, as_attachment=True)

# -----------------------------------------------------------------------------
# PRODUKTY & SKLAD
# -----------------------------------------------------------------------------
@app.route("/produkty")
@login_required
def produkty():
    produkty_list = Produkt.query.order_by(Produkt.skupina, Produkt.nazev).all()
    return render_template("produkty.html", produkty=produkty_list, skupiny=SKUPINY, user=current_user())

@app.route("/produkt/edit/<int:id>", methods=["GET", "POST"])
@login_required
@require_role("admin", "manager")
def edit_produkt(id):
    if id == 0:
        p = Produkt(nazev="", jednotka="ks", skupina="")
    else:
        p = Produkt.query.get_or_404(id)
    if request.method == "POST":
        p.nazev = request.form["nazev"].strip()
        p.jednotka = request.form.get("jednotka", "ks").strip()
        p.skupina = request.form.get("skupina", "").strip()
        if id == 0:
            db.session.add(p)
        db.session.commit()
        flash("Produkt uložen.", "success")
        return redirect(url_for("produkty"))
    return render_template("edit_produkt.html", produkt=p, skupiny=SKUPINY)

@app.route("/produkt/delete/<int:id>", methods=["POST"])
@login_required
@require_role("admin", "manager")
def delete_produkt(id):
    used_sklad = Sklad.query.filter_by(produkt_id=id).first()
    used_ap = AkceProdukt.query.filter_by(produkt_id=id).first()
    if used_sklad or used_ap:
        flash("Produkt nelze smazat – je použit v akci nebo má skladové pohyby.", "error")
        return redirect(url_for("produkty"))
    p = Produkt.query.get_or_404(id)
    db.session.delete(p); db.session.commit()
    flash("Produkt smazán.", "success")
    return redirect(url_for("produkty"))

@app.route("/sklad")
@login_required
def sklad():
    produkty_list = Produkt.query.order_by(Produkt.skupina, Produkt.nazev).all()
    stav = {p.id: stav_skladu(p.id) for p in produkty_list}
    return render_template("sklad.html", produkty=produkty_list, stav=stav, skupiny=SKUPINY, user=current_user())

@app.route("/naskladnit", methods=["GET", "POST"])
@login_required
@require_role("admin", "manager")
def naskladnit():
    produkty_list = Produkt.query.order_by(Produkt.skupina, Produkt.nazev).all()
    if request.method == "POST":
        produkt_id = int(request.form["produkt_id"])
        mnozstvi = float(request.form["mnozstvi"])
        db.session.add(Sklad(produkt_id=produkt_id, typ="naskladneni", mnozstvi=mnozstvi))
        db.session.commit()
        flash("Naskladněno.", "success")
        return redirect(url_for("sklad"))
    return render_template("naskladnit.html", produkty=produkty_list)

@app.route("/vyskladnit", methods=["GET", "POST"])
@login_required
@require_role("admin", "manager")
def vyskladnit():
    produkty_list = Produkt.query.order_by(Produkt.skupina, Produkt.nazev).all()
    akce_list = Akce.query.order_by(Akce.vytvoreno.desc()).all()
    if request.method == "POST":
        produkt_id = int(request.form["produkt_id"])
        akce_id = int(request.form.get("akce_id") or 0)
        mnozstvi = float(request.form["mnozstvi"])
        db.session.add(Sklad(produkt_id=produkt_id, akce_id=akce_id or None, typ="vyskladneni", mnozstvi=mnozstvi))
        db.session.commit()
        flash("Vyskladněno.", "success")
        return redirect(url_for("sklad"))
    return render_template("vyskladnit.html", produkty=produkty_list, akce_list=akce_list)

# -----------------------------------------------------------------------------
# ZAMĚSTNANCI – list + změna hesla (jen admin)
# -----------------------------------------------------------------------------
@app.route("/zamestnanci")
@login_required
@require_role("admin", "manager")  # list může vidět i manager
def zamestnanci():
    users = User.query.order_by(User.role.desc(), User.username.asc()).all()
    return render_template("zamestnanci.html", users=users, user=current_user())

@app.route("/zamestnanci/heslo/<int:user_id>", methods=["GET", "POST"])
@login_required
@require_role("admin")
def zamestnanci_heslo(user_id):
    u = User.query.get_or_404(user_id)
    if request.method == "POST":
        new_pw = request.form.get("password", "").strip()
        if len(new_pw) < 4:
            flash("Heslo musí mít aspoň 4 znaky.", "error")
        else:
            u.set_password(new_pw)
            db.session.commit()
            flash("Heslo změněno.", "success")
            return redirect(url_for("zamestnanci"))
    return render_template("zamestnanci_heslo.html", z=u)

# -----------------------------------------------------------------------------
# Export přehledu akcí (PDF)
# -----------------------------------------------------------------------------
@app.route("/export_pdf")
@login_required
def export_pdf():
    akce = Akce.query.order_by(Akce.vytvoreno.desc()).all()
    pdf_filename = "prehled_akci.pdf"
    c = canvas.Canvas(pdf_filename, pagesize=A4)
    w, h = A4

    logo = os.path.join(os.getcwd(), "static", "logo.png")
    if os.path.exists(logo):
        c.drawImage(logo, 20*mm, h-35*mm, width=45*mm, preserveAspectRatio=True, mask="auto")
    c.setFont("Helvetica-Bold", 18)
    c.drawCentredString(w/2, h-30*mm, "Přehled akcí – Ozvučení")

    y = h - 45*mm
    c.setFont("Helvetica", 12)
    for a in akce:
        if y < 25*mm:
            c.showPage(); y = h - 20*mm; c.setFont("Helvetica", 12)
        c.drawString(20*mm, y, f"Název: {a.nazev}")
        c.drawString(20*mm, y-5*mm, f"Datum: {a.datum}  Čas: {(a.cas_od or '')}-{(a.cas_do or '')}")
        c.drawString(20*mm, y-10*mm, f"Místo: {a.misto}")
        if a.poznamka:
            c.drawString(20*mm, y-15*mm, f"Poznámka: {a.poznamka}")
            y -= 25*mm
        else:
            y -= 20*mm

    c.save()
    return send_file(pdf_filename, as_attachment=True)

# -----------------------------------------------------------------------------
# DEV server (Render používá gunicorn / Procfile)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
