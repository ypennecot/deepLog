# Data Architecture: Marine Vehicle Log Analysis

**Feature**: 001-log-analysis  
**Created**: 2025-01-27  
**Database**: PostgreSQL 15+ with TimescaleDB extension

## Overview

This document defines the database schema optimized for storing and querying millions of navigation log entries from USV (Unmanned Surface Vehicle) and AUV (Autonomous Underwater Vehicle) systems.

## Data Characteristics

### Source Data Analysis

- **Navigation Log Format**: CSV with 3 columns: `timestamp`, `data`, `value`
- **USV Navigation Logs**: ~70 unique data types, ~700k-800k rows per file
- **AUV Navigation Logs**: ~68 unique data types, ~700k-800k rows per file
- **Settings Format**: CSV with 2 columns: `setting_name`, `setting_value`
- **USV Settings**: ~935 settings per file (configuration parameters)
- **AUV Settings**: ~1026 settings per file (configuration parameters)
- **Volume**: Several million rows per import session
- **Time Range**: Continuous time-series data with microsecond precision
- **Query Pattern**: Primarily time-range queries for visualization

### Data Type Categories

#### USV (Surface Vehicle) Data Types
- **GPS/Position**: `GPS_RAW_INT_lat`, `GPS_RAW_INT_lon`, `GPS_RAW_INT_alt`, `Lat`, `Lon`, `Easting`, `Northing`, `UTM_number`, `UTM_letter`
- **IMU Sensors**: `RAW_IMU_xacc`, `RAW_IMU_yacc`, `RAW_IMU_zacc`, `RAW_IMU_xgyro`, `RAW_IMU_ygyro`, `RAW_IMU_zgyro`, `RAW_IMU_xmag`, `RAW_IMU_ymag`, `RAW_IMU_zmag`
- **Orientation**: `Heading`, `Pitch`, `Roll`, `Yaw`, `GPS_RAW_INT_yaw`
- **Battery**: `BattVoltage`, `BattCurrent`, `BattLevel`
- **State**: `Armed`, `State`, `Mode`, `StateNb`
- **Controls**: `RC1` through `RC8`, `M1` through `M8`
- **GPS Metadata**: `GPS_RAW_INT_fix_type`, `GPS_RAW_INT_satellites_visible`, `GPS_RAW_INT_eph`, `GPS_RAW_INT_epv`, `gps_fix`

#### AUV (Underwater Vehicle) Data Types
- **Altitude/Depth**: `Altitude Kogger`, `Altitude Kogger raw`, `Altitude OA`, `Altitude OA filt`, `Depth`
- **Acoustic Positioning**: `BearingPing`, `DistancePing`, `ElevPing`, `Confidence`
- **IMU Sensors**: Same as USV (`RAW_IMU_*`, `SCALED_IMU2_*`)
- **Orientation**: `Heading`, `Pitch`, `Roll`
- **Battery**: `BattVoltage`, `BattCurrent`, `BattLevel`
- **State**: `Armed`, `Mode`, `Emergency`
- **Controls**: `RC1` through `RC8`, `M1` through `M8`
- **Sensors**: `Light13`, `Light14`

## Database Schema Design Philosophy

**Important**: The database schema is optimized for query performance and storage efficiency, and does NOT need to mirror the CSV file structure. The import process transforms CSV data into an optimized relational structure.

### Key Design Decisions

1. **Data Type Normalization**: Instead of storing full data type names (e.g., "RAW_IMU_xacc") in every row, we use a reference table with integer IDs. This reduces storage by ~15-20 bytes per row and improves index performance.

2. **Optimized for TimescaleDB**: The schema leverages TimescaleDB's strengths:
   - Time-based partitioning on the primary time column
   - Efficient compression of historical data
   - Fast time-range queries with proper indexing

3. **Traceability**: Source filename is preserved via foreign key relationships, not duplicated in every row.

4. **Query Optimization**: Schema designed for common query patterns:
   - Time-range queries by vehicle
   - Multi-data-type queries for visualization
   - Timeline availability queries

## Database Schema

### Core Tables

#### 1. `log_files` (Metadata Table)

Stores metadata about imported log files.

