"""
Vercel serverless entry point for HackScout's FastAPI backend.
Vercel looks for a file in the /api folder and wraps it automatically.
"""
import sys
import os
from pathlib import Path

# Add the backend/app directory to the Python path so imports work on Vercel
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.main import app  # noqa: E402  (import after sys.path change is intentional)
