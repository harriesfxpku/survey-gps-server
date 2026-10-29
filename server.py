from datetime import datetime, timezone, timedelta
import os
import json
import secrets
import hashlib
import re
from fastapi import UploadFile, File, Form

import psycopg
import uvicorn

import uuid
import urllib.request
import urllib.error
from urllib.parse import quote
from fastapi import Request, Header

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import APIKeyHeader, HTTPBasic, HTTPBasicCredentials
from fastapi import Security
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from html import escape


# =========================================================
# KONFIGURASI
# =========================================================

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# Normalize common provider URL schemes for psycopg 3.
if DATABASE_URL.startswith("//"):
    DATABASE_URL = "postgresql:" + DATABASE_URL
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
elif DATABASE_URL.startswith("postgresql+psycopg://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgresql+psycopg://"):]

API_TOKEN = os.environ.get("API_TOKEN", "")

api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
)

app = FastAPI(
    title="Survei GPS API",
    description="Server pusat aplikasi Survei GPS. Pengelolaan akun menggunakan istilah DPS (Kecamatan) dan DPSS (Desa/Kelurahan).",
    version="3.2.2-dps-district-surveys-fix",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# KONEKSI DATABASE
# =========================================================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL belum dikonfigurasi."
        )

    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=15,
    )


# =========================================================
# MEMBUAT TABEL DAN MEMASTIKAN ANGGOTA TES AKTIF
# =========================================================

