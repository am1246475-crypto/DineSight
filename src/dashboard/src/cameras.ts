/**
 * The cameras of the restaurant, in display order. The position in this list is the camera NUMBER shown in the
 * dashboard (1st = camera 1). Add as many as you have: the Camera page lays them out as a grid and any camera
 * can be opened full size.
 *
 *   id      must match the camera_id the CV sends (--camera-id) so events line up with the right camera
 *   name    what the staff see (nameAr = the same in Arabic, optional)
 *   stream  MJPEG address of the processed video (run the CV with --stream-port); leave it out if there is none yet
 *
 * Example of a bigger setup:
 *   { id: "kitchen", name: "Kitchen", stream: "http://192.168.1.20:8084/kitchen" },
 */
export interface CameraDef { id: string; name: string; /** Arabic name shown when the dashboard is in Arabic (falls back to `name`) */ nameAr?: string; stream?: string }

/** The name to show for a camera in the current language */
export const camLabel = (c: CameraDef, lang: string) => (lang === "ar" && c.nameAr ? c.nameAr : c.name);

const env = import.meta.env as Record<string, string | undefined>;

export const cameras: CameraDef[] = [
  { id: "entrance", name: "Entrance", nameAr: "المدخل", stream: env.VITE_STREAM_URL_ENTRANCE || env.VITE_STREAM_URL || undefined },
  { id: "exit", name: "Exit", nameAr: "المخرج", stream: env.VITE_STREAM_URL_EXIT || undefined },
  { id: "wide", name: "Hall", nameAr: "القاعة", stream: env.VITE_STREAM_URL_WIDE || undefined },
];
