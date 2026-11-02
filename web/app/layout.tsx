import type { Metadata } from "next";
import { Geist } from "next/font/google";
import "./globals.css";

// Downloaded at build time and served from this origin, so the CSP's font-src 'self' holds.
// One variable file; the text styles use 400 to 600 only. No Arial-based metric fallback.
const geist = Geist({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-geist",
  adjustFontFallback: false,
  fallback: ["ui-sans-serif", "system-ui", "sans-serif"],
});

export const metadata: Metadata = {
  title: "Fashion Finder | Find the look",
  description: "Search current Hungarian fashion offers by style and budget.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={geist.variable}>
      <body>
        <a className="skip-link button-sm" href="#main">Skip to content</a>
        {children}
      </body>
    </html>
  );
}
