import os
import sys

# Ensure app root is on sys.path
APP_ROOT = os.path.dirname(__file__)
sys.path.insert(0, APP_ROOT)

# Production-safe envs (optional)
os.environ.setdefault("FLASK_ENV", "production")
# PORT is ignored by Passenger WSGI, but harmless to keep
os.environ.setdefault("PORT", "8080")

# Expose WSGI application for Passenger
from app import app as application
