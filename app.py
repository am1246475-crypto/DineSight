from __future__ import annotations

import json
import re
import shutil
import tempfile
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
import inspect

import streamlit as st
from PIL import Image

try:
    from streamlit_image_coordinates import streamlit_image_coordinates
    MOUSE_SELECTION_AVAILABLE = True
except ImportError:
    streamlit_image_coordinates = None
    MOUSE_SELECTION_AVAILABLE = False

# Works on both old and new Streamlit: new versions use width="stretch",
# older ones only know use_container_width=True.
if "width" in inspect.signature(st.download_button).parameters:
    STRETCH = {"width": "stretch"}
else:
    STRETCH = {"use_container_width": True}

# Save this file as app.py in the DineSight project root, next to src/ and profiles/.
ROOT = Path(__file__).resolve().parent
DETECT_SCRIPT = ROOT / "src" / "detect.py"
PROFILES_DIR = ROOT / "profiles"
RUNS_DIR = ROOT / "runs" / "streamlit"
SUPPORTED_TYPES = ["mp4", "mov", "avi", "mkv", "m4v"]
PIPELINE_TIMEOUT_S = 60 * 60  # stop a stuck analysis after 1 hour
KEEP_LAST_RUNS = 5  # old inputs/outputs/reports beyond this are deleted

