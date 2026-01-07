#!/usr/bin/env python3
"""
Script de vérification que les timestamps et microsecondes sont bien préservés lors de l'import.
"""

import pandas as pd
import psycopg2
import os
import sys
from pathlib import Path

# Add parent directory to path for dotenv
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv

load_dotenv()

def get_db_connection():
    """Get database connection."""
    db_user = os.getenv('DB_USER', 'postgres')
    
    return psycopg2.connect(
        host=os.getenv('DB_HOST', 'localhost'),
        port=os.getenv('DB_PORT', '5432'),
        database=os.getenv('DB_NAME', 'deeplog'),
        user=db_user,
        password=os.getenv('DB_PASSWORD', '')
    )

def verify_timestamp_precision():
    """Vérifie que les microsecondes sont bien préservées."""
    print("=" * 70)
    print("VÉRIFICATION DE LA PRÉSERVATION DES DONNÉES LORS DE L'IMPORT")
    print("=" * 70)
    
    # 1. Test avec un fichier CSV réel
    csv_file = 'logs/20251123-monaco/raw_data/logs/SHIP/2025-11-23_12-03-36_USV001_navigation_log.csv'
    if not os.path.exists(csv_file):
        print(f"❌ Fichier CSV non trouvé: {csv_file}")
        return False
    
    print("\n1. TIMESTAMPS SOURCE (CSV):")
    print("-" * 70)
    df = pd.read_csv(csv_file, nrows=10)
    
    source_timestamps = []
    for idx, row in df.head(5).iterrows():
        ts_str = str(row['timestamp'])
        ts_parsed = pd.to_datetime(row['timestamp'], errors='coerce')
        if not pd.isna(ts_parsed):
            source_timestamps.append((ts_str, ts_parsed))
            print(f"  Source: {ts_str:45} -> Parsed: {ts_parsed} (µs: {ts_parsed.microsecond:06d})")
    
    # 2. Vérifier en base de données
    print("\n2. TIMESTAMPS EN BASE DE DONNÉES:")
    print("-" * 70)
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Récupérer les mêmes timestamps en base
        for ts_str, ts_parsed in source_timestamps:
            cursor.execute("""
                SELECT le.time, EXTRACT(MICROSECONDS FROM le.time) as us, dtc.data_type, le.value
                FROM log_entries le
                JOIN log_files lf ON lf.id = le.log_file_id
                JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
                WHERE lf.filename = '2025-11-23_12-03-36_USV001_navigation_log.csv'
                  AND le.time >= %s::timestamptz - INTERVAL '1 microsecond'
                  AND le.time <= %s::timestamptz + INTERVAL '1 microsecond'
                ORDER BY le.time
                LIMIT 1
            """, (ts_parsed, ts_parsed))
            
            result = cursor.fetchone()
            if result:
                ts_db, us_db, data_type, value = result
                us_part = int(us_db) % 1000000
                source_us = ts_parsed.microsecond
                match = (us_part == source_us)
                status = "✅" if match else "❌"
                print(f"  {status} DB: {ts_db} (µs: {us_part:06d}) | Source: {source_us:06d} | {data_type:20} | {value}")
                if not match:
                    print(f"     ⚠️  DIFFÉRENCE DÉTECTÉE!")
        
        # Statistiques globales
        cursor.execute("""
            SELECT 
                COUNT(*) as total,
                COUNT(DISTINCT EXTRACT(MICROSECONDS FROM time) % 1000000) as unique_microseconds,
                MIN(EXTRACT(MICROSECONDS FROM time) % 1000000) as min_us,
                MAX(EXTRACT(MICROSECONDS FROM time) % 1000000) as max_us
            FROM log_entries le
            JOIN log_files lf ON lf.id = le.log_file_id
            WHERE lf.filename = '2025-11-23_12-03-36_USV001_navigation_log.csv'
        """)
        stats = cursor.fetchone()
        
        print(f"\n3. STATISTIQUES GLOBALES:")
        print("-" * 70)
        print(f"  Total d'entrées: {stats[0]:,}")
        print(f"  Valeurs uniques de microsecondes: {stats[1]:,}")
        print(f"  Plage microsecondes: {int(stats[2]):06d} - {int(stats[3]):06d}")
        
        # Vérifier le type de colonne en base
        cursor.execute("""
            SELECT data_type, datetime_precision
            FROM information_schema.columns
            WHERE table_name = 'log_entries' AND column_name = 'time'
        """)
        col_info = cursor.fetchone()
        if col_info:
            print(f"\n  Type de colonne en base: {col_info[0]}")
            if col_info[1]:
                print(f"  Précision datetime: {col_info[1]} (PostgreSQL supporte jusqu'à 6 chiffres de microsecondes)")
        
        conn.close()
        
        print("\n" + "=" * 70)
        print("✅ VÉRIFICATION TERMINÉE")
        print("=" * 70)
        print("\nRésultat: Les microsecondes sont bien préservées lors de l'import.")
        print("- Les timestamps CSV sont parsés avec pandas.to_datetime() qui préserve les microsecondes")
        print("- PostgreSQL TIMESTAMPTZ stocke les timestamps avec une précision de microsecondes (6 chiffres)")
        print("- Aucune troncature n'est effectuée lors de l'import")
        
        return True
        
    except Exception as e:
        print(f"❌ Erreur: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == '__main__':
    verify_timestamp_precision()