```sql
CREATE TABLE log_files (
    id SERIAL PRIMARY KEY,
    filename VARCHAR(500) NOT NULL,
    file_path TEXT NOT NULL,
    vehicle_type VARCHAR(10) NOT NULL CHECK (vehicle_type IN ('USV', 'AUV')),
    vehicle_id VARCHAR(50),  -- e.g., 'USV001', 'AUV005'
    file_size_bytes BIGINT,
    row_count INTEGER,
    import_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    import_completed_at TIMESTAMPTZ,
    import_status VARCHAR(20) DEFAULT 'pending' CHECK (import_status IN ('pending', 'importing', 'completed', 'failed')),
    error_message TEXT,
    first_timestamp TIMESTAMPTZ,  -- First log entry timestamp
    last_timestamp TIMESTAMPTZ,   -- Last log entry timestamp
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_log_files_vehicle_type ON log_files(vehicle_type);
CREATE INDEX idx_log_files_import_status ON log_files(import_status);
CREATE INDEX idx_log_files_timestamp_range ON log_files(first_timestamp, last_timestamp);
```

#### 2b. `log_entries` (TimescaleDB Hypertable) - **OPTIMIZED SCHEMA**

Main table for storing all log entries. **Optimized design**: Uses data_type_id (integer) instead of data_type (string) to reduce storage and improve performance.

```sql
-- Create regular table first
CREATE TABLE log_entries (
    time TIMESTAMPTZ NOT NULL,
    vehicle_type VARCHAR(10) NOT NULL CHECK (vehicle_type IN ('USV', 'AUV')),
    vehicle_id VARCHAR(50),
    data_type_id INTEGER NOT NULL REFERENCES data_type_catalog(id) ON DELETE RESTRICT,
    value DOUBLE PRECISION NOT NULL,
    log_file_id INTEGER NOT NULL REFERENCES log_files(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Convert to TimescaleDB hypertable
-- Partition by time with 1-day chunks (adjustable based on data volume)
SELECT create_hypertable('log_entries', 'time', 
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- Indexes for efficient querying
-- Primary query pattern: vehicle + time range
CREATE INDEX idx_log_entries_vehicle_type_time ON log_entries(vehicle_type, time DESC);

-- Query by data type (using ID is faster than string)
CREATE INDEX idx_log_entries_data_type_id_time ON log_entries(data_type_id, time DESC);

-- Composite index for common query pattern: vehicle + data type + time
CREATE INDEX idx_log_entries_vehicle_data_time ON log_entries(vehicle_type, data_type_id, time DESC);

-- For traceability: find entries from a specific file
CREATE INDEX idx_log_entries_log_file_id ON log_entries(log_file_id);

-- Optimized index for visualization queries (most common pattern)
CREATE INDEX idx_log_entries_query_optimized ON log_entries(vehicle_type, data_type_id, time DESC);
```

**Storage Optimization**:
- **Before**: `data_type VARCHAR(100)` = ~20 bytes average per row
- **After**: `data_type_id INTEGER` = 4 bytes per row
- **Savings**: ~16 bytes per row × millions of rows = significant storage reduction
- **Performance**: Integer comparisons and joins are faster than string comparisons

**Query Pattern**: When querying, join with `data_type_catalog` to get the original name:
```sql
SELECT le.time, dtc.data_type, le.value
FROM log_entries le
JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
WHERE le.vehicle_type = 'USV' AND le.time >= :start_time;
```

#### 3. `settings_files` (Settings Metadata Table)

Stores metadata about imported settings files (configuration at vehicle launch time).

```sql
CREATE TABLE settings_files (
    id SERIAL PRIMARY KEY,
    filename VARCHAR(500) NOT NULL,
    file_path TEXT NOT NULL,
    vehicle_type VARCHAR(10) NOT NULL CHECK (vehicle_type IN ('USV', 'AUV')),
    vehicle_id VARCHAR(50),  -- e.g., 'USV001', 'AUV005'
    file_size_bytes BIGINT,
    setting_count INTEGER,  -- Number of settings in the file
    import_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    import_completed_at TIMESTAMPTZ,
    import_status VARCHAR(20) DEFAULT 'pending' CHECK (import_status IN ('pending', 'importing', 'completed', 'failed')),
    error_message TEXT,
    -- Extracted metadata from settings file
    git_branch VARCHAR(100),
    git_hash VARCHAR(40),
    format_version VARCHAR(20),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(filename, vehicle_type, vehicle_id)  -- Prevent duplicate imports
);

CREATE INDEX idx_settings_files_vehicle_type ON settings_files(vehicle_type);
CREATE INDEX idx_settings_files_vehicle_id ON settings_files(vehicle_id);
CREATE INDEX idx_settings_files_import_status ON settings_files(import_status);
```

