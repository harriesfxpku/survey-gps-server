from datetime import datetime, timezone, timedelta
import os
import json
import secrets
import hashlib
import re
import unicodedata
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
from fastapi.responses import FileResponse
from pathlib import Path

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
    version="3.11.0-member-id-directory",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/logo.png")
def get_logo():
    # Logo utama berada di static/logo.png pada repository.
    base_dir = Path(__file__).parent
    candidates = [base_dir / "static" / "logo.png", base_dir / "logo.png"]
    logo_path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if logo_path is None:
        raise HTTPException(status_code=404, detail="Logo tidak ditemukan di static/logo.png")
    return FileResponse(logo_path, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})

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
# ID ANGGOTA OTOMATIS BERDASARKAN WILAYAH
# =========================================================

def _member_short_code(value: str, aliases: dict[str, str] | None = None) -> str:
    """Bentuk kode wilayah pendek, tanpa spasi atau tanda baca."""
    normalized = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[A-Za-z0-9]+", normalized.upper())
    # Hilangkan awalan administratif agar kode tidak ikut memuat KEC/KAB/DESA.
    words = [word for word in words if word not in {"KEC", "KECAMATAN", "KAB", "KABUPATEN", "DESA", "KEL", "KELURAHAN"}]
    if not words:
        return "WLY"
    key = " ".join(words)
    aliases = aliases or {}
    if key in aliases:
        return aliases[key]
    # Nama dua kata atau lebih memakai inisial; nama satu kata memakai tiga huruf awal.
    if len(words) > 1:
        return "".join(word[0] for word in words)[:4]
    return words[0][:3]


def _member_code_prefix(org_level: str, region: str, village_name: str = "") -> str:
    """Prefix ringkas tanpa pemisah, misalnya DPSTBG001 dan DPSS TBG AST 001 (tanpa spasi pada ID)."""
    region_code = _member_short_code(region, {
        "TAMBANG": "TBG",
    })
    if str(org_level).upper() == "DPSS":
        village_code = _member_short_code(village_name, {
            "AUR SATI": "AST",
            "BALAM JAYA": "BLJ",
        })
        return f"DPSS{region_code}{village_code}"
    return f"DPS{region_code}"


