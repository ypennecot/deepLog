#!/usr/bin/env python3
"""
Flask web application for visualizing log data.
"""

from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
import os
import platform
from dotenv import load_dotenv
from pathlib import Path
import tempfile
import shutil
import sys
import threading
import uuid
import json
import time
import math
import pandas as pd
import subprocess
from datetime import datetime
from dateutil import parser as date_parser
import ffmpeg
from PIL import Image
from functools import wraps

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key')

# Performance monitoring decorator
def monitor_performance(f):
    """Decorator to monitor API endpoint performance."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        start_time = time.time()
        endpoint_name = f.__name__
        
        # Log request start
        print(f"[PERF] {endpoint_name} START")
        
        try:
            result = f(*args, **kwargs)
            duration = (time.time() - start_time) * 1000  # Convert to ms
            
            # Log completion with timing
            if duration > 1000:
                print(f"[PERF] {endpoint_name} SLOW! {duration:.0f}ms")
            elif duration > 500:
                print(f"[PERF] {endpoint_name} WARNING {duration:.0f}ms")
            else:
                print(f"[PERF] {endpoint_name} OK {duration:.0f}ms")
            
            return result
        except Exception as e:
            duration = (time.time() - start_time) * 1000
            print(f"[PERF] {endpoint_name} FAILED after {duration:.0f}ms: {str(e)}")
            raise
    
    return decorated_function

def get_debug_log_path():
    """Get the path to the debug log file, creating directory if needed."""
    debug_log_dir = Path(__file__).parent / '.cursor'
    debug_log_dir.mkdir(exist_ok=True)
    return debug_log_dir / 'debug.log'

# Store import status in memory (in production, use Redis or similar)
import_status = {}
import_status_lock = threading.Lock()

def get_db_connection():
    """Get database connection."""
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

def extract_video_metadata(file_path):
    """
    Extract metadata from video file using ffprobe.
    Returns dict with start_time (datetime), duration_seconds (float), and other metadata.
    """
    try:
        # Use ffprobe to get video metadata
        probe = ffmpeg.probe(file_path)
        
        # Get duration
        duration_seconds = None
        if 'format' in probe and 'duration' in probe['format']:
            duration_seconds = float(probe['format']['duration'])
        
        # Try to get creation time from format tags
        start_time = None
        if 'format' in probe and 'tags' in probe['format']:
            tags = probe['format']['tags']
            # Try common date/time tags
            for tag_name in ['creation_time', 'date', 'DATE', 'com.apple.quicktime.creationdate']:
                if tag_name in tags:
                    try:
                        start_time = date_parser.parse(tags[tag_name])
                        break
                    except (ValueError, TypeError):
                        continue
        
        # If no creation time found in format tags, try stream tags
        if start_time is None and 'streams' in probe:
            for stream in probe['streams']:
                if 'tags' in stream:
                    tags = stream['tags']
                    for tag_name in ['creation_time', 'date', 'DATE', 'com.apple.quicktime.creationdate']:
                        if tag_name in tags:
                            try:
                                start_time = date_parser.parse(tags[tag_name])
                                break
                            except (ValueError, TypeError):
                                continue
                if start_time is not None:
                    break
        
        return {
            'start_time': start_time,
            'duration_seconds': duration_seconds,
            'format': probe.get('format', {}).get('format_name', 'unknown'),
            'width': None,
            'height': None
        }
    except Exception as e:
        print(f"Error extracting video metadata from {file_path}: {str(e)}")
        return {
            'start_time': None,
            'duration_seconds': None,
            'format': 'unknown',
            'width': None,
            'height': None,
            'error': str(e)
        }

def extract_video_thumbnails(video_id, file_path):
    """
    Extract thumbnails from video file every 0.5 seconds.
    Runs in a background thread.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    error_message = None
    
    try:
        # Check if ffmpeg is available
        try:
            import subprocess
            result = subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
            if result.returncode != 0:
                raise Exception("ffmpeg command failed")
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as e:
            error_message = f"ffmpeg not found or not working: {str(e)}. Please install ffmpeg."
            raise Exception(error_message)
        
        # Check if video file exists
        if not Path(file_path).exists():
            error_message = f"Video file not found: {file_path}"
            raise FileNotFoundError(error_message)
        
        # Update status to processing
        cursor.execute("""
            UPDATE videos 
            SET thumbnail_status = 'processing' 
            WHERE id = %s
        """, (video_id,))
        conn.commit()
        
        # Get video duration
        cursor.execute("SELECT duration_seconds FROM videos WHERE id = %s", (video_id,))
        result = cursor.fetchone()
        if not result or not result[0]:
            error_message = "Video duration not found in database"
            raise ValueError(error_message)
        
        duration_seconds = float(result[0])
        if duration_seconds <= 0:
            error_message = f"Invalid video duration: {duration_seconds}"
            raise ValueError(error_message)
        
        # Create thumbnails directory
        thumbnails_dir = Path('static/videos/thumbnails') / str(video_id)
        thumbnails_dir.mkdir(parents=True, exist_ok=True)
        
        # Extract thumbnails every 0.5 seconds
        thumbnail_interval = 0.5
        current_time = 0.0
        thumbnail_count = 0
        errors = []
        thumbnail_batch = []  # Batch insert for better performance
        
        while current_time < duration_seconds:
            # Output filename
            output_file = thumbnails_dir / f"{current_time:.2f}.jpg"
            
            try:
                # Use ffmpeg to extract frame at specific time with VGA max resize
                # Scale filter: min(640,iw):min(480,ih) preserves aspect ratio, max VGA
                cmd = [
                    'ffmpeg',
                    '-i', str(file_path),
                    '-ss', str(current_time),
                    '-vframes', '1',
                    '-vf', "scale='min(640,iw)':'min(480,ih)':force_original_aspect_ratio=decrease",
                    '-q:v', '5',  # Quality 5 for good size/quality balance
                    '-y',  # Overwrite output file
                    str(output_file)
                ]
                
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                
                if result.returncode == 0 and output_file.exists():
                    # Get thumbnail metadata
                    try:
                        with Image.open(output_file) as img:
                            width, height = img.size
                        file_size = output_file.stat().st_size
                        
                        # Prepare for batch insert
                        relative_path = f'/static/videos/thumbnails/{video_id}/{current_time:.2f}.jpg'
                        thumbnail_batch.append((
                            video_id,
                            current_time,
                            relative_path,
                            file_size,
                            width,
                            height
                        ))
                        
                        thumbnail_count += 1
                    except Exception as img_error:
                        errors.append(f"Time {current_time:.2f}s: Error reading image metadata: {str(img_error)[:200]}")
                else:
                    error_msg = result.stderr[:200] if result.stderr else "Unknown error"
                    errors.append(f"Time {current_time:.2f}s: {error_msg}")
                    
            except subprocess.TimeoutExpired:
                errors.append(f"Time {current_time:.2f}s: Timeout")
            except Exception as e:
                errors.append(f"Time {current_time:.2f}s: {str(e)[:200]}")
            
            current_time += thumbnail_interval
            
            # Batch insert every 100 thumbnails for better performance
            if len(thumbnail_batch) >= 100:
                try:
                    from psycopg2.extras import execute_values
                    execute_values(
                        cursor,
                        """
                        INSERT INTO video_thumbnails 
                        (video_id, time_offset, file_path, file_size_bytes, width, height)
                        VALUES %s
                        ON CONFLICT (video_id, time_offset) DO NOTHING
                        """,
                        thumbnail_batch
                    )
                    conn.commit()
                    thumbnail_batch = []
                except Exception as batch_error:
                    print(f"Error in batch insert: {batch_error}")
                    errors.append(f"Batch insert error: {str(batch_error)[:200]}")
        
        # Insert remaining thumbnails in batch
        if thumbnail_batch:
            try:
                from psycopg2.extras import execute_values
                execute_values(
                    cursor,
                    """
                    INSERT INTO video_thumbnails 
                    (video_id, time_offset, file_path, file_size_bytes, width, height)
                    VALUES %s
                    ON CONFLICT (video_id, time_offset) DO NOTHING
                    """,
                    thumbnail_batch
                )
                conn.commit()
            except Exception as batch_error:
                print(f"Error in final batch insert: {batch_error}")
                errors.append(f"Final batch insert error: {str(batch_error)[:200]}")
        
        if thumbnail_count == 0:
            error_message = f"Failed to extract any thumbnails. Errors: {'; '.join(errors[:5])}"
            raise Exception(error_message)
        
        # Update status to completed
        cursor.execute("""
            UPDATE videos 
            SET thumbnail_status = 'completed' 
            WHERE id = %s
        """, (video_id,))
        conn.commit()
        
        print(f"✓ Extracted {thumbnail_count} thumbnails for video {video_id} (indexed in database)")
        
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        error_message = error_message or str(e)
        print(f"Error extracting thumbnails for video {video_id}: {error_message}")
        print(f"Details: {error_details}")
        
        # Update status to failed with error message
        try:
            # Note: We'd need to add an error_message column to videos table to store this
            cursor.execute("""
                UPDATE videos 
                SET thumbnail_status = 'failed' 
                WHERE id = %s
            """, (video_id,))
            conn.commit()
        except Exception as update_error:
            print(f"Error updating status: {update_error}")
    finally:
        cursor.close()
        conn.close()

@app.route('/')
def index():
    """Main page showing data tables."""
    return render_template('index.html')

@app.route('/settings')
def settings():
    """Page showing settings logs comparison across runs."""
    return render_template('settings.html')

@app.route('/run/<int:run_id>')
def run_detail(run_id):
    """Page showing detailed analysis of a specific AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Get run metadata
        cursor.execute("""
            SELECT 
                id,
                filename,
                vehicle_type,
                vehicle_id,
                first_timestamp,
                last_timestamp,
                row_count
            FROM log_files
            WHERE id = %s AND vehicle_type = 'AUV'
        """, (run_id,))
        
        run = cursor.fetchone()
        if not run:
            return "Run not found", 404
        
        # Convert timestamps to ISO format for template
        if run['first_timestamp']:
            run['first_timestamp'] = run['first_timestamp'].isoformat()
        if run['last_timestamp']:
            run['last_timestamp'] = run['last_timestamp'].isoformat()
        
        return render_template('run_detail.html', run=run)
    finally:
        cursor.close()
        conn.close()

@app.route('/run/<int:run_id>/data')
def run_data(run_id):
    """Page showing all data for a specific run in a table format."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Get run metadata
        cursor.execute("""
            SELECT 
                id,
                filename,
                vehicle_type,
                vehicle_id,
                first_timestamp,
                last_timestamp,
                row_count
            FROM log_files
            WHERE id = %s
        """, (run_id,))
        
        run = cursor.fetchone()
        if not run:
            return "Run not found", 404
        
        # Convert timestamps to ISO format for template
        if run['first_timestamp']:
            run['first_timestamp'] = run['first_timestamp'].isoformat()
        if run['last_timestamp']:
            run['last_timestamp'] = run['last_timestamp'].isoformat()
        
        return render_template('run_data.html', run=run)
    finally:
        cursor.close()
        conn.close()

@app.route('/api/log_entries')
def get_log_entries():
    """API endpoint to get log entries with pagination."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Get query parameters
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 100))
        vehicle_type = request.args.get('vehicle_type', '')
        data_type = request.args.get('data_type', '')
        start_time = request.args.get('start_time', '')
        end_time = request.args.get('end_time', '')
        
        offset = (page - 1) * per_page
        
        # Build query
        query = """
            SELECT 
                le.time,
                le.vehicle_type,
                le.vehicle_id,
                dtc.data_type,
                le.value,
                lf.filename AS source_filename
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            JOIN log_files lf ON lf.id = le.log_file_id
            WHERE 1=1
        """
        params = []
        
        if vehicle_type:
            query += " AND le.vehicle_type = %s"
            params.append(vehicle_type)
        
        if data_type:
            query += " AND dtc.data_type = %s"
            params.append(data_type)
        
        if start_time:
            query += " AND le.time >= %s"
            params.append(start_time)
        
        if end_time:
            query += " AND le.time <= %s"
            params.append(end_time)
        
        query += " ORDER BY le.time DESC LIMIT %s OFFSET %s"
        params.extend([per_page, offset])
        
        cursor.execute(query, params)
        entries = cursor.fetchall()
        
        # Get total count
        count_query = """
            SELECT COUNT(*) as total
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE 1=1
        """
        count_params = []
        
        if vehicle_type:
            count_query += " AND le.vehicle_type = %s"
            count_params.append(vehicle_type)
        
        if data_type:
            count_query += " AND dtc.data_type = %s"
            count_params.append(data_type)
        
        if start_time:
            count_query += " AND le.time >= %s"
            count_params.append(start_time)
        
        if end_time:
            count_query += " AND le.time <= %s"
            count_params.append(end_time)
        
        cursor.execute(count_query, count_params)
        total = cursor.fetchone()['total']
        
        # Convert entries to dict, ensuring timestamps preserve microseconds
        entries_dict = []
        for row in entries:
            entry = dict(row)
            # Convert timestamp to ISO format with microseconds if it's a datetime
            if 'time' in entry and entry['time']:
                if hasattr(entry['time'], 'isoformat'):
                    # Preserve microseconds in ISO format
                    entry['time'] = entry['time'].isoformat()
            entries_dict.append(entry)
        
        return jsonify({
            'entries': entries_dict,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': (total + per_page - 1) // per_page
        })
        
    finally:
        cursor.close()
        conn.close()

@app.route('/api/vehicle_types')
def get_vehicle_types():
    """Get list of available vehicle types."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        cursor.execute("SELECT DISTINCT vehicle_type FROM log_entries ORDER BY vehicle_type")
        return jsonify([row['vehicle_type'] for row in cursor.fetchall()])
    finally:
        cursor.close()
        conn.close()

@app.route('/api/data_types')
def get_data_types():
    """Get list of available data types."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        vehicle_type = request.args.get('vehicle_type', '')
        query = "SELECT DISTINCT data_type FROM data_type_catalog WHERE 1=1"
        params = []
        
        if vehicle_type:
            query += " AND (vehicle_type = %s OR vehicle_type = 'BOTH')"
            params.append(vehicle_type)
        
        query += " ORDER BY data_type"
        cursor.execute(query, params)
        return jsonify([row['data_type'] for row in cursor.fetchall()])
    finally:
        cursor.close()
        conn.close()

@app.route('/api/log_entries_pivoted')
def get_log_entries_pivoted():
    """API endpoint to get log entries in pivoted format (columns = data types)."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Get query parameters
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 100))
        vehicle_type = request.args.get('vehicle_type', '')
        data_types = request.args.getlist('data_types')  # Multiple data types
        start_time = request.args.get('start_time', '')
        end_time = request.args.get('end_time', '')
        
        if not data_types:
            return jsonify({'error': 'At least one data_type must be specified'}), 400
        
        offset = (page - 1) * per_page
        
        # Build pivot query - get all selected data types for each timestamp
        # First, get data_type_ids for selected data types
        placeholders = ','.join(['%s'] * len(data_types))
        cursor.execute(f"SELECT id, data_type FROM data_type_catalog WHERE data_type IN ({placeholders})", data_types)
        dt_mapping = {row['data_type']: row['id'] for row in cursor.fetchall()}
        
        # Build SELECT with conditional aggregation for each data type
        # Use time_bucket with 10ms to group entries that are very close in time
        # This ensures data with same timestamp (within 10ms) are on the same row
        select_parts = [
            "time_bucket('10 milliseconds', le.time) AS time",
            "le.vehicle_type", 
            "le.vehicle_id"
        ]
        for dt in data_types:
            if dt in dt_mapping:
                dt_id = dt_mapping[dt]
                # Escape data type name for column alias
                dt_escaped = dt.replace('"', '""')
                # Use MAX to get the value if multiple entries exist for same timestamp
                select_parts.append(f"""
                    MAX(CASE WHEN le.data_type_id = {dt_id} THEN le.value END) AS "{dt_escaped}"
                """)
        
        query = f"""
            SELECT 
                {', '.join(select_parts)}
            FROM log_entries le
            WHERE 1=1
        """
        params = []
        
        if vehicle_type:
            query += " AND le.vehicle_type = %s"
            params.append(vehicle_type)
        
        # Filter by data_type_ids
        if dt_mapping:
            dt_ids = list(dt_mapping.values())
            placeholders_ids = ','.join(['%s'] * len(dt_ids))
            query += f" AND le.data_type_id IN ({placeholders_ids})"
            params.extend(dt_ids)
        
        if start_time:
            query += " AND le.time >= %s"
            params.append(start_time)
        
        if end_time:
            query += " AND le.time <= %s"
            params.append(end_time)
        
        # Group by the bucketed time, vehicle_type, and vehicle_id
        # Use the same time_bucket expression in GROUP BY (10ms bucket)
        query += " GROUP BY time_bucket('10 milliseconds', le.time), le.vehicle_type, le.vehicle_id ORDER BY time_bucket('10 milliseconds', le.time) DESC LIMIT %s OFFSET %s"
        params.extend([per_page, offset])
        
        cursor.execute(query, params)
        entries = cursor.fetchall()
        
        # Get total count (number of unique timestamps, bucketed by 10ms)
        count_query = """
            SELECT COUNT(DISTINCT time_bucket('10 milliseconds', le.time)) as total
            FROM log_entries le
            WHERE 1=1
        """
        count_params = []
        
        if vehicle_type:
            count_query += " AND le.vehicle_type = %s"
            count_params.append(vehicle_type)
        
        # Filter by data_type_ids for count (use same mapping as main query)
        if dt_mapping:
            dt_ids = list(dt_mapping.values())
            placeholders_ids = ','.join(['%s'] * len(dt_ids))
            count_query += f" AND le.data_type_id IN ({placeholders_ids})"
            count_params.extend(dt_ids)
        
        if start_time:
            count_query += " AND le.time >= %s"
            count_params.append(start_time)
        
        if end_time:
            count_query += " AND le.time <= %s"
            count_params.append(end_time)
        
        cursor.execute(count_query, count_params)
        total = cursor.fetchone()['total']
        
        # Convert entries to dict, ensuring timestamps preserve microseconds
        entries_dict = []
        for row in entries:
            entry = dict(row)
            # Convert timestamp to ISO format with microseconds if it's a datetime
            if 'time' in entry and entry['time']:
                if hasattr(entry['time'], 'isoformat'):
                    # Preserve microseconds in ISO format
                    entry['time'] = entry['time'].isoformat()
            entries_dict.append(entry)
        
        return jsonify({
            'entries': entries_dict,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': (total + per_page - 1) // per_page,
            'data_types': data_types
        })
        
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run_depth')
def get_run_depth():
    """API endpoint to get depth data for a specific run (legacy, uses query param)."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        log_file_id = request.args.get('log_file_id', type=int)
        if not log_file_id:
            return jsonify({'error': 'log_file_id is required'}), 400
        
        # Get depth data for this run
        query = """
            SELECT 
                le.time,
                le.value as depth
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.log_file_id = %s
              AND dtc.data_type = 'Depth'
            ORDER BY le.time ASC
        """
        cursor.execute(query, (log_file_id,))
        depth_data = cursor.fetchall()
        
        # Convert to list with ISO timestamps
        result = []
        for row in depth_data:
            entry = dict(row)
            if entry['time']:
                entry['time'] = entry['time'].isoformat()
            result.append(entry)
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/videos_for_runs')
def get_videos_for_runs():
    """API endpoint to get videos for runs chart display."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        start_time = request.args.get('start_time', '')
        end_time = request.args.get('end_time', '')
        
        query = """
            SELECT 
                id,
                camera_id,
                effective_start_time,
                duration_seconds,
                filename
            FROM videos
            WHERE effective_start_time IS NOT NULL
              AND duration_seconds IS NOT NULL
              AND camera_id IS NOT NULL
        """
        params = []
        
        if start_time:
            query += " AND (effective_start_time + (duration_seconds || ' seconds')::INTERVAL) >= %s"
            params.append(start_time)
        
        if end_time:
            query += " AND effective_start_time <= %s"
            params.append(end_time)
        
        query += " ORDER BY camera_id, effective_start_time"
        
        cursor.execute(query, params)
        videos = cursor.fetchall()
        
        result = []
        for video in videos:
            effective_start = pd.Timestamp(video['effective_start_time'])
            effective_end = effective_start + pd.Timedelta(seconds=float(video['duration_seconds']))
            
            result.append({
                'id': video['id'],
                'camera_id': video['camera_id'],
                'start_time': effective_start.isoformat(),
                'end_time': effective_end.isoformat(),
                'duration_seconds': float(video['duration_seconds']),
                'filename': video['filename']
            })
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

def _check_materialized_view_exists(cursor, view_name):
    """Check if a materialized view exists."""
    cursor.execute("""
        SELECT EXISTS (
            SELECT FROM pg_matviews 
            WHERE schemaname = 'public' AND matviewname = %s
        ) as exists
    """, (view_name,))
    result = cursor.fetchone()
    return result['exists'] if result else False

def _get_data_type_id(cursor, data_type_name):
    """Get data_type_id for a given data type name."""
    cursor.execute("SELECT id FROM data_type_catalog WHERE data_type = %s", (data_type_name,))
    result = cursor.fetchone()
    return result['id'] if result else None

