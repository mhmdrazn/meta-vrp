# Entry point untuk Vercel Python runtime.
# Vercel mendeteksi variabel ASGI `app` di file ini secara otomatis.
from backend.app import app  # noqa: F401