#### 4. `vehicle_settings` (Settings Key-Value Table)

Stores individual settings from settings.csv files. This is NOT a time-series table - it stores static configuration at launch time.

```sql
CREATE TABLE vehicle_settings (
    id SERIAL PRIMARY KEY,
    settings_file_id INTEGER NOT NULL REFERENCES settings_files(id) ON DELETE CASCADE,
    setting_name VARCHAR(200) NOT NULL,
    setting_value TEXT NOT NULL,  -- TEXT to handle JSON strings, numbers, and other formats
    value_type VARCHAR(20),  -- 'number', 'string', 'json', 'boolean'
    category VARCHAR(50),  -- 'compass', 'ins', 'gps', 'battery', 'navigation', 'usbl', etc.
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(settings_file_id, setting_name)  -- One setting per name per file
);

CREATE INDEX idx_vehicle_settings_file_id ON vehicle_settings(settings_file_id);
CREATE INDEX idx_vehicle_settings_name ON vehicle_settings(setting_name);
CREATE INDEX idx_vehicle_settings_category ON vehicle_settings(category);
CREATE INDEX idx_vehicle_settings_name_category ON vehicle_settings(setting_name, category);
```

#### 2a. `data_type_catalog` (Reference Table) - **MUST BE CREATED BEFORE log_entries**

Catalog of all known data types with metadata for validation and display. This table is used to normalize data type names, reducing storage in the main log_entries table.

```sql
CREATE TABLE data_type_catalog (
    id SERIAL PRIMARY KEY,
    data_type VARCHAR(100) NOT NULL UNIQUE,  -- Original name from CSV
    vehicle_type VARCHAR(10) NOT NULL CHECK (vehicle_type IN ('USV', 'AUV', 'BOTH')),
    category VARCHAR(50),  -- e.g., 'GPS', 'IMU', 'Battery', 'State', 'Controls'
    unit VARCHAR(20),      -- e.g., 'degrees', 'meters', 'volts', 'percentage'
    description TEXT,
    min_value DOUBLE PRECISION,
    max_value DOUBLE PRECISION,
    is_numeric BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_data_type_catalog_vehicle_type ON data_type_catalog(vehicle_type);
CREATE INDEX idx_data_type_catalog_category ON data_type_catalog(category);
CREATE UNIQUE INDEX idx_data_type_catalog_name_unique ON data_type_catalog(data_type);
```

**Note**: This table is populated during import. If a data type doesn't exist, it's automatically added. This allows the schema to adapt to new data types without schema changes.

### TimescaleDB Optimizations

#### Compression Policy

Enable compression for older data to reduce storage:

```sql
-- Compress chunks older than 7 days
SELECT add_compression_policy('log_entries', INTERVAL '7 days', if_not_exists => TRUE);
```

#### Retention Policy (Optional)

Automatically drop data older than a specified period (if needed):

```sql
-- Example: Retain data for 1 year (adjust as needed)
-- SELECT add_retention_policy('log_entries', INTERVAL '1 year', if_not_exists => TRUE);
```

#### Continuous Aggregates (Optional)

Pre-compute common aggregations for faster timeline queries:

```sql
-- Create continuous aggregate for timeline data availability
CREATE MATERIALIZED VIEW log_entries_by_hour
WITH (timescaledb.continuous) AS
SELECT 
    time_bucket('1 hour', time) AS hour,
    vehicle_type,
    vehicle_id,
    COUNT(*) AS entry_count,
    MIN(time) AS first_entry,
    MAX(time) AS last_entry
FROM log_entries
GROUP BY hour, vehicle_type, vehicle_id;

-- Refresh policy: update every hour
SELECT add_continuous_aggregate_policy('log_entries_by_hour',
    start_offset => INTERVAL '3 hours',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE);
```

