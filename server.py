from datetime import datetime, timezone, timedelta
import os
import json
import secrets
import hashlib
import uuid
import math
from typing import Optional, List
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

import httpx
from urllib.parse import quote
from html import escape

from fastapi import FastAPI, HTTPException, Depends, Request, Header, Form, File, UploadFile, Query
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
    title="Survei GPS API",
    description="Server pusat aplikasi Survei GPS",
    version="3.2.0",
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
# MEMBUAT TABEL DAN MEMASTIKAN ANGGOTA TES AKTIF
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

        connection.execute("ALTER TABLE surveys ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(64) UNIQUE;")

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
        connection.execute("ALTER TABLE survey_media ADD COLUMN IF NOT EXISTS media_category VARCHAR(40) NOT NULL DEFAULT 'business_photo'")

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

        connection.execute(
            """
            INSERT INTO members (member_id, member_name, active, created_at)
            VALUES (%s, %s, 1, NOW())
            ON CONFLICT (member_id)
            DO UPDATE SET member_name = EXCLUDED.member_name, active = 1
            """,
            ("PETUGAS001", "Petugas Tes"),
        )


# =========================================================
# AUTENTIKASI DAN UTILS
# =========================================================

def verify_api_key(api_key: Optional[str] = Depends(api_key_header)):
    if not API_TOKEN or not api_key or not secrets.compare_digest(api_key, API_TOKEN):
        raise HTTPException(status_code=401, detail="Kunci akses tidak valid")
    return True


def _password_hash(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 310_000).hex()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_request_identity(
    authorization: Optional[str] = Header(default=None),
    api_key: Optional[str] = Depends(api_key_header),
    conn=Depends(get_db)
):
    if API_TOKEN and api_key and secrets.compare_digest(api_key, API_TOKEN):
        return {"auth_type": "api_key", "role": "LEGACY_ADMIN"}

    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            row = conn.execute(
                """
                SELECT o.member_id, o.member_name, o.role, o.region, o.active, s.expires_at
                FROM officer_sessions s
                JOIN officers o ON o.id = s.officer_id
                WHERE s.token_hash = %s AND s.expires_at > NOW()
                """,
                (_token_hash(token),)
            ).fetchone()
            if row and row["active"] == 1:
                return {
                    "auth_type": "officer",
                    "member_id": row["member_id"],
                    "member_name": row["member_name"],
                    "role": row["role"],
                    "region": row["region"],
                }

    raise HTTPException(status_code=401, detail="Sesi tidak valid atau sudah berakhir. Silakan login kembali.")


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
        raise HTTPException(status_code=403, detail="Fitur ini khusus akun petugas DPS.")
    return identity


# =========================================================
# MODEL DATA
# =========================================================

class OfficerLogin(BaseModel):
    member_id: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=200)


class OfficerCreate(BaseModel):
    member_id: str = Field(min_length=2, max_length=50)
    member_name: str = Field(min_length=2, max_length=100)
    password: str = Field(min_length=8, max_length=200)
    role: str
    region: str = Field(min_length=1, max_length=150)


class MemberCreate(BaseModel):
    member_id: str = Field(min_length=1, max_length=50)
    member_name: str = Field(min_length=1, max_length=100)


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
# HALAMAN UTAMA & HEALTH
# =========================================================

@app.get("/")
def home():
    return {
        "server": "Survei GPS",
        "status": "online",
        "message": "Server berjalan dengan baik",
    }


@app.get("/health")
def health(conn=Depends(get_db)):
    conn.execute("SELECT 1")
    return {
        "status": "ok",
        "database": "connected",
    }


# =========================================================
# AUTENTIKASI OFFICERS
# =========================================================

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


@app.get("/auth/me")
def officer_me(identity: dict = Depends(get_officer_identity)):
    return {key: identity[key] for key in ("member_id", "member_name", "role", "region")}


