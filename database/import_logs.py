#!/usr/bin/env python3
"""
Import script for navigation and settings log files.
Handles different import logic for AUV (navigation + settings) and USV (navigation only).
"""

import psycopg2
from psycopg2.extras import execute_values
import pandas as pd
import os
import re
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
import json
import ast
import time
try:
    # Try relative import first (when used as part of database package)
    from .decode_usbl import decode_usbl_message, convert_kogger_state_to_seaker
except ImportError:
    # Fall back to absolute import (when database is in sys.path)
    from decode_usbl import decode_usbl_message, convert_kogger_state_to_seaker
from antenna_driver_analysis.kogger_usbl_decoder import (
    decode_usbl_message_from_csv,
    parse_bytes_from_string,
    reassemble_received_messages,
    parse_message
)

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
            SELECT id, import_status FROM log_files 
            WHERE filename = %s AND vehicle_type = %s AND vehicle_id = %s
            LIMIT 1
        """, (filename, vehicle_type, vehicle_id))
        existing = cursor.fetchone()
        if existing:
            # Get full status info
            cursor.execute("""
                SELECT id, import_status, row_count, first_timestamp, last_timestamp, error_message
                FROM log_files 
                WHERE id = %s
            """, (existing[0],))
            status_info = cursor.fetchone()
            
            # If import failed, allow re-import by deleting the failed entry
            if status_info[1] == 'failed':
                print(f"⚠️  File {filename} previously failed, deleting and re-importing...")
                # Delete the failed entry and its associated log_entries
                cursor.execute("DELETE FROM log_entries WHERE log_file_id = %s", (existing[0],))
                cursor.execute("DELETE FROM log_files WHERE id = %s", (existing[0],))
                conn.commit()
            else:
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
        # Adjust chunk size based on file size for better performance
        file_size_mb = file_size / (1024 * 1024)
        if file_size_mb > 30:  # Large files (> 30 MB)
            chunk_size = 100000  # Bigger chunks for large files
            log_frequency = 10  # Log every 10 chunks
            print(f"  ℹ️  Large file detected ({file_size_mb:.1f} MB), using optimized settings")
        else:
            chunk_size = 50000
            log_frequency = 1  # Log every chunk
        
        total_rows = 0
        first_timestamp = None
        last_timestamp = None
        
        # Try to read CSV, handle errors gracefully
        # Try different encodings if UTF-8 fails
        # latin-1 can read any byte (1:1 mapping), so it will always work
        csv_reader = None
        encodings = ['utf-8', 'iso-8859-1', 'cp1252', 'latin-1']
        
        for encoding in encodings:
            try:
                # Try to create the reader and test reading first chunk
                # pandas reads lazily, so we need to test actual reading
                csv_reader = pd.read_csv(file_path, chunksize=chunk_size, on_bad_lines='skip', 
                                        engine='python', encoding=encoding)
                # Test if we can actually read the first chunk (pandas reads lazily)
                # This will catch encoding errors that occur during actual reading
                try:
                    first_chunk = next(iter(csv_reader))
                    # Reset the reader by creating a new one since we consumed the first chunk
                    csv_reader = pd.read_csv(file_path, chunksize=chunk_size, on_bad_lines='skip', 
                                            engine='python', encoding=encoding)
                    if encoding != 'utf-8':
                        print(f"  ⚠️  Using {encoding} encoding (UTF-8 failed)")
                    break
                except (UnicodeDecodeError, Exception) as e2:
                    # First chunk read failed, try next encoding
                    if encoding == encodings[-1]:  # Last encoding failed (should be latin-1 which always works)
                        raise e2
                    continue
            except (UnicodeDecodeError, Exception) as e:
                if encoding == encodings[-1]:  # Last encoding failed
                    raise e
                continue
        
        chunk_index = 0
        import time
        last_log_time = time.time()
        
        for chunk in csv_reader:
            chunk_start_time = time.time()
            print(f"[DEBUG] Starting chunk {chunk_index}, chunk size: {len(chunk)} rows")
            
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
            
            # Bulk insert with execute_values (much faster than executemany)
            if entries:
                insert_start = time.time()
                print(f"[DEBUG] Chunk {chunk_index}: Inserting {len(entries):,} entries...")
                
                execute_values(cursor, """
                    INSERT INTO log_entries (time, vehicle_type, vehicle_id, data_type_id, value, log_file_id)
                    VALUES %s
                """, entries, page_size=1000)
                
                insert_time = time.time() - insert_start
                print(f"[DEBUG] Chunk {chunk_index}: Insert took {insert_time:.2f}s")
                
                commit_start = time.time()
                conn.commit()
                commit_time = time.time() - commit_start
                print(f"[DEBUG] Chunk {chunk_index}: Commit took {commit_time:.2f}s")
                
                total_rows += len(entries)
                chunk_time = time.time() - chunk_start_time
                print(f"[DEBUG] Chunk {chunk_index}: Total time {chunk_time:.2f}s, Total rows: {total_rows:,}")
                
                # Log progress for large files (less frequently to reduce overhead)
                if chunk_index % log_frequency == 0:
                    print(f"  📊 Processed {total_rows:,} rows...")
            
            # Update file progress after each chunk
            if file_progress is not None:
                estimated_total_lines = file_progress.get('estimated_total_lines')
                if estimated_total_lines and estimated_total_lines > 0:
                    # Calculate progress ratio based on rows processed
                    progress_ratio = min(total_rows / estimated_total_lines, 1.0)
                    file_progress['bytes_processed'] = int(file_size * progress_ratio)
                    print(f"[DEBUG] Chunk {chunk_index}: Progress {progress_ratio*100:.1f}% ({total_rows:,}/{estimated_total_lines:,} rows)")
                else:
                    # Fallback: estimate based on file size and average bytes per row
                    # Use a more reasonable estimate: assume ~100 bytes per row on average
                    estimated_rows = max(1, int(file_size / 100))
                    progress_ratio = min(total_rows / estimated_rows, 1.0)
                    file_progress['bytes_processed'] = int(file_size * progress_ratio)
                    print(f"[DEBUG] Chunk {chunk_index}: Progress estimate {progress_ratio*100:.1f}% ({total_rows:,} rows processed)")
            
            chunk_index += 1
            
            # Log every 10 seconds for very slow imports
            current_time = time.time()
            if current_time - last_log_time > 10:
                print(f"[DEBUG] Still processing... chunk {chunk_index}, {total_rows:,} rows so far, {current_time - chunk_start_time:.1f}s elapsed")
                last_log_time = current_time
        
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
        if log_file_id:
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

def import_usbl_file(file_path, conn, file_progress=None):
    """Import a USBL log file.
    
    Args:
        file_path: Path to the USBL log file
        conn: Database connection
        file_progress: Optional dict to track file import progress
    
    Returns:
        True if successful, False otherwise
    """
    filename = os.path.basename(file_path)
    vehicle_type, vehicle_id = extract_vehicle_info(filename)
    
    if not vehicle_type:
        print(f"⚠️  Could not determine vehicle type from {filename}, skipping")
        return False
    
    # USBL files are from USV
    if vehicle_type != 'USV':
        print(f"⚠️  USBL file {filename} is not from USV, skipping")
        return False
    
    cursor = conn.cursor()
    log_file_id = None
    
    try:
        # Check if file already exists
        cursor.execute("""
            SELECT id, import_status FROM log_files 
            WHERE filename = %s AND vehicle_type = %s AND vehicle_id = %s
            LIMIT 1
        """, (filename, vehicle_type, vehicle_id))
        existing = cursor.fetchone()
        if existing:
            print(f"⚠️  File {filename} already imported, skipping")
            return False
        
        # Create log_file entry
        file_size = os.path.getsize(file_path)
        
        if file_progress is not None:
            file_progress['bytes_total'] = file_size
            file_progress['bytes_processed'] = 0
        
        cursor.execute("""
            INSERT INTO log_files (filename, file_path, vehicle_type, vehicle_id, file_size_bytes, import_status, import_started_at)
            VALUES (%s, %s, %s, %s, %s, 'importing', NOW())
            RETURNING id
        """, (filename, str(file_path), vehicle_type, vehicle_id, file_size))
        log_file_id = cursor.fetchone()[0]
        conn.commit()
        
        print(f"📄 Importing USBL file {filename} ({vehicle_type} {vehicle_id})...")
        
        # Read CSV line by line
        total_rows = 0
        entries = []
        data_type_ids = {}
        
        # Data types for USBL
        usbl_data_types = {
            'USBL_Bearing': 'acoustic',  # Azimuth en degrés
            'USBL_Elevation': 'acoustic',  # Elevation en degrés
            'USBL_Distance': 'acoustic',  # Distance en mètres
            'USBL_AUV_State': 'state',
            'USBL_SNR': 'acoustic',
            'USBL_Sequence': 'acoustic'  # For tracking message sequences
        }
        
        # Get or create data types
        for data_type_name, category in usbl_data_types.items():
            data_type_ids[data_type_name] = get_or_create_data_type(cursor, data_type_name, vehicle_type)
        
        first_timestamp = None
        last_timestamp = None
        
        # Buffer for reassembling RECEIVED messages
        received_buffer = b''
        last_direction = None
        
        # List to store raw data for bulk insert
        raw_data_rows = []
        
        # List to store decoded messages for bulk insert
        decoded_messages = []
        
        # Read file in chunks for large files
        chunk_size = 50000
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            chunk = []
            for line in f:
                chunk.append(line)
                if len(chunk) >= chunk_size:
                    # Process chunk
                    for csv_line in chunk:
                        parts = csv_line.strip().split(',', 2)
                        if len(parts) < 3:
                            continue
                        
                        timestamp_str = parts[0].strip()
                        direction = parts[1].strip()
                        data_str = ','.join(parts[2:])
                        
                        # Parse timestamp
                        try:
                            timestamp = pd.to_datetime(timestamp_str, errors='coerce')
                            if pd.isna(timestamp) or timestamp.year < 2000 or timestamp.year > 2100:
                                continue
                        except:
                            continue
                        
                        if first_timestamp is None or timestamp < first_timestamp:
                            first_timestamp = timestamp
                        if last_timestamp is None or timestamp > last_timestamp:
                            last_timestamp = timestamp
                        
                        # Store raw data for later decoding (even if decoding fails)
                        raw_data_rows.append((
                            log_file_id,
                            timestamp,
                            direction,
                            data_str
                        ))
                        
                        # Handle direction change - reset buffer
                        if direction != last_direction:
                            received_buffer = b''
                        last_direction = direction
                        
                        # Parse data bytes
                        data_bytes = parse_bytes_from_string(data_str)
                        if data_bytes is None:
                            # Store error message
                            decoded_messages.append((
                                log_file_id,
                                timestamp,
                                direction,
                                None,  # message_id
                                None,  # message_name
                                None,  # message_type
                                None,  # version
                                None,  # device_address
                                f"Error: Could not parse data literal: {data_str}",  # payload_decoded
                                data_str,  # payload_raw
                                None,  # length
                                None,  # distance
                                None,  # bearing
                                None,  # elevation
                                None,  # snr
                                None,  # device_id
                            ))
                            continue
                        
                        # Process SENT messages (complete)
                        if direction == 'SENT':
                            decoded = decode_usbl_message_from_csv(timestamp_str, direction, data_str)
                            if decoded:
                                decoded_messages.append((
                                    log_file_id,
                                    timestamp,
                                    direction,
                                    decoded.get('message_id'),
                                    decoded.get('message_name'),
                                    decoded.get('message_type'),
                                    decoded.get('version'),
                                    decoded.get('device_address'),
                                    decoded.get('payload_decoded', ''),
                                    decoded.get('payload_raw', ''),
                                    decoded.get('length'),
                                    # Extracted values
                                    decoded.get('distance'),
                                    decoded.get('bearing') or decoded.get('azimuth'),
                                    decoded.get('elevation'),
                                    decoded.get('snr'),
                                    decoded.get('device_id'),
                                ))
                        
                        # Process RECEIVED messages (may need reassembly)
                        elif direction == 'RECEIVED':
                            complete_messages, received_buffer = reassemble_received_messages(data_bytes, received_buffer)
                            
                            for msg_bytes in complete_messages:
                                decoded = parse_message(msg_bytes)
                                if decoded:
                                    decoded_messages.append((
                                        log_file_id,
                                        timestamp,
                                        direction,
                                        decoded.get('message_id'),
                                        decoded.get('message_name'),
                                        decoded.get('message_type'),
                                        decoded.get('version'),
                                        decoded.get('device_address'),
                                        decoded.get('payload_decoded', ''),
                                        decoded.get('payload_raw', ''),
                                        decoded.get('length'),
                                        # Extracted values
                                        decoded.get('distance'),
                                        decoded.get('bearing') or decoded.get('azimuth'),
                                        decoded.get('elevation'),
                                        decoded.get('snr'),
                                        decoded.get('device_id'),
                                    ))
                        
                        # Also process with old decoder for log_entries (keep existing functionality)
                        if direction == 'RECEIVED':
                            decoded_old = decode_usbl_message(data_str, direction)
                            if decoded_old and decoded_old.get('message_type') == 'POSITION_RESPONSE':
                                # Extract data from decoded message
                                seq = decoded_old.get('sequence')
                                azimuth_deg = decoded_old.get('azimuth_deg')
                                elevation_deg = decoded_old.get('elevation_deg')
                                distance_m = decoded_old.get('distance_m')
                                auv_state = decoded_old.get('auv_state')
                                snr = decoded_old.get('snr')
                                
                                # Store sequence number
                                if seq is not None:
                                    entries.append((
                                        timestamp,
                                        vehicle_type,
                                        vehicle_id,
                                        data_type_ids['USBL_Sequence'],
                                        float(seq),
                                        log_file_id
                                    ))
                                
                                # Store azimuth (bearing) if available
                                if azimuth_deg is not None:
                                    entries.append((
                                        timestamp,
                                        vehicle_type,
                                        vehicle_id,
                                        data_type_ids['USBL_Bearing'],
                                        float(azimuth_deg),
                                        log_file_id
                                    ))
                                
                                # Store elevation if available
                                if elevation_deg is not None:
                                    entries.append((
                                        timestamp,
                                        vehicle_type,
                                        vehicle_id,
                                        data_type_ids['USBL_Elevation'],
                                        float(elevation_deg),
                                        log_file_id
                                    ))
                                
                                # Store distance if available
                                if distance_m is not None and distance_m > 0:
                                    entries.append((
                                        timestamp,
                                        vehicle_type,
                                        vehicle_id,
                                        data_type_ids['USBL_Distance'],
                                        float(distance_m),
                                        log_file_id
                                    ))
                                
                                # Store AUV state if available (convert Kogger to Seaker)
                                if auv_state is not None:
                                    seaker_state = convert_kogger_state_to_seaker(auv_state)
                                    entries.append((
                                        timestamp,
                                        vehicle_type,
                                        vehicle_id,
                                        data_type_ids['USBL_AUV_State'],
                                        float(seaker_state),
                                        log_file_id
                                    ))
                                
                                # Store SNR if available
                                if snr is not None:
                                    entries.append((
                                        timestamp,
                                        vehicle_type,
                                        vehicle_id,
                                        data_type_ids['USBL_SNR'],
                                        float(snr),
                                        log_file_id
                                    ))
                                
                                total_rows += 1
                    
                    # Bulk insert raw data first (before decoding, so we have it even if decoding fails)
                    if raw_data_rows:
                        execute_values(cursor, """
                            INSERT INTO usbl_raw_data (log_file_id, timestamp, direction, data_raw)
                            VALUES %s
                        """, raw_data_rows, page_size=1000)
                        conn.commit()
                        raw_data_rows = []
                    
                    # Bulk insert decoded messages
                    if decoded_messages:
                        execute_values(cursor, """
                            INSERT INTO usbl_decoded_messages 
                            (log_file_id, timestamp, direction, message_id, message_name, message_type, 
                             version, device_address, payload_decoded, payload_raw, length,
                             distance, bearing, elevation, snr, device_id)
                            VALUES %s
                        """, decoded_messages, page_size=1000)
                        conn.commit()
                        decoded_messages = []
                    
                    # Bulk insert log entries
                    if entries:
                        execute_values(cursor, """
                            INSERT INTO log_entries (time, vehicle_type, vehicle_id, data_type_id, value, log_file_id)
                            VALUES %s
                        """, entries, page_size=1000)
                        conn.commit()
                        entries = []
                    
                    # Update progress
                    if file_progress is not None:
                        progress_ratio = min(total_rows / 100000, 1.0)  # Estimate
                        file_progress['bytes_processed'] = int(file_size * progress_ratio)
                    
                    chunk = []
            
            # Process remaining chunk
            if chunk:
                for csv_line in chunk:
                    parts = csv_line.strip().split(',', 2)
                    if len(parts) < 3:
                        continue
                    
                    timestamp_str = parts[0].strip()
                    direction = parts[1].strip()
                    data_str = ','.join(parts[2:])
                    
                    # Parse timestamp
                    try:
                        timestamp = pd.to_datetime(timestamp_str, errors='coerce')
                        if pd.isna(timestamp) or timestamp.year < 2000 or timestamp.year > 2100:
                            continue
                    except:
                        continue
                    
                    if first_timestamp is None or timestamp < first_timestamp:
                        first_timestamp = timestamp
                    if last_timestamp is None or timestamp > last_timestamp:
                        last_timestamp = timestamp
                    
                    # Store raw data for later decoding (even if decoding fails)
                    raw_data_rows.append((
                        log_file_id,
                        timestamp,
                        direction,
                        data_str
                    ))
                    
                    # Handle direction change - reset buffer
                    if direction != last_direction:
                        received_buffer = b''
                    last_direction = direction
                    
                    # Parse data bytes
                    data_bytes = parse_bytes_from_string(data_str)
                    if data_bytes is None:
                        # Store error message
                        decoded_messages.append((
                            log_file_id,
                            timestamp,
                            direction,
                            None,
                            None,
                            None,
                            None,
                            None,
                            f"Error: Could not parse data literal: {data_str}",
                            data_str,
                            None,
                            None,  # distance
                            None,  # bearing
                            None,  # elevation
                            None,  # snr
                            None,  # device_id
                        ))
                        continue
                    
                    # Process SENT messages (complete)
                    if direction == 'SENT':
                        decoded = decode_usbl_message_from_csv(timestamp_str, direction, data_str)
                        if decoded:
                            decoded_messages.append((
                                log_file_id,
                                timestamp,
                                direction,
                                decoded.get('message_id'),
                                decoded.get('message_name'),
                                decoded.get('message_type'),
                                decoded.get('version'),
                                decoded.get('device_address'),
                                decoded.get('payload_decoded', ''),
                                decoded.get('payload_raw', ''),
                                decoded.get('length'),
                                # Extracted values
                                decoded.get('distance'),
                                decoded.get('bearing') or decoded.get('azimuth'),
                                decoded.get('elevation'),
                                decoded.get('snr'),
                                decoded.get('device_id'),
                            ))
                    
                    # Process RECEIVED messages (may need reassembly)
                    elif direction == 'RECEIVED':
                        complete_messages, received_buffer = reassemble_received_messages(data_bytes, received_buffer)
                        
                        for msg_bytes in complete_messages:
                            decoded = parse_message(msg_bytes)
                            if decoded:
                                decoded_messages.append((
                                    log_file_id,
                                    timestamp,
                                    direction,
                                    decoded.get('message_id'),
                                    decoded.get('message_name'),
                                    decoded.get('message_type'),
                                    decoded.get('version'),
                                    decoded.get('device_address'),
                                    decoded.get('payload_decoded', ''),
                                    decoded.get('payload_raw', ''),
                                    decoded.get('length'),
                                    # Extracted values
                                    decoded.get('distance'),
                                    decoded.get('bearing') or decoded.get('azimuth'),
                                    decoded.get('elevation'),
                                    decoded.get('snr'),
                                    decoded.get('device_id'),
                                ))
                    
                    # Also process with old decoder for log_entries (keep existing functionality)
                    if direction == 'RECEIVED':
                        decoded = decode_usbl_message(data_str, direction)
                        # Accepter POSITION_RESPONSE et STATUS_RESPONSE
                        if decoded and decoded.get('message_type') in ['POSITION_RESPONSE', 'STATUS_RESPONSE']:
                            # Extract data from decoded message
                            seq = decoded.get('sequence')
                            azimuth_deg = decoded.get('azimuth_deg')
                            elevation_deg = decoded.get('elevation_deg')
                            distance_m = decoded.get('distance_m')
                            auv_state = decoded.get('auv_state')
                            snr = decoded.get('snr')
                            
                            # Store sequence number
                            if seq is not None:
                                entries.append((
                                    timestamp,
                                    vehicle_type,
                                    vehicle_id,
                                    data_type_ids['USBL_Sequence'],
                                    float(seq),
                                    log_file_id
                                ))
                            
                            # Store azimuth (bearing) if available
                            if azimuth_deg is not None:
                                entries.append((
                                    timestamp,
                                    vehicle_type,
                                    vehicle_id,
                                    data_type_ids['USBL_Bearing'],
                                    float(azimuth_deg),
                                    log_file_id
                                ))
                            
                            # Store elevation if available
                            if elevation_deg is not None:
                                entries.append((
                                    timestamp,
                                    vehicle_type,
                                    vehicle_id,
                                    data_type_ids['USBL_Elevation'],
                                    float(elevation_deg),
                                    log_file_id
                                ))
                            
                            # Store distance if available
                            if distance_m is not None and distance_m > 0:
                                entries.append((
                                    timestamp,
                                    vehicle_type,
                                    vehicle_id,
                                    data_type_ids['USBL_Distance'],
                                    float(distance_m),
                                    log_file_id
                                ))
                            
                            # Store AUV state if available (convert Kogger to Seaker)
                            if auv_state is not None:
                                seaker_state = convert_kogger_state_to_seaker(auv_state)
                                entries.append((
                                    timestamp,
                                    vehicle_type,
                                    vehicle_id,
                                    data_type_ids['USBL_AUV_State'],
                                    float(seaker_state),
                                    log_file_id
                                ))
                            
                            # Store SNR if available
                            if snr is not None:
                                entries.append((
                                    timestamp,
                                    vehicle_type,
                                    vehicle_id,
                                    data_type_ids['USBL_SNR'],
                                    float(snr),
                                    log_file_id
                                ))
                            
                            total_rows += 1
        
        # Final bulk insert raw data
        if raw_data_rows:
            execute_values(cursor, """
                INSERT INTO usbl_raw_data (log_file_id, timestamp, direction, data_raw)
                VALUES %s
            """, raw_data_rows, page_size=1000)
            conn.commit()
        
        # Final bulk insert decoded messages
        if decoded_messages:
            execute_values(cursor, """
                INSERT INTO usbl_decoded_messages 
                (log_file_id, timestamp, direction, message_id, message_name, message_type, 
                 version, device_address, payload_decoded, payload_raw, length,
                 distance, bearing, elevation, snr, device_id)
                VALUES %s
            """, decoded_messages, page_size=1000)
            conn.commit()
        
        # Final bulk insert log entries
        if entries:
            execute_values(cursor, """
                INSERT INTO log_entries (time, vehicle_type, vehicle_id, data_type_id, value, log_file_id)
                VALUES %s
            """, entries, page_size=1000)
            conn.commit()
        
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
        
        print(f"✅ Imported {total_rows:,} USBL entries from {filename}")
        return True
        
    except Exception as e:
        conn.rollback()
        if log_file_id:
            cursor.execute("""
                UPDATE log_files
                SET import_status = 'failed', error_message = %s
                WHERE id = %s
            """, (str(e), log_file_id))
            conn.commit()
        print(f"❌ Error importing USBL file {filename}: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        cursor.close()

def convert_state_string_to_numeric(state_str):
    """Convert state/mode strings to numeric codes for storage."""
    state_map = {
        # RTK Status
        '3D_FIX': 1,
        'NO_FIX': 0,
        '2D_FIX': 2,
        # Arm Status
        'ARM': 1,
        'DISARM': 0,
        # Motion Status
        'LOITER': 1,
        'MANUAL': 2,
        'AUTO': 3,
        'GUIDED': 4,
        # AUV States / Orders
        'ALT_HOLD_FOLLOW': 14,
        'DEPTH_HOLD_FOLLOW': 13,
        'ALT_HOLD': 11,
        'DEPTH_HOLD': 7,
        'STANDBY': 25,
        'SURFACE': 19,
        'EMERGENCY': 1,
        "PAS D'ORDRE": 0,
        'PAS_D_ORDRE': 0,
    }
    return state_map.get(state_str.upper(), 0)

def import_usv_full_file(file_path, conn, file_progress=None):
    """Import a USV full log file containing USBL AUV tracking data.
    
    Args:
        file_path: Path to the USV full log file
        conn: Database connection
        file_progress: Optional dict to track file import progress
    
    Returns:
        True if successful, False otherwise
    """
    filename = os.path.basename(file_path)
    vehicle_type, vehicle_id = extract_vehicle_info(filename)
    
    if not vehicle_type:
        print(f"⚠️  Could not determine vehicle type from {filename}, skipping")
        return False
    
    # USV full files are from USV
    if vehicle_type != 'USV':
        print(f"⚠️  USV full file {filename} is not from USV, skipping")
        return False
    
    cursor = conn.cursor()
    log_file_id = None
    
    try:
        # Check if file already exists
        cursor.execute("""
            SELECT id, import_status FROM log_files 
            WHERE filename = %s AND vehicle_type = %s AND vehicle_id = %s
            LIMIT 1
        """, (filename, vehicle_type, vehicle_id))
        existing = cursor.fetchone()
        if existing:
            print(f"⚠️  File {filename} already imported, skipping")
            return False
        
        # Create log_file entry
        file_size = os.path.getsize(file_path)
        
        if file_progress is not None:
            file_progress['bytes_total'] = file_size
            file_progress['bytes_processed'] = 0
        
        cursor.execute("""
            INSERT INTO log_files (filename, file_path, vehicle_type, vehicle_id, file_size_bytes, import_status, import_started_at)
            VALUES (%s, %s, %s, %s, %s, 'importing', NOW())
            RETURNING id
        """, (filename, str(file_path), vehicle_type, vehicle_id, file_size))
        log_file_id = cursor.fetchone()[0]
        conn.commit()
        
        print(f"📄 Importing USV full file {filename} ({vehicle_type} {vehicle_id})...")
        
        # Data types for USV full log
        usv_full_data_types = {
            'USV_FULL_AUV_Distance': 'acoustic',
            'USV_FULL_AUV_Bearing': 'acoustic',
            'USV_FULL_AUV_Elevation': 'acoustic',
            'USV_FULL_AUV_State': 'state',
            'USV_FULL_USV_RTK_Status': 'GPS',
            'USV_FULL_USV_Arm_Status': 'State',
            'USV_FULL_USV_Motion_Status': 'State',
            'USV_FULL_USV_Order': 'State',
            'USV_FULL_Unknown': 'acoustic'
        }
        
        # Get or create data types
        data_type_ids = {}
        for data_type_name, category in usv_full_data_types.items():
            data_type_ids[data_type_name] = get_or_create_data_type(cursor, data_type_name, vehicle_type)
        
        first_timestamp = None
        last_timestamp = None
        total_rows = 0
        entries = []
        
        # Regex pattern to match lines with AUV information
        # Pattern: timestamp | status | location - USV:... | AUV#N:...
        # Note: The data may be split across two lines (timestamp on first, data on second with tabs)
        pattern = re.compile(
            r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)\s+\|\s+\w+\s+\|\s+.*?-\s+'
            r'(?:.*?\n)?\s*USV:([^|]+)\s+\|\s+AUV#(\d+):([^,]+),\s+([\d.]+)m,\s+([\d.]+)°/([\d.-]+)°:([\d.]+)',
            re.MULTILINE | re.DOTALL
        )
        
        # Read file in chunks and process
        chunk_size = 50000
        bytes_processed = 0
        
        # Read entire file content to handle multi-line matches
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            file_content = f.read()
            bytes_processed = len(file_content.encode('utf-8'))
        
        # Process all matches
        matches = pattern.finditer(file_content)
        
        # Process matches in chunks
        chunk = []
        for match in matches:
            match_count += 1
            chunk.append(match)
            
            if len(chunk) >= chunk_size:
                # Process chunk
                for match in chunk:
                    try:
                        # Extract timestamp
                        timestamp_str = match.group(1)
                        timestamp = pd.to_datetime(timestamp_str, errors='coerce')
                        if pd.isna(timestamp) or timestamp.year < 2000 or timestamp.year > 2100:
                            continue
                        
                        # Extract USV data: RTK status, Arm status, Motion status, Order
                        usv_data = match.group(2).strip()
                        usv_parts = [p.strip() for p in usv_data.split(',')]
                        rtk_status = usv_parts[0] if len(usv_parts) > 0 else ''
                        arm_status = usv_parts[1] if len(usv_parts) > 1 else ''
                        motion_status = usv_parts[2] if len(usv_parts) > 2 else ''
                        usv_order = usv_parts[3] if len(usv_parts) > 3 else ''
                        
                        # Extract AUV data
                        auv_id = int(match.group(3))
                        auv_state = match.group(4).strip()
                        distance = float(match.group(5))
                        bearing = float(match.group(6))
                        elevation = float(match.group(7))
                        unknown_value = float(match.group(8))
                        
                        # Update timestamp range
                        if first_timestamp is None or timestamp < first_timestamp:
                            first_timestamp = timestamp
                        if last_timestamp is None or timestamp > last_timestamp:
                            last_timestamp = timestamp
                        
                        # Store USV data
                        if rtk_status:
                            entries.append((
                                timestamp,
                                vehicle_type,
                                vehicle_id,
                                data_type_ids['USV_FULL_USV_RTK_Status'],
                                float(convert_state_string_to_numeric(rtk_status)),
                                log_file_id
                            ))
                        
                        if arm_status:
                            entries.append((
                                timestamp,
                                vehicle_type,
                                vehicle_id,
                                data_type_ids['USV_FULL_USV_Arm_Status'],
                                float(convert_state_string_to_numeric(arm_status)),
                                log_file_id
                            ))
                        
                        if motion_status:
                            entries.append((
                                timestamp,
                                vehicle_type,
                                vehicle_id,
                                data_type_ids['USV_FULL_USV_Motion_Status'],
                                float(convert_state_string_to_numeric(motion_status)),
                                log_file_id
                            ))
                        
                        if usv_order:
                            entries.append((
                                timestamp,
                                vehicle_type,
                                vehicle_id,
                                data_type_ids['USV_FULL_USV_Order'],
                                float(convert_state_string_to_numeric(usv_order)),
                                log_file_id
                            ))
                        
                        # Store AUV data (with AUV ID in vehicle_id field)
                        auv_vehicle_id = f"AUV{auv_id}"
                        
                        entries.append((
                            timestamp,
                            'AUV',  # vehicle_type for AUV data
                            auv_vehicle_id,
                            data_type_ids['USV_FULL_AUV_Distance'],
                            distance,
                            log_file_id
                        ))
                        
                        entries.append((
                            timestamp,
                            'AUV',
                            auv_vehicle_id,
                            data_type_ids['USV_FULL_AUV_Bearing'],
                            bearing,
                            log_file_id
                        ))
                        
                        entries.append((
                            timestamp,
                            'AUV',
                            auv_vehicle_id,
                            data_type_ids['USV_FULL_AUV_Elevation'],
                            elevation,
                            log_file_id
                        ))
                        
                        entries.append((
                            timestamp,
                            'AUV',
                            auv_vehicle_id,
                            data_type_ids['USV_FULL_AUV_State'],
                            float(convert_state_string_to_numeric(auv_state)),
                            log_file_id
                        ))
                        
                        entries.append((
                            timestamp,
                            'AUV',
                            auv_vehicle_id,
                            data_type_ids['USV_FULL_Unknown'],
                            unknown_value,
                            log_file_id
                        ))
                        
                        total_rows += 1
                        
                    except (ValueError, IndexError) as e:
                        # Skip malformed lines
                        continue
                
                # Bulk insert entries
                if entries:
                    execute_values(cursor, """
                        INSERT INTO log_entries (time, vehicle_type, vehicle_id, data_type_id, value, log_file_id)
                        VALUES %s
                    """, entries, page_size=1000)
                    conn.commit()
                    
                    # Update progress
                    if file_progress is not None:
                        progress_ratio = min(bytes_processed / file_size, 1.0)
                        file_progress['bytes_processed'] = bytes_processed
                    
                    entries = []
                
                chunk = []
        
        # Process remaining chunk
        if chunk:
            for match in chunk:
                try:
                    timestamp_str = match.group(1)
                    timestamp = pd.to_datetime(timestamp_str, errors='coerce')
                    if pd.isna(timestamp) or timestamp.year < 2000 or timestamp.year > 2100:
                        continue
                    
                    usv_data = match.group(2).strip()
                    usv_parts = [p.strip() for p in usv_data.split(',')]
                    rtk_status = usv_parts[0] if len(usv_parts) > 0 else ''
                    arm_status = usv_parts[1] if len(usv_parts) > 1 else ''
                    motion_status = usv_parts[2] if len(usv_parts) > 2 else ''
                    usv_order = usv_parts[3] if len(usv_parts) > 3 else ''
                    
                    auv_id = int(match.group(3))
                    auv_state = match.group(4).strip()
                    distance = float(match.group(5))
                    bearing = float(match.group(6))
                    elevation = float(match.group(7))
                    unknown_value = float(match.group(8))
                    
                    if first_timestamp is None or timestamp < first_timestamp:
                        first_timestamp = timestamp
                    if last_timestamp is None or timestamp > last_timestamp:
                        last_timestamp = timestamp
                    
                    if rtk_status:
                        entries.append((
                            timestamp,
                            vehicle_type,
                            vehicle_id,
                            data_type_ids['USV_FULL_USV_RTK_Status'],
                            float(convert_state_string_to_numeric(rtk_status)),
                            log_file_id
                        ))
                    
                    if arm_status:
                        entries.append((
                            timestamp,
                            vehicle_type,
                            vehicle_id,
                            data_type_ids['USV_FULL_USV_Arm_Status'],
                            float(convert_state_string_to_numeric(arm_status)),
                            log_file_id
                        ))
                    
                    if motion_status:
                        entries.append((
                            timestamp,
                            vehicle_type,
                            vehicle_id,
                            data_type_ids['USV_FULL_USV_Motion_Status'],
                            float(convert_state_string_to_numeric(motion_status)),
                            log_file_id
                        ))
                    
                    if usv_order:
                        entries.append((
                            timestamp,
                            vehicle_type,
                            vehicle_id,
                            data_type_ids['USV_FULL_USV_Order'],
                            float(convert_state_string_to_numeric(usv_order)),
                            log_file_id
                        ))
                    
                    auv_vehicle_id = f"AUV{auv_id}"
                    
                    entries.append((
                        timestamp,
                        'AUV',
                        auv_vehicle_id,
                        data_type_ids['USV_FULL_AUV_Distance'],
                        distance,
                        log_file_id
                    ))
                    
                    entries.append((
                        timestamp,
                        'AUV',
                        auv_vehicle_id,
                        data_type_ids['USV_FULL_AUV_Bearing'],
                        bearing,
                        log_file_id
                    ))
                    
                    entries.append((
                        timestamp,
                        'AUV',
                        auv_vehicle_id,
                        data_type_ids['USV_FULL_AUV_Elevation'],
                        elevation,
                        log_file_id
                    ))
                    
                    entries.append((
                        timestamp,
                        'AUV',
                        auv_vehicle_id,
                        data_type_ids['USV_FULL_AUV_State'],
                        float(convert_state_string_to_numeric(auv_state)),
                        log_file_id
                    ))
                    
                    entries.append((
                        timestamp,
                        'AUV',
                        auv_vehicle_id,
                        data_type_ids['USV_FULL_Unknown'],
                        unknown_value,
                        log_file_id
                    ))
                    
                    total_rows += 1
                    
                except (ValueError, IndexError) as e:
                    continue
        
        # Final bulk insert
        if entries:
            execute_values(cursor, """
                INSERT INTO log_entries (time, vehicle_type, vehicle_id, data_type_id, value, log_file_id)
                VALUES %s
            """, entries, page_size=1000)
            conn.commit()
        
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
        
        print(f"✅ Imported {total_rows:,} USV full entries from {filename}")
        return True
        
    except Exception as e:
        conn.rollback()
        if log_file_id:
            cursor.execute("""
                UPDATE log_files
                SET import_status = 'failed', error_message = %s
                WHERE id = %s
            """, (str(e), log_file_id))
            conn.commit()
        print(f"❌ Error importing USV full file {filename}: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        cursor.close()

def flatten_json_to_settings(json_value, prefix, settings_list, settings_file_id):
    """
    Décompose récursivement un objet JSON en entrées individuelles.
    
    Args:
        json_value: Valeur JSON (dict, list ou valeur simple)
        prefix: Préfixe pour le nom du setting (ex: "camera")
        settings_list: Liste à remplir avec les tuples (settings_file_id, name, value, value_type, category)
        settings_file_id: ID du fichier de settings
    """
    if isinstance(json_value, dict):
        for key, value in json_value.items():
            new_prefix = f"{prefix}.{key}" if prefix else key
            flatten_json_to_settings(value, new_prefix, settings_list, settings_file_id)
    elif isinstance(json_value, list):
        # Pour les listes, créer une entrée avec la valeur sérialisée
        settings_list.append((settings_file_id, prefix, json.dumps(json_value), 'json', 'configuration'))
    else:
        # Valeur simple
        value_type = 'string'
        if isinstance(json_value, bool):
            value_type = 'boolean'
        elif isinstance(json_value, (int, float)):
            value_type = 'number'
        settings_list.append((settings_file_id, prefix, str(json_value), value_type, 'configuration'))

def import_settings_file(file_path, conn):
    """Import a settings file."""
    filename = os.path.basename(file_path)
    vehicle_type, vehicle_id = extract_vehicle_info(filename)
    
    if not vehicle_type:
        print(f"⚠️  Could not determine vehicle type from {filename}, skipping")
        return False
    
    cursor = conn.cursor()
    
    # List of JSON keys to decompose
    json_keys_to_decompose = ['camera', 'api', 'navigation', 'usbl', 'seaker', 'follow', 'Emergency']
    
    try:
        # Create settings_file entry
        file_size = os.path.getsize(file_path)
        
        # Check if file already exists
        cursor.execute("""
            SELECT id FROM settings_files 
            WHERE filename = %s AND vehicle_type = %s AND vehicle_id = %s
        """, (filename, vehicle_type, vehicle_id))
        existing = cursor.fetchone()
        
        if existing:
            # Delete existing settings to allow reimport with new structure
            settings_file_id = existing[0]
            print(f"⚠️  Settings file {filename} already imported, deleting old data and reimporting...")
            cursor.execute("DELETE FROM vehicle_settings WHERE settings_file_id = %s", (settings_file_id,))
            cursor.execute("""
                UPDATE settings_files 
                SET import_status = 'importing', import_started_at = NOW()
                WHERE id = %s
            """, (settings_file_id,))
            conn.commit()
        else:
            # Create new entry
            cursor.execute("""
                INSERT INTO settings_files (filename, file_path, vehicle_type, vehicle_id, file_size_bytes, import_status, import_started_at)
                VALUES (%s, %s, %s, %s, %s, 'importing', NOW())
                RETURNING id
            """, (filename, str(file_path), vehicle_type, vehicle_id, file_size))
            result = cursor.fetchone()
            settings_file_id = result[0]
            conn.commit()
        
        print(f"⚙️  Importing settings {filename} ({vehicle_type} {vehicle_id})...")
        
        # Read settings CSV - handle various formats
        # Settings files can use tab or comma as separator
        # But dict values contain commas, so we need to handle this carefully
        df_rows = []
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    
                    # Try to split on first tab or comma
                    # For tab-separated: name\tvalue
                    # For comma-separated: name,value (but value may contain commas)
                    if '\t' in line:
                        # Tab-separated: split on first tab
                        parts = line.split('\t', 1)
                        if len(parts) == 2:
                            df_rows.append({'setting_name': parts[0].strip(), 'setting_value': parts[1].strip()})
                    else:
                        # Comma-separated: split on first comma only
                        # Use a regex to split on first comma that's not inside quotes
                        import re
                        # Match first comma that's not inside single or double quotes
                        match = re.match(r"^([^,]+?),\s*(.+)$", line)
                        if match:
                            name = match.group(1).strip()
                            value = match.group(2).strip()
                            df_rows.append({'setting_name': name, 'setting_value': value})
                        else:
                            # Fallback: split on first comma
                            parts = line.split(',', 1)
                            if len(parts) == 2:
                                df_rows.append({'setting_name': parts[0].strip(), 'setting_value': parts[1].strip()})
            
            df = pd.DataFrame(df_rows)
            if df.empty:
                raise ValueError("No data read from file")
        except Exception as e:
            print(f"  ⚠️  Error reading file with custom parser: {e}")
            # Fallback to pandas
            try:
                df = pd.read_csv(file_path, header=None, names=['setting_name', 'setting_value'], 
                               sep='\t', on_bad_lines='skip', engine='python')
            except Exception:
                try:
                    df = pd.read_csv(file_path, header=None, names=['setting_name', 'setting_value'],
                                   sep=',', on_bad_lines='skip', engine='python', encoding='latin-1')
                except Exception as e2:
                    print(f"  ❌ Could not read settings file: {e2}")
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
            is_dict_like = False
            if value.startswith('{') and value.endswith('}'):
                value_type = 'json'
                is_dict_like = True
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
            
            # If this is a dict-like value for a key we want to decompose, parse and flatten it
            if is_dict_like and name in json_keys_to_decompose:
                try:
                    # Normalize the value: replace tabs with commas (but be careful with values containing tabs)
                    # First, try to parse as JSON
                    parsed_json = None
                    try:
                        parsed_json = json.loads(value)
                    except json.JSONDecodeError:
                        # If JSON parsing fails, normalize Python dict syntax
                        # The CSV uses tabs instead of commas between key-value pairs
                        # Replace tabs with commas (simple approach: replace all tabs in dict context)
                        import re
                        normalized_value = value
                        # Replace tabs that appear between key-value pairs
                        # Pattern: 'key': value\t'key2': -> 'key': value, 'key2':
                        # Look for tab followed by quote (start of next key)
                        normalized_value = re.sub(r"\t\s*(['\"][^'\"]*:)", r', \1', normalized_value)
                        
                        # Try to parse as Python dict
                        try:
                            parsed_json = ast.literal_eval(normalized_value)
                        except (ValueError, SyntaxError) as e1:
                            # If that fails, try a more aggressive normalization
                            # Replace all tabs with commas (might work if no tabs in values)
                            normalized_value2 = value.replace('\t', ', ')
                            try:
                                parsed_json = ast.literal_eval(normalized_value2)
                            except (ValueError, SyntaxError) as e2:
                                # Last resort: try original value
                                try:
                                    parsed_json = ast.literal_eval(value)
                                except (ValueError, SyntaxError):
                                    raise ValueError(f"Could not parse dict after normalization attempts: {e1}, {e2}")
                    
                    if parsed_json is None:
                        raise ValueError("Could not parse dict value")
                    
                    # Keep the original entry with full value
                    settings.append((settings_file_id, name, value, value_type, category))
                    # Add flattened entries
                    flatten_json_to_settings(parsed_json, name, settings, settings_file_id)
                except (json.JSONDecodeError, ValueError, SyntaxError) as e:
                    # If parsing fails, just add the original entry
                    print(f"  ⚠️  Could not parse dict for {name}: {e}")
                    print(f"     Value: {value[:100]}...")  # Print first 100 chars for debugging
                    settings.append((settings_file_id, name, value, value_type, category))
            else:
                # Regular setting, add as is
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

