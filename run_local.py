#!/usr/bin/env python
"""Run the Flask app locally. Use this instead of 'python app.py' so there is a single app instance with login_manager attached."""
import os
from dotenv import load_dotenv

# Load .env so GEMINI_API_KEY, SHOPIFY_*, etc. are available
# Use explicit path so it works regardless of CWD
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
load_dotenv(_env_path, override=True)

from app import app

if __name__ == '__main__':
    print("Starting server at http://127.0.0.1:5000")
    print("Press Ctrl+C to stop")
    app.run(host='0.0.0.0', port=5000, debug=True)
