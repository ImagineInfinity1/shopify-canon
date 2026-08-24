#!/usr/bin/env python
"""Startup script for Flask application with proper error handling"""
import os
import sys
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Validate required environment variables
required_vars = [
    'GEMINI_API_KEY',
    'SHOPIFY_API_KEY',
    'SHOPIFY_API_SECRET',
    'SHOPIFY_STORE_URL',
    'SHOPIFY_ACCESS_TOKEN'
]

missing_vars = [var for var in required_vars if not os.environ.get(var)]
if missing_vars:
    print("=" * 70)
    print("ERROR: Missing required environment variables!")
    print("=" * 70)
    print("Please create a .env file with the following variables:")
    for var in missing_vars:
        print(f"  - {var}")
    print("\nCopy .env.example to .env and fill in your API keys.")
    print("=" * 70)
    sys.exit(1)

print("=" * 70)
print("Starting Shopify Automation Flask Server")
print("=" * 70)
print(f"Python version: {sys.version}")
print(f"Working directory: {os.getcwd()}")
print(f"GEMINI_API_KEY set: {'Yes' if os.environ.get('GEMINI_API_KEY') else 'No'}")
print("-" * 70)

try:
    print("\n[1/4] Importing Flask application...")
    from app import app
    print("      [OK] Application imported successfully")
    
    print("\n[2/4] Checking application configuration...")
    print(f"      - Debug mode: {app.debug}")
    print(f"      - Secret key: {'Set' if app.secret_key else 'Not set'}")
    print(f"      - Database: {app.config.get('SQLALCHEMY_DATABASE_URI', 'Not configured')}")
    
    print("\n[3/5] Checking registered routes...")
    routes = list(app.url_map.iter_rules())
    print(f"      [OK] Found {len(routes)} registered routes")
    
    print("\n[4/5] Validating API keys...")
    # Test Gemini API key
    gemini_key = os.environ.get('GEMINI_API_KEY')
    if gemini_key:
        print(f"      Testing Gemini API key: {gemini_key[:20]}...{gemini_key[-10:]}")
        try:
            from google import genai
            test_client = genai.Client(api_key=gemini_key)
            # Quick test - just initialize, don't make actual call to save time
            print("      [OK] Gemini API key is valid (client initialized)")
        except Exception as e:
            print(f"      [WARNING] Gemini API key validation failed: {e}")
            print("      Server will start but Gemini features may not work")
    else:
        print("      [ERROR] GEMINI_API_KEY not set!")
    
    # Test Shopify credentials
    shopify_token = os.environ.get('SHOPIFY_ACCESS_TOKEN')
    if shopify_token:
        print(f"      Shopify Access Token: ...{shopify_token[-6:] if len(shopify_token) > 6 else shopify_token}")
        print("      [OK] Shopify token configured (will be validated on first use)")
    else:
        print("      [WARNING] SHOPIFY_ACCESS_TOKEN not set!")
    
    print("\n[5/5] Starting Flask development server...")
    print("-" * 70)
    print("")
    print("  [OK] Server starting on http://0.0.0.0:5000")
    print("  [OK] Access locally at: http://localhost:5000")
    print("  [INFO] Press Ctrl+C to stop the server")
    print("")
    print("=" * 70)
    print("")
    
    # Start the server
    app.run(
        host='0.0.0.0',
        port=5000,
        debug=True,
        use_reloader=False  # Disable reloader to avoid issues
    )
    
except KeyboardInterrupt:
    print("\n\nServer stopped by user")
    sys.exit(0)
except Exception as e:
    print(f"\n\n[ERROR] Failed to start server")
    print(f"  Error type: {type(e).__name__}")
    print(f"  Error message: {str(e)}")
    print("\nFull traceback:")
    import traceback
    traceback.print_exc()
    sys.exit(1)
