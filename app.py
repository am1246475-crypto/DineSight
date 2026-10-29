from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

# Save this file as app.py in the DineSight project root, next to src/ and profiles/.
ROOT = Path(__file__).resolve().parent
DETECT_SCRIPT = ROOT / "src" / "detect.py"
PROFILES_DIR = ROOT / "profiles"
RUNS_DIR = ROOT / "runs" / "streamlit"
SUPPORTED_TYPES = ["mp4", "mov", "avi", "mkv", "m4v"]

st.set_page_config(page_title="DineSight | Video Analytics", page_icon="🎥", layout="wide")
st.markdown("""
<style>
.stApp{background:#0b1220}.block-container{padding-top:1.6rem;max-width:1500px}
[data-testid="stSidebar"]{background:#101a2b;border-right:1px solid #26364d}
.hero{padding:1.4rem 1.5rem;border:1px solid #27364d;border-radius:18px;
background:linear-gradient(120deg,#14243a,#101827 65%,#142b32);margin-bottom:1.2rem}
.eyebrow{color:#55d6be;text-transform:uppercase;letter-spacing:.14em;font-size:.76rem;font-weight:700}
.muted,.small-note{color:#9aabc2}.small-note{font-size:.88rem}
div[data-testid="stMetric"]{background:#111d2e;border:1px solid #26364d;padding:14px 16px;border-radius:14px}
div.stButton>button[kind="primary"]{background:#39c6ad;color:#07131b;border:0;font-weight:750}
</style>
""", unsafe_allow_html=True)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def discover_profiles() -> list[str]:
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json")) if PROFILES_DIR.exists() else []


def load_json(path: str | Path | None) -> dict | None:
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_profile_config(name: str | None) -> dict | None:
    if not name:
        return None
    return load_json(PROFILES_DIR / f"{name}.json")


def parse_console_summary(text: str) -> dict:
    """Fallback parser for older runs where the JSON report is unavailable."""
    patterns = {
        "frames": r"Total Processed Frames\s*[:=]\s*(\d+)",
        "entries": r"(?:entries|total entries)\s*[:=]\s*(\d+)",
        "exits": r"(?:exits|total exits)\s*[:=]\s*(\d+)",
        "net_change": r"(?:net_change|net change)\s*[:=]\s*([+-]?\d+)",
        "total_tables": r"Total Tables Monitored\s*[:=]\s*(\d+)",
        "empty_tables": r"Empty Tables Count\s*[:=]\s*(\d+)",
        "occupied_tables": r"Occupied Tables Count\s*[:=]\s*(\d+)",
        "unique_tracks": r"Unique Track IDs\s*[:=]\s*(\d+)",
        "customers": r"Customers / Staff\s*[:=]\s*(\d+)",
        "staff": r"Customers / Staff\s*[:=]\s*\d+\s*/\s*(\d+)",
        "visible_now": r"Unknown / Visible Now\s*[:=]\s*\d+\s*/\s*(\d+)",
    }
    result: dict[str, int] = {}
    for key, pattern in patterns.items():
        matches = re.findall(pattern, text, flags=re.IGNORECASE)
        if matches:
            result[key] = int(matches[-1])
    return result


def build_pipeline_command(
    input_path: Path,
    output_path: Path,
    report_path: Path,
    profile: str | None,
    manual_line: tuple[float, float, float, float] | None,
    entry_direction: str,
) -> list[str]:
    command = [
        sys.executable,
        str(DETECT_SCRIPT),
        str(input_path),
        str(output_path),
        "--report",
        str(report_path),
    ]
    if profile:
        command.extend(["--profile", profile])
    if manual_line:
        command.extend([
            "--line",
            ",".join(f"{v:.5f}" for v in manual_line),
            "--entry-dir",
            entry_direction,
        ])
    return command


def run_pipeline(
    input_path: Path,
    output_path: Path,
    report_path: Path,
    profile: str | None,
    manual_line: tuple[float, float, float, float] | None,
    entry_direction: str,
) -> tuple[subprocess.CompletedProcess, list[str]]:
    command = build_pipeline_command(
        input_path, output_path, report_path, profile, manual_line, entry_direction
    )
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    return result, command


