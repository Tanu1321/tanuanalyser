import sys
import os

# Add root directory to path so Netlify functions can import main.py
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app
from mangum import Mangum

# Netlify AWS Lambda wrapper handler for ASGI applications
handler = Mangum(app)