## Query Patterns

### 1. Timeline Data Availability

Get time ranges where data exists for each vehicle:

```sql
SELECT 
    vehicle_type,
    vehicle_id,
    time_bucket('1 minute', time) AS time_bucket,
    COUNT(*) AS entry_count,
    MIN(time) AS start_time,
    MAX(time) AS end_time
FROM log_entries
WHERE time >= :start_time AND time <= :end_time
GROUP BY vehicle_type, vehicle_id, time_bucket
ORDER BY time_bucket, vehicle_type;
```

### 2. Data for Selected Time Period

Get all data for a specific time range and vehicle (with optimized schema):

```sql
SELECT 
    le.time,
    le.vehicle_type,
    le.vehicle_id,
    dtc.data_type,  -- Join to get original name
    le.value
FROM log_entries le
JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
WHERE le.vehicle_type = :vehicle_type
  AND le.time >= :start_time
  AND le.time <= :end_time
ORDER BY le.time, dtc.data_type;
```

### 3. Specific Data Types for Visualization

Get specific metrics for graph visualization (with optimized schema):

```sql
SELECT 
    le.time,
    dtc.data_type,
    le.value
FROM log_entries le
JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
WHERE le.vehicle_type = :vehicle_type
  AND dtc.data_type IN (:data_types)  -- e.g., ['Lat', 'Lon', 'Heading']
  AND le.time >= :start_time
  AND le.time <= :end_time
ORDER BY le.time, dtc.data_type;
```

**Alternative**: If you know the data_type_ids, query directly (faster):
```sql
SELECT 
    time,
    value
FROM log_entries
WHERE vehicle_type = :vehicle_type
  AND data_type_id IN (:data_type_ids)  -- Pre-resolved IDs
  AND time >= :start_time
  AND time <= :end_time
ORDER BY time;
```

### 4. Vehicle Synchronization

Get synchronized data from both vehicles for comparison (with optimized schema):

```sql
WITH usv_data AS (
    SELECT le.time, dtc.data_type, le.value
    FROM log_entries le
    JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
    WHERE le.vehicle_type = 'USV'
      AND dtc.data_type IN ('Lat', 'Lon', 'Heading')
      AND le.time >= :start_time AND le.time <= :end_time
),
auv_data AS (
    SELECT le.time, dtc.data_type, le.value
    FROM log_entries le
    JOIN data_type_catalog dtc ON dtc.id = le.data_type_id
    WHERE le.vehicle_type = 'AUV'
      AND dtc.data_type IN ('BearingPing', 'DistancePing', 'Altitude Kogger')
      AND le.time >= :start_time AND le.time <= :end_time
)
SELECT * FROM usv_data
UNION ALL
SELECT * FROM auv_data
ORDER BY time;
```

## Import Behavior by Vehicle Type

### AUV (Autonomous Underwater Vehicle) Import

When importing a directory containing AUV log files:
- **MUST import**: Navigation log files (containing "navigation" in filename)
- **MUST import**: Settings files (containing "settings" in filename)
- Both file types are required and must be associated with the same AUV vehicle
- Source filenames are preserved in `log_files` and `settings_files` tables

### USV (Unmanned Surface Vehicle) Import

When importing a directory containing USV log files:
- **MUST import**: Navigation log files (containing "navigation" in filename)
- **MUST ignore**: Settings files (even if present in directory)
- Only navigation files are processed for USV vehicles
- Source filename is preserved in `log_files` table

### Filename Traceability

All imported data must preserve the source filename:
- **Navigation logs**: Source filename stored in `log_files.filename`, referenced via `log_entries.log_file_id`
- **Settings**: Source filename stored in `settings_files.filename`, referenced via `vehicle_settings.settings_file_id`

Query to get source filename for a log entry:
```sql
SELECT le.*, lf.filename AS source_filename
FROM log_entries le
JOIN log_files lf ON lf.id = le.log_file_id
WHERE le.id = :entry_id;
```

## Data Import Strategy

### Import Process: CSV to Optimized Schema

The import process transforms CSV data into the optimized database schema:

