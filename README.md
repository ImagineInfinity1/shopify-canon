# Listing Cannon for Shopify

Listing Cannon is a Windows desktop and web application for preparing artwork listings, generating listing content, creating mockups, and publishing products to Shopify.

## Install the Windows app

Download and run [`release/ListingCannonSetup.exe`](release/ListingCannonSetup.exe). The installer includes the complete packaged application; Python is not required to run it.

Windows may show a SmartScreen warning because the installer is not code-signed. Review the publisher and file before choosing **More info → Run anyway**.

## Development setup

1. Install Python 3.11.
2. Copy `.env.example` to `.env` and enter the required Gemini and Shopify credentials.
3. Install dependencies with `pip install -r requirements.txt`.
4. Start the application with `python run_local.py`.

See [`DEPLOY.md`](DEPLOY.md) for Render deployment and Shopify OAuth setup.

## Build the desktop app

Run `build_desktop_app.ps1` from PowerShell. The build uses PyInstaller and writes the packaged application to `dist`.

The Inno Setup definition is available at `installer/ListingCannon.iss` for rebuilding `ListingCannonSetup.exe`.
