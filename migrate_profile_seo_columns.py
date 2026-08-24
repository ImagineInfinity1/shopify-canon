#!/usr/bin/env python3
"""One-time migration: add SEO, meta description, and vendor_manual columns to user_profiles.
Run with: python migrate_profile_seo_columns.py
Safe to run multiple times (skips if columns already exist).
Supports both SQLite and PostgreSQL.
"""
import os
import sys

def run_sqlite(conn):
    import sqlite3
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(user_profiles)")
    cols = {row[1] for row in cur.fetchall()}
    added = []
    for col, stmt in [
        ('seo_title_manual', 'ALTER TABLE user_profiles ADD COLUMN seo_title_manual BOOLEAN DEFAULT 0'),
        ('meta_desc_manual', 'ALTER TABLE user_profiles ADD COLUMN meta_desc_manual BOOLEAN DEFAULT 0'),
        ('manual_seo_title', 'ALTER TABLE user_profiles ADD COLUMN manual_seo_title VARCHAR(255)'),
        ('manual_meta_desc', 'ALTER TABLE user_profiles ADD COLUMN manual_meta_desc TEXT'),
        ('condition_manual', 'ALTER TABLE user_profiles ADD COLUMN condition_manual BOOLEAN DEFAULT 0'),
        ('decoration_material_manual', 'ALTER TABLE user_profiles ADD COLUMN decoration_material_manual BOOLEAN DEFAULT 0'),
        ('artwork_frame_material_manual', 'ALTER TABLE user_profiles ADD COLUMN artwork_frame_material_manual BOOLEAN DEFAULT 0'),
        ('manual_condition', 'ALTER TABLE user_profiles ADD COLUMN manual_condition VARCHAR(100)'),
        ('manual_decoration_material', 'ALTER TABLE user_profiles ADD COLUMN manual_decoration_material VARCHAR(100)'),
        ('manual_artwork_frame_material', 'ALTER TABLE user_profiles ADD COLUMN manual_artwork_frame_material VARCHAR(100)'),
        ('vendor_manual', 'ALTER TABLE user_profiles ADD COLUMN vendor_manual BOOLEAN DEFAULT 0'),
    ]:
        if col not in cols:
            cur.execute(stmt)
            added.append(col)
    conn.commit()
    return added

def run_postgres(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'user_profiles'
    """)
    cols = {row[0] for row in cur.fetchall()}
    added = []
    for col, stmt in [
        ('seo_title_manual', 'ALTER TABLE user_profiles ADD COLUMN seo_title_manual BOOLEAN DEFAULT FALSE'),
        ('meta_desc_manual', 'ALTER TABLE user_profiles ADD COLUMN meta_desc_manual BOOLEAN DEFAULT FALSE'),
        ('manual_seo_title', 'ALTER TABLE user_profiles ADD COLUMN manual_seo_title VARCHAR(255)'),
        ('manual_meta_desc', 'ALTER TABLE user_profiles ADD COLUMN manual_meta_desc TEXT'),
        ('condition_manual', 'ALTER TABLE user_profiles ADD COLUMN condition_manual BOOLEAN DEFAULT FALSE'),
        ('decoration_material_manual', 'ALTER TABLE user_profiles ADD COLUMN decoration_material_manual BOOLEAN DEFAULT FALSE'),
        ('artwork_frame_material_manual', 'ALTER TABLE user_profiles ADD COLUMN artwork_frame_material_manual BOOLEAN DEFAULT FALSE'),
        ('manual_condition', 'ALTER TABLE user_profiles ADD COLUMN manual_condition VARCHAR(100)'),
        ('manual_decoration_material', 'ALTER TABLE user_profiles ADD COLUMN manual_decoration_material VARCHAR(100)'),
        ('manual_artwork_frame_material', 'ALTER TABLE user_profiles ADD COLUMN manual_artwork_frame_material VARCHAR(100)'),
        ('vendor_manual', 'ALTER TABLE user_profiles ADD COLUMN vendor_manual BOOLEAN DEFAULT FALSE'),
    ]:
        if col not in cols:
            cur.execute(stmt)
            added.append(col)
    conn.commit()
    return added

def main():
    database_url = os.environ.get('DATABASE_URL', 'sqlite:///instance/app.db')
    if database_url.startswith('sqlite:///'):
        database_url = database_url  # keep as-is for sqlite
    # Handle postgres:// -> postgresql:// for psycopg2
    if database_url.startswith('postgres://'):
        database_url = 'postgresql://' + database_url[len('postgres://'):]

    if 'postgresql' in database_url.lower() or ('postgres' in database_url.lower() and 'sqlite' not in database_url.lower()):
        try:
            import psycopg2
            from urllib.parse import urlparse
            parsed = urlparse(database_url)
            conn = psycopg2.connect(
                host=parsed.hostname,
                port=parsed.port or 5432,
                dbname=(parsed.path or '/').lstrip('/').split('?')[0] or 'app',
                user=parsed.username,
                password=parsed.password,
            )
            added = run_postgres(conn)
            conn.close()
        except Exception as e:
            print('PostgreSQL migration failed:', e, file=sys.stderr)
            sys.exit(1)
    else:
        import sqlite3
        db_path = database_url.replace('sqlite:///', '')
        if not os.path.isfile(db_path):
            print(f'DB not found: {db_path}', file=sys.stderr)
            sys.exit(1)
        conn = sqlite3.connect(db_path)
        added = run_sqlite(conn)
        conn.close()

    if added:
        print('Added columns:', ', '.join(added))
    else:
        print('All profile columns already exist.')

if __name__ == '__main__':
    main()