1. **Parse CSV** (timestamp, data, value)
2. **Resolve data_type** → Look up or create entry in `data_type_catalog`, get `data_type_id`
3. **Extract vehicle info** from filename or content
4. **Insert into optimized schema** with `data_type_id` instead of string

### Batch Insert Optimization

For importing millions of rows:

1. **Use COPY command** for bulk inserts (fastest method)
2. **Batch size**: 10,000-50,000 rows per transaction
3. **Disable indexes during import**, rebuild after
4. **Resolve data_type_id once** per unique data_type, then reuse

Example import process:

```sql
-- 1. Create temporary table matching CSV structure
CREATE TEMP TABLE log_entries_temp (
    time TIMESTAMPTZ,
    data_type_name VARCHAR(100),  -- Original from CSV
    value DOUBLE PRECISION
);

-- 2. Load data via COPY
COPY log_entries_temp FROM '/path/to/file.csv' WITH (FORMAT csv, HEADER true);

-- 3. Resolve data_type_ids (create if doesn't exist)
INSERT INTO data_type_catalog (data_type, vehicle_type, category)
SELECT DISTINCT data_type_name, :vehicle_type, :category
FROM log_entries_temp
ON CONFLICT (data_type) DO NOTHING;

-- 4. Insert in batches with resolved IDs
INSERT INTO log_entries (time, vehicle_type, vehicle_id, data_type_id, value, log_file_id)
SELECT 
    let.time,
    :vehicle_type,
    :vehicle_id,
    dtc.id AS data_type_id,  -- Resolved ID
    let.value,
    :log_file_id
FROM log_entries_temp let
JOIN data_type_catalog dtc ON dtc.data_type = let.data_type_name
ON CONFLICT DO NOTHING;  -- Handle duplicates if needed
```

**Performance Note**: The JOIN to resolve data_type_id is fast because `data_type_catalog` is small (~70-100 rows) and indexed.

## Data Validation

### Constraints and Checks

- **Timestamp validation**: Ensure timestamps are within reasonable bounds
- **Value validation**: Check against `data_type_catalog` min/max if defined
- **Vehicle type consistency**: Ensure vehicle_type matches log_file metadata
- **Data type validation**: Verify data_type exists in catalog

### Data Quality Metrics

Track data quality during import:

```sql
-- Add to log_files table
ALTER TABLE log_files ADD COLUMN validation_errors INTEGER DEFAULT 0;
ALTER TABLE log_files ADD COLUMN duplicate_count INTEGER DEFAULT 0;
ALTER TABLE log_files ADD COLUMN invalid_timestamp_count INTEGER DEFAULT 0;
ALTER TABLE log_files ADD COLUMN out_of_range_value_count INTEGER DEFAULT 0;
```

## Performance Considerations

### Index Strategy

- **Primary index**: Time-based (handled by TimescaleDB hypertable)
- **Secondary indexes**: Vehicle type + time, data type + time
- **Composite indexes**: For common query patterns (vehicle + data type + time)

### Partitioning

- **Time-based partitioning**: Automatic via TimescaleDB hypertable (1-day chunks)
- **Considerations**: Adjust chunk size based on query patterns and data volume

### Query Optimization

- Use `time_bucket()` for aggregations
- Leverage continuous aggregates for common queries
- Use compression for historical data
- Consider materialized views for complex joins

## Storage Estimates

### Per Log Entry (Optimized Schema)
- `time`: 8 bytes (TIMESTAMPTZ)
- `vehicle_type`: ~4 bytes (VARCHAR(10))
- `vehicle_id`: ~10 bytes average (VARCHAR(50))
- `data_type_id`: 4 bytes (INTEGER) - **Optimized from VARCHAR(100)**
- `value`: 8 bytes (DOUBLE PRECISION)
- `log_file_id`: 4 bytes (INTEGER)
- `created_at`: 8 bytes (TIMESTAMPTZ)
- **Total**: ~46 bytes per row + overhead (vs ~62 bytes with string data_type)
- **Storage Savings**: ~26% reduction per row

### For 10 Million Rows
- **Raw data**: ~620 MB
- **With indexes**: ~1.2-1.5 GB (estimated)
- **Compressed (after 7 days)**: ~200-300 MB (estimated 60-70% compression)

## Migration and Maintenance

### Initial Setup

