# database.py
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import NullPool

# Path eksplisit (bukan auto-detect) — auto-detect python-dotenv tidak reliable
# saat dijalankan lewat `uvicorn --reload` di Windows (subprocess reloader-nya
# mengubah frame pemanggil sehingga pencarian .env jatuh ke CWD, bukan folder ini).
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("Error: DATABASE_URL tidak ditemukan. Cek file .env kamu.")

url = make_url(DATABASE_URL)

# Supabase (dan Postgres remote lain) mewajibkan TLS; localhost/docker tidak perlu.
connect_args = {}
if url.get_backend_name() == "postgresql" and url.host not in (
    None,
    "localhost",
    "127.0.0.1",
):
    connect_args["sslmode"] = os.getenv("DB_SSLMODE", "require")

# Saat DATABASE_URL mengarah ke Supabase connection pooler (pgbouncer, port 6543,
# host mengandung "pooler.supabase.com"), matikan pooling di sisi SQLAlchemy —
# pgbouncer sudah menangani pooling-nya. Ini juga pola yang tepat untuk deployment
# serverless (mis. Vercel) di mana tiap invocation idealnya membuka koneksi singkat.
_is_pgbouncer = "pooler.supabase.com" in (url.host or "") or url.port == 6543
USE_NULL_POOL = os.getenv("DB_USE_NULL_POOL", "1" if _is_pgbouncer else "0") == "1"

engine_kwargs = {"pool_pre_ping": True, "connect_args": connect_args}
if USE_NULL_POOL:
    engine_kwargs["poolclass"] = NullPool
else:
    engine_kwargs["pool_size"] = int(os.getenv("DB_POOL_SIZE", "5"))
    engine_kwargs["max_overflow"] = int(os.getenv("DB_MAX_OVERFLOW", "5"))

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
