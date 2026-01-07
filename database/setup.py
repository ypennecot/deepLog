#!/usr/bin/env python3
"""
Setup script for creating the database schema.
Creates all tables according to the optimized data model.
"""

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
import os
import platform
from dotenv import load_dotenv

load_dotenv()

def get_db_connection():
    """Get database connection from environment variables."""
    db_user = os.getenv('DB_USER')
    # Platform-specific default: Mac (Homebrew) uses system user, PC uses 'postgres'
    # If DB_USER is not set, or if it's set to 'postgres' on Mac, use platform-specific default
    if not db_user or (db_user == 'postgres' and platform.system() == 'Darwin'):
        system_name = platform.system()
        if system_name == 'Darwin':  # macOS
            db_user = os.getenv('USER', 'user')
        else:  # Windows, Linux, etc.
            db_user = 'postgres'
    
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
    # Platform-specific default: Mac (Homebrew) uses system user, PC uses 'postgres'
    # If DB_USER is not set, or if it's set to 'postgres' on Mac, use platform-specific default
    if not db_user or (db_user == 'postgres' and platform.system() == 'Darwin'):
        system_name = platform.system()
        if system_name == 'Darwin':  # macOS
            db_user = os.getenv('USER', 'user')
        else:  # Windows, Linux, etc.
            db_user = 'postgres'
    
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
    
    timescaledb_available = False
    try:
        # Enable TimescaleDB extension (optional)
        print("Enabling TimescaleDB extension...")
        try:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")
            conn.commit()
            print("[OK] TimescaleDB extension enabled")
            timescaledb_available = True
        except psycopg2.errors.FeatureNotSupported:
            conn.rollback()
            print("[WARNING] TimescaleDB extension not available. Continuing without it.")
            print("  To install TimescaleDB, see: https://docs.timescale.com/install/latest/self-hosted/")
        except Exception as e:
            conn.rollback()
            print(f"[WARNING] Could not enable TimescaleDB: {e}")
            print("  Continuing without TimescaleDB extension.")
        
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
        # Add unique constraint to prevent duplicate imports
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_log_files_unique_filename_vehicle 
            ON log_files(filename, vehicle_type, vehicle_id);
        """)
        conn.commit()
        print("[OK] log_files table created")
        
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
        print("[OK] data_type_catalog table created")
        
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
        print("[OK] log_entries table created")
        
        # Convert to TimescaleDB hypertable (only if TimescaleDB is available)
        if timescaledb_available:
            print("Converting log_entries to TimescaleDB hypertable...")
            try:
                cursor.execute("""
                    SELECT create_hypertable('log_entries', 'time', 
                        chunk_time_interval => INTERVAL '1 day',
                        if_not_exists => TRUE
                    );
                """)
                conn.commit()
                print("[OK] log_entries converted to hypertable")
            except Exception as e:
                conn.rollback()
                print(f"[WARNING] Could not create hypertable: {e}")
                print("  Continuing with regular table (no TimescaleDB features).")
        else:
            print("[INFO] Skipping hypertable conversion (TimescaleDB not available)")
        
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
        print("[OK] Indexes created for log_entries")
        
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
        print("[OK] settings_files table created")
        
        # 5. Create vehicle_settings table
        print("Creating vehicle_settings table...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS vehicle_settings (
                id SERIAL PRIMARY KEY,
                settings_file_id INTEGER NOT NULL REFERENCES settings_files(id) ON DELETE CASCADE,
                setting_name VARCHAR(500) NOT NULL,
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
        print("[OK] vehicle_settings table created")
        
        # 6. Create usbl_decoded_messages table
        print("Creating usbl_decoded_messages table...")
        # Create usbl_raw_data table to store raw CSV lines before decoding
        print("Creating usbl_raw_data table...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS usbl_raw_data (
                id SERIAL PRIMARY KEY,
                log_file_id INTEGER NOT NULL REFERENCES log_files(id) ON DELETE CASCADE,
                timestamp TIMESTAMPTZ NOT NULL,
                direction VARCHAR(10) NOT NULL CHECK (direction IN ('SENT', 'RECEIVED')),
                data_raw TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_usbl_raw_data_log_file_id ON usbl_raw_data(log_file_id);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_usbl_raw_data_timestamp ON usbl_raw_data(timestamp DESC);
        """)
        conn.commit()
        print("[OK] usbl_raw_data table created")
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS usbl_decoded_messages (
                id SERIAL PRIMARY KEY,
                log_file_id INTEGER NOT NULL REFERENCES log_files(id) ON DELETE CASCADE,
                timestamp TIMESTAMPTZ NOT NULL,
                direction VARCHAR(10) NOT NULL CHECK (direction IN ('SENT', 'RECEIVED')),
                message_id INTEGER,
                message_name VARCHAR(100),
                message_type VARCHAR(20),
                version INTEGER,
                device_address INTEGER,
                payload_decoded TEXT,
                payload_raw TEXT,
                length INTEGER,
                -- Extracted values from ID_USBL_SOLUTION messages
                distance DOUBLE PRECISION,
                bearing DOUBLE PRECISION,
                elevation DOUBLE PRECISION,
                snr DOUBLE PRECISION,
                device_id INTEGER,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_usbl_decoded_log_file_id ON usbl_decoded_messages(log_file_id);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_usbl_decoded_timestamp ON usbl_decoded_messages(timestamp);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_usbl_decoded_message_id ON usbl_decoded_messages(message_id);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_usbl_decoded_direction ON usbl_decoded_messages(direction);
        """)
        conn.commit()
        print("[OK] usbl_decoded_messages table created")
        
        # Create videos table
        print("Creating videos table...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id SERIAL PRIMARY KEY,
                filename VARCHAR(500) NOT NULL,
                file_path TEXT NOT NULL,
                start_time TIMESTAMPTZ,
                time_offset_seconds DOUBLE PRECISION DEFAULT 0.0,
                effective_start_time TIMESTAMPTZ,
                duration_seconds DOUBLE PRECISION,
                thumbnail_status VARCHAR(20) DEFAULT 'pending' CHECK (thumbnail_status IN ('pending', 'processing', 'completed', 'failed')),
                run_id INTEGER REFERENCES log_files(id) ON DELETE SET NULL,
                camera_id VARCHAR(50),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        
        # Create function to calculate effective_start_time
        cursor.execute("""
            CREATE OR REPLACE FUNCTION calculate_effective_start_time()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW.start_time IS NOT NULL AND NEW.time_offset_seconds IS NOT NULL THEN
                    NEW.effective_start_time := NEW.start_time + (NEW.time_offset_seconds || ' seconds')::INTERVAL;
                ELSE
                    NEW.effective_start_time := NULL;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """)
        
        # Create trigger to automatically update effective_start_time
        cursor.execute("""
            DROP TRIGGER IF EXISTS update_effective_start_time ON videos;
        """)
        cursor.execute("""
            CREATE TRIGGER update_effective_start_time
            BEFORE INSERT OR UPDATE OF start_time, time_offset_seconds ON videos
            FOR EACH ROW
            EXECUTE FUNCTION calculate_effective_start_time();
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_videos_run_id ON videos(run_id);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_videos_effective_start_time ON videos(effective_start_time);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_videos_camera_id ON videos(camera_id);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_videos_thumbnail_status ON videos(thumbnail_status);
        """)
        conn.commit()
        print("[OK] videos table created")
        
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
        print("[WARNING] Compression policy skipped (can be enabled later if needed)")
        
        print("\n[SUCCESS] Database schema setup completed successfully!")
        
    except Exception as e:
        conn.rollback()
        print(f"\n[ERROR] Error setting up schema: {e}")
        raise
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    print("Setting up database...")
    create_database()
    setup_schema()

