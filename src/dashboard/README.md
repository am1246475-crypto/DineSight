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
- `src/hooks/`, `src/components/`: data hooks and UI
- `src/config.ts`: frame size (768x432) and line position (0.25) used by the crossing map; match them to `cv/counting.py`