def table_dataframe(report: dict) -> pd.DataFrame:
    table_data = report.get("tables") or {}
    rows = []
    for table_id, info in table_data.items():
        count = int(info.get("count") or 0)
        capacity = int(info.get("capacity") or 0)
        seated_ids = info.get("seated_ids") or []
        unique_ids = info.get("unique_seated_ids") or []
        rows.append({
            "Table ID": int(table_id) if str(table_id).isdigit() else table_id,
            "Table": info.get("name", f"Table {table_id}"),
            "Status": info.get("status", "—"),
            "Seated": count,
            "Capacity": capacity,
            "Occupancy %": round((count / capacity) * 100) if capacity else 0,
            "Peak seated": int(info.get("peak_count") or 0),
            "Unique guests": len(unique_ids),
            "Seated track IDs": ", ".join(str(v) for v in seated_ids) or "—",
            "All guest track IDs": ", ".join(str(v) for v in unique_ids) or "—",
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["_order"] = pd.to_numeric(df["Table ID"], errors="coerce").fillna(0)
    return (
        df.sort_values(["_order", "Table"])
        .drop(columns="_order")
        .reset_index(drop=True)
    )


def events_dataframe(report: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    events = report.get("events") or []
    if not events:
        return pd.DataFrame(), pd.DataFrame()

    df = pd.DataFrame(events)
    if "time_s" not in df.columns:
        fps = report.get("fps") or 25
        df["time_s"] = df["frame"] / fps
    df = df.rename(columns={
        "frame": "Frame",
        "time_s": "Time (s)",
        "track_id": "Track ID",
        "type": "Event",
    })
    for column in ("Frame", "Time (s)", "Track ID", "Event"):
        if column not in df.columns:
            df[column] = None

    counted = df[df["Event"].isin(["ENTRY", "EXIT"])].copy()
    counted = counted.sort_values(["Frame", "Track ID"], kind="stable")
    counted["Entry total"] = (counted["Event"] == "ENTRY").cumsum().astype(int)
    counted["Exit total"] = (counted["Event"] == "EXIT").cumsum().astype(int)
    counted["Net"] = counted["Entry total"] - counted["Exit total"]
    return df, counted.reset_index(drop=True)


def occupation_history_dataframe(report: dict) -> pd.DataFrame:
    history = report.get("occupancy_history") or []
    if not history:
        return pd.DataFrame()
    df = pd.DataFrame(history)
    keep = [c for c in ("time_s", "seated", "occupied_tables", "empty_tables") if c in df.columns]
    if "time_s" not in keep:
        return pd.DataFrame()
    return df[keep].rename(columns={
        "time_s": "Time (s)",
        "seated": "Seated guests",
        "occupied_tables": "Occupied tables",
        "empty_tables": "Empty tables",
    }).set_index("Time (s)")


def occupancy_by_table_dataframe(report: dict) -> pd.DataFrame:
    history = report.get("occupancy_history") or []
    if not history:
        return pd.DataFrame()
    table_names = {
        str(table_id): info.get("name", f"Table {table_id}")
        for table_id, info in (report.get("tables") or {}).items()
    }
    rows = []
    for sample in history:
        row = {"Time (s)": sample.get("time_s", 0)}
        for table_id, count in (sample.get("tables") or {}).items():
            row[table_names.get(str(table_id), f"Table {table_id}")] = count
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("Time (s)")


def render_kpis(report: dict) -> None:
    classification = report.get("classification") or {}
    summary = report.get("table_summary") or {}
    scene = report.get("scene") or {}

    row1 = st.columns(4)
    row1[0].metric("Doorway entries", report.get("entries", 0))
    row1[1].metric("Doorway exits", report.get("exits", 0))
    row1[2].metric("Net change", f"{report.get('net_change', 0):+d}")
    row1[3].metric("Track IDs seen", classification.get("unique_track_ids", "—"))

    row2 = st.columns(4)
    row2[0].metric("Customers", classification.get("customers", "—"))
    row2[1].metric("Staff", classification.get("staff", "—"))
    row2[2].metric("Unknown", classification.get("unknown", "—"))
    row2[3].metric(
        "Tables monitored",
        summary.get("total_tables", scene.get("table_count", "—")),
    )

    row3 = st.columns(4)
    row3[0].metric("Occupied tables", summary.get("occupied", "—"))
    row3[1].metric("Empty tables", summary.get("empty", "—"))
    row3[2].metric("Full tables", summary.get("full", "—"))
    row3[3].metric("Guests seated now", summary.get("total_seated", "—"))

    st.caption(
        f"Visible at the end: **{classification.get('visible_at_end', '—')}** · "
        f"Doorway source: **{scene.get('doorway_source', '—')}** · "
        f"Entry source: **{scene.get('entry_source', '—')}** · "
        f"Table source: **{scene.get('tables_source', '—')}**"
    )


def render_profile_details(config: dict) -> None:
    if config.get("description"):
        st.caption(config["description"])
    line_start = config.get("line_start")
    line_end = config.get("line_end")
    if line_start and line_end:
        st.markdown(
            f"**Doorway line** `({line_start[0]:.3f}, {line_start[1]:.3f}) → "
            f"({line_end[0]:.3f}, {line_end[1]:.3f})`"
        )
    if config.get("entry_direction"):
        st.markdown(f"**Entry direction** `{config['entry_direction']}`")

    tables = config.get("tables") or []
    if tables:
        rows = [{
            "ID": t.get("id"),
            "Name": t.get("name", f"Table {t.get('id')}"),
            "Description": t.get("desc", "—"),
            "Capacity": t.get("capacity", "—"),
            "Box ratio": ", ".join(f"{v:.2f}" for v in t.get("box_ratio", [])),
        } for t in tables]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.caption("No table zones configured — the pipeline will auto-discover tables.")


# ----------------------------------------------------------------------
# Session + sidebar
# ----------------------------------------------------------------------
if "last_run" not in st.session_state:
    st.session_state.last_run = None

with st.sidebar:
    st.markdown("## 🎥 DineSight")
    st.caption("Restaurant video intelligence")
    st.divider()
    st.markdown("### Analysis setup")
    profiles = ["Automatic / no profile"] + discover_profiles()
    profile_label = st.selectbox(
        "Scene profile",
        profiles,
        help="Choose a saved scene profile or try processing without one.",
    )
    profile = None if profile_label == "Automatic / no profile" else profile_label

    profile_config = load_profile_config(profile)
    if profile_config:
        with st.expander("Profile details", expanded=False):
            render_profile_details(profile_config)

    use_manual_line = st.checkbox("Override doorway line", value=False)
    line = None
    direction = "left_to_right"
    if use_manual_line:
        st.caption("Normalized coordinates (0.0–1.0)")
        a, b = st.columns(2)
        with a:
            x1 = st.slider("Start X", 0.0, 1.0, 0.35, 0.01)
            y1 = st.slider("Start Y", 0.0, 1.0, 0.15, 0.01)
        with b:
            x2 = st.slider("End X", 0.0, 1.0, 0.35, 0.01)
            y2 = st.slider("End Y", 0.0, 1.0, 0.85, 0.01)
        direction = st.selectbox(
            "Entry direction",
            ["left_to_right", "right_to_left", "top_to_bottom", "bottom_to_top"],
        )
        line = (x1, y1, x2, y2)

    st.divider()
    st.markdown("**Detection** · YOLO11n  \n**Tracking** · ByteTrack  \n**Analytics** · DineSight counters")

st.markdown("""
<div class="hero">
 <div class="eyebrow">Computer vision · Restaurant analytics</div>
 <h1 style="margin:.35rem 0">Turn CCTV footage into insights.</h1>
 <div class="muted">Upload a restaurant video, choose a scene configuration, and generate
 an annotated video with person tracking, doorway counts, and table analytics.</div>
</div>
""", unsafe_allow_html=True)

if not DETECT_SCRIPT.exists():
    st.error(f"Pipeline not found: {DETECT_SCRIPT}")
    st.info("Save app.py in the project root, beside the src/ folder.")
    st.stop()

left, right = st.columns([1.15, .85], gap="large")
with left:
    st.subheader("01 · Video input")
    uploaded = st.file_uploader(
        "Choose a restaurant video",
        type=SUPPORTED_TYPES,
        help="Processing runs locally through your existing DineSight Python pipeline.",
    )
    if uploaded:
        st.success(f"Ready: {uploaded.name} · {uploaded.size / (1024 * 1024):.1f} MB")
        st.video(uploaded)
    st.subheader("02 · Run analysis")
    st.markdown(
        '<div class="small-note">For a new scene, try automatic mode first. If doorway '
        "placement is wrong, use a saved profile or override the line in the sidebar.</div>",
        unsafe_allow_html=True,
    )
    run_clicked = st.button(
        "▶  Analyze video",
        type="primary",
        width="stretch",
        disabled=uploaded is None,
    )

with right:
    st.subheader("Pipeline")
    st.markdown("""
**1. Input validation**  
<span class="muted">Read the uploaded video</span><br><br>
**2. Detection & tracking**  
<span class="muted">YOLO person detection + ByteTrack IDs</span><br><br>
**3. Counting & occupancy**  
<span class="muted">Doorway crossings, customer/staff states, and table occupancy</span><br><br>
**4. Full analytics report**  
<span class="muted">KPIs, table states, event log, and annotated output</span>
""", unsafe_allow_html=True)
    st.info(
        "Analytics are estimates. Results depend on camera angle, visibility, tracking quality, "
        "and correct scene configuration."
    )
    if profile_config and profile_config.get("description"):
        st.caption(f"Selected profile: {profile_config['description']}")

# ----------------------------------------------------------------------
# Run pipeline
# ----------------------------------------------------------------------
if run_clicked and uploaded is not None:
    run_id = uuid.uuid4().hex[:8]
    suffix = Path(uploaded.name).suffix.lower() or ".mp4"
    input_path = RUNS_DIR / f"input_{run_id}{suffix}"
    output_path = RUNS_DIR / f"dinesight_{run_id}.mp4"
    report_path = RUNS_DIR / f"report_{run_id}.json"
    try:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        input_path.write_bytes(uploaded.getbuffer())
        with st.spinner("Processing video… longer videos may take a while."):
            result, command = run_pipeline(
                input_path, output_path, report_path, profile, line, direction
            )
        st.session_state.last_run = {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "command": " ".join(command),
            "input_path": str(input_path),
            "output_path": str(output_path),
            "report_path": str(report_path),
            "input_name": uploaded.name,
            "profile": profile_label,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as exc:
        st.error(f"Could not start the pipeline: {exc}")

# ----------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------
run = st.session_state.last_run
if run:
    st.divider()
    st.subheader("03 · Analysis results")

    output_path = Path(run["output_path"])
    report = load_json(run.get("report_path"))
    fallback_metrics = parse_console_summary(run["stdout"] + "\n" + run["stderr"]) if not report else {}
    succeeded = run["returncode"] == 0 and output_path.exists()

    if succeeded:
        st.success("Analysis completed.")
        st.caption(
            f"Input: {run['input_name']} · Profile: {run['profile']} · {run['created_at']}"
        )
    else:
        st.error("Analysis failed. Inspect the logs below.")
        st.caption(f"Pipeline exit code: {run['returncode']}")

    if report:
        render_kpis(report)

    overview_tab, tables_tab, doorway_tab, scene_tab, logs_tab = st.tabs([
        "📊 Overview",
        "🪑 Tables",
        "🚪 Doorway & events",
        "🧭 Scene setup",
        "🧾 Logs & downloads",
    ])

    with overview_tab:
        video_col, facts_col = st.columns([1.25, .75], gap="large")
        with video_col:
            if output_path.exists():
                st.video(str(output_path))
                st.download_button(
                    "⬇ Download annotated video",
                    data=output_path.read_bytes(),
                    file_name=f"dinesight_{Path(run['input_name']).stem}_annotated.mp4",
                    mime="video/mp4",
                    width="stretch",
                    key="download_video",
                )
            else:
                st.warning("Annotated video not found on disk.")

        with facts_col:
            st.markdown("#### Run details")
            frames = report.get("frames", "—") if report else fallback_metrics.get("frames", "—")
            duration = report.get("duration_s", "—") if report else "—"
            fps = report.get("fps", "—") if report else "—"
            resolution = (report.get("resolution") or {}) if report else {}
            res_text = (
                f"{resolution.get('width')} × {resolution.get('height')}"
                if resolution else "—"
            )
            st.metric("Frames processed", frames)
            st.metric("Video duration", f"{duration} s" if duration != "—" else "—")
            st.metric("Frame rate", f"{fps} fps" if fps != "—" else "—")
            st.metric("Resolution", res_text)

            scene = report.get("scene", {}) if report else {}
            st.caption(
                f"Doorway source: **{scene.get('doorway_source', '—')}**  \n"
                f"Table source: **{scene.get('tables_source', '—')}**"
            )
            if report and report.get("net_change") is not None:
                st.caption(
                    "Net change is entries minus exits during the processed video; it is not a "
                    "full day's occupancy."
                )

        if report:
            table_df = table_dataframe(report)
            if not table_df.empty:
                st.markdown("#### Table occupancy at the end of the video")
                chart_df = table_df.set_index("Table")[["Seated", "Capacity"]]
                st.bar_chart(chart_df)

            history_df = occupation_history_dataframe(report)
            if not history_df.empty:
                st.markdown("#### Occupancy over time")
                seats = [c for c in ("Seated guests", "Occupied tables") if c in history_df.columns]
                if seats:
                    st.line_chart(history_df[seats])
        else:
            metrics = {k: v for k, v in fallback_metrics.items() if k != "frames"}
            if metrics:
                cols = st.columns(len(metrics))
                for col, (key, value) in zip(cols, metrics.items()):
                    col.metric(key.replace("_", " ").title(), value)
            st.caption(
                "No JSON report was produced for this run. Scores above were parsed from the "
                "console log; re-run to generate the full dashboard."
            )

    with tables_tab:
        if not report:
            st.info("No structured table data is available for this run.")
        else:
            summary = report.get("table_summary") or {}
            cols = st.columns(5)
            cols[0].metric("Monitored", summary.get("total_tables", "—"))
            cols[1].metric("Occupied", summary.get("occupied", "—"))
            cols[2].metric("Full", summary.get("full", "—"))
            cols[3].metric("Empty", summary.get("empty", "—"))
            cols[4].metric("Seated now", summary.get("total_seated", "—"))

            table_df = table_dataframe(report)
            if table_df.empty:
                st.info("No table zones were configured or discovered for this video.")
            else:
                st.dataframe(
                    table_df[
                        [
                            "Table",
                            "Status",
                            "Seated",
                            "Capacity",
                            "Occupancy %",
                            "Peak seated",
                            "Unique guests",
                            "Seated track IDs",
                            "All guest track IDs",
                        ]
                    ],
                    hide_index=True,
                    width="stretch",
                    column_config={
                        "Status": st.column_config.TextColumn("Status"),
                        "Occupancy %": st.column_config.ProgressColumn(
                            "Occupancy",
                            min_value=0,
                            max_value=100,
                            format="%d%%",
                        ),
                        "Peak seated": st.column_config.NumberColumn("Peak", format="%d"),
                        "Unique guests": st.column_config.NumberColumn("Unique guests", format="%d"),
                    },
                )
                st.bar_chart(table_df.set_index("Table")[["Seated", "Capacity"]])

                st.download_button(
                    "⬇ Download table data (CSV)",
                    data=table_df.to_csv(index=False).encode("utf-8"),
                    file_name=f"dinesight_tables_{Path(run['input_name']).stem}.csv",
                    mime="text/csv",
                    key="download_tables",
                )

            history_df = occupation_history_dataframe(report)
            if not history_df.empty:
                st.markdown("#### Occupancy over time")
                seats = [c for c in ("Seated guests", "Occupied tables") if c in history_df.columns]
                if seats:
                    st.line_chart(history_df[seats])

                per_table_df = occupancy_by_table_dataframe(report)
                if not per_table_df.empty:
                    with st.expander("Per-table seat counts over time"):
                        st.line_chart(per_table_df)

            with st.expander("Table status breakdown"):
                summary = report.get("table_summary") or {}
                st.markdown(
                    f"**Occupied ({summary.get('occupied', 0)}):** "
                    f"{', '.join(summary.get('occupied_tables') or []) or '—'}  \n"
                    f"**Full ({summary.get('full', 0)}):** "
                    f"{', '.join(summary.get('full_tables') or []) or '—'}  \n"
                    f"**Empty ({summary.get('empty', 0)}):** "
                    f"{', '.join(summary.get('empty_tables') or []) or '—'}"
                )

    with doorway_tab:
        if not report:
            st.info("No structured doorway data is available for this run.")
        else:
            cols = st.columns(3)
            cols[0].metric("Entries", report.get("entries", 0))
            cols[1].metric("Exits", report.get("exits", 0))
            cols[2].metric("Net change", f"{report.get('net_change', 0):+d}")

            all_events, counted_events = events_dataframe(report)
            if counted_events.empty:
                st.info("No doorway crossings were recorded.")
            else:
                st.markdown("#### Crossing timeline")
                timeline = counted_events.set_index("Time (s)")[
                    ["Entry total", "Exit total", "Net"]
                ]
                st.line_chart(timeline)

                st.markdown("#### Event log")
                st.dataframe(
                    counted_events[
                        ["Time (s)", "Frame", "Event", "Track ID", "Entry total", "Exit total", "Net"]
                    ],
                    hide_index=True,
                    width="stretch",
                )

                st.download_button(
                    "⬇ Download event log (CSV)",
                    data=counted_events.to_csv(index=False).encode("utf-8"),
                    file_name=f"dinesight_events_{Path(run['input_name']).stem}.csv",
                    mime="text/csv",
                    key="download_events",
                )

            if not all_events.empty:
                with st.expander("All doorway events (including cancelled reversals)"):
                    st.dataframe(
                        all_events[["Time (s)", "Frame", "Event", "Track ID"]],
                        hide_index=True,
                        width="stretch",
                    )

    with scene_tab:
        if not report:
            st.info("No structured scene data is available for this run.")
        else:
            scene = report.get("scene") or {}
            line_norm = report.get("doorway_line_normalized")
            line_px = report.get("doorway_line")
            resolution = (report.get("resolution") or {}) if report else {}

            col1, col2 = st.columns(2, gap="large")
            with col1:
                st.markdown("#### Doorway")
                st.markdown(f"**Source** `{scene.get('doorway_source', '—')}`")
                if scene.get("doorway_confidence") is not None:
                    st.markdown(
                        f"**Auto-estimate confidence** `{scene['doorway_confidence']:.2f}`"
                    )
                st.markdown(f"**Entry source** `{scene.get('entry_source', '—')}`")
                st.markdown(f"**Entry direction** `{report.get('entry', '—')}`")
                if line_norm:
                    st.markdown(
                        f"**Line (normalized)**  \n"
                        f"`({line_norm[0][0]:.4f}, {line_norm[0][1]:.4f}) → "
                        f"({line_norm[1][0]:.4f}, {line_norm[1][1]:.4f})`"
                    )
                if line_px:
                    st.markdown(
                        f"**Line (pixels)**  \n"
                        f"`({line_px[0][0]}, {line_px[0][1]}) → "
                        f"({line_px[1][0]}, {line_px[1][1]})`"
                    )

            with col2:
                st.markdown("#### Sources & video")
                st.markdown(f"**Profile** `{scene.get('profile', run['profile'])}`")
                st.markdown(f"**Source** `{report.get('source', run['input_path'])}`")
                st.markdown(f"**Table source** `{scene.get('tables_source', '—')}`")
                st.markdown(f"**Tables configured/discovered** `{scene.get('table_count', '—')}`")
                st.markdown(f"**Frames** `{report.get('frames', '—')}`")
                st.markdown(f"**FPS** `{report.get('fps', '—')}`")
                st.markdown(f"**Duration** `{report.get('duration_s', '—')} s`")
                if resolution:
                    st.markdown(
                        f"**Resolution** `{resolution.get('width')} × {resolution.get('height')}`"
                    )

            profile_name = scene.get("profile")
            if profile_name and profile_name != "automatic":
                config = load_profile_config(profile_name)
                if config:
                    st.markdown("#### Saved profile configuration")
                    render_profile_details(config)

            with st.expander("Raw JSON report"):
                st.json(report)

    with logs_tab:
        st.markdown("#### Downloads")
        dl1, dl2, dl3 = st.columns(3)
        if report:
            dl1.download_button(
                "⬇ JSON report",
                data=json.dumps(report, indent=2).encode("utf-8"),
                file_name=f"dinesight_report_{Path(run['input_name']).stem}.json",
                mime="application/json",
                width="stretch",
                key="download_report",
            )
        console_text = (run.get("stdout") or "") + "\n" + (run.get("stderr") or "")
        dl2.download_button(
            "⬇ Console log",
            data=console_text.encode("utf-8"),
            file_name=f"dinesight_log_{Path(run['input_name']).stem}.txt",
            mime="text/plain",
            width="stretch",
            key="download_log",
        )
        if run.get("command"):
            dl3.download_button(
                "⬇ Command",
                data=run["command"].encode("utf-8"),
                file_name="dinesight_command.txt",
                mime="text/plain",
                width="stretch",
                key="download_command",
            )

        if run.get("command"):
            st.markdown("**Pipeline command**")
            st.code(run["command"], language="bash")

        if run.get("stdout"):
            st.markdown("**Standard output**")
            st.code(run["stdout"], language="text")
        if run.get("stderr"):
            st.markdown("**Errors / warnings**")
            st.code(run["stderr"], language="text")

    st.caption(
        "Input, annotated output, and JSON reports are stored in runs/streamlit/. "
        "Remove that folder when no longer needed."
    )

st.divider()
st.caption("DineSight · Local video analytics prototype")