@app.route('/api/run/<int:run_id>/depth')
@monitor_performance
def get_run_depth_by_id(run_id):
    """API endpoint to get depth data for a specific AUV run.
    
    Query parameters:
        sample: Optional. If provided, sample data at this interval in seconds (e.g., ?sample=1 for 1 point/second)
    """
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        sample_interval = request.args.get('sample', type=float)
        print(f"[DEBUG] get_run_depth_by_id: run_id={run_id}, sample_interval={sample_interval}")
        
        # Check if materialized view exists
        use_materialized_view = False
        try:
            use_materialized_view = _check_materialized_view_exists(cursor, 'mv_run_depth')
            print(f"[DEBUG] Materialized view exists: {use_materialized_view}")
        except Exception as e:
            print(f"Warning: Could not check for materialized view: {e}")
            import traceback
            traceback.print_exc()
        
        if sample_interval and sample_interval > 0:
            # Sampling requested - use materialized view if available, otherwise log_entries
            depth_data = []
            
            if use_materialized_view:
                try:
                    # Use materialized view with sampling
                    query = """
                        SELECT DISTINCT ON (time_bucket)
                            time,
                            depth
                        FROM (
                            SELECT 
                                time,
                                depth,
                                FLOOR(EXTRACT(EPOCH FROM time) / %s) as time_bucket
                            FROM mv_run_depth
                            WHERE log_file_id = %s
                        ) subq
                        ORDER BY time_bucket, time ASC
                    """
                    print(f"[DEBUG] Executing depth query (mv) with sample_interval={sample_interval}, run_id={run_id}")
                    cursor.execute(query, (sample_interval, run_id))
                    depth_data = cursor.fetchall()
                    print(f"[DEBUG] Depth query (mv) returned {len(depth_data)} rows")
                except Exception as e:
                    print(f"Error querying materialized view with sampling, falling back: {e}")
                    import traceback
                    traceback.print_exc()
            
            # Fallback to log_entries if materialized view not available or failed
            if not depth_data:
                depth_type_id = _get_data_type_id(cursor, 'Depth')
                if depth_type_id:
                    try:
                        query = """
                            SELECT DISTINCT ON (time_bucket)
                                time,
                                depth
                            FROM (
                                SELECT 
                                    le.time,
                                    le.value as depth,
                                    FLOOR(EXTRACT(EPOCH FROM le.time) / %s) as time_bucket
                                FROM log_entries le
                                JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                                WHERE le.log_file_id = %s
                                  AND dtc.data_type = 'Depth'
                            ) subq
                            ORDER BY time_bucket, time ASC
                        """
                        print(f"[DEBUG] Executing depth query (log_entries) with sample_interval={sample_interval}, run_id={run_id}")
                        cursor.execute(query, (sample_interval, run_id))
                        depth_data = cursor.fetchall()
                        print(f"[DEBUG] Depth query (log_entries) returned {len(depth_data)} rows")
                    except Exception as e:
                        print(f"Error in depth query (log_entries): {e}")
                        import traceback
                        traceback.print_exc()
                        raise
                else:
                    print(f"[WARNING] Depth data type not found in catalog")
                    depth_data = []
        else:
            # Get all depth data - use materialized view if available
            if use_materialized_view:
                try:
                    query = """
                        SELECT time, depth
                        FROM mv_run_depth
                        WHERE log_file_id = %s
                        ORDER BY time ASC
                    """
                    cursor.execute(query, (run_id,))
                    depth_data = cursor.fetchall()
                except Exception as e:
                    print(f"Error querying materialized view, falling back: {e}")
                    use_materialized_view = False
                    depth_data = []
            else:
                depth_data = []
            
            # Fallback to original query if materialized view failed
            if not depth_data:
                query = """
                    SELECT 
                        le.time,
                        le.value as depth
                    FROM log_entries le
                    JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                    WHERE le.log_file_id = %s
                      AND dtc.data_type = 'Depth'
                    ORDER BY le.time ASC
                """
                cursor.execute(query, (run_id,))
                depth_data = cursor.fetchall()
        
        # Convert to list with ISO timestamps
        result = []
        for row in depth_data:
            entry = dict(row)
            if entry['time']:
                entry['time'] = entry['time'].isoformat()
            result.append(entry)
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/target_depth')
def get_run_target_depth(run_id):
    """API endpoint to get target depth data for a specific AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # First, check what target depth data types exist in the catalog
        cursor.execute("""
            SELECT data_type 
            FROM data_type_catalog 
            WHERE LOWER(data_type) LIKE '%target%depth%' 
               OR LOWER(data_type) LIKE '%depth%target%'
            ORDER BY data_type
        """)
        available_types = cursor.fetchall()
        print(f"[get_run_target_depth] Available target depth types: {[r['data_type'] for r in available_types]}")
        
        # Check if materialized view exists
        use_materialized_view = _check_materialized_view_exists(cursor, 'mv_run_target_depth')
        
        if use_materialized_view:
            query = """
                SELECT time, target_depth
                FROM mv_run_target_depth
                WHERE log_file_id = %s
                ORDER BY time ASC
            """
            cursor.execute(query, (run_id,))
            target_depth_data = cursor.fetchall()
        else:
            # Try different possible names for target depth
            # First try 'Target_depth'
            query = """
                SELECT 
                    le.time,
                    le.value as target_depth
                FROM log_entries le
                JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                WHERE le.log_file_id = %s
                  AND dtc.data_type = 'Target_depth'
                ORDER BY le.time ASC
            """
            cursor.execute(query, (run_id,))
            target_depth_data = cursor.fetchall()
            
            # If no results, try 'Target depth' (with space)
            if not target_depth_data:
                query = """
                    SELECT 
                        le.time,
                        le.value as target_depth
                    FROM log_entries le
                    JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                    WHERE le.log_file_id = %s
                      AND dtc.data_type = 'Target depth'
                    ORDER BY le.time ASC
                """
                cursor.execute(query, (run_id,))
                target_depth_data = cursor.fetchall()
            
            # If still no results, try case-insensitive search
            if not target_depth_data:
                query = """
                    SELECT 
                        le.time,
                        le.value as target_depth
                    FROM log_entries le
                    JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                    WHERE le.log_file_id = %s
                      AND LOWER(dtc.data_type) LIKE '%target%depth%'
                    ORDER BY le.time ASC
                """
                cursor.execute(query, (run_id,))
                target_depth_data = cursor.fetchall()
        
        print(f"[get_run_target_depth] Found {len(target_depth_data)} target depth points for run {run_id}")
        
        # Convert to list with ISO timestamps
        result = []
        for row in target_depth_data:
            entry = dict(row)
            if entry['time']:
                entry['time'] = entry['time'].isoformat()
            result.append(entry)
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/altitude')
@monitor_performance
def get_run_altitude(run_id):
    """API endpoint to get altitude data for a specific AUV run.
    
    Query parameters:
        sample: Optional. Sample interval in seconds (e.g., ?sample=1 for 1 point/second)
    """
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        sample_interval = request.args.get('sample', type=float)
        altitude_types = ['Altitude OA filt', 'Altitude Kogger', 'Altitude Kogger raw', 'Altitude OA', 'Target_altitude']
        
        if sample_interval and sample_interval > 0:
            # Use sampling for better performance
            placeholders = ','.join(['%s'] * len(altitude_types))
            query = f"""
                SELECT DISTINCT ON (data_type, time_bucket)
                    time,
                    data_type,
                    altitude
                FROM (
                    SELECT 
                        le.time,
                        dtc.data_type,
                        le.value as altitude,
                        FLOOR(EXTRACT(EPOCH FROM le.time) / %s) as time_bucket
                    FROM log_entries le
                    JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                    WHERE le.log_file_id = %s
                      AND dtc.data_type IN ({placeholders})
                ) subq
                ORDER BY data_type, time_bucket, time ASC
            """
            cursor.execute(query, [sample_interval, run_id] + altitude_types)
        else:
            # Get all data (no sampling)
            placeholders = ','.join(['%s'] * len(altitude_types))
            query = f"""
                SELECT 
                    le.time,
                    dtc.data_type,
                    le.value as altitude
                FROM log_entries le
                JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                WHERE le.log_file_id = %s
                  AND dtc.data_type IN ({placeholders})
                ORDER BY le.time ASC, dtc.data_type ASC
            """
            cursor.execute(query, [run_id] + altitude_types)
        
        altitude_data = cursor.fetchall()
        
        # Group by data type
        result = {}
        for row in altitude_data:
            data_type = row['data_type']
            if data_type not in result:
                result[data_type] = []
            
            entry = {
                'time': row['time'].isoformat() if row['time'] else None,
                'value': row['altitude']  # Use 'value' for consistency with other endpoints
            }
            result[data_type].append(entry)
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/battery')
@monitor_performance
def get_run_battery(run_id):
    """API endpoint to get battery data (battcurrent, battlevel, batVoltage) for a specific AUV run.
    
    Query parameters:
        sample: Optional. Sample interval in seconds (e.g., ?sample=1 for 1 point/second)
    """
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        sample_interval = request.args.get('sample', type=float)
        battery_types = ['BattCurrent', 'BattLevel', 'BattVoltage']
        
        if sample_interval and sample_interval > 0:
            # Use sampling for better performance
            placeholders = ','.join(['%s'] * len(battery_types))
            query = f"""
                SELECT DISTINCT ON (data_type, time_bucket)
                    time,
                    data_type,
                    battery_value
                FROM (
                    SELECT 
                        le.time,
                        dtc.data_type,
                        le.value as battery_value,
                        FLOOR(EXTRACT(EPOCH FROM le.time) / %s) as time_bucket
                    FROM log_entries le
                    JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                    WHERE le.log_file_id = %s
                      AND dtc.data_type IN ({placeholders})
                ) subq
                ORDER BY data_type, time_bucket, time ASC
            """
            cursor.execute(query, [sample_interval, run_id] + battery_types)
        else:
            # Get all data (no sampling)
            placeholders = ','.join(['%s'] * len(battery_types))
            query = f"""
                SELECT 
                    le.time,
                    dtc.data_type,
                    le.value as battery_value
                FROM log_entries le
                JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                WHERE le.log_file_id = %s
                  AND dtc.data_type IN ({placeholders})
                ORDER BY le.time ASC, dtc.data_type ASC
            """
            cursor.execute(query, [run_id] + battery_types)
        
        battery_data = cursor.fetchall()
        
        # Group by data type
        result = {}
        for row in battery_data:
            data_type = row['data_type']
            if data_type not in result:
                result[data_type] = []
            
            entry = {
                'time': row['time'].isoformat() if row['time'] else None,
                'value': row['battery_value']
            }
            result[data_type].append(entry)
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/imu2')
@monitor_performance
def get_run_imu2(run_id):
    """API endpoint to get IMU2 acceleration data (SCALED_IMU2_xacc, SCALED_IMU2_yacc, SCALED_IMU2_zacc) for a specific AUV run.
    
    Query parameters:
        sample: Optional. Sample interval in seconds (e.g., ?sample=1 for 1 point/second)
    """
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        sample_interval = request.args.get('sample', type=float)
        imu2_types = ['SCALED_IMU2_xacc', 'SCALED_IMU2_yacc', 'SCALED_IMU2_zacc']
        
        if sample_interval and sample_interval > 0:
            # Use sampling for better performance
            placeholders = ','.join(['%s'] * len(imu2_types))
            query = f"""
                SELECT DISTINCT ON (data_type, time_bucket)
                    time,
                    data_type,
                    imu2_value
                FROM (
                    SELECT 
                        le.time,
                        dtc.data_type,
                        le.value as imu2_value,
                        FLOOR(EXTRACT(EPOCH FROM le.time) / %s) as time_bucket
                    FROM log_entries le
                    JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                    WHERE le.log_file_id = %s
                      AND dtc.data_type IN ({placeholders})
                ) subq
                ORDER BY data_type, time_bucket, time ASC
            """
            cursor.execute(query, [sample_interval, run_id] + imu2_types)
        else:
            # Get all data (no sampling)
            placeholders = ','.join(['%s'] * len(imu2_types))
            query = f"""
                SELECT 
                    le.time,
                    dtc.data_type,
                    le.value as imu2_value
                FROM log_entries le
                JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                WHERE le.log_file_id = %s
                  AND dtc.data_type IN ({placeholders})
                ORDER BY le.time ASC, dtc.data_type ASC
            """
            cursor.execute(query, [run_id] + imu2_types)
        
        imu2_data = cursor.fetchall()
        
        # Group by data type
        result = {}
        for row in imu2_data:
            data_type = row['data_type']
            if data_type not in result:
                result[data_type] = []
            
            entry = {
                'time': row['time'].isoformat() if row['time'] else None,
                'value': row['imu2_value']
            }
            result[data_type].append(entry)
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/temperature')
@monitor_performance
def get_run_temperature(run_id):
    """API endpoint to get temperature data for a specific AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Get all temperature data types for this run
        temperature_types = ['Temperature de l\'eau', 'Temperature intérieur']
        
        # Build query to get all temperature data
        placeholders = ','.join(['%s'] * len(temperature_types))
        query = f"""
            SELECT 
                le.time,
                dtc.data_type,
                le.value as temperature_value
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.log_file_id = %s
              AND dtc.data_type IN ({placeholders})
            ORDER BY le.time ASC, dtc.data_type ASC
        """
        cursor.execute(query, [run_id] + temperature_types)
        temperature_data = cursor.fetchall()
        
        # Group by data type
        result = {}
        for row in temperature_data:
            data_type = row['data_type']
            if data_type not in result:
                result[data_type] = []
            
            entry = {
                'time': row['time'].isoformat() if row['time'] else None,
                'value': row['temperature_value']
            }
            result[data_type].append(entry)
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/motors')
@monitor_performance
def get_run_motors(run_id):
    """API endpoint to get motor data (M1-M8) for a specific AUV run.
    
    Query parameters:
        sample: Optional. If provided, sample data at this interval in seconds
    """
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        sample_interval = request.args.get('sample', type=float)
        motor_types = ['M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'M7', 'M8']
        
        # Check if materialized view exists
        use_materialized_view = False
        try:
            use_materialized_view = _check_materialized_view_exists(cursor, 'mv_run_motors')
        except Exception as e:
            print(f"Warning: Could not check for materialized view: {e}")
        
        if sample_interval and sample_interval > 0:
            # For now, skip optimized structures and use direct query to avoid errors
            # TODO: Re-enable optimized structures once they are properly set up
            try:
                placeholders = ','.join(['%s'] * len(motor_types))
                # Use a subquery to avoid parameter issues with DISTINCT ON
                query = f"""
                    SELECT DISTINCT ON (data_type, time_bucket)
                        time,
                        data_type,
                        motor_value
                    FROM (
                        SELECT 
                            le.time,
                            dtc.data_type,
                            le.value as motor_value,
                            FLOOR(EXTRACT(EPOCH FROM le.time) / %s) as time_bucket
                        FROM log_entries le
                        JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                        WHERE le.log_file_id = %s
                          AND dtc.data_type IN ({placeholders})
                    ) subq
                    ORDER BY data_type, time_bucket, time ASC
                """
                print(f"[DEBUG] Executing motors query with sample_interval={sample_interval}, run_id={run_id}")
                cursor.execute(query, [sample_interval, run_id] + motor_types)
                motor_data = cursor.fetchall()
                print(f"[DEBUG] Motors query returned {len(motor_data)} rows")
            except Exception as e:
                print(f"Error in motors query: {e}")
                import traceback
                traceback.print_exc()
                raise
        else:
            # Get all motor data - use materialized view if available
            motor_data = []
            if use_materialized_view:
                try:
                    placeholders = ','.join(['%s'] * len(motor_types))
                    query = f"""
                        SELECT time, data_type, motor_value
                        FROM mv_run_motors
                        WHERE log_file_id = %s
                          AND data_type IN ({placeholders})
                        ORDER BY time ASC, data_type ASC
                    """
                    cursor.execute(query, [run_id] + motor_types)
                    motor_data = cursor.fetchall()
                except Exception as e:
                    print(f"Error querying materialized view, falling back: {e}")
                    use_materialized_view = False
                    motor_data = []
            
            # Fallback to original query if materialized view failed
            if not motor_data:
                placeholders = ','.join(['%s'] * len(motor_types))
                query = f"""
                    SELECT 
                        le.time,
                        dtc.data_type,
                        le.value as motor_value
                    FROM log_entries le
                    JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                    WHERE le.log_file_id = %s
                      AND dtc.data_type IN ({placeholders})
                    ORDER BY le.time ASC, dtc.data_type ASC
                """
                cursor.execute(query, [run_id] + motor_types)
                motor_data = cursor.fetchall()
        
        # Group by data type
        result = {}
        for row in motor_data:
            data_type = row['data_type']
            if data_type not in result:
                result[data_type] = []
            
            entry = {
                'time': row['time'].isoformat() if row['time'] else None,
                'value': row['motor_value']
            }
            result[data_type].append(entry)
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        error_traceback = traceback.format_exc()
        print(f"[ERROR] get_run_motors failed for run_id={run_id}: {e}")
        print(f"[ERROR] Traceback:\n{error_traceback}")
        return jsonify({'error': str(e), 'traceback': error_traceback}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/state')
def get_run_state(run_id):
    """API endpoint to get StateNb data for a specific AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Check if materialized view exists
        use_materialized_view = False
        try:
            use_materialized_view = _check_materialized_view_exists(cursor, 'mv_run_state')
        except Exception as e:
            print(f"Warning: Could not check for materialized view: {e}")
        
        state_data = []
        if use_materialized_view:
            try:
                query = """
                    SELECT time, state as value
                    FROM mv_run_state
                    WHERE log_file_id = %s
                    ORDER BY time ASC
                """
                cursor.execute(query, (run_id,))
                state_data = cursor.fetchall()
            except Exception as e:
                print(f"Error querying materialized view, falling back: {e}")
                use_materialized_view = False
                state_data = []
        
        # Fallback to original query if materialized view failed
        if not state_data:
            query = """
                SELECT 
                    le.time,
                    le.value
                FROM log_entries le
                JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                WHERE le.log_file_id = %s
                  AND dtc.data_type = 'StateNb'
                ORDER BY le.time ASC
            """
            cursor.execute(query, (run_id,))
            state_data = cursor.fetchall()
        
        # Convert to list with ISO timestamps
        result = []
        for row in state_data:
            entry = dict(row)
            if entry['time']:
                entry['time'] = entry['time'].isoformat()
            result.append(entry)
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/bearing_ping')
def get_run_bearing_ping(run_id):
    """API endpoint to get BearingPing data for a specific AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Get BearingPing data for this run
        query = """
            SELECT 
                le.time,
                le.value
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.log_file_id = %s
              AND dtc.data_type = 'BearingPing'
            ORDER BY le.time ASC
        """
        cursor.execute(query, (run_id,))
        bearing_ping_data = cursor.fetchall()
        
        # Convert to list with ISO timestamps
        result = []
        for row in bearing_ping_data:
            entry = dict(row)
            if entry['time']:
                entry['time'] = entry['time'].isoformat()
            result.append(entry)
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/usv_position')
def get_run_usv_position(run_id):
    """API endpoint to get USV GPS position data for the time period of an AUV run."""
    # #region agent log
    import json
    with open(str(get_debug_log_path()), 'a') as f:
        f.write(json.dumps({"sessionId":"debug-session","runId":"api","hypothesisId":"A","location":"app.py:600","message":"API called","data":{"run_id":run_id},"timestamp":int(time.time()*1000)}) + '\n')
    # #endregion
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # First, get the time range of the AUV run
        cursor.execute("""
            SELECT first_timestamp, last_timestamp
            FROM log_files
            WHERE id = %s AND vehicle_type = 'AUV'
        """, (run_id,))
        
        run_info = cursor.fetchone()
        if not run_info:
            # #region agent log
            with open(str(get_debug_log_path()), 'a') as f:
                f.write(json.dumps({"sessionId":"debug-session","runId":"api","hypothesisId":"A","location":"app.py:612","message":"Run not found","data":{"run_id":run_id},"timestamp":int(time.time()*1000)}) + '\n')
            # #endregion
            return jsonify({'error': 'Run not found'}), 404
        
        start_time = run_info['first_timestamp']
        end_time = run_info['last_timestamp']
        
        # #region agent log
        with open(str(get_debug_log_path()), 'a') as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"api","hypothesisId":"C","location":"app.py:618","message":"AUV run time range","data":{"start_time":str(start_time),"end_time":str(end_time)},"timestamp":int(time.time()*1000)}) + '\n')
        # #endregion
        
        # Debug: print time range
        print(f"USV Position Query: Looking for data between {start_time} and {end_time}")
        
        # #region agent log - Hypothesis D: Check if USV data exists with different vehicle_type
        cursor.execute("""
            SELECT DISTINCT vehicle_type, COUNT(*) as count
            FROM log_entries
            WHERE time >= %s AND time <= %s
            GROUP BY vehicle_type
        """, (start_time, end_time))
        vehicle_types = cursor.fetchall()
        with open(str(get_debug_log_path()), 'a') as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"api","hypothesisId":"D","location":"app.py:625","message":"Vehicle types in time range","data":{"vehicle_types":[{"type":v['vehicle_type'],"count":v['count']} for v in vehicle_types]},"timestamp":int(time.time()*1000)}) + '\n')
        # #endregion
        
        # #region agent log - Hypothesis B: Check what GPS data types exist for USV
        cursor.execute("""
            SELECT DISTINCT dtc.data_type, COUNT(*) as count
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.vehicle_type = 'USV'
              AND dtc.data_type LIKE 'GPS%%'
              AND le.time >= %s
              AND le.time <= %s
            GROUP BY dtc.data_type
        """, (start_time, end_time))
        gps_types = cursor.fetchall()
        with open(str(get_debug_log_path()), 'a') as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"api","hypothesisId":"B","location":"app.py:635","message":"GPS data types for USV in time range","data":{"gps_types":[{"type":g['data_type'],"count":g['count']} for g in gps_types]},"timestamp":int(time.time()*1000)}) + '\n')
        # #endregion
        
        # #region agent log - Hypothesis A: Check if any USV navigation data exists at all
        cursor.execute("""
            SELECT COUNT(*) as total_count,
                   MIN(time) as min_time,
                   MAX(time) as max_time
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.vehicle_type = 'USV'
              AND dtc.data_type IN ('GPS_RAW_INT_lat', 'GPS_RAW_INT_lon')
        """)
        usv_gps_summary = cursor.fetchone()
        with open(str(get_debug_log_path()), 'a') as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"api","hypothesisId":"A","location":"app.py:645","message":"USV GPS data summary (all time)","data":{"total_count":usv_gps_summary['total_count'],"min_time":str(usv_gps_summary['min_time']) if usv_gps_summary['min_time'] else None,"max_time":str(usv_gps_summary['max_time']) if usv_gps_summary['max_time'] else None},"timestamp":int(time.time()*1000)}) + '\n')
        # #endregion
        
        # Get USV GPS position data for this time period
        # USV navigation logs use 'Lat' and 'Lon' (not 'GPS_RAW_INT_lat'/'GPS_RAW_INT_lon')
        # Try both naming conventions for compatibility
        query = """
            SELECT 
                le.time,
                le.value as lat_raw
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.vehicle_type = 'USV'
              AND dtc.data_type IN ('GPS_RAW_INT_lat', 'Lat', 'RAW_Lat')
              AND le.time >= %s
              AND le.time <= %s
            ORDER BY le.time ASC
        """
        cursor.execute(query, (start_time, end_time))
        lat_data = cursor.fetchall()
        
        # #region agent log
        with open(str(get_debug_log_path()), 'a') as f:
            sample_lats = [{"time":str(l['time']),"value":l['lat_raw']} for l in lat_data[:3]] if lat_data else []
            f.write(json.dumps({"sessionId":"debug-session","runId":"api","hypothesisId":"A","location":"app.py:660","message":"Latitude query result","data":{"count":len(lat_data),"samples":sample_lats},"timestamp":int(time.time()*1000)}) + '\n')
        # #endregion
        
        print(f"Found {len(lat_data)} latitude points")
        
        # Get longitude data
        query = """
            SELECT 
                le.time,
                le.value as lon_raw
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.vehicle_type = 'USV'
              AND dtc.data_type IN ('GPS_RAW_INT_lon', 'Lon', 'RAW_Lon')
              AND le.time >= %s
              AND le.time <= %s
            ORDER BY le.time ASC
        """
        cursor.execute(query, (start_time, end_time))
        lon_data = cursor.fetchall()
        
        # #region agent log
        with open(str(get_debug_log_path()), 'a') as f:
            sample_lons = [{"time":str(l['time']),"value":l['lon_raw']} for l in lon_data[:3]] if lon_data else []
            f.write(json.dumps({"sessionId":"debug-session","runId":"api","hypothesisId":"A","location":"app.py:675","message":"Longitude query result","data":{"count":len(lon_data),"samples":sample_lons},"timestamp":int(time.time()*1000)}) + '\n')
        # #endregion
        
        print(f"Found {len(lon_data)} longitude points")
        
        # Combine lat and lon by matching closest timestamps
        # Since timestamps may not match exactly, we'll use a time window approach
        # For each lat point, find the closest lon point within a small time window (e.g., 1 second)
        result = []
        
        # Convert to lists with timestamps as epoch for easier comparison
        # Filter out None values for both time and lat_raw/lon_raw
        lat_list = [(row['time'], row['lat_raw']) for row in lat_data if row['time'] and row['lat_raw'] is not None]
        lon_list = [(row['time'], row['lon_raw']) for row in lon_data if row['time'] and row['lon_raw'] is not None]
        
        # Use a simple approach: for each lat point, find the lon point with the closest timestamp
        lon_idx = 0
        for lat_time, lat_raw in lat_list:
            lat_epoch = lat_time.timestamp()
            
            # Find the closest lon point
            best_lon = None
            best_diff = float('inf')
            
            # Search forward from current position (lon_list is sorted)
            for i in range(lon_idx, len(lon_list)):
                lon_time, lon_raw = lon_list[i]
                lon_epoch = lon_time.timestamp()
                diff = abs(lat_epoch - lon_epoch)
                
                # If we're getting further away, stop searching forward
                if diff > best_diff and i > lon_idx:
                    break
                    
                if diff < best_diff:
                    best_diff = diff
                    best_lon = (lon_time, lon_raw)
                    lon_idx = i  # Update starting position for next search
            
            # Also check backwards a bit (in case we skipped some)
            for i in range(max(0, lon_idx - 10), lon_idx):
                lon_time, lon_raw = lon_list[i]
                lon_epoch = lon_time.timestamp()
                diff = abs(lat_epoch - lon_epoch)
                
                if diff < best_diff:
                    best_diff = diff
                    best_lon = (lon_time, lon_raw)
            
            # Only include if we found a match within 1 second
            if best_lon and best_diff < 1.0:
                lon_time, lon_raw = best_lon
                # Use the lat timestamp as the reference time
                # Note: Lat and Lon can be in different formats!
                # Check each value independently for GPS_RAW_INT format (large numbers > 1e6) or degrees
                # Also check for None values to avoid errors
                if lat_raw is not None and lon_raw is not None:
                    # Convert lat independently
                    if abs(lat_raw) > 1e6:
                        # GPS_RAW_INT format: divide by 1e7
                        lat_degrees = lat_raw / 1e7
                    else:
                        # Already in degrees
                        lat_degrees = lat_raw
                    
                    # Convert lon independently (may be different format than lat!)
                    if abs(lon_raw) > 1e6:
                        # GPS_RAW_INT format: divide by 1e7
                        lon_degrees = lon_raw / 1e7
                    else:
                        # Already in degrees
                        lon_degrees = lon_raw
                    
                    # #region agent log - Hypothesis E: Check if coordinates are (0,0)
                    if lat_degrees == 0 and lon_degrees == 0:
                        with open(str(get_debug_log_path()), 'a') as f:
                            f.write(json.dumps({"sessionId":"debug-session","runId":"api","hypothesisId":"E","location":"app.py:705","message":"Found (0,0) coordinate","data":{"time":lat_time.isoformat(),"lat_raw":lat_raw,"lon_raw":lon_raw},"timestamp":int(time.time()*1000)}) + '\n')
                    # #endregion
                    
                    result.append({
                        'time': lat_time.isoformat(),
                        'lat': lat_degrees,
                        'lon': lon_degrees
                    })
        
        # #region agent log
        with open(str(get_debug_log_path()), 'a') as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"api","hypothesisId":"A","location":"app.py:715","message":"Final result","data":{"result_count":len(result),"sample_results":result[:3] if result else []},"timestamp":int(time.time()*1000)}) + '\n')
        # #endregion
        
        print(f"Returning {len(result)} position points (matched within 1 second)")
        return jsonify(result)
        
    except Exception as e:
        # #region agent log
        import traceback
        error_msg = str(e)
        error_traceback = traceback.format_exc()
        with open(str(get_debug_log_path()), 'a') as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"api","hypothesisId":"ERROR","location":"app.py:usv_position","message":"Exception in usv_position","data":{"error":error_msg,"traceback":error_traceback},"timestamp":int(time.time()*1000)}) + '\n')
        # #endregion
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
        
    finally:
        cursor.close()
        conn.close()

