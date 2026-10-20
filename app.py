import base64
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

load_dotenv()


def resolve_database_uri():
    """Neon on Vercel injects DATABASE_URL (pooled). Local dev falls back to SQLite."""
    for key in ("DATABASE_URL", "POSTGRES_URL"):
        raw = os.getenv(key, "").strip()
        if not raw:
            continue
        if raw.startswith("postgres://"):
            raw = "postgresql://" + raw[len("postgres://") :]
        if raw.startswith("postgresql://"):
            return "postgresql+psycopg://" + raw[len("postgresql://") :]
        return raw
    if os.getenv("VERCEL"):
        raise RuntimeError(
            "DATABASE_URL is required on Vercel. Install Neon from the Vercel Marketplace "
            "or set DATABASE_URL to your Neon pooled connection string."
        )
    return "sqlite:///smart_bloodlink.db"


_database_uri = resolve_database_uri()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-this-secret-before-deployment")
app.config["SQLALCHEMY_DATABASE_URI"] = _database_uri
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
if _database_uri.startswith("postgresql"):
    # ponytail: one pool per serverless instance; Neon pooled URL + pre_ping avoids stale conns
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 300}
db = SQLAlchemy(app)

BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]

def normalize_phone_e164(phone: str) -> str:
    """Default India (+91) when users enter a 10-digit local mobile."""
    raw = phone.strip()
    if not raw:
        return ""
    compact = raw.replace(" ", "").replace("-", "")
    country = os.getenv("DEFAULT_PHONE_COUNTRY", "91").strip().lstrip("+") or "91"
    if compact.startswith("+"):
        digits = "".join(c for c in compact[1:] if c.isdigit())
        return f"+{digits}" if digits else compact
    digits = "".join(c for c in compact if c.isdigit())
    if not digits:
        return raw
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10:
        return f"+{country}{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"+{country}{digits[1:]}"
    if digits.startswith(country):
        return f"+{digits}"
    return f"+{digits}"