```sql
-- 1. Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- 2. Create tables in order (respecting foreign key dependencies)
--    a. log_files (no dependencies)
--    b. data_type_catalog (no dependencies, but needed by log_entries)
--    c. log_entries (depends on log_files and data_type_catalog)
--    d. settings_files (no dependencies)
--    e. vehicle_settings (depends on settings_files)

-- 3. Convert log_entries to hypertable
SELECT create_hypertable('log_entries', 'time', 
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- 4. Create indexes
-- (as defined above for each table)

-- 5. Set up compression policy
SELECT add_compression_policy('log_entries', INTERVAL '7 days', if_not_exists => TRUE);
```

### Maintenance Tasks

- **VACUUM**: Automatic via PostgreSQL autovacuum
- **ANALYZE**: Run periodically to update statistics
- **Chunk management**: TimescaleDB handles automatically
- **Compression**: Automatic via policy

## Settings File Import Strategy

### Settings File Structure

Settings files contain:
- **Metadata**: `vehicleId`, `vehicleType`, `git_branch`, `git_hash`, `FORMAT_VERSION`
- **JSON Objects**: `vehicle`, `camera`, `api`, `navigation`, `usbl`, `seaker`, `follow`, `Emergency` (stored as JSON strings)
- **Configuration Parameters**: Hundreds of settings like `COMPASS_*`, `INS_*`, `GPS_*`, `BATT_*`, etc.

### Import Process

1. **Parse settings file** and extract metadata (git_branch, git_hash, etc.)
2. **Create settings_file record** with metadata
3. **Insert all settings** as key-value pairs in `vehicle_settings` table
4. **Parse JSON objects** and store as JSON strings (can be queried with PostgreSQL JSON functions)
5. **Categorize settings** by prefix (COMPASS_ → 'compass', INS_ → 'ins', etc.)

### Querying Settings

Get all settings for a specific vehicle configuration:

```sql
SELECT 
    setting_name,
    setting_value,
    value_type,
    category
FROM vehicle_settings
WHERE settings_file_id = :settings_file_id
ORDER BY category, setting_name;
```

Get specific setting value:

```sql
SELECT setting_value
FROM vehicle_settings
WHERE settings_file_id = :settings_file_id
  AND setting_name = 'COMPASS_OFS_X';
```

Get JSON configuration objects:

```sql
SELECT 
    setting_name,
    setting_value::jsonb AS config_json
FROM vehicle_settings
WHERE settings_file_id = :settings_file_id
  AND setting_name IN ('navigation', 'usbl', 'follow', 'Emergency')
  AND value_type = 'json';
```

Link settings to navigation logs:

```sql
-- Find settings file for a specific log file based on vehicle and timestamp
SELECT sf.*, vs.setting_name, vs.setting_value
FROM settings_files sf
JOIN vehicle_settings vs ON vs.settings_file_id = sf.id
JOIN log_files lf ON lf.vehicle_type = sf.vehicle_type 
                 AND lf.vehicle_id = sf.vehicle_id
WHERE lf.id = :log_file_id
  AND sf.import_completed_at <= lf.first_timestamp  -- Settings must exist before log starts
ORDER BY sf.import_completed_at DESC
LIMIT 1;
```

## Notes

### Schema Optimization

- **Database schema is optimized and does NOT mirror CSV structure**
- Data types are normalized: stored as integer IDs (`data_type_id`) instead of strings
- This reduces storage by ~26% and improves query performance
- Source CSV structure (timestamp, data, value) is transformed during import

### Data Storage

- All timestamps stored in UTC (TIMESTAMPTZ)
- Vehicle IDs extracted from filenames (e.g., `USV001`, `AUV005`)
- Data type names preserved in `data_type_catalog` table (original CSV names, case-sensitive)
- Log entry values stored as DOUBLE PRECISION (handles both integers and floats)
- Settings values stored as TEXT to handle JSON, numbers, and strings
- Settings are NOT time-series data - they represent static configuration at launch

### Import Process

- CSV files are parsed and transformed into optimized schema during import
- Data type names from CSV are resolved to IDs via `data_type_catalog`
- If a data type doesn't exist in catalog, it's automatically added
- Source filename is preserved via foreign key relationships (not duplicated in every row)