@app.route('/api/runs')
def get_runs():
    """API endpoint to get AUV runs (navigation files with start/end times)."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        vehicle_type = request.args.get('vehicle_type', '')
        start_time = request.args.get('start_time', '')
        end_time = request.args.get('end_time', '')
        
        query = """
            SELECT 
                lf.id,
                lf.filename,
                lf.vehicle_type,
                lf.vehicle_id,
                lf.first_timestamp,
                lf.last_timestamp,
                lf.row_count,
                EXTRACT(EPOCH FROM (lf.last_timestamp - lf.first_timestamp)) as duration_seconds
            FROM log_files lf
            WHERE lf.import_status = 'completed'
              AND lf.first_timestamp IS NOT NULL
              AND lf.last_timestamp IS NOT NULL
        """
        params = []
        
        if vehicle_type:
            query += " AND lf.vehicle_type = %s"
            params.append(vehicle_type)
        
        if start_time:
            query += " AND lf.last_timestamp >= %s"
            params.append(start_time)
        
        if end_time:
            query += " AND lf.first_timestamp <= %s"
            params.append(end_time)
        
        query += " ORDER BY lf.first_timestamp ASC"
        
        cursor.execute(query, params)
        runs = cursor.fetchall()
        
        # Convert to dict and format timestamps
        runs_list = []
        for run in runs:
            run_dict = dict(run)
            
            # For AUV runs, calculate actual mission times from depth data
            if run_dict['vehicle_type'] == 'AUV' and run_dict['id']:
                # Get actual mission start/end from depth data
                depth_query = """
                    SELECT 
                        MIN(le.time) as actual_start,
                        MAX(le.time) as actual_end
                    FROM log_entries le
                    JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                    WHERE le.log_file_id = %s
                      AND dtc.data_type = 'Depth'
                """
                cursor.execute(depth_query, (run_dict['id'],))
                depth_result = cursor.fetchone()
                
                if depth_result and depth_result['actual_start'] and depth_result['actual_end']:
                    # Use actual mission times from depth data, but ensure they're within file timestamps
                    file_start = run_dict['first_timestamp']
                    file_end = run_dict['last_timestamp']
                    depth_start = depth_result['actual_start']
                    depth_end = depth_result['actual_end']
                    
                    # Use depth timestamps, but extend to file timestamps if depth range is shorter
                    # This ensures the full run is visible even if depth data doesn't cover the entire file
                    final_start = min(file_start, depth_start) if file_start else depth_start
                    final_end = max(file_end, depth_end) if file_end else depth_end
                    
                    run_dict['first_timestamp'] = final_start.isoformat()
                    run_dict['last_timestamp'] = final_end.isoformat()
                    # Recalculate duration
                    duration = (final_end - final_start).total_seconds()
                    run_dict['duration_seconds'] = duration
                else:
                    # Fallback to file timestamps if no depth data
                    if run_dict['first_timestamp']:
                        run_dict['first_timestamp'] = run_dict['first_timestamp'].isoformat()
                    if run_dict['last_timestamp']:
                        run_dict['last_timestamp'] = run_dict['last_timestamp'].isoformat()
            else:
                # For non-AUV runs, use file timestamps
                if run_dict['first_timestamp']:
                    run_dict['first_timestamp'] = run_dict['first_timestamp'].isoformat()
                if run_dict['last_timestamp']:
                    run_dict['last_timestamp'] = run_dict['last_timestamp'].isoformat()
            
            runs_list.append(run_dict)
        
        return jsonify(runs_list)
        
    finally:
        cursor.close()
        conn.close()

@app.route('/api/files_by_category')
def get_files_by_category():
    """API endpoint to get files grouped by vehicle and file category."""
    conn = None
    cursor = None
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Check if table exists
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name = 'log_files'
            ) as exists
        """)
        table_exists = cursor.fetchone()['exists']
        
        if not table_exists:
            return jsonify({}), 200
        
        vehicle_type = request.args.get('vehicle_type', '')
        start_time = request.args.get('start_time', '')
        end_time = request.args.get('end_time', '')
        
        query = """
            SELECT 
                lf.id,
                lf.filename,
                lf.vehicle_type,
                lf.vehicle_id,
                lf.first_timestamp,
                lf.last_timestamp,
                lf.row_count
            FROM log_files lf
            WHERE lf.import_status = 'completed'
              AND lf.first_timestamp IS NOT NULL
              AND lf.last_timestamp IS NOT NULL
              AND LOWER(lf.filename) NOT LIKE '%%settings%%'
        """
        params = []
        
        if vehicle_type:
            query += " AND lf.vehicle_type = %s"
            params.append(vehicle_type)
        
        if start_time:
            query += " AND lf.last_timestamp >= %s"
            params.append(start_time)
        
        if end_time:
            query += " AND lf.first_timestamp <= %s"
            params.append(end_time)
        
        query += " ORDER BY COALESCE(lf.vehicle_id, 'Unknown'), lf.first_timestamp ASC"
        
        cursor.execute(query, params)
        files = cursor.fetchall()
        
        # Helper function to determine file category from filename
        def get_file_category(filename):
            if not filename:
                return 'navigation'
            filename_lower = filename.lower()
            if 'navigation' in filename_lower:
                return 'navigation'
            elif 'usbl' in filename_lower:
                return 'usbl'
            elif 'full' in filename_lower:
                return 'usv_full'
            else:
                # Default to navigation for files that don't match patterns
                return 'navigation'
        
        # Group files by vehicle_id, then by file_category
        result = {}
        for file in files:
            try:
                file_dict = dict(file)
                vehicle_id = file_dict.get('vehicle_id') or 'Unknown'
                file_category = get_file_category(file_dict.get('filename'))
                
                # Initialize vehicle if not exists
                if vehicle_id not in result:
                    result[vehicle_id] = {
                        'navigation': [],
                        'usbl': [],
                        'usv_full': []
                    }
                
                # Format timestamps - handle both datetime objects and strings
                if file_dict.get('first_timestamp'):
                    timestamp = file_dict['first_timestamp']
                    if hasattr(timestamp, 'isoformat'):
                        file_dict['first_timestamp'] = timestamp.isoformat()
                    elif isinstance(timestamp, str):
                        # Already a string, keep it
                        pass
                    else:
                        file_dict['first_timestamp'] = str(timestamp)
                else:
                    file_dict['first_timestamp'] = None
                    
                if file_dict.get('last_timestamp'):
                    timestamp = file_dict['last_timestamp']
                    if hasattr(timestamp, 'isoformat'):
                        file_dict['last_timestamp'] = timestamp.isoformat()
                    elif isinstance(timestamp, str):
                        # Already a string, keep it
                        pass
                    else:
                        file_dict['last_timestamp'] = str(timestamp)
                else:
                    file_dict['last_timestamp'] = None
                
                # Add file_category to file dict
                file_dict['file_category'] = file_category
                
                # Add to appropriate category
                result[vehicle_id][file_category].append(file_dict)
            except Exception as file_error:
                # Log error but continue processing other files
                import traceback
                print(f"Error processing file: {file_error}")
                traceback.print_exc()
                continue
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        error_msg = str(e)
        error_traceback = traceback.format_exc()
        print(f"Error in get_files_by_category: {error_msg}")
        print(error_traceback)
        return jsonify({'error': error_msg}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route('/api/stats')
def get_stats():
    """Get database statistics."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        stats = {}
        
        # Total entries
        cursor.execute("SELECT COUNT(*) as total FROM log_entries")
        stats['total_entries'] = cursor.fetchone()['total']
        
        # Entries by vehicle type
        cursor.execute("""
            SELECT vehicle_type, COUNT(*) as count
            FROM log_entries
            GROUP BY vehicle_type
        """)
        stats['by_vehicle'] = {row['vehicle_type']: row['count'] for row in cursor.fetchall()}
        
        # Total files
        cursor.execute("SELECT COUNT(*) as total FROM log_files WHERE import_status = 'completed'")
        stats['total_files'] = cursor.fetchone()['total']
        
        # Time range
        cursor.execute("SELECT MIN(time) as min_time, MAX(time) as max_time FROM log_entries")
        time_range = cursor.fetchone()
        stats['time_range'] = {
            'min': str(time_range['min_time']) if time_range['min_time'] else None,
            'max': str(time_range['max_time']) if time_range['max_time'] else None
        }
        
        return jsonify(stats)
    finally:
        cursor.close()
        conn.close()

@app.route('/api/tables/log_files')
def get_table_log_files():
    """API endpoint to get log_files table data with pagination."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 100))
        vehicle_type = request.args.get('vehicle_type', '')
        import_status = request.args.get('import_status', '')
        
        offset = (page - 1) * per_page
        
        query = "SELECT * FROM log_files WHERE 1=1"
        count_query = "SELECT COUNT(*) as total FROM log_files WHERE 1=1"
        params = []
        count_params = []
        
        if vehicle_type:
            query += " AND vehicle_type = %s"
            count_query += " AND vehicle_type = %s"
            params.append(vehicle_type)
            count_params.append(vehicle_type)
        
        if import_status:
            query += " AND import_status = %s"
            count_query += " AND import_status = %s"
            params.append(import_status)
            count_params.append(import_status)
        
        query += " ORDER BY id DESC LIMIT %s OFFSET %s"
        params.extend([per_page, offset])
        
        cursor.execute(count_query, count_params)
        total = cursor.fetchone()['total']
        
        cursor.execute(query, params)
        entries = cursor.fetchall()
        
        # Convert to dict and format timestamps
        entries_list = []
        for entry in entries:
            entry_dict = dict(entry)
            # Convert timestamps to ISO format
            for key in ['import_started_at', 'import_completed_at', 'first_timestamp', 'last_timestamp', 'created_at']:
                if entry_dict.get(key):
                    entry_dict[key] = entry_dict[key].isoformat()
            entries_list.append(entry_dict)
        
        return jsonify({
            'entries': entries_list,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': (total + per_page - 1) // per_page
        })
    finally:
        cursor.close()
        conn.close()

@app.route('/api/tables/data_type_catalog')
def get_table_data_type_catalog():
    """API endpoint to get data_type_catalog table data with pagination."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 100))
        vehicle_type = request.args.get('vehicle_type', '')
        category = request.args.get('category', '')
        
        offset = (page - 1) * per_page
        
        query = "SELECT * FROM data_type_catalog WHERE 1=1"
        count_query = "SELECT COUNT(*) as total FROM data_type_catalog WHERE 1=1"
        params = []
        count_params = []
        
        if vehicle_type:
            query += " AND (vehicle_type = %s OR vehicle_type = 'BOTH')"
            count_query += " AND (vehicle_type = %s OR vehicle_type = 'BOTH')"
            params.append(vehicle_type)
            count_params.append(vehicle_type)
        
        if category:
            query += " AND category = %s"
            count_query += " AND category = %s"
            params.append(category)
            count_params.append(category)
        
        query += " ORDER BY id ASC LIMIT %s OFFSET %s"
        params.extend([per_page, offset])
        
        cursor.execute(count_query, count_params)
        total = cursor.fetchone()['total']
        
        cursor.execute(query, params)
        entries = cursor.fetchall()
        
        # Convert to dict and format timestamps
        entries_list = []
        for entry in entries:
            entry_dict = dict(entry)
            if entry_dict.get('created_at'):
                entry_dict['created_at'] = entry_dict['created_at'].isoformat()
            entries_list.append(entry_dict)
        
        return jsonify({
            'entries': entries_list,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': (total + per_page - 1) // per_page
        })
    finally:
        cursor.close()
        conn.close()

@app.route('/api/tables/log_entries')
def get_table_log_entries():
    """API endpoint to get log_entries table data with pagination and JOINs."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 100))
        vehicle_type = request.args.get('vehicle_type', '')
        data_type = request.args.get('data_type', '')
        start_time = request.args.get('start_time', '')
        end_time = request.args.get('end_time', '')
        
        offset = (page - 1) * per_page
        
        query = """
            SELECT 
                le.time,
                le.vehicle_type,
                le.vehicle_id,
                le.data_type_id,
                le.value,
                le.log_file_id,
                le.created_at,
                dtc.data_type,
                dtc.vehicle_type as data_type_vehicle_type,
                lf.filename AS source_filename
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            JOIN log_files lf ON lf.id = le.log_file_id
            WHERE 1=1
        """
        count_query = """
            SELECT COUNT(*) as total
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE 1=1
        """
        params = []
        count_params = []
        
        if vehicle_type:
            query += " AND le.vehicle_type = %s"
            count_query += " AND le.vehicle_type = %s"
            params.append(vehicle_type)
            count_params.append(vehicle_type)
        
        if data_type:
            query += " AND dtc.data_type = %s"
            count_query += " AND dtc.data_type = %s"
            params.append(data_type)
            count_params.append(data_type)
        
        if start_time:
            query += " AND le.time >= %s"
            count_query += " AND le.time >= %s"
            params.append(start_time)
            count_params.append(start_time)
        
        if end_time:
            query += " AND le.time <= %s"
            count_query += " AND le.time <= %s"
            params.append(end_time)
            count_params.append(end_time)
        
        query += " ORDER BY le.time DESC LIMIT %s OFFSET %s"
        params.extend([per_page, offset])
        
        cursor.execute(count_query, count_params)
        total = cursor.fetchone()['total']
        
        cursor.execute(query, params)
        entries = cursor.fetchall()
        
        # Convert to dict and format timestamps
        entries_list = []
        for entry in entries:
            entry_dict = dict(entry)
            if entry_dict.get('time'):
                entry_dict['time'] = entry_dict['time'].isoformat()
            if entry_dict.get('created_at'):
                entry_dict['created_at'] = entry_dict['created_at'].isoformat()
            entries_list.append(entry_dict)
        
        return jsonify({
            'entries': entries_list,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': (total + per_page - 1) // per_page
        })
    finally:
        cursor.close()
        conn.close()

@app.route('/api/tables/settings_files')
def get_table_settings_files():
    """API endpoint to get settings_files table data with pagination."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 100))
        vehicle_type = request.args.get('vehicle_type', '')
        import_status = request.args.get('import_status', '')
        
        offset = (page - 1) * per_page
        
        query = "SELECT * FROM settings_files WHERE 1=1"
        count_query = "SELECT COUNT(*) as total FROM settings_files WHERE 1=1"
        params = []
        count_params = []
        
        if vehicle_type:
            query += " AND vehicle_type = %s"
            count_query += " AND vehicle_type = %s"
            params.append(vehicle_type)
            count_params.append(vehicle_type)
        
        if import_status:
            query += " AND import_status = %s"
            count_query += " AND import_status = %s"
            params.append(import_status)
            count_params.append(import_status)
        
        query += " ORDER BY id DESC LIMIT %s OFFSET %s"
        params.extend([per_page, offset])
        
        cursor.execute(count_query, count_params)
        total = cursor.fetchone()['total']
        
        cursor.execute(query, params)
        entries = cursor.fetchall()
        
        # Convert to dict and format timestamps
        entries_list = []
        for entry in entries:
            entry_dict = dict(entry)
            # Convert timestamps to ISO format
            for key in ['import_started_at', 'import_completed_at', 'created_at']:
                if entry_dict.get(key):
                    entry_dict[key] = entry_dict[key].isoformat()
            entries_list.append(entry_dict)
        
        return jsonify({
            'entries': entries_list,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': (total + per_page - 1) // per_page
        })
    finally:
        cursor.close()
        conn.close()

@app.route('/api/tables/vehicle_settings')
def get_table_vehicle_settings():
    """API endpoint to get vehicle_settings table data with pagination and JOIN."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 100))
        settings_file_id = request.args.get('settings_file_id', '')
        category = request.args.get('category', '')
        setting_name = request.args.get('setting_name', '')
        
        offset = (page - 1) * per_page
        
        query = """
            SELECT 
                vs.*,
                sf.filename AS settings_filename,
                sf.vehicle_type,
                sf.vehicle_id
            FROM vehicle_settings vs
            JOIN settings_files sf ON sf.id = vs.settings_file_id
            WHERE 1=1
        """
        count_query = """
            SELECT COUNT(*) as total
            FROM vehicle_settings vs
            WHERE 1=1
        """
        params = []
        count_params = []
        
        if settings_file_id:
            query += " AND vs.settings_file_id = %s"
            count_query += " AND vs.settings_file_id = %s"
            params.append(settings_file_id)
            count_params.append(settings_file_id)
        
        if category:
            query += " AND vs.category = %s"
            count_query += " AND vs.category = %s"
            params.append(category)
            count_params.append(category)
        
        if setting_name:
            query += " AND vs.setting_name ILIKE %s"
            count_query += " AND vs.setting_name ILIKE %s"
            params.append(f'%{setting_name}%')
            count_params.append(f'%{setting_name}%')
        
        query += " ORDER BY vs.id DESC LIMIT %s OFFSET %s"
        params.extend([per_page, offset])
        
        cursor.execute(count_query, count_params)
        total = cursor.fetchone()['total']
        
        cursor.execute(query, params)
        entries = cursor.fetchall()
        
        # Convert to dict and format timestamps
        entries_list = []
        for entry in entries:
            entry_dict = dict(entry)
            if entry_dict.get('created_at'):
                entry_dict['created_at'] = entry_dict['created_at'].isoformat()
            entries_list.append(entry_dict)
        
        return jsonify({
            'entries': entries_list,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': (total + per_page - 1) // per_page
        })
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/usv_state')
def get_run_usv_state(run_id):
    """API endpoint to get StateNb data from USV logs for the time period of an AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # First, get the time range of the AUV run
        cursor.execute("""
            SELECT first_timestamp, last_timestamp
            FROM log_files
            WHERE id = %s AND vehicle_type = 'AUV'
        """, (run_id,))
        
        run_info = cursor.fetchone()
        if not run_info:
            return jsonify({'error': 'Run not found'}), 404
        
        start_time = run_info['first_timestamp']
        end_time = run_info['last_timestamp']
        
        # Get USV StateNb data for this time period
        query = """
            SELECT 
                le.time,
                le.value
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.vehicle_type = 'USV'
              AND dtc.data_type = 'StateNb'
              AND le.time >= %s
              AND le.time <= %s
            ORDER BY le.time ASC
        """
        cursor.execute(query, (start_time, end_time))
        state_data = cursor.fetchall()
        
        # Convert to list with ISO timestamps
        result = []
        for row in state_data:
            entry = dict(row)
            if entry['time']:
                entry['time'] = entry['time'].isoformat()
            result.append(entry)
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/orientation')
@monitor_performance
def get_run_orientation(run_id):
    """API endpoint to get Roll, Pitch, Yaw, and Heading data for a specific AUV run.
    
    Query parameters:
        sample: Optional. Sample interval in seconds (e.g., ?sample=1 for 1 point/second)
    """
    # #region agent log
    import json
    with open(r'c:\Users\ypenn\Documents\COSMA\Code\deepLog\.cursor\debug.log', 'a') as f:
        f.write(json.dumps({'location':'app.py:get_run_orientation:START','message':'Endpoint called','data':{'run_id':run_id,'sample':request.args.get('sample')},'timestamp':int(time.time()*1000),'sessionId':'debug-session','hypothesisId':'B,C'})+'\n')
    # #endregion
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        sample_interval = request.args.get('sample', type=float)
        orientation_types = ['Roll', 'Pitch', 'Yaw', 'Heading']
        
        if sample_interval and sample_interval > 0:
            # Use sampling for better performance
            placeholders = ','.join(['%s'] * len(orientation_types))
            query = f"""
                SELECT DISTINCT ON (data_type, time_bucket)
                    time,
                    data_type,
                    orientation_value
                FROM (
                    SELECT 
                        le.time,
                        dtc.data_type,
                        le.value as orientation_value,
                        FLOOR(EXTRACT(EPOCH FROM le.time) / %s) as time_bucket
                    FROM log_entries le
                    JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                    WHERE le.log_file_id = %s
                      AND dtc.data_type IN ({placeholders})
                ) subq
                ORDER BY data_type, time_bucket, time ASC
            """
            cursor.execute(query, [sample_interval, run_id] + orientation_types)
        else:
            # Get all data (no sampling)
            placeholders = ','.join(['%s'] * len(orientation_types))
            query = f"""
                SELECT 
                    le.time,
                    dtc.data_type,
                    le.value as orientation_value
                FROM log_entries le
                JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                WHERE le.log_file_id = %s
                  AND dtc.data_type IN ({placeholders})
                ORDER BY le.time ASC, dtc.data_type ASC
            """
            cursor.execute(query, [run_id] + orientation_types)
        
        orientation_data = cursor.fetchall()
        # #region agent log
        with open(r'c:\Users\ypenn\Documents\COSMA\Code\deepLog\.cursor\debug.log', 'a') as f:
            f.write(json.dumps({'location':'app.py:get_run_orientation:QUERY_DONE','message':'Query executed','data':{'row_count':len(orientation_data),'sample_interval':sample_interval},'timestamp':int(time.time()*1000),'sessionId':'debug-session','hypothesisId':'B,E'})+'\n')
        # #endregion
        
        # Group by data type (like motors API)
        result = {}
        for row in orientation_data:
            data_type = row['data_type']
            if data_type not in result:
                result[data_type] = []
            
            entry = {
                'time': row['time'].isoformat() if row['time'] else None,
                'value': row['orientation_value']
            }
            result[data_type].append(entry)
        
        # #region agent log
        with open(r'c:\Users\ypenn\Documents\COSMA\Code\deepLog\.cursor\debug.log', 'a') as f:
            f.write(json.dumps({'location':'app.py:get_run_orientation:RETURN','message':'Returning result','data':{'keys':list(result.keys()),'sizes':{k:len(v) for k,v in result.items()}},'timestamp':int(time.time()*1000),'sessionId':'debug-session','hypothesisId':'E'})+'\n')
        # #endregion
        return jsonify(result)
    except Exception as e:
        # #region agent log
        with open(r'c:\Users\ypenn\Documents\COSMA\Code\deepLog\.cursor\debug.log', 'a') as f:
            f.write(json.dumps({'location':'app.py:get_run_orientation:ERROR','message':'Exception in orientation endpoint','data':{'error':str(e),'type':type(e).__name__},'timestamp':int(time.time()*1000),'sessionId':'debug-session','hypothesisId':'B'})+'\n')
        # #endregion
        raise
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/water_temp')
def get_run_water_temp(run_id):
    """API endpoint to get water temperature data for a specific AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Try to find water temperature data type (may have different names)
        temp_query = """
            SELECT DISTINCT dtc.data_type
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.log_file_id = %s
              AND (dtc.data_type ILIKE '%%water%%temp%%' 
                   OR dtc.data_type ILIKE '%%temp%%water%%'
                   OR dtc.data_type ILIKE '%%eau%%temp%%'
                   OR dtc.data_type ILIKE '%%temp%%eau%%')
            LIMIT 5
        """
        cursor.execute(temp_query, (run_id,))
        temp_types = [row['data_type'] for row in cursor.fetchall()]
        
        # If no water-specific temperature found, try to find any temperature that might be water temp
        if not temp_types:
            temp_query2 = """
                SELECT DISTINCT dtc.data_type
                FROM log_entries le
                JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                WHERE le.log_file_id = %s
                  AND (dtc.data_type ILIKE '%%temp%%' OR dtc.data_type ILIKE '%%Temp%%')
                LIMIT 5
            """
            cursor.execute(temp_query2, (run_id,))
            temp_types = [row['data_type'] for row in cursor.fetchall()]
            # Take the first one as water temperature (if multiple, user can adjust)
            if temp_types:
                temp_types = [temp_types[0]]
        
        if not temp_types:
            return jsonify([])
        
        # Build query to get water temperature data
        # Use proper parameterization for IN clause
        placeholders = ','.join(['%s'] * len(temp_types))
        query = f"""
            SELECT 
                le.time,
                le.value
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.log_file_id = %s
              AND dtc.data_type IN ({placeholders})
            ORDER BY le.time ASC
        """
        cursor.execute(query, [run_id] + temp_types)
        temp_data = cursor.fetchall()
        
        # Format as array of {time, value} objects
        result = []
        for row in temp_data:
            entry = {
                'time': row['time'].isoformat() if row['time'] else None,
                'value': row['value']
            }
            result.append(entry)
        
        return jsonify(result)
    
    except Exception as e:
        print(f"Error in get_run_water_temp for run {run_id}: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

    finally:
        cursor.close()
        conn.close()

@app.route('/api/import', methods=['POST'])
def import_data():
    """API endpoint to import log files from uploaded directory."""
    try:
        if 'files' not in request.files:
            return jsonify({'status': 'error', 'message': 'Aucun fichier fourni'}), 400
        
        files = request.files.getlist('files')
        if not files or all(f.filename == '' for f in files):
            return jsonify({'status': 'error', 'message': 'Aucun fichier sélectionné'}), 400
        
        # Filter only CSV files
        csv_files = [f for f in files if f.filename.endswith('.csv')]
        if not csv_files:
            return jsonify({'status': 'error', 'message': 'Aucun fichier CSV trouvé'}), 400
        
        # Generate unique import ID
        import_id = str(uuid.uuid4())
        
        # Create temporary directory structure
        temp_dir = tempfile.mkdtemp()
        
        # Save all files to temp directory BEFORE starting the thread
        # This is critical because Flask file objects are closed after the request ends
        try:
            for file in csv_files:
                rel_path = file.filename
                full_path = Path(temp_dir) / rel_path
                full_path.parent.mkdir(parents=True, exist_ok=True)
                file.save(str(full_path))
        except Exception as e:
            # Clean up temp directory if file saving fails
            shutil.rmtree(temp_dir, ignore_errors=True)
            return jsonify({'status': 'error', 'message': f'Erreur lors de la sauvegarde des fichiers: {str(e)}'}), 500
        
        # Start processing in a separate thread (files are already saved)
        thread = threading.Thread(target=process_import_files, args=(import_id, temp_dir))
        thread.daemon = True
        thread.start()
        
        # Return immediately with import_id
        return jsonify({
            'status': 'started',
            'import_id': import_id,
            'message': 'Import démarré'
        })
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'Erreur lors de l\'import: {str(e)}'}), 500

