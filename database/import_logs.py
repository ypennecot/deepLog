#!/usr/bin/env python3
"""
Import script for navigation and settings log files.
Handles different import logic for AUV (navigation + settings) and USV (navigation only).
"""

import psycopg2
import pandas as pd
import os
import re
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
import json

load_dotenv()

def get_db_connection():
    """Get database connection."""
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

def extract_vehicle_info(filename):
    """Extract vehicle type and ID from filename."""
    # Pattern: YYYY-MM-DD_HH-MM-SS_VEHICLETYPEID_...
    match = re.search(r'_([AU]SV|AUV)(\d+)_', filename)
    if match:
        vehicle_type = 'USV' if match.group(1) == 'USV' else 'AUV'
        vehicle_id = f"{vehicle_type}{match.group(2)}"
        return vehicle_type, vehicle_id
    return None, None

def get_or_create_data_type(cursor, data_type_name, vehicle_type):
    """Get or create data type in catalog, return ID."""
    # Try to get existing
    cursor.execute("SELECT id FROM data_type_catalog WHERE data_type = %s", (data_type_name,))
    result = cursor.fetchone()
    if result:
        return result[0]
    
    # Determine category from prefix
    category = None
    if data_type_name.startswith('GPS_') or data_type_name in ['Lat', 'Lon', 'Easting', 'Northing']:
        category = 'GPS'
    elif data_type_name.startswith('RAW_IMU_') or data_type_name.startswith('SCALED_IMU'):
        category = 'IMU'
    elif data_type_name.startswith('Batt'):
        category = 'Battery'
    elif data_type_name in ['Armed', 'State', 'Mode', 'Emergency']:
        category = 'State'
    elif data_type_name.startswith('RC') or data_type_name.startswith('M'):
        category = 'Controls'
    elif 'Altitude' in data_type_name or data_type_name == 'Depth':
        category = 'Altitude'
    elif 'Ping' in data_type_name:
        category = 'Acoustic'
    elif data_type_name in ['Heading', 'Pitch', 'Roll', 'Yaw']:
        category = 'Orientation'
    
    # Create new entry
    cursor.execute("""
        INSERT INTO data_type_catalog (data_type, vehicle_type, category)
        VALUES (%s, %s, %s)
        RETURNING id
    """, (data_type_name, vehicle_type, category))
    return cursor.fetchone()[0]