@app.post("/auth/logout")
def officer_logout(authorization: Optional[str] = Header(default=None), conn=Depends(get_db)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sesi login tidak ditemukan.")
    token = authorization[7:].strip()
    conn.execute("DELETE FROM officer_sessions WHERE token_hash = %s", (_token_hash(token),))
    return {"message": "Berhasil keluar."}


@app.post("/admin/officers")
def create_officer(payload: OfficerCreate, _: bool = Depends(verify_api_key), conn=Depends(get_db)):
    member_id = payload.member_id.strip().upper()
    member_name = payload.member_name.strip()
    role = payload.role.strip().upper()
    region = payload.region.strip()

    if role not in {"DPSS", "DPS"}:
        raise HTTPException(status_code=400, detail="Role harus DPSS atau DPS.")

    salt = secrets.token_hex(16)
    hashed = _password_hash(payload.password, salt)
    now = datetime.now(timezone.utc)

    existing = conn.execute("SELECT member_id FROM officers WHERE member_id = %s", (member_id,)).fetchone()
    if existing:
        raise HTTPException(status_code=409, detail="Akun petugas dengan ID tersebut sudah ada.")

    conn.execute(
        """INSERT INTO members (member_id, member_name, active, created_at)
           VALUES (%s, %s, 1, %s) ON CONFLICT (member_id) DO UPDATE SET member_name = EXCLUDED.member_name, active = 1""",
        (member_id, member_name, now)
    )
    conn.execute(
        """INSERT INTO officers (member_id, member_name, password_salt, password_hash, role, region, active)
           VALUES (%s, %s, %s, %s, %s, %s, 1)""",
        (member_id, member_name, salt, hashed, role, region)
    )

    return {"message": "Akun petugas berhasil dibuat.", "member_id": member_id, "member_name": member_name, "role": role, "region": region, "active": True}


@app.get("/admin/officers")
def list_officers(_: bool = Depends(verify_api_key), conn=Depends(get_db)):
    rows = conn.execute("SELECT member_id, member_name, role, region, active, created_at, updated_at FROM officers ORDER BY role, region, member_id").fetchall()
    return [dict(row) for row in rows]


@app.post("/admin/officers/{member_id}/deactivate")
def deactivate_officer(member_id: str, _: bool = Depends(verify_api_key), conn=Depends(get_db)):
    mid = member_id.strip().upper()
    result = conn.execute("UPDATE officers SET active = 0, updated_at = NOW() WHERE member_id = %s", (mid,))
    conn.execute("DELETE FROM officer_sessions WHERE officer_id IN (SELECT id FROM officers WHERE member_id = %s)", (mid,))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Akun petugas tidak ditemukan.")
    conn.execute("UPDATE members SET active = 0 WHERE member_id = %s", (mid,))
    return {"message": "Akun petugas dinonaktifkan.", "member_id": mid}


# =========================================================
# MANAJEMEN ANGGOTA
# =========================================================

@app.post("/admin/members")
def create_member(member: MemberCreate, _: bool = Depends(verify_api_key), conn=Depends(get_db)):
    mid = member.member_id.strip()
    mname = member.member_name.strip()
    if not mid or not mname:
        raise HTTPException(status_code=400, detail="ID dan nama anggota wajib diisi")

    try:
        conn.execute(
            "INSERT INTO members (member_id, member_name, created_at) VALUES (%s, %s, NOW())",
            (mid, mname)
        )
    except psycopg.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="ID anggota sudah terdaftar")

    return {"message": "Anggota berhasil didaftarkan", "member_id": mid, "member_name": mname}


@app.get("/admin/members")
def list_members(_: bool = Depends(verify_api_key), conn=Depends(get_db)):
    rows = conn.execute("SELECT member_id, member_name, active, created_at FROM members ORDER BY member_id").fetchall()
    return [dict(row) for row in rows]


# =========================================================
# SURVEI GPS
# =========================================================

@app.post("/surveys")
def create_survey(survey: SurveyCreate, identity: dict = Depends(get_request_identity), conn=Depends(get_db)):
    if identity["auth_type"] == "officer" and identity.get("role") != "DPSS":
        raise HTTPException(status_code=403, detail="Akun DPS menggunakan menu verifikasi, bukan membuat survei awal.")

    member_id = identity["member_id"] if identity["auth_type"] == "officer" else survey.member_id.strip()

    # Cek Idempotency Key
    if survey.idempotency_key:
        existing = conn.execute("SELECT id FROM surveys WHERE idempotency_key = %s", (survey.idempotency_key,)).fetchone()
        if existing:
            return {"message": "Data survei sudah tersimpan sebelumnya (idempotent).", "survey_id": existing["id"], "member_id": member_id}

    member = conn.execute("SELECT member_id FROM members WHERE member_id = %s AND active = 1", (member_id,)).fetchone()
    if member is None:
        raise HTTPException(status_code=403, detail="ID anggota tidak terdaftar atau tidak aktif")

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
            ("Belum diverifikasi" if identity["auth_type"] == "officer" else survey.business_verified.strip()),
            ("Perlu ditinjau" if identity["auth_type"] == "officer" else survey.verification_status.strip()),
            ("" if identity["auth_type"] == "officer" else survey.verification_notes.strip()),
        )
    ).fetchone()

    return {"message": "Data survei berhasil disimpan", "survey_id": row["id"], "member_id": member_id}


