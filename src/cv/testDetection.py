# cv/test_detection.py
import cv2, json, time, sys
from detection import detect, load_model

src = sys.argv[1] if len(sys.argv) > 1 else "test.mp4" 
cap = cv2.VideoCapture(src)
load_model()

dump, n, t0 = [], 0, time.time()
while True:
    ok, frame = cap.read()
    if not ok:
        break
    dets = detect(frame)
    dump.append({"frame": n, "detections": dets})
    for d in dets:
        x1, y1, x2, y2 = map(int, d["bbox"])
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(frame, f'{d["conf"]:.2f}', (x1, y1 - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    n += 1
    fps = n / (time.time() - t0)
    cv2.putText(frame, f"FPS {fps:.1f}", (10, 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    cv2.imshow("detection", frame)
    if cv2.waitKey(1) == 27:   # ESC
        break

fps = cap.get(cv2.CAP_PROP_FPS)
w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
json.dump({"meta": {"fps": fps, "width": w, "height": h}, "frames": dump},
          open("detections.json", "w"))
print(f"frames={n}  avg FPS={n/(time.time()-t0):.1f}")