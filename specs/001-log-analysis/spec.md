# Feature Specification: Marine Vehicle Log Analysis

**Feature Branch**: `001-log-analysis`  
**Created**: 2025-01-27  
**Status**: Draft  
**Input**: User description: "l'objectif de ce projet est de permettre l'analyse de données de logs. Deux types d'engins naviguent ensemble: un unmaned surface Vehicle: USV et un AUV: Autonomous Underwater Vehicle. L'USV se positionne en surface grace au GPS. L'AUV ne connait que sa position relative à l'USV via une antenne acoustique qui lui fournit une distance à l'USV et un angle de bearing. L'application devra pouvoir charger dans une base de donnée les infroamtions fournies dans des fichiers de logs. une fois les logs chargés l'application afficher une time line ou l'on peut voir a quel moment les logs de chaque engins sont accessibles. A partir de cette time line, on peut sélectionner une periode de temps et l'application affiche un ensemble de graphiques qui permettent de comprendre le comportement de l'AUV et de l'USV sur la periode. Des exemples de logs sont dans le dossier log. Les logs à charger dans un premier temps ont le mot "navigation" dans leur nom et sont sous la forme d'un CSV. ces CSV ont 3 colonnes: un temps, le type de la donnée, et la valeur."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Load Navigation Logs and Settings into Database (Priority: P1)

A user needs to import log files from USV and AUV vehicles into the application database for analysis. The import process differs by vehicle type: for AUV vehicles, both navigation log files and settings files must be imported; for USV vehicles, only navigation log files are imported. The system must preserve the source filename for all imported data to enable traceability.

**Why this priority**: This is the foundational capability - without loaded data, no analysis can be performed. This must work before any visualization features.

**Independent Test**: Can be fully tested by loading a single navigation log file and verifying the data is stored correctly in the database with the source filename preserved. The test delivers value by enabling data persistence for future analysis.

**Acceptance Scenarios**:

1. **Given** the application is running with an empty database, **When** a user selects a CSV file with "navigation" in its filename containing valid timestamp, data type, and value columns, **Then** the application loads all rows into the database, preserves the source filename, and confirms successful import
2. **Given** a user imports a directory containing AUV log files, **When** the directory contains both navigation and settings files, **Then** the application imports both file types and associates them with the same AUV vehicle
3. **Given** a user imports a directory containing USV log files, **When** the directory contains navigation and settings files, **Then** the application imports only the navigation files and ignores settings files
4. **Given** the application has existing log data, **When** a user imports additional navigation log files, **Then** the new data is added to the database without overwriting existing data, and each log entry retains its source filename reference
5. **Given** a user attempts to import a CSV file with "navigation" in its filename, **When** the file has invalid format (missing columns, incorrect data types, or corrupted data), **Then** the application reports specific errors and does not import invalid data
6. **Given** a user selects multiple navigation log files, **When** the files are from different vehicles (USV and AUV), **Then** the application correctly identifies and stores which vehicle each log belongs to, and preserves the source filename for traceability
7. **Given** log entries are stored in the database, **When** a user queries log data, **Then** the system can identify the source filename from which each entry was imported

---

### User Story 2 - View Timeline of Available Logs (Priority: P2)

A user needs to see when log data is available for each vehicle (USV and AUV) to understand the temporal coverage of the loaded data. The application displays a timeline visualization showing time periods where logs exist for each vehicle.

**Why this priority**: Users need to understand what data is available before selecting time periods for analysis. This enables informed selection of analysis windows.

**Independent Test**: Can be fully tested by loading logs from both vehicles and verifying the timeline correctly displays the time ranges where each vehicle has data. The test delivers value by providing visibility into data availability.

**Acceptance Scenarios**:

1. **Given** navigation logs from both USV and AUV vehicles are loaded, **When** a user views the timeline, **Then** the timeline displays distinct visual indicators for USV and AUV showing their respective time ranges of available data
2. **Given** logs from multiple time periods with gaps, **When** a user views the timeline, **Then** the timeline shows all time periods with data and clearly indicates gaps where no data exists
3. **Given** overlapping time periods from the same vehicle, **When** a user views the timeline, **Then** the timeline correctly represents the union of all available time periods for that vehicle
4. **Given** the timeline is displayed, **When** a user hovers or interacts with timeline segments, **Then** the application shows details about the time range and vehicle type

---

### User Story 3 - Analyze Vehicle Behavior for Selected Time Period (Priority: P3)

A user needs to analyze the behavior of USV and AUV vehicles during a specific time period by viewing graphical representations of their data. The user selects a time range from the timeline, and the application displays relevant graphs showing vehicle behavior metrics.

**Why this priority**: This delivers the core analytical value - understanding vehicle behavior patterns. However, it depends on data loading and timeline selection being functional first.

**Independent Test**: Can be fully tested by selecting a time period with known data and verifying graphs display relevant metrics for both vehicles. The test delivers value by enabling data-driven insights into vehicle operations.

**Acceptance Scenarios**:

1. **Given** logs are loaded and timeline is displayed, **When** a user selects a time period where both USV and AUV have data, **Then** the application displays graphs showing relevant metrics (position, movement, sensor data) for both vehicles during that period
2. **Given** a time period is selected, **When** the period contains data from only one vehicle, **Then** the application displays graphs for the available vehicle and indicates which vehicle data is missing
3. **Given** graphs are displayed for a selected period, **When** a user views the graphs, **Then** the graphs show time-synchronized data allowing comparison of USV and AUV behavior
4. **Given** graphs are displayed, **When** the selected time period contains different data types (GPS coordinates for USV, relative position for AUV), **Then** the graphs appropriately represent each vehicle's available data types