def _allocate_member_code(connection, org_level: str, region: str, village_name: str = "") -> str:
    """Nomor urut tiga digit per wilayah; advisory lock mencegah nomor ganda saat pendaftaran bersamaan."""
    prefix = _member_code_prefix(org_level, region, village_name)
    connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (prefix,))
    existing = connection.execute(
        "SELECT member_code FROM organization_member_registrations WHERE member_code LIKE %s",
        (prefix + "%",),
    ).fetchall()
    used = []
    pattern = re.compile(r"^" + re.escape(prefix) + r"(\d+)$", re.IGNORECASE)
    for row in existing:
        match = pattern.fullmatch(str(row.get("member_code") or "").strip())
        if match:
            used.append(int(match.group(1)))
    number = max(used, default=0) + 1
    candidate = f"{prefix}{number:03d}"
    while connection.execute("""SELECT 1 FROM officers WHERE member_id=%s
            UNION ALL SELECT 1 FROM members WHERE member_id=%s LIMIT 1""", (candidate, candidate)).fetchone():
        number += 1
        candidate = f"{prefix}{number:03d}"
    return candidate


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
        # Profil personel terpisah dari kredensial akun. Dokumen disimpan di bucket privat.
        connection.execute("""
            CREATE TABLE IF NOT EXISTS officer_profiles (
                officer_id BIGINT PRIMARY KEY REFERENCES officers(id) ON DELETE CASCADE,
                nik VARCHAR(30) NOT NULL DEFAULT '',
                phone VARCHAR(30) NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                birth_place VARCHAR(100) NOT NULL DEFAULT '',
                birth_date DATE,
                profile_photo_path TEXT NOT NULL DEFAULT '',
                ktp_path TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # Pendaftaran mandiri anggota organisasi: terpisah dari akun login petugas.
        connection.execute("""
            CREATE TABLE IF NOT EXISTS organization_member_registrations (
                id BIGSERIAL PRIMARY KEY,
                registration_code VARCHAR(40) UNIQUE NOT NULL,
                member_code VARCHAR(80) NOT NULL DEFAULT '',
                full_name VARCHAR(150) NOT NULL,
                nik VARCHAR(30) NOT NULL,
                birth_place VARCHAR(100) NOT NULL DEFAULT '',
                birth_date DATE,
                gender VARCHAR(20) NOT NULL DEFAULT '',
                address TEXT NOT NULL,
                phone VARCHAR(30) NOT NULL,
                email VARCHAR(150) NOT NULL DEFAULT '',
                org_level VARCHAR(10) NOT NULL CHECK (org_level IN ('DPS','DPSS')),
                org_position VARCHAR(150) NOT NULL,
                region VARCHAR(150) NOT NULL DEFAULT '',
                village_id BIGINT REFERENCES villages(id),
                profile_photo_path TEXT NOT NULL DEFAULT '',
                ktp_path TEXT NOT NULL DEFAULT '',
                status VARCHAR(20) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','revision','approved','rejected')),
                admin_note TEXT NOT NULL DEFAULT '',
                linked_member_id VARCHAR(50) NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                reviewed_at TIMESTAMPTZ
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_org_reg_status_created ON organization_member_registrations(status, created_at DESC)")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_org_reg_nik_unique ON organization_member_registrations(nik)")
        connection.execute("ALTER TABLE surveys ADD COLUMN IF NOT EXISTS village_id BIGINT REFERENCES villages(id)")
        connection.execute("ALTER TABLE survey_media ADD COLUMN IF NOT EXISTS storage_bucket VARCHAR(100) NOT NULL DEFAULT 'survey-media'")
        connection.execute("ALTER TABLE verification_media ADD COLUMN IF NOT EXISTS storage_bucket VARCHAR(100) NOT NULL DEFAULT 'survey-media'")
        connection.execute("""INSERT INTO villages (village_name, district, bucket_name) VALUES
            ('Aur Sati', 'Tambang', 'Aur-Sati-Kec-Tambang'),
            ('Balam Jaya', 'Tambang', 'Balam-Jaya-Kec-Tambang')
            ON CONFLICT (village_name)
            DO UPDATE SET district = EXCLUDED.district""")
        connection.execute("ALTER TABLE organization_member_registrations ADD COLUMN IF NOT EXISTS member_code VARCHAR(80) NOT NULL DEFAULT ''")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_org_reg_member_code_unique ON organization_member_registrations(member_code) WHERE member_code <> ''")
        # Backfill ID untuk pendaftaran lama. ID login yang sudah dipakai dipertahankan agar akun aktif tidak berubah.
        old_members = connection.execute("""SELECT r.id,r.member_code,r.linked_member_id,r.org_level,r.region,
            COALESCE(v.village_name,'') AS village_name
            FROM organization_member_registrations r LEFT JOIN villages v ON v.id=r.village_id
            WHERE COALESCE(r.member_code,'')='' ORDER BY r.created_at,r.id""").fetchall()
        for old in old_members:
            old_code = (old.get('linked_member_id') or '').strip().upper()
            if not old_code:
                old_code = _allocate_member_code(connection, old['org_level'], old['region'], old['village_name'])
            connection.execute("UPDATE organization_member_registrations SET member_code=%s WHERE id=%s", (old_code, old['id']))
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
    member_id = payload.member_id.strip()
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
@app.post("/admin/akun-petugas", tags=["Pengelolaan Akun"], summary="Pembuatan akun melalui anggota terverifikasi")
def create_officer(
    payload: OfficerCreate,
    _: bool = Depends(verify_api_key),
):
    raise HTTPException(
        status_code=403,
        detail="Akun aplikasi hanya dapat diberikan kepada anggota yang sudah disetujui melalui Kelola Akses Aplikasi. Pembuatan akun manual dinonaktifkan.",
    )

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
    # Halaman pembuka publik: menggunakan potongan desain Theme 2 yang disetujui.
    # Tombol utama diarahkan ke formulir pendaftaran yang sudah ada.
    hero_image = "data:image/jpeg;base64,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"
    return HTMLResponse(f"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#31090e">
<title>555 | Pendaftaran Anggota</title>
<style>
*{{box-sizing:border-box}}
html,body{{margin:0;min-height:100%;background:#160609}}
body{{font-family:Arial,Helvetica,sans-serif;display:flex;align-items:center;justify-content:center;padding:22px 14px}}
.landing{{position:relative;width:min(100%,1480px);line-height:0;overflow:hidden;border-radius:9px;box-shadow:0 18px 55px rgba(0,0,0,.55);border:1px solid rgba(216,177,90,.35)}}
.landing img{{display:block;width:100%;height:auto}}
.start-link{{position:absolute;left:4.1%;top:76.5%;width:31.8%;height:14.2%;display:block;border-radius:999px;text-decoration:none;outline-offset:4px}}
.start-link:focus-visible{{outline:3px solid #fff}}
@media(max-width:600px){{body{{padding:10px 7px;align-items:flex-start}}.landing{{border-radius:6px}}}}
</style>
</head>
<body>
<main class="landing" aria-label="Halaman pembuka pendaftaran anggota Triple 555">
<img src="{hero_image}" alt="Mari Bergabung Bersama 555. Wujudkan UMKM yang lebih sejahtera. Daftar sekarang.">
<a class="start-link" href="/pendaftaran-anggota" aria-label="Daftar sekarang, buka formulir pendaftaran anggota"></a>
</main>
</body>
</html>""")


@app.get("/api/status")
def api_status():
    # Informasi status server lama tetap tersedia melalui endpoint ini.
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
def account_management_dashboard(credentials: HTTPBasicCredentials = Depends(admin_security)):
    # Halaman ini dapat dibuka admin maupun pimpinan yang kredensialnya dikonfigurasi.
    username = verify_pimpinan_or_admin(credentials)
    return HTMLResponse(r"""<!doctype html>
<html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kelola Akses Aplikasi | Triple 555</title>
<style>
:root{--maroon:#5b1018;--gold:#d8b15a;--ink:#292323;--muted:#6b7280;--line:#e8e1d8;--paper:#f6f3ee;--red:#b42318;--green:#17603b}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 system-ui,Arial,sans-serif}.wrap{max-width:1180px;margin:auto;padding:22px 15px 45px}.hero{background:linear-gradient(120deg,#31090e,var(--maroon));color:#fff;padding:23px;border-radius:16px}.hero h1{margin:0 0 5px}.hero p{margin:0;color:#f5e8e8}.panel{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px;margin:15px 0}.notice{background:#fff8e8;border-left:4px solid var(--gold);padding:12px 14px;border-radius:8px;color:#59451d}.toolbar{display:flex;gap:10px;align-items:end;flex-wrap:wrap}.field{display:flex;flex-direction:column;gap:5px;min-width:220px;flex:1}label{font-weight:650;font-size:13px}input,select{font:inherit;min-height:42px;border:1px solid #d4d0ca;border-radius:8px;padding:8px 10px;width:100%}button,.btn{display:inline-block;border:0;border-radius:8px;padding:10px 14px;background:var(--maroon);color:#fff;text-decoration:none;font:inherit;font-weight:700;cursor:pointer}button.secondary,.btn.secondary{background:#eee8e1;color:#332b2b}button.danger{background:var(--red)}button:disabled{opacity:.55;cursor:wait}.status{display:none;padding:11px 13px;border-radius:8px;margin:12px 0;white-space:pre-wrap}.status.show{display:block}.status.ok{background:#eaf7ef;color:var(--green)}.status.err{background:#fff0ef;color:#8a1c13}.tablewrap{overflow:auto;margin-top:13px}table{border-collapse:collapse;width:100%;min-width:850px}th,td{text-align:left;padding:11px 9px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;text-transform:uppercase;color:#625b55;background:#faf8f5}.pill{display:inline-block;padding:3px 9px;border-radius:20px;font-size:12px;font-weight:700;background:#e8f4ec;color:#17603b}.pill.off{background:#fce8e6;color:#9b1c14}.subtle{color:var(--muted);font-size:13px}.actions{display:flex;gap:7px;flex-wrap:wrap}.empty{text-align:center;padding:25px;color:var(--muted)}dialog{border:0;border-radius:14px;padding:0;width:min(520px,calc(100% - 28px));box-shadow:0 18px 70px #0004}dialog::backdrop{background:#17090bcc}.modalhead{background:var(--maroon);color:#fff;padding:17px 20px}.modalbody{padding:18px 20px}.modalbody .field{margin:12px 0}.modalfoot{display:flex;justify-content:flex-end;gap:8px;padding:0 20px 20px}.links{margin:14px 0;display:flex;gap:8px;flex-wrap:wrap}@media(max-width:650px){.wrap{padding:12px}.panel{padding:13px}.hero{padding:18px}.toolbar .field{min-width:100%}}
</style></head><body><main class="wrap">
<header class="hero"><h1>Kelola Akses Aplikasi</h1><p>Ikatan Persaudaraan Triple 555 · Admin/Pimpinan: __USERNAME__</p></header>
<div class="links"><a class="btn secondary" href="/admin/dashboard">Dashboard Utama Admin</a><a class="btn secondary" href="/admin/pendaftaran-anggota">Verifikasi Pendaftaran Anggota</a></div>
<section class="panel"><div class="notice"><b>Prinsip pengelolaan:</b> daftar anggota diambil dari pendaftaran yang sudah disetujui. Halaman ini tidak menginput ulang identitas anggota. Admin/pimpinan hanya memilih anggota yang ditugaskan, lalu memberikan atau mencabut akses aplikasi.</div>
<div id="status" class="status" role="status" aria-live="polite"></div>
<div class="toolbar" style="margin-top:16px"><div class="field"><label for="filter">Cari anggota</label><input id="filter" placeholder="Nama, NIK, jabatan, wilayah, atau ID login"></div><div class="field" style="max-width:220px"><label for="scope">Filter akses</label><select id="scope"><option value="all">Semua anggota disetujui</option><option value="none">Belum diberi akses</option><option value="active">Akses aktif</option><option value="inactive">Akses nonaktif</option></select></div><button id="refreshBtn" class="secondary" type="button">Muat Ulang</button></div>
<div class="tablewrap"><table><thead><tr><th>Anggota terverifikasi</th><th>Jabatan / Struktur</th><th>Wilayah</th><th>Akses aplikasi</th><th>Aksi</th></tr></thead><tbody id="membersBody"><tr><td colspan="5" class="empty">Memuat data anggota...</td></tr></tbody></table></div>
<p class="subtle">Data identitas bersumber dari pendaftaran anggota yang berstatus Disetujui. Pemberian akses tidak mengubah status keanggotaan. Menonaktifkan akses tidak menghapus data anggota maupun riwayat survei.</p></section>
</main>
<dialog id="grantDialog"><form id="grantForm"><div class="modalhead"><h2 style="margin:0;font-size:19px">Berikan Akses Aplikasi</h2><div id="selectedName" class="subtle" style="color:#f3e6e6"></div></div><div class="modalbody"><input type="hidden" id="registrationId"><div class="field"><label>ID Member otomatis (juga menjadi ID Login)</label><div id="memberCode" style="padding:11px;border:1px solid #d4d0ca;border-radius:8px;background:#f8f4ec;font-weight:800;color:#5b1018">-</div></div><div class="field"><label for="initialPassword">Kata sandi awal yang dibuat admin</label><input id="initialPassword" type="password" required minlength="10" maxlength="200" autocomplete="new-password" placeholder="Minimal 10 karakter"></div><p class="subtle">Peran DPS/DPSS dan wilayah tugas diambil otomatis dari data pendaftaran anggota yang telah disetujui.</p></div><div class="modalfoot"><button type="button" class="secondary" id="cancelGrant">Batal</button><button type="submit">Setujui & Berikan Akses</button></div></form></dialog>
<script>
(function(){const $=id=>document.getElementById(id);let members=[];
function msg(text,ok=false){const e=$('status');e.textContent=text;e.className='status show '+(ok?'ok':'err')}
async function api(path,options={}){let r;try{r=await fetch(path,Object.assign({credentials:'same-origin'},options))}catch(e){throw Error('Koneksi ke server gagal. Periksa internet lalu coba lagi.')}let d={};try{d=await r.json()}catch(e){}if(!r.ok)throw Error(d.detail||d.message||('Permintaan gagal ('+r.status+').'));return d}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function state(m){if(!m.login_member_id)return '<span class="pill off">Belum diberi akses</span>';return Number(m.account_active)===1?'<span class="pill">Akses aktif</span>':'<span class="pill off">Akses nonaktif</span>'}
function render(){const q=$('filter').value.trim().toLowerCase(),scope=$('scope').value;let rows=members.filter(m=>[m.full_name,m.nik,m.phone,m.org_position,m.org_level,m.region,m.village_name,m.member_code,m.login_member_id].join(' ').toLowerCase().includes(q));rows=rows.filter(m=>scope==='all'||(scope==='none'&&!m.login_member_id)||(scope==='active'&&m.login_member_id&&Number(m.account_active)===1)||(scope==='inactive'&&m.login_member_id&&Number(m.account_active)!==1));$('membersBody').innerHTML=rows.length?rows.map(m=>`<tr><td><b>${esc(m.full_name)}</b><br><span class="subtle">NIK: ${esc(m.nik)}</span><br><span class="subtle">HP: ${esc(m.phone)}</span></td><td>${esc(m.org_position)}<br><span class="subtle">${esc(m.org_level)}</span></td><td>${esc(m.region||'-')}${m.village_name?'<br><span class="subtle">Desa: '+esc(m.village_name)+'</span>':''}</td><td>${state(m)}<br><span class="subtle">ID Member: ${esc(m.member_code||'-')}</span>${m.login_member_id?'<br><span class="subtle">Akses dengan ID: '+esc(m.login_member_id)+'</span>':''}</td><td><div class="actions">${!m.login_member_id?`<button type="button" data-grant="${m.id}">Beri Akses</button>`:(Number(m.account_active)===1?`<button type="button" class="danger" data-disable="${m.id}">Cabut Akses</button>`:`<button type="button" data-enable="${m.id}">Aktifkan Akses</button>`)}</div></td></tr>`).join(''):'<tr><td colspan="5" class="empty">Tidak ada anggota yang cocok. Pastikan pendaftaran sudah berstatus Disetujui.</td></tr>'}
async function load(){ $('membersBody').innerHTML='<tr><td colspan="5" class="empty">Memuat data anggota terverifikasi...</td></tr>';try{const d=await api('/admin/kelola-akun/data');members=Array.isArray(d)?d:[];render();msg('Data dimuat: '+members.length+' anggota terverifikasi.',true)}catch(e){msg(e.message);$('membersBody').innerHTML='<tr><td colspan="5" class="empty">Gagal memuat data.</td></tr>'}}
$('refreshBtn').addEventListener('click',load);$('filter').addEventListener('input',render);$('scope').addEventListener('change',render);
$('membersBody').addEventListener('click',async e=>{const g=e.target.closest('[data-grant]'),d=e.target.closest('[data-disable]'),a=e.target.closest('[data-enable]');if(g){const m=members.find(x=>String(x.id)===g.dataset.grant);if(!m)return;$('registrationId').value=m.id;$('selectedName').textContent=m.full_name+' · '+m.org_level+' · '+m.org_position;$('memberCode').textContent=m.member_code||'ID belum tersedia';$('initialPassword').value='';$('grantDialog').showModal();return}if(d){const m=members.find(x=>String(x.id)===d.dataset.disable);if(!m||!confirm('Cabut akses aplikasi untuk '+m.full_name+'? Data anggota dan riwayat survei tetap disimpan.'))return;d.disabled=true;try{const r=await api('/admin/kelola-akun/'+m.id+'/nonaktifkan',{method:'POST'});msg(r.message,true);await load()}catch(err){msg(err.message);d.disabled=false}return}if(a){const m=members.find(x=>String(x.id)===a.dataset.enable);if(!m||!confirm('Aktifkan kembali akses aplikasi untuk '+m.full_name+'?'))return;a.disabled=true;try{const r=await api('/admin/kelola-akun/'+m.id+'/aktifkan',{method:'POST'});msg(r.message,true);await load()}catch(err){msg(err.message);a.disabled=false}}});
$('cancelGrant').addEventListener('click',()=>$('grantDialog').close());$('grantForm').addEventListener('submit',async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;try{const id=$('registrationId').value;const r=await api('/admin/kelola-akun/'+id+'/akses',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:$('initialPassword').value})});$('grantDialog').close();msg(r.message+' ID Member/Login: '+r.member_id,true);await load()}catch(err){msg(err.message)}finally{b.disabled=false}});
load();})();
</script></body></html>""".replace("__USERNAME__", escape(username)))


@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_main_dashboard(username: str = Depends(verify_admin)):
    """Halaman utama khusus admin untuk mengakses seluruh fungsi administrasi."""
    with get_db() as connection:
        stats = connection.execute("""
            SELECT
                (SELECT COUNT(*) FROM organization_member_registrations) AS registrations_total,
                (SELECT COUNT(*) FROM organization_member_registrations WHERE status='pending') AS registrations_pending,
                (SELECT COUNT(*) FROM organization_member_registrations WHERE status='revision') AS registrations_revision,
                (SELECT COUNT(*) FROM organization_member_registrations WHERE status='approved') AS registrations_approved,
                (SELECT COUNT(*) FROM organization_member_registrations WHERE status='rejected') AS registrations_rejected,
                (SELECT COUNT(*) FROM officers) AS accounts_total,
                (SELECT COUNT(*) FROM officers WHERE active=1) AS accounts_active,
                (SELECT COUNT(*) FROM officers WHERE active<>1 OR active IS NULL) AS accounts_inactive,
                (SELECT COUNT(*) FROM surveys) AS surveys_total,
                (SELECT COUNT(*) FROM survey_verifications) AS verifications_total
        """).fetchone()
        recent = connection.execute("""
            SELECT full_name, registration_code, org_level, org_position, region, status, created_at
            FROM organization_member_registrations
            WHERE status IN ('pending','revision')
            ORDER BY created_at DESC
            LIMIT 6
        """).fetchall()

    def stat(key):
        return int((stats or {}).get(key) or 0)

    recent_rows = []
    for row in recent:
        status = str(row.get('status') or '')
        status_label = 'Perlu perbaikan' if status == 'revision' else 'Menunggu verifikasi'
        status_class = 'revision' if status == 'revision' else 'pending'
        recent_rows.append(
            "<tr>"
            f"<td><b>{escape(str(row.get('full_name') or '-'))}</b><br><small>{escape(str(row.get('registration_code') or '-'))}</small></td>"
            f"<td>{escape(str(row.get('org_level') or '-'))} · {escape(str(row.get('org_position') or '-'))}</td>"
            f"<td>{escape(str(row.get('region') or '-'))}</td>"
            f"<td><span class='badge {status_class}'>{status_label}</span></td>"
            "</tr>"
        )
    recent_html = ''.join(recent_rows) or "<tr><td colspan='4' class='empty'>Tidak ada pendaftaran yang menunggu tindakan.</td></tr>"

    return HTMLResponse(f"""<!doctype html>
<html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Dashboard Utama Admin | Triple 555</title>
<style>
:root{{--maroon:#5b1018;--deep:#31090e;--gold:#d8b15a;--paper:#f5f1eb;--ink:#292323;--muted:#756d68;--line:#e8e0d7;--green:#17603b;--red:#9b2226}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 system-ui,Arial,sans-serif}}
.wrap{{max-width:1240px;margin:auto;padding:22px 16px 48px}}
.hero{{background:linear-gradient(120deg,var(--deep),var(--maroon));color:white;padding:25px;border-radius:17px;border-bottom:5px solid var(--gold);display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap}}
.brand{{display:flex;align-items:center;gap:15px}}.brand img{{width:68px;height:68px;object-fit:contain;border-radius:50%;background:#fff;padding:4px}}h1{{margin:0 0 4px;font-size:clamp(23px,4vw,32px)}}.hero p{{margin:0;color:#f0e5e1}}.user{{background:#ffffff18;border:1px solid #ffffff35;padding:9px 13px;border-radius:10px;font-size:13px}}
.section-title{{margin:25px 0 12px;font-size:19px;color:var(--maroon)}}.stats{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px}}.stat,.card,.panel{{background:white;border:1px solid var(--line);border-radius:14px;box-shadow:0 3px 12px #32100b08}}
.stat{{padding:16px}}.stat span{{display:block;color:var(--muted);font-size:12px;font-weight:650}}.stat strong{{display:block;font-size:30px;color:var(--maroon);margin-top:5px;line-height:1.2}}.stat small{{color:var(--muted)}}
.menu{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}.card{{padding:19px;display:flex;gap:14px;align-items:flex-start}}.icon{{width:45px;height:45px;flex:0 0 45px;border-radius:12px;background:#f6eddf;display:grid;place-items:center;font-size:23px}}.card h3{{margin:0 0 5px;color:var(--maroon);font-size:17px}}.card p{{margin:0 0 13px;color:var(--muted);font-size:13px}}.btn{{display:inline-block;background:var(--maroon);color:#fff;text-decoration:none;padding:9px 13px;border-radius:8px;font-weight:700;font-size:13px}}.btn:hover{{background:var(--deep)}}
.panel{{padding:17px;margin-top:15px;overflow:hidden}}.panel h2{{font-size:18px;color:var(--maroon);margin:0 0 12px}}.tablewrap{{overflow:auto}}table{{width:100%;border-collapse:collapse;min-width:650px}}th,td{{text-align:left;padding:11px 9px;border-bottom:1px solid var(--line);vertical-align:top}}th{{font-size:12px;text-transform:uppercase;color:#6b625b;background:#faf8f5}}small{{color:var(--muted)}}.badge{{display:inline-block;border-radius:20px;padding:4px 9px;font-size:11px;font-weight:750}}.pending{{background:#fff2d6;color:#81570c}}.revision{{background:#fce8e6;color:#9b2226}}.empty{{text-align:center;color:var(--muted);padding:22px}}.foot{{margin-top:22px;text-align:center;color:var(--muted);font-size:12px}}
@media(max-width:950px){{.stats{{grid-template-columns:repeat(3,minmax(0,1fr))}}}}@media(max-width:620px){{.wrap{{padding:12px}}.hero{{padding:18px}}.brand img{{width:54px;height:54px}}.stats{{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}}.stat{{padding:12px}}.stat strong{{font-size:25px}}.menu{{grid-template-columns:1fr}}.card{{padding:15px}}}}
</style></head><body><main class='wrap'>
<header class='hero'><div class='brand'><img src='/logo.png' alt='Logo 555'><div><h1>Dashboard Utama Admin</h1><p>Pusat pengelolaan administrasi sistem organisasi Triple 555</p></div></div><div class='user'>Login sebagai Admin: <b>{escape(username)}</b></div></header>
<h2 class='section-title'>Ringkasan Sistem</h2>
<section class='stats'>
<div class='stat'><span>Total Pendaftaran Anggota</span><strong>{stat('registrations_total')}</strong><small>Seluruh status</small></div>
<div class='stat'><span>Menunggu Verifikasi</span><strong>{stat('registrations_pending')}</strong><small>Perlu diperiksa admin</small></div>
<div class='stat'><span>Perlu Perbaikan</span><strong>{stat('registrations_revision')}</strong><small>Menunggu pembaruan data</small></div>
<div class='stat'><span>Anggota Disetujui</span><strong>{stat('registrations_approved')}</strong><small>Masuk daftar anggota terverifikasi</small></div>
<div class='stat'><span>Pendaftaran Ditolak</span><strong>{stat('registrations_rejected')}</strong><small>Sudah diputuskan</small></div>
<div class='stat'><span>Total Akun Aplikasi</span><strong>{stat('accounts_total')}</strong><small>Petugas yang pernah diberi akses</small></div>
<div class='stat'><span>Akun Aktif</span><strong>{stat('accounts_active')}</strong><small>Dapat menggunakan aplikasi</small></div>
<div class='stat'><span>Akun Nonaktif</span><strong>{stat('accounts_inactive')}</strong><small>Akses sedang dicabut</small></div>
<div class='stat'><span>Total Survei</span><strong>{stat('surveys_total')}</strong><small>Data survei tersimpan</small></div>
<div class='stat'><span>Hasil Verifikasi DPS</span><strong>{stat('verifications_total')}</strong><small>Verifikasi tersimpan</small></div>
</section>
<h2 class='section-title'>Menu Administrasi</h2>
<section class='menu'>
<article class='card'><div class='icon'>📝</div><div><h3>Verifikasi Pendaftaran Anggota</h3><p>Periksa pendaftaran, edit data yang keliru, setujui, minta perbaikan, atau tolak pendaftaran.</p><a class='btn' href='/admin/pendaftaran-anggota'>Buka Verifikasi</a></div></article>
<article class='card'><div class='icon'>🗂️</div><div><h3>Daftar Seluruh Anggota</h3><p>Lihat seluruh anggota DPS dan DPSS yang sudah disetujui, termasuk foto profil dan dokumen identitas.</p><a class='btn' href='/admin/data-anggota'>Buka Data Anggota</a></div></article>
<article class='card'><div class='icon'>👥</div><div><h3>Data Personel Petugas</h3><p>Lihat dan kelola data profil petugas yang sudah memiliki akun aplikasi.</p><a class='btn' href='/admin/data-personel'>Buka Data Personel</a></div></article>
<article class='card'><div class='icon'>🔐</div><div><h3>Kelola Akses Aplikasi</h3><p>Pilih anggota terverifikasi yang ditugaskan, berikan akses, aktifkan kembali, atau cabut akses aplikasi.</p><a class='btn' href='/admin/kelola-akun'>Kelola Akses</a></div></article>
<article class='card'><div class='icon'>📍</div><div><h3>Data Survei dan Media</h3><p>Pantau data survei, lokasi GPS, dokumen, foto, video, dan hasil verifikasi yang tersimpan.</p><a class='btn' href='/admin'>Buka Data Survei</a></div></article>
</section>
<section class='panel'><h2>Pendaftaran yang Memerlukan Tindakan</h2><div class='tablewrap'><table><thead><tr><th>Nama / Kode</th><th>Struktur / Jabatan</th><th>Wilayah</th><th>Status</th></tr></thead><tbody>{recent_html}</tbody></table></div><p style='margin:14px 0 0'><a class='btn' href='/admin/pendaftaran-anggota?status=pending'>Periksa Pendaftaran</a></p></section>
<div class='foot'>Dashboard Administrasi Triple 555 · Data ditampilkan langsung dari server</div>
</main></body></html>""")


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
        <div class="info"><a href="/admin/dashboard">Dashboard Utama Admin</a> &nbsp; | &nbsp; <a href="/admin/pendaftaran-anggota">Verifikasi Pendaftaran Anggota</a> &nbsp; | &nbsp; <a href="/admin/kelola-akun">Kelola Akses Aplikasi</a></div>
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
# DASHBOARD KHUSUS PIMPINAN: REKAP TERPISAH SURVEI DPSS DAN VERIFIKASI DPS
# =========================================================
from io import BytesIO
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBasicCredentials
from urllib.parse import urlencode


def verify_pimpinan_or_admin(credentials: HTTPBasicCredentials = Depends(admin_security)):
    """Akses dashboard untuk admin lama atau akun pimpinan dari environment."""
    allowed_pairs = [
        (os.environ.get("ADMIN_USERNAME", ""), os.environ.get("ADMIN_PASSWORD", "")),
        (os.environ.get("PIMPINAN_USERNAME", ""), os.environ.get("PIMPINAN_PASSWORD", "")),
    ]
    supplied_user = credentials.username.encode("utf-8")
    supplied_password = credentials.password.encode("utf-8")
    for expected_user, expected_password in allowed_pairs:
        if expected_user and expected_password and secrets.compare_digest(supplied_user, expected_user.encode("utf-8")) and secrets.compare_digest(supplied_password, expected_password.encode("utf-8")):
            return credentials.username
    raise HTTPException(status_code=401, detail="Username atau kata sandi admin/pimpinan salah atau belum dikonfigurasi.", headers={"WWW-Authenticate": "Basic"})



class OfficerAccessGrant(BaseModel):
    password: str = Field(min_length=10, max_length=200)


@app.get("/admin/kelola-akun/data")
def list_verified_members_for_app_access(username: str = Depends(verify_pimpinan_or_admin)):
    """Daftar anggota yang sudah disetujui; identitas tetap bersumber dari pendaftaran."""
    with get_db() as connection:
        rows = connection.execute("""
            SELECT r.id, r.full_name, r.nik, r.phone, r.org_level, r.org_position,
                   r.region, r.member_code, r.linked_member_id AS login_member_id,
                   v.village_name, o.active AS account_active
            FROM organization_member_registrations r
            LEFT JOIN villages v ON v.id = r.village_id
            LEFT JOIN officers o ON o.member_id = r.linked_member_id
            WHERE r.status = 'approved'
            ORDER BY r.org_level, r.region, r.full_name
        """).fetchall()
    return [dict(row) for row in rows]


@app.post("/admin/kelola-akun/{registration_id}/akses")
def grant_app_access_to_verified_member(
    registration_id: int, payload: OfficerAccessGrant,
    username: str = Depends(verify_pimpinan_or_admin),
):
    """Buat/aktifkan kredensial aplikasi hanya untuk anggota yang sudah disetujui."""
    with get_db() as connection:
        registration = connection.execute(
            "SELECT * FROM organization_member_registrations WHERE id=%s AND status='approved'",
            (registration_id,),
        ).fetchone()
        if not registration:
            raise HTTPException(status_code=404, detail="Anggota terverifikasi tidak ditemukan. Pastikan status pendaftarannya Disetujui.")
        if registration["org_level"] not in {"DPS", "DPSS"}:
            raise HTTPException(status_code=422, detail="Tingkat kepengurusan anggota tidak valid untuk akses aplikasi survei.")
        if registration["org_level"] == "DPSS" and not registration["village_id"]:
            raise HTTPException(status_code=422, detail="Data anggota DPSS belum memiliki desa penugasan. Perbaiki data pendaftarannya terlebih dahulu.")
        member_id = (registration.get("linked_member_id") or registration.get("member_code") or "").strip().upper()
        if not member_id:
            raise HTTPException(status_code=409, detail="ID anggota belum terbentuk. Periksa data wilayah pendaftaran.")
        linked_id = (registration.get("linked_member_id") or "").strip().upper()
        if linked_id and linked_id != member_id:
            raise HTTPException(status_code=409, detail=f"Anggota ini sudah terhubung dengan ID login {linked_id}. Gunakan ID tersebut atau periksa tautan data.")
        other_link = connection.execute(
            "SELECT id FROM organization_member_registrations WHERE linked_member_id=%s AND id<>%s",
            (member_id, registration_id),
        ).fetchone()
        if other_link:
            raise HTTPException(status_code=409, detail="ID login ini sudah ditautkan ke pendaftaran anggota lain.")
        officer = connection.execute("SELECT id FROM officers WHERE member_id=%s", (member_id,)).fetchone()
        if officer and not linked_id:
            raise HTTPException(status_code=409, detail="ID login sudah digunakan oleh akun lain. Pilih ID login yang berbeda.")
        salt = secrets.token_hex(16)
        hashed = _password_hash(payload.password, salt)
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "INSERT INTO members (member_id,member_name,active,created_at) VALUES (%s,%s,1,%s) "
            "ON CONFLICT (member_id) DO UPDATE SET member_name=EXCLUDED.member_name,active=1",
            (member_id, registration["full_name"], now),
        )
        if officer:
            connection.execute("""UPDATE officers SET member_name=%s,password_salt=%s,password_hash=%s,
                role=%s,region=%s,active=1,updated_at=NOW() WHERE id=%s""",
                (registration["full_name"], salt, hashed, registration["org_level"], registration["region"], officer["id"]))
            officer_id = officer["id"]
        else:
            officer_row = connection.execute("""INSERT INTO officers
                (member_id,member_name,password_salt,password_hash,role,region,active)
                VALUES (%s,%s,%s,%s,%s,%s,1) RETURNING id""",
                (member_id, registration["full_name"], salt, hashed, registration["org_level"], registration["region"])).fetchone()
            officer_id = officer_row["id"]
        connection.execute("""INSERT INTO officer_profiles
            (officer_id,nik,phone,address,birth_place,birth_date,profile_photo_path,ktp_path,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (officer_id) DO UPDATE SET nik=EXCLUDED.nik,phone=EXCLUDED.phone,
            address=EXCLUDED.address,birth_place=EXCLUDED.birth_place,birth_date=EXCLUDED.birth_date,
            profile_photo_path=CASE WHEN EXCLUDED.profile_photo_path='' THEN officer_profiles.profile_photo_path ELSE EXCLUDED.profile_photo_path END,
            ktp_path=CASE WHEN EXCLUDED.ktp_path='' THEN officer_profiles.ktp_path ELSE EXCLUDED.ktp_path END,
            updated_at=NOW()""",
            (officer_id, registration["nik"], registration["phone"], registration["address"],
             registration["birth_place"], registration["birth_date"], registration["profile_photo_path"], registration["ktp_path"]))
        connection.execute("UPDATE organization_member_registrations SET linked_member_id=%s,updated_at=NOW() WHERE id=%s", (member_id, registration_id))
        # Penugasan desa berasal dari data anggota terverifikasi. DPS tanpa desa khusus
        # mengikuti cakupan kecamatan melalui filter wilayah yang sudah ada.
        connection.execute("DELETE FROM officer_villages WHERE officer_id=%s", (officer_id,))
        if registration["village_id"]:
            connection.execute("INSERT INTO officer_villages (officer_id,village_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (officer_id, registration["village_id"]))
        connection.execute("DELETE FROM officer_sessions WHERE officer_id=%s", (officer_id,))
    return {"message": "Akses aplikasi berhasil diberikan berdasarkan data anggota terverifikasi.", "member_id": member_id, "role": registration["org_level"], "region": registration["region"]}


@app.post("/admin/kelola-akun/{registration_id}/nonaktifkan")
def revoke_app_access_from_verified_member(registration_id: int, username: str = Depends(verify_pimpinan_or_admin)):
    with get_db() as connection:
        row = connection.execute("""SELECT r.linked_member_id,o.id AS officer_id
            FROM organization_member_registrations r
            LEFT JOIN officers o ON o.member_id=r.linked_member_id
            WHERE r.id=%s AND r.status='approved'""", (registration_id,)).fetchone()
        if not row or not row["linked_member_id"] or not row["officer_id"]:
            raise HTTPException(status_code=404, detail="Akun aplikasi anggota ini belum tersedia.")
        connection.execute("UPDATE officers SET active=0,updated_at=NOW() WHERE id=%s", (row["officer_id"],))
        connection.execute("UPDATE members SET active=0 WHERE member_id=%s", (row["linked_member_id"],))
        connection.execute("DELETE FROM officer_sessions WHERE officer_id=%s", (row["officer_id"],))
    return {"message": "Akses aplikasi berhasil dicabut. Data anggota dan riwayat survei tetap disimpan."}


@app.post("/admin/kelola-akun/{registration_id}/aktifkan")
def reactivate_app_access_for_verified_member(registration_id: int, username: str = Depends(verify_pimpinan_or_admin)):
    with get_db() as connection:
        row = connection.execute("""SELECT r.linked_member_id,o.id AS officer_id
            FROM organization_member_registrations r
            LEFT JOIN officers o ON o.member_id=r.linked_member_id
            WHERE r.id=%s AND r.status='approved'""", (registration_id,)).fetchone()
        if not row or not row["linked_member_id"] or not row["officer_id"]:
            raise HTTPException(status_code=404, detail="Akun aplikasi anggota ini belum tersedia.")
        connection.execute("UPDATE officers SET active=1,updated_at=NOW() WHERE id=%s", (row["officer_id"],))
        connection.execute("UPDATE members SET active=1 WHERE member_id=%s", (row["linked_member_id"],))
    return {"message": "Akses aplikasi berhasil diaktifkan kembali."}

def _lead_filters(request):
    return {k: request.query_params.get(k, "").strip() for k in ("tanggal_mulai", "tanggal_selesai", "wilayah", "petugas")}


def _lead_where(filters, alias="s"):
    clauses, params = [], []
    if filters.get("tanggal_mulai"):
        clauses.append(f"NULLIF({alias}.surveyed_at, '')::timestamptz::date >= %s::date")
        params.append(filters["tanggal_mulai"])
    if filters.get("tanggal_selesai"):
        clauses.append(f"NULLIF({alias}.surveyed_at, '')::timestamptz::date <= %s::date")
        params.append(filters["tanggal_selesai"])
    if filters.get("wilayah"):
        clauses.append(f"COALESCE({alias}.village, '') ILIKE %s")
        params.append("%" + filters["wilayah"] + "%")
    if filters.get("petugas"):
        clauses.append(f"(COALESCE({alias}.member_id, '') ILIKE %s OR COALESCE(o.member_name, '') ILIKE %s)")
        params.extend(["%" + filters["petugas"] + "%", "%" + filters["petugas"] + "%"])
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), tuple(params)


def _lead_survey_rows(filters):
    where_sql, params = _lead_where(filters, "s")
    sql = """SELECT s.id AS survey_id, s.member_id, COALESCE(o.member_name,s.member_id) AS officer_name,
        s.surveyed_at, s.received_at, s.village, s.applicant_name, s.applicant_phone, s.applicant_address,
        s.business_name, s.business_type, s.business_location, s.capital_amount, s.turnover_amount,
        s.net_profit_amount, s.worker_count, s.assistance_types, s.assistance_amount, s.notes,
        s.latitude, s.longitude, s.accuracy
        FROM surveys s LEFT JOIN officers o ON o.member_id=s.member_id""" + where_sql + " ORDER BY s.id DESC"
    with get_db() as connection:
        return [dict(r) for r in connection.execute(sql, params).fetchall()]


def _lead_verification_rows(filters):
    where_sql, params = _lead_where(filters, "s")
    sql = """SELECT s.id AS survey_id, s.member_id, COALESCE(o.member_name,s.member_id) AS officer_name,
        s.surveyed_at, s.village, s.applicant_name, s.business_name, s.business_type,
        v.decision, v.verifier_id, v.verifier_name, v.verifier_region, v.verified_at,
        v.business_condition, v.actual_business_activity, v.existing_assets, v.needs,
        v.working_capital_details, v.verified_turnover, v.verified_profit, v.verified_worker_count,
        v.data_match, v.discrepancy_notes, v.verification_notes, v.rejection_reason,
        v.latitude AS verification_latitude, v.longitude AS verification_longitude, v.accuracy AS verification_accuracy,
        v.total_needs_amount
        FROM surveys s LEFT JOIN officers o ON o.member_id=s.member_id
        JOIN survey_verifications v ON v.survey_id=s.id""" + where_sql + " ORDER BY v.verified_at DESC NULLS LAST, s.id DESC"
    with get_db() as connection:
        return [dict(r) for r in connection.execute(sql, params).fetchall()]


def _lead_media(survey_ids, verification=False, include_identity=False):
    if not survey_ids:
        return {}
    table = "verification_media" if verification else "survey_media"
    with get_db() as connection:
        rows = connection.execute(
            f"SELECT survey_id, object_path, content_type, original_filename, file_size, storage_bucket" +
            (", NULL AS media_category" if verification else ", COALESCE(media_category, 'business_photo') AS media_category") +
            f" FROM {table} WHERE survey_id = ANY(%s) ORDER BY id", (survey_ids,)
        ).fetchall()
    grouped = {}
    for item in rows:
        item = dict(item)
        category = str(item.get("media_category") or "business_photo").lower()
        # Dokumen identitas hanya boleh muncul pada halaman dashboard survei.
        # Tetap dikecualikan dari laporan verifikasi dan ekspor.
        is_identity = any(word in category for word in ("ktp", "kk", "identity", "face", "selfie", "document", "dokumen"))
        if is_identity and (verification or not include_identity):
            continue
        url = create_supabase_signed_url(item["object_path"], bucket=item.get("storage_bucket") or "survey-media")
        item["signed_url"] = url
        grouped.setdefault(item["survey_id"], []).append(item)
    return grouped


def _lead_e(value):
    return escape("" if value is None else str(value), quote=True)


def _lead_qs(filters):
    return urlencode({k: v for k, v in filters.items() if v})


def _lead_page(title, username, content):
    return f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{_lead_e(title)} | Ikatan Persaudaraan Triple 555</title><style>
:root{{--maroon:#5b1018;--deep:#31090e;--gold:#d8b15a;--paper:#f6f3ee;--ink:#262626;--line:#e5e0d8;--muted:#6b7280}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:14px/1.5 system-ui,Arial,sans-serif}}header{{background:linear-gradient(120deg,var(--deep),var(--maroon));color:white;padding:24px max(16px,calc((100vw - 1400px)/2));border-bottom:4px solid var(--gold)}}.brand{{display:flex;align-items:center;gap:14px}}.brand img{{width:64px;height:64px;object-fit:contain;border-radius:50%;background:#fff;padding:3px}}header h1{{margin:0;font-size:clamp(22px,3vw,32px)}}header p{{margin:4px 0 0;color:#f3e5d0}}.user{{margin-top:10px;color:#f3e5d0;font-size:13px}}main{{max-width:1400px;margin:auto;padding:20px 16px 40px}}.nav{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}}a.btn,button{{display:inline-block;border:0;border-radius:9px;background:var(--maroon);color:white;padding:10px 14px;font:inherit;font-weight:700;text-decoration:none;cursor:pointer}}a.gold{{background:var(--gold);color:#2b1b08}}a.light{{background:#fff;color:var(--maroon);border:1px solid var(--line)}}.panel,.metric{{background:white;border:1px solid var(--line);border-radius:13px;padding:16px;margin:12px 0;box-shadow:0 3px 12px #2b1b0808}}.metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}}.metric strong{{display:block;font-size:26px;color:var(--maroon)}}.metric span{{color:var(--muted)}}.filters{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}}label{{display:block;font-size:12px;font-weight:700;margin-bottom:5px}}input{{width:100%;min-height:40px;padding:8px;border:1px solid #d7d2ca;border-radius:8px;font:inherit}}.actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}}.tablewrap{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;min-width:850px}}th,td{{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{background:#f8f4ec;color:#5b1018;font-size:12px;white-space:nowrap}}td small{{color:var(--muted)}}.media{{display:flex;gap:8px;flex-wrap:wrap;max-width:300px}}.media img{{width:100px;height:78px;object-fit:cover;border-radius:7px;border:1px solid var(--line)}}.media video{{width:180px;max-width:100%;border-radius:7px}}.muted{{color:var(--muted)}}.cards{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}.report-card{{background:white;border:1px solid var(--line);border-top:4px solid var(--gold);border-radius:14px;padding:20px}}.report-card h2{{margin:0 0 8px;color:var(--maroon)}}

/* Galeri dokumentasi Dashboard Pimpinan */
.lead-media-gallery{{display:grid;gap:18px;min-width:0}}
.lead-media-section{{min-width:0}}
.lead-media-heading{{font-size:14px;color:var(--maroon);margin:0 0 10px;padding-bottom:7px;border-bottom:1px solid var(--line)}}
.lead-identity-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:12px}}
.lead-photo-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}}
.lead-video-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:12px}}
.lead-identity-item,.lead-photo-item,.lead-video-item{{min-width:0;background:#fff;border:1px solid var(--line);border-radius:11px;overflow:hidden;padding:10px}}
.lead-media-label{{font-weight:700;font-size:12px;color:var(--maroon);margin-bottom:8px}}
.lead-identity-item a,.lead-photo-item a{{display:flex;align-items:center;justify-content:center;background:#f8f5ef;border-radius:7px;overflow:hidden}}
.lead-identity-image{{display:block;width:100%;height:auto;max-height:340px;object-fit:contain}}
.lead-photo-image{{display:block;width:100%;height:190px;object-fit:cover;transition:transform .2s ease}}
.lead-photo-item a:hover .lead-photo-image{{transform:scale(1.03)}}
.lead-video-item video{{display:block;width:100%;max-height:320px;background:#1c1c1c;border-radius:7px}}
.lead-media-name{{font-size:11px;color:var(--muted);overflow-wrap:anywhere;margin-top:7px}}
.lead-media-empty,.lead-media-unavailable{{padding:12px;background:#faf8f4;border:1px dashed var(--line);border-radius:8px;color:var(--muted)}}
.lead-media-unavailable p{{margin:5px 0 0;overflow-wrap:anywhere}}
.lead-media-unavailable-list{{display:grid;gap:8px}}
.lead-media-gallery img{{cursor:zoom-in}}
@media(max-width:700px){{.lead-photo-grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}
@media(max-width:420px){{.lead-photo-grid{{grid-template-columns:1fr}}.lead-photo-image{{height:auto;max-height:280px;object-fit:contain}}}}

 .people-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:12px}}.person-card{{display:flex;gap:14px;align-items:center;background:#fff;border:1px solid var(--line);border-left:4px solid var(--gold);border-radius:13px;padding:14px;box-shadow:0 3px 12px #2b1b0808}}.person-photo{{width:82px;height:82px;flex:0 0 82px;border-radius:12px;object-fit:cover;background:#f2eee7}}.no-photo{{display:flex;align-items:center;justify-content:center;text-align:center;color:var(--muted);font-size:11px}}.person-info h3{{margin:0 0 3px;color:var(--maroon)}}.person-info p{{margin:6px 0}}.status{{display:inline-block;padding:4px 9px;border-radius:20px;font-size:11px;font-weight:800}}.status.active{{background:#e3f4e8;color:#176534}}.status.inactive{{background:#fff0df;color:#8a4b08}}footer{{text-align:center;color:var(--muted);font-size:12px;padding:20px}}@media(max-width:800px){{.metrics{{grid-template-columns:repeat(2,minmax(0,1fr))}}.filters{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}@media(max-width:520px){{.cards,.filters{{grid-template-columns:1fr}}header{{padding:18px 16px}}}}
</style></head><body><header><div class='brand'><img src='/logo.png' alt='Logo Triple 555'><div><h1>Ikatan Persaudaraan Triple 555</h1><p>Portal khusus pimpinan · Sistem Survei Bantuan UMKM</p><div class='user'>Masuk sebagai: {_lead_e(username)} · Pimpinan/Admin</div></div></div></header><main><nav class='nav'><a class='btn light' href='/pimpinan'>Ringkasan</a><a class='btn light' href='/pimpinan/survei'>Laporan Survei DPSS</a><a class='btn light' href='/pimpinan/verifikasi'>Laporan Verifikasi DPS</a><a class='btn light' href='/pimpinan/peta'>Peta Lokasi GPS</a><a class='btn light' href='/pimpinan/personel'>Data Personel</a><a class='btn light' href='/admin/kelola-akun'>Kelola Akses Aplikasi</a></nav>{content}</main><footer>© 2026 Ikatan Persaudaraan Triple 555 · Data digunakan sesuai kewenangan organisasi.</footer></body></html>"""



# =========================================================
# MODUL PROFIL PERSONEL DPS / DPSS
# =========================================================

def _officer_private_bucket():
    return os.environ.get("SUPABASE_OFFICER_BUCKET", "officer-private").strip() or "officer-private"


def _upload_officer_private(file: UploadFile | None, member_id: str, kind: str) -> str:
    if file is None or not file.filename:
        return ""
    content_type = (file.content_type or "").lower().split(";", 1)[0].strip()
    allowed_types = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    if content_type not in allowed_types:
        raise HTTPException(status_code=400, detail=f"{kind}: format harus JPG, PNG, atau WEBP.")
    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail=f"{kind}: berkas kosong.")
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"{kind}: ukuran maksimal 5 MB.")
    supabase_url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    supabase_key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if not supabase_url or not supabase_key:
        raise HTTPException(status_code=503, detail="Penyimpanan dokumen personel belum dikonfigurasi.")
    ext = allowed_types[content_type]
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", member_id)
    object_path = f"{safe_id}/{kind}-{uuid.uuid4().hex}{ext}"
    bucket = _officer_private_bucket()
    upload_url = f"{supabase_url}/storage/v1/object/{quote(bucket, safe='')}/{quote(object_path, safe='/')}"
    req = urllib.request.Request(
        upload_url, data=data,
        headers={
            "Authorization": f"Bearer {supabase_key}",
            "apikey": supabase_key,
            "Content-Type": content_type or "application/octet-stream",
            "x-upsert": "false",
        }, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:500]
        raise HTTPException(status_code=502, detail=f"Gagal mengunggah {kind} ke penyimpanan privat: {detail}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=f"Koneksi penyimpanan {kind} gagal: {exc}")
    return object_path


def _officer_profile_rows(search_text=""):
    with get_db() as connection:
        return connection.execute("""
            SELECT o.id AS officer_id, o.member_id, o.member_name, o.role, o.region, o.active,
                   COALESCE(p.nik,'') AS nik, COALESCE(p.phone,'') AS phone,
                   COALESCE(p.address,'') AS address,
                   COALESCE(p.birth_place,'') AS birth_place, p.birth_date,
                   COALESCE(p.profile_photo_path,'') AS profile_photo_path,
                   COALESCE(p.ktp_path,'') AS ktp_path,
                   COALESCE(string_agg(DISTINCT v.village_name, ', '), '') AS villages
            FROM officers o
            LEFT JOIN officer_profiles p ON p.officer_id=o.id
            LEFT JOIN officer_villages ov ON ov.officer_id=o.id
            LEFT JOIN villages v ON v.id=ov.village_id
            WHERE (%s = '' OR o.member_id ILIKE %s OR o.member_name ILIKE %s
                   OR COALESCE(p.phone,'') ILIKE %s OR COALESCE(o.region,'') ILIKE %s
                   OR COALESCE(v.village_name,'') ILIKE %s)
            GROUP BY o.id, p.officer_id
            ORDER BY CASE WHEN o.role='DPS' THEN 0 ELSE 1 END, o.member_name
        """, (search_text, f"%{search_text}%", f"%{search_text}%",
              f"%{search_text}%", f"%{search_text}%", f"%{search_text}%")).fetchall()


@app.get("/admin/data-anggota/{registration_id}/dokumen-identitas")
def admin_view_member_identity_document(registration_id: int, username: str = Depends(verify_admin)):
    """Buat signed URL baru saat admin membuka dokumen identitas anggota."""
    with get_db() as connection:
        row = connection.execute("SELECT ktp_path FROM organization_member_registrations WHERE id=%s AND status='approved'", (registration_id,)).fetchone()
    if not row or not row.get("ktp_path"):
        raise HTTPException(status_code=404, detail="Dokumen identitas anggota tidak ditemukan.")
    signed_url = create_supabase_signed_url(row["ktp_path"], expires_in=300, bucket=_officer_private_bucket())
    if not signed_url:
        raise HTTPException(status_code=502, detail="Dokumen identitas tidak dapat dibuka dari penyimpanan privat.")
    return RedirectResponse(url=signed_url, status_code=302)


@app.get("/admin/data-anggota", response_class=HTMLResponse)
def admin_data_anggota(q: str = "", tingkat: str = "SEMUA", username: str = Depends(verify_admin)):
    """Direktori seluruh anggota yang telah disetujui, terpisah dari daftar akun aplikasi."""
    tingkat = tingkat.strip().upper()
    if tingkat not in {"SEMUA", "DPS", "DPSS"}:
        tingkat = "SEMUA"
    term = q.strip()
    with get_db() as connection:
        rows = connection.execute("""SELECT r.*,v.village_name
            FROM organization_member_registrations r
            LEFT JOIN villages v ON v.id=r.village_id
            WHERE r.status='approved' AND (%s='SEMUA' OR r.org_level=%s)
              AND (%s='' OR r.full_name ILIKE %s OR r.member_code ILIKE %s OR r.nik ILIKE %s
                   OR r.phone ILIKE %s OR r.org_position ILIKE %s OR r.region ILIKE %s
                   OR COALESCE(v.village_name,'') ILIKE %s)
            ORDER BY r.org_level,r.region,v.village_name,r.full_name""",
            (tingkat,tingkat,term,*([f"%{term}%"]*7))).fetchall()
    cards=[]
    for r in rows:
        photo_url=create_supabase_signed_url(r['profile_photo_path'],1800,_officer_private_bucket()) if r['profile_photo_path'] else None
        has_ktp=bool(r['ktp_path'])
        photo_html=f"<img class='photo' src='{_lead_e(photo_url)}' alt='Foto {_lead_e(r['full_name'])}'>" if photo_url else "<div class='photo no-photo'>Foto belum tersedia</div>"
        doc_html=f"<a class='btn doc' href='/admin/data-anggota/{int(r['id'])}/dokumen-identitas' target='_blank' rel='noopener'>Lihat Dokumen Identitas</a>" if has_ktp else "<span class='muted'>Dokumen identitas belum tersedia</span>"
        birth_date=r['birth_date'].isoformat() if r['birth_date'] else '-'
        village=r['village_name'] or ('Seluruh Desa' if r['org_level']=='DPS' else '-')
        cards.append(f"""<article class='member-card'>{photo_html}<div class='member-data'><div class='topline'><div><h2>{_lead_e(r['full_name'])}</h2><span class='id'>ID Member: {_lead_e(r['member_code'] or '-')}</span></div><span class='level'>{_lead_e(r['org_level'])}</span></div>
        <div class='details'><div><b>NIK</b><span>{_lead_e(r['nik'])}</span></div><div><b>Jenis Kelamin</b><span>{_lead_e(r['gender'] or '-')}</span></div><div><b>Tempat, Tanggal Lahir</b><span>{_lead_e(r['birth_place'] or '-')}, {_lead_e(birth_date)}</span></div><div><b>Nomor HP / WhatsApp</b><span>{_lead_e(r['phone'] or '-')}</span></div><div><b>Email</b><span>{_lead_e(r['email'] or '-')}</span></div><div><b>Jabatan</b><span>{_lead_e(r['org_position'])}</span></div><div><b>Wilayah</b><span>{_lead_e(r['region'] or '-')} · {_lead_e(village)}</span></div><div class='wide'><b>Alamat</b><span>{_lead_e(r['address'] or '-')}</span></div></div><div class='doc-row'>{doc_html}<a class='btn light' href='/admin/pendaftaran-anggota/{int(r['id'])}/edit'>Edit Data</a></div></div></article>""")
    cards_html=''.join(cards) or "<div class='empty'>Belum ada anggota terverifikasi yang sesuai dengan pencarian.</div>"
    html=f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Daftar Seluruh Anggota | Triple 555</title>
    <style>*{{box-sizing:border-box}}body{{margin:0;background:#f5f1eb;color:#292323;font:14px/1.5 system-ui,Arial,sans-serif}}header{{background:linear-gradient(120deg,#31090e,#5b1018);color:#fff;padding:22px;border-bottom:4px solid #d8b15a}}main{{max-width:1250px;margin:auto;padding:18px}}.nav,.filters,.doc-row{{display:flex;gap:9px;flex-wrap:wrap;align-items:center}}.nav{{margin-bottom:15px}}.btn,button{{display:inline-block;background:#5b1018;color:#fff;text-decoration:none;border:0;border-radius:8px;padding:9px 12px;font-weight:700;cursor:pointer}}.btn.light{{background:#eee8e1;color:#382e2e}}.panel{{background:#fff;padding:16px;border-radius:13px;margin-bottom:14px}}input,select{{padding:10px;border:1px solid #d6cec6;border-radius:8px;font:inherit}}.filters input{{flex:1;min-width:220px}}.count{{color:#6c625b;margin:10px 0}}.grid{{display:grid;gap:13px}}.member-card{{display:grid;grid-template-columns:145px 1fr;gap:17px;background:#fff;border:1px solid #e7ded4;border-radius:14px;padding:16px;box-shadow:0 3px 12px #32100b08}}.photo{{width:145px;height:170px;border-radius:10px;object-fit:cover;background:#f0ece7}}.no-photo{{display:grid;place-items:center;text-align:center;color:#777;padding:10px}}.topline{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}}h2{{margin:0 0 4px;color:#5b1018;font-size:19px}}.id{{font-weight:800;color:#775313}}.level{{background:#f5ead5;color:#5b1018;border-radius:20px;padding:5px 11px;font-weight:800}}.details{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:14px 0}}.details div{{min-width:0}}.details b,.details span{{display:block}}.details b{{font-size:11px;color:#786e65;text-transform:uppercase}}.details span{{overflow-wrap:anywhere}}.details .wide{{grid-column:1/-1}}.doc-row{{border-top:1px solid #eee5dc;padding-top:12px}}.doc{{background:#8b641c}}.muted,.empty{{color:#766d65}}.empty{{background:#fff;padding:28px;text-align:center;border-radius:12px}}@media(max-width:700px){{main{{padding:11px}}.member-card{{grid-template-columns:90px 1fr;padding:11px;gap:11px}}.photo{{width:90px;height:115px}}.details{{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}}.topline{{flex-direction:column}}}}</style></head><body><header><h1 style='margin:0'>Daftar Seluruh Anggota DPS dan DPSS</h1><div>Data anggota terverifikasi · Administrasi Organisasi Triple 555</div></header><main>
    <nav class='nav'><a class='btn light' href='/admin/dashboard'>Dashboard Utama Admin</a><a class='btn light' href='/admin/kelola-akun'>Kelola Akses Aplikasi</a><a class='btn light' href='/admin/pendaftaran-anggota'>Verifikasi Pendaftaran</a></nav>
    <section class='panel'><form class='filters' method='get'><input name='q' value='{_lead_e(term)}' placeholder='Cari nama, ID Member, NIK, HP, jabatan, atau wilayah'><select name='tingkat'><option value='SEMUA' {'selected' if tingkat=='SEMUA' else ''}>Semua tingkat</option><option value='DPS' {'selected' if tingkat=='DPS' else ''}>DPS</option><option value='DPSS' {'selected' if tingkat=='DPSS' else ''}>DPSS</option></select><button type='submit'>Cari / Filter</button><a class='btn light' href='/admin/data-anggota'>Reset</a></form><p class='count'>Jumlah anggota terverifikasi: <b>{len(rows)}</b></p></section><section class='grid'>{cards_html}</section></main></body></html>"""
    return HTMLResponse(html)


@app.get("/admin/data-personel", response_class=HTMLResponse)
def admin_data_personel(q: str = "", username: str = Depends(verify_admin)):
    rows = _officer_profile_rows(q.strip())
    body = []
    for r in rows:
        status = "Aktif" if r["active"] == 1 else "Nonaktif"
        photo = create_supabase_signed_url(r["profile_photo_path"], bucket=_officer_private_bucket()) if r["profile_photo_path"] else None
        photo_html = f"<img class='avatar' src='{_lead_e(photo)}'>" if photo else "<div class='avatar blank'>Foto<br>belum ada</div>"
        territory = r["villages"] or r["region"] or "-"
        body.append(f"""<tr><td>{photo_html}</td><td><b>{_lead_e(r['member_name'])}</b><br><small>{_lead_e(r['member_id'])} · {_lead_e(r['role'])}</small></td><td>{_lead_e(r['phone'] or '-')}</td><td>{_lead_e(territory)}</td><td>{status}</td><td><div style='display:flex;gap:7px;flex-wrap:wrap'><a class='btn' href='/admin/data-personel/{r['officer_id']}'>Isi / Edit Data</a><form method='post' action='/admin/data-personel/{r['officer_id']}/hapus' onsubmit="return confirm('Hapus data personel {_lead_e(r['member_name'])} dengan ID {_lead_e(r['member_id'])}? Akun dan pendaftaran terkait akan dihapus. Data survei/verifikasi tidak akan dihapus.');"><button class='btn danger' type='submit'>Hapus</button></form></div></td></tr>""")
    if not body:
        body = ["<tr><td colspan='6'>Belum ada akun petugas.</td></tr>"]
    deleted_notice = "<div style='background:#eaf7ef;color:#14532d;padding:11px 14px;border-radius:9px;margin:12px 0'>Data personel dan pendaftaran terkait berhasil dihapus.</div>" if request.query_params.get("deleted") == "1" else ""
    html = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Data Personel DPS/DPSS</title>
    <style>body{{font:14px system-ui;background:#f6f3ee;margin:0;color:#292323}}header{{background:linear-gradient(120deg,#31090e,#5b1018);color:white;padding:22px}}main{{max-width:1200px;margin:auto;padding:20px}}.panel{{background:white;padding:18px;border-radius:13px;margin:12px 0;overflow:auto}}table{{width:100%;border-collapse:collapse;min-width:800px}}td,th{{padding:10px;border-bottom:1px solid #e5e0d8;text-align:left}}th{{color:#5b1018;background:#f8f4ec}}input{{padding:10px;border:1px solid #ddd;border-radius:8px}}.btn,button{{background:#5b1018;color:white;border:0;border-radius:8px;padding:9px 12px;text-decoration:none;font-weight:bold;cursor:pointer}}.danger{{background:#a32020}}form{{margin:0}}.avatar{{width:58px;height:58px;border-radius:50%;object-fit:cover;background:#eee}}.blank{{display:flex;align-items:center;justify-content:center;text-align:center;font-size:10px;color:#777}}small{{color:#777}}</style></head><body><header><h1>Data Personel DPS dan DPSS</h1><p>Dashboard pendataan identitas petugas · Masuk sebagai {_lead_e(username)}</p></header><main><p><a class='btn' href='/admin/dashboard'>Dashboard Utama Admin</a> <a class='btn' href='/admin/pendaftaran-anggota'>Verifikasi Pendaftaran Mandiri</a> <a class='btn' href='/pendaftaran-anggota' target='_blank' rel='noopener'>Buka Formulir Anggota</a></p>{deleted_notice}<section class='panel'><form method='get'><input name='q' value='{_lead_e(q)}' placeholder='Cari nama, ID, nomor HP, wilayah' style='width:min(440px,75%)'><button>Cari</button></form><h2>Daftar akun petugas ({len(rows)})</h2><table><thead><tr><th>Foto</th><th>Nama / ID / Peran</th><th>Nomor HP</th><th>Wilayah / Desa</th><th>Status akun</th><th>Kelola</th></tr></thead><tbody>{''.join(body)}</tbody></table></section></main></body></html>"""
    return HTMLResponse(html)


@app.get("/admin/data-personel/{officer_id}", response_class=HTMLResponse)
def admin_edit_data_personel(officer_id: int, request: Request, username: str = Depends(verify_admin)):
    with get_db() as connection:
        row = connection.execute("""
            SELECT o.id AS officer_id,o.member_id,o.member_name,o.role,o.region,o.active,
                   p.nik,p.phone,p.address,p.birth_place,p.birth_date,p.profile_photo_path,p.ktp_path
            FROM officers o LEFT JOIN officer_profiles p ON p.officer_id=o.id WHERE o.id=%s
        """, (officer_id,)).fetchone()
        village_rows = connection.execute(
            "SELECT id, village_name, district FROM villages WHERE active=1 ORDER BY district, village_name"
        ).fetchall()
        assigned_rows = connection.execute(
            "SELECT village_id FROM officer_villages WHERE officer_id=%s", (officer_id,)
        ).fetchall()
    if not row:
        raise HTTPException(status_code=404, detail="Akun petugas tidak ditemukan.")
    assigned_ids = {v["village_id"] for v in assigned_rows}
    village_controls = "".join(
        f"<label class='village'><input type='checkbox' name='village_ids' value='{v['id']}' {'checked' if v['id'] in assigned_ids else ''}><span>{_lead_e(v['village_name'])} <small>· {_lead_e(v['district'])}</small></span></label>"
        for v in village_rows
    ) or "<p>Belum ada data desa aktif. Tambahkan melalui pengelolaan wilayah terlebih dahulu.</p>"
    photo = create_supabase_signed_url(row["profile_photo_path"], bucket=_officer_private_bucket()) if row["profile_photo_path"] else None
    ktp = create_supabase_signed_url(row["ktp_path"], bucket=_officer_private_bucket()) if row["ktp_path"] else None
    saved = "<div class='notice'>Perubahan profil berhasil disimpan.</div>" if request.query_params.get("saved") == "1" else ""
    html = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Edit Profil Personel</title><style>
    body{{font:14px system-ui;background:#f6f3ee;margin:0;color:#292323}}header{{background:#31090e;color:white;padding:20px}}main{{max-width:900px;margin:auto;padding:20px}}form,.panel{{background:white;padding:20px;border-radius:12px;margin-bottom:14px}}label{{display:block;font-weight:bold;margin:12px 0 5px}}input,textarea,select{{width:100%;padding:10px;border:1px solid #ddd;border-radius:8px;box-sizing:border-box}}input[type=checkbox]{{width:18px}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}button,.btn{{display:inline-block;background:#5b1018;color:white;padding:10px 14px;border:0;border-radius:8px;text-decoration:none;margin-top:16px;cursor:pointer}}img{{max-width:180px;max-height:180px;object-fit:contain;margin:8px}}small,.muted{{color:#777}}.village-list{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:8px}}.village{{display:flex;align-items:center;gap:8px;border:1px solid #e4ded7;border-radius:8px;padding:8px;margin:0}}.notice{{background:#eaf7ef;color:#14532d;padding:10px;border-radius:8px;margin-bottom:12px}}@media(max-width:650px){{.grid,.village-list{{grid-template-columns:1fr}}}}
    </style></head><body><header><h1>Pengelolaan Data Personel</h1><div>{_lead_e(row['member_name'])} · {_lead_e(row['member_id'])} · {_lead_e(row['role'])}</div></header><main><p><a class='btn' href='/admin/data-personel'>Kembali ke daftar personel</a></p>{saved}<form method='post' enctype='multipart/form-data' action='/admin/data-personel/{officer_id}'>
    <h2>1. Data akun dan penugasan</h2><div class='grid'><div><label>Nama lengkap</label><input name='member_name' value='{_lead_e(row['member_name'])}' maxlength='100' required></div><div><label>ID anggota / petugas</label><input value='{_lead_e(row['member_id'])}' disabled></div>
    <div><label>Peran</label><select name='role'><option value='DPSS' {'selected' if row['role']=='DPSS' else ''}>DPSS · Desa/Kelurahan</option><option value='DPS' {'selected' if row['role']=='DPS' else ''}>DPS · Kecamatan</option></select></div>
    <div><label>Wilayah / Kecamatan</label><input name='region' value='{_lead_e(row['region'] or '')}' maxlength='150' placeholder='Contoh: Tambang'></div>
    <div><label>Status akun</label><select name='active'><option value='1' {'selected' if row['active']==1 else ''}>Aktif</option><option value='0' {'selected' if row['active']!=1 else ''}>Nonaktif</option></select></div></div>
    <label>Desa penugasan (khusus DPSS)</label><div class='village-list'>{village_controls}</div><p class='muted'>Pilih desa yang menjadi wilayah tugas petugas DPSS. Untuk akun DPS, daftar desa tidak digunakan.</p>
    <h2 style='margin-top:28px'>2. Identitas dan kontak</h2><div class='grid'><div><label>NIK</label><input name='nik' value='{_lead_e(row['nik'] or '')}' maxlength='30' inputmode='numeric'></div><div><label>Nomor HP</label><input name='phone' value='{_lead_e(row['phone'] or '')}' maxlength='30' inputmode='tel'></div><div><label>Tempat lahir</label><input name='birth_place' value='{_lead_e(row['birth_place'] or '')}' maxlength='100'></div><div><label>Tanggal lahir</label><input type='date' name='birth_date' value='{_lead_e(row['birth_date'].isoformat() if row['birth_date'] else '')}'></div></div>
    <label>Alamat lengkap</label><textarea name='address' rows='3'>{_lead_e(row['address'] or '')}</textarea>
    <h2 style='margin-top:28px'>3. Dokumen personel</h2><label>Foto profil (JPG/PNG/WEBP, maksimal 5 MB)</label><input type='file' name='profile_photo' accept='image/jpeg,image/png,image/webp'>{f'<img src="{_lead_e(photo)}" alt="Foto profil saat ini">' if photo else '<small>Belum ada foto profil.</small>'}
    <label>Foto / scan KTP (JPG/PNG/WEBP, maksimal 5 MB)</label><input type='file' name='ktp_photo' accept='image/jpeg,image/png,image/webp'>{f'<img src="{_lead_e(ktp)}" alt="KTP tersimpan">' if ktp else '<small>Belum ada dokumen KTP.</small>'}
    <p><button type='submit'>Simpan Seluruh Data Personel</button></p></form></main></body></html>"""
    return HTMLResponse(html)


@app.post("/admin/data-personel/{officer_id}")
def admin_save_data_personel(
    officer_id: int,
    member_name: str = Form(...), role: str = Form(...), region: str = Form(""),
    active: int = Form(1), village_ids: list[int] = Form([]),
    nik: str = Form(""), phone: str = Form(""), address: str = Form(""),
    birth_place: str = Form(""), birth_date: str = Form(""),
    profile_photo: UploadFile | None = File(None), ktp_photo: UploadFile | None = File(None),
    username: str = Depends(verify_admin),
):
    member_name = member_name.strip()
    role = role.strip().upper()
    region = region.strip()
    if len(member_name) < 2:
        raise HTTPException(status_code=422, detail="Nama lengkap minimal 2 karakter.")
    if role not in {"DPS", "DPSS"}:
        raise HTTPException(status_code=422, detail="Peran hanya boleh DPS atau DPSS.")
    if active not in (0, 1):
        raise HTTPException(status_code=422, detail="Status akun tidak valid.")
    if birth_date.strip():
        try:
            datetime.strptime(birth_date.strip(), "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Tanggal lahir tidak valid.") from exc
    with get_db() as connection:
        officer = connection.execute("SELECT id,member_id FROM officers WHERE id=%s", (officer_id,)).fetchone()
        if not officer:
            raise HTTPException(status_code=404, detail="Akun petugas tidak ditemukan.")
        if role == "DPSS" and village_ids:
            valid_villages = connection.execute(
                "SELECT id FROM villages WHERE active=1 AND id=ANY(%s)", (village_ids,)
            ).fetchall()
            if len({v["id"] for v in valid_villages}) != len(set(village_ids)):
                raise HTTPException(status_code=422, detail="Ada desa penugasan yang tidak valid atau tidak aktif.")
    photo_path = _upload_officer_private(profile_photo, officer["member_id"], "profile") if profile_photo and profile_photo.filename else ""
    ktp_path = _upload_officer_private(ktp_photo, officer["member_id"], "ktp") if ktp_photo and ktp_photo.filename else ""
    with get_db() as connection:
        connection.execute(
            "UPDATE officers SET member_name=%s, role=%s, region=%s, active=%s, updated_at=NOW() WHERE id=%s",
            (member_name, role, region, active, officer_id),
        )
        connection.execute(
            "UPDATE members SET member_name=%s, active=%s WHERE member_id=%s",
            (member_name, active, officer["member_id"]),
        )
        if role == "DPSS":
            connection.execute("DELETE FROM officer_villages WHERE officer_id=%s", (officer_id,))
            for village_id in sorted(set(village_ids)):
                connection.execute(
                    "INSERT INTO officer_villages (officer_id,village_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (officer_id, village_id),
                )
        else:
            connection.execute("DELETE FROM officer_villages WHERE officer_id=%s", (officer_id,))
        if active == 0:
            connection.execute("DELETE FROM officer_sessions WHERE officer_id=%s", (officer_id,))
        connection.execute("""
            INSERT INTO officer_profiles (officer_id,nik,phone,address,birth_place,birth_date,profile_photo_path,ktp_path,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (officer_id) DO UPDATE SET
              nik=EXCLUDED.nik, phone=EXCLUDED.phone, address=EXCLUDED.address,
              birth_place=EXCLUDED.birth_place, birth_date=EXCLUDED.birth_date,
              profile_photo_path=CASE WHEN EXCLUDED.profile_photo_path='' THEN officer_profiles.profile_photo_path ELSE EXCLUDED.profile_photo_path END,
              ktp_path=CASE WHEN EXCLUDED.ktp_path='' THEN officer_profiles.ktp_path ELSE EXCLUDED.ktp_path END,
              updated_at=NOW()
        """, (officer_id, nik.strip(), phone.strip(), address.strip(), birth_place.strip(),
              birth_date.strip() or None, photo_path, ktp_path))
    return RedirectResponse(url=f"/admin/data-personel/{officer_id}?saved=1", status_code=303)


@app.post("/admin/data-personel/{officer_id}/hapus")
def admin_delete_data_personel(officer_id: int, username: str = Depends(verify_admin)):
    """Hapus akun/personel dan pendaftaran terkait hanya jika belum memiliki aktivitas survei/verifikasi."""
    with get_db() as connection:
        officer = connection.execute(
            "SELECT id, member_id, member_name FROM officers WHERE id=%s FOR UPDATE",
            (officer_id,),
        ).fetchone()
        if not officer:
            raise HTTPException(status_code=404, detail="Akun personel tidak ditemukan.")

        member_id = officer["member_id"]
        survey_count = connection.execute(
            "SELECT COUNT(*) AS n FROM surveys WHERE member_id=%s", (member_id,)
        ).fetchone()["n"]
        media_count = connection.execute(
            "SELECT COUNT(*) AS n FROM survey_media WHERE member_id=%s", (member_id,)
        ).fetchone()["n"]
        verification_count = connection.execute(
            "SELECT COUNT(*) AS n FROM survey_verifications WHERE verifier_id=%s", (member_id,)
        ).fetchone()["n"]
        verification_media_count = connection.execute(
            "SELECT COUNT(*) AS n FROM verification_media WHERE verifier_id=%s", (member_id,)
        ).fetchone()["n"]
        if survey_count or media_count or verification_count or verification_media_count:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Data tidak dapat dihapus karena akun sudah memiliki aktivitas: "
                    f"{survey_count} survei, {media_count} media survei, "
                    f"{verification_count} verifikasi, dan {verification_media_count} media verifikasi. "
                    "Nonaktifkan akun jika akses perlu dihentikan."
                ),
            )

        # Hanya pendaftaran yang tertaut ke ID akun ini yang ikut dibersihkan.
        connection.execute(
            "DELETE FROM organization_member_registrations WHERE linked_member_id=%s OR member_code=%s",
            (member_id, member_id),
        )
        # FK ON DELETE CASCADE membersihkan sesi, profil, dan penugasan desa milik officer.
        connection.execute("DELETE FROM officers WHERE id=%s", (officer_id,))
        connection.execute("DELETE FROM members WHERE member_id=%s", (member_id,))

    return RedirectResponse(url="/admin/data-personel?deleted=1", status_code=303)


# =========================================================
# PENDAFTARAN MANDIRI ANGGOTA ORGANISASI 555
# =========================================================

@app.get("/pendaftaran-anggota", response_class=HTMLResponse)
def public_member_registration_form():
    with get_db() as connection:
        villages = connection.execute(
            "SELECT id, village_name, district FROM villages WHERE active=1 ORDER BY village_name"
        ).fetchall()
    village_options = "".join(
        f"<option value='{int(v['id'])}'>{_lead_e(v['village_name'])} · {_lead_e(v['district'])}</option>"
        for v in villages
    )
    html = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'><title>Pendataan Anggota 555</title>
    <style>
    :root{{--maroon:#5b1018;--deep:#31090e;--gold:#d8b15a}}*{{box-sizing:border-box}}
    body{{margin:0;background:#f4f0eb;color:#292323;font:15px/1.5 system-ui,Arial,sans-serif}}
    header{{background:linear-gradient(120deg,var(--deep),var(--maroon));color:#fff;padding:25px 18px;border-bottom:4px solid var(--gold)}}
    header .brand{{max-width:850px;margin:auto;display:flex;align-items:center;gap:16px}}header .brand img{{width:76px;height:76px;object-fit:contain;border-radius:50%;background:#fff;padding:4px;border:2px solid var(--gold)}}header .brand h1{{margin:0 0 5px;font-size:25px}}header .brand p{{margin:0;color:#f3e5d0}}
    main{{padding:20px 14px 45px}}.panel{{background:white;border:1px solid #e5ddd4;border-radius:14px;padding:20px;margin-bottom:15px;box-shadow:0 3px 14px #3b1b1010}}
    h2{{font-size:18px;margin:0 0 13px;color:var(--maroon)}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
    label{{display:block;font-weight:650;margin:10px 0 5px}}input,select,textarea{{width:100%;padding:11px;border:1px solid #d5cec7;border-radius:8px;font:inherit;background:white}}
    input[type=file]{{padding:8px}}.full{{grid-column:1/-1}}.hint{{font-size:13px;color:#6b625c;margin:5px 0}}button{{background:var(--maroon);color:white;border:0;border-radius:9px;padding:13px 20px;font:inherit;font-weight:700;cursor:pointer}}
    .notice{{background:#fff7e5;border-left:4px solid var(--gold);padding:12px;border-radius:7px;font-size:13px}}
    @media(max-width:620px){{header .brand img{{width:58px;height:58px}}header .brand h1{{font-size:20px}}.grid{{grid-template-columns:1fr}}.full{{grid-column:auto}}main{{padding:12px}}.panel{{padding:15px}}}}
    </style></head><body><header><div class='brand'><img src='/logo.png' alt='Logo Ikatan Persaudaraan Triple 555'><div><h1>Pendataan Mandiri Anggota</h1><p>Ikatan Persaudaraan Triple 555</p></div></div></header>
    <main><div class='panel'><div class='notice'><b>Petunjuk:</b> Formulir ini diisi oleh masing-masing anggota. Data akan diperiksa admin sebelum ditetapkan sebagai data resmi organisasi. Pastikan NIK dan nomor HP benar. Foto KTP hanya dapat diakses admin yang berwenang.</div>
    <form method='post' action='/pendaftaran-anggota' enctype='multipart/form-data'>
    <h2 style='margin-top:18px'>1. Identitas anggota</h2><div class='grid'>
    <div class='full'><label>Nama lengkap sesuai KTP *</label><input name='full_name' required maxlength='150' autocomplete='name'></div>
    <div><label>NIK *</label><input name='nik' required minlength='16' maxlength='16' inputmode='numeric' pattern='[0-9]{{16}}' title='NIK harus 16 digit'></div>
    <div><label>Jenis kelamin *</label><select name='gender' required><option value=''>Pilih</option><option value='Laki-laki'>Laki-laki</option><option value='Perempuan'>Perempuan</option></select></div>
    <div><label>Tempat lahir</label><input name='birth_place' maxlength='100'></div><div><label>Tanggal lahir</label><input type='date' name='birth_date'></div>
    <div class='full'><label>Alamat lengkap *</label><textarea name='address' rows='3' required maxlength='1500'></textarea></div>
    <div><label>Nomor HP / WhatsApp *</label><input name='phone' required maxlength='30' inputmode='tel' autocomplete='tel'></div>
    <div><label>Email (opsional)</label><input type='email' name='email' maxlength='150' autocomplete='email'></div></div></div>
    <div class='panel'><h2>2. Kedudukan dalam organisasi</h2><div class='grid'>
    <div><label>Tingkat kepengurusan *</label><select name='org_level' id='org_level' required><option value=''>Pilih</option><option value='DPS'>DPS · Kecamatan</option><option value='DPSS'>DPSS · Desa/Kelurahan</option></select></div>
    <div><label>Jabatan organisasi sesuai SK *</label><input name='org_position' required maxlength='150' placeholder='Isi jabatan sesuai SK pengangkatan'></div>
    <div><label>Wilayah / Kecamatan *</label><input name='region' required maxlength='150' value='Tambang'></div>
    <div><label id='village_label'>Cakupan wilayah desa</label><select name='village_id' id='village_id'><option value='all'>Seluruh Desa (cakupan DPS)</option>{village_options}</select><p class='hint' id='village_hint'>Untuk DPS, pilih Seluruh Desa atau pilih desa tertentu bila penugasan hanya mencakup desa tersebut.</p></div>
    </div><p class='hint'>Isi jabatan persis seperti yang tercantum dalam SK. Admin akan memeriksa kesesuaian tingkat dan wilayah.</p></div>
    <div class='panel'><h2>3. Foto dan dokumen</h2>
    <label>Foto profil *</label><input type='file' name='profile_photo' accept='image/jpeg,image/png,image/webp' required><p class='hint'>JPG, PNG, atau WEBP. Maksimal 5 MB.</p>
    <label>Foto / scan KTP *</label><input type='file' name='ktp_photo' accept='image/jpeg,image/png,image/webp' required><p class='hint'>Pastikan tulisan pada KTP terbaca. File disimpan di penyimpanan privat.</p>
    <label>Dokumen SK (opsional, PDF/JPG/PNG belum diaktifkan pada tahap ini)</label><p class='hint'>Untuk sementara, dokumen SK dapat diperiksa admin secara terpisah. Formulir ini hanya menerima foto profil dan KTP.</p>
    </div><div class='panel'><div class='notice'>Dengan mengirim formulir ini, anggota menyatakan data yang diisi benar dan menyetujui penggunaannya untuk administrasi internal organisasi 555.</div><p style='margin:16px 0 0'><button type='submit'>Kirim Data Anggota</button></p></div>
    </form><script>
    (function(){{
      const level=document.getElementById('org_level');
      const village=document.getElementById('village_id');
      const label=document.getElementById('village_label');
      const hint=document.getElementById('village_hint');
      function syncVillage(){{
        if(level.value==='DPSS'){{
          label.textContent='Desa / Kelurahan DPSS *';
          hint.textContent='Untuk DPSS, wajib pilih desa tempat kepengurusan.';
          village.required=true;
          if(village.value==='all') village.value='';
          village.options[0].textContent='Pilih desa';
          village.options[0].value='';
        }}else{{
          label.textContent='Cakupan wilayah desa';
          hint.textContent='Untuk DPS, pilih Seluruh Desa atau pilih desa tertentu bila penugasan hanya mencakup desa tersebut.';
          village.required=false;
          village.options[0].textContent='Seluruh Desa (cakupan DPS)';
          village.options[0].value='all';
          if(!village.value) village.value='all';
        }}
      }}
      level.addEventListener('change',syncVillage);
      syncVillage();
    }})();
    </script></main></body></html>"""
    return HTMLResponse(html)


@app.post("/pendaftaran-anggota", response_class=HTMLResponse)
def submit_public_member_registration(
    full_name: str = Form(...), nik: str = Form(...), gender: str = Form(...),
    birth_place: str = Form(""), birth_date: str = Form(""), address: str = Form(...),
    phone: str = Form(...), email: str = Form(""), org_level: str = Form(...),
    org_position: str = Form(...), region: str = Form(...), village_id: str = Form("all"),
    profile_photo: UploadFile | None = File(None),
    ktp_photo: UploadFile | None = File(None),
):
    full_name, nik, gender = full_name.strip(), re.sub(r"\D", "", nik), gender.strip()
    address, phone, email = address.strip(), phone.strip(), email.strip()
    org_level, org_position, region = org_level.strip().upper(), org_position.strip(), region.strip()
    village_value = (village_id or "").strip().lower()
    if village_value in {"", "all", "semua"}:
        village_id = None
    else:
        try:
            village_id = int(village_value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Pilihan desa tidak valid.") from exc
    if len(full_name) < 3 or len(nik) != 16 or gender not in {"Laki-laki", "Perempuan"}:
        raise HTTPException(status_code=422, detail="Periksa nama, NIK 16 digit, dan jenis kelamin.")
    if not address or len(phone) < 8 or org_level not in {"DPS", "DPSS"} or len(org_position) < 2 or not region:
        raise HTTPException(status_code=422, detail="Lengkapi alamat, nomor HP, tingkat kepengurusan, jabatan, dan wilayah.")
    if birth_date.strip():
        try:
            datetime.strptime(birth_date.strip(), "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Tanggal lahir tidak valid.") from exc
    if not profile_photo or not profile_photo.filename or not ktp_photo or not ktp_photo.filename:
        raise HTTPException(status_code=422, detail="Foto profil dan foto KTP wajib diunggah.")
    code = "REG-" + uuid.uuid4().hex[:16].upper()
    with get_db() as connection:
        duplicate = connection.execute(
            "SELECT id,status FROM organization_member_registrations WHERE nik=%s ORDER BY id DESC LIMIT 1", (nik,)
        ).fetchone()
        if duplicate:
            raise HTTPException(status_code=409, detail="NIK ini sudah pernah didaftarkan. Hubungi admin untuk memperbaiki atau memeriksa pendaftaran sebelumnya.")
        if village_id is not None:
            village = connection.execute("SELECT id FROM villages WHERE id=%s AND active=1", (village_id,)).fetchone()
            if not village:
                raise HTTPException(status_code=422, detail="Desa yang dipilih tidak tersedia.")
        if org_level == "DPSS" and village_id is None:
            raise HTTPException(status_code=422, detail="Pilih desa untuk kepengurusan DPSS.")
    photo_path = _upload_officer_private(profile_photo, code, "profile")
    ktp_path = _upload_officer_private(ktp_photo, code, "ktp")
    with get_db() as connection:
        village_name = ""
        if village_id is not None:
            village_row = connection.execute("SELECT village_name FROM villages WHERE id=%s", (village_id,)).fetchone()
            village_name = village_row["village_name"] if village_row else ""
        # Nomor dialokasikan dan baris pendaftaran disimpan dalam transaksi yang sama.
        member_code = _allocate_member_code(connection, org_level, region, village_name)
        connection.execute("""INSERT INTO organization_member_registrations
            (registration_code,member_code,full_name,nik,birth_place,birth_date,gender,address,phone,email,
             org_level,org_position,region,village_id,profile_photo_path,ktp_path)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (code,member_code,full_name,nik,birth_place.strip(),birth_date.strip() or None,gender,address,phone,email,
             org_level,org_position,region,village_id,photo_path,ktp_path))
    return HTMLResponse(f"""<!doctype html><html lang='id'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>Pendaftaran diterima</title><body style='font:16px system-ui;background:#f4f0eb;padding:24px'><main style='max-width:600px;margin:8vh auto;background:white;padding:28px;border-radius:14px;border-top:6px solid #d8b15a'><h2 style='color:#5b1018'>Data berhasil dikirim</h2><p>Terima kasih. Pendaftaran anggota telah diterima dan menunggu pemeriksaan admin.</p><p><b>ID Member: {escape(member_code)}</b></p><p><b>Kode pendaftaran: {escape(code)}</b></p><p>Simpan ID Member dan kode pendaftaran ini untuk keperluan administrasi.</p><p><a href='/pendaftaran-anggota' style='display:inline-block;background:#5b1018;color:#fff;text-decoration:none;padding:11px 16px;border-radius:8px'>Kembali ke Formulir</a></p></main></body></html>""")


@app.get("/admin/pendaftaran-anggota", response_class=HTMLResponse)
def admin_member_registration_list(status: str = "pending", q: str = "", username: str = Depends(verify_admin)):
    allowed = {"pending", "revision", "approved", "rejected", "all"}
    if status not in allowed:
        status = "pending"
    with get_db() as connection:
        rows = connection.execute("""SELECT r.*,v.village_name FROM organization_member_registrations r
            LEFT JOIN villages v ON v.id=r.village_id
            WHERE (%s='all' OR r.status=%s) AND (%s='' OR r.full_name ILIKE %s OR r.nik ILIKE %s OR r.phone ILIKE %s OR r.registration_code ILIKE %s)
            ORDER BY r.created_at DESC LIMIT 300""",
            (status,status,q.strip(),f"%{q.strip()}%",f"%{q.strip()}%",f"%{q.strip()}%",f"%{q.strip()}%")).fetchall()
    cards=[]
    for r in rows:
        photo=create_supabase_signed_url(r['profile_photo_path'],1800,_officer_private_bucket()) if r['profile_photo_path'] else None
        ktp=create_supabase_signed_url(r['ktp_path'],1800,_officer_private_bucket()) if r['ktp_path'] else None
        photo_html=f"<img src='{_lead_e(photo)}' style='width:100px;height:100px;object-fit:cover;border-radius:10px'>" if photo else ''
        ktp_html=f"<a target='_blank' rel='noopener' href='{_lead_e(ktp)}'>Lihat KTP</a>" if ktp else 'Tidak ada'
        cards.append(f"""<article class='card'><div class='head'>{photo_html}<div><b>{_lead_e(r['full_name'])}</b><div>{_lead_e(r['registration_code'])} · ID Member: {_lead_e(r.get('member_code') or '-')} · {_lead_e(r['status'].upper())}</div><div>{_lead_e(r['org_level'])} · {_lead_e(r['org_position'])}</div></div></div>
        <p><b>NIK:</b> {_lead_e(r['nik'])} · <b>HP:</b> {_lead_e(r['phone'])}</p><p><b>Wilayah:</b> {_lead_e(r['region'])} · {_lead_e(('Seluruh Desa' if r['org_level']=='DPS' and not r['village_id'] else (r['village_name'] or '-')))}</p>
        <p><b>Alamat:</b> {_lead_e(r['address'])}</p><p><b>Dokumen:</b> {ktp_html}</p>
        <p style='display:flex;gap:8px;flex-wrap:wrap'><a href='/admin/pendaftaran-anggota/{int(r['id'])}/edit' style='display:inline-block;background:#8a5b16;color:white;padding:9px 12px;border-radius:7px;text-decoration:none'>Edit Data</a></p>
        <form method='post' action='/admin/pendaftaran-anggota/{int(r['id'])}/hapus' onsubmit="return confirm('Hapus permanen pendaftaran ini? Akun personel dan riwayat survei tidak akan dihapus.')"><button type='submit' style='background:#9b2226'>Hapus Pendaftaran</button></form>
        <form method='post' action='/admin/pendaftaran-anggota/{int(r['id'])}'><label>Status pemeriksaan</label><select name='new_status'><option value='pending' {'selected' if r['status']=='pending' else ''}>Menunggu</option><option value='revision' {'selected' if r['status']=='revision' else ''}>Perlu perbaikan</option><option value='approved' {'selected' if r['status']=='approved' else ''}>Disetujui</option><option value='rejected' {'selected' if r['status']=='rejected' else ''}>Ditolak</option></select>
        <label>Catatan admin</label><textarea name='admin_note' rows='2'>{_lead_e(r['admin_note'])}</textarea><p><b>ID Member otomatis:</b> {_lead_e(r.get('member_code') or '-')}<br><small>ID login aplikasi akan menggunakan ID Member ini setelah admin memberikan akses.</small></p><button type='submit'>Simpan Pemeriksaan</button></form></article>""")
    content=f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Verifikasi Pendaftaran Anggota</title><style>
    body{{font:14px system-ui;background:#f4f0eb;margin:0;color:#292323}}header{{background:#31090e;color:white;padding:16px 20px;border-bottom:4px solid #d8b15a}}header .brand{{max-width:1100px;margin:auto;display:flex;align-items:center;gap:14px}}header .brand img{{width:62px;height:62px;object-fit:contain;border-radius:50%;background:#fff;padding:3px}}header h1{{margin:0 0 4px}}main{{max-width:1100px;margin:auto;padding:18px}}.card{{background:white;padding:17px;border-radius:12px;margin:12px 0;border:1px solid #e5ddd4}}.head{{display:flex;gap:14px;align-items:center}}label{{display:block;font-weight:bold;margin:10px 0 5px}}input,select,textarea{{width:100%;padding:10px;border:1px solid #d5cec7;border-radius:8px;box-sizing:border-box;font:inherit}}button{{background:#5b1018;color:white;border:0;padding:11px 15px;border-radius:8px;margin-top:12px}}.filters{{display:flex;gap:8px;flex-wrap:wrap}}.filters>*{{padding:9px;border-radius:8px}}a{{color:#5b1018}}small{{color:#777}}
    </style></head><body><header><div class='brand'><img src='/logo.png' alt='Logo Ikatan Persaudaraan Triple 555'><div><h1>Verifikasi Pendaftaran Anggota 555</h1><div>Admin organisasi · dokumen identitas hanya ditampilkan di halaman ini</div></div></div></header><main>
    <form class='filters' method='get'><select name='status'><option value='pending' {'selected' if status=='pending' else ''}>Menunggu</option><option value='revision' {'selected' if status=='revision' else ''}>Perlu perbaikan</option><option value='approved' {'selected' if status=='approved' else ''}>Disetujui</option><option value='rejected' {'selected' if status=='rejected' else ''}>Ditolak</option><option value='all' {'selected' if status=='all' else ''}>Semua</option></select><input name='q' value='{_lead_e(q)}' placeholder='Cari nama, NIK, HP, kode'><button type='submit'>Tampilkan</button></form>
    <p><a href='/admin/dashboard'>Dashboard Utama Admin</a> &nbsp; | &nbsp; <a href='/admin/data-personel'>Data Personel</a></p>{''.join(cards) if cards else '<p>Belum ada pendaftaran pada kategori ini.</p>'}</main></body></html>"""
    return HTMLResponse(content)


@app.get("/admin/pendaftaran-anggota/{registration_id}/edit", response_class=HTMLResponse)
def admin_edit_member_registration_page(registration_id: int, username: str = Depends(verify_admin)):
    with get_db() as connection:
        row = connection.execute("SELECT * FROM organization_member_registrations WHERE id=%s", (registration_id,)).fetchone()
        villages = connection.execute("SELECT id,village_name FROM villages WHERE active=1 ORDER BY village_name").fetchall()
    if not row:
        raise HTTPException(status_code=404, detail="Pendaftaran tidak ditemukan.")
    village_options = "<option value=''>Seluruh Desa (cakupan DPS)</option>" + "".join(
        f"<option value='{int(v['id'])}' {'selected' if row['village_id']==v['id'] else ''}>{_lead_e(v['village_name'])}</option>" for v in villages
    )
    photo = create_supabase_signed_url(row['profile_photo_path'], 1800, _officer_private_bucket()) if row['profile_photo_path'] else None
    ktp = create_supabase_signed_url(row['ktp_path'], 1800, _officer_private_bucket()) if row['ktp_path'] else None
    html = f"""<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Edit Pendaftaran Anggota</title>
    <style>body{{font:15px system-ui;background:#f4f0eb;margin:0;color:#292323}}header{{background:#31090e;color:white;padding:20px;border-bottom:4px solid #d8b15a}}main{{max-width:850px;margin:22px auto;padding:0 14px}}form,.box{{background:white;padding:20px;border-radius:12px;margin:12px 0}}label{{display:block;font-weight:700;margin:12px 0 5px}}input,select,textarea{{width:100%;box-sizing:border-box;padding:10px;border:1px solid #d5cec7;border-radius:8px;font:inherit}}button,a.btn{{display:inline-block;background:#5b1018;color:white;border:0;border-radius:8px;padding:11px 15px;text-decoration:none;margin-top:14px;cursor:pointer}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}img{{max-width:150px;max-height:150px;object-fit:contain}}small{{color:#666}}@media(max-width:600px){{.grid{{grid-template-columns:1fr}}}}</style></head><body><header><h1>Edit Data Pendaftaran Anggota</h1><div>{_lead_e(row['registration_code'])} · Status: {_lead_e(row['status'])}</div></header><main>
    <form method='post' enctype='multipart/form-data' action='/admin/pendaftaran-anggota/{registration_id}/edit'>
    <div class='grid'><div><label>Nama lengkap *</label><input name='full_name' required maxlength='150' value='{_lead_e(row['full_name'])}'></div><div><label>NIK *</label><input name='nik' required minlength='16' maxlength='16' pattern='[0-9]{{16}}' value='{_lead_e(row['nik'])}'></div>
    <div><label>Jenis kelamin *</label><select name='gender' required><option value='Laki-laki' {'selected' if row['gender']=='Laki-laki' else ''}>Laki-laki</option><option value='Perempuan' {'selected' if row['gender']=='Perempuan' else ''}>Perempuan</option></select></div><div><label>Tempat lahir</label><input name='birth_place' maxlength='100' value='{_lead_e(row['birth_place'])}'></div>
    <div><label>Tanggal lahir</label><input type='date' name='birth_date' value='{_lead_e(row['birth_date'].isoformat() if row['birth_date'] else '')}'></div><div><label>Nomor HP *</label><input name='phone' required maxlength='30' value='{_lead_e(row['phone'])}'></div>
    <div><label>Email</label><input type='email' name='email' maxlength='150' value='{_lead_e(row['email'])}'></div><div><label>Tingkat organisasi *</label><select name='org_level' id='org_level'><option value='DPS' {'selected' if row['org_level']=='DPS' else ''}>DPS</option><option value='DPSS' {'selected' if row['org_level']=='DPSS' else ''}>DPSS</option></select></div>
    <div><label>Jabatan *</label><input name='org_position' required maxlength='150' value='{_lead_e(row['org_position'])}'></div><div><label>Wilayah / Kecamatan *</label><input name='region' required maxlength='150' value='{_lead_e(row['region'])}'></div>
    <div><label>Desa / Cakupan DPS</label><select name='village_id' id='village_id'>{village_options}</select><small>Untuk DPS, kosongkan jika mencakup seluruh desa. Untuk DPSS, wajib pilih desa.</small></div></div>
    <label>Alamat lengkap *</label><textarea name='address' required rows='3' maxlength='1500'>{_lead_e(row['address'])}</textarea>
    <div class='grid'><div><label>Foto profil saat ini</label>{f"<img src='{_lead_e(photo)}'>" if photo else 'Belum ada'}<label>Ganti foto profil (opsional)</label><input type='file' name='profile_photo' accept='image/jpeg,image/png,image/webp'></div>
    <div><label>Foto KTP saat ini</label>{f"<img src='{_lead_e(ktp)}'>" if ktp else 'Belum ada'}<label>Ganti foto KTP (opsional)</label><input type='file' name='ktp_photo' accept='image/jpeg,image/png,image/webp'></div></div>
    <button type='submit'>Simpan Perubahan</button> <a class='btn' href='/admin/pendaftaran-anggota'>Batal</a></form></main>
    <script>const level=document.getElementById('org_level'), village=document.getElementById('village_id');function syncVillage(){{village.required=level.value==='DPSS';if(level.value==='DPSS'&&!village.value){{}}}}level.addEventListener('change',syncVillage);syncVillage();</script></body></html>"""
    return HTMLResponse(html)


@app.post("/admin/pendaftaran-anggota/{registration_id}/edit")
def admin_edit_member_registration(
    registration_id: int, full_name: str = Form(...), nik: str = Form(...), gender: str = Form(...),
    birth_place: str = Form(""), birth_date: str = Form(""), address: str = Form(...), phone: str = Form(...),
    email: str = Form(""), org_level: str = Form(...), org_position: str = Form(...), region: str = Form(...),
    village_id: str = Form(""), profile_photo: UploadFile | None = File(None), ktp_photo: UploadFile | None = File(None),
    username: str = Depends(verify_admin),
):
    full_name, nik, gender = full_name.strip(), re.sub(r"\D", "", nik), gender.strip()
    address, phone, email = address.strip(), phone.strip(), email.strip()
    org_level, org_position, region = org_level.strip().upper(), org_position.strip(), region.strip()
    if len(full_name) < 3 or len(nik) != 16 or gender not in {"Laki-laki", "Perempuan"} or not address or len(phone) < 8 or org_level not in {"DPS", "DPSS"} or len(org_position) < 2 or not region:
        raise HTTPException(status_code=422, detail="Periksa kembali seluruh data wajib.")
    if birth_date.strip():
        try: datetime.strptime(birth_date.strip(), "%Y-%m-%d")
        except ValueError as exc: raise HTTPException(status_code=422, detail="Tanggal lahir tidak valid.") from exc
    village_value = (village_id or "").strip()
    try:
        village_id_value = None if village_value in {"", "all"} else int(village_value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Pilihan desa tidak valid.") from exc
    if org_level == "DPSS" and village_id_value is None:
        raise HTTPException(status_code=422, detail="DPSS wajib memilih desa.")
    with get_db() as connection:
        row = connection.execute("SELECT * FROM organization_member_registrations WHERE id=%s", (registration_id,)).fetchone()
        if not row: raise HTTPException(status_code=404, detail="Pendaftaran tidak ditemukan.")
        duplicate = connection.execute("SELECT id FROM organization_member_registrations WHERE nik=%s AND id<>%s", (nik, registration_id)).fetchone()
        if duplicate: raise HTTPException(status_code=409, detail="NIK sudah digunakan pada pendaftaran lain.")
        if village_id_value is not None:
            valid_village = connection.execute("SELECT id FROM villages WHERE id=%s AND active=1", (village_id_value,)).fetchone()
            if not valid_village: raise HTTPException(status_code=422, detail="Desa tidak tersedia.")
    new_photo = _upload_officer_private(profile_photo, row['registration_code'], "profile") if profile_photo and profile_photo.filename else row['profile_photo_path']
    new_ktp = _upload_officer_private(ktp_photo, row['registration_code'], "ktp") if ktp_photo and ktp_photo.filename else row['ktp_path']
    with get_db() as connection:
        new_member_code = row.get('member_code') or ''
        if not row.get('linked_member_id'):
            new_village_name = ''
            if village_id_value is not None:
                new_village_row = connection.execute("SELECT village_name FROM villages WHERE id=%s", (village_id_value,)).fetchone()
                new_village_name = new_village_row['village_name'] if new_village_row else ''
            old_village_name = ''
            if row.get('village_id'):
                old_village_row = connection.execute("SELECT village_name FROM villages WHERE id=%s", (row['village_id'],)).fetchone()
                old_village_name = old_village_row['village_name'] if old_village_row else ''
            if _member_code_prefix(row['org_level'], row['region'], old_village_name) != _member_code_prefix(org_level, region, new_village_name):
                new_member_code = _allocate_member_code(connection, org_level, region, new_village_name)
        connection.execute("""UPDATE organization_member_registrations SET member_code=%s,full_name=%s,nik=%s,gender=%s,birth_place=%s,birth_date=%s,address=%s,phone=%s,email=%s,org_level=%s,org_position=%s,region=%s,village_id=%s,profile_photo_path=%s,ktp_path=%s,updated_at=NOW() WHERE id=%s""",
            (new_member_code,full_name,nik,gender,birth_place.strip(),birth_date.strip() or None,address,phone,email,org_level,org_position,region,village_id_value,new_photo,new_ktp,registration_id))
        if row['linked_member_id'] and row['status']=='approved':
            officer = connection.execute("SELECT id FROM officers WHERE member_id=%s", (row['linked_member_id'],)).fetchone()
            if officer:
                connection.execute("UPDATE officers SET member_name=%s,role=%s,region=%s,updated_at=NOW() WHERE id=%s", (full_name,org_level,region,officer['id']))
                connection.execute("UPDATE members SET member_name=%s WHERE member_id=%s", (full_name,row['linked_member_id']))
                connection.execute("""UPDATE officer_profiles SET nik=%s,phone=%s,address=%s,birth_place=%s,birth_date=%s,profile_photo_path=%s,ktp_path=%s,updated_at=NOW() WHERE officer_id=%s""", (nik,phone,address,birth_place.strip(),birth_date.strip() or None,new_photo,new_ktp,officer['id']))
    return RedirectResponse(url="/admin/pendaftaran-anggota?status=all", status_code=303)


@app.post("/admin/pendaftaran-anggota/{registration_id}/hapus")
def admin_delete_member_registration(registration_id: int, username: str = Depends(verify_admin)):
    with get_db() as connection:
        row = connection.execute("SELECT id,profile_photo_path,ktp_path,linked_member_id,status FROM organization_member_registrations WHERE id=%s", (registration_id,)).fetchone()
    if not row: raise HTTPException(status_code=404, detail="Pendaftaran tidak ditemukan.")
    # Jika sudah disetujui dan ditautkan, file yang sama mungkin sedang dipakai profil personel.
    # Karena itu hapus hanya catatan pendaftarannya, jangan hapus objek dokumen personel.
    keep_personnel_files = bool(row['linked_member_id'] and row['status'] == 'approved')
    paths = [] if keep_personnel_files else [path for path in (row['profile_photo_path'], row['ktp_path']) if path]
    if paths:
        supabase_url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
        supabase_key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
        if not supabase_url or not supabase_key:
            raise HTTPException(status_code=503, detail="Konfigurasi Supabase belum lengkap. Data tidak dihapus.")
        bucket = _officer_private_bucket()
        request = urllib.request.Request(f"{supabase_url}/storage/v1/object/{quote(bucket, safe='')}", data=json.dumps({"prefixes": paths}).encode("utf-8"), headers={"Authorization": f"Bearer {supabase_key}", "apikey": supabase_key, "Content-Type": "application/json"}, method="DELETE")
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                if response.status not in (200, 204): raise HTTPException(status_code=502, detail="Supabase tidak mengonfirmasi penghapusan dokumen. Data belum dihapus.")
        except urllib.error.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Gagal menghapus dokumen privat. Data belum dihapus.") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise HTTPException(status_code=502, detail="Penyimpanan dokumen tidak dapat dijangkau. Data belum dihapus.") from exc
    with get_db() as connection:
        connection.execute("DELETE FROM organization_member_registrations WHERE id=%s", (registration_id,))
    return RedirectResponse(url="/admin/pendaftaran-anggota?status=all", status_code=303)


@app.post("/admin/pendaftaran-anggota/{registration_id}")
def admin_review_member_registration(
    registration_id: int, new_status: str = Form(...), admin_note: str = Form(""),
    username: str = Depends(verify_admin),
):
    if new_status not in {"pending", "revision", "approved", "rejected"}:
        raise HTTPException(status_code=422, detail="Status pemeriksaan tidak valid.")
    with get_db() as connection:
        row = connection.execute("SELECT * FROM organization_member_registrations WHERE id=%s", (registration_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Pendaftaran tidak ditemukan.")
        # Jika anggota lama sudah terhubung ke akun, sinkronkan profilnya. Pendaftaran baru
        # tidak meminta admin mengetik ID; ID login ditetapkan otomatis saat akses diberikan.
        linked_member_id = (row.get("linked_member_id") or "").strip().upper()
        if linked_member_id and new_status == "approved":
            officer = connection.execute("SELECT id FROM officers WHERE member_id=%s", (linked_member_id,)).fetchone()
            if officer:
                connection.execute("""INSERT INTO officer_profiles
                    (officer_id,nik,phone,address,birth_place,birth_date,profile_photo_path,ktp_path,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW()) ON CONFLICT (officer_id) DO UPDATE SET
                    nik=EXCLUDED.nik,phone=EXCLUDED.phone,address=EXCLUDED.address,birth_place=EXCLUDED.birth_place,
                    birth_date=EXCLUDED.birth_date,
                    profile_photo_path=CASE WHEN EXCLUDED.profile_photo_path='' THEN officer_profiles.profile_photo_path ELSE EXCLUDED.profile_photo_path END,
                    ktp_path=CASE WHEN EXCLUDED.ktp_path='' THEN officer_profiles.ktp_path ELSE EXCLUDED.ktp_path END,updated_at=NOW()""",
                    (officer['id'],row['nik'],row['phone'],row['address'],row['birth_place'],row['birth_date'],row['profile_photo_path'],row['ktp_path']))
                connection.execute("UPDATE officers SET member_name=%s,role=%s,region=%s,updated_at=NOW() WHERE id=%s",
                    (row['full_name'],row['org_level'],row['region'],officer['id']))
                connection.execute("UPDATE members SET member_name=%s WHERE member_id=%s", (row['full_name'],linked_member_id))
        connection.execute("""UPDATE organization_member_registrations SET status=%s,admin_note=%s,
            updated_at=NOW(),reviewed_at=NOW() WHERE id=%s""",
            (new_status,admin_note.strip(),registration_id))
    return RedirectResponse(url="/admin/pendaftaran-anggota?status="+new_status, status_code=303)


@app.get("/pimpinan/personel", response_class=HTMLResponse)
def pimpinan_data_personel(q: str = "", username: str = Depends(verify_pimpinan_or_admin)):
    with get_db() as connection:
        rows = connection.execute("""
            SELECT r.id, r.full_name, r.registration_code, r.org_level, r.org_position,
                   r.region, r.phone, r.profile_photo_path, r.linked_member_id,
                   o.active AS account_active, v.village_name
            FROM organization_member_registrations r
            LEFT JOIN officers o ON o.member_id=r.linked_member_id
            LEFT JOIN villages v ON v.id=r.village_id
            WHERE r.status='approved'
              AND (%s='' OR r.full_name ILIKE %s OR r.nik ILIKE %s
                   OR r.phone ILIKE %s OR r.org_position ILIKE %s
                   OR r.region ILIKE %s OR COALESCE(v.village_name,'') ILIKE %s)
            ORDER BY r.org_level, r.region, r.full_name
        """, (q.strip(), f"%{q.strip()}%", f"%{q.strip()}%", f"%{q.strip()}%",
              f"%{q.strip()}%", f"%{q.strip()}%", f"%{q.strip()}%")).fetchall()
    cards = []
    for r in rows:
        photo = create_supabase_signed_url(r["profile_photo_path"], expires_in=1800, bucket=_officer_private_bucket()) if r["profile_photo_path"] else None
        avatar = f"<img class='person-photo' src='{_lead_e(photo)}' alt='Foto {_lead_e(r['full_name'])}'>" if photo else "<div class='person-photo no-photo'>Foto<br>belum ada</div>"
        territory = r["village_name"] or r["region"] or "-"
        if not r["linked_member_id"]:
            access = "<span class='status inactive'>BELUM DIBERI AKSES</span>"
        elif r["account_active"] == 1:
            access = "<span class='status active'>AKSES AKTIF</span>"
        else:
            access = "<span class='status inactive'>AKSES NONAKTIF</span>"
        cards.append(f"<article class='person-card'>{avatar}<div class='person-info'><h3>{_lead_e(r['full_name'])}</h3><div class='muted'>{_lead_e(r['org_level'])} · {_lead_e(r['org_position'])}</div><p>☎ {_lead_e(r['phone'] or 'Nomor HP belum diisi')}</p><p>⌖ {_lead_e(territory)}</p>{access}</div></article>")
    if not cards:
        cards = ["<div class='panel'>Belum ada anggota dengan pendaftaran berstatus Disetujui.</div>"]
    content = f"""<section class='panel'><form method='get'><label>Cari anggota</label><input name='q' value='{_lead_e(q)}' placeholder='Nama, nomor HP, jabatan, wilayah'><button type='submit'>Cari</button> <a class='btn light' href='/pimpinan/personel'>Reset</a></form><p class='muted'>Menampilkan {len(rows)} anggota yang pendaftarannya sudah disetujui. Status akses aplikasi ditampilkan terpisah dari status keanggotaan.</p><p><a class='btn' href='/admin/kelola-akun'>Kelola Akses Aplikasi</a></p></section><section class='people-grid'>{''.join(cards)}</section>"""
    return HTMLResponse(_lead_page("Data Anggota Terverifikasi", username, content))


def _lead_map_embed(surveys, verifications, map_id="lead-dashboard-map"):
    markers = []
    for row in surveys:
        try:
            lat, lon = float(row.get("latitude")), float(row.get("longitude"))
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
        except (TypeError, ValueError):
            continue
        markers.append({"kind":"survey","lat":lat,"lon":lon,"survey_id":row.get("survey_id"),
            "applicant":row.get("applicant_name") or "-","business":row.get("business_name") or "-",
            "village":row.get("village") or "-","officer":row.get("officer_name") or row.get("member_id") or "-",
            "date":row.get("surveyed_at") or "-","accuracy":row.get("accuracy")})
    for row in verifications:
        try:
            lat, lon = float(row.get("verification_latitude")), float(row.get("verification_longitude"))
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
        except (TypeError, ValueError):
            continue
        markers.append({"kind":"verification","lat":lat,"lon":lon,"survey_id":row.get("survey_id"),
            "applicant":row.get("applicant_name") or "-","business":row.get("business_name") or "-",
            "village":row.get("village") or "-","officer":row.get("verifier_name") or row.get("verifier_id") or "-",
            "date":row.get("verified_at") or "-","accuracy":row.get("verification_accuracy"),
            "decision":row.get("decision") or "-"})
    data = json.dumps(markers, ensure_ascii=False, default=str).replace("</", "<\\/")
    survey_count = sum(1 for m in markers if m["kind"] == "survey")
    verify_count = len(markers) - survey_count
    return f"""<section class='panel'>
      <h2 style='margin-top:0;color:#5b1018'>🗺️ Peta Sebaran GPS Survei dan Verifikasi</h2>
      <p class='muted'>Peta lokasi tersimpan. Merah = survei DPSS, emas = verifikasi DPS. Pilih titik untuk melihat detail dan akurasi GPS.</p>
      <div style='display:flex;gap:18px;flex-wrap:wrap;margin:10px 0;font-weight:700'><span>🔴 Survei DPSS: {survey_count}</span><span>🟡 Verifikasi DPS: {verify_count}</span><span>📍 Total: {len(markers)}</span><a class='btn light' href='/pimpinan/peta'>Buka Peta & Filter Lengkap</a></div>
      <div id='{map_id}' style='height:440px;min-height:320px;border:1px solid #ddd;border-radius:12px;z-index:1'></div>
      <p id='{map_id}-status' class='muted' aria-live='polite' style='margin-bottom:0'>Memuat peta…</p>
    </section>
    <link rel='stylesheet' href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css' crossorigin=''/>
    <script src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js' crossorigin=''></script>
    <script type='application/json' id='{map_id}-data'>{data}</script>
    <script>(function(){{const el=document.getElementById('{map_id}'),st=document.getElementById('{map_id}-status');const data=JSON.parse(document.getElementById('{map_id}-data').textContent||'[]');if(!window.L){{st.textContent='Peta gagal dimuat. Periksa koneksi internet browser.';return;}}const map=L.map(el).setView([0.5,101.4],8);L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}}).addTo(map);const bounds=[];const safe=v=>String(v===null||v===undefined||v===''?'-':v).replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));data.forEach(x=>{{const survey=x.kind==='survey';const m=L.circleMarker([x.lat,x.lon],{{radius:8,color:survey?'#8f1825':'#9a6b12',weight:2,fillColor:survey?'#d94a55':'#e5b94f',fillOpacity:.9}}).addTo(map);const acc=x.accuracy===null||x.accuracy===undefined?'-':safe(x.accuracy)+' m';m.bindPopup(`<b>${{survey?'SURVEI DPSS':'VERIFIKASI DPS'}}</b><br>ID: ${{safe(x.survey_id)}}<br>Pemohon: ${{safe(x.applicant)}}<br>Usaha: ${{safe(x.business)}}<br>Desa: ${{safe(x.village)}}<br>Petugas: ${{safe(x.officer)}}<br>Waktu: ${{safe(x.date)}}<br>Koordinat: ${{x.lat.toFixed(6)}}, ${{x.lon.toFixed(6)}}<br>Akurasi: ${{acc}}${{x.decision?'<br>Keputusan: '+safe(x.decision):''}}`);bounds.push([x.lat,x.lon]);}});if(bounds.length){{map.fitBounds(bounds,{{padding:[28,28],maxZoom:15}});st.textContent=data.length+' titik GPS ditampilkan. Klik penanda untuk detail.';}}else{{st.textContent='Belum ada data koordinat GPS yang dapat dipetakan.';}}setTimeout(()=>map.invalidateSize(),180);}})();</script>"""

@app.get("/pimpinan", response_class=HTMLResponse)
def pimpinan_dashboard(request: Request, username: str = Depends(verify_pimpinan_or_admin)):
    filters = _lead_filters(request)
    surveys = _lead_survey_rows(filters)
    verifications = _lead_verification_rows(filters)
    pending = max(0, len(surveys) - len(verifications))
    content = f"""<section class='metrics'><div class='metric'><span>Total survei DPSS</span><strong>{len(surveys)}</strong></div><div class='metric'><span>Sudah diverifikasi DPS</span><strong>{len(verifications)}</strong></div><div class='metric'><span>Menunggu verifikasi</span><strong>{pending}</strong></div><div class='metric'><span>Wilayah dalam hasil filter</span><strong>{len(set(str(r.get('village') or '') for r in surveys if r.get('village')))}</strong></div></section>
{_lead_map_embed(surveys, verifications)}
<section class='cards'><article class='report-card'><h2>📋 Laporan Survei DPSS</h2><p>Data awal pemohon dan usaha yang dikirim petugas desa. Foto usaha dan video survei ditampilkan pada halaman ini. Data identitas pribadi tidak dimasukkan ke laporan ekspor.</p><a class='btn' href='/pimpinan/survei'>Buka Laporan Survei</a></article><article class='report-card'><h2>🔎 Laporan Verifikasi DPS</h2><p>Hasil pemeriksaan DPS, keputusan, catatan verifikasi, rincian kebutuhan, serta media verifikasi. Laporan ini terpisah dari data survei awal.</p><a class='btn' href='/pimpinan/verifikasi'>Buka Laporan Verifikasi</a></article></section>
<section class='cards'><article class='report-card'><h2>👥 Data Personel DPS dan DPSS</h2><p>Daftar anggota terverifikasi dan status penugasan akses aplikasi. Pemberian akses hanya untuk anggota yang ditunjuk dan disetujui pimpinan/admin.</p><a class='btn' href='/pimpinan/personel'>Lihat Data Personel</a></article><article class='report-card'><h2>🗂️ Pendataan Identitas Petugas</h2><p>Identitas anggota bersumber dari pendaftaran mandiri yang telah diverifikasi. Pengaturan akun hanya untuk memberikan atau mencabut akses aplikasi.</p><a class='btn' href='/admin/kelola-akun'>Kelola Akses Aplikasi</a></article></section>
<section class='panel'><h2>Catatan akses</h2><p class='muted'>Dashboard ini hanya menampilkan rekap. Fungsi input dan pengiriman dari aplikasi HP tidak diubah. Ekspor laporan survei dan verifikasi tersedia masing-masing pada halaman laporan.</p></section>"""
    return HTMLResponse(_lead_page("Dashboard Pimpinan", username, content))



@app.get("/pimpinan/peta", response_class=HTMLResponse)
def pimpinan_peta_gps(request: Request, username: str = Depends(verify_pimpinan_or_admin)):
    filters = _lead_filters(request)
    surveys = _lead_survey_rows(filters)
    verifications = _lead_verification_rows(filters)
    markers = []
    villages = sorted({str(r.get("village") or "").strip() for r in surveys if str(r.get("village") or "").strip()}, key=str.casefold)
    for row in surveys:
        try:
            lat, lon = float(row.get("latitude")), float(row.get("longitude"))
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
        except (TypeError, ValueError):
            continue
        markers.append({
            "kind": "survey", "lat": lat, "lon": lon,
            "survey_id": row.get("survey_id"), "applicant": row.get("applicant_name") or "-",
            "business": row.get("business_name") or "-", "village": row.get("village") or "-",
            "officer": row.get("officer_name") or row.get("member_id") or "-",
            "date": row.get("surveyed_at") or "-", "accuracy": row.get("accuracy"),
        })
    for row in verifications:
        try:
            lat, lon = float(row.get("verification_latitude")), float(row.get("verification_longitude"))
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
        except (TypeError, ValueError):
            continue
        markers.append({
            "kind": "verification", "lat": lat, "lon": lon,
            "survey_id": row.get("survey_id"), "applicant": row.get("applicant_name") or "-",
            "business": row.get("business_name") or "-", "village": row.get("village") or "-",
            "officer": row.get("verifier_name") or row.get("verifier_id") or "-",
            "date": row.get("verified_at") or "-", "accuracy": row.get("verification_accuracy"),
            "decision": row.get("decision") or "-",
        })
    marker_json = json.dumps(markers, ensure_ascii=False, default=str).replace("</", "<\\/")
    village_options = "".join(f"<option value='{_lead_e(v)}' {'selected' if filters.get('wilayah') == v else ''}>{_lead_e(v)}</option>" for v in villages)
    content = f"""<section class='panel'>
      <h2 style='margin-top:0;color:#5b1018'>Peta Sebaran Lokasi Survei dan Verifikasi</h2>
      <p class='muted'>Titik merah menunjukkan GPS survei DPSS, sedangkan titik emas menunjukkan GPS verifikasi DPS. Peta menggunakan koordinat yang tersimpan di database, bukan lokasi perangkat yang sedang membuka dashboard.</p>
      <form method='get' action='/pimpinan/peta' class='filters'>
        <div><label>TANGGAL MULAI</label><input type='date' name='tanggal_mulai' value='{_lead_e(filters.get('tanggal_mulai'))}'></div>
        <div><label>TANGGAL SELESAI</label><input type='date' name='tanggal_selesai' value='{_lead_e(filters.get('tanggal_selesai'))}'></div>
        <div><label>DESA</label><select name='wilayah' style='width:100%;min-height:40px;padding:8px;border:1px solid #d7d2ca;border-radius:8px'><option value=''>Semua desa</option>{village_options}</select></div>
        <div><label>PETUGAS DPSS</label><input name='petugas' value='{_lead_e(filters.get('petugas'))}' placeholder='ID atau nama petugas'></div>
        <div class='actions' style='grid-column:1/-1'><button type='submit'>Terapkan Filter</button><a class='btn light' href='/pimpinan/peta'>Reset</a></div>
      </form>
      <div class='map-legend' style='display:flex;gap:18px;flex-wrap:wrap;margin:14px 0;font-weight:700'><span>🔴 GPS Survei DPSS: {sum(1 for m in markers if m['kind']=='survey')}</span><span>🟡 GPS Verifikasi DPS: {sum(1 for m in markers if m['kind']=='verification')}</span><span>📍 Total titik: {len(markers)}</span></div>
      <div id='lead-gps-map' style='height:560px;min-height:360px;border:1px solid #ddd;border-radius:12px;z-index:1'></div>
      <p id='lead-map-status' class='muted' aria-live='polite' style='margin-bottom:0'>Memuat peta…</p>
    </section>
    <script type='application/json' id='lead-map-data'>{marker_json}</script>
    <link rel='stylesheet' href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css' crossorigin=''/>
    <script src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js' crossorigin=''></script>
    <script>
    (function(){{
      const el=document.getElementById('lead-gps-map'), status=document.getElementById('lead-map-status');
      const data=JSON.parse(document.getElementById('lead-map-data').textContent||'[]');
      if(!window.L){{status.textContent='Pustaka peta tidak berhasil dimuat. Periksa koneksi internet browser.';return;}}
      const map=L.map(el).setView([0.5,101.4],8);
      L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}}).addTo(map);
      const bounds=[];
      data.forEach(item=>{{
        const isSurvey=item.kind==='survey';
        const marker=L.circleMarker([item.lat,item.lon],{{radius:8,color:isSurvey?'#8f1825':'#9a6b12',weight:2,fillColor:isSurvey?'#d94a55':'#e5b94f',fillOpacity:.9}}).addTo(map);
        const safe=v=>String(v===null||v===undefined||v===''?'-':v).replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));
        const acc=item.accuracy===null||item.accuracy===undefined?'-':safe(item.accuracy)+' m';
        marker.bindPopup(`<b>${{isSurvey?'SURVEI DPSS':'VERIFIKASI DPS'}}</b><br>ID Survei: ${{safe(item.survey_id)}}<br>Pemohon: ${{safe(item.applicant)}}<br>Usaha: ${{safe(item.business)}}<br>Desa: ${{safe(item.village)}}<br>Petugas: ${{safe(item.officer)}}<br>Waktu: ${{safe(item.date)}}<br>Koordinat: ${{item.lat.toFixed(6)}}, ${{item.lon.toFixed(6)}}<br>Akurasi GPS: ${{acc}}${{item.decision?'<br>Keputusan: '+safe(item.decision):''}}`);
        bounds.push([item.lat,item.lon]);
      }});
      if(bounds.length){{map.fitBounds(bounds,{{padding:[30,30],maxZoom:15}});status.textContent=`${{data.length}} titik GPS ditampilkan. Pilih penanda untuk melihat detail.`;}}
      else{{status.textContent='Belum ada koordinat GPS yang cocok dengan filter ini.';}}
      setTimeout(()=>map.invalidateSize(),150);
    }})();
    </script>"""
    return HTMLResponse(_lead_page("Peta Lokasi GPS", username, content))

def _lead_filters_form(filters, base, export_prefix):
    qs = _lead_qs(filters)
    exp = "?" + qs if qs else ""
    return f"""<section class='panel'><form method='get' action='{base}'><div class='filters'>
    <div><label>TANGGAL MULAI</label><input type='date' name='tanggal_mulai' value='{_lead_e(filters.get('tanggal_mulai'))}'></div>
    <div><label>TANGGAL SELESAI</label><input type='date' name='tanggal_selesai' value='{_lead_e(filters.get('tanggal_selesai'))}'></div>
    <div><label>WILAYAH / DESA</label><input name='wilayah' value='{_lead_e(filters.get('wilayah'))}' placeholder='Cari wilayah'></div>
    <div><label>PETUGAS DPSS (ID/NAMA)</label><input name='petugas' value='{_lead_e(filters.get('petugas'))}' placeholder='Cari petugas'></div></div>
    <div class='actions'><button type='submit'>Terapkan Filter</button><a class='btn light' href='{base}'>Reset</a><a class='btn gold' href='{export_prefix}/excel{exp}'>Ekspor Excel</a><a class='btn' target='_blank' href='{export_prefix}/pdf{exp}'>Ekspor PDF</a></div></form></section>"""


def _lead_media_html(media_rows):
    identity_bits = []
    photo_bits = []
    video_bits = []
    other_bits = []

    category_labels = {
        "applicant_ktp": "KTP Pemohon",
        "family_card": "Kartu Keluarga",
        "recipient_photo": "Foto Penerima",
        "business_photo": "Foto Usaha",
        "business_video": "Video Usaha",
    }

    for media in media_rows:
        url = media.get("signed_url")
        name = _lead_e(media.get("original_filename") or "Media")
        category = str(
            media.get("media_category") or "business_photo"
        ).lower()
        ctype = str(media.get("content_type") or "").lower()

        label = _lead_e(
            category_labels.get(
                category,
                category.replace("_", " ").title()
            )
        )

        if not url:
            other_bits.append(
                f"<div class='lead-media-unavailable'>"
                f"<small>{label}</small>"
                f"<p>{name} · Pratinjau tidak tersedia</p>"
                f"</div>"
            )
            continue

        safe_url = _lead_e(url)

        if ctype.startswith("image/"):
            # Dokumen identitas ditampilkan utuh, tidak dipotong.
            is_identity = category in (
                "applicant_ktp",
                "family_card",
            )

            css_class = (
                "lead-identity-item"
                if is_identity
                else "lead-photo-item"
            )

            image_class = (
                "lead-identity-image"
                if is_identity
                else "lead-photo-image"
            )

            html = (
                f"<div class='{css_class}'>"
                f"<div class='lead-media-label'>{label}</div>"
                f"<a href='{safe_url}' target='_blank' "
                f"rel='noopener noreferrer' "
                f"aria-label='Buka {label} ukuran penuh'>"
                f"<img class='{image_class}' "
                f"src='{safe_url}' alt='{name}' loading='lazy'>"
                f"</a>"
                f"<div class='lead-media-name'>{name}</div>"
                f"</div>"
            )

            if is_identity:
                identity_bits.append(html)
            else:
                photo_bits.append(html)

        elif ctype.startswith("video/"):
            video_bits.append(
                f"<div class='lead-video-item'>"
                f"<div class='lead-media-label'>{label}</div>"
                f"<video controls preload='metadata' "
                f"playsinline src='{safe_url}'></video>"
                f"<div class='lead-media-name'>{name}</div>"
                f"</div>"
            )

    sections = []

    if identity_bits:
        sections.append(
            "<section class='lead-media-section'>"
            "<h4 class='lead-media-heading'>"
            "Dokumen Identitas</h4>"
            "<div class='lead-identity-grid'>"
            + "".join(identity_bits) +
            "</div></section>"
        )

    if photo_bits:
        sections.append(
            "<section class='lead-media-section'>"
            "<h4 class='lead-media-heading'>"
            "Galeri Foto Usaha</h4>"
            "<div class='lead-photo-grid'>"
            + "".join(photo_bits) +
            "</div></section>"
        )

    if video_bits:
        sections.append(
            "<section class='lead-media-section'>"
            "<h4 class='lead-media-heading'>"
            "Video Survei</h4>"
            "<div class='lead-video-grid'>"
            + "".join(video_bits) +
            "</div></section>"
        )

    if other_bits:
        sections.append(
            "<section class='lead-media-section'>"
            "<div class='lead-media-unavailable-list'>"
            + "".join(other_bits) +
            "</div></section>"
        )

    if not sections:
        return (
            "<div class='lead-media-empty'>"
            "Belum ada foto atau video dokumentasi.</div>"
        )

    return "<div class='lead-media-gallery'>" + "".join(sections) + "</div>"


def _lead_report_html(rows, media_map, verification=False):
    body = []
    for r in rows:
        media = _lead_media_html(media_map.get(r["survey_id"], []))
        if not verification:
            detail = "<br>".join(_lead_e(x) for x in [r.get("business_type"), r.get("business_location"), "Catatan: " + str(r.get("notes") or "")] if x)
            body.append(f"<tr><td>{r['survey_id']}</td><td>{_lead_e(r.get('surveyed_at'))}</td><td>{_lead_e(r.get('officer_name'))}<br><small>{_lead_e(r.get('member_id'))}</small></td><td>{_lead_e(r.get('village'))}</td><td><b>{_lead_e(r.get('applicant_name'))}</b><br>{_lead_e(r.get('applicant_phone'))}<br>{_lead_e(r.get('applicant_address'))}</td><td><b>{_lead_e(r.get('business_name'))}</b><br>{detail}</td><td>{_lead_e(r.get('capital_amount'))}<br><small>Omzet: {_lead_e(r.get('turnover_amount'))}</small></td><td>{media}</td></tr>")
        else:
            needs = r.get("needs") or []
            if isinstance(needs, str):
                try: needs = json.loads(needs)
                except ValueError: needs = []
            needs_text = "<br>".join(_lead_e(f"{n.get('item_name','Item')}: {n.get('quantity',0)} {n.get('unit','unit')} × Rp {n.get('unit_price',0)}") for n in needs if isinstance(n, dict)) or "-"
            body.append(f"<tr><td>{r['survey_id']}</td><td>{_lead_e(r.get('verified_at'))}</td><td>{_lead_e(r.get('verifier_name'))}<br><small>{_lead_e(r.get('verifier_id'))}</small></td><td>{_lead_e(r.get('verifier_region') or r.get('village'))}</td><td><b>{_lead_e(r.get('applicant_name'))}</b><br>{_lead_e(r.get('business_name'))}</td><td><b>{_lead_e(r.get('decision'))}</b><br>Kondisi: {_lead_e(r.get('business_condition'))}<br>Aktivitas: {_lead_e(r.get('actual_business_activity'))}<br>Kesesuaian: {_lead_e(r.get('data_match'))}<br>Catatan: {_lead_e(r.get('verification_notes'))}<br>Alasan: {_lead_e(r.get('rejection_reason'))}</td><td>{needs_text}<br><b>Total: Rp {_lead_e(r.get('total_needs_amount'))}</b></td><td>{media}</td></tr>")
    if not body:
        body = ["<tr><td colspan='8' style='text-align:center'>Tidak ada data yang sesuai filter.</td></tr>"]
    if verification:
        heading = "Laporan Verifikasi DPS"
        headers = "<th>ID Survei</th><th>Waktu Verifikasi</th><th>Verifikator</th><th>Wilayah DPS</th><th>Pemohon / Usaha</th><th>Hasil Pemeriksaan</th><th>Rincian Kebutuhan</th><th>Foto Verifikasi</th>"
    else:
        heading = "Laporan Survei DPSS"
        headers = "<th>ID</th><th>Waktu Survei</th><th>Petugas DPSS</th><th>Desa</th><th>Pemohon</th><th>Informasi Usaha</th><th>Modal / Omzet Awal</th><th>KTP / Foto / Video Survei</th>"
    return f"<section class='panel'><h2>{heading} · {len(rows)} data</h2><div class='tablewrap'><table><thead><tr>{headers}</tr></thead><tbody>{''.join(body)}</tbody></table></div></section>"


@app.get("/pimpinan/survei", response_class=HTMLResponse)
def pimpinan_laporan_survei(request: Request, username: str = Depends(verify_pimpinan_or_admin)):
    filters = _lead_filters(request)
    rows = _lead_survey_rows(filters)
    # Halaman dashboard survei menampilkan KTP bersama foto/video survei.
    # Endpoint ekspor tidak memanggil include_identity=True, sehingga KTP tetap privat.
    media = _lead_media([r["survey_id"] for r in rows], verification=False, include_identity=True)
    content = _lead_filters_form(filters, "/pimpinan/survei", "/pimpinan/survei/export") + _lead_report_html(rows, media, False)
    return HTMLResponse(_lead_page("Laporan Survei DPSS", username, content))


@app.get("/pimpinan/verifikasi", response_class=HTMLResponse)
def pimpinan_laporan_verifikasi(request: Request, username: str = Depends(verify_pimpinan_or_admin)):
    filters = _lead_filters(request)
    rows = _lead_verification_rows(filters)
    media = _lead_media([r["survey_id"] for r in rows], verification=True)
    content = _lead_filters_form(filters, "/pimpinan/verifikasi", "/pimpinan/verifikasi/export") + _lead_report_html(rows, media, True)
    return HTMLResponse(_lead_page("Laporan Verifikasi DPS", username, content))


def _lead_export_excel(rows, verification, filename, sheet_title):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    if verification:
        columns = [("ID Survei","survey_id"),("Waktu Verifikasi","verified_at"),("ID Petugas DPSS","member_id"),("Nama Petugas DPSS","officer_name"),("Desa","village"),("Nama Pemohon","applicant_name"),("Nama Usaha","business_name"),("Keputusan DPS","decision"),("ID Verifikator","verifier_id"),("Nama Verifikator","verifier_name"),("Wilayah DPS","verifier_region"),("Kondisi Usaha","business_condition"),("Aktivitas Aktual","actual_business_activity"),("Aset","existing_assets"),("Rincian Kebutuhan","needs"),("Total Kebutuhan","total_needs_amount"),("Omzet Terverifikasi","verified_turnover"),("Laba Terverifikasi","verified_profit"),("Jumlah Pekerja","verified_worker_count"),("Kesesuaian Data","data_match"),("Perbedaan","discrepancy_notes"),("Catatan Verifikasi","verification_notes"),("Alasan Penolakan","rejection_reason")]
    else:
        columns = [("ID Survei","survey_id"),("Waktu Survei","surveyed_at"),("ID Petugas DPSS","member_id"),("Nama Petugas DPSS","officer_name"),("Desa","village"),("Nama Pemohon","applicant_name"),("Telepon","applicant_phone"),("Alamat","applicant_address"),("Nama Usaha","business_name"),("Jenis Usaha","business_type"),("Lokasi Usaha","business_location"),("Modal Awal","capital_amount"),("Omzet Awal","turnover_amount"),("Laba Awal","net_profit_amount"),("Jumlah Pekerja","worker_count"),("Jenis Bantuan","assistance_types"),("Nilai Bantuan","assistance_amount"),("Catatan Survei","notes"),("Latitude","latitude"),("Longitude","longitude")]
    wb=Workbook(); ws=wb.active; ws.title=sheet_title; ws.append([c[0] for c in columns])
    for cell in ws[1]:
        cell.font=Font(bold=True,color="FFFFFF"); cell.fill=PatternFill("solid",fgColor="5B1018"); cell.alignment=Alignment(wrap_text=True,vertical="top")
    for r in rows:
        values=[]
        for _, key in columns:
            value=r.get(key)
            if key == "needs" and not isinstance(value, str): value=json.dumps(value or [], ensure_ascii=False)
            values.append(value)
        ws.append(values)
    ws.freeze_panes="A2"; ws.auto_filter.ref=ws.dimensions
    for i,(label,_) in enumerate(columns,1): ws.column_dimensions[get_column_letter(i)].width=min(max(len(label)+3,14),34)
    for row in ws.iter_rows(min_row=2):
        for cell in row: cell.alignment=Alignment(wrap_text=True,vertical="top")
    out=BytesIO(); wb.save(out); out.seek(0)
    return StreamingResponse(out,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",headers={"Content-Disposition":f"attachment; filename={filename}"})


def _lead_export_pdf(rows, verification, filename, title):
    from xml.sax.saxutils import escape as xml_escape
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    if verification:
        keys=[("ID","survey_id"),("Waktu verifikasi","verified_at"),("Pemohon / usaha","applicant_name"),("Keputusan","decision"),("Verifikator","verifier_name"),("Kondisi","business_condition"),("Catatan","verification_notes"),("Total kebutuhan","total_needs_amount")]
    else:
        keys=[("ID","survey_id"),("Waktu survei","surveyed_at"),("Petugas","officer_name"),("Desa","village"),("Pemohon","applicant_name"),("Usaha","business_name"),("Modal awal","capital_amount"),("Omzet awal","turnover_amount")]
    out=BytesIO(); doc=SimpleDocTemplate(out,pagesize=landscape(A4),rightMargin=20,leftMargin=20,topMargin=22,bottomMargin=22,title=title)
    styles=getSampleStyleSheet(); styles.add(ParagraphStyle(name="LeadCell",parent=styles["BodyText"],fontSize=7,leading=8)); styles.add(ParagraphStyle(name="LeadHead",parent=styles["BodyText"],fontName="Helvetica-Bold",fontSize=7,textColor=colors.white,leading=8))
    story=[Paragraph(xml_escape(title),styles["Title"]),Spacer(1,8),Paragraph(f"Jumlah data: {len(rows)}. Foto dan video tidak disematkan di PDF; buka dashboard untuk melihat media.",styles["Normal"]),Spacer(1,10)]
    data=[[Paragraph(xml_escape(label),styles["LeadHead"]) for label,_ in keys]]
    for r in rows:
        vals=[]
        for _,key in keys:
            value=r.get(key)
            if key=="applicant_name": value=(str(value or "")+"\n"+str(r.get("applicant_phone") or "")).strip()
            if key=="business_name": value=(str(value or "")+"\n"+str(r.get("business_type") or "")).strip()
            vals.append(Paragraph(xml_escape("" if value is None else str(value)).replace("\n","<br/>"),styles["LeadCell"]))
        data.append(vals)
    widths=[30,75,90,65,100,100,65,65] if not verification else [30,75,100,75,90,100,150,75]
    table=Table(data,colWidths=widths,repeatRows=1,hAlign="LEFT")
    table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#5B1018")),("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#C9C3BB")),("VALIGN",(0,0),(-1,-1),"TOP"),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F8F4EC")]),("LEFTPADDING",(0,0),(-1,-1),4),("RIGHTPADDING",(0,0),(-1,-1),4)]))
    story.append(table); doc.build(story); out.seek(0)
    return StreamingResponse(out,media_type="application/pdf",headers={"Content-Disposition":f"attachment; filename={filename}"})


@app.get("/pimpinan/survei/export/excel")
def pimpinan_survei_excel(request: Request, username: str = Depends(verify_pimpinan_or_admin)):
    return _lead_export_excel(_lead_survey_rows(_lead_filters(request)),False,"laporan_survei_dpss.xlsx","Survei DPSS")

@app.get("/pimpinan/survei/export/pdf")
def pimpinan_survei_pdf(request: Request, username: str = Depends(verify_pimpinan_or_admin)):
    return _lead_export_pdf(_lead_survey_rows(_lead_filters(request)),False,"laporan_survei_dpss.pdf","Laporan Survei DPSS")

@app.get("/pimpinan/verifikasi/export/excel")
def pimpinan_verifikasi_excel(request: Request, username: str = Depends(verify_pimpinan_or_admin)):
    return _lead_export_excel(_lead_verification_rows(_lead_filters(request)),True,"laporan_verifikasi_dps.xlsx","Verifikasi DPS")

@app.get("/pimpinan/verifikasi/export/pdf")
def pimpinan_verifikasi_pdf(request: Request, username: str = Depends(verify_pimpinan_or_admin)):
    return _lead_export_pdf(_lead_verification_rows(_lead_filters(request)),True,"laporan_verifikasi_dps.pdf","Laporan Verifikasi DPS")

# =========================================================
# MENJALANKAN SERVER SECARA LOKAL
# =========================================================

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )
