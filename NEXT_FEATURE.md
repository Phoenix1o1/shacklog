# Feature Proposal: HomeAssistant Integration

> **Status (14 Oct 2026):** Phase 1 is implemented: `shacklog/services/ha.py`,
> `shacklog/web/views/api_ha.py`, *Settings › General › Home Assistant*, user guide
> section *Home Assistant*. Changes from this proposal: (1) a **separate read-only token**
> (stored as a hash), not the sync token, which can change the logbook; (2) **one**
> endpoint, `GET /api/ha/state`, feeding every sensor in one poll, with a generated
> `configuration.yaml` block; (3) sync status as the **hub** sees it (each PC's last
> sync; `oldest_hours` to alert on), since Home Assistant reaches the hub, not the PCs.
> Phase 2 (MQTT) is deferred: the hub learns of QSOs only when a PC syncs, so push adds
> no immediacy until a live source (CAT, WSJT-X UDP) exists; hook point then is the
> change stamping in `db.py` (covers form, imports and sync), not `qso_io.py`.

## 1. Objective
To provide a seamless bridge between ShackLog and HomeAssistant (HA), allowing users to monitor logbook status, sync activity, and radio operations via HA dashboards and automate smart-home actions based on radio activity.

## 2. Proposed Integration Architectures

### Option A: REST API (Pull-based) — *Recommended for Phase 1*
ShackLog exposes a set of read-only, authenticated JSON endpoints. HomeAssistant uses its native `rest` or `restful` integration to poll these endpoints at regular intervals.
- **Pros**: Simple implementation; no additional infrastructure (like MQTT) required; leverages existing authentication.
- **Cons**: Polling introduces latency; not truly real-time.

### Option B: MQTT (Push-based) — *Recommended for Phase 2*
ShackLog acts as an MQTT client, publishing state changes and events to a broker.
- **Pros**: Real-time updates; extremely lightweight for HA; follows standard IoT patterns.
- **Cons**: Requires an MQTT broker; slightly more complex event-handling logic in ShackLog.

## 3. Data Requirements & Schema

### 3.1 System & Sync Status
*Monitored via `GET /api/ha/status` or `shacklog/state/#` topics.*
- **Service Health**: Binary sensor indicating if the ShackLog service is reachable.
- **Sync Status**:
    - `last_sync_timestamp`: ISO8601 time of last successful sync.
    - `sync_state`: `idle`, `running`, or `error`.
    - `sync_error_message`: Description of the last error (if any).
- **Backup Status**:
    - `last_backup_timestamp`: Time of the last successful database/file backup.

### 3.2 Activity Monitoring
*Monitored via `GET /api/ha/activity` or `shacklog/events/#` topics.*
- **QSO Activity**:
    - `last_qso`: Object containing `{callsign, freq, mode, time_utc}`.
    - `qso_count_today`: Integer count of QSOs logged in the current UTC day.
- **Listening Log (SWL/SDR)**:
    - `latest_swl_entry`: Object containing `{freq, mode, heard_ident, time_utc}`.
- **Journaling**:
    - `latest_diary_entry`: A summary or title of the most recent diary entry.

### 3.3 Station Context
- **Current Mode**: If possible, a sensor indicating if the station is "Active" (based on recent activity) or "Idle".
- **Station Profile**: Name and location (for map-based dashboard elements).

## 4. Implementation Roadmap

### Phase 1: The "Observer" (REST API)
1.  **New View**: Create `shacklog/web/views/ha.py` containing the blueprint and endpoints.
2.  **Service Layer**: Add a helper in `shacklog/services/` to aggregate the required state data into clean JSON payloads.
3.  **Authentication**: Ensure endpoints require a valid Bearer token (existing sync token style).
4.  **Endpoints**:
    - `GET /api/ha/status`
    - `GET /api/ha/activity`

### Phase 2: The "Messenger" (MQTT)
1.  **New Service**: Implement `shacklog/services/mqtt.py` to handle broker connections and publishing.
2.  **Event Hooks**:
    - Hook into `qso_io.py` to publish `shacklog/events/qso_new` on successful log entry.
    - Hook into `sync_engine.py` and `backup.py` to publish status updates.
3.  **Standard Topics**:
    - `shacklog/state/service`
    - `shacklog/state/sync`
    - `shacklog/events/qso`

## 5. Potential HomeAssistant Use Cases
- **Dashboard Decoration**: A "Radio Station" card showing the last 5 QSOs and current sync status.
- **Smart Alerts**:
    - *Notification*: "New QSO logged: [Callsign]!" sent to mobile.
    - *Visual feedback*: Turn a smart light green when a sync completes successfully, or red if a sync fails.
- **Automation**:
    - Automatically turn on a "Radio Room" scene (lights, AC) when a new QSO is detected.
    - Log radio activity to a long-term statistics graph in HA.