def process_import_files(import_id, temp_dir):
    """Process files in a separate thread and update status.
    
    Note: Files are already saved to temp_dir before this function is called.
    """
    global import_status
    
    try:
        with import_status_lock:
            import_status[import_id] = {
                'status': 'processing',
                'current_file': None,
                'file_index': 0,
                'total_files': 0,
                'files_processed': 0,
                'files_imported': 0,
                'files_skipped': 0,
                'current_file_bytes_processed': 0,
                'current_file_bytes_total': 0,
                'errors': []
            }
        
        # Import using the existing import_logs module
        sys.path.insert(0, str(Path(__file__).parent / 'database'))
        from import_logs import import_navigation_file, import_settings_file, import_usbl_file, import_usv_full_file, extract_vehicle_info
        
        conn = get_db_connection()
        files_processed = 0
        files_imported = 0
        files_skipped = 0
        errors = []
        
        # Use rglob to find all navigation, settings, USBL, and full files recursively
        temp_path = Path(temp_dir)
        nav_files = list(temp_path.rglob('*navigation*.csv'))
        settings_files = list(temp_path.rglob('*settings*.csv'))
        usbl_files = list(temp_path.rglob('*usbl*.csv'))
        full_files = list(temp_path.rglob('*full*.csv'))
        all_files = nav_files + settings_files + usbl_files + full_files
        total_files = len(all_files)
        
        with import_status_lock:
            import_status[import_id]['total_files'] = total_files
        
        # Import navigation files
        for nav_file in nav_files:
            # Create file_progress dict to track progress for this file
            file_progress = {'bytes_processed': 0, 'bytes_total': 0, 'estimated_total_lines': None}
            
            try:
                with import_status_lock:
                    import_status[import_id]['current_file'] = nav_file.name
                    import_status[import_id]['file_index'] = files_processed + 1
                    import_status[import_id]['current_file_bytes_processed'] = 0
                    import_status[import_id]['current_file_bytes_total'] = 0
                
                # Start a thread to periodically update import_status with file progress
                progress_update_active = threading.Event()
                progress_update_active.set()
                
                def update_progress_periodically():
                    while progress_update_active.is_set():
                        with import_status_lock:
                            if import_id in import_status:
                                import_status[import_id]['current_file_bytes_processed'] = file_progress.get('bytes_processed', 0)
                                import_status[import_id]['current_file_bytes_total'] = file_progress.get('bytes_total', 0)
                        time.sleep(0.2)  # Update every 200ms
                
                progress_thread = threading.Thread(target=update_progress_periodically, daemon=True)
                progress_thread.start()
                
                # Use optimized import with reduced commits and larger page_size
                result = import_navigation_file(nav_file, conn, file_progress, commit_frequency=5, use_copy=False)
                
                # Stop progress update thread
                progress_update_active.clear()
                progress_thread.join(timeout=0.5)
                
                # Final update
                with import_status_lock:
                    if import_id in import_status:
                        import_status[import_id]['current_file_bytes_processed'] = file_progress.get('bytes_processed', 0)
                        import_status[import_id]['current_file_bytes_total'] = file_progress.get('bytes_total', 0)
                
                if result:
                    files_imported += 1
                else:
                    files_skipped += 1
            except Exception as e:
                import traceback
                error_traceback = traceback.format_exc()
                error_msg = f"Error importing {nav_file.name}: {str(e)}"
                print(error_msg)
                print(error_traceback)
                errors.append(f"{nav_file.name}: {str(e)}")
            finally:
                files_processed += 1
                with import_status_lock:
                    import_status[import_id]['files_processed'] = files_processed
                    import_status[import_id]['files_imported'] = files_imported
                    import_status[import_id]['files_skipped'] = files_skipped
                    import_status[import_id]['current_file_bytes_processed'] = 0
                    import_status[import_id]['current_file_bytes_total'] = 0
        
        # Import settings files (only for AUV)
        for settings_file in settings_files:
            try:
                with import_status_lock:
                    import_status[import_id]['current_file'] = settings_file.name
                    import_status[import_id]['file_index'] = files_processed + 1
                
                vehicle_type, _ = extract_vehicle_info(settings_file.name)
                if vehicle_type == 'AUV':
                    result = import_settings_file(settings_file, conn)
                    if result:
                        files_imported += 1
                    else:
                        files_skipped += 1
                else:
                    print(f"Skipping settings file {settings_file.name} (USV)")
            except Exception as e:
                import traceback
                error_traceback = traceback.format_exc()
                error_msg = f"Error importing {settings_file.name}: {str(e)}"
                print(error_msg)
                print(error_traceback)
                errors.append(f"{settings_file.name}: {str(e)}")
            finally:
                files_processed += 1
                with import_status_lock:
                    import_status[import_id]['files_processed'] = files_processed
                    import_status[import_id]['files_imported'] = files_imported
                    import_status[import_id]['files_skipped'] = files_skipped
        
        # Import USBL files
        for usbl_file in usbl_files:
            # Create file_progress dict to track progress for this file
            file_progress = {'bytes_processed': 0, 'bytes_total': 0, 'estimated_total_lines': None}
            
            try:
                with import_status_lock:
                    import_status[import_id]['current_file'] = usbl_file.name
                    import_status[import_id]['file_index'] = files_processed + 1
                    import_status[import_id]['current_file_bytes_processed'] = 0
                    import_status[import_id]['current_file_bytes_total'] = 0
                
                # Start a thread to periodically update import_status with file progress
                progress_update_active = threading.Event()
                progress_update_active.set()
                
                def update_progress_periodically():
                    while progress_update_active.is_set():
                        with import_status_lock:
                            if import_id in import_status:
                                import_status[import_id]['current_file_bytes_processed'] = file_progress.get('bytes_processed', 0)
                                import_status[import_id]['current_file_bytes_total'] = file_progress.get('bytes_total', 0)
                        time.sleep(0.2)  # Update every 200ms
                
                progress_thread = threading.Thread(target=update_progress_periodically, daemon=True)
                progress_thread.start()
                
                # Use optimized import with reduced commits and larger page_size
                result = import_usbl_file(usbl_file, conn, file_progress, commit_frequency=5)
                
                # Stop progress update thread
                progress_update_active.clear()
                progress_thread.join(timeout=0.5)
                
                # Final update
                with import_status_lock:
                    if import_id in import_status:
                        import_status[import_id]['current_file_bytes_processed'] = file_progress.get('bytes_processed', 0)
                        import_status[import_id]['current_file_bytes_total'] = file_progress.get('bytes_total', 0)
                
                if result:
                    files_imported += 1
                else:
                    files_skipped += 1
            except Exception as e:
                import traceback
                error_traceback = traceback.format_exc()
                error_msg = f"Error importing {usbl_file.name}: {str(e)}"
                print(error_msg)
                print(error_traceback)
                errors.append(f"{usbl_file.name}: {str(e)}")
            finally:
                files_processed += 1
                with import_status_lock:
                    import_status[import_id]['files_processed'] = files_processed
                    import_status[import_id]['files_imported'] = files_imported
                    import_status[import_id]['files_skipped'] = files_skipped
                    import_status[import_id]['current_file_bytes_processed'] = 0
                    import_status[import_id]['current_file_bytes_total'] = 0
        
        # Import USV full files
        for full_file in full_files:
            # Create file_progress dict to track progress for this file
            file_progress = {'bytes_processed': 0, 'bytes_total': 0, 'estimated_total_lines': None}
            
            try:
                with import_status_lock:
                    import_status[import_id]['current_file'] = full_file.name
                    import_status[import_id]['file_index'] = files_processed + 1
                    import_status[import_id]['current_file_bytes_processed'] = 0
                    import_status[import_id]['current_file_bytes_total'] = 0
                
                # Start a thread to periodically update import_status with file progress
                progress_update_active = threading.Event()
                progress_update_active.set()
                
                def update_progress_periodically():
                    while progress_update_active.is_set():
                        with import_status_lock:
                            if import_id in import_status:
                                import_status[import_id]['current_file_bytes_processed'] = file_progress.get('bytes_processed', 0)
                                import_status[import_id]['current_file_bytes_total'] = file_progress.get('bytes_total', 0)
                        time.sleep(0.2)  # Update every 200ms
                
                progress_thread = threading.Thread(target=update_progress_periodically, daemon=True)
                progress_thread.start()
                
                # Use optimized import with reduced commits and larger page_size
                result = import_usv_full_file(full_file, conn, file_progress, commit_frequency=5)
                
                # Stop progress update thread
                progress_update_active.clear()
                progress_thread.join(timeout=0.5)
                
                # Final update
                with import_status_lock:
                    if import_id in import_status:
                        import_status[import_id]['current_file_bytes_processed'] = file_progress.get('bytes_processed', 0)
                        import_status[import_id]['current_file_bytes_total'] = file_progress.get('bytes_total', 0)
                
                if result:
                    files_imported += 1
                else:
                    files_skipped += 1
            except Exception as e:
                import traceback
                error_traceback = traceback.format_exc()
                error_msg = f"Error importing {full_file.name}: {str(e)}"
                print(error_msg)
                print(error_traceback)
                errors.append(f"{full_file.name}: {str(e)}")
            finally:
                files_processed += 1
                with import_status_lock:
                    import_status[import_id]['files_processed'] = files_processed
                    import_status[import_id]['files_imported'] = files_imported
                    import_status[import_id]['files_skipped'] = files_skipped
                    import_status[import_id]['current_file_bytes_processed'] = 0
                    import_status[import_id]['current_file_bytes_total'] = 0
        
        conn.close()
        
        # Build message
        message_parts = []
        if files_imported > 0:
            message_parts.append(f'{files_imported} fichier(s) importé(s)')
        if files_skipped > 0:
            message_parts.append(f'{files_skipped} fichier(s) déjà importé(s)')
        if errors:
            message_parts.append(f'{len(errors)} erreur(s)')
        
        message = ', '.join(message_parts) if message_parts else 'Aucun fichier traité'
        
        # Save cleaned files to a permanent location before cleanup
        # When importing via web interface, files are in temp directory which gets deleted
        # So we save cleaned files to Logs/cleaned_files/ to preserve them
        try:
            sys.path.insert(0, str(Path(__file__).parent / 'database'))
            temp_path = Path(temp_dir)
            
            # Find all cleaned files in temp directory
            cleaned_files = list(temp_path.rglob('*_clean.csv'))
            
            if cleaned_files:
                # Create cleaned_files directory in Logs folder
                logs_dir = Path(__file__).parent / 'Logs' / 'cleaned_files'
                logs_dir.mkdir(parents=True, exist_ok=True)
                
                for cleaned_file in cleaned_files:
                    # Get relative path from temp_dir to preserve structure
                    rel_path = cleaned_file.relative_to(temp_path)
                    # Create target path in cleaned_files directory
                    target_path = logs_dir / rel_path
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    # Copy cleaned file
                    shutil.copy2(cleaned_file, target_path)
                    print(f"Saved cleaned file to: {target_path}")
        except Exception as e:
            print(f"Warning: Could not save cleaned files: {e}")
            import traceback
            traceback.print_exc()
        
        # Clean up temporary directory
        shutil.rmtree(temp_dir, ignore_errors=True)
        
        # Update final status
        with import_status_lock:
            import_status[import_id].update({
                'status': 'completed',
                'current_file': None,
                'files_processed': files_processed,
                'files_imported': files_imported,
                'files_skipped': files_skipped,
                'message': message,
                'errors': errors[:10]  # Show up to 10 errors instead of 5
            })
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        with import_status_lock:
            import_status[import_id].update({
                'status': 'error',
                'message': f'Erreur lors de l\'import: {str(e)}'
            })
        # Clean up on error
        shutil.rmtree(temp_dir, ignore_errors=True)

@app.route('/api/import/status/<import_id>')
def import_status_stream(import_id):
    """SSE endpoint to stream import progress updates."""
    def generate():
        global import_status
        last_file_index = -1
        last_status_str = None
        
        while True:
            with import_status_lock:
                status = import_status.get(import_id, None)
            
            if status is None:
                yield f"data: {json.dumps({'error': 'Import ID not found'})}\n\n"
                break
            
            # Create a simple string representation for comparison
            current_status_str = f"{status['status']}_{status.get('file_index', 0)}_{status.get('files_processed', 0)}"
            
            # Send update if status changed
            if current_status_str != last_status_str:
                if status['status'] == 'completed':
                    yield f"event: complete\ndata: {json.dumps(status)}\n\n"
                    break
                elif status['status'] == 'error':
                    yield f"event: error\ndata: {json.dumps(status)}\n\n"
                    break
                else:
                    yield f"event: progress\ndata: {json.dumps(status)}\n\n"
                last_status_str = current_status_str
            
            time.sleep(0.5)  # Poll every 500ms
        
        # Clean up after completion
        with import_status_lock:
            if import_id in import_status:
                del import_status[import_id]
    
    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/api/reset', methods=['POST'])
def reset_database():
    """API endpoint to reset the database (delete all logs)."""
    try:
        sys.path.insert(0, str(Path(__file__).parent / 'database'))
        from clear_and_reimport import clear_all_data
        
        success = clear_all_data()
        
        if success:
            return jsonify({
                'status': 'success',
                'message': 'Base de données réinitialisée avec succès'
            })
        else:
            return jsonify({
                'status': 'error',
                'message': 'Erreur lors de la réinitialisation de la base de données'
            }), 500
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            'status': 'error',
            'message': f'Erreur lors du reset: {str(e)}'
        }), 500