def import_navigation_file(file_path, conn, file_progress=None):
    """Import a navigation log file.
    
    Args:
        file_path: Path to the navigation log file
        conn: Database connection
        file_progress: Optional dict to track file import progress. Will be updated with:
            - 'bytes_total': Total file size in bytes
            - 'bytes_processed': Estimated bytes processed
    """
    filename = os.path.basename(file_path)
    vehicle_type, vehicle_id = extract_vehicle_info(filename)
    
    if not vehicle_type:
        print(f"⚠️  Could not determine vehicle type from {filename}, skipping")
        return False
    
    cursor = conn.cursor()
    
    try:
        # Check if file already exists
        cursor.execute("""
            SELECT id FROM log_files 
            WHERE filename = %s AND vehicle_type = %s AND vehicle_id = %s
            LIMIT 1
        """, (filename, vehicle_type, vehicle_id))
        existing = cursor.fetchone()
        if existing:
            print(f"⚠️  File {filename} already imported, skipping")
            return False
        
        # Create log_file entry
        file_size = os.path.getsize(file_path)
        
        # Initialize file progress tracking
        if file_progress is not None:
            file_progress['bytes_total'] = file_size
            file_progress['bytes_processed'] = 0
            file_progress['estimated_total_lines'] = None
        cursor.execute("""
            INSERT INTO log_files (filename, file_path, vehicle_type, vehicle_id, file_size_bytes, import_status, import_started_at)
            VALUES (%s, %s, %s, %s, %s, 'importing', NOW())
            RETURNING id
        """, (filename, str(file_path), vehicle_type, vehicle_id, file_size))
        log_file_id = cursor.fetchone()[0]
        conn.commit()
        
        print(f"📄 Importing {filename} ({vehicle_type} {vehicle_id})...")
        
        # Read CSV in chunks for large files
        chunk_size = 50000
        total_rows = 0
        first_timestamp = None
        last_timestamp = None
        
        # Try to read CSV, handle errors gracefully
        # Try different encodings if UTF-8 fails
        csv_reader = None
        encodings = ['utf-8', 'latin-1', 'iso-8859-1', 'cp1252']
        
        for encoding in encodings:
            try:
                csv_reader = pd.read_csv(file_path, chunksize=chunk_size, on_bad_lines='skip', 
                                        engine='python', encoding=encoding)
                if encoding != 'utf-8':
                    print(f"  ⚠️  Using {encoding} encoding (UTF-8 failed)")
                break
            except (UnicodeDecodeError, Exception) as e:
                if encoding == encodings[-1]:  # Last encoding failed
                    raise e
                continue
        
        chunk_index = 0
        for chunk in csv_reader:
            # Prepare data for bulk insert
            entries = []
            data_type_ids = {}
            
            # Estimate total lines based on first chunk if not already estimated
            if file_progress is not None and file_progress.get('estimated_total_lines') is None:
                chunk_rows = len(chunk)
                if chunk_rows > 0:
                    # Try to estimate average bytes per row by reading a sample of the file
                    # We'll use a simple heuristic: read first few lines to estimate line size
                    try:
                        with open(file_path, 'rb') as f:
                            # Read first 10KB to estimate average line size
                            sample = f.read(10240)
                            if sample:
                                sample_lines = sample.count(b'\n')
                                if sample_lines > 0:
                                    avg_bytes_per_line = len(sample) / sample_lines
                                    estimated_total_lines = max(int(file_size / avg_bytes_per_line), chunk_rows)
                                else:
                                    # Fallback: estimate based on chunk size
                                    estimated_total_lines = max(int((file_size / chunk_size) * chunk_rows), chunk_rows)
                            else:
                                estimated_total_lines = chunk_rows
                    except Exception:
                        # Fallback: estimate based on chunk size
                        estimated_total_lines = max(int((file_size / chunk_size) * chunk_rows), chunk_rows)
                    
                    file_progress['estimated_total_lines'] = estimated_total_lines
            
            for _, row in chunk.iterrows():
                try:
                    # Skip rows with invalid timestamp
                    timestamp_str = str(row['timestamp']).strip()
                    
                    # Check for invalid timezone offsets (like -23:00 which is out of range)
                    if timestamp_str.endswith('-23:00') or timestamp_str.endswith('+23:00'):
                        # Skip rows with invalid timezone
                        continue
                    
                    timestamp = pd.to_datetime(timestamp_str, errors='coerce')
                    if pd.isna(timestamp):
                        continue
                    
                    # Validate timestamp is within reasonable range
                    # Check if timestamp is in the future beyond reasonable bounds (e.g., year > 2100)
                    # or in the past before reasonable bounds (e.g., year < 2000)
                    if timestamp.year < 2000 or timestamp.year > 2100:
                        # Skip obviously invalid timestamps
                        continue
                    
                    data_type_name = str(row['data']).strip()
                    
                    # Try to convert value to float, skip if not numeric
                    try:
                        value = float(row['value'])
                    except (ValueError, TypeError):
                        # Skip non-numeric values (like 'DISARMED', 'False', 'MANUAL', etc.)
                        continue
                    
                    # Get or create data type
                    if data_type_name not in data_type_ids:
                        data_type_ids[data_type_name] = get_or_create_data_type(
                            cursor, data_type_name, vehicle_type
                        )
                    
                    entries.append((
                        timestamp,
                        vehicle_type,
                        vehicle_id,
                        data_type_ids[data_type_name],
                        value,
                        log_file_id
                    ))
                    
                    if first_timestamp is None or timestamp < first_timestamp:
                        first_timestamp = timestamp
                    if last_timestamp is None or timestamp > last_timestamp:
                        last_timestamp = timestamp
                        
                except Exception as e:
                    # Silently skip problematic rows
                    continue
            
            # Bulk insert
            if entries:
                cursor.executemany("""
                    INSERT INTO log_entries (time, vehicle_type, vehicle_id, data_type_id, value, log_file_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, entries)
                total_rows += len(entries)
                conn.commit()
            
            # Update file progress after each chunk
            if file_progress is not None:
                estimated_total_lines = file_progress.get('estimated_total_lines')
                if estimated_total_lines and estimated_total_lines > 0:
                    # Calculate progress ratio based on rows processed
                    progress_ratio = min(total_rows / estimated_total_lines, 1.0)
                    file_progress['bytes_processed'] = int(file_size * progress_ratio)
                else:
                    # Fallback: estimate based on chunk index (less accurate)
                    # Assume we're processing roughly chunk_size rows per chunk
                    estimated_chunks = max(1, int(file_size / (chunk_size * 100)))
                    progress_ratio = min((chunk_index + 1) / estimated_chunks, 1.0)
                    file_progress['bytes_processed'] = int(file_size * progress_ratio)
            
            chunk_index += 1
        
        # Update log_file with completion status
        cursor.execute("""
            UPDATE log_files
            SET import_status = 'completed',
                import_completed_at = NOW(),
                row_count = %s,
                first_timestamp = %s,
                last_timestamp = %s
            WHERE id = %s
        """, (total_rows, first_timestamp, last_timestamp, log_file_id))
        conn.commit()
        
        print(f"✅ Imported {total_rows:,} rows from {filename}")
        return True
        
    except Exception as e:
        conn.rollback()
        cursor.execute("""
            UPDATE log_files
            SET import_status = 'failed', error_message = %s
            WHERE id = %s
        """, (str(e), log_file_id))
        conn.commit()
        print(f"❌ Error importing {filename}: {e}")
        return False
    finally:
        cursor.close()

def import_settings_file(file_path, conn):
    """Import a settings file."""
    filename = os.path.basename(file_path)
    vehicle_type, vehicle_id = extract_vehicle_info(filename)
    
    if not vehicle_type:
        print(f"⚠️  Could not determine vehicle type from {filename}, skipping")
        return False
    
    cursor = conn.cursor()
    
    try:
        # Create settings_file entry
        file_size = os.path.getsize(file_path)
        cursor.execute("""
            INSERT INTO settings_files (filename, file_path, vehicle_type, vehicle_id, file_size_bytes, import_status, import_started_at)
            VALUES (%s, %s, %s, %s, %s, 'importing', NOW())
            ON CONFLICT (filename, vehicle_type, vehicle_id) DO NOTHING
            RETURNING id
        """, (filename, str(file_path), vehicle_type, vehicle_id, file_size))
        result = cursor.fetchone()
        if not result:
            print(f"⚠️  Settings file {filename} already imported, skipping")
            return False
        settings_file_id = result[0]
        conn.commit()
        
        print(f"⚙️  Importing settings {filename} ({vehicle_type} {vehicle_id})...")
        
        # Read settings CSV - handle various formats
        try:
            df = pd.read_csv(file_path, header=None, names=['setting_name', 'setting_value'], 
                           on_bad_lines='skip', engine='python')
        except Exception:
            # Try with different separator or encoding
            try:
                df = pd.read_csv(file_path, header=None, names=['setting_name', 'setting_value'],
                               sep=',', on_bad_lines='skip', engine='python', encoding='latin-1')
            except Exception as e:
                print(f"  ❌ Could not read settings file: {e}")
                raise
        
        # Extract metadata
        git_branch = None
        git_hash = None
        format_version = None
        
        settings = []
        for _, row in df.iterrows():
            name = str(row['setting_name']).strip()
            value = str(row['setting_value']).strip()
            
            # Extract metadata
            if name == 'git_branch':
                git_branch = value
            elif name == 'git_hash':
                git_hash = value
            elif name == 'FORMAT_VERSION':
                format_version = value
            
            # Determine value type and category
            value_type = 'string'
            if value.startswith('{') and value.endswith('}'):
                value_type = 'json'
            elif value.replace('.', '', 1).replace('-', '', 1).isdigit():
                value_type = 'number'
            elif value.lower() in ['true', 'false']:
                value_type = 'boolean'
            
            category = None
            if name.startswith('COMPASS_'):
                category = 'compass'
            elif name.startswith('INS_'):
                category = 'ins'
            elif name.startswith('GPS_'):
                category = 'gps'
            elif name.startswith('BATT_'):
                category = 'battery'
            elif name in ['vehicle', 'camera', 'api', 'navigation', 'usbl', 'seaker', 'follow', 'Emergency']:
                category = 'configuration'
            
            settings.append((settings_file_id, name, value, value_type, category))
        
        # Bulk insert settings
        cursor.executemany("""
            INSERT INTO vehicle_settings (settings_file_id, setting_name, setting_value, value_type, category)
            VALUES (%s, %s, %s, %s, %s)
        """, settings)
        
        # Update settings_file with metadata
        cursor.execute("""
            UPDATE settings_files
            SET import_status = 'completed',
                import_completed_at = NOW(),
                setting_count = %s,
                git_branch = %s,
                git_hash = %s,
                format_version = %s
            WHERE id = %s
        """, (len(settings), git_branch, git_hash, format_version, settings_file_id))
        conn.commit()
        
        print(f"✅ Imported {len(settings)} settings from {filename}")
        return True
        
    except Exception as e:
        conn.rollback()
        cursor.execute("""
            UPDATE settings_files
            SET import_status = 'failed', error_message = %s
            WHERE id = %s
        """, (str(e), settings_file_id))
        conn.commit()
        print(f"❌ Error importing {filename}: {e}")
        return False
    finally:
        cursor.close()

def import_directory(directory_path):
    """Import all navigation and settings files from a directory."""
    conn = get_db_connection()
    path = Path(directory_path)
    
    # Find all navigation and settings files
    nav_files = list(path.rglob('*navigation*.csv'))
    settings_files = list(path.rglob('*settings*.csv'))
    
    print(f"\n📁 Found {len(nav_files)} navigation files and {len(settings_files)} settings files")
    
    # Import navigation files
    for nav_file in nav_files:
        import_navigation_file(nav_file, conn)
    
    # Import settings files (only for AUV according to spec)
    for settings_file in settings_files:
        vehicle_type, _ = extract_vehicle_info(settings_file.name)
        if vehicle_type == 'AUV':
            import_settings_file(settings_file, conn)
        else:
            print(f"⏭️  Skipping settings file {settings_file.name} (USV - settings not imported)")
    
    conn.close()
    print("\n✅ Import completed!")

if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("Usage: python import_logs.py <directory_path>")
        print("Example: python import_logs.py logs/20251123-monaco/raw_data/logs")
        sys.exit(1)
    
    directory = sys.argv[1]
    if not os.path.isdir(directory):
        print(f"Error: {directory} is not a valid directory")
        sys.exit(1)
    
    import_directory(directory)

