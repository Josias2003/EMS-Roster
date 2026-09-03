"""
Vercel's Python runtime looks for an ASGI-compatible `app` importable from
a file under api/ — this just re-exports the real FastAPI app so the same
code that runs locally (uvicorn app.main:app) also runs as Vercel's function.
"""
from app.main import app
