#!/usr/bin/env python3
"""
Setup script for creating the database schema.
Creates all tables according to the optimized data model.
"""

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
import os
from dotenv import load_dotenv

load_dotenv()

def get_db_connection():
    """Get database connection from environment variables."""
    db_user = os.getenv('DB_USER')
    if not db_user or db_user == 'postgres':
        # Use system user as default for Homebrew PostgreSQL
        db_user = os.getenv('USER', 'user')
    
    return psycopg2.connect(
        host=os.getenv('DB_HOST', 'localhost'),
        port=os.getenv('DB_PORT', '5432'),
        database=os.getenv('DB_NAME', 'deeplog'),
        user=db_user,
        password=os.getenv('DB_PASSWORD', '')
    )

def create_database():
    """Create the database if it doesn't exist."""
    db_user = os.getenv('DB_USER')
    if not db_user or db_user == 'postgres':
        # Use system user as default for Homebrew PostgreSQL
        db_user = os.getenv('USER', 'user')
    
    conn = psycopg2.connect(
        host=os.getenv('DB_HOST', 'localhost'),
        port=os.getenv('DB_PORT', '5432'),
        database='postgres',  # Connect to default database
        user=db_user,
        password=os.getenv('DB_PASSWORD', '')
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cursor = conn.cursor()
    
    db_name = os.getenv('DB_NAME', 'deeplog')
    cursor.execute(f"SELECT 1 FROM pg_database WHERE datname = '{db_name}'")
    exists = cursor.fetchone()
    
    if not exists:
        cursor.execute(f'CREATE DATABASE {db_name}')
        print(f"Database '{db_name}' created.")
    else:
        print(f"Database '{db_name}' already exists.")
    
    cursor.close()
    conn.close()

def setup_schema():
    """Create all database tables according to the optimized schema."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Enable TimescaleDB extension
        print("Enabling TimescaleDB extension...")
        cursor.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")
        conn.commit()
        print("✓ TimescaleDB extension enabled")
        
        # 1. Create log_files table
        print("Creating log_files table...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS log_files (
                id SERIAL PRIMARY KEY,
                filename VARCHAR(500) NOT NULL,
                file_path TEXT NOT NULL,
                vehicle_type VARCHAR(10) NOT NULL CHECK (vehicle_type IN ('USV', 'AUV')),
                vehicle_id VARCHAR(50),
                file_size_bytes BIGINT,
                row_count INTEGER,
                import_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                import_completed_at TIMESTAMPTZ,
                import_status VARCHAR(20) DEFAULT 'pending' CHECK (import_status IN ('pending', 'importing', 'completed', 'failed')),
                error_message TEXT,
                first_timestamp TIMESTAMPTZ,
                last_timestamp TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_log_files_vehicle_type ON log_files(vehicle_type);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_log_files_import_status ON log_files(import_status);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_log_files_timestamp_range ON log_files(first_timestamp, last_timestamp);
        """)
        conn.commit()
        print("✓ log_files table created")
        
        # 2. Create data_type_catalog table (MUST be before log_entries)
        print("Creating data_type_catalog table...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS data_type_catalog (
                id SERIAL PRIMARY KEY,
                data_type VARCHAR(100) NOT NULL UNIQUE,
                vehicle_type VARCHAR(10) NOT NULL CHECK (vehicle_type IN ('USV', 'AUV', 'BOTH')),
                category VARCHAR(50),
                unit VARCHAR(20),
                description TEXT,
                min_value DOUBLE PRECISION,
                max_value DOUBLE PRECISION,
                is_numeric BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_data_type_catalog_vehicle_type ON data_type_catalog(vehicle_type);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_data_type_catalog_category ON data_type_catalog(category);
        """)
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_data_type_catalog_name_unique ON data_type_catalog(data_type);
        """)
        conn.commit()
        print("✓ data_type_catalog table created")
        
        # 3. Create log_entries table (optimized with data_type_id)
        print("Creating log_entries table...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS log_entries (
                time TIMESTAMPTZ NOT NULL,
                vehicle_type VARCHAR(10) NOT NULL CHECK (vehicle_type IN ('USV', 'AUV')),
                vehicle_id VARCHAR(50),
                data_type_id INTEGER NOT NULL REFERENCES data_type_catalog(id) ON DELETE RESTRICT,
                value DOUBLE PRECISION NOT NULL,
                log_file_id INTEGER NOT NULL REFERENCES log_files(id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        conn.commit()
        print("✓ log_entries table created")
        
        # Convert to TimescaleDB hypertable
        print("Converting log_entries to TimescaleDB hypertable...")
        cursor.execute("""
            SELECT create_hypertable('log_entries', 'time', 
                chunk_time_interval => INTERVAL '1 day',
                if_not_exists => TRUE
            );
        """)
        conn.commit()
        print("✓ log_entries converted to hypertable")
        
        # Create indexes for log_entries
        print("Creating indexes for log_entries...")
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_log_entries_vehicle_type_time ON log_entries(vehicle_type, time DESC);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_log_entries_data_type_id_time ON log_entries(data_type_id, time DESC);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_log_entries_vehicle_data_time ON log_entries(vehicle_type, data_type_id, time DESC);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_log_entries_log_file_id ON log_entries(log_file_id);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_log_entries_query_optimized ON log_entries(vehicle_type, data_type_id, time DESC);
        """)
        conn.commit()
        print("✓ Indexes created for log_entries")
        
        # 4. Create settings_files table
        print("Creating settings_files table...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings_files (
                id SERIAL PRIMARY KEY,
                filename VARCHAR(500) NOT NULL,
                file_path TEXT NOT NULL,
                vehicle_type VARCHAR(10) NOT NULL CHECK (vehicle_type IN ('USV', 'AUV')),
                vehicle_id VARCHAR(50),
                file_size_bytes BIGINT,
                setting_count INTEGER,
                import_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                import_completed_at TIMESTAMPTZ,
                import_status VARCHAR(20) DEFAULT 'pending' CHECK (import_status IN ('pending', 'importing', 'completed', 'failed')),
                error_message TEXT,
                git_branch VARCHAR(100),
                git_hash VARCHAR(40),
                format_version VARCHAR(20),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(filename, vehicle_type, vehicle_id)
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_settings_files_vehicle_type ON settings_files(vehicle_type);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_settings_files_vehicle_id ON settings_files(vehicle_id);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_settings_files_import_status ON settings_files(import_status);
        """)
        conn.commit()
        print("✓ settings_files table created")
        
        # 5. Create vehicle_settings table
        print("Creating vehicle_settings table...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS vehicle_settings (
                id SERIAL PRIMARY KEY,
                settings_file_id INTEGER NOT NULL REFERENCES settings_files(id) ON DELETE CASCADE,
                setting_name VARCHAR(200) NOT NULL,
                setting_value TEXT NOT NULL,
                value_type VARCHAR(20),
                category VARCHAR(50),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(settings_file_id, setting_name)
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_vehicle_settings_file_id ON vehicle_settings(settings_file_id);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_vehicle_settings_name ON vehicle_settings(setting_name);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_vehicle_settings_category ON vehicle_settings(category);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_vehicle_settings_name_category ON vehicle_settings(setting_name, category);
        """)
        conn.commit()
        print("✓ vehicle_settings table created")
        
        # Set up compression policy (optional - requires columnstore to be enabled)
        # For now, we'll skip compression policy as it requires additional configuration
        # Uncomment and configure if needed:
        # print("Setting up compression policy...")
        # cursor.execute("ALTER TABLE log_entries SET (timescaledb.compress = true);")
        # cursor.execute("""
        #     SELECT add_compression_policy('log_entries', INTERVAL '7 days', if_not_exists => TRUE);
        # """)
        # conn.commit()
        # print("✓ Compression policy configured")
        print("⚠️  Compression policy skipped (can be enabled later if needed)")
        
        print("\n✅ Database schema setup completed successfully!")
        
    except Exception as e:
        conn.rollback()
        print(f"\n❌ Error setting up schema: {e}")
        raise
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    print("Setting up database...")
    create_database()
    setup_schema()