class Hospital(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    city = db.Column(db.String(80), nullable=False, default="Mumbai")
    phone = db.Column(db.String(30), nullable=False)
    email = db.Column(db.String(120), default="")
    verified = db.Column(db.Boolean, default=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    stocks = db.relationship("BloodStock", backref="hospital", cascade="all, delete-orphan")

class BloodStock(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    hospital_id = db.Column(db.Integer, db.ForeignKey("hospital.id"), nullable=False)
    blood_group = db.Column(db.String(5), nullable=False)
    units = db.Column(db.Integer, nullable=False, default=0)
    __table_args__ = (db.UniqueConstraint("hospital_id", "blood_group", name="unique_hospital_blood_group"),)

class BloodRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    patient_name = db.Column(db.String(120), nullable=False)
    blood_group = db.Column(db.String(5), nullable=False)
    units = db.Column(db.Integer, nullable=False, default=1)
    city = db.Column(db.String(80), nullable=False, default="Mumbai")
    contact_phone = db.Column(db.String(30), nullable=False)
    urgency = db.Column(db.String(20), nullable=False, default="Urgent")
    status = db.Column(db.String(30), nullable=False, default="Open")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class AlertLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("blood_request.id"), nullable=False)
    hospital_id = db.Column(db.Integer, db.ForeignKey("hospital.id"), nullable=True)
    hospital_name = db.Column(db.String(120), nullable=False)
    result = db.Column(db.String(40), nullable=False)
    details = db.Column(db.String(255), default="")
    call_sid = db.Column(db.String(64), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    blood_request = db.relationship("BloodRequest", backref="alerts")

def twilio_from_number():
    """Single outbound Caller ID — all voice calls use TWILIO_PHONE_NUMBER from env."""
    return normalize_phone_e164(os.getenv("TWILIO_PHONE_NUMBER", "").strip())

def twilio_credentials():
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    return sid, token, twilio_from_number()

def twilio_rest_client():
    """Use API key (SK…) if set; otherwise Account SID + primary Auth Token."""
    from twilio.rest import Client
    account_sid, auth_token, _ = twilio_credentials()
    api_key = os.getenv("TWILIO_API_KEY_SID", "").strip()
    api_secret = os.getenv("TWILIO_API_KEY_SECRET", "").strip()
    if api_key and api_secret:
        return Client(api_key, api_secret, account_sid)
    return Client(account_sid, auth_token)

def public_base_url():
    url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not url:
        vercel_host = os.getenv("VERCEL_URL", "").strip()
        if vercel_host:
            url = f"https://{vercel_host}".rstrip("/")
    return url

# Twilio Console “Try out Voice” templates (allowed on trial accounts).
TWILIO_TRIAL_VOICE_TEMPLATE = (
    "https://webhooks.twilio.com/v1/Voice/Template/voice_speech_recognition"
)

def twilio_use_trial_voice_flag():
    return os.getenv("TWILIO_USE_TRIAL_VOICE", "").strip().lower() in ("1", "true", "yes")

def twilio_use_production_voice():
    """Full /voice webhook with press 1/2 — requires paid Twilio + working HTTPS webhooks."""
    return os.getenv("TWILIO_VOICE_MODE", "").strip().lower() in ("production", "full", "paid")

def should_use_trial_script():
    """Default on Vercel/trial unless TWILIO_VOICE_MODE=production (avoids broken /voice URL fetches)."""
    if twilio_use_production_voice():
        return False
    script_flag = os.getenv("TWILIO_TRIAL_SCRIPT", "").strip().lower()
    if script_flag in ("0", "false", "no"):
        return False
    explicit_url = os.getenv("TWILIO_TRIAL_VOICE_URL", "").strip()
    if explicit_url and script_flag not in ("1", "true", "yes") and not twilio_use_trial_voice_flag():
        return False
    return True

def twilio_trial_script_enabled():
    return should_use_trial_script()

def twilio_trial_voice_url():
    """Twilio-hosted demo URL when trial mode is on but inline script is off."""
    if not twilio_use_trial_voice_flag() or twilio_trial_script_enabled():
        return ""
    explicit = os.getenv("TWILIO_TRIAL_VOICE_URL", "").strip()
    if explicit:
        return explicit
    return TWILIO_TRIAL_VOICE_TEMPLATE

def twilio_outbound_voice_url(request_id, hospital_id, public_url):
    if should_use_trial_script() and public_url:
        return trial_voice_webhook_url(request_id, hospital_id, public_url)
    trial = twilio_trial_voice_url()
    if trial:
        return trial
    return f"{public_url}/voice/{request_id}/{hospital_id}"

def voice_xml_response(twiml_body):
    from flask import Response
    return Response(twiml_body, mimetype="text/xml; charset=utf-8")

def blood_request_voice_intro(blood_request, hospital, include_keypad=True):
    intro = (
        f"Hello {hospital.name}. This is Smart BloodLink with an urgent blood request. "
        f"We need {blood_request.units} units of {blood_request.blood_group} blood in {blood_request.city}. "
        f"Requester contact number is {blood_request.contact_phone}."
    )
    if include_keypad:
        return intro + " If you can supply this blood, press 1. If not available, press 2."
    return intro + " Please check availability and contact the requester. Goodbye."

def build_trial_alert_twiml(blood_request, hospital):
    from xml.sax.saxutils import escape
    spoken = blood_request_voice_intro(blood_request, hospital, include_keypad=False)
    return f"<Response><Say voice=\"alice\">{escape(spoken)}</Say></Response>"

def trial_voice_webhook_url(request_id, hospital_id, public_url):
    return f"{public_url}/voice/trial/{request_id}/{hospital_id}"

def twilio_calls_enabled():
    sid, token, number = twilio_credentials()
    api_key = os.getenv("TWILIO_API_KEY_SID", "").strip()
    api_secret = os.getenv("TWILIO_API_KEY_SECRET", "").strip()
    has_api_auth = bool(api_key and api_secret)
    has_token_auth = bool(token)
    has_webhooks = bool(public_base_url())
    return bool(sid and (has_api_auth or has_token_auth) and number)

def twilio_call_error_detail(exc, to_e164=""):
    """Short, actionable message for alert history (raw Twilio errors are long)."""
    raw = str(exc)
    lower = raw.lower()
    dest = to_e164 or "this number"
    india_dest = dest.startswith("+91")
    if "from' number isn't assigned" in lower or "assigned to this verified voice recipient" in lower:
        return (
            "Twilio trial: From/To pairing rejected. Use TWILIO_PHONE_NUMBER that matches your trial "
            "(or upgrade). For +91 hospitals on a US trial, Voice to India usually requires upgrade."
        )[:255]
    if (
        india_dest
        or "within your country" in lower
        or "invalid to phone number for trial" in lower
        or "14111" in raw
    ):
        return (
            f"Twilio trial Voice to {dest}: US trial accounts usually cannot call India (+91), even when "
            "the number is in Verified Caller IDs. Upgrade Twilio, or test with a verified US +1 number."
        )[:255]
    if "verified recipient" in lower or "trial phone number is assigned" in lower:
        if india_dest:
            return (
                f"{dest} is verified in Console, but US trial Voice often cannot dial +91. Upgrade the account "
                "or alert a verified US test number."
            )[:255]
        return (
            f"Twilio trial: could not call {dest}. Add the exact E.164 in Verified Caller IDs "
            "(e.g. +919325427119), matching the hospital record."
        )[:255]
    if "limited parameter access" in lower or "disallowed parameters" in lower:
        hint = (
            "Set TWILIO_USE_TRIAL_VOICE=1 (and TWILIO_TRIAL_SCRIPT=1 for the spoken blood-request message), "
            "or upgrade the account to use full /voice keypad responses."
        )
        if not twilio_use_trial_voice_flag():
            return f"Twilio trial blocked call parameters. {hint}"[:255]
        return (
            "Twilio trial blocked this call. Verify the recipient number and call within your trial region."
        )[:255]
    return raw[:255]

def twilio_voice_webhook_ok():
    # ponytail: Vercel + trial Voice — signature URL often mismatches; Reject → “check your URL” on the call
    if request.path.startswith("/voice/") and not twilio_use_production_voice():
        return True
    return twilio_request_is_valid()

def _twilio_http_auth():
    account_sid, auth_token, _ = twilio_credentials()
    api_key = os.getenv("TWILIO_API_KEY_SID", "").strip()
    api_secret = os.getenv("TWILIO_API_KEY_SECRET", "").strip()
    if api_key and api_secret:
        return account_sid, (api_key, api_secret)
    return account_sid, (account_sid, auth_token)

def _trial_call_create_without_from(to, **kwargs):
    """Voice trial API allows To + Url/Twiml without From; Python SDK does not."""
    account_sid, auth = _twilio_http_auth()
    form = [("To", to)]
    if kwargs.get("url"):
        form.append(("Url", kwargs["url"]))
    if kwargs.get("twiml"):
        form.append(("Twiml", kwargs["twiml"]))
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json",
        data=body,
        method="POST",
    )
    token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode(errors="replace")
        raise RuntimeError(err_body or str(exc)) from exc
    sid = payload.get("sid", "")
    return type("TwilioCall", (), {"sid": sid})()

def create_outbound_call(client, to, from_, **kwargs):
    from_num = (from_ or twilio_from_number()).strip()
    if twilio_use_production_voice():
        if not from_num:
            raise RuntimeError("Set TWILIO_PHONE_NUMBER to your Twilio voice number in E.164 format.")
        return client.calls.create(to=to, from_=from_num, **kwargs)
    try:
        return _trial_call_create_without_from(to, **kwargs)
    except Exception:
        if not from_num:
            raise RuntimeError(
                "Trial outbound failed. Set TWILIO_PHONE_NUMBER to your Twilio or verified Caller ID, or upgrade."
            )
        return client.calls.create(to=to, from_=from_num, **kwargs)

def place_trial_script_call(client, to, from_, public_url, request_id, hospital_id, twiml_body):
    """Blood-request audio without the full /voice keypad flow (works on Twilio trial)."""
    attempts = []
    if public_url:
        trial_url = trial_voice_webhook_url(request_id, hospital_id, public_url)
        attempts.append(
            ("app_webhook", lambda: create_outbound_call(client, to, from_, url=trial_url))
        )
    attempts.append(
        ("inline_twiml", lambda: create_outbound_call(client, to, from_, twiml=twiml_body))
    )
    errors = []
    for label, create in attempts:
        try:
            return create(), label
        except Exception as exc:
            errors.append(f"{label}: {exc}")
    raise RuntimeError(
        "Trial blood-request script failed. "
        + "; ".join(errors)[:400]
        + " Ensure the latest app is deployed and PUBLIC_BASE_URL matches your Vercel URL."
    )

def place_outbound_call(client, to, from_, voice_url, public_url, twiml=None):
    """Trial accounts reject extra Call.create params."""
    if twiml:
        return create_outbound_call(client, to, from_, twiml=twiml)
    if voice_url.startswith("https://webhooks.twilio.com/"):
        return create_outbound_call(client, to, from_, url=voice_url)
    try:
        return create_outbound_call(client, to, from_, url=voice_url)
    except Exception as exc:
        msg = str(exc).lower()
        if "limited parameter access" not in msg and "disallowed parameters" not in msg:
            raise
        return create_outbound_call(
            client, to, from_, url=voice_url, status_callback=f"{public_url}/call-status"
        )

def twilio_request_is_valid():
    """Validate X-Twilio-Signature when Auth Token is configured."""
    _, token, _ = twilio_credentials()
    if not token:
        return True
    from twilio.request_validator import RequestValidator
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        return False
    validator = RequestValidator(token)
    # Behind reverse proxies, request.url may differ from the URL Twilio called.
    callback_url = public_base_url()
    if callback_url:
        path = request.full_path.rstrip("?") if request.query_string else request.path
        url = f"{callback_url}{path}"
    else:
        url = request.url
    if request.method == "GET":
        flat = request.args.to_dict(flat=True)
    else:
        params = request.form.to_dict(flat=False)
        flat = {k: (v[0] if len(v) == 1 else v) for k, v in params.items()}
    return validator.validate(url, flat, signature)

def seed_demo_data():
    if Hospital.query.count():
        return
    demo_hospitals = [
        ("Dishant Bhoir", "Mumbai", "+919325427119", "demo-care@example.com", False),
        ("Asmi Chande", "Mumbai", "+919167102509", "demo-lifeline@example.com", False),
        ("Akshay More", "Navi Mumbai", "+917304355117", "demo-greencross@example.com", False),
    ]
    for name, city, phone, email, verified in demo_hospitals:
        h = Hospital(name=name, city=city, phone=phone, email=email, verified=verified)
        db.session.add(h)
        db.session.flush()
        values = {"A+": 6, "A-": 2, "B+": 8, "B-": 1, "AB+": 3, "AB-": 0, "O+": 10, "O-": 2}
        for group, units in values.items():
            db.session.add(BloodStock(hospital_id=h.id, blood_group=group, units=units))
    db.session.commit()

@app.route("/")
def index():
    hospitals = Hospital.query.order_by(Hospital.name).all()
    requests_list = BloodRequest.query.order_by(BloodRequest.created_at.desc()).limit(8).all()
    low_stock = BloodStock.query.filter(BloodStock.units <= 2).all()
    total_units = db.session.query(db.func.sum(BloodStock.units)).scalar() or 0
    stats = {
        "hospitals": Hospital.query.count(),
        "open_requests": BloodRequest.query.filter_by(status="Open").count(),
        "low_stock": len(low_stock),
        "units": total_units,
    }
    alert_logs = AlertLog.query.order_by(AlertLog.created_at.desc()).limit(12).all()
    return render_template(
        "index.html",
        hospitals=hospitals,
        requests=requests_list,
        low_stock=low_stock,
        stats=stats,
        blood_groups=BLOOD_GROUPS,
        alert_logs=alert_logs,
        real_calls_enabled=twilio_calls_enabled(),
        trial_voice_mode=twilio_use_trial_voice_flag() and not should_use_trial_script(),
        trial_script_mode=should_use_trial_script(),
    )

@app.route("/hospital/add", methods=["POST"])
def add_hospital():
    name = request.form.get("name", "").strip()
    city = request.form.get("city", "").strip()
    phone = request.form.get("phone", "").strip()
    email = request.form.get("email", "").strip()
    phone = normalize_phone_e164(phone)
    if not name or not city or not phone:
        flash("Please enter hospital name, city and phone number.", "error")
        return redirect(url_for("index"))
    hospital = Hospital(name=name, city=city, phone=phone, email=email, verified=False)
    db.session.add(hospital)
    db.session.flush()
    for group in BLOOD_GROUPS:
        db.session.add(BloodStock(hospital_id=hospital.id, blood_group=group, units=0))
    db.session.commit()
    flash("Hospital added. Its contact details must be verified before real alerts are enabled.", "success")
    return redirect(url_for("index"))

@app.route("/hospital/<int:hospital_id>/verify", methods=["POST"])
def verify_hospital(hospital_id):
    expected = os.getenv("ADMIN_VERIFY_KEY", "").strip()
    provided = request.form.get("admin_key", "")
    if not expected:
        flash("Hospital verification is disabled. Set ADMIN_VERIFY_KEY in .env first.", "error")
        return redirect(url_for("index"))
    if not secrets.compare_digest(provided, expected):
        flash("Invalid administrator key.", "error")
        return redirect(url_for("index"))
    hospital = db.session.get(Hospital, hospital_id)
    if not hospital:
        flash("Hospital not found.", "error")
        return redirect(url_for("index"))
    hospital.verified = True
    db.session.commit()
    flash(f"{hospital.name} marked as verified for outbound alerts.", "success")
    return redirect(url_for("index"))

@app.route("/stock/update", methods=["POST"])
def update_stock():
    try:
        hospital_id = int(request.form.get("hospital_id", ""))
        units = int(request.form.get("units", ""))
    except ValueError:
        flash("Please choose a hospital and enter a valid unit count.", "error")
        return redirect(url_for("index"))
    group = request.form.get("blood_group", "")
    if group not in BLOOD_GROUPS or units < 0 or units > 10000:
        flash("Enter a valid blood group and a non-negative unit count.", "error")
        return redirect(url_for("index"))
    stock = BloodStock.query.filter_by(hospital_id=hospital_id, blood_group=group).first()
    if not stock:
        flash("Stock record not found.", "error")
        return redirect(url_for("index"))
    stock.units = units
    db.session.commit()
    flash(f"{group} stock updated successfully.", "success")
    return redirect(url_for("index"))

@app.route("/request/add", methods=["POST"])
def add_request():
    name = request.form.get("patient_name", "").strip()
    group = request.form.get("blood_group", "")
    city = request.form.get("city", "").strip()
    phone = normalize_phone_e164(request.form.get("contact_phone", "").strip())
    urgency = request.form.get("urgency", "Urgent")
    try:
        units = int(request.form.get("units", "1"))
    except ValueError:
        units = 0
    if not name or not city or not phone or group not in BLOOD_GROUPS or not 1 <= units <= 20:
        flash("Please complete all request fields with valid values.", "error")
        return redirect(url_for("index"))
    blood_request = BloodRequest(patient_name=name, blood_group=group, units=units,
                                 city=city, contact_phone=phone, urgency=urgency)
    db.session.add(blood_request)
    db.session.commit()
    flash(f"Blood request #{blood_request.id} created. Select a hospital and use “Alert hospital” to notify.", "success")
    return redirect(url_for("index"))

@app.route("/request/<int:request_id>/alert", methods=["POST"])
def alert_hospitals(request_id):
    blood_request = db.session.get(BloodRequest, request_id)
    if not blood_request:
        flash("Blood request not found.", "error")
        return redirect(url_for("index"))

    try:
        hospital_id = int(request.form.get("hospital_id", ""))
    except ValueError:
        flash("Please select a hospital to alert.", "error")
        return redirect(url_for("index"))

    hospital = db.session.get(Hospital, hospital_id)
    if not hospital:
        flash("Hospital not found.", "error")
        return redirect(url_for("index"))

    _, _, twilio_number = twilio_credentials()
    public_url = public_base_url()
    real_calls_enabled = twilio_calls_enabled()

    stock = BloodStock.query.filter_by(
        hospital_id=hospital.id, blood_group=blood_request.blood_group
    ).first()
    stock_units = stock.units if stock else 0
    called = False

    if real_calls_enabled and hospital.verified:
        to_phone = normalize_phone_e164(hospital.phone)
        try:
            client = twilio_rest_client()
            if twilio_trial_script_enabled():
                twiml_body = build_trial_alert_twiml(blood_request, hospital)
                call, delivery = place_trial_script_call(
                    client,
                    to_phone,
                    twilio_number,
                    public_url,
                    blood_request.id,
                    hospital.id,
                    twiml_body,
                )
                if delivery == "app_webhook":
                    call_details = (
                        "Trial script via /voice/trial — blood-request message spoken "
                        "(no press-1/2 on trial)."
                    )
                else:
                    call_details = (
                        "Trial script via inline TwiML — blood-request message spoken "
                        "(no press-1/2 on trial)."
                    )
            else:
                voice_url = twilio_outbound_voice_url(
                    blood_request.id, hospital.id, public_url
                )
                call = place_outbound_call(
                    client,
                    to_phone,
                    twilio_number,
                    voice_url,
                    public_url,
                )
                call_details = None
            if call_details is None:
                if twilio_trial_voice_url():
                    call_details = (
                        "Twilio Console demo template (generic script). "
                        "Set TWILIO_TRIAL_SCRIPT=1 and redeploy for the blood-request message."
                    )
                else:
                    call_details = (
                        "Production voice — awaiting keypad response (requires TWILIO_VOICE_MODE=production "
                        "and working /voice webhooks)."
                    )
            db.session.add(AlertLog(
                request_id=blood_request.id,
                hospital_id=hospital.id,
                hospital_name=hospital.name,
                result="Call placed",
                details=call_details,
                call_sid=call.sid,
            ))
            called = True
        except Exception as exc:
            detail = twilio_call_error_detail(exc, to_e164=to_phone)
            db.session.add(
                AlertLog(
                    request_id=blood_request.id,
                    hospital_name=hospital.name,
                    result="Call failed",
                    details=detail,
                )
            )
    else:
        result = "Demo alert"
        if real_calls_enabled and not hospital.verified:
            details = (
                f"Hospital not verified — no call placed. "
                f"Inventory listed: {stock_units} unit(s) of {blood_request.blood_group}. "
                f"Use “Verify hospital” in the network section (requires ADMIN_VERIFY_KEY)."
            )
        else:
            details = (
                f"Inventory listed: {stock_units} unit(s) of {blood_request.blood_group}. "
                f"No call placed (Twilio or public URL not configured)."
            )
        db.session.add(AlertLog(
            request_id=blood_request.id,
            hospital_name=hospital.name,
            result=result,
            details=details,
        ))
    db.session.commit()
    if real_calls_enabled:
        if called:
            flash(
                f"Call placed to {hospital.name}. Check alert history and confirm the response manually.",
                "success",
            )
        elif hospital.verified:
            flash(f"Call to {hospital.name} failed. See alert history for details.", "error")
        else:
            flash(
                f"{hospital.name} is not verified for outbound calls. "
                "Verify the hospital in the network section, then try again.",
                "error",
            )
    else:
        flash(
            f"Demo alert logged for {hospital.name}. No real phone call was placed.",
            "success",
        )
    return redirect(url_for("index"))

@app.route("/voice/trial/<int:request_id>/<int:hospital_id>", methods=["GET", "POST"])
def voice_trial_script(request_id, hospital_id):
    if not twilio_voice_webhook_ok():
        return voice_xml_response("<Response><Reject/></Response>")
    blood_request = db.session.get(BloodRequest, request_id)
    hospital = db.session.get(Hospital, hospital_id)
    if not blood_request or not hospital:
        return voice_xml_response(
            "<Response><Say voice=\"alice\">Smart BloodLink request not found. Goodbye.</Say></Response>"
        )
    return voice_xml_response(build_trial_alert_twiml(blood_request, hospital))

@app.route("/voice/<int:request_id>/<int:hospital_id>", methods=["GET", "POST"])
def voice_prompt(request_id, hospital_id):
    from xml.sax.saxutils import escape
    if not twilio_voice_webhook_ok():
        return voice_xml_response("<Response><Reject/></Response>")
    blood_request = db.session.get(BloodRequest, request_id)
    hospital = db.session.get(Hospital, hospital_id)
    if not blood_request or not hospital:
        return voice_xml_response(
            "<Response><Say voice=\"alice\">Request not found. Goodbye.</Say></Response>"
        )
    if not twilio_use_production_voice():
        return voice_xml_response(build_trial_alert_twiml(blood_request, hospital))
    base = public_base_url()
    respond_url = f"{base}/voice/{request_id}/{hospital_id}/respond"
    intro = blood_request_voice_intro(blood_request, hospital, include_keypad=True)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Gather numDigits="1" action="{escape(respond_url)}" method="POST" timeout="8">'
        f'<Say voice="alice">{escape(intro)}</Say>'
        "</Gather>"
        "<Say>We did not receive a response. Please call the requesting hospital directly. Goodbye.</Say>"
        "</Response>"
    )
    return voice_xml_response(xml)

@app.route("/voice/<int:request_id>/<int:hospital_id>/respond", methods=["POST"])
def voice_respond(request_id, hospital_id):
    from xml.sax.saxutils import escape
    if not twilio_voice_webhook_ok():
        return voice_xml_response("<Response><Reject/></Response>")
    digit = request.form.get("Digits", "")
    call_sid = request.form.get("CallSid", "")
    blood_request = db.session.get(BloodRequest, request_id)
    hospital = db.session.get(Hospital, hospital_id)
    log = None
    if call_sid:
        log = AlertLog.query.filter_by(call_sid=call_sid).first()
    if not log:
        log = AlertLog.query.filter_by(request_id=request_id, hospital_id=hospital_id).order_by(
            AlertLog.created_at.desc()
        ).first()
    if digit == "1":
        reply = "Thank you. Your response is recorded as available. A coordinator may call you shortly."
        if log:
            log.result = "Available"
            log.details = f"Hospital pressed 1 (can supply {blood_request.blood_group if blood_request else 'blood'})"
    elif digit == "2":
        reply = "Thank you. Your response is recorded as not available. Goodbye."
        if log:
            log.result = "Not available"
            log.details = "Hospital pressed 2 (cannot supply)"
    else:
        reply = "Invalid input. Please contact the requesting hospital directly. Goodbye."
        if log:
            log.result = "No valid response"
            log.details = f"Unexpected keypad input: {digit or 'none'}"
    if log:
        db.session.commit()
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="alice">{escape(reply)}</Say></Response>'
    return voice_xml_response(xml)

@app.route("/voice/health", methods=["GET", "POST"])
def voice_health():
    """Twilio can hit this to confirm voice webhooks return valid TwiML."""
    return voice_xml_response(
        '<Response><Say voice="alice">Smart BloodLink voice webhooks are working.</Say></Response>'
    )

@app.route("/call-status", methods=["POST"])
def call_status():
    if not twilio_request_is_valid():
        return ("Forbidden", 403)
    status = request.form.get("CallStatus", "unknown")
    sid = request.form.get("CallSid", "")
    log = AlertLog.query.filter_by(call_sid=sid).first() if sid else None
    if log and status in {"busy", "failed", "no-answer", "canceled"}:
        log.result = "Call failed"
        log.details = f"Twilio status: {status}"
        db.session.commit()
    return ("", 204)

@app.route("/api/summary")
def api_summary():
    return jsonify({
        "hospitals": Hospital.query.count(),
        "open_requests": BloodRequest.query.filter_by(status="Open").count(),
        "low_stock": BloodStock.query.filter(BloodStock.units <= 2).count(),
        "total_units": db.session.query(db.func.sum(BloodStock.units)).scalar() or 0
    })

@app.route("/api/twilio-status")
def api_twilio_status():
    """Debug deployed Twilio voice settings (no secrets)."""
    from_num = twilio_from_number()
    tail = from_num[-4:] if len(from_num) >= 4 else ""
    return jsonify({
        "calls_configured": twilio_calls_enabled(),
        "trial_script": should_use_trial_script(),
        "production_voice": twilio_use_production_voice(),
        "configured_from_last4": tail,
        "sends_from_on_trial": bool(twilio_from_number()),
        "public_base_host": (public_base_url() or "").replace("https://", "").split("/")[0],
        "trial_note": (
            "TWILIO_PHONE_NUMBER must match the Twilio number linked to each verified recipient on trial. "
            "US trial cannot call +91 — upgrade for India."
        ),
    })

def ensure_alert_log_columns():
    """create_all does not add new columns on existing databases."""
    from sqlalchemy import inspect, text
    if not inspect(db.engine).has_table("alert_log"):
        return
    cols = {c["name"] for c in inspect(db.engine).get_columns("alert_log")}
    dialect = db.engine.dialect.name
    additions = []
    if "hospital_id" not in cols:
        additions.append(
            "ALTER TABLE alert_log ADD COLUMN IF NOT EXISTS hospital_id INTEGER"
            if dialect == "postgresql"
            else "ALTER TABLE alert_log ADD COLUMN hospital_id INTEGER"
        )
    if "call_sid" not in cols:
        additions.append(
            "ALTER TABLE alert_log ADD COLUMN IF NOT EXISTS call_sid VARCHAR(64) DEFAULT ''"
            if dialect == "postgresql"
            else "ALTER TABLE alert_log ADD COLUMN call_sid VARCHAR(64) DEFAULT ''"
        )
    for stmt in additions:
        db.session.execute(text(stmt))
    if additions:
        db.session.commit()

def normalize_stored_phone_numbers():
    """One-time fixup on boot for numbers saved without +91."""
    changed = False
    for hospital in Hospital.query.all():
        normalized = normalize_phone_e164(hospital.phone)
        if normalized and normalized != hospital.phone:
            hospital.phone = normalized
            changed = True
    for blood_request in BloodRequest.query.all():
        normalized = normalize_phone_e164(blood_request.contact_phone)
        if normalized and normalized != blood_request.contact_phone:
            blood_request.contact_phone = normalized
            changed = True
    if changed:
        db.session.commit()

with app.app_context():
    db.create_all()
    ensure_alert_log_columns()
    seed_demo_data()
    normalize_stored_phone_numbers()

if __name__ == "__main__":
    app.run(debug=True)