st.set_page_config(page_title="DineSight | Video Analytics", page_icon="🎥", layout="wide")
st.markdown("""
<style>
:root{
  --bg:#0E1117; --surface:#161B26; --surface-2:#1C2333; --border:#2A3347;
  --text:#E8ECF4; --muted:#A8B3C7; --accent:#F59E0B; --accent-2:#2DD4BF;
}
.stApp{background:var(--bg);color:var(--text)}
.block-container{padding-top:1.6rem;max-width:1500px}
[data-testid="stSidebar"]{background:var(--surface);border-right:1px solid var(--border)}

.hero{padding:1.5rem 1.6rem;border:1px solid var(--border);border-radius:18px;
  background:linear-gradient(120deg,#1C2333 0%,#161B26 60%,#2B2110 100%);
  margin-bottom:1.2rem}
.eyebrow{color:var(--accent);text-transform:uppercase;letter-spacing:.14em;
  font-size:.76rem;font-weight:700}
.muted,.small-note{color:var(--muted)}.small-note{font-size:.88rem}

div[data-testid="stMetric"]{background:var(--surface-2);border:1px solid var(--border);
  padding:14px 16px;border-radius:14px;border-left:3px solid var(--accent-2)}
div[data-testid="stMetricValue"]{color:var(--text)}
div[data-testid="stMetricLabel"]{color:var(--muted)}

div.stButton>button[kind="primary"],
div.stDownloadButton>button{background:var(--accent);color:#1A1204;border:0;
  font-weight:700;border-radius:10px}
div.stButton>button[kind="primary"]:hover,
div.stDownloadButton>button:hover{background:#FBB43A;color:#1A1204}

/* ---- text colors ---- */
h1,h2,h3,h4,h5{color:#F8FAFC !important;letter-spacing:-.01em}
h4{color:var(--accent) !important;font-size:1.02rem;font-weight:700}
.stMarkdown p,.stMarkdown li,[data-testid="stSidebar"] p{color:var(--text)}
strong{color:#FFFFFF}
[data-testid="stCaptionContainer"],[data-testid="stCaptionContainer"] p{color:var(--muted) !important}
[data-testid="stWidgetLabel"] p,label p{color:var(--text) !important;font-weight:600}
[data-testid="stSidebar"] h2,[data-testid="stSidebar"] h3{color:var(--accent) !important}
a,a:visited{color:var(--accent-2) !important}
code{color:#FCD34D !important;background:#1C2333 !important;border-radius:6px;padding:.1em .35em}
.hl{color:var(--accent)}
button[data-baseweb="tab"]{color:var(--muted)}
button[data-baseweb="tab"]:hover{color:var(--text)}
button[data-baseweb="tab"][aria-selected="true"]{color:var(--accent)}
div[data-baseweb="tab-highlight"]{background:var(--accent)}
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
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=PIPELINE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        result = subprocess.CompletedProcess(
            command, 124, "", f"Pipeline timed out after {PIPELINE_TIMEOUT_S // 60} minutes."
        )
    return result, command


def _find_ffmpeg() -> str | None:
    """System ffmpeg if installed, otherwise the binary bundled with imageio-ffmpeg (pip install imageio-ffmpeg)."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def make_browser_playable(path: Path) -> tuple[bool, str]:
    """Re-encode to H.264 so st.video can play the annotated output in the browser.

    Returns (ok, message). OpenCV writes MPEG-4 Part 2 ("mp4v"), which browsers cannot play,
    so without this step the player stays black/empty even though the file is fine.
    """
    ffmpeg = _find_ffmpeg()
    if not path.exists():
        return False, "Output video was not created."
    if not ffmpeg:
        return False, "ffmpeg not found. Run: pip install imageio-ffmpeg  (or install ffmpeg and add it to PATH)."
    tmp = path.with_suffix(".h264.mp4")
    try:
        r = subprocess.run(
            [ffmpeg, "-y", "-i", str(path),
             "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-preset", "veryfast", "-movflags", "+faststart", str(tmp)],
            capture_output=True,
            check=False,
        )
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(path)
            return True, ""
        err = r.stderr.decode("utf-8", "ignore")[-300:] if r.stderr else ""
        return False, f"ffmpeg failed to re-encode the video. {err}"
    except OSError as exc:
        return False, f"ffmpeg could not be started: {exc}"
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def cleanup_old_runs(keep_runs: int = KEEP_LAST_RUNS) -> None:
    """Keep only the newest files (about 3 files per run) in runs/streamlit/."""
    if not RUNS_DIR.exists():
        return
    files = sorted(
        (f for f in RUNS_DIR.iterdir() if f.is_file()),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    for old in files[keep_runs * 3:]:
        old.unlink(missing_ok=True)


ENTRY_VECTORS = {
    "left_to_right": (1.0, 0.0),
    "right_to_left": (-1.0, 0.0),
    "top_to_bottom": (0.0, 1.0),
    "bottom_to_top": (0.0, -1.0),
}


def order_quad(points):
    """Sort 4 clicked points around their centre so click order doesn't matter."""
    import math
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    return sorted(points, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))


def quad_to_line(points):
    """Doorway crossing line from 4 corners: joins the midpoints of the two opposite
    edges that are farthest apart (the door's centre line along its long side)."""
    q = order_quad(points)
    mid = lambda a, b: ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    m = [mid(q[i], q[(i + 1) % 4]) for i in range(4)]
    dist = lambda a, b: (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2
    a, b = (m[0], m[2]) if dist(m[0], m[2]) >= dist(m[1], m[3]) else (m[1], m[3])
    return (a[0], a[1], b[0], b[1])


def get_first_frame(uploaded_file):
    """First frame of the uploaded video as a BGR array (cached per upload)."""
    key = f"first_frame_{uploaded_file.name}_{uploaded_file.size}"
    if key in st.session_state:
        return st.session_state[key]
    suffix = Path(uploaded_file.name).suffix or ".mp4"
    tmp_path = None
    frame = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(uploaded_file.getbuffer())
            tmp_path = tmp.name
        cap = cv2.VideoCapture(tmp_path)
        try:
            ok, img = cap.read()
            frame = img if ok else None
        finally:
            cap.release()
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
    st.session_state[key] = frame
    return frame


def draw_line_preview(frame, line, direction, quad=None):
    """Draw the doorway line and entry arrow on a frame; returns an RGB image."""
    img = frame.copy()
    h, w = img.shape[:2]
    p1 = (int(line[0] * w), int(line[1] * h))
    p2 = (int(line[2] * w), int(line[3] * h))
    if quad:
        poly = np.array([[int(x * w), int(y * h)] for x, y in order_quad(quad)], np.int32)
        overlay = img.copy()
        cv2.fillPoly(overlay, [poly], (11, 158, 245))
        img = cv2.addWeighted(overlay, 0.25, img, 0.75, 0)
        cv2.polylines(img, [poly], True, (11, 158, 245), 2)
    cv2.line(img, p1, p2, (11, 158, 245), 4)
    for p in (p1, p2):
        cv2.circle(img, p, 7, (11, 158, 245), -1)
    mid = ((p1[0] + p2[0]) // 2, (p1[1] + p2[1]) // 2)
    vx, vy = ENTRY_VECTORS.get(direction, (1.0, 0.0))
    reach = int(0.09 * max(w, h))
    tip = (mid[0] + int(vx * reach), mid[1] + int(vy * reach))
    cv2.arrowedLine(img, mid, tip, (191, 212, 45), 4, tipLength=0.35)
    cv2.putText(img, "ENTRY", (tip[0] + 8, tip[1] + 6), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (191, 212, 45), 2, cv2.LINE_AA)
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def save_doorway_profile(name: str, line, direction: str, quad=None) -> Path | None:
    """Save the doorway as profiles/<name>.json (keeps tables if the profile already exists)."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip()).strip("_")
    if not safe:
        return None
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    path = PROFILES_DIR / f"{safe}.json"
    config = load_json(path) or {}
    config.setdefault("description", f"Doorway saved from the app ({safe})")
    config["line_start"] = [round(line[0], 4), round(line[1], 4)]
    config["line_end"] = [round(line[2], 4), round(line[3], 4)]
    config["entry_direction"] = direction
    if quad:
        config["doorway_quad"] = [[round(x, 4), round(y, 4)] for x, y in order_quad(quad)]
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# Scene editing: table zones + staff uniform colour
# ----------------------------------------------------------------------
MIN_TABLE_SIDE = 0.02      # smallest table zone side (fraction of the frame)
MAX_UNIFORM_SAMPLES = 6


def rerun() -> None:
    if hasattr(st, "rerun"):
        st.rerun()
    else:
        st.experimental_rerun()


def normalize_box(p1, p2) -> list[float] | None:
    """Two clicked corners (normalized) -> [x1, y1, x2, y2] sorted and clamped; None if too small."""
    x1, x2 = sorted((p1[0], p2[0]))
    y1, y2 = sorted((p1[1], p2[1]))
    x1, y1, x2, y2 = (min(1.0, max(0.0, v)) for v in (x1, y1, x2, y2))
    if x2 - x1 < MIN_TABLE_SIDE or y2 - y1 < MIN_TABLE_SIDE:
        return None
    return [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]


def overlap_share(a, b) -> float:
    """Overlap area as a share of the SMALLER box (a small zone swallowed by a big one scores high)."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return (ix * iy) / smaller if smaller > 0 else 0.0


def overlapping_table_pairs(tables: list[dict], threshold: float = 0.15) -> list[tuple[str, str, float]]:
    pairs = []
    for i in range(len(tables)):
        for j in range(i + 1, len(tables)):
            share = overlap_share(tables[i]["box_ratio"], tables[j]["box_ratio"])
            if share > threshold:
                pairs.append((tables[i]["name"], tables[j]["name"], share))
    return pairs


def tables_to_profile_entries(drawn: list[dict]) -> list[dict]:
    """Drawn tables -> the 'tables' list format that detect.py reads from a profile."""
    entries = []
    for i, t in enumerate(drawn):
        name = str(t.get("name") or "").strip() or f"Table {i + 1}"
        entries.append({
            "id": i + 1,
            "name": name,
            "capacity": max(1, int(t.get("capacity", 4))),
            "box_ratio": [round(float(v), 4) for v in t["box_ratio"]],
        })
    return entries


def profile_tables_to_drawn(config: dict | None) -> list[dict]:
    drawn = []
    for t in (config or {}).get("tables") or []:
        box = t.get("box_ratio")
        if isinstance(box, (list, tuple)) and len(box) == 4:
            drawn.append({
                "name": t.get("name", f"Table {t.get('id', len(drawn) + 1)}"),
                "capacity": int(t.get("capacity", 4)),
                "box_ratio": [float(v) for v in box],
            })
    return drawn


def sample_hsv(frame_bgr, nx: float, ny: float, patch: int = 21) -> list[int]:
    """OpenCV-HSV colour (H 0-179, S/V 0-255) around a click: median of a small patch, circular for hue."""
    import math
    h, w = frame_bgr.shape[:2]
    cx, cy, r = int(nx * w), int(ny * h), patch // 2
    crop = frame_bgr[max(0, cy - r): cy + r + 1, max(0, cx - r): cx + r + 1]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(float)
    sat, val = int(np.median(hsv[:, 1])), int(np.median(hsv[:, 2]))
    if sat >= 40:  # coloured: average the hue on the circle so reds (0 and 179) do not cancel out
        ang = hsv[:, 0] * 2.0 * math.pi / 180.0
        hue = int(round((math.atan2(np.sin(ang).mean(), np.cos(ang).mean()) % (2 * math.pi)) * 180.0 / (2 * math.pi))) % 180
    else:
        hue = int(np.median(hsv[:, 0]))
    return [hue, sat, val]


def hsv_to_rgb(hsv) -> tuple[int, int, int]:
    r, g, b = cv2.cvtColor(np.uint8([[hsv]]), cv2.COLOR_HSV2RGB)[0, 0]
    return int(r), int(g), int(b)


def _profile_safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip()).strip("_")


def apply_scene_to_config(config: dict, tables: list[dict] | None, uniform_samples: list[list[int]] | None) -> dict:
    """Return *config* with tables / uniform replaced. None = leave untouched; [] = clear."""
    config = dict(config or {})
    if tables is not None:
        if tables:
            config["tables"] = tables_to_profile_entries(tables)
        else:
            config.pop("tables", None)  # no tables -> the pipeline discovers them automatically
    if uniform_samples is not None:
        if uniform_samples:
            block = dict(config["staff_uniform"]) if isinstance(config.get("staff_uniform"), dict) else {}
            block["samples"] = [[int(v) for v in x] for x in uniform_samples]
            config["staff_uniform"] = block
        else:
            config.pop("staff_uniform", None)
    return config


def save_scene_to_profile(name: str, tables: list[dict] | None, uniform_samples: list[list[int]] | None) -> Path | None:
    """Save table zones and/or uniform samples into profiles/<name>.json (keeps the doorway and other keys)."""
    safe = _profile_safe_name(name)
    if not safe:
        return None
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    path = PROFILES_DIR / f"{safe}.json"
    config = load_json(path) or {}
    config.setdefault("description", f"Scene saved from the app ({safe})")
    config = apply_scene_to_config(config, tables, uniform_samples)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def build_run_profile(base_config: dict | None, tables, uniform_samples, run_id: str) -> Path:
    """Temporary profile for ONE run: the selected profile + what was drawn/sampled but not saved."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    config = apply_scene_to_config(base_config or {}, tables, uniform_samples)
    path = RUNS_DIR / f"profile_{run_id}.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def get_frame_at(uploaded_file, fraction: float):
    """Frame at *fraction* (0-1) of the uploaded video as BGR, cached per upload and position."""
    if fraction <= 0:
        return get_first_frame(uploaded_file)
    key = f"frame_{uploaded_file.name}_{uploaded_file.size}_{int(fraction * 100)}"
    if key in st.session_state:
        return st.session_state[key]
    suffix = Path(uploaded_file.name).suffix or ".mp4"
    tmp_path, frame = None, None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(uploaded_file.getbuffer())
            tmp_path = tmp.name
        cap = cv2.VideoCapture(tmp_path)
        try:
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if total > 1:
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(fraction * (total - 1)))
            ok, img = cap.read()
            frame = img if ok else None
        finally:
            cap.release()
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
    st.session_state[key] = frame
    return frame


def draw_tables_on(img_rgb, tables: list[dict], pending=None) -> None:
    """Draw table zones (and a half-finished first corner) on an RGB image, in place."""
    h, w = img_rgb.shape[:2]
    color = (245, 158, 11)
    for i, t in enumerate(tables):
        x1, y1, x2, y2 = t["box_ratio"]
        p1, p2 = (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h))
        cv2.rectangle(img_rgb, p1, p2, color, 2)
        label = f"{i + 1}: {t['name']} ({t['capacity']})"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(img_rgb, (p1[0], p1[1]), (p1[0] + tw + 6, p1[1] + th + 8), color, -1)
        cv2.putText(img_rgb, label, (p1[0] + 3, p1[1] + th + 3), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (20, 20, 20), 1, cv2.LINE_AA)
    if pending is not None:
        c = (int(pending[0] * w), int(pending[1] * h))
        cv2.circle(img_rgb, c, 7, (45, 212, 191), -1)
        cv2.putText(img_rgb, "corner 1", (c[0] + 9, c[1] - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (45, 212, 191), 2, cv2.LINE_AA)


def pick_point(frame_bgr, state_key: str, draw=None):
    """Show the frame and return the normalized (x, y) of ONE new click, or None.
    The click widget is re-keyed after each click so the same click is never reported twice."""
    if not MOUSE_SELECTION_AVAILABLE:
        return None
    counter_key = f"{state_key}_n"
    n = st.session_state.get(counter_key, 0)
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    oh, ow = rgb.shape[:2]
    dw = min(ow, 900)
    dh = max(1, int(oh * dw / ow))
    img = cv2.resize(rgb, (dw, dh))
    if draw is not None:
        draw(img)
    clicked = streamlit_image_coordinates(Image.fromarray(img), width=dw, key=f"{state_key}_{n}")
    if clicked is not None and clicked.get("x") is not None and clicked.get("y") is not None:
        st.session_state[counter_key] = n + 1
        return (min(1.0, max(0.0, float(clicked["x"]) / dw)), min(1.0, max(0.0, float(clicked["y"]) / dh)))
    return None


def render_scene_editor(uploaded, profile: str | None, profile_config: dict | None) -> None:
    """Optional expander: draw table zones and sample the staff uniform colour on a video frame."""
    ss = st.session_state
    identity = f"{uploaded.name}_{uploaded.size}|{profile}"
    if ss.get("scene_seed_key") != identity:  # new video or profile: start from what the profile contains
        ss.scene_seed_key = identity
        ss.drawn_tables = profile_tables_to_drawn(profile_config)
        block = (profile_config or {}).get("staff_uniform")
        raw = block.get("samples", []) if isinstance(block, dict) else []
        ss.uniform_samples = [[int(v) for v in x] for x in raw if isinstance(x, (list, tuple)) and len(x) == 3]
        ss.tables_dirty = False
        ss.uniform_dirty = False
        ss.table_corner = None
        ss.table_msg = ""
        ss.uni_last_click = None

    with st.expander("🪑 Table zones & 🧑‍🍳 staff uniform (optional)", expanded=bool(ss.drawn_tables or ss.uniform_samples)):
        st.caption(
            "Draw the tables yourself instead of relying on automatic discovery, and/or tell the system what "
            "the staff uniform looks like. Changing the video or the profile reloads these from the profile."
        )
        pos = st.select_slider("Reference frame", options=list(range(0, 100, 10)), value=0,
                               format_func=lambda v: f"{v}% of the video", key="scene_frame_pos")
        frame = get_frame_at(uploaded, pos / 100.0)
        if frame is None:
            st.warning("Could not read a frame from this video.")
            return
        tab_tables, tab_uniform = st.tabs(["🪑 Tables", "🧑‍🍳 Staff uniform"])

        # ---------------- tables ----------------
        with tab_tables:
            st.caption(
                "Click two opposite corners of each table zone. Draw the zone around the table AND its chairs: "
                "a person belongs to a table when the centre of their body box is inside it. Zones must not overlap."
            )
            seats = st.number_input("Seats at the next table", min_value=1, max_value=20, value=4, step=1, key="tbl_seats")
            if MOUSE_SELECTION_AVAILABLE:
                point = pick_point(frame, "tbl_pick", lambda img: draw_tables_on(img, ss.drawn_tables, ss.table_corner))
                if point is not None:
                    if ss.table_corner is None:
                        ss.table_corner = point
                    else:
                        box = normalize_box(ss.table_corner, point)
                        ss.table_corner = None
                        if box is None:
                            ss.table_msg = "That zone is too small: click two corners further apart."
                        else:
                            ss.drawn_tables.append({
                                "name": f"Table {len(ss.drawn_tables) + 1}",
                                "capacity": int(seats),
                                "box_ratio": box,
                            })
                            ss.tables_dirty = True
                            ss.table_msg = ""
                    rerun()
                if ss.table_msg:
                    st.warning(ss.table_msg)
                st.info("Corner 1 set: click the opposite corner." if ss.table_corner is not None
                        else "Click the first corner of the next table.")
            else:
                st.warning("Mouse drawing needs streamlit-image-coordinates (pip install streamlit-image-coordinates). "
                           "Use the sliders instead.")
                st.image(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                c1, c2 = st.columns(2)
                x1 = c1.slider("Left", 0.0, 1.0, 0.1, 0.01, key="tbl_x1")
                x2 = c2.slider("Right", 0.0, 1.0, 0.3, 0.01, key="tbl_x2")
                y1 = c1.slider("Top", 0.0, 1.0, 0.1, 0.01, key="tbl_y1")
                y2 = c2.slider("Bottom", 0.0, 1.0, 0.3, 0.01, key="tbl_y2")
                if st.button("➕ Add this table"):
                    box = normalize_box((x1, y1), (x2, y2))
                    if box is None:
                        st.warning("That zone is too small.")
                    else:
                        ss.drawn_tables.append({"name": f"Table {len(ss.drawn_tables) + 1}", "capacity": int(seats), "box_ratio": box})
                        ss.tables_dirty = True
                        rerun()

            if ss.drawn_tables:
                df = pd.DataFrame([{"Name": t["name"], "Seats": t["capacity"]} for t in ss.drawn_tables])
                if hasattr(st, "data_editor"):
                    edited = st.data_editor(df, hide_index=True, key=f"tbl_editor_{len(ss.drawn_tables)}")
                    for i, row in enumerate(edited.reset_index(drop=True).itertuples(index=False)):
                        if i >= len(ss.drawn_tables):
                            break
                        name = str(row[0]).strip() or f"Table {i + 1}"
                        cap = int(row[1]) if pd.notna(row[1]) and int(row[1]) >= 1 else 4
                        if name != ss.drawn_tables[i]["name"] or cap != ss.drawn_tables[i]["capacity"]:
                            ss.drawn_tables[i]["name"], ss.drawn_tables[i]["capacity"] = name, cap
                            ss.tables_dirty = True
                else:
                    st.dataframe(df, hide_index=True)
                for a, b, share in overlapping_table_pairs(ss.drawn_tables):
                    st.warning(f"“{a}” and “{b}” overlap ({share:.0%} of the smaller zone). "
                               "People in the overlap can be assigned to the wrong table.")
                b1, b2 = st.columns(2)
                if b1.button("↩ Remove last table"):
                    ss.drawn_tables.pop()
                    ss.table_corner = None
                    ss.tables_dirty = True
                    rerun()
                if b2.button("🗑 Clear all tables"):
                    ss.drawn_tables = []
                    ss.table_corner = None
                    ss.tables_dirty = True
                    rerun()
            if ss.tables_dirty:
                st.success(f"The next analysis will use these {len(ss.drawn_tables)} table(s)"
                           if ss.drawn_tables else "Tables cleared: the next analysis discovers tables automatically.")
            tbl_profile_name = st.text_input("Save the tables to profile", value=profile or "", placeholder="e.g. main_hall",
                                             key="tbl_profile_name")
            if st.button("💾 Save tables to profile", disabled=not ss.drawn_tables):
                saved = save_scene_to_profile(tbl_profile_name, ss.drawn_tables, None)
                if saved:
                    st.success(f"Saved {len(ss.drawn_tables)} table(s) to {saved.name}. Pick it under 'Scene profile'.")
                else:
                    st.warning("Enter a profile name first.")

        # ---------------- uniform ----------------
        with tab_uniform:
            st.caption(
                "Pick a frame where a waiter is clearly visible and click on their shirt (chest). Add one sample "
                "per uniform colour. Customers wearing a similar colour will also be labelled staff, so this works "
                "best with a distinctive colour (black, grey and white uniforms are ambiguous)."
            )
            if not MOUSE_SELECTION_AVAILABLE:
                st.warning("Needs streamlit-image-coordinates: pip install streamlit-image-coordinates")
            else:
                def _draw_cross(img):
                    if ss.uni_last_click is not None:
                        h, w = img.shape[:2]
                        c = (int(ss.uni_last_click[0] * w), int(ss.uni_last_click[1] * h))
                        cv2.drawMarker(img, c, (45, 212, 191), cv2.MARKER_CROSS, 22, 2)

                point = pick_point(frame, "uni_pick", _draw_cross)
                if point is not None:
                    if len(ss.uniform_samples) < MAX_UNIFORM_SAMPLES:
                        ss.uniform_samples.append(sample_hsv(frame, *point))
                        ss.uniform_dirty = True
                    ss.uni_last_click = point
                    rerun()
            for i, hsv in enumerate(ss.uniform_samples):
                r, g, b = hsv_to_rgb(hsv)
                st.markdown(
                    f'<span style="display:inline-block;width:34px;height:18px;background:rgb({r},{g},{b});'
                    f'border:1px solid #888;border-radius:4px;vertical-align:middle"></span> '
                    f'sample {i + 1} · HSV ({hsv[0]}, {hsv[1]}, {hsv[2]})',
                    unsafe_allow_html=True,
                )
            if any(x[1] < 40 for x in ss.uniform_samples):
                st.warning("A grey/black/white sample matches many customers' clothes too. Expect false staff labels.")
            if ss.uniform_samples and st.button("🗑 Clear uniform samples"):
                ss.uniform_samples = []
                ss.uni_last_click = None
                ss.uniform_dirty = True
                rerun()
            if ss.uniform_dirty:
                st.success(f"The next analysis will use {len(ss.uniform_samples)} uniform sample(s)"
                           if ss.uniform_samples else "Uniform cleared: staff are identified by behaviour only.")
            uni_profile_name = st.text_input("Save the uniform to profile", value=profile or "", placeholder="e.g. main_hall",
                                             key="uni_profile_name")
            if st.button("💾 Save uniform to profile", disabled=not ss.uniform_samples):
                saved = save_scene_to_profile(uni_profile_name, None, ss.uniform_samples)
                if saved:
                    st.success(f"Saved {len(ss.uniform_samples)} sample(s) to {saved.name}.")
                else:
                    st.warning("Enter a profile name first.")


def fmt_net(report: dict) -> str:
    try:
        return f"{int(report.get('net_change') or 0):+d}"
    except (TypeError, ValueError):
        return "—"


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
    if "frame" not in df.columns:
        df["frame"] = range(len(df))
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
    row1[2].metric("Net change", fmt_net(report))
    row1[3].metric(
        "Track IDs seen",
        classification.get("unique_track_ids", "—"),
        help="Tracker IDs, not necessarily people: one person can get several IDs. "
             "See 'Result reliability' below for how far to trust this number.",
    )

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


_SOURCE_LABELS = {
    "line": "Seen crossing the line",
    "appeared-in-door-zone": "Inferred: first seen inside the door zone",
    "lost-at-door": "Inferred: lost at the door while walking out",
}


def render_quality(report: dict) -> None:
    """Reliability panel: where the numbers come from and where to doubt them."""
    quality = report.get("quality")
    if not quality:
        st.caption("This report has no reliability section (older run). Re-run the analysis to get one.")
        return
    confidence = str(quality.get("confidence", "—"))
    icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(confidence, "⚪")
    ids = quality.get("ids") or {}
    door = quality.get("door_events") or {}
    sources = quality.get("sources") or {}

    with st.expander(f"{icon} Result reliability: {confidence.upper()}", expanded=confidence != "high"):
        for w in quality.get("warnings") or []:
            level = w.get("level")
            show = st.error if level == "critical" else (st.warning if level == "warning" else st.info)
            show(w.get("message", ""))

        cols = st.columns(4)
        cols[0].metric("Track IDs seen", ids.get("unique_track_ids", "—"))
        cols[1].metric("Peak people on screen", ids.get("max_simultaneous_people", "—"))
        cols[2].metric("IDs merged", ids.get("ids_merged_into_earlier_id", "—"))
        cols[3].metric("Inferred door events", f"{door.get('guessed', 0)} ({float(door.get('guessed_share') or 0):.0%})")
        ratio = ids.get("ids_vs_expected_people")
        if ratio is not None:
            st.caption(
                f"IDs per person expected (people inside at start + entries): **{ratio}×**. "
                "Close to 1 is healthy; a high value means the tracker split people into several IDs."
            )

        rows = [
            {
                "Door event source": _SOURCE_LABELS.get(src, src),
                "Entries": counts.get("entries", 0),
                "Exits": counts.get("exits", 0),
            }
            for src, counts in (door.get("by_source") or {}).items()
        ]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, **STRETCH)
        if door.get("cancelled_reversals"):
            st.caption(f"{door['cancelled_reversals']} quick back-and-forth crossing(s) were cancelled as jitter.")

        st.caption(
            f"Doorway: **{sources.get('doorway', '—')}** · Entry direction: **{sources.get('entry', '—')}** · "
            f"Tables: **{sources.get('tables', '—')}**"
            + (f" · Per-camera settings from profile: **{', '.join(sources['analytics_from_profile'])}**"
               if sources.get("analytics_from_profile") else "")
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
    uniform = config.get("staff_uniform")
    if isinstance(uniform, dict) and uniform.get("samples"):
        st.markdown(f"**Staff uniform** `{len(uniform['samples'])} colour sample(s)`")
    analytics = config.get("analytics")
    if isinstance(analytics, dict) and analytics:
        st.markdown(
            "**Classification settings** "
            + ", ".join(f"`{k}={v}`" for k, v in analytics.items())
        )

    tables = config.get("tables") or []
    if tables:
        rows = [{
            "ID": t.get("id"),
            "Name": t.get("name", f"Table {t.get('id')}"),
            "Description": t.get("desc", "—"),
            "Capacity": t.get("capacity", "—"),
            "Box ratio": ", ".join(f"{v:.2f}" for v in t.get("box_ratio", [])),
        } for t in tables]
        st.dataframe(pd.DataFrame(rows), hide_index=True, **STRETCH)
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
    st.markdown("---")
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
    quad = None
    direction = "left_to_right"
    selection_method = "Mouse (4 clicks)"
    if use_manual_line:
        selection_options = ["Mouse (4 clicks)", "Sliders"]
        if not MOUSE_SELECTION_AVAILABLE:
            st.warning(
                "Mouse selection needs streamlit-image-coordinates. "
                "Install it with: pip install streamlit-image-coordinates"
            )
            selection_options = ["Sliders"]
        selection_method = st.radio(
            "Doorway selection method",
            selection_options,
            index=0,
        )

        if selection_method == "Sliders":
            st.caption("Normalized coordinates (0.0–1.0)")
            a, b = st.columns(2)
            with a:
                x1 = st.slider("Start X", 0.0, 1.0, 0.35, 0.01)
                y1 = st.slider("Start Y", 0.0, 1.0, 0.15, 0.01)
            with b:
                x2 = st.slider("End X", 0.0, 1.0, 0.35, 0.01)
                y2 = st.slider("End Y", 0.0, 1.0, 0.85, 0.01)
            line = (x1, y1, x2, y2)

        direction = st.selectbox(
            "Entry direction",
            ["left_to_right", "right_to_left", "top_to_bottom", "bottom_to_top"],
        )

        st.markdown("---")
        new_profile_name = st.text_input("Save this doorway as a profile", placeholder="e.g. main_hall")
        if st.button("💾 Save doorway profile"):
            line_to_save = line
            quad_to_save = None
            if selection_method == "Mouse (4 clicks)":
                points = st.session_state.get("doorway_mouse_points", [])
                if len(points) == 4:
                    line_to_save = quad_to_line(points)
                    quad_to_save = points
            if line_to_save:
                saved = save_doorway_profile(new_profile_name, line_to_save, direction, quad_to_save)
                if saved:
                    st.success(f"Saved: {saved.name}. Pick it from 'Scene profile' next time.")
                else:
                    st.warning("Enter a profile name first.")
            else:
                st.warning("Select the two doorway points first.")

    st.markdown("---")
    st.markdown("**Detection** · YOLO11s  \n**Tracking** · ByteTrack  \n**Analytics** · DineSight counters")

st.markdown("""
<div class="hero">
 <div class="eyebrow">Computer vision · Restaurant analytics</div>
 <h1 style="margin:.35rem 0">Turn CCTV footage into <span class="hl">insights</span>.</h1>
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

    # Mouse-based doorway selection: click the two ends of the threshold.
    if uploaded and use_manual_line and selection_method == "Mouse (4 clicks)":
        first = get_first_frame(uploaded)
        if first is not None:
            upload_identity = f"{uploaded.name}_{uploaded.size}"
            if st.session_state.get("doorway_upload_identity") != upload_identity:
                st.session_state.doorway_upload_identity = upload_identity
                st.session_state.doorway_mouse_points = []
                st.session_state.doorway_mouse_widget_key = 0

            st.markdown("#### Select the doorway with 4 clicks")
            st.caption(
                "Click the 4 corners of the door (in any order). The app draws the door area "
                "and uses its centre line as the crossing line."
            )

            points = st.session_state.get("doorway_mouse_points", [])
            if len(points) < 4 and MOUSE_SELECTION_AVAILABLE:
                frame_rgb = cv2.cvtColor(first, cv2.COLOR_BGR2RGB)
                max_display_width = 900
                original_h, original_w = frame_rgb.shape[:2]
                display_w = min(original_w, max_display_width)
                display_h = max(1, int(original_h * display_w / original_w))
                resized = cv2.resize(frame_rgb, (display_w, display_h))
                for i, (px, py) in enumerate(points):
                    c = (int(px * display_w), int(py * display_h))
                    cv2.circle(resized, c, 7, (245, 158, 11), -1)
                    cv2.putText(resized, str(i + 1), (c[0] + 9, c[1] - 9),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (245, 158, 11), 2, cv2.LINE_AA)
                clicked = streamlit_image_coordinates(
                    Image.fromarray(resized),
                    width=display_w,
                    key=f"doorway_mouse_click_{st.session_state.get('doorway_mouse_widget_key', 0)}",
                )
                if clicked is not None and clicked.get("x") is not None and clicked.get("y") is not None:
                    nx = min(1.0, max(0.0, float(clicked["x"]) / display_w))
                    ny = min(1.0, max(0.0, float(clicked["y"]) / display_h))
                    st.session_state.doorway_mouse_points = list(points) + [(nx, ny)]
                    st.session_state.doorway_mouse_widget_key = (
                        st.session_state.get("doorway_mouse_widget_key", 0) + 1
                    )
                    if hasattr(st, "rerun"):
                        st.rerun()
                    else:
                        st.experimental_rerun()

            points = st.session_state.get("doorway_mouse_points", [])
            if 0 < len(points) < 4:
                st.info(f"{len(points)} of 4 points selected. Click {4 - len(points)} more.")
            elif len(points) == 4:
                line = quad_to_line(points)
                quad = points
                st.success("Doorway selected. Check the preview and entry arrow below.")
            if points and st.button("↩ Reset the points"):
                st.session_state.doorway_mouse_points = []
                st.session_state.doorway_mouse_widget_key = (
                    st.session_state.get("doorway_mouse_widget_key", 0) + 1
                )
                if hasattr(st, "rerun"):
                    st.rerun()
                else:
                    st.experimental_rerun()
        else:
            st.warning("Could not read a frame from this video for mouse selection.")

    if uploaded and line is not None:
        st.markdown("#### Doorway preview")
        first = get_first_frame(uploaded)
        if first is not None:
            st.image(
                draw_line_preview(first, line, direction, quad),
                caption="Place the line on the door threshold; the arrow must point INTO the room.",
            )
        else:
            st.warning("Could not read a frame from this video for the preview.")
    if uploaded:
        render_scene_editor(uploaded, profile, profile_config)
    st.subheader("02 · Run analysis")
    st.markdown(
        '<div class="small-note">For a new scene, try automatic mode first. If doorway '
        "placement is wrong, use a saved profile or override the line in the sidebar.</div>",
        unsafe_allow_html=True,
    )
    run_clicked = st.button(
        "▶  Analyze video",
        type="primary",
        **STRETCH,
        disabled=uploaded is None
        or (use_manual_line and selection_method == "Mouse (4 clicks)" and line is None),
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
        cleanup_old_runs()
        input_path.write_bytes(uploaded.getbuffer())
        # Tables / uniform drawn in the app but not saved are applied to this run through a temporary profile.
        profile_for_run, temp_profile = profile, None
        ss_run = st.session_state
        if ss_run.get("tables_dirty") or ss_run.get("uniform_dirty"):
            temp_profile = build_run_profile(
                profile_config,
                ss_run.get("drawn_tables") if ss_run.get("tables_dirty") else None,
                ss_run.get("uniform_samples") if ss_run.get("uniform_dirty") else None,
                run_id,
            )
            profile_for_run = str(temp_profile)
        with st.spinner("Processing video… longer videos may take a while."):
            try:
                result, command = run_pipeline(
                    input_path, output_path, report_path, profile_for_run, line, direction
                )
            finally:
                if temp_profile is not None:
                    temp_profile.unlink(missing_ok=True)
            playable_ok, playable_msg = True, ""
            if result.returncode == 0:
                playable_ok, playable_msg = make_browser_playable(output_path)
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
            "playable_ok": playable_ok,
            "playable_msg": playable_msg,
        }
    except Exception as exc:
        st.error(f"Could not start the pipeline: {exc}")

# ----------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------
run = st.session_state.last_run
if run:
    st.markdown("---")
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
        render_quality(report)

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
                if not run.get("playable_ok", True):
                    st.warning(
                        "The video could not be converted for browser playback, so it may not play here. "
                        + run.get("playable_msg", "")
                        + " You can still download it below and open it in VLC."
                    )
                st.video(str(output_path))
                st.download_button(
                    "⬇ Download annotated video",
                    data=output_path.read_bytes(),
                    file_name=f"dinesight_{Path(run['input_name']).stem}_annotated.mp4",
                    mime="video/mp4",
                    **STRETCH,
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
                st.bar_chart(chart_df, color=["#F59E0B", "#2DD4BF"])

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
                    **STRETCH,
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
                st.bar_chart(table_df.set_index("Table")[["Seated", "Capacity"]], color=["#F59E0B", "#2DD4BF"])

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
            cols[2].metric("Net change", fmt_net(report))

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
                    **STRETCH,
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
                        **STRETCH,
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
                **STRETCH,
                key="download_report",
            )
        console_text = (run.get("stdout") or "") + "\n" + (run.get("stderr") or "")
        dl2.download_button(
            "⬇ Console log",
            data=console_text.encode("utf-8"),
            file_name=f"dinesight_log_{Path(run['input_name']).stem}.txt",
            mime="text/plain",
            **STRETCH,
            key="download_log",
        )
        if run.get("command"):
            dl3.download_button(
                "⬇ Command",
                data=run["command"].encode("utf-8"),
                file_name="dinesight_command.txt",
                mime="text/plain",
                **STRETCH,
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

st.markdown("---")
st.caption("DineSight · Local video analytics prototype")