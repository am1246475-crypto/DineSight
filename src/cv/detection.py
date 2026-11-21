# cv/detection.py
import torch
from ultralytics import RTDETR

_DEVICE = 0 if torch.cuda.is_available() else "cpu"
_model = None

def load_model(weights="rtdetr-l.pt"):
    global _model
    _model = RTDETR(weights)
    return _model

def detect(frame, conf=0.4, imgsz=640, classes=None):
    if _model is None:
        load_model()
    h, w = frame.shape[:2]          # frame size, needed for clipping
    target_classes = [0] if classes is None else list(classes)
    res = _model.predict(
        frame,
        classes=target_classes,
        conf=conf,
        imgsz=imgsz,
        device=_DEVICE,
        verbose=False,
    )[0]
    out = []
    classes_list = res.boxes.cls.tolist() if res.boxes.cls is not None else [target_classes[0]] * len(res.boxes)
    for box, c, cls_id in zip(res.boxes.xyxy.tolist(), res.boxes.conf.tolist(), classes_list):
        x1, y1 = max(0.0, box[0]), max(0.0, box[1])
        x2, y2 = min(float(w), box[2]), min(float(h), box[3])
        out.append({"bbox": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                    "conf": round(c, 3),
                    "class_id": int(cls_id)})
    return out