@app.route('/api/run/<int:run_id>/usbl_bearing')
@monitor_performance
def get_run_usbl_bearing(run_id):
    """API endpoint to get USBL bearing (azimuth) data for the time period of an AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # First, get the time range of the AUV run
        cursor.execute("""
            SELECT first_timestamp, last_timestamp
            FROM log_files
            WHERE id = %s AND vehicle_type = 'AUV'
        """, (run_id,))
        
        run_info = cursor.fetchone()
        if not run_info:
            return jsonify({'error': 'Run not found'}), 404
        
        start_time = run_info['first_timestamp']
        end_time = run_info['last_timestamp']
        
        # Get USBL bearing data for this time period
        # Note: The data type is USV_FULL_AUV_Bearing, not USBL_Bearing
        query = """
            SELECT 
                le.time,
                le.value
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE dtc.data_type = 'USV_FULL_AUV_Bearing'
              AND le.time >= %s
              AND le.time <= %s
            ORDER BY le.time ASC
        """
        cursor.execute(query, (start_time, end_time))
        bearing_data = cursor.fetchall()
        
        # Convert to list with ISO timestamps
        result = []
        for row in bearing_data:
            entry = dict(row)
            if entry['time']:
                entry['time'] = entry['time'].isoformat()
            result.append(entry)
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/usbl_elevation')
def get_run_usbl_elevation(run_id):
    """API endpoint to get USBL elevation data for the time period of an AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # First, get the time range of the AUV run
        cursor.execute("""
            SELECT first_timestamp, last_timestamp
            FROM log_files
            WHERE id = %s AND vehicle_type = 'AUV'
        """, (run_id,))
        
        run_info = cursor.fetchone()
        if not run_info:
            return jsonify({'error': 'Run not found'}), 404
        
        start_time = run_info['first_timestamp']
        end_time = run_info['last_timestamp']
        
        # Get USBL elevation data for this time period
        query = """
            SELECT 
                le.time,
                le.value
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.vehicle_type = 'USV'
              AND dtc.data_type = 'USBL_Elevation'
              AND le.time >= %s
              AND le.time <= %s
            ORDER BY le.time ASC
        """
        cursor.execute(query, (start_time, end_time))
        elevation_data = cursor.fetchall()
        
        # Convert to list with ISO timestamps
        result = []
        for row in elevation_data:
            entry = dict(row)
            if entry['time']:
                entry['time'] = entry['time'].isoformat()
            result.append(entry)
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/usbl_distance')
def get_run_usbl_distance(run_id):
    """API endpoint to get USBL distance data for the time period of an AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # First, get the time range of the AUV run
        cursor.execute("""
            SELECT first_timestamp, last_timestamp
            FROM log_files
            WHERE id = %s AND vehicle_type = 'AUV'
        """, (run_id,))
        
        run_info = cursor.fetchone()
        if not run_info:
            return jsonify({'error': 'Run not found'}), 404
        
        start_time = run_info['first_timestamp']
        end_time = run_info['last_timestamp']
        
        # Get USBL distance data for this time period
        query = """
            SELECT 
                le.time,
                le.value
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.vehicle_type = 'USV'
              AND dtc.data_type = 'USBL_Distance'
              AND le.time >= %s
              AND le.time <= %s
            ORDER BY le.time ASC
        """
        cursor.execute(query, (start_time, end_time))
        distance_data = cursor.fetchall()
        
        # Convert to list with ISO timestamps
        result = []
        for row in distance_data:
            entry = dict(row)
            if entry['time']:
                entry['time'] = entry['time'].isoformat()
            result.append(entry)
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/usv_full_auv_data')
def get_run_usv_full_auv_data(run_id):
    """API endpoint to get USV full log AUV tracking data for the time period of an AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # First, get the time range of the AUV run
        cursor.execute("""
            SELECT first_timestamp, last_timestamp, vehicle_id
            FROM log_files
            WHERE id = %s AND vehicle_type = 'AUV'
        """, (run_id,))
        
        run_info = cursor.fetchone()
        if not run_info:
            return jsonify({'error': 'Run not found'}), 404
        
        start_time = run_info['first_timestamp']
        end_time = run_info['last_timestamp']
        auv_vehicle_id = run_info['vehicle_id']  # e.g., 'AUV005'
        
        # #region agent log
        with open(str(get_debug_log_path()), 'a') as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"api","hypothesisId":"B","location":"app.py:1562","message":"API called","data":{"run_id":run_id,"start_time":str(start_time),"end_time":str(end_time),"auv_vehicle_id":auv_vehicle_id},"timestamp":int(time.time()*1000)}) + '\n')
        # #endregion
        
        # Get USV full AUV data for this time period
        # Query for distance, bearing, elevation, and state
        # Note: We don't filter by vehicle_id because USV Full logs store AUV IDs as "AUV0", "AUV1", etc.
        # which may not match the run's vehicle_id (e.g., "AUV005"). We rely on time range filtering only.
        query = """
            SELECT 
                le.time,
                dtc.data_type,
                le.value,
                le.vehicle_id as auv_id
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.vehicle_type = 'AUV'
              AND dtc.data_type IN ('USV_FULL_AUV_Distance', 'USV_FULL_AUV_Bearing', 
                                     'USV_FULL_AUV_Elevation', 'USV_FULL_AUV_State')
              AND le.time >= %s
              AND le.time <= %s
            ORDER BY le.time ASC, dtc.data_type ASC
        """
        cursor.execute(query, (start_time, end_time))
        data = cursor.fetchall()
        
        # #region agent log
        with open(str(get_debug_log_path()), 'a') as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"api","hypothesisId":"B","location":"app.py:1585","message":"Query executed","data":{"row_count":len(data)},"timestamp":int(time.time()*1000)}) + '\n')
        # #endregion
        
        # Group data by type
        result = {
            'distance': [],
            'bearing': [],
            'elevation': [],
            'state': []
        }
        
        for row in data:
            entry = {
                'time': row['time'].isoformat() if row['time'] else None,
                'value': row['value'],
                'auv_id': row['auv_id']
            }
            
            data_type = row['data_type']
            if data_type == 'USV_FULL_AUV_Distance':
                result['distance'].append(entry)
            elif data_type == 'USV_FULL_AUV_Bearing':
                result['bearing'].append(entry)
            elif data_type == 'USV_FULL_AUV_Elevation':
                result['elevation'].append(entry)
            elif data_type == 'USV_FULL_AUV_State':
                result['state'].append(entry)
        
        # #region agent log
        with open(str(get_debug_log_path()), 'a') as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"api","hypothesisId":"B","location":"app.py:1615","message":"Returning result","data":{"distance_count":len(result['distance']),"bearing_count":len(result['bearing']),"elevation_count":len(result['elevation']),"state_count":len(result['state'])},"timestamp":int(time.time()*1000)}) + '\n')
        # #endregion
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/usv_order')
def get_run_usv_order(run_id):
    """API endpoint to get 'Order to AUV' data from USV logs for the time period of an AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # First, get the time range of the AUV run
        cursor.execute("""
            SELECT first_timestamp, last_timestamp
            FROM log_files
            WHERE id = %s AND vehicle_type = 'AUV'
        """, (run_id,))
        
        run_info = cursor.fetchone()
        if not run_info:
            return jsonify({'error': 'Run not found'}), 404
        
        start_time = run_info['first_timestamp']
        end_time = run_info['last_timestamp']
        
        # Get USV 'Order to AUV' data for this time period
        # Try different possible names for this data type
        possible_names = ['ORDER_TO_AUV', 'Order to AUV', 'OrderToAUV', 'Order to Auv', 'OrderToAuv', 'order_to_auv']
        
        result = []
        for data_type_name in possible_names:
            query = """
                SELECT 
                    le.time,
                    le.value
                FROM log_entries le
                JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                WHERE le.vehicle_type = 'USV'
                  AND dtc.data_type = %s
                  AND le.time >= %s
                  AND le.time <= %s
                ORDER BY le.time ASC
            """
            cursor.execute(query, (data_type_name, start_time, end_time))
            order_data = cursor.fetchall()
            
            if order_data:
                # Convert to list with ISO timestamps
                for row in order_data:
                    entry = dict(row)
                    if entry['time']:
                        entry['time'] = entry['time'].isoformat()
                    result.append(entry)
                break  # Found data, stop trying other names
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/settings')
def get_run_settings(run_id):
    """Get specific settings for a run (minAltitudeStartBreak and minAltitudeFullReverse)."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Get run info
        cursor.execute("""
            SELECT vehicle_type, vehicle_id, first_timestamp
            FROM log_files
            WHERE id = %s
        """, (run_id,))
        run = cursor.fetchone()
        
        if not run:
            return jsonify({'error': 'Run not found'}), 404
        
        # Find associated settings file (same logic as get_settings)
        cursor.execute("""
            SELECT 
                sf.id as settings_file_id,
                sf.filename as settings_filename,
                sf.vehicle_id,
                sf.import_completed_at,
                sf.import_status
            FROM settings_files sf
            WHERE sf.vehicle_type = %s AND sf.import_status = 'completed'
            ORDER BY sf.filename ASC
        """, (run['vehicle_type'],))
        all_settings_files = cursor.fetchall()
        
        # Extract timestamp from filename and find best match
        import re
        from datetime import datetime
        best_match = None
        best_timestamp = None
        
        if run['first_timestamp']:
            run_timestamp = run['first_timestamp']
            if isinstance(run_timestamp, str):
                try:
                    run_timestamp = datetime.fromisoformat(run_timestamp.replace('Z', '+00:00'))
                except:
                    run_timestamp = None
            elif not isinstance(run_timestamp, datetime):
                run_timestamp = None
            
            if run_timestamp:
                for sf in all_settings_files:
                    if sf['vehicle_id'] == run['vehicle_id']:
                        filename = sf['settings_filename']
                        match = re.match(r'^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})', filename)
                        if match:
                            try:
                                timestamp_str = match.group(1)
                                file_timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d_%H-%M-%S')
                                
                                if isinstance(run_timestamp, datetime):
                                    if run_timestamp.tzinfo is not None:
                                        run_timestamp_naive = run_timestamp.replace(tzinfo=None)
                                    else:
                                        run_timestamp_naive = run_timestamp
                                    
                                    if file_timestamp <= run_timestamp_naive:
                                        if best_timestamp is None or file_timestamp > best_timestamp:
                                            best_timestamp = file_timestamp
                                            best_match = sf['settings_file_id']
                            except Exception:
                                pass
        
        if not best_match:
            return jsonify({
                'minAltitudeStartBreak': None,
                'minAltitudeFullReverse': None
            })
        
        # Get the two settings
        cursor.execute("""
            SELECT setting_name, setting_value, value_type
            FROM vehicle_settings
            WHERE settings_file_id = %s 
            AND setting_name IN ('follow.minAltitudeStartBreak', 'follow.minAltitudeFullReverse')
        """, (best_match,))
        settings = cursor.fetchall()
        
        result = {
            'minAltitudeStartBreak': None,
            'minAltitudeFullReverse': None
        }
        
        for setting in settings:
            value = setting['setting_value']
            # Convert to number if possible
            try:
                if setting['value_type'] == 'number':
                    value = float(value)
                elif setting['value_type'] == 'boolean':
                    value = bool(value)
            except:
                pass
            
            if setting['setting_name'] == 'follow.minAltitudeStartBreak':
                result['minAltitudeStartBreak'] = value
            elif setting['setting_name'] == 'follow.minAltitudeFullReverse':
                result['minAltitudeFullReverse'] = value
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/settings')
def get_settings():
    """API endpoint to get settings organized by runs for comparison."""
    # #region agent log
    import json
    with open(str(get_debug_log_path()), 'a') as f:
        f.write(json.dumps({"sessionId":"debug-session","runId":"initial","hypothesisId":"C","location":"app.py:1544","message":"API /api/settings called","data":{"vehicle_type":request.args.get('vehicle_type', 'AUV')},"timestamp":int(time.time()*1000)}) + '\n')
    # #endregion
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        vehicle_type = request.args.get('vehicle_type', 'AUV')
        
        # #region agent log
        with open(str(get_debug_log_path()), 'a') as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"initial","hypothesisId":"A","location":"app.py:1552","message":"Checking settings_files in DB","data":{"vehicle_type":vehicle_type},"timestamp":int(time.time()*1000)}) + '\n')
        # #endregion
        
        # First, check if settings_files exist for this vehicle type
        cursor.execute("""
            SELECT COUNT(*) as count, 
                   COUNT(CASE WHEN import_status = 'completed' THEN 1 END) as completed_count
            FROM settings_files
            WHERE vehicle_type = %s
        """, (vehicle_type,))
        settings_count = cursor.fetchone()
        
        # #region agent log
        with open(str(get_debug_log_path()), 'a') as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"initial","hypothesisId":"A","location":"app.py:1562","message":"Settings files count","data":{"total":settings_count['count'],"completed":settings_count['completed_count']},"timestamp":int(time.time()*1000)}) + '\n')
        # #endregion
        
        # Debug: Log all settings files found
        with open(str(get_debug_log_path()), 'a') as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"initial","hypothesisId":"A","location":"app.py:1565","message":"All settings files query","data":{"vehicle_type":vehicle_type},"timestamp":int(time.time()*1000)}) + '\n')
        
        # Get all settings files for this vehicle type
        # Show ALL settings files, not just those associated with runs
        cursor.execute("""
            SELECT 
                sf.id as settings_file_id,
                sf.filename as settings_filename,
                sf.vehicle_id,
                sf.import_completed_at,
                sf.import_status
            FROM settings_files sf
            WHERE sf.vehicle_type = %s
            ORDER BY sf.filename ASC
        """, (vehicle_type,))
        all_settings_files_raw = cursor.fetchall()
        
        # Filter to only completed files and log what we found
        all_settings_files = []
        for sf in all_settings_files_raw:
            if sf['import_status'] == 'completed':
                all_settings_files.append(sf)
            else:
                # Log files that are not completed
                with open(str(get_debug_log_path()), 'a') as f:
                    f.write(json.dumps({"sessionId":"debug-session","runId":"initial","hypothesisId":"A","location":"app.py:1850","message":"Settings file not completed","data":{"filename":sf['settings_filename'],"status":sf['import_status']},"timestamp":int(time.time()*1000)}) + '\n')
        
        # Log how many completed files we found
        with open(str(get_debug_log_path()), 'a') as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"initial","hypothesisId":"A","location":"app.py:1855","message":"Completed settings files","data":{"count":len(all_settings_files),"files":[{"id":sf['settings_file_id'],"filename":sf['settings_filename']} for sf in all_settings_files]},"timestamp":int(time.time()*1000)}) + '\n')
        
        # Extract timestamp from filename in Python (more reliable than SQL)
        import re
        from datetime import datetime
        for sf in all_settings_files:
            filename = sf['settings_filename']
            # Extract timestamp from filename: YYYY-MM-DD_HH-MM-SS
            match = re.match(r'^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})', filename)
            if match:
                try:
                    timestamp_str = match.group(1)
                    file_timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d_%H-%M-%S')
                    sf['file_timestamp'] = file_timestamp.isoformat()
                except:
                    sf['file_timestamp'] = None
            else:
                sf['file_timestamp'] = None
        
        # Get all runs for this vehicle type to show which settings file is associated with each run
        # We'll match settings files to runs in Python after extracting timestamps
        cursor.execute("""
            SELECT 
                lf.id as run_id,
                lf.filename as run_filename,
                lf.first_timestamp,
                lf.vehicle_id
            FROM log_files lf
            WHERE lf.vehicle_type = %s
                AND lf.import_status = 'completed'
                AND lf.first_timestamp IS NOT NULL
            ORDER BY lf.first_timestamp ASC
        """, (vehicle_type,))
        runs_data = cursor.fetchall()
        
        # Match settings files to runs based on timestamp
        import re
        from datetime import datetime
        for run in runs_data:
            run['associated_settings_file_id'] = None
            if run['first_timestamp']:
                # Find the most recent settings file for this vehicle that was created before the run
                best_match = None
                best_timestamp = None
                
                # Convert run timestamp to datetime for comparison
                run_timestamp = run['first_timestamp']
                if isinstance(run_timestamp, str):
                    try:
                        run_timestamp = datetime.fromisoformat(run_timestamp.replace('Z', '+00:00'))
                    except:
                        continue
                elif not isinstance(run_timestamp, datetime):
                    # If it's a datetime object from PostgreSQL, use it directly
                    run_timestamp = run_timestamp
                
                for sf in all_settings_files:
                    if sf['vehicle_id'] == run['vehicle_id']:
                        filename = sf['settings_filename']
                        match = re.match(r'^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})', filename)
                        if match:
                            try:
                                timestamp_str = match.group(1)
                                file_timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d_%H-%M-%S')
                                
                                # Compare timestamps (handle timezone-aware vs naive)
                                if isinstance(run_timestamp, datetime):
                                    # Make both timezone-naive for comparison
                                    if run_timestamp.tzinfo is not None:
                                        run_timestamp_naive = run_timestamp.replace(tzinfo=None)
                                    else:
                                        run_timestamp_naive = run_timestamp
                                    
                                    if file_timestamp <= run_timestamp_naive:
                                        if best_timestamp is None or file_timestamp > best_timestamp:
                                            best_timestamp = file_timestamp
                                            best_match = sf['settings_file_id']
                            except Exception as e:
                                # Skip if timestamp parsing fails
                                pass
                
                run['associated_settings_file_id'] = best_match
        
        # #region agent log
        with open(str(get_debug_log_path()), 'a') as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"initial","hypothesisId":"B","location":"app.py:1590","message":"Runs query result","data":{"runs_found":len(runs_data),"runs_with_settings":sum(1 for r in runs_data if r.get('associated_settings_file_id'))},"timestamp":int(time.time()*1000)}) + '\n')
        # #endregion
        
        # #region agent log - Debug: Check settings files and runs timestamps
        cursor.execute("""
            SELECT id, filename, vehicle_type, vehicle_id, import_completed_at, import_status
            FROM settings_files
            WHERE vehicle_type = %s AND import_status = 'completed'
            ORDER BY import_completed_at DESC
            LIMIT 5
        """, (vehicle_type,))
        sample_settings = cursor.fetchall()
        with open(str(get_debug_log_path()), 'a') as f:
            settings_list = []
            for s in sample_settings:
                settings_list.append({
                    "id": s['id'],
                    "filename": s['filename'],
                    "vehicle_id": s['vehicle_id'],
                    "import_completed_at": s['import_completed_at'].isoformat() if s['import_completed_at'] else None
                })
            f.write(json.dumps({"sessionId":"debug-session","runId":"initial","hypothesisId":"B","location":"app.py:1610","message":"Sample settings files","data":{"settings":settings_list},"timestamp":int(time.time()*1000)}) + '\n')
        
        if runs_data:
            sample_runs = []
            for r in runs_data[:3]:
                sample_runs.append({
                    "run_id": r['run_id'],
                    "filename": r['run_filename'],
                    "vehicle_id": r['vehicle_id'],
                    "first_timestamp": r['first_timestamp'].isoformat() if r['first_timestamp'] else None,
                    "associated_settings_file_id": r.get('associated_settings_file_id')
                })
            with open(str(get_debug_log_path()), 'a') as f:
                f.write(json.dumps({"sessionId":"debug-session","runId":"initial","hypothesisId":"B","location":"app.py:1625","message":"Sample runs","data":{"runs":sample_runs},"timestamp":int(time.time()*1000)}) + '\n')
        # #endregion
        
        if not all_settings_files:
            # #region agent log
            with open(str(get_debug_log_path()), 'a') as f:
                f.write(json.dumps({"sessionId":"debug-session","runId":"initial","hypothesisId":"B","location":"app.py:1595","message":"No settings files found","data":{"vehicle_type":vehicle_type},"timestamp":int(time.time()*1000)}) + '\n')
            # #endregion
            return jsonify({
                'runs': [],
                'settings_files': [],
                'settings_matrix': {},
                'all_setting_names': []
            })
        
        # Build list of settings files (columns in the table)
        settings_files_list = []
        for sf in all_settings_files:
            settings_file_dict = {
                'settings_file_id': sf['settings_file_id'],
                'settings_filename': sf['settings_filename'],
                'vehicle_id': sf['vehicle_id'],
                'file_timestamp': sf.get('file_timestamp')  # Already a string or None
            }
            settings_files_list.append(settings_file_dict)
        
        # Get all settings for all settings files
        settings_matrix = {}
        all_setting_names_set = set()
        
        for sf in all_settings_files:
            settings_file_id = sf['settings_file_id']
            
            # #region agent log
            with open(str(get_debug_log_path()), 'a') as f:
                f.write(json.dumps({"sessionId":"debug-session","runId":"initial","hypothesisId":"B","location":"app.py:1615","message":"Processing settings file","data":{"settings_file_id":settings_file_id,"filename":sf['settings_filename']},"timestamp":int(time.time()*1000)}) + '\n')
            # #endregion
            
            # Get settings for this settings file
            settings_query = """
                SELECT setting_name, setting_value, category
                FROM vehicle_settings
                WHERE settings_file_id = %s
                ORDER BY category, setting_name
            """
            cursor.execute(settings_query, (settings_file_id,))
            settings = cursor.fetchall()
            
            # #region agent log
            with open(str(get_debug_log_path()), 'a') as f:
                f.write(json.dumps({"sessionId":"debug-session","runId":"initial","hypothesisId":"C","location":"app.py:1628","message":"Settings loaded for settings file","data":{"settings_file_id":settings_file_id,"settings_count":len(settings)},"timestamp":int(time.time()*1000)}) + '\n')
            # #endregion
            
            for setting in settings:
                setting_name = setting['setting_name']
                all_setting_names_set.add(setting_name)
                
                if setting_name not in settings_matrix:
                    settings_matrix[setting_name] = {}
                
                settings_matrix[setting_name][f"settings_file_{settings_file_id}"] = setting['setting_value']
        
        # Sort setting names for consistent display
        all_setting_names = sorted(list(all_setting_names_set))
        
        # Build complete matrix (fill missing values with null)
        for setting_name in all_setting_names:
            for sf in settings_files_list:
                settings_file_key = f"settings_file_{sf['settings_file_id']}"
                if settings_file_key not in settings_matrix[setting_name]:
                    settings_matrix[setting_name][settings_file_key] = None
        
        # Build settings groups (group settings by prefix before first dot)
        settings_groups = {}
        json_keys_to_group = ['camera', 'api', 'navigation', 'usbl', 'seaker', 'follow', 'Emergency']
        # Also create lowercase versions for case-insensitive matching
        json_keys_to_group_lower = [k.lower() for k in json_keys_to_group]
        
        for setting_name in all_setting_names:
            setting_name_lower = setting_name.lower()
            # Check if this setting belongs to a group (has a dot and starts with a known JSON key)
            if '.' in setting_name:
                prefix = setting_name.split('.')[0]
                prefix_lower = prefix.lower()
                # Check both exact match and case-insensitive match
                if prefix in json_keys_to_group or prefix_lower in json_keys_to_group_lower:
                    # Use the canonical name (from json_keys_to_group)
                    canonical_prefix = prefix
                    for key in json_keys_to_group:
                        if key.lower() == prefix_lower:
                            canonical_prefix = key
                            break
                    if canonical_prefix not in settings_groups:
                        settings_groups[canonical_prefix] = []
                    settings_groups[canonical_prefix].append(setting_name)
            # Also check if the setting name itself is one of the JSON keys (the original entry)
            elif setting_name in json_keys_to_group or setting_name_lower in json_keys_to_group_lower:
                # Use the canonical name
                canonical_name = setting_name
                for key in json_keys_to_group:
                    if key.lower() == setting_name_lower:
                        canonical_name = key
                        break
                if canonical_name not in settings_groups:
                    settings_groups[canonical_name] = []
                settings_groups[canonical_name].append(setting_name)
        
        # Sort settings within each group
        for group_name in settings_groups:
            settings_groups[group_name] = sorted(settings_groups[group_name])
        
        # Build runs list for reference (which run uses which settings file)
        runs_list = []
        for run in runs_data:
            run_dict = {
                'run_id': run['run_id'],
                'run_filename': run['run_filename'],
                'first_timestamp': run['first_timestamp'].isoformat() if run['first_timestamp'] else None,
                'vehicle_id': run['vehicle_id'],
                'associated_settings_file_id': run['associated_settings_file_id']
            }
            runs_list.append(run_dict)
        
        # #region agent log
        with open(str(get_debug_log_path()), 'a') as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"initial","hypothesisId":"C","location":"app.py:1645","message":"API response prepared","data":{"runs_count":len(runs_list),"settings_count":len(all_setting_names)},"timestamp":int(time.time()*1000)}) + '\n')
        # #endregion
        
        return jsonify({
            'runs': runs_list,
            'settings_files': settings_files_list,
            'settings_matrix': settings_matrix,
            'settings_groups': settings_groups,
            'all_setting_names': all_setting_names
        })
        
    except Exception as e:
        import traceback
        error_msg = str(e)
        error_traceback = traceback.format_exc()
        with open(str(get_debug_log_path()), 'a') as f:
            f.write(json.dumps({"sessionId":"debug-session","runId":"initial","hypothesisId":"ERROR","location":"app.py:get_settings","message":"Exception in get_settings","data":{"error":error_msg,"traceback":error_traceback},"timestamp":int(time.time()*1000)}) + '\n')
        return jsonify({'error': f'Erreur lors de la récupération des settings: {error_msg}'}), 500
        
    finally:
        cursor.close()
        conn.close()

def interpolate_value(timestamp, data_array, max_time_gap_seconds=None):
    """
    Interpolate value at given timestamp from sorted data array.
    
    Args:
        timestamp: Target timestamp (datetime or ISO string)
        data_array: List of dicts with 'time' and 'value' keys, sorted by time
        max_time_gap_seconds: Maximum acceptable time gap for interpolation (default: None = unlimited)
                              If gap > max_time_gap_seconds, return closest value instead of interpolating
    
    Returns:
        Interpolated value or None if outside range
    """
    if not data_array or len(data_array) == 0:
        return None
    
    # Convert timestamp to comparable format
    from datetime import datetime
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    
    # Find surrounding points
    for i in range(len(data_array) - 1):
        time1 = data_array[i]['time']
        time2 = data_array[i + 1]['time']
        
        if isinstance(time1, str):
            time1 = datetime.fromisoformat(time1.replace('Z', '+00:00'))
        if isinstance(time2, str):
            time2 = datetime.fromisoformat(time2.replace('Z', '+00:00'))
        
        if time1 <= timestamp <= time2:
            # Check time gap if max_time_gap_seconds is specified
            gap_seconds = (time2 - time1).total_seconds()
            
            if max_time_gap_seconds and gap_seconds > max_time_gap_seconds:
                # Gap too large, return closest value instead of interpolating
                time_to_t1 = (timestamp - time1).total_seconds()
                time_to_t2 = (time2 - timestamp).total_seconds()
                
                if time_to_t1 < time_to_t2:
                    return data_array[i]['value']
                else:
                    return data_array[i + 1]['value']
            
            # Linear interpolation
            value1 = data_array[i]['value']
            value2 = data_array[i + 1]['value']
            
            if time1 == time2:
                return value1
            
            ratio = (timestamp - time1).total_seconds() / gap_seconds
            return value1 + (value2 - value1) * ratio
    
    # Outside range - return closest value if within reasonable distance
    first_time = data_array[0]['time']
    last_time = data_array[-1]['time']
    
    if isinstance(first_time, str):
        first_time = datetime.fromisoformat(first_time.replace('Z', '+00:00'))
    if isinstance(last_time, str):
        last_time = datetime.fromisoformat(last_time.replace('Z', '+00:00'))
    
    # Check if timestamp is within max_time_gap of first/last point
    if timestamp < first_time:
        if max_time_gap_seconds:
            gap = (first_time - timestamp).total_seconds()
            if gap <= max_time_gap_seconds:
                return data_array[0]['value']
        else:
            return data_array[0]['value']
    elif timestamp > last_time:
        if max_time_gap_seconds:
            gap = (timestamp - last_time).total_seconds()
            if gap <= max_time_gap_seconds:
                return data_array[-1]['value']
        else:
            return data_array[-1]['value']
    
    return None

