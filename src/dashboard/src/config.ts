import { cameras } from "./cameras";

export const config = {
  useMock: import.meta.env.VITE_USE_MOCK === "true",
  apiUrl: (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000",
  // Camera frame size and the virtual line position (see cv/counting.py DEFAULT_LINE = x 0.25)
  frame: { w: 768, h: 432 },
  lineX: 0.25,
  // Table polygons are normalized 0..1 of this frame, so the map must keep its aspect ratio (768x432 = 16:9)
  // Optional processed stream (MJPEG). When empty, the page plays the sample video instead.
  streamUrl: (import.meta.env.VITE_STREAM_URL as string | undefined) || undefined,
  videoFile: "/case.mp4",
  // The list of cameras (and their numbers) lives in src/cameras.ts
  cameras,
} as const;
