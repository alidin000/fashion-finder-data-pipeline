import type { Metadata, Viewport } from "next";
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

const title = "Fashion Finder | Find the look";
const description = "Search current Hungarian fashion offers by style and budget.";

export const metadata: Metadata = {
  // The public demo address from the README; share images need an absolute URL.
  metadataBase: new URL("https://fashion-finder-web.onrender.com"),
  title,
  description,
  applicationName: "Fashion Finder",
  openGraph: { type: "website", siteName: "Fashion Finder", title, description },
  twitter: { card: "summary_large_image", title, description },
};

export const viewport: Viewport = {
  themeColor: "#f8f6f1",
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