@app.get("/admin/surveys")
def list_surveys(member_id: Optional[str] = None, _: bool = Depends(verify_api_key), conn=Depends(get_db)):
    if member_id:
        rows = conn.execute("SELECT * FROM surveys WHERE member_id = %s ORDER BY id DESC", (member_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM surveys ORDER BY id DESC").fetchall()
    return [dict(row) for row in rows]


# =========================================================
# DPS VERIFIKASI BERBASIS WILAYAH (ISOLASI DATA)
# =========================================================

@app.get("/dps/surveys")
def list_surveys_for_verification(
    include_completed: bool = False,
    identity: dict = Depends(require_dps),
    conn=Depends(get_db)
):
    region = identity.get("region", "").strip()
    query = "SELECT * FROM surveys WHERE village ILIKE %s"
    params = [f"%{region}%"] if region else ["%"]

    if not include_completed:
        query += " AND COALESCE(verification_status, '') NOT IN ('Sudah diverifikasi', 'Ditolak')"

    query += " ORDER BY id DESC"
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def create_supabase_signed_url(object_path: str, expires_in: int = 3600) -> Optional[str]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    sign_url = f"{SUPABASE_URL}/storage/v1/object/sign/survey-media/{quote(object_path, safe='/')}"
    payload = json.dumps({"expiresIn": expires_in}).encode("utf-8")

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                sign_url,
                content=payload,
                headers={
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "apikey": SUPABASE_KEY,
                    "Content-Type": "application/json",
                }
            )
            if resp.status_code == 200:
                result = resp.json()
                signed_path = result.get("signedURL") or result.get("signedUrl")
                if not signed_path:
                    return None
                if signed_path.startswith("http://") or signed_path.startswith("https://"):
                    return signed_path
                if signed_path.startswith("/"):
                    if signed_path.startswith("/storage/v1/"):
                        return f"{SUPABASE_URL}{signed_path}"
                    return f"{SUPABASE_URL}/storage/v1{signed_path}"
                return f"{SUPABASE_URL}/storage/v1/{signed_path.lstrip('/')}"
    except Exception:
        pass
    return None


@app.get("/dps/surveys/{survey_id}")
def get_survey_for_verification(survey_id: int, identity: dict = Depends(require_dps), conn=Depends(get_db)):
    survey = conn.execute("SELECT * FROM surveys WHERE id = %s", (survey_id,)).fetchone()
    if survey is None:
        raise HTTPException(status_code=404, detail="Data survei tidak ditemukan.")

    original_media = conn.execute("SELECT id, media_category, content_type, original_filename, object_path, file_size, uploaded_at FROM survey_media WHERE survey_id = %s ORDER BY id", (survey_id,)).fetchall()
    verification = conn.execute("SELECT * FROM survey_verifications WHERE survey_id = %s", (survey_id,)).fetchone()
    verification_media = conn.execute("SELECT id, content_type, original_filename, object_path, file_size, uploaded_at FROM verification_media WHERE survey_id = %s ORDER BY id", (survey_id,)).fetchall()

    def media_with_url(row):
        item = dict(row)
        item["signed_url"] = create_supabase_signed_url(item["object_path"])
        return item

    return {
        "survey": dict(survey),
        "survey_media": [media_with_url(row) for row in original_media],
        "verification": dict(verification) if verification else None,
        "verification_media": [media_with_url(row) for row in verification_media],
    }


# =========================================================
# ASYNC MEDIA UPLOADS (NON-BLOCKING HTTPX)
# =========================================================

@app.post("/dps/surveys/{survey_id}/verification-media")
async def upload_verification_media(
    survey_id: int,
    file: UploadFile = File(...),
    identity: dict = Depends(require_dps),
    conn=Depends(get_db)
):
    content_type = (file.content_type or "").lower()
    extensions = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    if content_type not in extensions:
        raise HTTPException(status_code=400, detail="Bukti verifikasi harus berupa foto JPEG, PNG, atau WebP.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="File foto kosong.")
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Ukuran setiap foto verifikasi maksimal 20 MB.")

    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(status_code=503, detail="Konfigurasi penyimpanan Supabase belum lengkap.")

    survey = conn.execute("SELECT id FROM surveys WHERE id = %s", (survey_id,)).fetchone()
    if survey is None:
        raise HTTPException(status_code=404, detail="Data survei tidak ditemukan.")

    count = conn.execute("SELECT COUNT(*) AS total FROM verification_media WHERE survey_id = %s", (survey_id,)).fetchone()["total"]
    if count >= 5:
        raise HTTPException(status_code=400, detail="Maksimal 5 foto bukti verifikasi per survei.")

    object_path = f"verifications/{survey_id}/{uuid.uuid4().hex}{extensions[content_type]}"
    storage_url = f"{SUPABASE_URL}/storage/v1/object/survey-media/{quote(object_path, safe='/')}"

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            storage_url,
            content=content,
            headers={
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "apikey": SUPABASE_KEY,
                "Content-Type": content_type,
                "x-upsert": "false",
            }
        )
        if resp.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail=f"Supabase menolak foto verifikasi (HTTP {resp.status_code}).")

    conn.execute(
        """INSERT INTO verification_media (survey_id, verifier_id, object_path, content_type, original_filename, file_size)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (survey_id, identity["member_id"], object_path, content_type, os.path.basename(file.filename or "foto-verifikasi"), len(content)),
    )

    return {"message": "Foto verifikasi berhasil diunggah.", "survey_id": survey_id, "file_path": object_path}


@app.post("/dps/surveys/{survey_id}/verification")
def submit_survey_verification(
    survey_id: int,
    payload: VerificationCreate,
    identity: dict = Depends(require_dps),
    conn=Depends(get_db)
):
    decision = payload.decision.strip()
    if decision not in {"Sudah diverifikasi", "Ditolak"}:
        raise HTTPException(status_code=422, detail="Pilihan keputusan hanya 'Sudah diverifikasi' atau 'Ditolak'.")
    if decision == "Ditolak" and not payload.rejection_reason.strip():
        raise HTTPException(status_code=422, detail="Alasan penolakan wajib diisi.")
    if not payload.business_condition.strip():
        raise HTTPException(status_code=422, detail="Kondisi nyata usaha wajib diisi.")
    if not payload.actual_business_activity.strip():
        raise HTTPException(status_code=422, detail="Aktivitas usaha aktual wajib diisi.")
    if not payload.data_match:
        raise HTTPException(status_code=422, detail="Kesesuaian data survei awal wajib dipilih.")
    if not payload.needs:
        raise HTTPException(status_code=422, detail="Tambahkan minimal satu item kebutuhan UMKM yang terperinci.")

    needs_data = [item.dict() for item in payload.needs]
    total_needs_amount = round(sum(item["quantity"] * item["unit_price"] for item in needs_data), 2)

    survey = conn.execute("SELECT id FROM surveys WHERE id = %s", (survey_id,)).fetchone()
    if survey is None:
        raise HTTPException(status_code=404, detail="Data survei tidak ditemukan.")

    existing = conn.execute("SELECT id FROM survey_verifications WHERE survey_id = %s", (survey_id,)).fetchone()
    if existing:
        raise HTTPException(status_code=409, detail="Survei ini sudah memiliki hasil verifikasi.")

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

    return {
        "message": "Hasil verifikasi berhasil disimpan.",
        "survey_id": survey_id,
        "decision": decision,
        "verifier_id": identity["member_id"],
        "total_needs_amount": total_needs_amount,
    }


@app.post("/surveys/{survey_id}/media")
async def upload_survey_media(
    survey_id: int,
    member_id: str = Form(...),
    media_category: str = Form("business_photo"),
    file: UploadFile = File(...),
    identity: dict = Depends(get_request_identity),
    conn=Depends(get_db)
):
    if identity["auth_type"] == "officer":
        if identity.get("role") != "DPSS":
            raise HTTPException(status_code=403, detail="Akun DPS mengunggah bukti melalui menu verifikasi.")
        if member_id.strip().upper() != identity["member_id"].upper():
            raise HTTPException(status_code=403, detail="Petugas hanya boleh mengunggah media untuk surveinya sendiri.")
        member_id = identity["member_id"]

    allowed_types = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "video/mp4": ".mp4", "video/quicktime": ".mov",
    }

    content_type = (file.content_type or "").lower()
    if content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Jenis file tidak diizinkan. Gunakan JPEG, PNG, WebP, MP4, atau MOV.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="File kosong.")
    if len(content) > 200 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Ukuran file maksimal 200 MB.")

    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(status_code=503, detail="Konfigurasi penyimpanan Supabase belum lengkap.")

    member_id = member_id.strip()
    survey = conn.execute("SELECT id FROM surveys WHERE id = %s AND member_id = %s", (survey_id, member_id)).fetchone()
    if survey is None:
        raise HTTPException(status_code=404, detail="Data survei tidak ditemukan untuk ID anggota tersebut.")

    object_path = f"surveys/{survey_id}/{media_category}/{uuid.uuid4().hex}{allowed_types[content_type]}"
    storage_url = f"{SUPABASE_URL}/storage/v1/object/survey-media/{quote(object_path, safe='/')}"

    async with httpx.AsyncClient(timeout=240.0) as client:
        resp = await client.post(
            storage_url,
            content=content,
            headers={
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "apikey": SUPABASE_KEY,
                "Content-Type": content_type,
                "x-upsert": "false",
            }
        )
        if resp.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail=f"Supabase menolak unggahan (HTTP {resp.status_code}).")

    conn.execute(
        """
        INSERT INTO survey_media (survey_id, member_id, object_path, content_type, original_filename, file_size, media_category)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (survey_id, member_id, object_path, content_type, os.path.basename(file.filename or "file"), len(content), media_category)
    )

    return {
        "message": "File berhasil diunggah.",
        "survey_id": survey_id,
        "member_id": member_id,
        "file_path": object_path,
        "content_type": content_type,
        "file_size": len(content),
        "media_category": media_category,
    }