def initialize_database():
    with get_db() as connection:

        connection.execute("""
            CREATE TABLE IF NOT EXISTS members (
                id BIGSERIAL PRIMARY KEY,
                member_id VARCHAR(50) UNIQUE NOT NULL,
                member_name VARCHAR(100) NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS surveys (
                id BIGSERIAL PRIMARY KEY,
                member_id VARCHAR(50) NOT NULL,
                latitude DOUBLE PRECISION NOT NULL,
                longitude DOUBLE PRECISION NOT NULL,
                accuracy DOUBLE PRECISION,
                notes TEXT NOT NULL DEFAULT '',
                surveyed_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
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
                capital_amount DOUBLE PRECISION,
                turnover_amount DOUBLE PRECISION,
                net_profit_amount DOUBLE PRECISION,
                worker_count INTEGER,
                business_active TEXT NOT NULL DEFAULT '',
                business_issues TEXT NOT NULL DEFAULT '',
                assistance_types TEXT NOT NULL DEFAULT '',
                assistance_amount DOUBLE PRECISION,
                assistance_reason TEXT NOT NULL DEFAULT '',
                business_verified TEXT NOT NULL DEFAULT '',
                verification_status TEXT NOT NULL DEFAULT '',
                verification_notes TEXT NOT NULL DEFAULT ''
            )
        """)

        # Migrasi aman untuk database yang sudah berisi data survei lama.
        migration_columns = {
            "applicant_name": "TEXT NOT NULL DEFAULT ''",
            "applicant_nik": "TEXT NOT NULL DEFAULT ''",
            "applicant_phone": "TEXT NOT NULL DEFAULT ''",
            "applicant_address": "TEXT NOT NULL DEFAULT ''",
            "village": "TEXT NOT NULL DEFAULT ''",
            "business_name": "TEXT NOT NULL DEFAULT ''",
            "business_type": "TEXT NOT NULL DEFAULT ''",
            "business_since": "TEXT NOT NULL DEFAULT ''",
            "business_location": "TEXT NOT NULL DEFAULT ''",
            "ownership_status": "TEXT NOT NULL DEFAULT ''",
            "nib": "TEXT NOT NULL DEFAULT ''",
            "capital_amount": "DOUBLE PRECISION",
            "turnover_amount": "DOUBLE PRECISION",
            "net_profit_amount": "DOUBLE PRECISION",
            "worker_count": "INTEGER",
            "business_active": "TEXT NOT NULL DEFAULT ''",
            "business_issues": "TEXT NOT NULL DEFAULT ''",
            "assistance_types": "TEXT NOT NULL DEFAULT ''",
            "assistance_amount": "DOUBLE PRECISION",
            "assistance_reason": "TEXT NOT NULL DEFAULT ''",
            "business_verified": "TEXT NOT NULL DEFAULT ''",
            "verification_status": "TEXT NOT NULL DEFAULT ''",
            "verification_notes": "TEXT NOT NULL DEFAULT ''",
        }
        for column_name, column_type in migration_columns.items():
            connection.execute(
                f"ALTER TABLE surveys ADD COLUMN IF NOT EXISTS {column_name} {column_type}"
            )

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
            )
        """)
        connection.execute(
            "ALTER TABLE survey_media ADD COLUMN IF NOT EXISTS media_category VARCHAR(40) NOT NULL DEFAULT 'business_photo'"
        )
        connection.execute(
            "ALTER TABLE survey_media ADD COLUMN IF NOT EXISTS storage_bucket VARCHAR(100) NOT NULL DEFAULT 'survey-media'"
        )

        # Akun petugas dan sesi login ditambahkan tanpa mengubah tabel survei/media lama.
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
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS officer_sessions (
                id BIGSERIAL PRIMARY KEY,
                officer_id BIGINT NOT NULL REFERENCES officers(id) ON DELETE CASCADE,
                token_hash VARCHAR(64) UNIQUE NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # Migrate officer columns used by login/admin on older databases.
        connection.execute("ALTER TABLE officers ADD COLUMN IF NOT EXISTS password_salt VARCHAR(64)")
        connection.execute("ALTER TABLE officers ADD COLUMN IF NOT EXISTS password_hash VARCHAR(128)")
        connection.execute("ALTER TABLE officers ADD COLUMN IF NOT EXISTS role VARCHAR(10) NOT NULL DEFAULT 'DPSS'")
        connection.execute("ALTER TABLE officers ADD COLUMN IF NOT EXISTS region VARCHAR(150) NOT NULL DEFAULT ''")
        connection.execute("ALTER TABLE officers ADD COLUMN IF NOT EXISTS active INTEGER NOT NULL DEFAULT 1")
        connection.execute("ALTER TABLE officers ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        # Data verifikasi dipisahkan dari survei awal agar data DPSS tidak tertimpa.
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
                verified_turnover DOUBLE PRECISION,
                verified_profit DOUBLE PRECISION,
                verified_worker_count INTEGER,
                data_match VARCHAR(40) NOT NULL DEFAULT '',
                discrepancy_notes TEXT NOT NULL DEFAULT '',
                verification_notes TEXT NOT NULL DEFAULT '',
                rejection_reason TEXT NOT NULL DEFAULT '',
                latitude DOUBLE PRECISION NOT NULL,
                longitude DOUBLE PRECISION NOT NULL,
                accuracy DOUBLE PRECISION,
                verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                total_needs_amount DOUBLE PRECISION NOT NULL DEFAULT 0
            )
        """)
        # Ensure verification columns exist when upgrading an older database.
        verification_migrations = {
            "verifier_region": "VARCHAR(150) NOT NULL DEFAULT ''",
            "business_condition": "TEXT NOT NULL DEFAULT ''",
            "actual_business_activity": "TEXT NOT NULL DEFAULT ''",
            "existing_assets": "TEXT NOT NULL DEFAULT ''",
            "needs": "JSONB NOT NULL DEFAULT '[]'::jsonb",
            "working_capital_details": "TEXT NOT NULL DEFAULT ''",
            "verified_turnover": "DOUBLE PRECISION",
            "verified_profit": "DOUBLE PRECISION",
            "verified_worker_count": "INTEGER",
            "data_match": "VARCHAR(40) NOT NULL DEFAULT ''",
            "discrepancy_notes": "TEXT NOT NULL DEFAULT ''",
            "verification_notes": "TEXT NOT NULL DEFAULT ''",
            "rejection_reason": "TEXT NOT NULL DEFAULT ''",
            "latitude": "DOUBLE PRECISION",
            "longitude": "DOUBLE PRECISION",
            "accuracy": "DOUBLE PRECISION",
            "verified_at": "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
            "total_needs_amount": "DOUBLE PRECISION NOT NULL DEFAULT 0",
        }
        for column_name, column_type in verification_migrations.items():
            connection.execute(
                f"ALTER TABLE survey_verifications ADD COLUMN IF NOT EXISTS {column_name} {column_type}"
            )

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
            )
        """)
        # Konfigurasi desa dan penugasan petugas, ditambahkan tanpa menghapus data lama.
        connection.execute("""CREATE TABLE IF NOT EXISTS villages (
            id BIGSERIAL PRIMARY KEY, village_name VARCHAR(150) UNIQUE NOT NULL,
            district VARCHAR(150) NOT NULL DEFAULT 'Tambang', bucket_name VARCHAR(100) NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        connection.execute("""CREATE TABLE IF NOT EXISTS officer_villages (
            officer_id BIGINT NOT NULL REFERENCES officers(id) ON DELETE CASCADE,
            village_id BIGINT NOT NULL REFERENCES villages(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY (officer_id, village_id))""")
        connection.execute("ALTER TABLE surveys ADD COLUMN IF NOT EXISTS village_id BIGINT REFERENCES villages(id)")
        connection.execute("ALTER TABLE survey_media ADD COLUMN IF NOT EXISTS storage_bucket VARCHAR(100) NOT NULL DEFAULT 'survey-media'")
        connection.execute("ALTER TABLE verification_media ADD COLUMN IF NOT EXISTS storage_bucket VARCHAR(100) NOT NULL DEFAULT 'survey-media'")
        connection.execute("""INSERT INTO villages (village_name, district, bucket_name) VALUES
            ('Aur Sati', 'Tambang', 'Aur-Sati-Kec-Tambang'),
            ('Balam Jaya', 'Tambang', 'Balam-Jaya-Kec-Tambang')
            ON CONFLICT (village_name)
            DO UPDATE SET district = EXCLUDED.district""")
        # Hanya tautkan survei lama jika nama desa cocok persis; media lama tetap di bucket survey-media.
        connection.execute("""UPDATE surveys s SET village_id = v.id FROM villages v
            WHERE s.village_id IS NULL AND LOWER(TRIM(s.village)) = LOWER(v.village_name)""")

        # Tidak membuat atau mengaktifkan ulang akun anggota secara otomatis saat startup.


def get_village_bucket(connection, village_id):
    if village_id is None:
        return os.environ.get("SUPABASE_DEFAULT_BUCKET", "survey-media")
    row = connection.execute("SELECT bucket_name FROM villages WHERE id = %s AND active = 1", (village_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=400, detail="Desa survei tidak dikenal atau tidak aktif.")
    return row["bucket_name"]


def ensure_dps_survey_access(connection, identity, survey_id):
    # DPS (Kecamatan) dapat membuka survei dari desa di kecamatannya.
    # Penugasan desa eksplisit tetap didukung untuk menjaga kompatibilitas.
    row = connection.execute("""SELECT s.id
        FROM surveys s
        JOIN officers o ON o.member_id = %s AND o.active = 1
        WHERE s.id = %s
          AND (
            EXISTS (
                SELECT 1 FROM officer_villages ov
                WHERE ov.officer_id = o.id AND ov.village_id = s.village_id
            )
            OR EXISTS (
                SELECT 1 FROM villages v
                WHERE v.active = 1
                  AND lower(trim(regexp_replace(coalesce(v.district, ''), '^(kecamatan|kec\\.?)[[:space:]]+', '', 'i'))) =
                      lower(trim(regexp_replace(regexp_replace(coalesce(o.region, ''), '^(kecamatan|kec\\.?)[[:space:]]+', '', 'i'), '[,;].*$', '')))
                  AND (
                      v.id = s.village_id
                      OR lower(trim(regexp_replace(coalesce(v.village_name, ''), '^(desa|kelurahan)[[:space:]]+', '', 'i'))) =
                         lower(trim(regexp_replace(coalesce(s.village, ''), '^(desa|kelurahan)[[:space:]]+', '', 'i')))
                  )
            )
          )""", (identity["member_id"], survey_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=403, detail="Survei ini berada di luar wilayah kecamatan akun DPS.")


@app.on_event("startup")
def startup_event():
    initialize_database()


# =========================================================
# AUTENTIKASI
# =========================================================

def verify_api_key(
    api_key: str = Depends(api_key_header),
):
    if (
        not API_TOKEN
        or not api_key
        or not secrets.compare_digest(api_key, API_TOKEN)
    ):
        raise HTTPException(
            status_code=401,
            detail="Kunci akses tidak valid",
        )

    return True


# =========================================================
# AUTENTIKASI PETUGAS
# =========================================================

def _password_hash(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 310_000
    ).hex()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_request_identity(
    authorization: str | None = Header(default=None),
    api_key: str | None = Depends(api_key_header),
):
    # API key lama tetap diterima untuk kompatibilitas dashboard/versi aplikasi lama.
    if API_TOKEN and api_key and secrets.compare_digest(api_key, API_TOKEN):
        return {"auth_type": "api_key", "role": "LEGACY_ADMIN"}

    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            with get_db() as connection:
                row = connection.execute(
                    """
                    SELECT o.member_id, o.member_name, o.role, o.region, o.active,
                           s.expires_at
                    FROM officer_sessions s
                    JOIN officers o ON o.id = s.officer_id
                    WHERE s.token_hash = %s AND s.expires_at > NOW()
                    """,
                    (_token_hash(token),),
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


def get_officer_identity(
    authorization: str | None = Header(default=None),
):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Silakan login menggunakan akun petugas.")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Token login tidak valid.")
    with get_db() as connection:
        row = connection.execute(
            """
            SELECT o.member_id, o.member_name, o.role, o.region, o.active, s.expires_at
            FROM officer_sessions s
            JOIN officers o ON o.id = s.officer_id
            WHERE s.token_hash = %s AND s.expires_at > NOW()
            """,
            (_token_hash(token),),
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


class OfficerLogin(BaseModel):
    member_id: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=200)


class OfficerCreate(BaseModel):
    member_id: str = Field(min_length=2, max_length=50)
    member_name: str = Field(min_length=2, max_length=100)
    password: str = Field(min_length=10, max_length=200)
    role: str
    region: str = Field(min_length=1, max_length=150)


@app.post("/auth/login")
def officer_login(payload: OfficerLogin):
    member_id = payload.member_id.strip().upper()
    with get_db() as connection:
        officer = connection.execute(
            "SELECT * FROM officers WHERE member_id = %s", (member_id,)
        ).fetchone()
        if (
            not officer
            or officer["active"] != 1
            or not officer.get("password_salt")
            or not officer.get("password_hash")
            or not secrets.compare_digest(
                _password_hash(payload.password, officer["password_salt"]),
                officer["password_hash"],
            )
        ):
            raise HTTPException(status_code=401, detail="ID petugas atau kata sandi salah, atau akun tidak aktif.")
        token = secrets.token_urlsafe(48)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=12)
        connection.execute(
            "DELETE FROM officer_sessions WHERE expires_at <= NOW()"
        )
        connection.execute(
            "INSERT INTO officer_sessions (officer_id, token_hash, expires_at) VALUES (%s, %s, %s)",
            (officer["id"], _token_hash(token), expires_at),
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
def officer_logout(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sesi login tidak ditemukan.")
    token = authorization[7:].strip()
    with get_db() as connection:
        connection.execute("DELETE FROM officer_sessions WHERE token_hash = %s", (_token_hash(token),))
    return {"message": "Berhasil keluar."}


@app.post("/admin/officers", include_in_schema=False)
@app.post("/admin/akun-petugas", tags=["Pengelolaan Akun"], summary="Buat akun DPS atau DPSS")
def create_officer(
    payload: OfficerCreate,
    _: bool = Depends(verify_api_key),
):
    member_id = payload.member_id.strip().upper()
    member_name = payload.member_name.strip()
    role = payload.role.strip().upper()
    region = payload.region.strip()
    if role not in {"DPSS", "DPS"}:
        raise HTTPException(status_code=400, detail="Role harus DPSS atau DPS.")
    salt = secrets.token_hex(16)
    hashed = _password_hash(payload.password, salt)
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as connection:
        existing = connection.execute(
            "SELECT member_id FROM officers WHERE member_id = %s", (member_id,)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Akun petugas dengan ID tersebut sudah ada.")
        connection.execute(
            """INSERT INTO members (member_id, member_name, active, created_at)
               VALUES (%s, %s, 1, %s)
               ON CONFLICT (member_id) DO UPDATE SET member_name = EXCLUDED.member_name, active = 1""",
            (member_id, member_name, now),
        )
        connection.execute(
            """INSERT INTO officers (member_id, member_name, password_salt, password_hash, role, region, active)
               VALUES (%s, %s, %s, %s, %s, %s, 1)""",
            (member_id, member_name, salt, hashed, role, region),
        )
    return {"message": "Akun petugas berhasil dibuat.", "member_id": member_id, "member_name": member_name, "role": role, "region": region, "active": True}


@app.get("/admin/officers", include_in_schema=False)
@app.get("/admin/akun-petugas", tags=["Pengelolaan Akun"], summary="Lihat daftar akun DPS dan DPSS")
def list_officers(_: bool = Depends(verify_api_key)):
    with get_db() as connection:
        rows = connection.execute(
            """SELECT o.member_id, o.member_name, o.role, o.region, o.active,
                      o.created_at, o.updated_at,
                      ARRAY(SELECT v.village_name
                            FROM officer_villages ov
                            JOIN villages v ON v.id = ov.village_id
                            WHERE ov.officer_id = o.id
                            ORDER BY v.village_name) AS villages
               FROM officers o ORDER BY o.role, o.region, o.member_id"""
        ).fetchall()
    return [dict(row) for row in rows]


class VillageCreate(BaseModel):
    village_name: str = Field(min_length=2, max_length=150)
    district: str = Field(default="Tambang", min_length=2, max_length=150)
    bucket_name: str | None = Field(default=None, max_length=100)


def _storage_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")
    return slug[:100] or "survey-media"


@app.get("/admin/villages", include_in_schema=False)
@app.get("/admin/data-desa", tags=["Data Wilayah"], summary="Lihat daftar desa dan kecamatan")
def list_villages(_: bool = Depends(verify_api_key)):
    with get_db() as connection:
        rows = connection.execute(
            "SELECT id, village_name, district, bucket_name, active, created_at FROM villages ORDER BY district, village_name"
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/admin/villages", include_in_schema=False)
@app.post("/admin/data-desa", tags=["Data Wilayah"], summary="Tambah data desa dan kecamatan")
def create_village(payload: VillageCreate, _: bool = Depends(verify_api_key)):
    village_name = payload.village_name.strip()
    district = payload.district.strip()
    bucket_name = (payload.bucket_name or _storage_slug(village_name)).strip()
    if not village_name or not district or not bucket_name:
        raise HTTPException(status_code=422, detail="Nama desa, kecamatan, dan nama bucket wajib diisi.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,99}", bucket_name):
        raise HTTPException(status_code=422, detail="Nama bucket hanya boleh berisi huruf kecil, angka, dan tanda hubung; panjang 2-100 karakter.")
    try:
        with get_db() as connection:
            row = connection.execute(
                """INSERT INTO villages (village_name, district, bucket_name)
                   VALUES (%s, %s, %s)
                   RETURNING id, village_name, district, bucket_name, active, created_at""",
                (village_name, district, bucket_name),
            ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="Nama desa atau nama bucket sudah terdaftar.") from exc
    return {"message": "Desa berhasil didaftarkan. Pastikan bucket dengan nama tersebut sudah dibuat sebagai PRIVATE di Supabase Storage.", "village": dict(row)}


class OfficerVillageAssignment(BaseModel):
    villages: list[str] = Field(min_length=1, max_length=20)


@app.post("/admin/officers/{member_id}/villages", include_in_schema=False)
@app.post("/admin/akun-petugas/{member_id}/penugasan-desa", tags=["Pengelolaan Akun"], summary="Atur desa tugas akun petugas")
def assign_officer_villages(
    member_id: str,
    payload: OfficerVillageAssignment,
    _: bool = Depends(verify_api_key),
):
    """Ganti daftar desa tugas petugas. villages berisi nama desa yang terdaftar."""
    normalized_id = member_id.strip().upper()
    requested = list(dict.fromkeys(name.strip() for name in payload.villages if name.strip()))
    if not requested:
        raise HTTPException(status_code=422, detail="Isi minimal satu nama desa.")
    with get_db() as connection:
        officer = connection.execute("SELECT id, role FROM officers WHERE member_id=%s AND active=1", (normalized_id,)).fetchone()
        if not officer:
            raise HTTPException(status_code=404, detail="Akun petugas aktif tidak ditemukan.")
        villages = connection.execute("SELECT id, village_name FROM villages WHERE active=1 AND LOWER(village_name)=ANY(%s)", ([v.lower() for v in requested],)).fetchall()
        found = {v["village_name"].lower() for v in villages}
        missing = [v for v in requested if v.lower() not in found]
        if missing:
            raise HTTPException(status_code=400, detail="Desa tidak dikenal: " + ", ".join(missing))
        connection.execute("DELETE FROM officer_villages WHERE officer_id=%s", (officer["id"],))
        for village in villages:
            connection.execute("INSERT INTO officer_villages (officer_id, village_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (officer["id"], village["id"]))
    return {"message": "Penugasan desa berhasil diperbarui.", "member_id": normalized_id, "villages": [v["village_name"] for v in villages]}


@app.post("/admin/officers/{member_id}/deactivate", include_in_schema=False)
@app.post("/admin/akun-petugas/{member_id}/nonaktifkan", tags=["Pengelolaan Akun"], summary="Nonaktifkan akun petugas")
def deactivate_officer(member_id: str, _: bool = Depends(verify_api_key)):
    with get_db() as connection:
        result = connection.execute(
            "UPDATE officers SET active = 0, updated_at = NOW() WHERE member_id = %s",
            (member_id.strip().upper(),),
        )
        connection.execute(
            "DELETE FROM officer_sessions WHERE officer_id IN (SELECT id FROM officers WHERE member_id = %s)",
            (member_id.strip().upper(),),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Akun petugas tidak ditemukan.")
        connection.execute("UPDATE members SET active = 0 WHERE member_id = %s", (member_id.strip().upper(),))
    return {"message": "Akun petugas dinonaktifkan.", "member_id": member_id.strip().upper()}


@app.delete("/admin/akun-petugas/{member_id}", tags=["Pengelolaan Akun"], summary="Hapus akun petugas")
def delete_officer(member_id: str, _: bool = Depends(verify_api_key)):
    """Hapus akses akun petugas tanpa menghapus riwayat survei yang sudah dikirim."""
    normalized_id = member_id.strip().upper()
    with get_db() as connection:
        officer = connection.execute(
            "SELECT id FROM officers WHERE member_id = %s", (normalized_id,)
        ).fetchone()
        if officer is None:
            raise HTTPException(status_code=404, detail="Akun petugas tidak ditemukan.")
        connection.execute(
            "DELETE FROM officer_sessions WHERE officer_id = %s", (officer["id"],)
        )
        connection.execute(
            "DELETE FROM officers WHERE member_id = %s", (normalized_id,)
        )
        # Pertahankan baris anggota nonaktif untuk menjaga riwayat dan kompatibilitas data lama.
        connection.execute(
            "UPDATE members SET active = 0 WHERE member_id = %s", (normalized_id,)
        )
    return {"message": "Akun petugas berhasil dihapus. Riwayat survei tetap tersimpan.", "member_id": normalized_id}


# =========================================================
# MODEL DATA
# =========================================================

class MemberCreate(BaseModel):
    member_id: str = Field(
        min_length=1,
        max_length=50,
    )

    member_name: str = Field(
        min_length=1,
        max_length=100,
    )


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
    needs: list[AssistanceNeedItem] = Field(default_factory=list, max_length=50)
    working_capital_details: str = Field(default="", max_length=3000)
    verified_turnover: float | None = Field(default=None, ge=0)
    verified_profit: float | None = Field(default=None, ge=0)
    verified_worker_count: int | None = Field(default=None, ge=0, le=100000)
    data_match: str = Field(default="", max_length=40)
    discrepancy_notes: str = Field(default="", max_length=3000)
    verification_notes: str = Field(default="", max_length=3000)
    rejection_reason: str = Field(default="", max_length=3000)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)


class SurveyCreate(BaseModel):
    member_id: str = Field(
        min_length=1,
        max_length=50,
    )

    latitude: float = Field(
        ge=-90,
        le=90,
    )

    longitude: float = Field(
        ge=-180,
        le=180,
    )

    accuracy: float | None = Field(
        default=None,
        ge=0,
    )

    notes: str = Field(
        default="",
        max_length=5000,
    )

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
    capital_amount: float | None = Field(default=None, ge=0)
    turnover_amount: float | None = Field(default=None, ge=0)
    net_profit_amount: float | None = Field(default=None, ge=0)
    worker_count: int | None = Field(default=None, ge=0, le=100000)
    business_active: str = Field(default="", max_length=50)
    business_issues: str = Field(default="", max_length=3000)
    assistance_types: str = Field(default="", max_length=1000)
    assistance_amount: float | None = Field(default=None, ge=0)
    assistance_reason: str = Field(default="", max_length=3000)
    business_verified: str = Field(default="", max_length=50)
    verification_status: str = Field(default="", max_length=50)
    verification_notes: str = Field(default="", max_length=3000)


# =========================================================
# HALAMAN UTAMA
# =========================================================

@app.get("/")
def home():
    return {
        "server": "Survei GPS",
        "status": "online",
        "message": "Server berjalan dengan baik",
    }


@app.get("/health")
def health():
    with get_db() as connection:
        connection.execute("SELECT 1")

    return {
        "status": "ok",
        "database": "connected",
    }


# =========================================================
# MANAJEMEN ANGGOTA
# =========================================================

@app.post("/admin/members")
def create_member(
    member: MemberCreate,
    _: bool = Depends(verify_api_key),
):
    member_id = member.member_id.strip()
    member_name = member.member_name.strip()

    if not member_id or not member_name:
        raise HTTPException(
            status_code=400,
            detail="ID dan nama anggota wajib diisi",
        )

    try:
        with get_db() as connection:
            connection.execute(
                """
                INSERT INTO members (
                    member_id,
                    member_name,
                    created_at
                )
                VALUES (%s, %s, %s)
                """,
                (
                    member_id,
                    member_name,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    except psycopg.errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="ID anggota sudah terdaftar",
        )

    return {
        "message": "Anggota berhasil didaftarkan",
        "member_id": member_id,
        "member_name": member_name,
    }


@app.get("/admin/members")
def list_members(
    _: bool = Depends(verify_api_key),
):
    with get_db() as connection:
        rows = connection.execute(
            """
            SELECT
                member_id,
                member_name,
                active,
                created_at
            FROM members
            ORDER BY member_id
            """
        ).fetchall()

    return [dict(row) for row in rows]


# =========================================================
# PENGIRIMAN DATA SURVEI GPS
# =========================================================

@app.post("/surveys")
def create_survey(
    survey: SurveyCreate,
    identity: dict = Depends(get_request_identity),
):
    if identity["auth_type"] == "officer" and identity.get("role") != "DPSS":
        raise HTTPException(status_code=403, detail="Akun DPS menggunakan menu verifikasi, bukan membuat survei awal.")
    member_id = (identity["member_id"] if identity["auth_type"] == "officer" else survey.member_id).strip()

    with get_db() as connection:
        member = connection.execute(
            """
            SELECT member_id
            FROM members
            WHERE member_id = %s
              AND active = 1
            """,
            (member_id,),
        ).fetchone()

        if member is None:
            raise HTTPException(
                status_code=403,
                detail=(
                    "ID anggota tidak terdaftar "
                    "atau tidak aktif"
                ),
            )

        row = connection.execute(
            """
            INSERT INTO surveys (
                member_id, latitude, longitude, accuracy, notes,
                surveyed_at, received_at, applicant_name, applicant_nik,
                applicant_phone, applicant_address, village, business_name,
                business_type, business_since, business_location, ownership_status,
                nib, capital_amount, turnover_amount, net_profit_amount,
                worker_count, business_active, business_issues, assistance_types,
                assistance_amount, assistance_reason, business_verified,
                verification_status, verification_notes
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            RETURNING id
            """,
            (
                member_id, survey.latitude, survey.longitude, survey.accuracy,
                survey.notes, survey.surveyed_at.isoformat(),
                datetime.now(timezone.utc).isoformat(), survey.applicant_name.strip(),
                survey.applicant_nik.strip(), survey.applicant_phone.strip(),
                survey.applicant_address.strip(), survey.village.strip(),
                survey.business_name.strip(), survey.business_type.strip(),
                survey.business_since.strip(), survey.business_location.strip(),
                survey.ownership_status.strip(), survey.nib.strip(),
                survey.capital_amount, survey.turnover_amount, survey.net_profit_amount,
                survey.worker_count, survey.business_active.strip(),
                survey.business_issues.strip(), survey.assistance_types.strip(),
                survey.assistance_amount, survey.assistance_reason.strip(),
                "Belum diverifikasi",
                "Perlu ditinjau",
                "",
            ),
        ).fetchone()

        survey_id = row["id"]
        village_name = survey.village.strip()
        village_row = None
        if village_name:
            village_row = connection.execute(
                "SELECT id, village_name FROM villages WHERE LOWER(TRIM(village_name)) = LOWER(%s) AND active = 1",
                (village_name,),
            ).fetchone()
        if identity["auth_type"] == "officer":
            if village_row is None:
                raise HTTPException(status_code=400, detail="Pilih desa yang terdaftar: Aur Sati atau Balam Jaya.")
            assigned = connection.execute("""SELECT 1 FROM officer_villages ov JOIN officers o ON o.id=ov.officer_id
                WHERE o.member_id=%s AND o.active=1 AND ov.village_id=%s""", (member_id, village_row["id"])).fetchone()
            if not assigned:
                raise HTTPException(status_code=403, detail="Akun DPSS belum ditugaskan ke desa survei ini.")
            connection.execute("UPDATE surveys SET village_id=%s, village=%s WHERE id=%s", (village_row["id"], village_row["village_name"], survey_id))
        elif village_row:
            connection.execute("UPDATE surveys SET village_id=%s, village=%s WHERE id=%s", (village_row["id"], village_row["village_name"], survey_id))

    return {
        "message": "Data survei berhasil disimpan",
        "survey_id": survey_id,
        "member_id": member_id,
    }


# =========================================================
# MELIHAT HASIL SURVEI
# =========================================================

@app.get("/admin/surveys")
def list_surveys(
    member_id: str | None = None,
    _: bool = Depends(verify_api_key),
):
    with get_db() as connection:
        if member_id:
            rows = connection.execute(
                """
                SELECT *
                FROM surveys
                WHERE member_id = %s
                ORDER BY id DESC
                """,
                (member_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT *
                FROM surveys
                ORDER BY id DESC
                """
            ).fetchall()

    return [dict(row) for row in rows]



# =========================================================
# UNGGAH FOTO DAN VIDEO SURVEI
# =========================================================

def _validate_file_signature(content: bytes, content_type: str) -> bool:
    """Validasi sederhana signature berkas, bukan hanya Content-Type dari klien."""
    if content_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/webp":
        return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    if content_type in {"video/mp4", "video/quicktime"}:
        return len(content) >= 12 and content[4:8] == b"ftyp"
    return False


def require_dps(identity: dict = Depends(get_officer_identity)):
    if identity.get("role") != "DPS":
        raise HTTPException(status_code=403, detail="Fitur ini khusus akun petugas DPS.")
    return identity


@app.get("/dps/surveys")
def list_surveys_for_verification(
    include_completed: bool = False,
    identity: dict = Depends(require_dps),
):
    """Daftar survei awal yang dapat dibuka petugas DPS untuk verifikasi."""
    with get_db() as connection:
        # DPS melihat survei dari seluruh desa dalam kecamatan pada akun DPS.
        # Penugasan desa manual yang sudah ada juga tetap dihormati.
        base_query = """SELECT DISTINCT s.*
            FROM surveys s
            JOIN officers o ON o.member_id = %s AND o.active = 1
            WHERE (
                EXISTS (
                    SELECT 1 FROM officer_villages ov
                    WHERE ov.officer_id = o.id AND ov.village_id = s.village_id
                )
                OR EXISTS (
                    SELECT 1 FROM villages v
                    WHERE v.active = 1
                      AND lower(trim(regexp_replace(coalesce(v.district, ''), '^(kecamatan|kec\\.?)[[:space:]]+', '', 'i'))) =
                          lower(trim(regexp_replace(regexp_replace(coalesce(o.region, ''), '^(kecamatan|kec\\.?)[[:space:]]+', '', 'i'), '[,;].*$', '')))
                      AND (
                          v.id = s.village_id
                          OR lower(trim(regexp_replace(coalesce(v.village_name, ''), '^(desa|kelurahan)[[:space:]]+', '', 'i'))) =
                             lower(trim(regexp_replace(coalesce(s.village, ''), '^(desa|kelurahan)[[:space:]]+', '', 'i')))
                      )
                )
            )"""
        if not include_completed:
            base_query += " AND COALESCE(s.verification_status, '') NOT IN ('Sudah diverifikasi', 'Ditolak')"
        base_query += " ORDER BY s.id DESC"
        rows = connection.execute(base_query, (identity["member_id"],)).fetchall()
    return [dict(row) for row in rows]


@app.get("/dps/surveys/{survey_id}")
def get_survey_for_verification(
    survey_id: int,
    identity: dict = Depends(require_dps),
):
    with get_db() as connection:
        survey = connection.execute("SELECT * FROM surveys WHERE id = %s", (survey_id,)).fetchone()
        if survey is None:
            raise HTTPException(status_code=404, detail="Data survei tidak ditemukan.")
        ensure_dps_survey_access(connection, identity, survey_id)
        original_media = connection.execute(
            """SELECT id, media_category, content_type, original_filename, object_path, file_size, uploaded_at, storage_bucket
               FROM survey_media WHERE survey_id = %s ORDER BY id""", (survey_id,)
        ).fetchall()
        verification = connection.execute(
            "SELECT * FROM survey_verifications WHERE survey_id = %s", (survey_id,)
        ).fetchone()
        verification_media = connection.execute(
            """SELECT id, content_type, original_filename, object_path, file_size, uploaded_at, storage_bucket
               FROM verification_media WHERE survey_id = %s ORDER BY id""", (survey_id,)
        ).fetchall()

    def media_with_url(row):
        item = dict(row)
        item["signed_url"] = create_supabase_signed_url(item["object_path"], bucket=item.get("storage_bucket", "survey-media"))
        return item

    return {
        "survey": dict(survey),
        "survey_media": [media_with_url(row) for row in original_media],
        "verification": dict(verification) if verification else None,
        "verification_media": [media_with_url(row) for row in verification_media],
    }


@app.post("/dps/surveys/{survey_id}/verification-media")
async def upload_verification_media(
    survey_id: int,
    file: UploadFile = File(...),
    identity: dict = Depends(require_dps),
):
    content_type = (file.content_type or "").lower()
    extensions = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    if content_type not in extensions:
        raise HTTPException(status_code=400, detail="Bukti verifikasi harus berupa foto JPEG, PNG, atau WebP.")
    content = await file.read(20 * 1024 * 1024 + 1)
    if not content:
        raise HTTPException(status_code=400, detail="File foto kosong.")
    if not _validate_file_signature(content, content_type):
        raise HTTPException(status_code=400, detail="Isi file tidak cocok dengan format foto yang dikirim.")
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Ukuran setiap foto verifikasi maksimal 20 MB.")
    supabase_url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    supabase_key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if not supabase_url or not supabase_key:
        raise HTTPException(status_code=503, detail="Konfigurasi penyimpanan Supabase belum lengkap.")
    with get_db() as connection:
        survey = connection.execute("SELECT id, village_id FROM surveys WHERE id = %s", (survey_id,)).fetchone()
        if survey is None:
            raise HTTPException(status_code=404, detail="Data survei tidak ditemukan.")
        ensure_dps_survey_access(connection, identity, survey_id)
        storage_bucket = get_village_bucket(connection, survey["village_id"])
        count = connection.execute("SELECT COUNT(*) AS total FROM verification_media WHERE survey_id = %s", (survey_id,)).fetchone()["total"]
        if count >= 5:
            raise HTTPException(status_code=400, detail="Maksimal 5 foto bukti verifikasi per survei.")

    object_path = f"verifications/{survey_id}/{uuid.uuid4().hex}{extensions[content_type]}"
    storage_url = f"{supabase_url}/storage/v1/object/{quote(storage_bucket, safe='')}/{quote(object_path, safe='/')}"
    request = urllib.request.Request(storage_url, data=content, headers={
        "Authorization": f"Bearer {supabase_key}", "apikey": supabase_key,
        "Content-Type": content_type, "x-upsert": "false",
    }, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status not in (200, 201):
                raise HTTPException(status_code=502, detail="Supabase gagal menyimpan foto verifikasi.")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"Supabase verification upload error: HTTP {exc.code}; {body[:500]}", flush=True)
        raise HTTPException(status_code=502, detail=f"Supabase menolak foto verifikasi (HTTP {exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail="Supabase tidak dapat dijangkau saat mengunggah foto verifikasi.") from exc

    with get_db() as connection:
        connection.execute(
            """INSERT INTO verification_media (survey_id, verifier_id, object_path, content_type, original_filename, file_size, storage_bucket)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (survey_id, identity["member_id"], object_path, content_type, os.path.basename(file.filename or "foto-verifikasi"), len(content), storage_bucket),
        )
    return {"message": "Foto verifikasi berhasil diunggah.", "survey_id": survey_id, "file_path": object_path}


@app.post("/dps/surveys/{survey_id}/verification")
def submit_survey_verification(
    survey_id: int,
    payload: VerificationCreate,
    identity: dict = Depends(require_dps),
):
    with get_db() as connection:
        exists = connection.execute("SELECT id FROM surveys WHERE id=%s", (survey_id,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="Data survei tidak ditemukan.")
        ensure_dps_survey_access(connection, identity, survey_id)
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
    if payload.data_match and payload.data_match not in {"Sesuai", "Sebagian sesuai", "Tidak sesuai"}:
        raise HTTPException(status_code=422, detail="Kesesuaian data harus Sesuai, Sebagian sesuai, atau Tidak sesuai.")
    needs_data = [item.dict() for item in payload.needs]
    total_needs_amount = round(sum(item["quantity"] * item["unit_price"] for item in needs_data), 2)
    with get_db() as connection:
        survey = connection.execute("SELECT id FROM surveys WHERE id = %s", (survey_id,)).fetchone()
        if survey is None:
            raise HTTPException(status_code=404, detail="Data survei tidak ditemukan.")
        ensure_dps_survey_access(connection, identity, survey_id)
        existing = connection.execute("SELECT id FROM survey_verifications WHERE survey_id = %s", (survey_id,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Survei ini sudah memiliki hasil verifikasi. Data awal dan hasil verifikasi tidak ditimpa.")
        connection.execute(
            """INSERT INTO survey_verifications (
                survey_id, verifier_id, verifier_name, verifier_region, decision,
                business_condition, actual_business_activity, existing_assets, needs,
                working_capital_details, verified_turnover, verified_profit, verified_worker_count,
                data_match, discrepancy_notes, verification_notes, rejection_reason,
                latitude, longitude, accuracy, total_needs_amount
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )""",
            (
                survey_id, identity["member_id"], identity["member_name"], identity["region"], decision,
                payload.business_condition.strip(), payload.actual_business_activity.strip(), payload.existing_assets.strip(),
                Jsonb(needs_data), payload.working_capital_details.strip(), payload.verified_turnover,
                payload.verified_profit, payload.verified_worker_count, payload.data_match.strip(),
                payload.discrepancy_notes.strip(), payload.verification_notes.strip(), payload.rejection_reason.strip(),
                payload.latitude, payload.longitude, payload.accuracy, total_needs_amount,
            ),
        )
        connection.execute(
            """UPDATE surveys SET business_verified = %s, verification_status = %s, verification_notes = %s WHERE id = %s""",
            ("Ya" if decision == "Sudah diverifikasi" else "Tidak", decision,
             payload.verification_notes.strip() or payload.rejection_reason.strip(), survey_id),
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
):
    if identity["auth_type"] == "officer":
        if identity.get("role") != "DPSS":
            raise HTTPException(status_code=403, detail="Akun DPS mengunggah bukti melalui menu verifikasi.")
        if member_id.strip().upper() != identity["member_id"].upper():
            raise HTTPException(status_code=403, detail="Petugas hanya boleh mengunggah media untuk surveinya sendiri.")
        member_id = identity["member_id"]

    allowed_types = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
    }

    content_type = (file.content_type or "").lower()
    if content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail="Jenis file tidak diizinkan. Gunakan JPEG, PNG, WebP, MP4, atau MOV.",
        )

    allowed_categories = {
        "applicant_ktp": (1, "image"),
        "family_card": (1, "image"),
        "recipient_photo": (1, "image"),
        "business_photo": (5, "image"),
        "business_video": (1, "video"),
    }
    if media_category not in allowed_categories:
        raise HTTPException(status_code=400, detail="Kategori media tidak dikenal.")
    category_limit, category_kind = allowed_categories[media_category]
    is_video = content_type.startswith("video/")
    if (category_kind == "video") != is_video:
        raise HTTPException(status_code=400, detail="Jenis file tidak sesuai kategori media.")

    max_size = 200 * 1024 * 1024
    content = await file.read(max_size + 1)
    if not content:
        raise HTTPException(status_code=400, detail="File kosong.")
    if not _validate_file_signature(content, content_type):
        raise HTTPException(status_code=400, detail="Isi file tidak cocok dengan format berkas yang dikirim.")
    if len(content) > max_size:
        raise HTTPException(status_code=413, detail="Ukuran file maksimal 200 MB.")

    supabase_url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    supabase_key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if not supabase_url or not supabase_key:
        raise HTTPException(
            status_code=503,
            detail="Konfigurasi penyimpanan Supabase belum lengkap.",
        )

    member_id = member_id.strip()
    with get_db() as connection:
        survey = connection.execute(
            """
            SELECT id, member_id, village_id
            FROM surveys
            WHERE id = %s AND member_id = %s
            """,
            (survey_id, member_id),
        ).fetchone()
        category_count = connection.execute(
            "SELECT COUNT(*) AS total FROM survey_media WHERE survey_id = %s AND media_category = %s",
            (survey_id, media_category),
        ).fetchone()["total"]

    if survey is None:
        raise HTTPException(
            status_code=404,
            detail="Data survei tidak ditemukan untuk ID anggota tersebut.",
        )

    if category_count >= category_limit:
        label = {
            "applicant_ktp": "foto KTP",
            "family_card": "foto KK",
            "recipient_photo": "foto penerima bantuan",
            "business_photo": "foto dokumentasi usaha (maksimal 5)",
            "business_video": "video usaha",
        }[media_category]
        raise HTTPException(status_code=400, detail=f"Batas {label} sudah tercapai.")

    # Bucket diselesaikan dari desa di database, bukan dari input bebas aplikasi.
    with get_db() as connection:
        storage_bucket = get_village_bucket(connection, survey["village_id"])
    object_path = f"surveys/{survey_id}/{media_category}/{uuid.uuid4().hex}{allowed_types[content_type]}"
    storage_url = (
        f"{supabase_url}/storage/v1/object/{quote(storage_bucket, safe='')}/"
        f"{quote(object_path, safe='/')}"
    )
    request = urllib.request.Request(
        storage_url,
        data=content,
        headers={
            "Authorization": f"Bearer {supabase_key}",
            "apikey": supabase_key,
            "Content-Type": content_type,
            "x-upsert": "false",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            if response.status not in (200, 201):
                raise HTTPException(
                    status_code=502,
                    detail="Supabase gagal menyimpan file.",
                )
    except urllib.error.HTTPError as exc:
        # Log respons Supabase untuk diagnosis. Secret key tidak dicatat.
        error_body = exc.read().decode("utf-8", errors="replace")
        print(
            f"Supabase upload error: HTTP {exc.code}; "
            f"response: {error_body[:1000]}",
            flush=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Supabase menolak unggahan (HTTP {exc.code}).",
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        print(
            f"Supabase connection error: {type(exc).__name__}: {exc}",
            flush=True,
        )
        raise HTTPException(
            status_code=502,
            detail="Supabase tidak dapat dijangkau saat mengunggah file.",
        ) from exc

    with get_db() as connection:
        connection.execute(
            """
            INSERT INTO survey_media (
                survey_id,
                member_id,
                object_path,
                content_type,
                original_filename,
                file_size,
                media_category, storage_bucket
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                survey_id,
                member_id,
                object_path,
                content_type,
                os.path.basename(file.filename or "file"),
                len(content),
                media_category,
                storage_bucket,
            ),
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
def list_survey_media(
    survey_id: int,
    _: bool = Depends(verify_api_key),
):
    with get_db() as connection:
        rows = connection.execute(
            """
            SELECT id, survey_id, member_id, object_path,
                   content_type, original_filename, file_size, uploaded_at,
                   media_category
            FROM survey_media
            WHERE survey_id = %s
            ORDER BY id
            """,
            (survey_id,),
        ).fetchall()
    return [dict(row) for row in rows]


# =========================================================
# HAPUS SATU SURVEI BESERTA MEDIA TERKAIT
# =========================================================

def delete_survey_record(survey_id: int):
    """Hapus media dari bucket masing-masing, lalu hapus catatan survei."""
    with get_db() as connection:
        survey = connection.execute("SELECT id FROM surveys WHERE id=%s", (survey_id,)).fetchone()
        if survey is None:
            raise HTTPException(status_code=404, detail="Data survei tidak ditemukan.")
        media_rows = connection.execute("SELECT object_path, COALESCE(storage_bucket, 'survey-media') AS storage_bucket FROM survey_media WHERE survey_id=%s", (survey_id,)).fetchall()
        verification_rows = connection.execute("SELECT object_path, COALESCE(storage_bucket, 'survey-media') AS storage_bucket FROM verification_media WHERE survey_id=%s", (survey_id,)).fetchall()
    groups = {}
    for row in list(media_rows) + list(verification_rows):
        groups.setdefault(row["storage_bucket"] or "survey-media", []).append(row["object_path"])
    if groups:
        supabase_url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
        supabase_key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
        if not supabase_url or not supabase_key:
            raise HTTPException(status_code=503, detail="Konfigurasi Supabase belum lengkap. Survei tidak dihapus.")
        for bucket, paths in groups.items():
            url = f"{supabase_url}/storage/v1/object/{quote(bucket, safe='')}"
            request = urllib.request.Request(url, data=json.dumps({"prefixes": paths}).encode("utf-8"), headers={
                "Authorization": f"Bearer {supabase_key}", "apikey": supabase_key, "Content-Type": "application/json"}, method="DELETE")
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    if response.status not in (200, 204):
                        raise HTTPException(status_code=502, detail=f"Supabase tidak mengonfirmasi penghapusan bucket {bucket}.")
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                print(f"Supabase delete error bucket={bucket} HTTP={exc.code}: {body[:300]}", flush=True)
                raise HTTPException(status_code=502, detail=f"Gagal menghapus media dari bucket {bucket}. Survei belum dihapus dari database.") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise HTTPException(status_code=502, detail=f"Supabase tidak dapat dijangkau untuk bucket {bucket}. Survei belum dihapus dari database.") from exc
    with get_db() as connection:
        connection.execute("DELETE FROM survey_media WHERE survey_id=%s", (survey_id,))
        connection.execute("DELETE FROM verification_media WHERE survey_id=%s", (survey_id,))
        result = connection.execute("DELETE FROM surveys WHERE id=%s", (survey_id,))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Data survei tidak ditemukan.")
    return {"message": "Survei dan media terkait berhasil dihapus.", "survey_id": survey_id, "media_deleted": sum(len(v) for v in groups.values())}


@app.delete("/admin/surveys/{survey_id}")
def delete_survey_api(
    survey_id: int,
    _: bool = Depends(verify_api_key),
):
    return delete_survey_record(survey_id)




# =========================================================
# DASHBOARD WEB ADMIN - DILINDUNGI LOGIN
# =========================================================

admin_security = HTTPBasic()


def verify_admin(
    credentials: HTTPBasicCredentials = Depends(admin_security),
):
    admin_username = os.environ.get("ADMIN_USERNAME", "")
    admin_password = os.environ.get("ADMIN_PASSWORD", "")

    if not admin_username or not admin_password:
        raise HTTPException(
            status_code=503,
            detail="Login admin belum dikonfigurasi di server."
        )

    username_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        admin_username.encode("utf-8"),
    )

    password_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        admin_password.encode("utf-8"),
    )

    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=401,
            detail="Username atau kata sandi salah.",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username


def create_supabase_signed_url(object_path: str, expires_in: int = 3600, bucket: str = "survey-media") -> str | None:
    """Buat URL sementara untuk menampilkan file dari bucket privat."""
    supabase_url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    supabase_key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if not supabase_url or not supabase_key:
        print("Dashboard media: konfigurasi Supabase belum lengkap.", flush=True)
        return None

    sign_url = (
        f"{supabase_url}/storage/v1/object/sign/{quote(bucket, safe='')}/"
        f"{quote(object_path, safe='/')}"
    )
    payload = json.dumps({"expiresIn": expires_in}).encode("utf-8")
    request = urllib.request.Request(
        sign_url,
        data=payload,
        headers={
            "Authorization": f"Bearer {supabase_key}",
            "apikey": supabase_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
        signed_path = result.get("signedURL") or result.get("signedUrl")
        if not signed_path:
            print("Dashboard media: respons URL bertanda tangan tidak berisi signedURL.", flush=True)
            return None
        if signed_path.startswith("http://") or signed_path.startswith("https://"):
            return signed_path
        if signed_path.startswith("/"):
            # Supabase biasanya mengembalikan path /object/sign/...
            if signed_path.startswith("/storage/v1/"):
                return f"{supabase_url}{signed_path}"
            return f"{supabase_url}/storage/v1{signed_path}"
        return f"{supabase_url}/storage/v1/{signed_path.lstrip('/')}"
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        status = getattr(exc, "code", "connection")
        print(f"Dashboard media: gagal membuat signed URL ({status}).", flush=True)
        return None


@app.post("/admin/surveys/{survey_id}/delete")
def delete_survey_from_admin(
    survey_id: int,
    username: str = Depends(verify_admin),
):
    try:
        delete_survey_record(survey_id)
    except HTTPException as exc:
        message = escape(str(exc.detail))
        return HTMLResponse(
            content=(
                "<h2>Penghapusan gagal</h2>"
                f"<p>{message}</p>"
                '<p><a href="/admin">Kembali ke dashboard</a></p>'
            ),
            status_code=exc.status_code,
        )
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin/kelola-akun", response_class=HTMLResponse)
def account_management_dashboard():
    return HTMLResponse(r"""<!doctype html>
<html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kelola Akun | Survei GPS</title>
<style>
:root{--maroon:#5b1018;--gold:#d8b15a;--ink:#252525;--muted:#6b7280;--line:#e5e7eb;--paper:#fffaf3;--white:#fff;--red:#b42318;--green:#147d4b}
*{box-sizing:border-box}body{margin:0;background:#f6f3ee;color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,Arial,sans-serif}.wrap{max-width:1120px;margin:auto;padding:24px 16px 48px}.top{background:var(--maroon);color:#fff;padding:22px;border-radius:16px;margin-bottom:18px}.top h1{margin:0 0 4px;font-size:25px}.top p{margin:0;color:#f4e8e8}.panel{background:var(--white);border:1px solid var(--line);border-radius:14px;padding:18px;margin:14px 0}.panel h2{font-size:19px;margin:0 0 14px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.field{display:flex;flex-direction:column;gap:5px;min-width:0}.field.full{grid-column:1/-1}label{font-weight:650;font-size:13px}input,select{font:inherit;width:100%;min-height:44px;border:1px solid #cfd2d7;border-radius:9px;padding:9px 11px;background:#fff;color:var(--ink)}button{font:inherit;font-weight:700;border:0;border-radius:9px;min-height:44px;padding:10px 15px;cursor:pointer;background:var(--maroon);color:#fff}button.secondary{background:#ece7e2;color:#332b2b}button.danger{background:var(--red)}button:disabled{opacity:.55;cursor:wait}.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px}.hint{color:var(--muted);font-size:13px;margin:7px 0 0}.status{padding:11px 13px;border-radius:9px;margin:12px 0;display:none;white-space:pre-wrap}.status.show{display:block}.status.ok{background:#eaf7ef;color:#14532d}.status.err{background:#fff0ef;color:#8a1c13}.toolbar{display:flex;gap:10px;align-items:end;flex-wrap:wrap}.toolbar .field{flex:1;min-width:190px}.tablewrap{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:690px}th,td{text-align:left;padding:11px 9px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:#626262;background:#fafafa}.pill{display:inline-block;padding:3px 8px;border-radius:20px;font-size:12px;font-weight:700;background:#e8f4ec;color:#17603b}.pill.off{background:#fce8e6;color:#9b1c14}.villages{display:flex;gap:8px;flex-wrap:wrap;margin-top:5px}.village{display:flex;align-items:center;gap:7px;border:1px solid var(--line);border-radius:8px;padding:7px 9px;font-size:14px}.village input{width:17px;min-height:17px;margin:0}.smallbtn{min-height:34px;padding:6px 10px;font-size:13px}.keyrow{display:flex;gap:9px;align-items:end;flex-wrap:wrap}.keyrow .field{flex:1;min-width:220px}.subtle{font-size:13px;color:var(--muted)}@media(max-width:640px){.grid{grid-template-columns:1fr}.field.full{grid-column:auto}.wrap{padding:12px}.panel{padding:14px}.top{padding:18px}.top h1{font-size:22px}}
</style></head><body><main class="wrap">
<header class="top"><h1>Kelola Akun Petugas</h1><p>Survei GPS · DPS (Kecamatan) dan DPSS (Desa/Kelurahan)</p></header>
<section class="panel"><h2>1. Sambungkan ke server</h2><div class="keyrow"><div class="field"><label for="apiKey">Kunci akses admin (API Token)</label><input id="apiKey" type="password" autocomplete="off" placeholder="Masukkan kunci akses server"></div><button id="connectBtn">Hubungkan & Muat Data</button></div><p class="hint">Kunci hanya dipakai di halaman ini dan tidak disimpan otomatis.</p><div id="status" class="status" role="status" aria-live="polite"></div></section>
<section class="panel"><h2>2. Buat akun petugas</h2><form id="createForm"><div class="grid">
<div class="field"><label for="newId">ID Petugas</label><input id="newId" required minlength="2" maxlength="50" placeholder="Contoh: DPSS003" autocomplete="off"></div>
<div class="field"><label for="newName">Nama Petugas</label><input id="newName" required minlength="2" maxlength="100" placeholder="Nama lengkap"></div>
<div class="field"><label for="newRole">Peran</label><select id="newRole"><option value="DPSS">DPSS · Desa/Kelurahan</option><option value="DPS">DPS · Kecamatan</option></select></div>
<div class="field"><label for="newRegion">Wilayah</label><input id="newRegion" required maxlength="150" placeholder="Nama desa atau kecamatan"></div>
<div class="field full"><label for="newPassword">Kata sandi awal (minimal 10 karakter)</label><input id="newPassword" type="password" required minlength="10" maxlength="200" autocomplete="new-password" placeholder="Buat kata sandi untuk petugas"></div>
</div><div class="actions"><button type="submit">Buat Akun</button></div></form></section>
<section class="panel"><h2>3. Atur desa tugas</h2><div class="grid"><div class="field"><label for="assignAccount">Pilih akun petugas</label><select id="assignAccount"><option value="">Hubungkan server dulu</option></select></div><div class="field"><label>Pilih desa tugas</label><div id="villageChoices" class="villages"><span class="subtle">Daftar desa akan tampil setelah data dimuat.</span></div></div></div><div class="actions"><button id="assignBtn" type="button">Simpan Penugasan Desa</button></div><p class="hint">Centang satu atau beberapa desa. Menyimpan akan mengganti daftar penugasan akun tersebut.</p></section>
<section class="panel"><div class="toolbar"><div><h2 style="margin-bottom:3px">4. Daftar akun</h2><div class="subtle">Akun yang dihapus tidak akan menghapus riwayat survei yang sudah dikirim.</div></div><div class="field"><label for="filter">Cari akun</label><input id="filter" placeholder="Cari ID, nama, peran, atau wilayah"></div><button id="refreshBtn" class="secondary" type="button">Muat Ulang</button></div><div class="tablewrap" style="margin-top:14px"><table><thead><tr><th>ID / Nama</th><th>Peran</th><th>Wilayah</th><th>Desa tugas</th><th>Status</th><th>Aksi</th></tr></thead><tbody id="accountsBody"><tr><td colspan="6">Hubungkan server untuk memuat akun.</td></tr></tbody></table></div></section>
</main><script>
(function(){const $=id=>document.getElementById(id);let accounts=[],villages=[];
function key(){return $('apiKey').value.trim()}function status(msg,ok=false){const e=$('status');e.textContent=msg;e.className='status show '+(ok?'ok':'err')}
async function api(path,options={}){if(!key())throw Error('Masukkan kunci akses admin terlebih dahulu.');const headers=Object.assign({'X-API-Key':key()},options.headers||{});if(options.body)headers['Content-Type']='application/json';let response;try{response=await fetch(path,Object.assign({},options,{headers}));}catch(e){throw Error('Tidak dapat terhubung ke server. Periksa koneksi internet.')}let data={};try{data=await response.json()}catch(e){}if(!response.ok)throw Error(data.detail||data.message||('Permintaan gagal ('+response.status+').'));return data}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function load(){try{const [a,v]=await Promise.all([api('/admin/akun-petugas'),api('/admin/data-desa')]);accounts=Array.isArray(a)?a:[];villages=Array.isArray(v)?v:[];renderAccounts();renderAssign();status('Terhubung. '+accounts.length+' akun dan '+villages.length+' data desa dimuat.',true)}catch(e){status(e.message)}}
function renderAccounts(){const q=$('filter').value.trim().toLowerCase();const rows=accounts.filter(a=>[a.member_id,a.member_name,a.role,a.region,...(a.villages||[])].join(' ').toLowerCase().includes(q));$('accountsBody').innerHTML=rows.length?rows.map(a=>`<tr><td><strong>${esc(a.member_id)}</strong><br>${esc(a.member_name)}</td><td>${esc(a.role)}</td><td>${esc(a.region)}</td><td>${(a.villages||[]).length?(a.villages||[]).map(esc).join(', '):'<span class="subtle">Belum ditetapkan</span>'}</td><td><span class="pill ${Number(a.active)===1?'':'off'}">${Number(a.active)===1?'Aktif':'Nonaktif'}</span></td><td><button class="danger smallbtn" type="button" data-delete="${esc(a.member_id)}">Hapus Akun</button></td></tr>`).join(''):'<tr><td colspan="6">Tidak ada akun yang cocok.</td></tr>';}
function renderAssign(){const active=accounts.filter(a=>Number(a.active)===1);$('assignAccount').innerHTML='<option value="">Pilih akun DPSS</option>'+active.map(a=>`<option value="${esc(a.member_id)}">${esc(a.member_id)} · ${esc(a.member_name)} (${esc(a.role)})</option>`).join('');$('villageChoices').innerHTML=villages.filter(v=>Number(v.active)===1).map(v=>`<label class="village"><input type="checkbox" value="${esc(v.village_name)}"> <span>${esc(v.village_name)} <span class="subtle">· ${esc(v.district||'')}</span></span></label>`).join('')||'<span class="subtle">Belum ada data desa aktif.</span>';}
$('connectBtn').addEventListener('click',load);$('refreshBtn').addEventListener('click',load);$('filter').addEventListener('input',renderAccounts);
$('createForm').addEventListener('submit',async e=>{e.preventDefault();const btn=e.submitter;btn.disabled=true;try{const payload={member_id:$('newId').value.trim().toUpperCase(),member_name:$('newName').value.trim(),role:$('newRole').value,region:$('newRegion').value.trim(),password:$('newPassword').value};const r=await api('/admin/akun-petugas',{method:'POST',body:JSON.stringify(payload)});$('createForm').reset();status((r.message||'Akun berhasil dibuat.')+' ID: '+payload.member_id,true);await load()}catch(err){status(err.message)}finally{btn.disabled=false}});
$('assignBtn').addEventListener('click',async()=>{const id=$('assignAccount').value;const selected=[...$('villageChoices').querySelectorAll('input:checked')].map(x=>x.value);if(!id){status('Pilih akun petugas terlebih dahulu.');return}if(!selected.length){status('Pilih minimal satu desa tugas.');return}const btn=$('assignBtn');btn.disabled=true;try{const r=await api('/admin/akun-petugas/'+encodeURIComponent(id)+'/penugasan-desa',{method:'POST',body:JSON.stringify({villages:selected})});status((r.message||'Penugasan berhasil disimpan.')+' '+id+': '+selected.join(', '),true);await load();$('assignAccount').value=id;[...$('villageChoices').querySelectorAll('input')].forEach(x=>x.checked=selected.includes(x.value))}catch(e){status(e.message)}finally{btn.disabled=false}});
$('accountsBody').addEventListener('click',async e=>{const btn=e.target.closest('[data-delete]');if(!btn)return;const id=btn.getAttribute('data-delete');if(!confirm('Hapus akun '+id+'? Akun tidak bisa login lagi. Riwayat survei yang sudah dikirim tetap disimpan.'))return;btn.disabled=true;try{const r=await api('/admin/akun-petugas/'+encodeURIComponent(id),{method:'DELETE'});status(r.message||('Akun '+id+' berhasil dihapus.'),true);await load()}catch(err){status(err.message);btn.disabled=false}});
$('assignAccount').addEventListener('change',()=>{const id=$('assignAccount').value;const a=accounts.find(x=>x.member_id===id);const selected=(a&&a.villages)||[];[...$('villageChoices').querySelectorAll('input')].forEach(x=>x.checked=selected.includes(x.value))});
})();</script></body></html>""")


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(
    username: str = Depends(verify_admin),
):
    with get_db() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM surveys
            ORDER BY id DESC
            """
        ).fetchall()
        media_rows = connection.execute(
            """
            SELECT id, survey_id, member_id, object_path, content_type,
                   original_filename, file_size, uploaded_at, media_category, storage_bucket
            FROM survey_media
            ORDER BY id DESC
            """
        ).fetchall()
        verification_rows = connection.execute(
            "SELECT * FROM survey_verifications ORDER BY id DESC"
        ).fetchall()
        verification_media_rows = connection.execute(
            "SELECT * FROM verification_media ORDER BY id DESC"
        ).fetchall()

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
        map_url = escape(f"https://www.google.com/maps?q={lat},{lon}", quote=True)

        media_html = []
        for media in media_by_survey.get(survey_id, []):
            signed_url = create_supabase_signed_url(media["object_path"], bucket=media.get("storage_bucket") or "survey-media")
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
                    media_html.append(
                        f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">{filename}</a>'
                    )
            else:
                media_html.append(
                    f'<span>{filename} (tersimpan, pratinjau gagal dibuat)</span>'
                )
        for media in verification_media_by_survey.get(survey_id, []):
            signed_url = create_supabase_signed_url(media["object_path"], bucket=media.get("storage_bucket") or "survey-media")
            filename = escape(str(media.get("original_filename") or "Foto verifikasi"))
            if signed_url:
                safe_url = escape(signed_url, quote=True)
                media_html.append(
                    f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">'
                    f'<img class="media-thumb" src="{safe_url}" alt="{filename}"></a>'
                    f'<div><strong>Foto Verifikasi DPS</strong><br>'
                    f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">{filename}</a></div>'
                )
            else:
                media_html.append(f'<span>{filename} (pratinjau gagal dibuat)</span>')
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
            ) or "Tidak ada item kebutuhan yang dicatat"

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

# =========================================================
# DASHBOARD PIMPINAN + EKSPOR EXCEL/PDF
# =========================================================

from io import BytesIO
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBasicCredentials


def verify_pimpinan_or_admin(
    credentials: HTTPBasicCredentials = Depends(admin_security),
):
    """Izinkan login admin lama atau akun pimpinan khusus dari environment."""
    allowed_pairs = [
        (os.environ.get("ADMIN_USERNAME", ""), os.environ.get("ADMIN_PASSWORD", "")),
        (os.environ.get("PIMPINAN_USERNAME", ""), os.environ.get("PIMPINAN_PASSWORD", "")),
    ]
    supplied_user = credentials.username.encode("utf-8")
    supplied_password = credentials.password.encode("utf-8")
    for expected_user, expected_password in allowed_pairs:
        if not expected_user or not expected_password:
            continue
        user_ok = secrets.compare_digest(supplied_user, expected_user.encode("utf-8"))
        password_ok = secrets.compare_digest(supplied_password, expected_password.encode("utf-8"))
        if user_ok and password_ok:
            return credentials.username
    raise HTTPException(
        status_code=401,
        detail="Username atau kata sandi admin/pimpinan salah atau belum dikonfigurasi.",
        headers={"WWW-Authenticate": "Basic"},
    )


def _pimpinan_filtered_rows(tanggal_mulai="", tanggal_selesai="", wilayah="", petugas="", status=""):
    clauses = []
    params = []

    if tanggal_mulai.strip():
        clauses.append("NULLIF(s.surveyed_at, '')::timestamptz::date >= %s::date")
        params.append(tanggal_mulai.strip())
    if tanggal_selesai.strip():
        clauses.append("NULLIF(s.surveyed_at, '')::timestamptz::date <= %s::date")
        params.append(tanggal_selesai.strip())
    if wilayah.strip():
        clauses.append("COALESCE(s.village, '') ILIKE %s")
        params.append("%" + wilayah.strip() + "%")
    if petugas.strip():
        clauses.append("(COALESCE(s.member_id, '') ILIKE %s OR COALESCE(o.member_name, '') ILIKE %s)")
        params.extend(["%" + petugas.strip() + "%", "%" + petugas.strip() + "%"])
    if status.strip():
        clauses.append("COALESCE(v.decision, s.verification_status, 'Belum diverifikasi') = %s")
        params.append(status.strip())

    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = """
        SELECT
            s.id AS survey_id, s.member_id, COALESCE(o.member_name, s.member_id) AS officer_name,
            s.surveyed_at, s.received_at, s.village, s.applicant_name, s.applicant_nik,
            s.applicant_phone, s.applicant_address, s.business_name, s.business_type,
            s.business_location, s.capital_amount, s.turnover_amount, s.net_profit_amount,
            s.worker_count, s.assistance_types, s.assistance_amount,
            COALESCE(v.decision, s.verification_status, 'Belum diverifikasi') AS final_status,
            v.verifier_id, v.verifier_name, v.verifier_region, v.verified_at,
            v.business_condition, v.actual_business_activity, v.existing_assets,
            v.data_match, v.discrepancy_notes, v.verification_notes, v.rejection_reason,
            v.total_needs_amount, v.verified_turnover, v.verified_profit, v.verified_worker_count,
            s.latitude, s.longitude
        FROM surveys s
        LEFT JOIN survey_verifications v ON v.survey_id = s.id
        LEFT JOIN officers o ON o.member_id = s.member_id
    """ + where_sql + " ORDER BY s.id DESC"

    with get_db() as connection:
        return [dict(row) for row in connection.execute(sql, tuple(params)).fetchall()]


def _pimpinan_filters(request):
    return {
        "tanggal_mulai": request.query_params.get("tanggal_mulai", "").strip(),
        "tanggal_selesai": request.query_params.get("tanggal_selesai", "").strip(),
        "wilayah": request.query_params.get("wilayah", "").strip(),
        "petugas": request.query_params.get("petugas", "").strip(),
        "status": request.query_params.get("status", "").strip(),
    }


def _pimpinan_qs(filters):
    from urllib.parse import urlencode
    return urlencode({k: v for k, v in filters.items() if v})


def _pimpinan_summary(rows):
    total = len(rows)
    verified = sum(1 for r in rows if r.get("final_status") == "Sudah diverifikasi")
    rejected = sum(1 for r in rows if r.get("final_status") == "Ditolak")
    pending = total - verified - rejected
    return total, verified, pending, rejected


@app.get("/pimpinan", response_class=HTMLResponse)
def pimpinan_dashboard(request: Request, username: str = Depends(verify_pimpinan_or_admin)):
    f = _pimpinan_filters(request)
    rows = _pimpinan_filtered_rows(**f)
    total, verified, pending, rejected = _pimpinan_summary(rows)
    qs = _pimpinan_qs(f)

    def e(value):
        return escape("" if value is None else str(value), quote=True)

    table_rows = []
    for r in rows:
        gps = f"{r.get('latitude') or ''}, {r.get('longitude') or ''}"
        table_rows.append(
            "<tr>"
            f"<td>{e(r.get('survey_id'))}</td>"
            f"<td>{e(r.get('officer_name'))}<br><small>{e(r.get('member_id'))}</small></td>"
            f"<td>{e(r.get('village'))}</td>"
            f"<td><b>{e(r.get('applicant_name'))}</b><br>NIK: {e(r.get('applicant_nik'))}<br>HP: {e(r.get('applicant_phone'))}<br>{e(r.get('applicant_address'))}</td>"
            f"<td><b>{e(r.get('business_name'))}</b><br>{e(r.get('business_type'))}<br>Modal: {e(r.get('capital_amount'))}<br>Omzet awal: {e(r.get('turnover_amount'))}</td>"
            f"<td><b>{e(r.get('final_status'))}</b><br>Verifikator: {e(r.get('verifier_name'))} ({e(r.get('verifier_id'))})<br>Wilayah verifikasi: {e(r.get('verifier_region'))}<br>Waktu: {e(r.get('verified_at'))}</td>"
            f"<td>{e(r.get('business_condition'))}<br>Aktivitas: {e(r.get('actual_business_activity'))}<br>Kesesuaian: {e(r.get('data_match'))}<br>Catatan: {e(r.get('verification_notes'))}<br>Alasan penolakan: {e(r.get('rejection_reason'))}<br>Total kebutuhan: {e(r.get('total_needs_amount'))}</td>"
            f"<td><a target='_blank' rel='noopener' href='https://www.google.com/maps?q={e(gps)}'>Buka peta</a><br>{e(r.get('surveyed_at'))}</td>"
            "</tr>"
        )

    options = "".join(
        f"<option value='{e(v)}' {'selected' if f['status'] == v else ''}>{e(v)}</option>"
        for v in ["Sudah diverifikasi", "Belum diverifikasi", "Perlu ditinjau", "Ditolak"]
    )
    export_qs = ("?" + qs) if qs else ""
        page = f"""<!doctype html><html lang='id'>
<head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Dashboard Pimpinan | Ikatan Persaudaraan Triple 555</title>
<style>
:root{{--wine:#651b2b;--wine2:#43111d;--gold:#c8a34b;--gold-light:#f0d98c;--ink:#24212a;--muted:#77727b;--paper:#f7f5f2;--line:#e8e1dc;--green:#207653;--amber:#9b6813;--red:#a92f3e}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--paper);color:var(--ink);font:14px/1.55 Inter,Segoe UI,Arial,sans-serif}}
a{{color:inherit}}.shell{{display:grid;grid-template-columns:230px minmax(0,1fr);min-height:100vh}}
.sidebar{{background:linear-gradient(180deg,var(--wine2),#32101a);color:#f8f0e7;padding:24px 16px;display:flex;flex-direction:column;gap:26px}}
.brand-mini{{display:flex;align-items:center;gap:11px;padding:4px 7px 22px;border-bottom:1px solid #ffffff24}}
.seal{{width:46px;height:46px;flex:0 0 46px;border:2px solid var(--gold);border-radius:50%;display:grid;place-items:center;color:var(--gold-light);font-weight:900;font-size:15px;letter-spacing:-1px;box-shadow:inset 0 0 0 3px #c8a34b25}}
.brand-mini strong{{display:block;font-size:12px;line-height:1.35;letter-spacing:.7px;color:var(--gold-light)}}.brand-mini small{{color:#e8d9d5;font-size:10px}}
.nav-label{{color:#c8a34b;font-size:10px;font-weight:800;letter-spacing:1.8px;padding:0 10px}}
.nav a{{display:flex;align-items:center;gap:12px;text-decoration:none;padding:11px 12px;margin:4px 0;border-radius:9px;color:#eadfe0;font-weight:600}}
.nav a.active,.nav a:hover{{background:#ffffff12;color:var(--gold-light);border-left:3px solid var(--gold)}}.side-note{{margin-top:auto;color:#c9b7b9;font-size:11px;padding:12px 9px;border-top:1px solid #ffffff24}}
.main{{min-width:0}}.topbar{{background:#fff;border-bottom:1px solid var(--line);padding:13px clamp(16px,3vw,34px);display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}}
.crumb{{font-size:12px;color:var(--muted)}}.user-pill{{border:1px solid var(--line);background:#fffaf5;border-radius:99px;padding:8px 13px;font-weight:700;color:var(--wine)}}
.content{{padding:clamp(16px,3vw,32px);max-width:1800px;margin:auto}}
.hero{{position:relative;overflow:hidden;border-radius:17px;padding:clamp(22px,3vw,34px);color:#fff;background:radial-gradient(ellipse at 90% 0%,#8b3b42 0,transparent 42%),linear-gradient(120deg,var(--wine2),var(--wine));box-shadow:0 12px 30px #43111d18}}
.hero:after{{content:'555';position:absolute;right:30px;top:-30px;font-size:clamp(100px,15vw,190px);font-weight:900;letter-spacing:-12px;color:#ffffff08;pointer-events:none}}
.hero-brand{{display:flex;align-items:center;gap:16px;position:relative;z-index:1}}.hero .seal{{width:64px;height:64px;flex-basis:64px;font-size:19px;border-width:2px}}
.eyebrow{{font-size:10px;text-transform:uppercase;letter-spacing:2.2px;color:var(--gold-light);font-weight:800;margin-bottom:4px}}
.hero h1{{margin:0;color:var(--gold-light);font-size:clamp(19px,2.5vw,29px);line-height:1.25;letter-spacing:.6px}}.hero p{{margin:7px 0 0;color:#f2e5e5;font-size:13px}}.hero-foot{{position:relative;z-index:1;margin-top:20px;padding-top:13px;border-top:1px solid #ffffff25;color:#f1dfe0;font-size:12px}}
.page-title{{display:flex;justify-content:space-between;align-items:end;gap:12px;flex-wrap:wrap;margin:25px 0 15px}}.page-title h2{{font-size:22px;margin:0;color:var(--wine2)}}.page-title p{{margin:3px 0 0;color:var(--muted)}}
.cards{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:16px 0 22px}}.card{{background:#fff;border:1px solid var(--line);border-radius:13px;padding:17px;box-shadow:0 4px 12px #28101905;position:relative;overflow:hidden}}.card:before{{content:'';position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--gold)}}.card-label{{font-size:12px;color:var(--muted);font-weight:700}}.card strong{{display:block;font-size:30px;line-height:1.25;margin-top:8px;color:var(--wine2)}}.card-icon{{float:right;color:var(--gold);font-size:20px}}
.panel{{background:#fff;border:1px solid var(--line);border-radius:14px;padding:clamp(15px,2vw,22px);margin:16px 0;box-shadow:0 5px 16px #28101905}}.panel-head{{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:15px}}.panel h3{{margin:0;font-size:16px;color:var(--wine2)}}.panel-head small{{color:var(--muted)}}
.filters{{display:grid;grid-template-columns:repeat(5,minmax(125px,1fr));gap:12px;align-items:end}}label{{display:block;font-size:12px;font-weight:750;color:#51434a;margin-bottom:6px}}input,select,button,.btn{{width:100%;min-height:42px;padding:9px 11px;border:1px solid #ddd2d0;border-radius:8px;font:inherit;background:#fff;color:var(--ink)}}input:focus,select:focus{{outline:2px solid #c8a34b66;border-color:var(--gold)}}button,.btn{{display:inline-flex;align-items:center;justify-content:center;gap:7px;background:var(--wine);border-color:var(--wine);color:#fff;text-align:center;text-decoration:none;cursor:pointer;font-weight:750;transition:transform .15s,background .15s}}button:hover,.btn:hover{{background:var(--wine2);transform:translateY(-1px)}}.btn.gold{{background:var(--gold);border-color:var(--gold);color:#32101a}}.btn.light{{background:#fffaf1;border-color:#e6d5a9;color:var(--wine)}}.actions{{display:flex;gap:9px;flex-wrap:wrap;margin-top:15px}}.actions .btn,.actions button{{width:auto;min-width:125px}}
.tablewrap{{overflow:auto;border:1px solid var(--line);border-radius:9px}}table{{border-collapse:separate;border-spacing:0;width:100%;min-width:1350px;background:#fff}}th,td{{padding:12px;border-bottom:1px solid #eee8e4;text-align:left;vertical-align:top;max-width:300px;overflow-wrap:anywhere}}th{{background:#f6efe9;color:var(--wine2);font-size:11px;text-transform:uppercase;letter-spacing:.4px;position:sticky;top:0}}tbody tr:hover{{background:#fffaf5}}tbody tr:last-child td{{border-bottom:0}}td{{font-size:12px}}td b{{color:#35252a}}small{{color:var(--muted)}}.status{{display:inline-block;padding:4px 8px;border-radius:99px;background:#f7edcf;color:#81580c;font-size:11px;font-weight:800}}.note{{color:var(--muted);font-size:11px;margin:12px 0 0}}.footer{{text-align:center;color:#8b7d7f;font-size:11px;padding:20px 10px}}
@media(max-width:1100px){{.shell{{grid-template-columns:190px minmax(0,1fr)}}.filters{{grid-template-columns:repeat(3,minmax(0,1fr))}}.cards{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}
@media(max-width:700px){{.shell{{display:block}}.sidebar{{padding:12px;gap:12px}}.brand-mini{{padding:3px 5px 12px}}.nav{{display:flex;overflow:auto;gap:5px}}.nav-label,.side-note{{display:none}}.nav a{{white-space:nowrap;padding:9px 11px;margin:0}}.nav a.active{{border-left:0;border-bottom:2px solid var(--gold)}}.content{{padding:12px}}.hero{{padding:20px}}.hero .seal{{width:50px;height:50px;flex-basis:50px}}.hero-brand{{gap:11px}}.filters{{grid-template-columns:1fr 1fr}}.cards{{gap:9px}}.card{{padding:14px}}.card strong{{font-size:25px}}.topbar{{padding:11px 14px}}}}
@media(max-width:390px){{.filters{{grid-template-columns:1fr}}.hero-brand{{align-items:flex-start}}.hero h1{{font-size:17px}}}}
</style></head>
<body><div class='shell'>
<aside class='sidebar'>
  <div class='brand-mini'><div class='seal'>555</div><div><strong>TRIPLE 555</strong><small>Sistem Informasi Organisasi</small></div></div>
  <div class='nav-label'>MENU UTAMA</div>
  <nav class='nav'><a class='active' href='#ringkasan'>▦ &nbsp; Dashboard</a><a href='#filter'>⌕ &nbsp; Filter Data</a><a href='#data'>▤ &nbsp; Data Survei</a><a href='#laporan'>⇩ &nbsp; Laporan</a></nav>
  <div class='side-note'>Portal internal<br><span style='color:var(--gold-light)'>Ikatan Persaudaraan Triple 555</span></div>
</aside>
<div class='main'>
  <div class='topbar'><div class='crumb'>Portal Organisasi &nbsp;›&nbsp; Dashboard Pimpinan</div><div class='user-pill'>● &nbsp; {e(username)} &nbsp; · &nbsp; Pimpinan/Admin</div></div>
  <main class='content'>
    <header class='hero'><div class='hero-brand'><div class='seal'>555</div><div><div class='eyebrow'>Portal resmi organisasi</div><h1>IKATAN PERSAUDARAAN TRIPLE 555</h1><p>Sistem Informasi Survei dan Verifikasi</p></div></div><div class='hero-foot'>Dashboard Pimpinan &nbsp;•&nbsp; Rekap data survei dan hasil verifikasi</div></header>
    <section id='ringkasan'><div class='page-title'><div><h2>Ringkasan Data</h2><p>Pantau perkembangan survei dan verifikasi dalam satu tempat.</p></div></div>
      <div class='cards'><div class='card'><span class='card-icon'>▤</span><div class='card-label'>TOTAL SURVEI</div><strong>{total:,}</strong></div><div class='card'><span class='card-icon'>✓</span><div class='card-label'>TERVERIFIKASI</div><strong>{verified:,}</strong></div><div class='card'><span class='card-icon'>◷</span><div class='card-label'>MENUNGGU</div><strong>{pending:,}</strong></div><div class='card'><span class='card-icon'>×</span><div class='card-label'>DITOLAK</div><strong>{rejected:,}</strong></div></div>
    </section>
    <section class='panel' id='filter'><div class='panel-head'><div><h3>Filter & Unduh Laporan</h3><small>Pilih kriteria untuk menyaring data yang ditampilkan.</small></div></div>
      <form method='get' action='/pimpinan'><div class='filters'>
      <div><label>TANGGAL MULAI</label><input type='date' name='tanggal_mulai' value='{e(f['tanggal_mulai'])}'></div>
      <div><label>TANGGAL AKHIR</label><input type='date' name='tanggal_selesai' value='{e(f['tanggal_selesai'])}'></div>
      <div><label>DESA / WILAYAH</label><input name='wilayah' value='{e(f['wilayah'])}' placeholder='Cari wilayah'></div>
      <div><label>PETUGAS (ID/NAMA)</label><input name='petugas' value='{e(f['petugas'])}' placeholder='Cari petugas'></div>
      <div><label>STATUS VERIFIKASI</label><select name='status'><option value=''>Semua status</option>{options}</select></div></div>
      <div class='actions'><button type='submit'>⌕ Terapkan Filter</button><a class='btn light' href='/pimpinan'>↺ Reset</a><a class='btn gold' id='laporan' href='/pimpinan/export/excel{export_qs}'>↓ Ekspor Excel</a><a class='btn' target='_blank' href='/pimpinan/export/pdf{export_qs}'>▣ Ekspor PDF</a></div>
      <p class='note'>Ekspor mengikuti filter aktif. PDF dapat dicetak atau disimpan sebagai berkas PDF.</p></form>
    </section>
    <section class='panel' id='data'><div class='panel-head'><div><h3>Data Survei & Hasil Verifikasi</h3><small>Informasi survei, petugas, wilayah, dan catatan verifikasi.</small></div><span class='status'>{len(rows):,} DATA</span></div>
      <div class='tablewrap'><table><thead><tr><th>ID</th><th>Petugas</th><th>Wilayah</th><th>Pemohon</th><th>Usaha</th><th>Hasil Verifikasi</th><th>Rincian Verifikasi</th><th>GPS / Waktu Survei</th></tr></thead><tbody>{''.join(table_rows) if table_rows else '<tr><td colspan="8" style="text-align:center;padding:28px">Tidak ada data yang sesuai dengan filter.</td></tr>'}</tbody></table></div>
      <p class='note'>Data bersifat sensitif. Gunakan dan unduh laporan sesuai kewenangan organisasi.</p>
    </section>
    <footer class='footer'>© 2026 Ikatan Persaudaraan Triple 555 · Sistem Informasi Survei dan Verifikasi</footer>
  </main>
</div></div></body></html>"""


    return HTMLResponse(content=page)


@app.get("/pimpinan/export/excel")
def pimpinan_export_excel(request: Request, username: str = Depends(verify_pimpinan_or_admin)):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    f = _pimpinan_filters(request)
    rows = _pimpinan_filtered_rows(**f)
    headers = [
        ("ID Survei", "survey_id"), ("ID Petugas", "member_id"), ("Nama Petugas", "officer_name"),
        ("Tanggal Survei", "surveyed_at"), ("Wilayah", "village"), ("Nama Pemohon", "applicant_name"),
        ("NIK", "applicant_nik"), ("Telepon", "applicant_phone"), ("Alamat", "applicant_address"),
        ("Nama Usaha", "business_name"), ("Jenis Usaha", "business_type"), ("Lokasi Usaha", "business_location"),
        ("Modal Awal", "capital_amount"), ("Omzet Awal", "turnover_amount"), ("Laba Awal", "net_profit_amount"),
        ("Jumlah Pekerja Awal", "worker_count"), ("Jenis Bantuan", "assistance_types"), ("Nilai Bantuan", "assistance_amount"),
        ("Status Verifikasi", "final_status"), ("ID Verifikator", "verifier_id"), ("Nama Verifikator", "verifier_name"),
        ("Wilayah Verifikator", "verifier_region"), ("Waktu Verifikasi", "verified_at"), ("Kondisi Usaha", "business_condition"),
        ("Aktivitas Usaha Aktual", "actual_business_activity"), ("Aset", "existing_assets"), ("Kesesuaian Data", "data_match"),
        ("Catatan Perbedaan", "discrepancy_notes"), ("Catatan Verifikasi", "verification_notes"), ("Alasan Penolakan", "rejection_reason"),
        ("Total Kebutuhan", "total_needs_amount"), ("Omzet Terverifikasi", "verified_turnover"), ("Laba Terverifikasi", "verified_profit"),
        ("Pekerja Terverifikasi", "verified_worker_count"), ("Latitude", "latitude"), ("Longitude", "longitude"),
    ]
    wb = Workbook()
    ws = wb.active
    ws.title = "Rekap Survei Verifikasi"
    ws.append([h for h, _ in headers])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="5B1018")
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    for row in rows:
        ws.append([row.get(key) for _, key in headers])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for index, (header, _) in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(index)].width = min(max(len(header) + 3, 14), 32)
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=rekap_survei_verifikasi.xlsx"},
    )


@app.get("/pimpinan/export/pdf")
def pimpinan_export_pdf(request: Request, username: str = Depends(verify_pimpinan_or_admin)):
    from xml.sax.saxutils import escape as xml_escape
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

    f = _pimpinan_filters(request)
    rows = _pimpinan_filtered_rows(**f)
    output = BytesIO()
    doc = SimpleDocTemplate(
        output, pagesize=landscape(A4),
        rightMargin=18, leftMargin=18, topMargin=20, bottomMargin=20,
        title="Rekap Survei dan Verifikasi",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="CellSmall", parent=styles["BodyText"], fontName="Helvetica", fontSize=6.5, leading=8, alignment=TA_LEFT, spaceAfter=0))
    styles.add(ParagraphStyle(name="HeaderSmall", parent=styles["BodyText"], fontName="Helvetica-Bold", fontSize=7, leading=8, textColor=colors.white, alignment=TA_LEFT))
    story = [Paragraph("Rekap Data Survei dan Verifikasi", styles["Title"])]
    filter_line = (
        f"Total data: {len(rows)} | Tanggal: {xml_escape(f['tanggal_mulai'] or 'semua')} s.d. "
        f"{xml_escape(f['tanggal_selesai'] or 'semua')} | Wilayah: {xml_escape(f['wilayah'] or 'semua')} | "
        f"Petugas: {xml_escape(f['petugas'] or 'semua')} | Status: {xml_escape(f['status'] or 'semua')}"
    )
    story.extend([Paragraph(filter_line, styles["Normal"]), Spacer(1, 10)])

    headers = ["ID", "Waktu survei", "Petugas", "Wilayah", "Pemohon", "Usaha", "Status / verifikator", "Rincian verifikasi"]
    data = [[Paragraph(h, styles["HeaderSmall"]) for h in headers]]
    def cell(value):
        text = "" if value is None else str(value)
        return Paragraph(xml_escape(text).replace("\n", "<br/>"), styles["CellSmall"])
    for r in rows:
        applicant = "\n".join(filter(None, [
            str(r.get("applicant_name") or ""),
            ("NIK: " + str(r.get("applicant_nik") or "")) if r.get("applicant_nik") else "",
            str(r.get("applicant_phone") or ""), str(r.get("applicant_address") or "")
        ]))
        business = "\n".join(filter(None, [
            str(r.get("business_name") or ""), str(r.get("business_type") or ""),
            ("Modal: " + str(r.get("capital_amount"))) if r.get("capital_amount") is not None else "",
            ("Omzet awal: " + str(r.get("turnover_amount"))) if r.get("turnover_amount") is not None else ""
        ]))
        status_detail = "\n".join(filter(None, [
            str(r.get("final_status") or ""),
            ("Verifikator: " + str(r.get("verifier_name") or "") + " (" + str(r.get("verifier_id") or "") + ")") if r.get("verifier_id") or r.get("verifier_name") else "",
            ("Wilayah: " + str(r.get("verifier_region"))) if r.get("verifier_region") else "",
            ("Waktu: " + str(r.get("verified_at"))) if r.get("verified_at") else ""
        ]))
        verification = "\n".join(filter(None, [
            ("Kondisi: " + str(r.get("business_condition"))) if r.get("business_condition") else "",
            ("Aktivitas: " + str(r.get("actual_business_activity"))) if r.get("actual_business_activity") else "",
            ("Kesesuaian: " + str(r.get("data_match"))) if r.get("data_match") else "",
            ("Catatan: " + str(r.get("verification_notes"))) if r.get("verification_notes") else "",
            ("Alasan penolakan: " + str(r.get("rejection_reason"))) if r.get("rejection_reason") else "",
            ("Total kebutuhan: " + str(r.get("total_needs_amount"))) if r.get("total_needs_amount") is not None else ""
        ]))
        data.append([
            cell(r.get("survey_id")), cell(r.get("surveyed_at")),
            cell(str(r.get("officer_name") or "") + "\n" + str(r.get("member_id") or "")),
            cell(r.get("village")), cell(applicant), cell(business), cell(status_detail), cell(verification)
        ])

    widths = [28, 65, 74, 58, 112, 95, 105, 180]
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#5B1018")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B7BDC6")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F7F9")]),
    ]))
    story.append(table)
    doc.build(story)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=rekap_survei_verifikasi.pdf"},
    )

# =========================================================
# MENJALANKAN SERVER SECARA LOKAL
# =========================================================

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )
