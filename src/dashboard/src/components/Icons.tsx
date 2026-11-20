import type { ReactNode } from "react";

const Svg = ({ children, size = 16 }: { children: ReactNode; size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
       strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>
);
export const IconIn = () => <Svg><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4" /><path d="M10 17l5-5-5-5" /><path d="M15 12H3" /></Svg>;
export const IconOut = () => <Svg><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="M16 17l5-5-5-5" /><path d="M21 12H9" /></Svg>;
export const IconPeak = () => <Svg><path d="M3 17l6-6 4 4 8-8" /><path d="M15 7h6v6" /></Svg>;
export const IconClock = () => <Svg><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></Svg>;
export const IconRefresh = () => <Svg><path d="M21 12a9 9 0 1 1-3-6.7" /><path d="M21 4v5h-5" /></Svg>;
export const IconDownload = () => <Svg><path d="M12 3v12" /><path d="M7 10l5 5 5-5" /><path d="M4 21h16" /></Svg>;
export const IconUsers = () => <Svg><circle cx="9" cy="8" r="3.5" /><path d="M2.5 20a6.5 6.5 0 0 1 13 0" /><path d="M16 4.5a3.5 3.5 0 0 1 0 7" /><path d="M18 14a6 6 0 0 1 3.5 6" /></Svg>;
export const IconCam = () => <Svg><rect x="3" y="6" width="13" height="12" rx="2" /><path d="M16 10l5-3v10l-5-3" /></Svg>;
export const IconTable = () => <Svg><rect x="4" y="9" width="16" height="4" rx="1" /><path d="M7 13v7M17 13v7" /></Svg>;
export const IconAlert = () => <Svg size={18}><path d="M12 3l10 18H2L12 3z" /><path d="M12 10v5M12 18h.01" /></Svg>;

export const Logo = () => (
  <svg className="logo" viewBox="0 0 40 40" aria-hidden="true">
    <defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#5AA9FF" /><stop offset="1" stopColor="#34D3B0" /></linearGradient></defs>
    <rect width="40" height="40" rx="11" fill="url(#lg)" />
    <circle cx="20" cy="20" r="9" fill="none" stroke="#fff" strokeWidth="2.4" />
    <circle cx="20" cy="20" r="3.6" fill="#fff" />
  </svg>
);