@app.get("/admin/surveys/{survey_id}/media")
def list_survey_media(survey_id: int, _: bool = Depends(verify_api_key), conn=Depends(get_db)):
    rows = conn.execute("SELECT id, survey_id, member_id, object_path, content_type, original_filename, file_size, uploaded_at, media_category FROM survey_media WHERE survey_id = %s ORDER BY id", (survey_id,)).fetchall()
    return [dict(row) for row in rows]


# =========================================================
# PENGHAPUSAN SURVEI
# =========================================================

def delete_survey_record(survey_id: int, conn):
    survey = conn.execute("SELECT id FROM surveys WHERE id = %s", (survey_id,)).fetchone()
    if survey is None:
        raise HTTPException(status_code=404, detail="Data survei tidak ditemukan.")

    media_rows = conn.execute("SELECT object_path FROM survey_media WHERE survey_id = %s", (survey_id,)).fetchall()
    verification_media_rows = conn.execute("SELECT object_path FROM verification_media WHERE survey_id = %s", (survey_id,)).fetchall()

    object_paths = [row["object_path"] for row in media_rows + verification_media_rows]

    if object_paths and SUPABASE_URL and SUPABASE_KEY:
        storage_url = f"{SUPABASE_URL}/storage/v1/object/survey-media"
        payload = json.dumps({"prefixes": object_paths})
        try:
            with httpx.Client(timeout=45.0) as client:
                resp = client.request(
                    "DELETE",
                    storage_url,
                    content=payload,
                    headers={
                        "Authorization": f"Bearer {SUPABASE_KEY}",
                        "apikey": SUPABASE_KEY,
                        "Content-Type": "application/json",
                    }
                )
                if resp.status_code not in (200, 204):
                    raise HTTPException(status_code=502, detail="Supabase tidak mengonfirmasi penghapusan media.")
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Gagal terhubung ke Supabase saat menghapus media.") from exc

    conn.execute("DELETE FROM survey_media WHERE survey_id = %s", (survey_id,))
    conn.execute("DELETE FROM verification_media WHERE survey_id = %s", (survey_id,))
    result = conn.execute("DELETE FROM surveys WHERE id = %s", (survey_id,))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Data survei tidak ditemukan.")

    return {"message": "Survei dan media terkait berhasil dihapus.", "survey_id": survey_id, "media_deleted": len(object_paths)}


