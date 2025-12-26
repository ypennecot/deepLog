#!/usr/bin/env python3
"""
Flask web application for visualizing log data.
"""

from flask import Flask, render_template, request, jsonify
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key')

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
        altitude_types = ['Altitude OA filt', 'Altitude Kogger', 'Altitude Kogger raw', 'Altitude OA']
        
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
                'altitude': row['altitude']
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

