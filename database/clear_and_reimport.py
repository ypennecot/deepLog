#!/usr/bin/env python3
"""
Script to clear all imported data and reimport from logs directory.
"""

import psycopg2
import os
from pathlib import Path
from dotenv import load_dotenv
import sys

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

load_dotenv()

def get_db_connection():
    """Get database connection."""
    db_user = os.getenv('DB_USER')
    if not db_user or db_user == 'postgres':
        db_user = os.getenv('USER', 'user')
    
    return psycopg2.connect(
        host=os.getenv('DB_HOST', 'localhost'),
        port=os.getenv('DB_PORT', '5432'),
        database=os.getenv('DB_NAME', 'deeplog'),
        user=db_user,
        password=os.getenv('DB_PASSWORD', '')
    )

def clear_all_data():
    """Clear all imported log data from database."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        print("🗑️  Clearing all imported data...")
        
        # Delete in correct order (respecting foreign keys)
        print("  - Deleting log entries...")
        cursor.execute("DELETE FROM log_entries;")
        deleted_entries = cursor.rowcount
        
        print("  - Deleting vehicle settings...")
        cursor.execute("DELETE FROM vehicle_settings;")
        deleted_settings = cursor.rowcount
        
        print("  - Deleting settings files...")
        cursor.execute("DELETE FROM settings_files;")
        deleted_settings_files = cursor.rowcount
        
        print("  - Deleting log files...")
        cursor.execute("DELETE FROM log_files;")
        deleted_log_files = cursor.rowcount
        
        # Reset sequences
        print("  - Resetting sequences...")
        cursor.execute("ALTER SEQUENCE log_files_id_seq RESTART WITH 1;")
        cursor.execute("ALTER SEQUENCE settings_files_id_seq RESTART WITH 1;")
        cursor.execute("ALTER SEQUENCE vehicle_settings_id_seq RESTART WITH 1;")
        
        conn.commit()
        
        print(f"✅ Cleared:")
        print(f"   - {deleted_entries:,} log entries")
        print(f"   - {deleted_settings:,} vehicle settings")
        print(f"   - {deleted_settings_files:,} settings files")
        print(f"   - {deleted_log_files:,} log files")
        
        return True
        
    except Exception as e:
        conn.rollback()
        print(f"❌ Error clearing data: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        cursor.close()
        conn.close()

def verify_import():
    """Verify that a specific file was imported correctly."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        filename = '2025-11-23_08-33-01_AUV005_navigation_log.csv'
        print(f"\n🔍 Verifying import of {filename}...")
        
        cursor.execute("""
            SELECT 
                lf.filename,
                MIN(le.time) as first_time,
                MAX(le.time) as last_time,
                COUNT(*) as count,
                COUNT(DISTINCT dtc.data_type) as distinct_data_types
            FROM log_entries le
            JOIN log_files lf ON lf.id = le.log_file_id
            JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
            WHERE lf.filename = %s
            GROUP BY lf.filename
        """, (filename,))
        
        result = cursor.fetchone()
        if result:
            fn, first_time, last_time, count, distinct_types = result
            print(f"  ✅ File found:")
            print(f"     First timestamp: {first_time}")
            print(f"     Last timestamp:  {last_time}")
            print(f"     Total entries:   {count:,}")
            print(f"     Data types:      {distinct_types}")
            
            # Check against expected values
            expected_first = "2025-11-23 08:33:06.099519"
            expected_last = "2025-11-23 10:16:11.681345"
            
            first_str = str(first_time).split('+')[0]  # Remove timezone
            last_str = str(last_time).split('+')[0]
            
            if expected_first in first_str:
                print(f"  ✅ First timestamp matches expected: {expected_first}")
            else:
                print(f"  ⚠️  First timestamp mismatch:")
                print(f"     Expected: {expected_first}")
                print(f"     Got:      {first_str}")
            
            if expected_last in last_str:
                print(f"  ✅ Last timestamp matches expected: {expected_last}")
            else:
                print(f"  ⚠️  Last timestamp mismatch:")
                print(f"     Expected: {expected_last}")
                print(f"     Got:      {last_str}")
        else:
            print(f"  ❌ File not found in database")
        
    except Exception as e:
        print(f"❌ Error verifying: {e}")
        import traceback
        traceback.print_exc()
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--clear-only':
        clear_all_data()
    elif len(sys.argv) > 1 and sys.argv[1] == '--verify':
        verify_import()
    else:
        print("Usage:")
        print("  python3 clear_and_reimport.py --clear-only  # Clear all data")
        print("  python3 clear_and_reimport.py --verify      # Verify import")
        print("\nTo reimport, run: python3 database/import_logs.py logs/")