@app.delete("/admin/surveys/{survey_id}")
def delete_survey_api(survey_id: int, _: bool = Depends(verify_api_key), conn=Depends(get_db)):
    return delete_survey_record(survey_id, conn)


# =========================================================
# DASHBOARD WEB ADMIN LENGKAP
# =========================================================

admin_security = HTTPBasic()


def verify_admin(credentials: HTTPBasicCredentials = Depends(admin_security)):
    admin_username = os.environ.get("ADMIN_USERNAME", "")
    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    if not admin_username or not admin_password:
        raise HTTPException(status_code=503, detail="Login admin belum dikonfigurasi di server.")

    if not (
        secrets.compare_digest(credentials.username.encode("utf-8"), admin_username.encode("utf-8"))
        and secrets.compare_digest(credentials.password.encode("utf-8"), admin_password.encode("utf-8"))
    ):
        raise HTTPException(status_code=401, detail="Username atau kata sandi salah.", headers={"WWW-Authenticate": "Basic"})

    return credentials.username


@app.post("/admin/surveys/{survey_id}/delete")
def delete_survey_from_admin(survey_id: int, username: str = Depends(verify_admin), conn=Depends(get_db)):
    try:
        delete_survey_record(survey_id, conn)
    except HTTPException as exc:
        message = escape(str(exc.detail))
        return HTMLResponse(
            content=f"<h2>Penghapusan gagal</h2><p>{message}</p><p><a href='/admin'>Kembali ke dashboard</a></p>",
            status_code=exc.status_code,
        )
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(username: str = Depends(verify_admin), conn=Depends(get_db)):
    rows = conn.execute("SELECT * FROM surveys ORDER BY id DESC").fetchall()
    media_rows = conn.execute("SELECT id, survey_id, member_id, object_path, content_type, original_filename, file_size, uploaded_at, media_category FROM survey_media ORDER BY id DESC").fetchall()
    verification_rows = conn.execute("SELECT * FROM survey_verifications ORDER BY id DESC").fetchall()
    verification_media_rows = conn.execute("SELECT * FROM verification_media ORDER BY id DESC").fetchall()

    verification_by_survey = {row["survey_id"]: row for row in verification_rows}
    verification_media_by_survey = {}
    for media in verification_media_rows:
        verification_media_by_survey.setdefault(media["survey_id"], []).append(media)

    media_by_survey = {}
    for media in media_rows:
        media_by_survey.setdefault(media["survey_id"], []).append(media)

    table_rows = ""
    for row in rows:
        survey_id = row["id"]
        lat = row["latitude"]
        lon = row["longitude"]
        map_url = f"https://www.google.com/maps?q={lat},{lon}"

        media_html = []
        for media in media_by_survey.get(survey_id, []):
            signed_url = create_supabase_signed_url(media["object_path"])
            filename = escape(str(media["original_filename"] or "Media"))
            media_category = escape(str(media.get("media_category") or "business_photo"))
            category_labels = {
                "applicant_ktp": "KTP Pemohon",
                "family_card": "KK Pemohon",
                "recipient_photo": "Foto Penerima",
                "business_photo": "Foto Usaha",
                "business_video": "Video Usaha",
            }
            category_label = category_labels.get(media_category, media_category)
            content_type = str(media["content_type"] or "")
            size_mb = media["file_size"] / (1024 * 1024)
            if signed_url:
                safe_url = escape(signed_url, quote=True)
                if content_type.startswith("image/"):
                    media_html.append(
                        f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">'
                        f'<img class="media-thumb" src="{safe_url}" alt="{filename}"></a>'
                        f'<div><strong>{category_label}</strong><br><a href="{safe_url}" target="_blank" rel="noopener noreferrer">{filename}</a>'
                        f'<small>{size_mb:.2f} MB</small></div>'
                    )
                elif content_type.startswith("video/"):
                    media_html.append(
                        f'<video class="media-video" controls preload="metadata" src="{safe_url}"></video>'
                        f'<div><strong>{category_label}</strong><br><a href="{safe_url}" target="_blank" rel="noopener noreferrer">{filename}</a>'
                        f'<small>{size_mb:.2f} MB</small></div>'
                    )
                else:
                    media_html.append(f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">{filename}</a>')
            else:
                media_html.append(f'<span>{filename} (tersimpan, pratinjau gagal)</span>')

        for media in verification_media_by_survey.get(survey_id, []):
            signed_url = create_supabase_signed_url(media["object_path"])
            filename = escape(str(media.get("original_filename") or "Foto verifikasi"))
            if signed_url:
                safe_url = escape(signed_url, quote=True)
                media_html.append(
                    f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">'
                    f'<img class="media-thumb" src="{safe_url}" alt="{filename}"></a>'
                    f'<div><strong>Foto Verifikasi DPS</strong><br><a href="{safe_url}" target="_blank" rel="noopener noreferrer">{filename}</a></div>'
                )
            else:
                media_html.append(f'<span>{filename} (pratinjau gagal)</span>')

        media_cell = "<br>".join(media_html) if media_html else '<span class="muted">Belum ada media</span>'
        verification_row = verification_by_survey.get(survey_id)
        needs_summary = "-"
        if verification_row:
            needs = verification_row.get("needs") or []
            if isinstance(needs, str):
                try:
                    needs = json.loads(needs)
                except ValueError:
                    needs = []
            needs_summary = "\n".join(
                f"- {item.get('item_name', 'Item')}: {item.get('quantity', 0)} {item.get('unit', 'unit')} x Rp {item.get('unit_price', 0):,.0f}"
                for item in needs
            ) or "Tidak ada item kebutuhan"

        detail_text = "\n".join([
            f"Pemohon: {row.get('applicant_name') or '-'}",
            f"NIK: {row.get('applicant_nik') or '-'}",
            f"Telepon: {row.get('applicant_phone') or '-'}",
            f"Alamat: {row.get('applicant_address') or '-'}",
            f"Desa: {row.get('village') or '-'}",
            f"Usaha: {row.get('business_name') or '-'}",
            f"Jenis usaha: {row.get('business_type') or '-'}",
            f"Mulai usaha: {row.get('business_since') or '-'}",
            f"Lokasi usaha: {row.get('business_location') or '-'}",
            f"Kepemilikan: {row.get('ownership_status') or '-'}",
            f"NIB: {row.get('nib') or '-'}",
            f"Modal: {row.get('capital_amount') if row.get('capital_amount') is not None else '-'}",
            f"Omzet: {row.get('turnover_amount') if row.get('turnover_amount') is not None else '-'}",
            f"Laba bersih: {row.get('net_profit_amount') if row.get('net_profit_amount') is not None else '-'}",
            f"Pekerja: {row.get('worker_count') if row.get('worker_count') is not None else '-'}",
            f"Status usaha: {row.get('business_active') or '-'}",
            f"Kendala: {row.get('business_issues') or '-'}",
            f"Bantuan diminta: {row.get('assistance_types') or '-'}",
            f"Perkiraan bantuan: {row.get('assistance_amount') if row.get('assistance_amount') is not None else '-'}",
            f"Alasan: {row.get('assistance_reason') or '-'}",
            f"Usaha ditemukan: {row.get('business_verified') or '-'}",
            f"Status verifikasi: {row.get('verification_status') or '-'}",
            f"Catatan verifikasi: {row.get('verification_notes') or '-'}",
            f"Catatan survei: {row.get('notes') or '-'}",
            f"\n=== HASIL VERIFIKASI DPS ===",
            f"Keputusan: {verification_row.get('decision') if verification_row else '-'}",
            f"Petugas DPS: {verification_row.get('verifier_name') if verification_row else '-'} ({verification_row.get('verifier_id') if verification_row else '-'})",
            f"Wilayah DPS: {verification_row.get('verifier_region') if verification_row else '-'}",
            f"Waktu verifikasi: {verification_row.get('verified_at') if verification_row else '-'}",
            f"Kondisi usaha: {verification_row.get('business_condition') if verification_row else '-'}",
            f"Aktivitas usaha aktual: {verification_row.get('actual_business_activity') if verification_row else '-'}",
            f"Aset yang sudah dimiliki: {verification_row.get('existing_assets') if verification_row else '-'}",
            f"Rincian kebutuhan: {needs_summary}",
            f"Total estimasi kebutuhan: Rp {verification_row.get('total_needs_amount', 0):,.0f}" if verification_row else "Total estimasi kebutuhan: -",
            f"Rincian modal kerja: {verification_row.get('working_capital_details') if verification_row else '-'}",
            f"Omzet terverifikasi: {verification_row.get('verified_turnover') if verification_row else '-'}",
            f"Laba terverifikasi: {verification_row.get('verified_profit') if verification_row else '-'}",
            f"Jumlah pekerja terverifikasi: {verification_row.get('verified_worker_count') if verification_row else '-'}",
            f"Kesesuaian data: {verification_row.get('data_match') if verification_row else '-'}",
            f"Perbedaan data: {verification_row.get('discrepancy_notes') if verification_row else '-'}",
            f"Catatan verifikasi: {verification_row.get('verification_notes') if verification_row else '-'}",
            f"Alasan penolakan: {verification_row.get('rejection_reason') if verification_row else '-'}",
            f"GPS verifikasi: {str(verification_row.get('latitude')) + ', ' + str(verification_row.get('longitude')) if verification_row else '-'}",
        ])

        table_rows += f"""
        <tr>
            <td>{survey_id}</td>
            <td>{escape(str(row['member_id']))}</td>
            <td class="notes">{escape(detail_text)}</td>
            <td>{lat}<br>{lon}<br><a href="{map_url}" target="_blank" rel="noopener noreferrer">Lihat Peta</a></td>
            <td>{escape(str(row['surveyed_at']))}</td>
            <td class="media-cell">{media_cell}</td>
            <td>
                <form method="post" action="/admin/surveys/{survey_id}/delete"
                      onsubmit="return confirm('Hapus permanen survei ID {survey_id} beserta foto dan videonya?');">
                    <button type="submit" class="delete-button">Delete</button>
                </form>
            </td>
        </tr>
        """

    if not table_rows:
        table_rows = '<tr><td colspan="7">Belum ada data survei.</td></tr>'

    page = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta http-equiv="refresh" content="60">
        <title>Dashboard Survei GPS</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 24px; background: #f1f5f9; color: #1e293b; }}
            h1 {{ margin-bottom: 8px; }}
            .info {{ margin-bottom: 20px; color: #475569; }}
            .card {{ background: white; padding: 18px; border-radius: 10px; margin-bottom: 20px; }}
            .table-wrap {{ overflow-x: auto; }}
            table {{ border-collapse: collapse; width: 100%; background: white; font-size: 14px; }}
            th, td {{ border: 1px solid #e2e8f0; padding: 10px; text-align: left; vertical-align: top; }}
            th {{ background: #e2e8f0; white-space: nowrap; }}
            tr:nth-child(even) {{ background: #f8fafc; }}
            a {{ color: #0369a1; font-weight: bold; }}
            .notes {{ min-width: 280px; max-width: 440px; white-space: pre-wrap; }}
            .media-cell {{ min-width: 180px; max-width: 260px; overflow-wrap: anywhere; }}
            .media-thumb {{ display: block; width: auto; max-width: 180px; max-height: 150px; border-radius: 6px; margin: 4px 0; }}
            .media-video {{ display: block; width: 220px; max-width: 100%; margin: 4px 0; }}
            small {{ display: block; color: #64748b; margin-bottom: 8px; }}
            .muted {{ color: #64748b; }}
            .delete-button {{ background: #b91c1c; color: #fff; border: 0; border-radius: 5px; padding: 8px 10px; cursor: pointer; }}
            .delete-button:hover {{ background: #991b1b; }}
            form {{ margin: 0; }}
        </style>
    </head>
    <body>
        <h1>Dashboard Survei GPS</h1>
        <div class="info">Login admin berhasil. Data diperbarui otomatis setiap 60 detik. Pratinjau media menggunakan tautan sementara untuk bucket privat.</div>
        <div class="card"><strong>Total data survei:</strong> {len(rows)} &nbsp; | &nbsp; <strong>Total file media:</strong> {len(media_rows) + len(verification_media_rows)} &nbsp; | &nbsp; <strong>Total hasil verifikasi:</strong> {len(verification_rows)}</div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>ID</th><th>ID Anggota</th><th>Data Pemohon, Usaha & Bantuan</th>
                        <th>Lokasi GPS</th><th>Waktu Survei</th><th>Dokumen / Foto / Video</th><th>Aksi</th>
                    </tr>
                </thead>
                <tbody>{table_rows}</tbody>
            </table>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=page)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
