from datetime import datetime, timezone, timedelta
import os
import json
import secrets
import hashlib
import uuid
from typing import Optional, List
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

import httpx
from urllib.parse import quote
from html import escape

from fastapi import FastAPI, HTTPException, Depends, Request, Header, Form, File, UploadFile
from fastapi.security import APIKeyHeader, HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# =========================================================
# KONFIGURASI DAN DATABASE CONNECTION POOL
# =========================================================

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("//"):
    DATABASE_URL = "postgresql:" + DATABASE_URL

API_TOKEN = os.environ.get("API_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY", "").strip()

api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
)

db_pool: Optional[ConnectionPool] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    if DATABASE_URL:
        db_pool = ConnectionPool(
            conninfo=DATABASE_URL,
            kwargs={"row_factory": dict_row},
            min_size=2,
            max_size=20,
            open=True,
        )
        initialize_database()
    yield
    if db_pool:
        db_pool.close()


app = FastAPI(
    title="Survei GPS API - Multi-Wilayah Terisolasi",
    description="Server pusat aplikasi Survei GPS dengan isolasi wilayah DPS dan dukungan teks, foto, serta video.",
    version="3.4.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    if not db_pool:
        raise RuntimeError("DATABASE_URL belum dikonfigurasi atau pool belum aktif.")
    with db_pool.connection() as conn:
        yield conn


# =========================================================
# INISIALISASI TABEL DATABASE
# =========================================================

def initialize_database():
    if not db_pool:
        return

    with db_pool.connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS members (
                id BIGSERIAL PRIMARY KEY,
                member_id VARCHAR(50) UNIQUE NOT NULL,
                member_name VARCHAR(100) NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS surveys (
                id BIGSERIAL PRIMARY KEY,
                idempotency_key VARCHAR(64) UNIQUE,
                member_id VARCHAR(50) NOT NULL,
                latitude DOUBLE PRECISION NOT NULL,
                longitude DOUBLE PRECISION NOT NULL,
                accuracy DOUBLE PRECISION,
                notes TEXT NOT NULL DEFAULT '',
                surveyed_at TIMESTAMPTZ NOT NULL,
                received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                applicant_name TEXT NOT NULL DEFAULT '',
                applicant_nik TEXT NOT NULL DEFAULT '',
                applicant_phone TEXT NOT NULL DEFAULT '',
                applicant_address TEXT NOT NULL DEFAULT '',
                village TEXT NOT NULL DEFAULT '',
                business_name TEXT NOT NULL DEFAULT '',
                business_type TEXT NOT NULL DEFAULT '',
                business_since TEXT NOT NULL DEFAULT '',
                business_location TEXT NOT NULL DEFAULT '',
                ownership_status TEXT NOT NULL DEFAULT '',
                nib TEXT NOT NULL DEFAULT '',
                capital_amount NUMERIC(15, 2),
                turnover_amount NUMERIC(15, 2),
                net_profit_amount NUMERIC(15, 2),
                worker_count INTEGER,
                business_active TEXT NOT NULL DEFAULT '',
                business_issues TEXT NOT NULL DEFAULT '',
                assistance_types TEXT NOT NULL DEFAULT '',
                assistance_amount NUMERIC(15, 2),
                assistance_reason TEXT NOT NULL DEFAULT '',
                business_verified TEXT NOT NULL DEFAULT '',
                verification_status TEXT NOT NULL DEFAULT '',
                verification_notes TEXT NOT NULL DEFAULT ''
            );
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS survey_media (
                id BIGSERIAL PRIMARY KEY,
                survey_id BIGINT NOT NULL REFERENCES surveys(id) ON DELETE CASCADE,
                member_id VARCHAR(50) NOT NULL,
                object_path TEXT NOT NULL UNIQUE,
                content_type VARCHAR(100) NOT NULL,
                original_filename TEXT,
                file_size BIGINT NOT NULL,
                uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                media_category VARCHAR(40) NOT NULL DEFAULT 'business_photo'
            );
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS officers (
                id BIGSERIAL PRIMARY KEY,
                member_id VARCHAR(50) UNIQUE NOT NULL REFERENCES members(member_id),
                member_name VARCHAR(100) NOT NULL,
                password_salt VARCHAR(64) NOT NULL,
                password_hash VARCHAR(128) NOT NULL,
                role VARCHAR(10) NOT NULL CHECK (role IN ('DPSS', 'DPS')),
                region VARCHAR(150) NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS officer_sessions (
                id BIGSERIAL PRIMARY KEY,
                officer_id BIGINT NOT NULL REFERENCES officers(id) ON DELETE CASCADE,
                token_hash VARCHAR(64) UNIQUE NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS survey_verifications (
                id BIGSERIAL PRIMARY KEY,
                survey_id BIGINT NOT NULL UNIQUE REFERENCES surveys(id) ON DELETE CASCADE,
                verifier_id VARCHAR(50) NOT NULL,
                verifier_name VARCHAR(100) NOT NULL,
                verifier_region VARCHAR(150) NOT NULL DEFAULT '',
                decision VARCHAR(40) NOT NULL CHECK (decision IN ('Sudah diverifikasi', 'Ditolak')),
                business_condition TEXT NOT NULL DEFAULT '',
                actual_business_activity TEXT NOT NULL DEFAULT '',
                existing_assets TEXT NOT NULL DEFAULT '',
                needs JSONB NOT NULL DEFAULT '[]'::jsonb,
                working_capital_details TEXT NOT NULL DEFAULT '',
                verified_turnover NUMERIC(15, 2),
                verified_profit NUMERIC(15, 2),
                verified_worker_count INTEGER,
                data_match VARCHAR(40) NOT NULL DEFAULT '',
                discrepancy_notes TEXT NOT NULL DEFAULT '',
                verification_notes TEXT NOT NULL DEFAULT '',
                rejection_reason TEXT NOT NULL DEFAULT '',
                latitude DOUBLE PRECISION NOT NULL,
                longitude DOUBLE PRECISION NOT NULL,
                accuracy DOUBLE PRECISION,
                verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                total_needs_amount NUMERIC(15, 2) NOT NULL DEFAULT 0
            );
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS verification_media (
                id BIGSERIAL PRIMARY KEY,
                survey_id BIGINT NOT NULL REFERENCES surveys(id) ON DELETE CASCADE,
                verifier_id VARCHAR(50) NOT NULL,
                object_path TEXT NOT NULL UNIQUE,
                content_type VARCHAR(100) NOT NULL,
                original_filename TEXT,
                file_size BIGINT NOT NULL,
                uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)


# =========================================================
# AUTENTIKASI DAN KEAMANAN TOKEN
# =========================================================

def verify_api_key(api_key: Optional[str] = Depends(api_key_header)):
    if not API_TOKEN or not api_key or not secrets.compare_digest(api_key, API_TOKEN):
        raise HTTPException(status_code=401, detail="Kunci akses API tidak valid")
    return True


def _password_hash(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 310_000).hex()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_officer_identity(authorization: Optional[str] = Header(default=None), conn=Depends(get_db)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Silakan login menggunakan akun petugas.")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Token login tidak valid.")

    row = conn.execute(
        """
        SELECT o.member_id, o.member_name, o.role, o.region, o.active, s.expires_at
        FROM officer_sessions s
        JOIN officers o ON o.id = s.officer_id
        WHERE s.token_hash = %s AND s.expires_at > NOW()
        """,
        (_token_hash(token),)
    ).fetchone()

    if not row or row["active"] != 1:
        raise HTTPException(status_code=401, detail="Sesi tidak valid atau sudah berakhir. Silakan login kembali.")

    return {
        "auth_type": "officer",
        "member_id": row["member_id"],
        "member_name": row["member_name"],
        "role": row["role"],
        "region": row["region"],
    }


def require_dps(identity: dict = Depends(get_officer_identity)):
    if identity.get("role") != "DPS":
        raise HTTPException(status_code=403, detail="Fitur ini khusus akun petugas verifikasi wilayah (DPS).")
    return identity


# =========================================================
# MODEL VALIDASI PYDANTIC (TEKS & STRUKTUR DATA)
# =========================================================

class OfficerLogin(BaseModel):
    member_id: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=200)


class AssistanceNeedItem(BaseModel):
    item_name: str = Field(min_length=1, max_length=200)
    specification: str = Field(default="", max_length=1000)
    quantity: float = Field(gt=0, le=1000000)
    unit: str = Field(default="unit", max_length=40)
    unit_price: float = Field(ge=0, le=1000000000000)
    notes: str = Field(default="", max_length=1000)


class VerificationCreate(BaseModel):
    decision: str
    business_condition: str = Field(default="", max_length=3000)
    actual_business_activity: str = Field(default="", max_length=3000)
    existing_assets: str = Field(default="", max_length=3000)
    needs: List[AssistanceNeedItem] = Field(default_factory=list, max_length=50)
    working_capital_details: str = Field(default="", max_length=3000)
    verified_turnover: Optional[float] = Field(default=None, ge=0)
    verified_profit: Optional[float] = Field(default=None, ge=0)
    verified_worker_count: Optional[int] = Field(default=None, ge=0, le=100000)
    data_match: str = Field(default="", max_length=40)
    discrepancy_notes: str = Field(default="", max_length=3000)
    verification_notes: str = Field(default="", max_length=3000)
    rejection_reason: str = Field(default="", max_length=3000)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: Optional[float] = Field(default=None, ge=0)


class SurveyCreate(BaseModel):
    idempotency_key: Optional[str] = Field(default=None, max_length=64)
    member_id: str = Field(min_length=1, max_length=50)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: Optional[float] = Field(default=None, ge=0)
    notes: str = Field(default="", max_length=5000)
    surveyed_at: datetime
    applicant_name: str = Field(default="", max_length=150)
    applicant_nik: str = Field(default="", max_length=30)
    applicant_phone: str = Field(default="", max_length=40)
    applicant_address: str = Field(default="", max_length=1000)
    village: str = Field(default="", max_length=120)
    business_name: str = Field(default="", max_length=200)
    business_type: str = Field(default="", max_length=150)
    business_since: str = Field(default="", max_length=30)
    business_location: str = Field(default="", max_length=1000)
    ownership_status: str = Field(default="", max_length=100)
    nib: str = Field(default="", max_length=100)
    capital_amount: Optional[float] = Field(default=None, ge=0)
    turnover_amount: Optional[float] = Field(default=None, ge=0)
    net_profit_amount: Optional[float] = Field(default=None, ge=0)
    worker_count: Optional[int] = Field(default=None, ge=0, le=100000)
    business_active: str = Field(default="", max_length=50)
    business_issues: str = Field(default="", max_length=3000)
    assistance_types: str = Field(default="", max_length=1000)
    assistance_amount: Optional[float] = Field(default=None, ge=0)
    assistance_reason: str = Field(default="", max_length=3000)
    business_verified: str = Field(default="", max_length=50)
    verification_status: str = Field(default="", max_length=50)
    verification_notes: str = Field(default="", max_length=3000)


# =========================================================
# ENDPOINT UTAMA & LOGIN
# =========================================================

@app.get("/")
def home():
    return {"server": "Survei GPS Multi-Wilayah API", "status": "online"}


@app.post("/auth/login")
def officer_login(payload: OfficerLogin, conn=Depends(get_db)):
    member_id = payload.member_id.strip().upper()
    officer = conn.execute("SELECT * FROM officers WHERE member_id = %s", (member_id,)).fetchone()
    if (
        not officer
        or officer["active"] != 1
        or not secrets.compare_digest(_password_hash(payload.password, officer["password_salt"]), officer["password_hash"])
    ):
        raise HTTPException(status_code=401, detail="ID petugas atau kata sandi salah, atau akun tidak aktif.")

    token = secrets.token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=12)
    conn.execute("DELETE FROM officer_sessions WHERE expires_at <= NOW()")
    conn.execute(
        "INSERT INTO officer_sessions (officer_id, token_hash, expires_at) VALUES (%s, %s, %s)",
        (officer["id"], _token_hash(token), expires_at)
    )

    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_at": expires_at.isoformat(),
        "member_id": officer["member_id"],
        "member_name": officer["member_name"],
        "role": officer["role"],
        "region": officer["region"],
    }


# =========================================================
# ENDPOINT SURVEI & ISOLASI WILAYAH DPS
# =========================================================

@app.post("/surveys")
def create_survey(survey: SurveyCreate, identity: dict = Depends(get_officer_identity), conn=Depends(get_db)):
    if identity.get("role") != "DPSS":
        raise HTTPException(status_code=403, detail="Akun DPS menggunakan menu verifikasi, bukan membuat survei awal.")

    member_id = identity["member_id"]

    if survey.idempotency_key:
        existing = conn.execute("SELECT id FROM surveys WHERE idempotency_key = %s", (survey.idempotency_key,)).fetchone()
        if existing:
            return {"message": "Data survei sudah tersimpan sebelumnya.", "survey_id": existing["id"]}

    row = conn.execute(
        """
        INSERT INTO surveys (
            idempotency_key, member_id, latitude, longitude, accuracy, notes,
            surveyed_at, received_at, applicant_name, applicant_nik, applicant_phone,
            applicant_address, village, business_name, business_type, business_since,
            business_location, ownership_status, nib, capital_amount, turnover_amount,
            net_profit_amount, worker_count, business_active, business_issues,
            assistance_types, assistance_amount, assistance_reason, business_verified,
            verification_status, verification_notes
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        ) RETURNING id
        """,
        (
            survey.idempotency_key, member_id, survey.latitude, survey.longitude, survey.accuracy,
            survey.notes, survey.surveyed_at, survey.applicant_name.strip(),
            survey.applicant_nik.strip(), survey.applicant_phone.strip(), survey.applicant_address.strip(),
            survey.village.strip(), survey.business_name.strip(), survey.business_type.strip(),
            survey.business_since.strip(), survey.business_location.strip(), survey.ownership_status.strip(),
            survey.nib.strip(), survey.capital_amount, survey.turnover_amount, survey.net_profit_amount,
            survey.worker_count, survey.business_active.strip(), survey.business_issues.strip(),
            survey.assistance_types.strip(), survey.assistance_amount, survey.assistance_reason.strip(),
            "Belum diverifikasi", "Perlu ditinjau", "",
        )
    ).fetchone()

    return {"message": "Data teks survei berhasil disimpan", "survey_id": row["id"]}


@app.get("/dps/surveys")
def list_surveys_for_verification(
    include_completed: bool = False,
    identity: dict = Depends(require_dps),
    conn=Depends(get_db)
):
    # ISOLASI WILAYAH: DPS hanya bisa menarik data survei di desanya sendiri
    region = identity.get("region", "").strip()
    query = "SELECT * FROM surveys WHERE village ILIKE %s"
    params = [f"%{region}%"] if region else ["%"]

    if not include_completed:
        query += " AND COALESCE(verification_status, '') NOT IN ('Sudah diverifikasi', 'Ditolak')"

    query += " ORDER BY id DESC"
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


# =========================================================
# PENGUNGGAHAN MEDIA TERISOLASI (FOTO & VIDEO)
# =========================================================

def create_supabase_signed_url(object_path: str, expires_in: int = 3600) -> Optional[str]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    sign_url = f"{SUPABASE_URL}/storage/v1/object/sign/survey-media/{quote(object_path, safe='/')}"
    payload = json.dumps({"expiresIn": expires_in}).encode("utf-8")

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                sign_url, content=payload,
                headers={"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY, "Content-Type": "application/json"}
            )
            if resp.status_code == 200:
                result = resp.json()
                signed_path = result.get("signedURL") or result.get("signedUrl")
                if signed_path:
                    if signed_path.startswith("http"):
                        return signed_path
                    return f"{SUPABASE_URL}/storage/v1{signed_path if signed_path.startswith('/') else '/' + signed_path}"
    except Exception:
        pass
    return None


@app.post("/dps/surveys/{survey_id}/verification-media")
async def upload_verification_media(
    survey_id: int,
    file: UploadFile = File(...),
    identity: dict = Depends(require_dps),
    conn=Depends(get_db)
):
    dps_region = identity.get("region", "").strip().lower()
    
    # Validasi wilayah survei terhadap wilayah tugas DPS
    survey = conn.execute("SELECT id, village FROM surveys WHERE id = %s", (survey_id,)).fetchone()
    if not survey:
        raise HTTPException(status_code=404, detail="Data survei tidak ditemukan.")
    
    if dps_region and dps_region not in survey["village"].lower():
        raise HTTPException(status_code=403, detail="Akses ditolak: Wilayah survei ini di luar wilayah penugasan Anda.")

    content_type = (file.content_type or "").lower()
    extensions = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4"}
    if content_type not in extensions:
        raise HTTPException(status_code=400, detail="Format file foto atau video tidak diizinkan.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="File kosong.")

    # Penyimpanan terisolasi berdasarkan folder wilayah DPS
    safe_region_folder = dps_region.replace(" ", "_") if dps_region else "umum"
    object_path = f"verifications/{safe_region_folder}/{survey_id}/{uuid.uuid4().hex}{extensions[content_type]}"
    storage_url = f"{SUPABASE_URL}/storage/v1/object/survey-media/{quote(object_path, safe='/')}"

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            storage_url, content=content,
            headers={"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY, "Content-Type": content_type, "x-upsert": "false"}
        )
        if resp.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail="Supabase menolak penyimpanan file verifikasi.")

    conn.execute(
        """INSERT INTO verification_media (survey_id, verifier_id, object_path, content_type, original_filename, file_size)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (survey_id, identity["member_id"], object_path, content_type, os.path.basename(file.filename or "media"), len(content))
    )
    return {"message": "Media verifikasi berhasil diunggah ke folder wilayah Anda.", "file_path": object_path}


@app.post("/dps/surveys/{survey_id}/verification")
def submit_survey_verification(
    survey_id: int,
    payload: VerificationCreate,
    identity: dict = Depends(require_dps),
    conn=Depends(get_db)
):
    decision = payload.decision.strip()
    if decision not in {"Sudah diverifikasi", "Ditolak"}:
        raise HTTPException(status_code=422, detail="Keputusan harus 'Sudah diverifikasi' atau 'Ditolak'.")

    needs_data = [item.dict() for item in payload.needs]
    total_needs_amount = round(sum(item["quantity"] * item["unit_price"] for item in needs_data), 2)

    conn.execute(
        """INSERT INTO survey_verifications (
            survey_id, verifier_id, verifier_name, verifier_region, decision,
            business_condition, actual_business_activity, existing_assets, needs,
            working_capital_details, verified_turnover, verified_profit, verified_worker_count,
            data_match, discrepancy_notes, verification_notes, rejection_reason,
            latitude, longitude, accuracy, total_needs_amount
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            survey_id, identity["member_id"], identity["member_name"], identity["region"], decision,
            payload.business_condition.strip(), payload.actual_business_activity.strip(), payload.existing_assets.strip(),
            Jsonb(needs_data), payload.working_capital_details.strip(), payload.verified_turnover,
            payload.verified_profit, payload.verified_worker_count, payload.data_match.strip(),
            payload.discrepancy_notes.strip(), payload.verification_notes.strip(), payload.rejection_reason.strip(),
            payload.latitude, payload.longitude, payload.accuracy, total_needs_amount,
        )
    )

    conn.execute(
        "UPDATE surveys SET business_verified = %s, verification_status = %s, verification_notes = %s WHERE id = %s",
        ("Ya" if decision == "Sudah diverifikasi" else "Tidak", decision, payload.verification_notes.strip() or payload.rejection_reason.strip(), survey_id)
    )
    return {"message": "Hasil verifikasi berhasil disimpan.", "survey_id": survey_id}


# =========================================================
# DASHBOARD ADMIN PUSAT (MONITORING LENGKAP)
# =========================================================

admin_security = HTTPBasic()

def verify_admin(credentials: HTTPBasicCredentials = Depends(admin_security)):
    admin_username = os.environ.get("ADMIN_USERNAME", "")
    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    if not admin_username or not admin_password or not secrets.compare_digest(credentials.username, admin_username) or not secrets.compare_digest(credentials.password, admin_password):
        raise HTTPException(status_code=401, detail="Akses admin ditolak.", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(username: str = Depends(verify_admin), conn=Depends(get_db)):
    rows = conn.execute("SELECT * FROM surveys ORDER BY id DESC").fetchall()
    officer_rows = conn.execute("SELECT member_id, member_name, role, region, active FROM officers ORDER BY role, region").fetchall()

    table_rows = "".join([f"<tr><td>{r['id']}</td><td>{r['member_id']}</td><td>{r['applicant_name']}</td><td>{r['village']}</td><td>{r['verification_status']}</td></tr>" for r in rows])
    officers_html = "".join([f"<tr><td>{o['member_id']}</td><td>{o['member_name']}</td><td>{o['role']}</td><td>{o.get('region', '-')}</td></tr>" for o in officer_rows])

    return f"""
    <html>
    <head><title>Dashboard Pusat - Survei GPS</title>
    <style>body{{font-family:Arial;padding:20px;background:#f8fafc}} table{{width:100%;border-collapse:collapse;background:white;margin-bottom:20px}} th,td{{border:1px solid #cbd5e1;padding:8px;font-size:13px}} th{{background:#e2e8f0}}</style>
    </head>
    <body>
        <h1>Dashboard Pusat Monitoring</h1>
        <h2>Daftar Petugas (DPSS & DPS Berbasis Wilayah)</h2>
        <table><tr><th>ID</th><th>Nama</th><th>Role</th><th>Wilayah DPS</th></tr>{officers_html}</table>
        <h2>Semua Data Survei Lapangan</h2>
        <table><tr><th>ID</th><th>Petugas</th><th>Pemohon</th><th>Desa/Wilayah</th><th>Status</th></tr>{table_rows}</table>
    </body>
    </html>
    """


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
