# People Counter Dashboard

React 18 + TypeScript + Vite + TanStack Query + Recharts.

## Run (with the real backend)
    # 1) backend (from src/backend)
    uvicorn app:app --reload --port 8000
    # 2) dashboard (from src/dashboard)
    npm install
    cp .env.example .env.local
    npm run dev

No backend yet? Set `VITE_USE_MOCK=true` in `.env.local`.

## Contract notes
Matches `src/backend/app.py`: `GET /events` returns `{id, camera_id, track_id, event_type, timestamp, meta}`; `WS /ws/live` sends the current occupancy right after connect, then on every `POST /events`.

## Live dashboard contract (tables, room count, WebSocket)
The parsers in `src/lib/normalize.ts` accept the aliases shown in brackets, and drop malformed rows instead of crashing.

| Endpoint | Shape |
|---|---|
| `GET /tables` | `[{ "id": "T1", "name": "Table 1", "polygon": [[x,y], ...] }]`  x, y are 0..1 of the camera frame, at least 3 points  [`table_id`, `points`, `[{x,y}]`] |
| `GET /tables/status` | `[{ "table_id": "T1", "status": "occupied"/"free", "people_count": 3, "occupied_since": "ISO time" }]`  [`id`, `people`, `since`, or `duration_seconds` / `duration_minutes`] |
| `GET /room-count/history` | `[{ "timestamp": "ISO time", "count": 7 }]`  [`ts`, `room_count`, `[time, count]`] |
| `WS /ws/live` | `{type:"occupancy", current, entered, exited}`  entrance camera (computed)<br>`{type:"room_count", count, timestamp?}`  hall camera (what is really visible)<br>`{type:"table", table_id, status, people_count, occupied_since}` or `{type:"table", tables:[...]}` |

- `People Inside` = hall camera (`room_count`). `Entry Counter` = entrance camera (`occupancy.current`). The gap between them is shown under the cards.
- The table map is an SVG with the same aspect ratio as the camera frame (`config.frame`, 768x432 = 16:9), so polygons never drift when the window is resized. If your camera is not 16:9, change `config.frame`.
- `table` messages patch the cached `/tables/status` data directly; `room_count` messages are appended to the chart. After a reconnect everything is refetched.
- WebSocket reconnect: 1 s, 2 s, 4 s ... up to 15 s, with a countdown and a "Retry now" button.

## What the dashboard shows (all derived from the real backend data)
- Live occupancy + capacity gauge (capacity is saved in the browser), entered/exited, period peak, average stay, last update
- Occupancy curve and hourly traffic for the chosen range (today / 24h / 7 days / all) and camera
- Per-camera breakdown, and a map of where people cross the virtual line (`meta.anchor`)
- Counting-quality hints: exits > entries, tracks that cross the line 4+ times
- Events log: filter by type, search by track_id, pause refresh, export CSV

## Structure
- `src/api/client.ts`: the only module that talks to the backend (HTTP + WebSocket with backoff)
- `src/api/mock.ts`: in-memory simulator of the same contract
- `src/lib/analytics.ts`: pure functions that turn the events list into charts/insights
- `src/lib/tables.ts`, `src/lib/normalize.ts`: table geometry, durations, payload parsing (unit-tested in `tables.test.ts`)
- `src/hooks/useLive.ts`: the single WebSocket; `useTables.ts` / `useRoomHistory.ts`: REST data
- `src/components/`: `OccupancyCards`, `TableMap` (SVG) + `TablesPanel` (map and list), `RoomCountChart`, `ConnectionBadge`, `States` (errors, skeletons)
- `src/config.ts`: frame size (768x432) and line position (0.25) used by the crossing map; match them to `cv/counting.py`
