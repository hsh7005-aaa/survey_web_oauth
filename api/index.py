import os
import json
import functools
from datetime import datetime

from flask import (
    Flask, render_template, request,
    redirect, url_for, session, jsonify
)
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import google.auth.transport.requests

app = Flask(
    __name__,
    template_folder="../templates",
    static_folder="../static"
)

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-this")

# ── 환경변수 ──────────────────────────────────────────────
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
SPREADSHEET_ID       = os.environ.get("SPREADSHEET_ID")
ADMIN_EMAIL          = os.environ.get("ADMIN_EMAIL")
ADMIN_PASSWORD       = os.environ.get("ADMIN_PASSWORD")
BASE_URL             = os.environ.get("BASE_URL", "http://localhost:5000")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

# ── 토큰 저장/로드 ────────────────────────────────────────
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "../token_store.json")

def load_token() -> dict | None:
    raw = os.environ.get("GOOGLE_TOKEN_JSON")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)
    return None

def save_token(token_dict: dict):
    try:
        with open(TOKEN_FILE, "w") as f:
            json.dump(token_dict, f)
    except OSError:
        pass

def get_server_credentials() -> Credentials | None:
    token_data = load_token()
    if not token_data:
        return None
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(google.auth.transport.requests.Request())
        save_token(creds_to_dict(creds))
    return creds

def creds_to_dict(creds: Credentials) -> dict:
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
    }

def get_sheets_service():
    creds = get_server_credentials()
    if not creds:
        raise RuntimeError("Google 인증이 필요합니다.")
    return build("sheets", "v4", credentials=creds)

def ensure_header(service):
    res = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range="Sheet1!A1:F1"
    ).execute()
    if not res.get("values"):
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range="Sheet1!A1",
            valueInputOption="RAW",
            body={"values": [["이름", "나이대", "만족도(1-5)", "추천여부", "의견", "제출시각"]]}
        ).execute()

# ── OAuth Flow ────────────────────────────────────────────
def make_flow():
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"{BASE_URL}/admin/oauth/callback"],
        }
    }
    return Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=f"{BASE_URL}/admin/oauth/callback"
    )

# ── 관리자 인증 데코레이터 ─────────────────────────────────
def admin_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated

# ═════════════════════════════════════════════════════════
# 공개 라우트
# ═════════════════════════════════════════════════════════

@app.route("/")
def index():
    """설문조사 메인 페이지 - 항상 표시"""
    return render_template("survey.html", is_connected=True)  # 항상 True


@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json(force=True)

    required = ["name", "age_group", "satisfaction", "recommend", "opinion"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"'{field}' 항목이 누락되었습니다."}), 400

    try:
        service = get_sheets_service()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503

    ensure_header(service)

    row = [[
        data["name"],
        data["age_group"],
        int(data["satisfaction"]),
        data["recommend"],
        data["opinion"],
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ]]

    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Sheet1!A:F",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": row}
    ).execute()

    return jsonify({"success": True})


# ═════════════════════════════════════════════════════════
# 관리자 전용 라우트
# ═════════════════════════════════════════════════════════

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == ADMIN_PASSWORD:
            session["admin_authed"] = True
            if load_token():
                session["is_admin"] = True
                return redirect(url_for("admin_dashboard"))
            return redirect(url_for("admin_google_auth"))
        else:
            error = "비밀번호가 올바르지 않습니다."
    return render_template("admin_login.html", error=error)


@app.route("/admin/google-auth")
def admin_google_auth():
    if not session.get("admin_authed"):
        return redirect(url_for("admin_login"))
    flow = make_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true"
    )
    session["oauth_state"] = state
    return redirect(auth_url)


@app.route("/admin/oauth/callback")
def admin_oauth_callback():
    if not session.get("admin_authed"):
        return redirect(url_for("admin_login"))

    flow = make_flow()
    flow.fetch_token(authorization_response=request.url.replace("http://", "https://") if BASE_URL.startswith("https") else request.url)

    creds = flow.credentials

    if ADMIN_EMAIL:
        user_service = build("oauth2", "v2", credentials=creds)
        info = user_service.userinfo().get().execute()
        if info.get("email") != ADMIN_EMAIL:
            session.clear()
            return "❌ 허용되지 않은 Google 계정입니다.", 403

    save_token(creds_to_dict(creds))
    session["is_admin"] = True

    return redirect(url_for("admin_dashboard"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    is_connected = load_token() is not None
    return render_template("results.html", is_connected=is_connected)


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/admin/disconnect-google")
@admin_required
def admin_disconnect():
    try:
        os.remove(TOKEN_FILE)
    except OSError:
        pass
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/api/results")
@admin_required
def api_results():
    try:
        service = get_sheets_service()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503

    res = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range="Sheet1!A:F"
    ).execute()

    values = res.get("values", [])
    rows = values[1:] if len(values) > 1 else []

    if not rows:
        return jsonify({
            "total": 0, "satisfaction_avg": 0,
            "age_distribution": {}, "recommend_ratio": {"예": 0, "아니오": 0},
            "satisfaction_distribution": {"1":0,"2":0,"3":0,"4":0,"5":0},
            "recent_opinions": [], "daily_responses": {}
        })

    total = len(rows)
    sat_sum = 0
    age_dist = {}
    rec_ratio = {"예": 0, "아니오": 0}
    sat_dist = {"1":0,"2":0,"3":0,"4":0,"5":0}
    daily = {}
    opinions = []

    for row in rows:
        while len(row) < 6:
            row.append("")
        name, age, sat, rec, opinion, ts = row

        try:
            s = int(sat)
            sat_sum += s
            if str(s) in sat_dist:
                sat_dist[str(s)] += 1
        except (ValueError, TypeError):
            pass

        if age:
            age_dist[age] = age_dist.get(age, 0) + 1

        if rec in rec_ratio:
            rec_ratio[rec] += 1

        if ts:
            day = ts[:10]
            daily[day] = daily.get(day, 0) + 1

        if opinion:
            opinions.append({"name": name, "opinion": opinion, "timestamp": ts})

    return jsonify({
        "total": total,
        "satisfaction_avg": round(sat_sum / total, 2) if total else 0,
        "age_distribution": age_dist,
        "recommend_ratio": rec_ratio,
        "satisfaction_distribution": sat_dist,
        "daily_responses": dict(sorted(daily.items())),
        "recent_opinions": opinions[-10:][::-1],
    })
