#!/usr/bin/env python3
"""
Flask web application for visualizing log data.
"""

from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from dotenv import load_dotenv
from pathlib import Path
import tempfile
import shutil
import sys
import threading
import uuid
import json
import time

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key')

# Store import status in memory (in production, use Redis or similar)
import_status = {}
import_status_lock = threading.Lock()

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

@app.route('/')
def index():
    """Main page showing data tables."""
    return render_template('index.html')

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
        
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/depth')
def get_run_depth_by_id(run_id):
    """API endpoint to get depth data for a specific AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
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
        
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/altitude')
def get_run_altitude(run_id):
    """API endpoint to get altitude data for a specific AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Get all altitude data types for this run
        altitude_types = ['Altitude OA filt', 'Altitude Kogger', 'Altitude Kogger raw', 'Altitude OA', 'Target_altitude']
        
        # Build query to get all altitude data
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
        
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/motors')
def get_run_motors(run_id):
    """API endpoint to get motor data (M1-M8) for a specific AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Get all motor data types for this run
        motor_types = ['M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'M7', 'M8']
        
        # Build query to get all motor data
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
        
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/state')
def get_run_state(run_id):
    """API endpoint to get StateNb data for a specific AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Get StateNb data for this run
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
        
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/usv_position')
def get_run_usv_position(run_id):
    """API endpoint to get USV GPS position data for the time period of an AUV run."""
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
        
        # Debug: print time range
        print(f"USV Position Query: Looking for data between {start_time} and {end_time}")
        
        # Get USV GPS position data for this time period
        # GPS_RAW_INT_lat and GPS_RAW_INT_lon are in degrees * 1e7 format
        query = """
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
        cursor.execute(query, (start_time, end_time))
        lat_data = cursor.fetchall()
        print(f"Found {len(lat_data)} latitude points")
        
        # Get longitude data
        query = """
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
        cursor.execute(query, (start_time, end_time))
        lon_data = cursor.fetchall()
        print(f"Found {len(lon_data)} longitude points")
        
        # Combine lat and lon by matching closest timestamps
        # Since timestamps may not match exactly, we'll use a time window approach
        # For each lat point, find the closest lon point within a small time window (e.g., 1 second)
        result = []
        
        # Convert to lists with timestamps as epoch for easier comparison
        lat_list = [(row['time'], row['lat_raw']) for row in lat_data if row['time']]
        lon_list = [(row['time'], row['lon_raw']) for row in lon_data if row['time']]
        
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
                lat_degrees = lat_raw / 1e7
                lon_degrees = lon_raw / 1e7
                result.append({
                    'time': lat_time.isoformat(),
                    'lat': lat_degrees,
                    'lon': lon_degrees
                })
        
        print(f"Returning {len(result)} position points (matched within 1 second)")
        return jsonify(result)
        
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
            # Convert timestamps to ISO format
            if run_dict['first_timestamp']:
                run_dict['first_timestamp'] = run_dict['first_timestamp'].isoformat()
            if run_dict['last_timestamp']:
                run_dict['last_timestamp'] = run_dict['last_timestamp'].isoformat()
            runs_list.append(run_dict)
        
        return jsonify(runs_list)
        
    finally:
        cursor.close()
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
        
    finally:
        cursor.close()
        conn.close()

@app.route('/api/run/<int:run_id>/orientation')
def get_run_orientation(run_id):
    """API endpoint to get Roll, Pitch, Yaw, and Heading data for a specific AUV run."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Get all orientation data types for this run
        orientation_types = ['Roll', 'Pitch', 'Yaw', 'Heading']
        
        # Build query to get all orientation data
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
        
        return jsonify(result)

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
        from import_logs import import_navigation_file, import_settings_file, extract_vehicle_info
        
        conn = get_db_connection()
        files_processed = 0
        files_imported = 0
        files_skipped = 0
        errors = []
        
        # Use rglob to find all navigation and settings files recursively
        temp_path = Path(temp_dir)
        nav_files = list(temp_path.rglob('*navigation*.csv'))
        settings_files = list(temp_path.rglob('*settings*.csv'))
        all_files = nav_files + settings_files
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
                
                result = import_navigation_file(nav_file, conn, file_progress)
                
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
                error_msg = f"Error importing {nav_file.name}: {str(e)}"
                print(error_msg)
                errors.append(error_msg)
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
                error_msg = f"Error importing {settings_file.name}: {str(e)}"
                print(error_msg)
                errors.append(error_msg)
            finally:
                files_processed += 1
                with import_status_lock:
                    import_status[import_id]['files_processed'] = files_processed
                    import_status[import_id]['files_imported'] = files_imported
                    import_status[import_id]['files_skipped'] = files_skipped
        
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
                'errors': errors[:5]
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
        
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)

