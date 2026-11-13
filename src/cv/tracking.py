from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import cv2
import numpy as np
import torch
from boxmot import BotSort, BotSortConfig, ReIDConfig
from boxmot.reid import create_reid_encoder
from boxmot.structures import Boxes, Detections, Frame


class Tracker:
    """Stateful BoT-SORT tracker with duplicate suppression and persistent ReID recovery."""

    def __init__(
        self,
        device: Optional[Union[str, int]] = None,
        with_reid: bool = True,
        match_thresh: float = 0.85,
        max_lost_frames: int = 180,
        min_hits: int = 5,
        track_high_thresh: float = 0.45,
        new_track_thresh: float = 0.65,
        det_thresh: float = 0.35,
        iou_threshold: float = 0.2,
        proximity_thresh: float = 0.99,
        appearance_thresh: float = 0.80,
        use_cmc: bool = False,
        nms_iou_thresh: float = 0.55,
        containment_thresh: float = 0.80,
        recovery_max_frames: int = 3600,
        recovery_sim_thresh: float = 0.58,
    ) -> None:
        self.nms_iou_thresh = nms_iou_thresh
        self.containment_thresh = containment_thresh
        self.recovery_max_frames = recovery_max_frames
        self.recovery_sim_thresh = recovery_sim_thresh
        self.with_reid = with_reid

        if device is None:
            resolved_device = "0" if torch.cuda.is_available() else "cpu"
        else:
            resolved_device = str(device)

        config = BotSortConfig(
            match_thresh=match_thresh,
            max_lost_frames=max_lost_frames,
            max_age=max_lost_frames,
            min_hits=min_hits,
            track_high_thresh=track_high_thresh,
            new_track_thresh=new_track_thresh,
            det_thresh=det_thresh,
            iou_threshold=iou_threshold,
            proximity_thresh=proximity_thresh,
            appearance_thresh=appearance_thresh,
            use_cmc=use_cmc,
            use_embeddings=with_reid,
        )

        self._reid_encoder = None
        if with_reid:
            precision = "fp16" if resolved_device != "cpu" else "fp32"
            reid_cfg = ReIDConfig(device=resolved_device, precision=precision)
            self.tracker = BotSort(config=config, reid=reid_cfg)
            self._reid_encoder = create_reid_encoder(reid_cfg)
        else:
            self.tracker = BotSort(config=config)

        # Persistent session gallery for recovering IDs when people leave and re-enter
        self._frame_idx: int = 0
        self._id_remap: Dict[int, int] = {}
        self._seen_raw_ids: set[int] = set()
        self._active_gallery: Dict[int, Dict[str, Any]] = {}
        self._lost_gallery: Dict[int, Dict[str, Any]] = {}

    def _deduplicate(self, dets: np.ndarray) -> np.ndarray:
        """Suppress nested/duplicate RT-DETR boxes (e.g. upper-body + full-body on same person)."""
        if len(dets) <= 1:
            return dets

        order = np.argsort(-dets[:, 4])
        keep: List[int] = []
        areas = (dets[:, 2] - dets[:, 0]) * (dets[:, 3] - dets[:, 1])

        for idx in order:
            box = dets[idx]
            area_i = max(float(areas[idx]), 1e-6)
            is_duplicate = False

            for kept_idx in keep:
                kbox = dets[kept_idx]
                ix1 = max(float(box[0]), float(kbox[0]))
                iy1 = max(float(box[1]), float(kbox[1]))
                ix2 = min(float(box[2]), float(kbox[2]))
                iy2 = min(float(box[3]), float(kbox[3]))

                iw = max(0.0, ix2 - ix1)
                ih = max(0.0, iy2 - iy1)
                inter = iw * ih
                if inter <= 0.0:
                    continue

                area_k = max(float(areas[kept_idx]), 1e-6)
                iou = inter / (area_i + area_k - inter)
                containment = inter / min(area_i, area_k)

                if iou >= self.nms_iou_thresh or containment >= self.containment_thresh:
                    is_duplicate = True
                    break

            if not is_duplicate:
                keep.append(int(idx))

        return dets[keep]

    @staticmethod
    def _extract_hsv_feature(frame: np.ndarray, bbox: List[float]) -> Optional[np.ndarray]:
        """Fallback normalized HS histogram on the central person crop."""
        h_img, w_img = frame.shape[:2]
        x1 = max(0, int(bbox[0]))
        y1 = max(0, int(bbox[1]))
        x2 = min(w_img, int(bbox[2]))
        y2 = min(h_img, int(bbox[3]))
        bw, bh = x2 - x1, y2 - y1
        if bw < 12 or bh < 12:
            return None

        px1, px2 = x1 + int(bw * 0.15), x2 - int(bw * 0.15)
        py1, py2 = y1 + int(bh * 0.10), y2 - int(bh * 0.10)
        crop = frame[py1:py2, px1:px2]
        if crop.size == 0:
            return None

        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256]).flatten()
        norm = np.linalg.norm(hist)
        return (hist / norm).astype(np.float32) if norm > 0 else None

    def _extract_batch_features(
        self, frame: np.ndarray, items: List[Tuple[int, List[float], float]]
    ) -> List[Optional[np.ndarray]]:
        """Extract normalized appearance embeddings (OSNet if enabled, else HSV)."""
        if not items:
            return []

        if self._reid_encoder is not None:
            try:
                rgb_chw = np.ascontiguousarray(frame[:, :, ::-1].transpose(2, 0, 1))
                sample_id = f"cam:{self._frame_idx:06d}"
                frame_obj = Frame(
                    image=torch.from_numpy(rgb_chw),
                    sample_id=sample_id,
                    sequence_id="cam",
                    frame_index=self._frame_idx,
                    timestamp_s=self._frame_idx / 30.0,
                    source_uri="cam",
                )
                boxes_t = torch.tensor([it[1] for it in items], dtype=torch.float32)
                scores_t = torch.tensor([it[2] for it in items], dtype=torch.float32)
                cls_t = torch.zeros(len(items), dtype=torch.int64)
                det_obj = Detections(
                    geometry=Boxes(boxes_t),
                    scores=scores_t,
                    class_ids=cls_t,
                    sample_id=sample_id,
                )
                embs = self._reid_encoder.encode([frame_obj], [det_obj])[0]
                if isinstance(embs, torch.Tensor):
                    embs_np = embs.detach().cpu().numpy().astype(np.float32)
                else:
                    embs_np = np.asarray(embs, dtype=np.float32)
                norms = np.linalg.norm(embs_np, axis=1, keepdims=True)
                embs_np = np.divide(embs_np, np.maximum(norms, 1e-6))
                return [embs_np[i] for i in range(len(items))]
            except Exception:
                pass

        return [self._extract_hsv_feature(frame, it[1]) for it in items]

    @staticmethod
    def _center(bbox: List[float]) -> Tuple[float, float]:
        return (bbox[0] + bbox[2]) * 0.5, (bbox[1] + bbox[3]) * 0.5

    def _try_recover_id(
        self,
        raw_id: int,
        bbox: List[float],
        feat: Optional[np.ndarray],
        current_final_ids: set[int],
    ) -> int:
        """Match a newly created raw_id against lost tracks via local proximity or ReID."""
        if raw_id in self._id_remap:
            return self._id_remap[raw_id]

        if raw_id in self._seen_raw_ids or not self._lost_gallery or feat is None:
            self._seen_raw_ids.add(raw_id)
            return raw_id

        self._seen_raw_ids.add(raw_id)
        cx, cy = self._center(bbox)
        best_old_id: Optional[int] = None
        best_score: float = -1.0

        for old_id, info in self._lost_gallery.items():
            if old_id in current_final_ids:
                continue
            frames_lost = self._frame_idx - info["last_frame"]
            if frames_lost <= 0 or frames_lost > self.recovery_max_frames:
                continue

            sim = float(np.dot(feat, info["feat"]))
            old_cx, old_cy = self._center(info["bbox"])
            dist = float(np.hypot(cx - old_cx, cy - old_cy))

            # Local occlusion recovery (e.g., seated person briefly blocked): relax ReID threshold
            is_local_reappear = dist <= 65.0 and frames_lost <= 200 and sim >= 0.35
            is_global_reid = sim >= self.recovery_sim_thresh

            if is_local_reappear or is_global_reid:
                score = sim + (0.25 if is_local_reappear else 0.0)
                if score > best_score:
                    best_score = score
                    best_old_id = old_id

        if best_old_id is not None:
            self._id_remap[raw_id] = best_old_id
            self._lost_gallery.pop(best_old_id, None)
            return best_old_id

        return raw_id

    @staticmethod
    def _to_numpy(
        detections: Union[np.ndarray, Sequence[Dict[str, Any]]],
    ) -> np.ndarray:
        """Convert detection.py output ([{'bbox': [x1,y1,x2,y2], 'conf': c}]) to (N, 6) array."""
        if isinstance(detections, np.ndarray):
            if detections.size == 0:
                return np.empty((0, 6), dtype=np.float32)
            return detections.astype(np.float32)

        if not detections:
            return np.empty((0, 6), dtype=np.float32)

        rows: List[List[float]] = []
        for det in detections:
            bbox = det.get("bbox")
            if bbox is None or len(bbox) != 4:
                continue
            conf = float(det.get("conf", det.get("confidence", 1.0)))
            cls_id = float(det.get("class_id", det.get("cls", 0)))
            rows.append(
                [float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]), conf, cls_id]
            )

        if not rows:
            return np.empty((0, 6), dtype=np.float32)

        return np.array(rows, dtype=np.float32)

    def update(
        self,
        detections: Union[np.ndarray, Sequence[Dict[str, Any]]],
        frame: np.ndarray,
    ) -> List[Dict[str, Any]]:
        """Update tracker state for the current frame and return tracked person dicts."""
        self._frame_idx += 1
        dets_np = self._deduplicate(self._to_numpy(detections))

        # Always call update even when dets_np is empty so BoT-SORT ages lost tracks
        tracks = self.tracker.update(dets_np, frame)

        results: List[Dict[str, Any]] = []
        current_final_ids: set[int] = set()

        if tracks is not None and len(tracks) > 0:
            raw_items: List[Tuple[int, List[float], float]] = []
            for track_row in tracks:
                x1, y1, x2, y2, raw_id, conf = track_row[:6]
                rid = int(raw_id)
                bbox = [
                    round(float(x1), 1),
                    round(float(y1), 1),
                    round(float(x2), 1),
                    round(float(y2), 1),
                ]
                raw_items.append((rid, bbox, round(float(conf), 3)))
                if rid in self._id_remap:
                    current_final_ids.add(self._id_remap[rid])
                elif rid in self._seen_raw_ids:
                    current_final_ids.add(rid)

            feats = self._extract_batch_features(frame, raw_items)

            for (rid, bbox, conf), feat in zip(raw_items, feats):
                final_id = self._try_recover_id(rid, bbox, feat, current_final_ids)
                current_final_ids.add(final_id)
                self._lost_gallery.pop(final_id, None)

                # Update EMA appearance feature and last-known bbox
                if feat is not None and conf >= 0.45:
                    prev = self._active_gallery.get(final_id)
                    if prev is not None:
                        blended = 0.85 * prev["feat"] + 0.15 * feat
                        norm = np.linalg.norm(blended)
                        ema_feat = (blended / norm).astype(np.float32) if norm > 0 else feat
                    else:
                        ema_feat = feat

                    self._active_gallery[final_id] = {
                        "feat": ema_feat,
                        "bbox": bbox,
                        "last_frame": self._frame_idx,
                    }
                elif final_id in self._active_gallery:
                    self._active_gallery[final_id]["bbox"] = bbox
                    self._active_gallery[final_id]["last_frame"] = self._frame_idx

                results.append(
                    {
                        "track_id": final_id,
                        "bbox": bbox,
                        "conf": conf,
                    }
                )

        # Move tracks that disappeared in this frame into _lost_gallery
        disappeared = [tid for tid in self._active_gallery if tid not in current_final_ids]
        for tid in disappeared:
            self._lost_gallery[tid] = self._active_gallery.pop(tid)

        # Prune expired entries from _lost_gallery
        expired = [
            tid
            for tid, info in self._lost_gallery.items()
            if (self._frame_idx - info["last_frame"]) > self.recovery_max_frames
        ]
        for tid in expired:
            self._lost_gallery.pop(tid, None)

        return results


# Alias in case callers import `track` directly
track = Tracker