def interpolate_position(timestamp, position_array):
    """
    Interpolate position (lat/lon) at given timestamp from sorted position array.
    
    Args:
        timestamp: Target timestamp (datetime or ISO string)
        position_array: List of dicts with 'time', 'lat', 'lon' keys, sorted by time
    
    Returns:
        Dict with 'lat' and 'lon' or None if outside range
    """
    if not position_array or len(position_array) == 0:
        return None
    
    # Convert timestamp to comparable format
    from datetime import datetime
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    
    # Find surrounding points
    for i in range(len(position_array) - 1):
        time1 = position_array[i]['time']
        time2 = position_array[i + 1]['time']
        
        if isinstance(time1, str):
            time1 = datetime.fromisoformat(time1.replace('Z', '+00:00'))
        if isinstance(time2, str):
            time2 = datetime.fromisoformat(time2.replace('Z', '+00:00'))
        
        if time1 <= timestamp <= time2:
            # Linear interpolation for both lat and lon
            lat1 = position_array[i]['lat']
            lon1 = position_array[i]['lon']
            lat2 = position_array[i + 1]['lat']
            lon2 = position_array[i + 1]['lon']
            
            if time1 == time2:
                return {'lat': lat1, 'lon': lon1}
            
            ratio = (timestamp - time1).total_seconds() / (time2 - time1).total_seconds()
            return {
                'lat': lat1 + (lat2 - lat1) * ratio,
                'lon': lon1 + (lon2 - lon1) * ratio
            }
    
    # Outside range - return closest value
    first_time = position_array[0]['time']
    last_time = position_array[-1]['time']
    
    if isinstance(first_time, str):
        first_time = datetime.fromisoformat(first_time.replace('Z', '+00:00'))
    if isinstance(last_time, str):
        last_time = datetime.fromisoformat(last_time.replace('Z', '+00:00'))
    
    if timestamp < first_time:
        return {'lat': position_array[0]['lat'], 'lon': position_array[0]['lon']}
    elif timestamp > last_time:
        return {'lat': position_array[-1]['lat'], 'lon': position_array[-1]['lon']}
    
    return None

def calculate_position_from_distance_bearing(lat_ref, lon_ref, distance_m, bearing_deg):
    """
    Calculate new position from reference position using distance and bearing.
    
    Args:
        lat_ref: Reference latitude in degrees
        lon_ref: Reference longitude in degrees
        distance_m: Distance in meters
        bearing_deg: Bearing in degrees (0 = North, 90 = East)
    
    Returns:
        (lat, lon) tuple in degrees
    """
    R = 6371000  # Earth radius in meters
    
    lat1 = math.radians(lat_ref)
    lon1 = math.radians(lon_ref)
    bearing = math.radians(bearing_deg)
    
    lat2 = math.asin(
        math.sin(lat1) * math.cos(distance_m / R) +
        math.cos(lat1) * math.sin(distance_m / R) * math.cos(bearing)
    )
    
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(distance_m / R) * math.cos(lat1),
        math.cos(distance_m / R) - math.sin(lat1) * math.sin(lat2)
    )
    
    return (math.degrees(lat2), math.degrees(lon2))

