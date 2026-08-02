# Entry point untuk Vercel Python runtime (monorepo: frontend + backend satu project).
# Vercel mewajibkan file function berada di top-level "api/" folder, karena itu
# file ini tidak bisa diletakkan di "backend/api/index.py" (nested).
#
# Rewrite di vercel.json meneruskan request "/api/*" ke sini dengan path ASLI
# (termasuk prefix "/api") tetap terlihat oleh function ini — bukan di-strip.
# Rute asli di backend.app (mis. "/health", "/optimize", "/groups") tidak pakai
# prefix "/api", jadi kita mount app tsb di bawah "/api" di sini saja, tanpa
# perlu mengubah route manapun di backend.app / routers/.
from fastapi import FastAPI

from backend.app import app as backend_app

app = FastAPI()
app.mount("/api", backend_app)
