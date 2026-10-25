from datetime import datetime, timezone
import os
import secrets
from fastapi import UploadFile, File, Form

import psycopg
import uvicorn

import uuid
import urllib.request
import urllib.error
from urllib.parse import quote
from fastapi import Request

from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import APIKeyHeader, HTTPBasic, HTTPBasicCredentials
from fastapi import Security
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from html import escape


# =========================================================
# KONFIGURASI
# =========================================================

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if DATABASE_URL.startswith("//"):
    DATABASE_URL = "postgresql:" + DATABASE_URL

API_TOKEN = os.environ.get("API_TOKEN", "")

api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
)

app = FastAPI(
    title="Survei GPS API",
    description="Server pusat aplikasi Survei GPS",
    version="2.1.0",
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
                received_at TEXT NOT NULL
            )
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
                uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Pastikan anggota tes tersedia dan aktif.
        # Anggota lain dan data survei tidak dihapus.
        connection.execute(
            """
            INSERT INTO members (
                member_id,
                member_name,
                active,
                created_at
            )
            VALUES (%s, %s, 1, %s)
            ON CONFLICT (member_id)
            DO UPDATE SET
                member_name = EXCLUDED.member_name,
                active = 1
            """,
            (
                "PETUGAS001",
                "Petugas Tes",
                datetime.now(timezone.utc).isoformat(),
            ),
        )


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
    _: bool = Depends(verify_api_key),
):
    member_id = survey.member_id.strip()

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
                member_id,
                latitude,
                longitude,
                accuracy,
                notes,
                surveyed_at,
                received_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                member_id,
                survey.latitude,
                survey.longitude,
                survey.accuracy,
                survey.notes,
                survey.surveyed_at.isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        ).fetchone()

        survey_id = row["id"]

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

@app.post("/surveys/{survey_id}/media")
async def upload_survey_media(
    survey_id: int,
    member_id: str = Form(...),
    file: UploadFile = File(...),
    _: bool = Depends(verify_api_key),
):
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

    max_size = 50 * 1024 * 1024
    content = await file.read(max_size + 1)
    if not content:
        raise HTTPException(status_code=400, detail="File kosong.")
    if len(content) > max_size:
        raise HTTPException(status_code=413, detail="Ukuran file maksimal 50 MB.")

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
            SELECT id, member_id
            FROM surveys
            WHERE id = %s AND member_id = %s
            """,
            (survey_id, member_id),
        ).fetchone()

    if survey is None:
        raise HTTPException(
            status_code=404,
            detail="Data survei tidak ditemukan untuk ID anggota tersebut.",
        )

    # Nama objek dibuat oleh server, bukan dari nama file pengguna.
    object_path = f"surveys/{survey_id}/{uuid.uuid4().hex}{allowed_types[content_type]}"
    storage_url = (
        f"{supabase_url}/storage/v1/object/survey-media/"
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
        with urllib.request.urlopen(request, timeout=90) as response:
            if response.status not in (200, 201):
                raise HTTPException(
                    status_code=502,
                    detail="Supabase gagal menyimpan file.",
                )
    except urllib.error.HTTPError as exc:
        # Jangan kirim secret key ke respons; catat hanya kode status.
        raise HTTPException(
            status_code=502,
            detail=f"Supabase menolak unggahan (HTTP {exc.code}). Periksa bucket dan konfigurasi.",
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
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
                file_size
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                survey_id,
                member_id,
                object_path,
                content_type,
                os.path.basename(file.filename or "file"),
                len(content),
            ),
        )

    return {
        "message": "File berhasil diunggah.",
        "survey_id": survey_id,
        "member_id": member_id,
        "file_path": object_path,
        "content_type": content_type,
        "file_size": len(content),
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
                   content_type, original_filename, file_size, uploaded_at
            FROM survey_media
            WHERE survey_id = %s
            ORDER BY id
            """,
            (survey_id,),
        ).fetchall()
    return [dict(row) for row in rows]


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


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(
    username: str = Depends(verify_admin),
):
    with get_db() as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                member_id,
                latitude,
                longitude,
                accuracy,
                notes,
                surveyed_at,
                received_at
            FROM surveys
            ORDER BY id DESC
            """
        ).fetchall()

    table_rows = ""

    for row in rows:
        lat = row["latitude"]
        lon = row["longitude"]

        map_url = f"https://www.google.com/maps?q={lat},{lon}"

        table_rows += f"""
        <tr>
            <td>{row['id']}</td>
            <td>{escape(str(row['member_id']))}</td>
            <td>{lat}</td>
            <td>{lon}</td>
            <td>{row['accuracy'] if row['accuracy'] is not None else '-'}</td>
            <td>{escape(str(row['notes'] or '-'))}</td>
            <td>{escape(str(row['surveyed_at']))}</td>
            <td><a href="{map_url}" target="_blank" rel="noopener noreferrer">Lihat Peta</a></td>
        </tr>
        """

    if not table_rows:
        table_rows = """
        <tr><td colspan="8">Belum ada data survei.</td></tr>
        """

    page = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta http-equiv="refresh" content="30">
        <title>Dashboard Survei GPS</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 24px; background: #f1f5f9; color: #1e293b; }}
            h1 {{ margin-bottom: 8px; }}
            .info {{ margin-bottom: 20px; color: #475569; }}
            .card {{ background: white; padding: 18px; border-radius: 10px; margin-bottom: 20px; }}
            .table-wrap {{ overflow-x: auto; }}
            table {{ border-collapse: collapse; width: 100%; background: white; font-size: 14px; }}
            th, td {{ border: 1px solid #e2e8f0; padding: 10px; text-align: left; white-space: nowrap; }}
            th {{ background: #e2e8f0; }}
            tr:nth-child(even) {{ background: #f8fafc; }}
            a {{ color: #0369a1; font-weight: bold; }}
        </style>
    </head>
    <body>
        <h1>Dashboard Survei GPS</h1>
        <div class="info">Login admin berhasil. Data diperbarui otomatis setiap 30 detik.</div>
        <div class="card"><strong>Total data survei:</strong> {len(rows)}</div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>ID Anggota</th>
                        <th>Latitude</th>
                        <th>Longitude</th>
                        <th>Akurasi (m)</th>
                        <th>Catatan</th>
                        <th>Waktu Survei</th>
                        <th>Peta</th>
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
# MENJALANKAN SERVER SECARA LOKAL
# =========================================================

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )
