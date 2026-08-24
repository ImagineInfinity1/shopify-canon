#!/usr/bin/env python
"""
Create a staff user account from the command line.

Usage:
    python create_user.py --username admin --email admin@example.com --password "strongpassword"

This script is used to seed user accounts when public registration is disabled.
It requires the app's database to be accessible (same DATABASE_URL as the app).
"""

import argparse
import os
import sys
import uuid

# Load .env so DATABASE_URL etc. are available
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    load_dotenv(_env_path, override=True)
except ImportError:
    pass

from app import app
from extensions import db
from models import User


def create_user(username: str, email: str, password: str):
    """Create a new user in the database."""
    with app.app_context():
        # Check for existing user
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            print(f"Error: Username '{username}' already exists.")
            sys.exit(1)

        existing_email = User.query.filter_by(email=email.lower()).first()
        if existing_email:
            print(f"Error: Email '{email}' is already registered.")
            sys.exit(1)

        # Create the user
        user_id = str(uuid.uuid4())[:8]
        user = User(id=user_id, username=username, email=email.lower(), password_hash='')
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        print(f"User created successfully:")
        print(f"  ID:       {user.id}")
        print(f"  Username: {user.username}")
        print(f"  Email:    {user.email}")


def main():
    parser = argparse.ArgumentParser(description="Create a staff user account")
    parser.add_argument("--username", required=True, help="Username for the new account")
    parser.add_argument("--email", required=True, help="Email address for the new account")
    parser.add_argument("--password", required=True, help="Password for the new account")
    args = parser.parse_args()

    if len(args.password) < 6:
        print("Error: Password must be at least 6 characters.")
        sys.exit(1)

    create_user(args.username, args.email, args.password)


if __name__ == "__main__":
    main()
