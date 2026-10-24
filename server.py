from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import secrets
import os
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

# Folder dan database
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "survey.db"

# KUNCI AKSES SEMENTARA
# Kunci ini juga akan dipasang pada aplikasi Flutter.
API_TOKEN = os.environ.get("API_TOKEN", "")

api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
)

app = FastAPI(
    title="Survei GPS API",
    description="Server pusat aplikasi Survei GPS",
    version="1.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def verify_api_key(api_key: str = Depends(api_key_header)):
    if not api_key or not secrets.compare_digest(api_key, API_TOKEN):
        raise HTTPException(
            status_code=401,
            detail="Kunci akses tidak valid",
        )
    return True


def get_db():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    with get_db() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id TEXT UNIQUE NOT NULL,
                member_name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS surveys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id TEXT NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                accuracy REAL,
                notes TEXT NOT NULL DEFAULT '',
                surveyed_at TEXT NOT NULL,
                received_at TEXT NOT NULL
            )
        """)


class MemberCreate(BaseModel):
    member_id: str = Field(min_length=1, max_length=50)
    member_name: str = Field(min_length=1, max_length=100)


class SurveyCreate(BaseModel):
    member_id: str = Field(min_length=1, max_length=50)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)
    notes: str = Field(default="", max_length=5000)
    surveyed_at: datetime


initialize_database()


@app.get("/")
def home():
    return {
        "server": "Survei GPS",
        "status": "online",
        "message": "Server berjalan dengan baik",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


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
                INSERT INTO members
                    (member_id, member_name, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    member_id,
                    member_name,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    except sqlite3.IntegrityError:
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
def list_members(_: bool = Depends(verify_api_key)):
    with get_db() as connection:
        rows = connection.execute(
            """
            SELECT member_id, member_name, active, created_at
            FROM members
            ORDER BY member_id
            """
        ).fetchall()

    return [dict(row) for row in rows]


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
            WHERE member_id = ? AND active = 1
            """,
            (member_id,),
        ).fetchone()

        if member is None:
            raise HTTPException(
                status_code=403,
                detail="ID anggota tidak terdaftar atau tidak aktif",
            )

        cursor = connection.execute(
            """
            INSERT INTO surveys (
                member_id, latitude, longitude, accuracy,
                notes, surveyed_at, received_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
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
        )

        survey_id = cursor.lastrowid

    return {
        "message": "Data survei berhasil disimpan",
        "survey_id": survey_id,
        "member_id": member_id,
    }


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
                WHERE member_id = ?
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


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
    )