---

### Edge Cases

- What happens when a user attempts to load a navigation log file that is currently being written to by another process?
- How does the system handle navigation log files with timestamps that are far in the future or past (timezone issues, clock synchronization problems)?
- What happens when a navigation log file contains duplicate timestamps for the same data type?
- How does the system handle navigation log files with extremely large file sizes (millions of rows)?
- What happens when a user selects a time period that has no data for either vehicle?
- How does the system handle navigation log files with inconsistent timestamp formats or missing timestamps?
- What happens when a navigation log file contains data types that are not recognized or expected?
- How does the system handle navigation log files where the vehicle type cannot be determined from the filename or content?
- What happens when a user imports a directory containing both USV and AUV files - how are they processed differently?
- How does the system handle AUV directories that are missing settings files?
- How does the system handle USV directories that contain settings files (should they be ignored)?
- How can users trace which source file a specific log entry came from?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST allow users to select and import CSV files containing "navigation" in their filename
- **FR-002**: System MUST parse navigation CSV files with three columns: timestamp, data type, and value
- **FR-003**: System MUST allow users to select and import CSV files containing "settings" in their filename
- **FR-004**: System MUST parse settings CSV files with two columns: setting name and setting value
- **FR-005**: System MUST store imported navigation log data in a database with proper indexing for time-based queries
- **FR-006**: System MUST store imported settings data in a database (non-time-series table)
- **FR-007**: System MUST identify and associate log data with the correct vehicle (USV or AUV) based on file source or content
- **FR-008**: System MUST identify and associate settings data with the correct vehicle (USV or AUV) based on file source or content
- **FR-009**: System MUST preserve the source filename for all imported navigation log entries in the database
- **FR-010**: System MUST preserve the source filename for all imported settings entries in the database
- **FR-011**: System MUST import both navigation and settings files when processing AUV vehicle directories
- **FR-012**: System MUST import only navigation files (and ignore settings files) when processing USV vehicle directories
- **FR-013**: System MUST display a timeline visualization showing time periods where log data is available for each vehicle
- **FR-014**: System MUST allow users to select a time period from the timeline
- **FR-015**: System MUST display graphs showing vehicle behavior metrics for the selected time period
- **FR-016**: System MUST handle USV data types (GPS coordinates, position data) appropriately in visualizations
- **FR-017**: System MUST handle AUV data types (relative position to USV via distance and bearing) appropriately in visualizations
- **FR-018**: System MUST validate CSV file format before importing and report errors for invalid files
- **FR-019**: System MUST preserve all imported log and settings data without data loss during import process
- **FR-020**: System MUST support importing multiple navigation log files in a single session
- **FR-021**: System MUST support importing multiple settings files in a single session (for AUV vehicles)
- **FR-022**: System MUST display graphs that enable comparison of USV and AUV behavior during the selected time period

### Key Entities *(include if feature involves data)*

- **Log File**: Represents a CSV file containing navigation log data. Key attributes: filename (preserved for traceability), source path, vehicle type (USV/AUV), import timestamp, file size
- **Log Entry**: Represents a single row of log data. Key attributes: timestamp, data type, value, vehicle association, source filename reference (via log_file_id)
- **Settings File**: Represents a CSV file containing vehicle configuration settings at launch time. Key attributes: filename, source path, vehicle type (USV/AUV), vehicle ID, git branch/hash, import timestamp
- **Vehicle Setting**: Represents a single configuration parameter from a settings file. Key attributes: setting name, setting value, value type (number/string/json), category, associated settings file
- **Time Period**: Represents a selected range of time for analysis. Key attributes: start time, end time, associated vehicle data availability
- **Vehicle**: Represents either a USV or AUV. Key attributes: vehicle type, available data types, time ranges of available logs, configuration settings
- **Graph/Visualization**: Represents a graphical display of vehicle data. Key attributes: data type displayed, time period, vehicle(s) shown, visualization type

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Users can successfully import navigation log files containing up to 1 million rows within 5 minutes
- **SC-002**: Timeline visualization displays accurate time ranges for available log data with resolution down to 1 second
- **SC-003**: Users can select a time period and view corresponding graphs within 3 seconds of selection
- **SC-004**: System correctly identifies and displays data from both USV and AUV vehicles in 100% of cases when vehicle type can be determined
- **SC-005**: Graphs display time-synchronized data allowing users to correlate USV and AUV behavior during selected periods
- **SC-006**: Users can successfully analyze vehicle behavior for time periods ranging from 1 minute to 24 hours
- **SC-007**: System handles import errors gracefully, reporting specific issues without crashing for 95% of invalid file scenarios

## Technical Assumptions

### Database Technology Selection

**Decision**: PostgreSQL with TimescaleDB extension

**Rationale**: 
- The system must handle several million log entries efficiently
- Time-series queries (selecting data by time period) are the primary access pattern
- Aligns with constitution principles: mature, stable, well-documented technology with strong community support

**Benefits**:
- PostgreSQL provides ACID compliance, reliability, and standard SQL interface
- TimescaleDB extension optimizes time-series data with automatic partitioning, compression, and time-based indexing
- Excellent performance for temporal queries required by timeline and graph features
- Proven scalability for millions of rows in production environments
- Comprehensive documentation and active community support

**Impact on Implementation**:
- Database schema design should leverage TimescaleDB hypertables for log entry storage
- Time-based indexes will be automatically optimized by TimescaleDB
- Query patterns should utilize TimescaleDB time-series functions for efficient data retrieval