@app.route('/api/run/<int:run_id>/usv_heading')
def get_run_usv_heading(run_id):
    """API endpoint to get USV Heading data for the time period of an AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # First, get the time range of the AUV run
        cursor.execute("""
            SELECT first_timestamp, last_timestamp
            FROM log_files
            WHERE id = %s AND vehicle_type = 'AUV'
        """, (run_id,))
        
        run_info = cursor.fetchone()
        if not run_info:
            return jsonify({'error': 'Run not found'}), 404
        
        start_time = run_info['first_timestamp']
        end_time = run_info['last_timestamp']
        
        # Get USV Heading data for this time period
        query = """
            SELECT 
                le.time,
                le.value
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.vehicle_type = 'USV'
              AND dtc.data_type = 'Heading'
              AND le.time >= %s
              AND le.time <= %s
            ORDER BY le.time ASC
        """
        cursor.execute(query, (start_time, end_time))
        heading_data = cursor.fetchall()
        
        # Convert to list with ISO timestamps
        result = []
        for row in heading_data:
            entry = dict(row)
            if entry['time']:
                entry['time'] = entry['time'].isoformat()
            result.append(entry)
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/usbl/decoded_messages')
def get_usbl_decoded_messages():
    """API endpoint to get decoded USBL messages with pagination and filters."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Check if table exists
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name = 'usbl_decoded_messages'
            ) as exists;
        """)
        table_exists = cursor.fetchone()['exists']
        
        if not table_exists:
            return jsonify({
                'entries': [],
                'total': 0,
                'page': 1,
                'per_page': 100,
                'pages': 0,
                'message': 'Table usbl_decoded_messages does not exist. Please run database setup.'
            })
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 100))
        log_file_id = request.args.get('log_file_id', '')
        direction = request.args.get('direction', '')
        message_id = request.args.get('message_id', '')
        message_name = request.args.get('message_name', '')
        start_time = request.args.get('start_time', '')
        end_time = request.args.get('end_time', '')
        
        offset = (page - 1) * per_page
        
        query = """
            SELECT 
                udm.*,
                lf.filename AS log_filename,
                lf.vehicle_type,
                lf.vehicle_id
            FROM usbl_decoded_messages udm
            JOIN log_files lf ON lf.id = udm.log_file_id
            WHERE 1=1
        """
        count_query = """
            SELECT COUNT(*) as total
            FROM usbl_decoded_messages udm
            WHERE 1=1
        """
        params = []
        count_params = []
        
        if log_file_id:
            query += " AND udm.log_file_id = %s"
            count_query += " AND udm.log_file_id = %s"
            params.append(log_file_id)
            count_params.append(log_file_id)
        
        if direction:
            query += " AND udm.direction = %s"
            count_query += " AND udm.direction = %s"
            params.append(direction)
            count_params.append(direction)
        
        if message_id:
            query += " AND udm.message_id = %s"
            count_query += " AND udm.message_id = %s"
            params.append(message_id)
            count_params.append(message_id)
        
        if message_name:
            query += " AND udm.message_name ILIKE %s"
            count_query += " AND udm.message_name ILIKE %s"
            params.append(f'%{message_name}%')
            count_params.append(f'%{message_name}%')
        
        if start_time:
            query += " AND udm.timestamp >= %s"
            count_query += " AND udm.timestamp >= %s"
            params.append(start_time)
            count_params.append(start_time)
        
        if end_time:
            query += " AND udm.timestamp <= %s"
            count_query += " AND udm.timestamp <= %s"
            params.append(end_time)
            count_params.append(end_time)
        
        query += " ORDER BY udm.timestamp DESC LIMIT %s OFFSET %s"
        params.extend([per_page, offset])
        
        cursor.execute(count_query, count_params)
        total = cursor.fetchone()['total']
        
        cursor.execute(query, params)
        entries = cursor.fetchall()
        
        # Convert to dict and format timestamps
        entries_list = []
        for entry in entries:
            entry_dict = dict(entry)
            if entry_dict.get('timestamp'):
                entry_dict['timestamp'] = entry_dict['timestamp'].isoformat()
            if entry_dict.get('created_at'):
                entry_dict['created_at'] = entry_dict['created_at'].isoformat()
            entries_list.append(entry_dict)
        
        return jsonify({
            'entries': entries_list,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': (total + per_page - 1) // per_page
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        # Return empty result on error
        # Get per_page from request if available, otherwise default
        per_page_default = int(request.args.get('per_page', 100))
        return jsonify({
            'entries': [],
            'total': 0,
            'page': 1,
            'per_page': per_page_default,
            'pages': 0,
            'error': str(e)
        })
    finally:
        cursor.close()
        conn.close()

@app.route('/api/usbl/log_files')
def get_usbl_log_files():
    """API endpoint to get list of USBL log files."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Check if table exists
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name = 'usbl_decoded_messages'
            ) as exists;
        """)
        table_exists = cursor.fetchone()['exists']
        
        if table_exists:
            cursor.execute("""
                SELECT 
                    lf.id, 
                    lf.filename, 
                    lf.vehicle_id, 
                    lf.first_timestamp, 
                    lf.last_timestamp,
                    lf.file_path,
                    COUNT(udm.id) as decoded_count
                FROM log_files lf
                LEFT JOIN usbl_decoded_messages udm ON udm.log_file_id = lf.id
                WHERE lf.filename LIKE '%usbl%' AND lf.import_status IN ('completed', 'failed')
                GROUP BY lf.id, lf.filename, lf.vehicle_id, lf.first_timestamp, lf.last_timestamp, lf.file_path
                ORDER BY COALESCE(lf.first_timestamp, lf.import_started_at, '1970-01-01'::timestamp) DESC
            """)
        else:
            # Table doesn't exist, return files without decoded count
            cursor.execute("""
                SELECT 
                    lf.id, 
                    lf.filename, 
                    lf.vehicle_id, 
                    lf.first_timestamp, 
                    lf.last_timestamp,
                    lf.file_path,
                    0 as decoded_count
                FROM log_files lf
                WHERE lf.filename LIKE '%usbl%' AND lf.import_status IN ('completed', 'failed')
                ORDER BY COALESCE(lf.first_timestamp, lf.import_started_at, '1970-01-01'::timestamp) DESC
            """)
        
        files = cursor.fetchall()
        
        files_list = []
        for file in files:
            file_dict = dict(file)
            # Handle None timestamps
            if file_dict.get('first_timestamp'):
                file_dict['first_timestamp'] = file_dict['first_timestamp'].isoformat()
            else:
                file_dict['first_timestamp'] = None
            if file_dict.get('last_timestamp'):
                file_dict['last_timestamp'] = file_dict['last_timestamp'].isoformat()
            else:
                file_dict['last_timestamp'] = None
            files_list.append(file_dict)
        
        return jsonify(files_list)
    except Exception as e:
        import traceback
        traceback.print_exc()
        # Return empty list on error instead of error object to avoid breaking forEach
        return jsonify([])
    finally:
        cursor.close()
        conn.close()

@app.route('/api/usbl/decode/<int:log_file_id>', methods=['POST'])
def decode_usbl_file(log_file_id):
    """API endpoint to decode an already imported USBL file."""
    from database.import_logs import import_usbl_file
    import threading
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Check if file exists and is a USBL file
        cursor.execute("""
            SELECT id, filename, file_path, import_status
            FROM log_files
            WHERE id = %s AND filename LIKE '%%usbl%%'
        """, (log_file_id,))
        file_info = cursor.fetchone()
        
        if not file_info:
            return jsonify({'error': 'File not found or not a USBL file'}), 404
        
        # Allow decoding even if import failed (might have failed due to missing table)
        if file_info['import_status'] not in ['completed', 'failed']:
            return jsonify({'error': 'File import not completed or failed'}), 400
        
        # Delete existing decoded messages and log entries for this file
        cursor.execute("DELETE FROM usbl_decoded_messages WHERE log_file_id = %s", (log_file_id,))
        # Also delete existing USBL log entries to avoid duplicates
        cursor.execute("""
            DELETE FROM log_entries 
            WHERE log_file_id = %s 
            AND data_type_id IN (
                SELECT id FROM data_type_catalog 
                WHERE data_type IN ('USBL_Distance', 'USBL_Bearing', 'USBL_Elevation', 'USBL_SNR')
            )
        """, (log_file_id,))
        conn.commit()
        
        # Import the decoder functions (optional - only needed for USBL decoding)
        try:
            from antenna_driver_analysis.kogger_usbl_decoder import (
                decode_usbl_message_from_csv,
                parse_bytes_from_string,
                reassemble_received_messages,
                parse_message
            )
        except ImportError:
            return jsonify({
                'status': 'error',
                'message': 'antenna_driver_analysis module not installed. Install it to enable USBL file imports.'
            }), 500
        from psycopg2.extras import execute_values
        
        # Get vehicle info from log_file
        cursor.execute("SELECT vehicle_type, vehicle_id FROM log_files WHERE id = %s", (log_file_id,))
        file_info_extended = cursor.fetchone()
        vehicle_type = file_info_extended['vehicle_type']
        vehicle_id = file_info_extended['vehicle_id']
        
        # Get or create data types for USBL values
        usbl_data_types = {
            'USBL_Bearing': 'acoustic',
            'USBL_Elevation': 'acoustic',
            'USBL_Distance': 'acoustic',
            'USBL_SNR': 'acoustic'
        }
        data_type_ids = {}
        for data_type_name, category in usbl_data_types.items():
            cursor.execute("""
                SELECT id FROM data_type_catalog WHERE data_type = %s AND vehicle_type = %s
            """, (data_type_name, vehicle_type))
            result = cursor.fetchone()
            if result:
                data_type_ids[data_type_name] = result['id']
            else:
                cursor.execute("""
                    INSERT INTO data_type_catalog (data_type, vehicle_type, category)
                    VALUES (%s, %s, %s)
                    RETURNING id
                """, (data_type_name, vehicle_type, category))
                data_type_ids[data_type_name] = cursor.fetchone()['id']
                conn.commit()
        
        decoded_messages = []
        log_entries = []
        received_buffer = b''
        last_direction = None
        
        # First, try to read from usbl_raw_data table (preferred method)
        cursor.execute("""
            SELECT COUNT(*) as count FROM usbl_raw_data WHERE log_file_id = %s
        """, (log_file_id,))
        raw_data_count = cursor.fetchone()['count']
        
        if raw_data_count > 0:
            # Read from database
            cursor.execute("""
                SELECT timestamp, direction, data_raw
                FROM usbl_raw_data
                WHERE log_file_id = %s
                ORDER BY timestamp ASC
            """, (log_file_id,))
            raw_rows = cursor.fetchall()
            
            for row in raw_rows:
                timestamp = row['timestamp']
                direction = row['direction']
                data_str = row['data_raw']
                
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
                    timestamp_str = timestamp.isoformat()
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
                        
                        # Extract USBL values from ID_USBL_SOLUTION messages (SENT)
                        if decoded.get('message_name') == 'ID_USBL_SOLUTION' and decoded.get('distance') is not None:
                            # Store distance
                            if decoded.get('distance') > 0:
                                log_entries.append((
                                    timestamp,
                                    vehicle_type,
                                    vehicle_id,
                                    data_type_ids['USBL_Distance'],
                                    float(decoded['distance']),
                                    log_file_id
                                ))
                            
                            # Store bearing (azimuth)
                            if decoded.get('azimuth') is not None:
                                log_entries.append((
                                    timestamp,
                                    vehicle_type,
                                    vehicle_id,
                                    data_type_ids['USBL_Bearing'],
                                    float(decoded['azimuth']),
                                    log_file_id
                                ))
                            
                            # Store elevation
                            if decoded.get('elevation') is not None:
                                log_entries.append((
                                    timestamp,
                                    vehicle_type,
                                    vehicle_id,
                                    data_type_ids['USBL_Elevation'],
                                    float(decoded['elevation']),
                                    log_file_id
                                ))
                            
                            # Store SNR
                            if decoded.get('snr') is not None:
                                log_entries.append((
                                    timestamp,
                                    vehicle_type,
                                    vehicle_id,
                                    data_type_ids['USBL_SNR'],
                                    float(decoded['snr']),
                                    log_file_id
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
                            
                            # Extract USBL values from ID_USBL_SOLUTION messages
                            if decoded.get('message_name') == 'ID_USBL_SOLUTION' and decoded.get('distance') is not None:
                                # Store distance
                                if decoded.get('distance') > 0:
                                    log_entries.append((
                                        timestamp,
                                        vehicle_type,
                                        vehicle_id,
                                        data_type_ids['USBL_Distance'],
                                        float(decoded['distance']),
                                        log_file_id
                                    ))
                                
                                # Store bearing (azimuth)
                                if decoded.get('azimuth') is not None:
                                    log_entries.append((
                                        timestamp,
                                        vehicle_type,
                                        vehicle_id,
                                        data_type_ids['USBL_Bearing'],
                                        float(decoded['azimuth']),
                                        log_file_id
                                    ))
                                
                                # Store elevation
                                if decoded.get('elevation') is not None:
                                    log_entries.append((
                                        timestamp,
                                        vehicle_type,
                                        vehicle_id,
                                        data_type_ids['USBL_Elevation'],
                                        float(decoded['elevation']),
                                        log_file_id
                                    ))
                                
                                # Store SNR
                                if decoded.get('snr') is not None:
                                    log_entries.append((
                                        timestamp,
                                        vehicle_type,
                                        vehicle_id,
                                        data_type_ids['USBL_SNR'],
                                        float(decoded['snr']),
                                        log_file_id
                                    ))
                
                # Bulk insert every 1000 messages
                if len(decoded_messages) >= 1000:
                    execute_values(cursor, """
                        INSERT INTO usbl_decoded_messages 
                        (log_file_id, timestamp, direction, message_id, message_name, message_type, 
                         version, device_address, payload_decoded, payload_raw, length,
                         distance, bearing, elevation, snr, device_id)
                        VALUES %s
                    """, decoded_messages, page_size=1000)
                    conn.commit()
                    decoded_messages = []
                
                # Bulk insert log entries every 1000 entries
                if len(log_entries) >= 1000:
                    execute_values(cursor, """
                        INSERT INTO log_entries (time, vehicle_type, vehicle_id, data_type_id, value, log_file_id)
                        VALUES %s
                    """, log_entries, page_size=1000)
                    conn.commit()
                    log_entries = []
        else:
            # Fallback: read from file (for old imports without raw data)
            file_path = file_info['file_path']
            filename = file_info['filename']
            
            # Check if file exists on disk
            if not os.path.exists(file_path):
                # Try to find file in logs directory
                import glob
                logs_path = os.path.join('logs', filename)
                if os.path.exists(logs_path):
                    file_path = logs_path
                else:
                    # Try recursive search in logs directory
                    found_files = glob.glob(f'logs/**/{filename}', recursive=True)
                    if found_files:
                        file_path = found_files[0]
                    else:
                        return jsonify({
                            'error': f'File not found on disk and no raw data in database. Original path: {file_path}. Please re-import the file.'
                        }), 404
            
            import pandas as pd
            
            # Read and decode the file
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    parts = line.strip().split(',', 2)
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
                            
                            # Extract USBL values from ID_USBL_SOLUTION messages (SENT)
                            if decoded.get('message_name') == 'ID_USBL_SOLUTION' and decoded.get('distance') is not None:
                                # Store distance
                                if decoded.get('distance') > 0:
                                    log_entries.append((
                                        timestamp,
                                        vehicle_type,
                                        vehicle_id,
                                        data_type_ids['USBL_Distance'],
                                        float(decoded['distance']),
                                        log_file_id
                                    ))
                                
                                # Store bearing (azimuth)
                                if decoded.get('azimuth') is not None:
                                    log_entries.append((
                                        timestamp,
                                        vehicle_type,
                                        vehicle_id,
                                        data_type_ids['USBL_Bearing'],
                                        float(decoded['azimuth']),
                                        log_file_id
                                    ))
                                
                                # Store elevation
                                if decoded.get('elevation') is not None:
                                    log_entries.append((
                                        timestamp,
                                        vehicle_type,
                                        vehicle_id,
                                        data_type_ids['USBL_Elevation'],
                                        float(decoded['elevation']),
                                        log_file_id
                                    ))
                                
                                # Store SNR
                                if decoded.get('snr') is not None:
                                    log_entries.append((
                                        timestamp,
                                        vehicle_type,
                                        vehicle_id,
                                        data_type_ids['USBL_SNR'],
                                        float(decoded['snr']),
                                        log_file_id
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
                                
                                # Extract USBL values from ID_USBL_SOLUTION messages (RECEIVED)
                                if decoded.get('message_name') == 'ID_USBL_SOLUTION' and decoded.get('distance') is not None:
                                    # Store distance
                                    if decoded.get('distance') > 0:
                                        log_entries.append((
                                            timestamp,
                                            vehicle_type,
                                            vehicle_id,
                                            data_type_ids['USBL_Distance'],
                                            float(decoded['distance']),
                                            log_file_id
                                        ))
                                    
                                    # Store bearing (azimuth)
                                    if decoded.get('azimuth') is not None:
                                        log_entries.append((
                                            timestamp,
                                            vehicle_type,
                                            vehicle_id,
                                            data_type_ids['USBL_Bearing'],
                                            float(decoded['azimuth']),
                                            log_file_id
                                        ))
                                    
                                    # Store elevation
                                    if decoded.get('elevation') is not None:
                                        log_entries.append((
                                            timestamp,
                                            vehicle_type,
                                            vehicle_id,
                                            data_type_ids['USBL_Elevation'],
                                            float(decoded['elevation']),
                                            log_file_id
                                        ))
                                    
                                    # Store SNR
                                    if decoded.get('snr') is not None:
                                        log_entries.append((
                                            timestamp,
                                            vehicle_type,
                                            vehicle_id,
                                            data_type_ids['USBL_SNR'],
                                            float(decoded['snr']),
                                            log_file_id
                                        ))
                    
                    # Bulk insert every 1000 messages
                    if len(decoded_messages) >= 1000:
                        execute_values(cursor, """
                            INSERT INTO usbl_decoded_messages 
                            (log_file_id, timestamp, direction, message_id, message_name, message_type, 
                             version, device_address, payload_decoded, payload_raw, length,
                             distance, bearing, elevation, snr, device_id)
                            VALUES %s
                        """, decoded_messages, page_size=1000)
                        conn.commit()
                        decoded_messages = []
                    
                    # Bulk insert log entries every 1000 entries
                    if len(log_entries) >= 1000:
                        execute_values(cursor, """
                            INSERT INTO log_entries (time, vehicle_type, vehicle_id, data_type_id, value, log_file_id)
                            VALUES %s
                        """, log_entries, page_size=1000)
                        conn.commit()
                        log_entries = []
        
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
        if log_entries:
            execute_values(cursor, """
                INSERT INTO log_entries (time, vehicle_type, vehicle_id, data_type_id, value, log_file_id)
                VALUES %s
            """, log_entries, page_size=1000)
            conn.commit()
        
        # Count decoded messages
        cursor.execute("SELECT COUNT(*) as count FROM usbl_decoded_messages WHERE log_file_id = %s", (log_file_id,))
        count = cursor.fetchone()['count']
        
        # Count log entries created
        cursor.execute("""
            SELECT COUNT(*) as count FROM log_entries 
            WHERE log_file_id = %s AND data_type_id IN %s
        """, (log_file_id, tuple(data_type_ids.values())))
        log_entries_count = cursor.fetchone()['count']
        
        # Invalidate AUV position cache for all AUV runs (USBL data affects all runs)
        try:
            cursor.execute("SELECT id FROM log_files WHERE vehicle_type = 'AUV' AND import_status = 'completed'")
            auv_runs = cursor.fetchall()
            from database.invalidate_cache import invalidate_auv_position_cache
            for run in auv_runs:
                invalidate_auv_position_cache(run['id'], conn=conn, queue_recalculation=True)
            print(f"Invalidated AUV position cache for {len(auv_runs)} runs after USBL decode")
        except Exception as e:
            print(f"Warning: Could not invalidate AUV position cache: {e}")
        
        return jsonify({
            'status': 'success',
            'message': f'File decoded successfully. {count} messages decoded, {log_entries_count} USBL values extracted.',
            'decoded_count': count,
            'log_entries_count': log_entries_count
        })
        
    except Exception as e:
        conn.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/recalculate_position', methods=['POST'])
def recalculate_auv_position(run_id):
    """API endpoint to synchronously recalculate AUV positions."""
    import time
    start_time = time.time()
    
    try:
        print(f"[RECALC] Starting recalculation for run {run_id}...")
        from database.calculate_auv_positions import calculate_auv_positions_for_run
        
        # Calculate synchronously
        success, points_calculated, error_message = calculate_auv_positions_for_run(run_id)
        
        elapsed_time = time.time() - start_time
        print(f"[RECALC] Calculation completed in {elapsed_time:.2f}s: success={success}, points={points_calculated}")
        
        if success:
            return jsonify({
                'status': 'success',
                'message': f'Recalcul réussi: {points_calculated} positions calculées',
                'points_calculated': points_calculated
            })
        else:
            return jsonify({
                'status': 'error',
                'error': error_message or 'Erreur lors du recalcul'
            }), 500
            
    except Exception as e:
        import traceback
        elapsed_time = time.time() - start_time
        error_traceback = traceback.format_exc()
        print(f"[RECALC] Error after {elapsed_time:.2f}s: {str(e)}")
        print(f"[RECALC] Traceback:\n{error_traceback}")
        return jsonify({
            'status': 'error',
            'error': str(e),
            'traceback': error_traceback
        }), 500

@app.route('/api/run/<int:run_id>/auv_calculated_position')
@monitor_performance
def get_run_auv_calculated_position(run_id):
    """
    API endpoint to get AUV calculated positions (from cache if available).
    If cache doesn't exist, calculate on-demand and queue for background calculation.
    """
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Check cache status first
        cursor.execute("""
            SELECT status, completed_at, points_calculated, error_message, calculation_version
            FROM auv_position_calculation_status
            WHERE log_file_id = %s
        """, (run_id,))
        cache_status = cursor.fetchone()
        
        # If cache exists and is completed, return it
        if cache_status and cache_status['status'] == 'completed':
            cursor.execute("""
                SELECT time, lat, lon
                FROM auv_calculated_positions
                WHERE log_file_id = %s
                ORDER BY time ASC
            """, (run_id,))
            positions = cursor.fetchall()
            
            result = []
            for row in positions:
                result.append({
                    'time': row['time'].isoformat() if row['time'] else None,
                    'lat': row['lat'],
                    'lon': row['lon']
                })
            
            print(f"[Cache HIT] Returning {len(result)} cached positions for run {run_id}")
            return jsonify(result)
        
        # If currently calculating, return status
        if cache_status and cache_status['status'] == 'calculating':
            print(f"[Cache CALCULATING] Run {run_id} positions are being calculated in background")
            return jsonify({
                'status': 'calculating',
                'message': 'Positions are being calculated in background. Please refresh in a moment.',
                'data': []
            })
        
        # If failed, retry calculation
        if cache_status and cache_status['status'] == 'failed':
            print(f"[Cache FAILED] Previous calculation failed for run {run_id}: {cache_status.get('error_message')}")
            # Queue for retry
            try:
                from database.background_calculator import queue_calculation
                queue_calculation(run_id)
            except Exception as e:
                print(f"Error queuing calculation: {e}")
        
        # Otherwise, calculate synchronously (fallback) and queue for background
        print(f"[Cache MISS] Calculating positions on-demand for run {run_id}...")
        
        # Queue for background calculation (for next time)
        try:
            from database.background_calculator import queue_calculation
            queue_calculation(run_id)
        except Exception as e:
            print(f"Error queuing calculation: {e}")
        
        # Calculate synchronously as fallback (OLD CODE BELOW)
        # First, get the time range of the AUV run
        cursor.execute("""
            SELECT first_timestamp, last_timestamp
            FROM log_files
            WHERE id = %s AND vehicle_type = 'AUV'
        """, (run_id,))
        
        run_info = cursor.fetchone()
        if not run_info:
            return jsonify({'error': 'Run not found'}), 404
        
        start_time = run_info['first_timestamp']
        end_time = run_info['last_timestamp']
        
        # Get USV full AUV data (distance and bearing)
        # NOTE: The data is stored in the USV full file, not the AUV navigation file
        #       So we search by time range, not by log_file_id
        query = """
            SELECT 
                le.time,
                dtc.data_type,
                le.value
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE dtc.data_type IN ('USV_FULL_AUV_Distance', 'USV_FULL_AUV_Bearing')
              AND le.time >= %s
              AND le.time <= %s
            ORDER BY le.time ASC, dtc.data_type ASC
        """
        cursor.execute(query, (start_time, end_time))
        usv_full_data = cursor.fetchall()
        
        # Group by distance and bearing
        distance_data = []
        bearing_data = []
        for row in usv_full_data:
            entry = {
                'time': row['time'].isoformat() if row['time'] else None,
                'value': row['value']
            }
            if row['data_type'] == 'USV_FULL_AUV_Distance':
                distance_data.append(entry)
            elif row['data_type'] == 'USV_FULL_AUV_Bearing':
                bearing_data.append(entry)
        
        # Get USV heading data
        query = """
            SELECT 
                le.time,
                le.value
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.vehicle_type = 'USV'
              AND dtc.data_type = 'Heading'
              AND le.time >= %s
              AND le.time <= %s
            ORDER BY le.time ASC
        """
        cursor.execute(query, (start_time, end_time))
        heading_data = cursor.fetchall()
        usv_heading = [{'time': row['time'].isoformat() if row['time'] else None, 'value': row['value']} for row in heading_data]
        
        # Get USV position data (lat/lon)
        query_lat = """
            SELECT 
                le.time,
                le.value as lat_raw
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.vehicle_type = 'USV'
              AND dtc.data_type = 'GPS_RAW_INT_lat'
              AND le.time >= %s
              AND le.time <= %s
            ORDER BY le.time ASC
        """
        cursor.execute(query_lat, (start_time, end_time))
        lat_data = cursor.fetchall()
        
        query_lon = """
            SELECT 
                le.time,
                le.value as lon_raw
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.vehicle_type = 'USV'
              AND dtc.data_type = 'GPS_RAW_INT_lon'
              AND le.time >= %s
              AND le.time <= %s
            ORDER BY le.time ASC
        """
        cursor.execute(query_lon, (start_time, end_time))
        lon_data = cursor.fetchall()
        
        # Combine lat and lon by matching closest timestamps
        usv_position = []
        lon_idx = 0
        for lat_row in lat_data:
            lat_time = lat_row['time']
            lat_raw = lat_row['lat_raw']
            
            if not lat_time:
                continue
            
            lat_epoch = lat_time.timestamp()
            
            # Find closest lon point
            best_lon = None
            best_diff = float('inf')
            
            for i in range(lon_idx, len(lon_data)):
                lon_row = lon_data[i]
                lon_time = lon_row['time']
                lon_raw = lon_row['lon_raw']
                
                if not lon_time:
                    continue
                
                lon_epoch = lon_time.timestamp()
                diff = abs(lat_epoch - lon_epoch)
                
                if diff > best_diff and i > lon_idx:
                    break
                    
                if diff < best_diff:
                    best_diff = diff
                    best_lon = (lon_time, lon_raw)
                    lon_idx = i
            
            # Check backwards a bit
            for i in range(max(0, lon_idx - 10), lon_idx):
                lon_row = lon_data[i]
                lon_time = lon_row['time']
                lon_raw = lon_row['lon_raw']
                
                if not lon_time:
                    continue
                
                lon_epoch = lon_time.timestamp()
                diff = abs(lat_epoch - lon_epoch)
                
                if diff < best_diff:
                    best_diff = diff
                    best_lon = (lon_time, lon_raw)
            
            # Only include if we found a match within 1 second
            if best_lon and best_diff < 1.0:
                lon_time, lon_raw = best_lon
                # Check for None values and format (GPS_RAW_INT vs degrees)
                # Lat and Lon can be in different formats, so check each independently
                if lat_raw is not None and lon_raw is not None:
                    # Convert lat independently
                    if abs(lat_raw) > 1e6:
                        # GPS_RAW_INT format: divide by 1e7
                        lat_degrees = lat_raw / 1e7
                    else:
                        # Already in degrees
                        lat_degrees = lat_raw
                    
                    # Convert lon independently (may be different format than lat!)
                    if abs(lon_raw) > 1e6:
                        # GPS_RAW_INT format: divide by 1e7
                        lon_degrees = lon_raw / 1e7
                    else:
                        # Already in degrees
                        lon_degrees = lon_raw
                    
                    usv_position.append({
                        'time': lat_time.isoformat(),
                        'lat': lat_degrees,
                        'lon': lon_degrees
                    })
        
        # Get AUV depth data
        query = """
            SELECT 
                le.time,
                le.value as depth
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.log_file_id = %s
              AND dtc.data_type = 'Depth'
            ORDER BY le.time ASC
        """
        cursor.execute(query, (run_id,))
        depth_data = cursor.fetchall()
        auv_depth = [{'time': row['time'].isoformat() if row['time'] else None, 'value': row['depth']} for row in depth_data]
        # Ensure depth data is sorted by time (required for interpolation)
        auv_depth.sort(key=lambda x: x['time'] if x['time'] else '')
        
        # Calculate AUV positions
        result = []
        
        # Match distance and bearing by timestamp (they should be synchronized)
        distance_dict = {d['time']: d['value'] for d in distance_data if d['time']}
        bearing_dict = {b['time']: b['value'] for b in bearing_data if b['time']}
        
        # Get all timestamps that have both distance and bearing
        common_timestamps = set(distance_dict.keys()) & set(bearing_dict.keys())
        
        for timestamp_str in sorted(common_timestamps):
            distance = distance_dict[timestamp_str]
            bearing = bearing_dict[timestamp_str]
            
            # Interpolate USV heading (allow up to 5 seconds gap)
            usv_heading_value = interpolate_value(timestamp_str, usv_heading, max_time_gap_seconds=5.0)
            if usv_heading_value is None:
                continue
            
            # Interpolate USV position
            usv_pos = interpolate_position(timestamp_str, usv_position)
            if usv_pos is None:
                continue
            
            # Interpolate AUV depth (allow up to 5 seconds gap)
            depth_value = interpolate_value(timestamp_str, auv_depth, max_time_gap_seconds=5.0)
            # Accept negative depth values (AUV can be above surface) but reject None
            if depth_value is None:
                continue
            
            # Calculate horizontal distance (Pythagorean theorem)
            # Use absolute value of depth (depth can be negative if AUV is above surface)
            # Note: Distance might be in decimeters, so divide by 10 to convert to meters
            distance_m = distance / 10.0
            depth_abs = abs(depth_value)
            if distance_m <= depth_abs:
                # Invalid: distance should be >= depth
                continue
            
            horizontal_distance = math.sqrt(distance_m * distance_m - depth_abs * depth_abs)
            
            # Calculate absolute direction (USV heading + USBL bearing)
            # USBL bearing is relative to USV heading, so we add them
            absolute_bearing = (usv_heading_value + bearing) % 360
            if absolute_bearing < 0:
                absolute_bearing += 360
            
            # Calculate AUV position from USV position
            auv_lat, auv_lon = calculate_position_from_distance_bearing(
                usv_pos['lat'], usv_pos['lon'],
                horizontal_distance,
                absolute_bearing
            )
            
            result.append({
                'time': timestamp_str,
                'lat': auv_lat,
                'lon': auv_lon
            })
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
        
    finally:
        cursor.close()
        conn.close()

def associate_video_to_run(video_id, effective_start_time, duration_seconds):
    """
    Automatically associate a video to a run if their time ranges overlap.
    """
    if not effective_start_time or not duration_seconds:
        return None
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Convert to datetime if needed
        if isinstance(effective_start_time, str):
            effective_start_time = pd.Timestamp(effective_start_time)
        elif not isinstance(effective_start_time, pd.Timestamp):
            effective_start_time = pd.Timestamp(effective_start_time)
        
        effective_end_time = effective_start_time + pd.Timedelta(seconds=duration_seconds)
        
        # Find runs that overlap with video time range
        query = """
            SELECT id, first_timestamp, last_timestamp
            FROM log_files
            WHERE vehicle_type = 'AUV'
              AND first_timestamp IS NOT NULL
              AND last_timestamp IS NOT NULL
              AND (
                  (first_timestamp <= %s AND last_timestamp >= %s) OR
                  (first_timestamp <= %s AND last_timestamp >= %s) OR
                  (first_timestamp >= %s AND last_timestamp <= %s)
              )
            ORDER BY first_timestamp
            LIMIT 1
        """
        cursor.execute(query, (
            effective_start_time, effective_start_time,
            effective_end_time, effective_end_time,
            effective_start_time, effective_end_time
        ))
        result = cursor.fetchone()
        
        if result:
            run_id = result[0]
            cursor.execute("""
                UPDATE videos 
                SET run_id = %s 
                WHERE id = %s
            """, (run_id, video_id))
            conn.commit()
            return run_id
        
        return None
    except Exception as e:
        print(f"Error associating video {video_id} to run: {str(e)}")
        return None
    finally:
        cursor.close()
        conn.close()

@app.route('/api/videos/upload', methods=['POST'])
def upload_videos():
    """API endpoint to upload video files."""
    try:
        if 'files' not in request.files:
            return jsonify({'status': 'error', 'message': 'Aucun fichier fourni'}), 400
        
        files = request.files.getlist('files')
        if not files or all(f.filename == '' for f in files):
            return jsonify({'status': 'error', 'message': 'Aucun fichier sélectionné'}), 400
        
        # Filter video files
        video_extensions = ['.mp4', '.mov', '.avi', '.mkv', '.m4v']
        video_files = [f for f in files if any(f.filename.lower().endswith(ext) for ext in video_extensions)]
        
        if not video_files:
            return jsonify({'status': 'error', 'message': 'Aucun fichier vidéo trouvé'}), 400
        
        # Create videos directory
        videos_dir = Path('static/videos')
        videos_dir.mkdir(parents=True, exist_ok=True)
        
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        uploaded_videos = []
        
        try:
            for file in video_files:
                # Save file
                filename = file.filename
                file_path = videos_dir / filename
                file.save(str(file_path))
                
                # Extract metadata
                metadata = extract_video_metadata(str(file_path))
                
                # Insert into database
                cursor.execute("""
                    INSERT INTO videos (filename, file_path, start_time, duration_seconds, thumbnail_status)
                    VALUES (%s, %s, %s, %s, 'pending')
                    RETURNING id, effective_start_time
                """, (
                    filename,
                    str(file_path),
                    metadata['start_time'],
                    metadata['duration_seconds']
                ))
                result = cursor.fetchone()
                video_id = result['id']
                effective_start_time = result['effective_start_time']
                
                # Associate to run if possible
                if effective_start_time and metadata['duration_seconds']:
                    run_id = associate_video_to_run(video_id, effective_start_time, metadata['duration_seconds'])
                else:
                    run_id = None
                
                # Start thumbnail extraction in background thread
                thread = threading.Thread(
                    target=extract_video_thumbnails,
                    args=(video_id, str(file_path))
                )
                thread.daemon = True
                thread.start()
                
                uploaded_videos.append({
                    'id': video_id,
                    'filename': filename,
                    'start_time': metadata['start_time'].isoformat() if metadata['start_time'] else None,
                    'duration_seconds': metadata['duration_seconds'],
                    'time_offset_seconds': 0.0,
                    'effective_start_time': effective_start_time.isoformat() if effective_start_time else None,
                    'thumbnail_status': 'pending',
                    'run_id': run_id,
                    'camera_id': None
                })
            
            conn.commit()
            
            return jsonify({
                'status': 'success',
                'message': f'{len(uploaded_videos)} vidéo(s) uploadée(s)',
                'videos': uploaded_videos
            })
            
        except Exception as e:
            conn.rollback()
            import traceback
            traceback.print_exc()
            return jsonify({'status': 'error', 'message': f'Erreur lors de l\'upload: {str(e)}'}), 500
        finally:
            cursor.close()
            conn.close()
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': f'Erreur: {str(e)}'}), 500

@app.route('/api/videos', methods=['GET'])
def get_videos():
    """API endpoint to get all videos."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Check if table exists first
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name = 'videos'
            ) as exists
        """)
        result = cursor.fetchone()
        table_exists = result['exists'] if result else False
        
        if not table_exists:
            return jsonify({
                'error': 'Videos table does not exist. Please run: python3 database/setup.py'
            }), 500
        
        cursor.execute("""
            SELECT 
                id,
                filename,
                file_path,
                start_time,
                time_offset_seconds,
                effective_start_time,
                duration_seconds,
                thumbnail_status,
                run_id,
                camera_id,
                created_at
            FROM videos
            ORDER BY created_at DESC
        """)
        videos = cursor.fetchall()
        
        result = []
        for video in videos:
            result.append({
                'id': video['id'],
                'filename': video['filename'],
                'start_time': video['start_time'].isoformat() if video['start_time'] else None,
                'time_offset_seconds': float(video['time_offset_seconds']) if video['time_offset_seconds'] else 0.0,
                'effective_start_time': video['effective_start_time'].isoformat() if video['effective_start_time'] else None,
                'duration_seconds': float(video['duration_seconds']) if video['duration_seconds'] else None,
                'thumbnail_status': video['thumbnail_status'],
                'run_id': video['run_id'],
                'camera_id': video['camera_id'],
                'created_at': video['created_at'].isoformat() if video['created_at'] else None
            })
        
        return jsonify(result)
    finally:
        cursor.close()
        conn.close()

@app.route('/api/videos/<int:video_id>/offset', methods=['PUT'])
def update_video_offset(video_id):
    """API endpoint to update video time offset."""
    data = request.get_json()
    if 'time_offset_seconds' not in data:
        return jsonify({'error': 'time_offset_seconds is required'}), 400
    
    try:
        offset = float(data['time_offset_seconds'])
    except (ValueError, TypeError):
        return jsonify({'error': 'time_offset_seconds must be a number'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Update offset (trigger will update effective_start_time)
        cursor.execute("""
            UPDATE videos 
            SET time_offset_seconds = %s
            WHERE id = %s
            RETURNING id, effective_start_time, duration_seconds
        """, (offset, video_id))
        
        result = cursor.fetchone()
        if not result:
            return jsonify({'error': 'Video not found'}), 404
        
        effective_start_time = result['effective_start_time']
        duration_seconds = result['duration_seconds']
        
        # Reassociate to run if needed
        if effective_start_time and duration_seconds:
            associate_video_to_run(video_id, effective_start_time, duration_seconds)
        
        conn.commit()
        
        # Get updated video data
        cursor.execute("""
            SELECT 
                id,
                filename,
                start_time,
                time_offset_seconds,
                effective_start_time,
                duration_seconds,
                thumbnail_status,
                run_id
            FROM videos
            WHERE id = %s
        """, (video_id,))
        video = cursor.fetchone()
        
        return jsonify({
            'id': video['id'],
            'filename': video['filename'],
            'start_time': video['start_time'].isoformat() if video['start_time'] else None,
            'time_offset_seconds': float(video['time_offset_seconds']) if video['time_offset_seconds'] else 0.0,
            'effective_start_time': video['effective_start_time'].isoformat() if video['effective_start_time'] else None,
            'duration_seconds': float(video['duration_seconds']) if video['duration_seconds'] else None,
            'thumbnail_status': video['thumbnail_status'],
            'run_id': video['run_id'],
            'camera_id': video['camera_id']
        })
    finally:
        cursor.close()
        conn.close()

@app.route('/api/videos/<int:video_id>/camera', methods=['PUT'])
def update_video_camera(video_id):
    """API endpoint to update video camera_id."""
    data = request.get_json()
    if 'camera_id' not in data:
        return jsonify({'error': 'camera_id is required'}), 400
    
    try:
        camera_id = int(data['camera_id']) if data['camera_id'] is not None and data['camera_id'] != '' else None
    except (ValueError, TypeError):
        return jsonify({'error': 'camera_id must be a number or null'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        cursor.execute("""
            UPDATE videos 
            SET camera_id = %s
            WHERE id = %s
            RETURNING id, camera_id
        """, (camera_id, video_id))
        
        result = cursor.fetchone()
        if not result:
            return jsonify({'error': 'Video not found'}), 404
        
        conn.commit()
        
        return jsonify({
            'id': result['id'],
            'camera_id': result['camera_id']
        })
    finally:
        cursor.close()
        conn.close()

@app.route('/api/videos/<int:video_id>/thumbnail', methods=['GET'])
def get_video_thumbnail(video_id):
    """API endpoint to get thumbnail closest to a timestamp."""
    timestamp_str = request.args.get('timestamp')
    if not timestamp_str:
        return jsonify({'error': 'timestamp parameter is required'}), 400
    
    try:
        # Timestamp can be in milliseconds (from JavaScript) or ISO format
        if timestamp_str.isdigit():
            # Assume milliseconds
            timestamp_ms = int(timestamp_str)
            target_time = pd.Timestamp(datetime.fromtimestamp(timestamp_ms / 1000.0))
        else:
            # Try to parse as ISO format
            target_time = pd.Timestamp(timestamp_str)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid timestamp format'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Get video info
        cursor.execute("""
            SELECT effective_start_time, duration_seconds
            FROM videos
            WHERE id = %s
        """, (video_id,))
        video = cursor.fetchone()
        
        if not video or not video['effective_start_time'] or not video['duration_seconds']:
            return jsonify({'error': 'Video not found or missing metadata'}), 404
        
        # Calculate relative time in video
        effective_start = pd.Timestamp(video['effective_start_time'])
        relative_time = (target_time - effective_start).total_seconds()
        
        if relative_time < 0 or relative_time > video['duration_seconds']:
            return jsonify({'error': 'Timestamp outside video range'}), 404
        
        # Find closest thumbnail using database query (optimized with index)
        # Round to nearest 0.5s interval
        target_offset = round(relative_time * 2) / 2.0
        
        # Query for exact match first, then closest if not found
        cursor.execute("""
            SELECT time_offset, file_path
            FROM video_thumbnails
            WHERE video_id = %s
              AND time_offset = %s
            LIMIT 1
        """, (video_id, target_offset))
        
        thumbnail = cursor.fetchone()
        
        # If exact match not found, find closest thumbnail
        if not thumbnail:
            cursor.execute("""
                SELECT time_offset, file_path
                FROM video_thumbnails
                WHERE video_id = %s
                  AND time_offset >= %s
                ORDER BY time_offset ASC
                LIMIT 1
            """, (video_id, target_offset))
            
            thumbnail_after = cursor.fetchone()
            
            cursor.execute("""
                SELECT time_offset, file_path
                FROM video_thumbnails
                WHERE video_id = %s
                  AND time_offset <= %s
                ORDER BY time_offset DESC
                LIMIT 1
            """, (video_id, target_offset))
            
            thumbnail_before = cursor.fetchone()
            
            # Choose closest thumbnail
            if thumbnail_after and thumbnail_before:
                diff_after = abs(thumbnail_after['time_offset'] - target_offset)
                diff_before = abs(thumbnail_before['time_offset'] - target_offset)
                thumbnail = thumbnail_after if diff_after < diff_before else thumbnail_before
            elif thumbnail_after:
                thumbnail = thumbnail_after
            elif thumbnail_before:
                thumbnail = thumbnail_before
        
        if not thumbnail:
            return jsonify({'error': 'Thumbnail not found'}), 404
        
        thumbnail_time = float(thumbnail['time_offset'])
        thumbnail_path = thumbnail['file_path']
        
        # Return relative path for serving
        return jsonify({
            'thumbnail_path': thumbnail_path,
            'thumbnail_time': thumbnail_time,
            'target_time': relative_time,
            'time_difference': relative_time - thumbnail_time
        })
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/video', methods=['GET'])
def get_run_video(run_id):
    """API endpoint to get video associated with a run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Get run time range
        cursor.execute("""
            SELECT first_timestamp, last_timestamp
            FROM log_files
            WHERE id = %s AND vehicle_type = 'AUV'
        """, (run_id,))
        run = cursor.fetchone()
        
        if not run:
            return jsonify({'error': 'Run not found'}), 404
        
        if not run['first_timestamp'] or not run['last_timestamp']:
            return jsonify({'video': None})
        
        # Find video that overlaps with run
        cursor.execute("""
            SELECT 
                id,
                filename,
                start_time,
                time_offset_seconds,
                effective_start_time,
                duration_seconds,
                thumbnail_status
            FROM videos
            WHERE run_id = %s
               OR (
                   effective_start_time IS NOT NULL
                   AND duration_seconds IS NOT NULL
                   AND effective_start_time <= %s
                   AND (effective_start_time + (duration_seconds || ' seconds')::INTERVAL) >= %s
               )
            ORDER BY effective_start_time
            LIMIT 1
        """, (run_id, run['last_timestamp'], run['first_timestamp']))
        
        video = cursor.fetchone()
        
        if not video:
            return jsonify({'video': None})
        
        return jsonify({
            'video': {
                'id': video['id'],
                'filename': video['filename'],
                'start_time': video['start_time'].isoformat() if video['start_time'] else None,
                'time_offset_seconds': float(video['time_offset_seconds']) if video['time_offset_seconds'] else 0.0,
                'effective_start_time': video['effective_start_time'].isoformat() if video['effective_start_time'] else None,
                'duration_seconds': float(video['duration_seconds']) if video['duration_seconds'] else None,
                'thumbnail_status': video['thumbnail_status']
            }
        })
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/all_data')
@monitor_performance
def get_run_all_data(run_id):
    """API endpoint to get all data for a run, pivoted by timestamp.
    Uses sampling to avoid loading too much data at once."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Get vehicle type, row count, and time range for this run
        cursor.execute("""
            SELECT vehicle_type, row_count, first_timestamp, last_timestamp
            FROM log_files 
            WHERE id = %s
        """, (run_id,))
        run_info = cursor.fetchone()
        if not run_info:
            return jsonify({'error': 'Run not found'}), 404
        
        vehicle_type = run_info['vehicle_type']
        row_count = run_info['row_count'] or 0
        run_start_time = run_info['first_timestamp']
        run_end_time = run_info['last_timestamp']
        
        # Determine sampling interval based on data size
        # For large datasets (>100k rows), sample at 1 point per second
        # For smaller datasets, use all data
        use_sampling = row_count > 100000
        sample_interval = 1.0 if use_sampling else None
        
        # Get all data types from the run's file (AUV data)
        cursor.execute("""
            SELECT DISTINCT dtc.data_type
            FROM log_entries le
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE le.log_file_id = %s
            ORDER BY dtc.data_type
            LIMIT 500
        """, (run_id,))
        run_data_types = [row['data_type'] for row in cursor.fetchall()]
        
        # Also get USV data types from all USV files in the same time period
        usv_data_types_from_period = []
        if run_start_time and run_end_time:
            cursor.execute("""
                SELECT DISTINCT dtc.data_type
                FROM log_entries le
                JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                WHERE le.vehicle_type = 'USV'
                  AND le.time >= %s
                  AND le.time <= %s
                ORDER BY dtc.data_type
                LIMIT 500
            """, (run_start_time, run_end_time))
            usv_data_types_from_period = [row['data_type'] for row in cursor.fetchall()]
        
        # Combine all data types
        data_types = sorted(list(set(run_data_types + usv_data_types_from_period)))
        
        if not data_types:
            return jsonify({
                'data_types': [],
                'auv_data_types': [],
                'usv_data_types': [],
                'timestamps': [],
                'data': [],
                'sampled': False,
                'total_rows': row_count,
                'displayed_rows': 0
            })
        
        # Get vehicle types for these data types (separate query to avoid complex JOIN)
        placeholders = ','.join(['%s'] * len(data_types))
        cursor.execute(f"""
            SELECT data_type, COALESCE(vehicle_type, 'BOTH') as vehicle_type
            FROM data_type_catalog
            WHERE data_type IN ({placeholders})
        """, data_types)
        vehicle_type_map = {row['data_type']: row['vehicle_type'] for row in cursor.fetchall()}
        
        # Separate data types by vehicle type
        auv_data_types = []
        usv_data_types = []
        common_data_types = []
        
        for dt_name in data_types:
            dt_vehicle_type = vehicle_type_map.get(dt_name, 'BOTH')
            
            if dt_vehicle_type == 'AUV':
                auv_data_types.append(dt_name)
            elif dt_vehicle_type == 'USV':
                usv_data_types.append(dt_name)
            else:  # 'BOTH' or unknown
                common_data_types.append(dt_name)
        
        # Add common types to both lists
        auv_data_types = sorted(list(set(auv_data_types + common_data_types)))
        usv_data_types = sorted(list(set(usv_data_types + common_data_types)))
        
        # Ensure that if this is a USV run, all its data types appear in usv_data_types
        # And if this is an AUV run, all its data types appear in auv_data_types
        # This handles cases where data types might be incorrectly categorized in the catalog
        if vehicle_type == 'USV':
            # Add all data types from this USV run to usv_data_types
            usv_data_types = sorted(list(set(usv_data_types + data_types)))
        elif vehicle_type == 'AUV':
            # Add all data types from this AUV run to auv_data_types
            auv_data_types = sorted(list(set(auv_data_types + data_types)))
            # Also add USV data types from the period to usv_data_types
            # (even if they're not in the run's file, they're relevant for the USV tab)
            if usv_data_types_from_period:
                usv_data_types = sorted(list(set(usv_data_types + usv_data_types_from_period)))
        
        # Fallback: if no separation worked, add all to both lists
        if len(auv_data_types) == 0 and len(usv_data_types) == 0 and len(data_types) > 0:
            auv_data_types = sorted(data_types)
            usv_data_types = sorted(data_types)
        
        if not data_types:
            return jsonify({
                'data_types': [],
                'auv_data_types': [],
                'usv_data_types': [],
                'timestamps': [],
                'data': [],
                'sampled': False,
                'total_rows': 0
            })
        
        # Get timestamps from the run's file (AUV data)
        if use_sampling:
            # Get one point per second for each timestamp
            cursor.execute("""
                SELECT DISTINCT ON (FLOOR(EXTRACT(EPOCH FROM le.time)))
                    le.time
                FROM log_entries le
                WHERE le.log_file_id = %s
                ORDER BY FLOOR(EXTRACT(EPOCH FROM le.time)), le.time ASC
            """, (run_id,))
        else:
            # Get all unique timestamps
            cursor.execute("""
                SELECT DISTINCT time
                FROM log_entries
                WHERE log_file_id = %s
                ORDER BY time ASC
            """, (run_id,))
        
        run_timestamps = [row['time'] for row in cursor.fetchall()]
        
        # Also get timestamps from USV files in the same time period
        usv_timestamps = []
        if run_start_time and run_end_time:
            if use_sampling:
                cursor.execute("""
                    SELECT DISTINCT ON (FLOOR(EXTRACT(EPOCH FROM le.time)))
                        le.time
                    FROM log_entries le
                    WHERE le.vehicle_type = 'USV'
                      AND le.time >= %s
                      AND le.time <= %s
                    ORDER BY FLOOR(EXTRACT(EPOCH FROM le.time)), le.time ASC
                """, (run_start_time, run_end_time))
            else:
                cursor.execute("""
                    SELECT DISTINCT time
                    FROM log_entries
                    WHERE vehicle_type = 'USV'
                      AND time >= %s
                      AND time <= %s
                    ORDER BY time ASC
                """, (run_start_time, run_end_time))
            usv_timestamps = [row['time'] for row in cursor.fetchall()]
        
        # Combine and deduplicate timestamps
        all_timestamps = sorted(list(set(run_timestamps + usv_timestamps)))
        timestamps = all_timestamps
        
        if not timestamps:
            return jsonify({
                'data_types': data_types,
                'auv_data_types': auv_data_types,
                'usv_data_types': usv_data_types,
                'timestamps': [],
                'data': [],
                'sampled': use_sampling,
                'total_rows': row_count,
                'displayed_rows': 0
            })
        
        # Limit the number of timestamps to avoid memory issues
        # Even with sampling, limit to 5000 rows max to prevent server crashes
        max_rows = 5000
        if len(timestamps) > max_rows:
            # Take evenly spaced samples
            step = max(1, len(timestamps) // max_rows)
            timestamps = timestamps[::step][:max_rows]
            use_sampling = True
        
        # Build data structure - fetch data for each timestamp
        data_by_time = {}
        
        # Fetch data in batches to avoid memory issues
        batch_size = 1000
        for i in range(0, len(timestamps), batch_size):
            batch_timestamps = timestamps[i:i+batch_size]
            
            # Fetch data from the run's file (AUV data)
            placeholders = ','.join(['%s'] * len(batch_timestamps))
            query = f"""
                SELECT 
                    le.time,
                    dtc.data_type,
                    le.value
                FROM log_entries le
                JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                WHERE le.log_file_id = %s
                  AND le.time IN ({placeholders})
                ORDER BY le.time ASC, dtc.data_type ASC
            """
            cursor.execute(query, [run_id] + batch_timestamps)
            
            for row in cursor.fetchall():
                timestamp = row['time']
                data_type = row['data_type']
                value = row['value']
                
                if timestamp not in data_by_time:
                    data_by_time[timestamp] = {}
                
                data_by_time[timestamp][data_type] = value
            
            # Also fetch USV data from all USV files in the time period
            if run_start_time and run_end_time:
                query_usv = f"""
                    SELECT 
                        le.time,
                        dtc.data_type,
                        le.value
                    FROM log_entries le
                    JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                    WHERE le.vehicle_type = 'USV'
                      AND le.time >= %s
                      AND le.time <= %s
                      AND le.time IN ({placeholders})
                    ORDER BY le.time ASC, dtc.data_type ASC
                """
                cursor.execute(query_usv, [run_start_time, run_end_time] + batch_timestamps)
                
                for row in cursor.fetchall():
                    timestamp = row['time']
                    data_type = row['data_type']
                    value = row['value']
                    
                    if timestamp not in data_by_time:
                        data_by_time[timestamp] = {}
                    
                    # USV data can override AUV data if same timestamp and data_type
                    # (or we could merge them, but typically they're different types)
                    data_by_time[timestamp][data_type] = value
        
        # Build result
        result = {
            'data_types': data_types,
            'auv_data_types': auv_data_types,
            'usv_data_types': usv_data_types,
            'timestamps': [ts.isoformat() if hasattr(ts, 'isoformat') else str(ts) for ts in timestamps],
            'data': [],
            'sampled': use_sampling,
            'total_rows': row_count,
            'displayed_rows': len(timestamps)
        }
        
        for timestamp in timestamps:
            result['data'].append({
                'time': timestamp.isoformat() if hasattr(timestamp, 'isoformat') else str(timestamp),
                'values': data_by_time.get(timestamp, {})
            })
        
        return jsonify(result)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/filtered_data')
@monitor_performance
def get_run_filtered_data(run_id):
    """
    API endpoint to get filtered data for a run based on selected columns.
    Only returns rows that have at least one value in the selected columns.
    Supports pagination (100 rows per page).
    """
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Get run time range first
        cursor.execute("""
            SELECT first_timestamp, last_timestamp, vehicle_type
            FROM log_files 
            WHERE id = %s
        """, (run_id,))
        run_info = cursor.fetchone()
        if not run_info:
            return jsonify({'error': 'Run not found'}), 404
        
        run_start_time = run_info['first_timestamp']
        run_end_time = run_info['last_timestamp']
        run_vehicle_type = run_info['vehicle_type']
        
        # Get selected columns from query parameters
        selected_columns = request.args.getlist('columns')
        page = int(request.args.get('page', 1))
        rows_per_page = int(request.args.get('rows_per_page', 100))
        
        print(f"[DEBUG] get_run_filtered_data: run_id={run_id}, columns={selected_columns}, page={page}, rows_per_page={rows_per_page}")
        
        if not selected_columns:
            return jsonify({
                'data': [],
                'total_rows': 0,
                'page': page,
                'rows_per_page': rows_per_page,
                'total_pages': 0
            })
        
        # Get data type IDs and vehicle types for selected columns
        placeholders = ','.join(['%s'] * len(selected_columns))
        cursor.execute(f"""
            SELECT id, data_type, COALESCE(vehicle_type, 'BOTH') as vehicle_type
            FROM data_type_catalog
            WHERE data_type IN ({placeholders})
        """, selected_columns)
        data_type_info = cursor.fetchall()
        data_type_map = {row['data_type']: row['id'] for row in data_type_info}
        data_type_ids = list(data_type_map.values())
        
        # Separate columns by vehicle type
        auv_columns = []
        usv_columns = []
        both_columns = []
        for row in data_type_info:
            if row['vehicle_type'] == 'AUV':
                auv_columns.append(row['data_type'])
            elif row['vehicle_type'] == 'USV':
                usv_columns.append(row['data_type'])
            else:
                both_columns.append(row['data_type'])
        
        # Determine which columns to fetch from which source
        # For AUV run: AUV columns from run file, USV columns from USV files in period
        # For USV run: USV columns from run file, AUV columns from AUV files in period (if any)
        if run_vehicle_type == 'AUV':
            columns_from_run = auv_columns + both_columns
            columns_from_period = usv_columns
            period_vehicle_type = 'USV'
        else:  # USV run
            columns_from_run = usv_columns + both_columns
            columns_from_period = auv_columns
            period_vehicle_type = 'AUV'
        
        print(f"[DEBUG] Found {len(data_type_ids)} data type IDs for columns: {data_type_map}")
        
        if not data_type_ids:
            return jsonify({
                'data': [],
                'total_rows': 0,
                'page': page,
                'rows_per_page': rows_per_page,
                'total_pages': 0,
                'message': f'No data types found for columns: {selected_columns}'
            })
        
        # Get total count of rows that have at least one value in selected columns
        # Combine counts from run file and period files
        type_id_placeholders = ','.join(['%s'] * len(data_type_ids))
        
        # Count from run file
        run_type_ids = [data_type_map[col] for col in columns_from_run] if columns_from_run else []
        period_type_ids = [data_type_map[col] for col in columns_from_period] if columns_from_period else []
        
        count_queries = []
        count_params = []
        
        if run_type_ids:
            run_type_placeholders = ','.join(['%s'] * len(run_type_ids))
            count_queries.append(f"""
                SELECT DISTINCT le.time
                FROM log_entries le
                WHERE le.log_file_id = %s
                  AND le.data_type_id IN ({run_type_placeholders})
                  AND le.value IS NOT NULL
                  AND CAST(le.value AS TEXT) != ''
            """)
            count_params.append([run_id] + run_type_ids)
        
        if period_type_ids and run_start_time and run_end_time:
            period_type_placeholders = ','.join(['%s'] * len(period_type_ids))
            count_queries.append(f"""
                SELECT DISTINCT le.time
                FROM log_entries le
                WHERE le.vehicle_type = %s
                  AND le.data_type_id IN ({period_type_placeholders})
                  AND le.time >= %s
                  AND le.time <= %s
                  AND le.value IS NOT NULL
                  AND CAST(le.value AS TEXT) != ''
            """)
            count_params.append([period_vehicle_type] + period_type_ids + [run_start_time, run_end_time])
        
        # Combine distinct timestamps from all queries
        all_timestamps_set = set()
        for query, params in zip(count_queries, count_params):
            cursor.execute(query, params)
            for row in cursor.fetchall():
                all_timestamps_set.add(row['time'])
        
        total_count = len(all_timestamps_set)
        
        print(f"[DEBUG] Total rows with data: {total_count}")
        
        # Calculate pagination
        total_pages = (total_count + rows_per_page - 1) // rows_per_page if total_count > 0 else 0
        offset = (page - 1) * rows_per_page
        
        # Get distinct timestamps that have data in selected columns (with pagination)
        # Combine timestamps from run file and period files, then paginate
        all_timestamps_list = sorted(list(all_timestamps_set))
        
        # Apply pagination
        total_pages = (total_count + rows_per_page - 1) // rows_per_page if total_count > 0 else 0
        timestamps = all_timestamps_list[offset:offset + rows_per_page]
        
        print(f"[DEBUG] Retrieved {len(timestamps)} timestamps for page {page}")
        
        if not timestamps:
            return jsonify({
                'data': [],
                'total_rows': total_count,
                'page': page,
                'rows_per_page': rows_per_page,
                'total_pages': total_pages
            })
        
        # Get all data for these timestamps and selected columns
        # Fetch from run file
        data_by_time = {}
        
        if timestamps and columns_from_run:
            timestamp_placeholders = ','.join(['%s'] * len(timestamps))
            run_type_placeholders = ','.join(['%s'] * len(run_type_ids)) if run_type_ids else 'NULL'
            
            if run_type_ids:
                cursor.execute(f"""
                    SELECT 
                        le.time,
                        dtc.data_type,
                        le.value
                    FROM log_entries le
                    JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                    WHERE le.log_file_id = %s
                      AND le.data_type_id IN ({run_type_placeholders})
                      AND le.time IN ({timestamp_placeholders})
                    ORDER BY le.time ASC, dtc.data_type ASC
                """, [run_id] + run_type_ids + timestamps)
                
                for row in cursor.fetchall():
                    timestamp = row['time']
                    data_type = row['data_type']
                    value = row['value']
                    
                    if timestamp not in data_by_time:
                        data_by_time[timestamp] = {}
                    data_by_time[timestamp][data_type] = value
        
        # Fetch from period files (USV or AUV data)
        if timestamps and columns_from_period and run_start_time and run_end_time:
            timestamp_placeholders = ','.join(['%s'] * len(timestamps))
            period_type_placeholders = ','.join(['%s'] * len(period_type_ids)) if period_type_ids else 'NULL'
            
            if period_type_ids:
                cursor.execute(f"""
                    SELECT 
                        le.time,
                        dtc.data_type,
                        le.value
                    FROM log_entries le
                    JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                    WHERE le.vehicle_type = %s
                      AND le.data_type_id IN ({period_type_placeholders})
                      AND le.time >= %s
                      AND le.time <= %s
                      AND le.time IN ({timestamp_placeholders})
                    ORDER BY le.time ASC, dtc.data_type ASC
                """, [period_vehicle_type] + period_type_ids + [run_start_time, run_end_time] + timestamps)
                
                for row in cursor.fetchall():
                    timestamp = row['time']
                    data_type = row['data_type']
                    value = row['value']
                    
                    if timestamp not in data_by_time:
                        data_by_time[timestamp] = {}
                    data_by_time[timestamp][data_type] = value
        
        # Build result
        result_data = []
        for timestamp in timestamps:
            result_data.append({
                'time': timestamp.isoformat() if hasattr(timestamp, 'isoformat') else str(timestamp),
                'values': data_by_time.get(timestamp, {})
            })
        
        return jsonify({
            'data': result_data,
            'total_rows': total_count,
            'page': page,
            'rows_per_page': rows_per_page,
            'total_pages': total_pages
        })
        
    except Exception as e:
        import traceback
        error_traceback = traceback.format_exc()
        print(f"[ERROR] get_run_filtered_data failed: {str(e)}")
        print(f"[ERROR] Traceback:\n{error_traceback}")
        return jsonify({
            'error': str(e),
            'traceback': error_traceback if app.debug else None
        }), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route('/api/run/<int:run_id>/export_csv')
@monitor_performance
def export_run_csv(run_id):
    """
    API endpoint to export filtered data as CSV.
    Streams CSV data directly to the client for better performance.
    """
    from flask import Response
    import csv
    import io
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Get selected columns from query parameters
        selected_columns = request.args.getlist('columns')
        
        if not selected_columns:
            return jsonify({'error': 'No columns selected'}), 400
        
        print(f"[DEBUG] export_run_csv: run_id={run_id}, columns={selected_columns}")
        
        # Get data type IDs for selected columns
        placeholders = ','.join(['%s'] * len(selected_columns))
        cursor.execute(f"""
            SELECT id, data_type
            FROM data_type_catalog
            WHERE data_type IN ({placeholders})
        """, selected_columns)
        data_type_map = {row['data_type']: row['id'] for row in cursor.fetchall()}
        data_type_ids = list(data_type_map.values())
        
        if not data_type_ids:
            return jsonify({'error': 'No valid data types found'}), 400
        
        # Get all distinct timestamps that have data in selected columns
        type_id_placeholders = ','.join(['%s'] * len(data_type_ids))
        cursor.execute(f"""
            SELECT DISTINCT le.time
            FROM log_entries le
            WHERE le.log_file_id = %s
              AND le.data_type_id IN ({type_id_placeholders})
              AND le.value IS NOT NULL
              AND CAST(le.value AS TEXT) != ''
            ORDER BY le.time ASC
        """, [run_id] + data_type_ids)
        timestamps = [row['time'] for row in cursor.fetchall()]
        
        if not timestamps:
            return jsonify({'error': 'No data found'}), 404
        
        print(f"[DEBUG] Found {len(timestamps)} timestamps with data")
        
        # Create CSV in memory
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write header
        header = ['Timestamp'] + selected_columns
        writer.writerow(header)
        
        # Fetch data in batches to avoid memory issues
        batch_size = 1000
        for i in range(0, len(timestamps), batch_size):
            batch_timestamps = timestamps[i:i+batch_size]
            timestamp_placeholders = ','.join(['%s'] * len(batch_timestamps))
            
            # Get all data for this batch
            cursor.execute(f"""
                SELECT 
                    le.time,
                    dtc.data_type,
                    le.value
                FROM log_entries le
                JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                WHERE le.log_file_id = %s
                  AND le.data_type_id IN ({type_id_placeholders})
                  AND le.time IN ({timestamp_placeholders})
                ORDER BY le.time ASC, dtc.data_type ASC
            """, [run_id] + data_type_ids + batch_timestamps)
            
            # Build data structure for this batch
            data_by_time = {}
            for row in cursor.fetchall():
                timestamp = row['time']
                data_type = row['data_type']
                value = row['value']
                
                if timestamp not in data_by_time:
                    data_by_time[timestamp] = {}
                data_by_time[timestamp][data_type] = value
            
            # Write rows for this batch
            for timestamp in batch_timestamps:
                row_data = [timestamp.isoformat() if hasattr(timestamp, 'isoformat') else str(timestamp)]
                for col in selected_columns:
                    value = data_by_time.get(timestamp, {}).get(col, '')
                    # Convert to string, handle None
                    if value is None:
                        row_data.append('')
                    else:
                        row_data.append(str(value))
                writer.writerow(row_data)
        
        # Get CSV content
        csv_content = output.getvalue()
        output.close()
        
        # Create response with CSV
        response = Response(
            csv_content,
            mimetype='text/csv',
            headers={
                'Content-Disposition': f'attachment; filename=run_{run_id}_data_{datetime.now().strftime("%Y-%m-%d")}.csv'
            }
        )
        
        return response
        
    except Exception as e:
        import traceback
        error_traceback = traceback.format_exc()
        print(f"[ERROR] export_run_csv failed: {str(e)}")
        print(f"[ERROR] Traceback:\n{error_traceback}")
        return jsonify({
            'error': str(e),
            'traceback': error_traceback if app.debug else None
        }), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

if __name__ == '__main__':
    # Start background calculator for AUV positions
    try:
        from database.background_calculator import start_background_calculator
        start_background_calculator()
        print("Background AUV position calculator started")
    except Exception as e:
        print(f"Warning: Could not start background calculator: {e}")
    
    app.run(debug=True, host='0.0.0.0', port=5000)

