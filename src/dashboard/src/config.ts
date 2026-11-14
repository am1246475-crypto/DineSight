export const config = {
  useMock: import.meta.env.VITE_USE_MOCK === "true",
  apiUrl: (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000",
  // Camera frame size and the virtual line position (see cv/counting.py DEFAULT_LINE = x 0.25)
  frame: { w: 768, h: 432 },
  lineX: 0.25,
  cameras: ["entrance", "exit", "wide"],
} as const;
