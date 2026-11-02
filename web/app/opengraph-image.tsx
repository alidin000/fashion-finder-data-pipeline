import { ImageResponse } from "next/og";

// Rendered once at build time. next/og draws text in its bundled Geist Regular, the site's typeface.
export const alt = "Fashion Finder: find the piece, make it yours. Search current Hungarian fashion offers by style and budget.";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function OpengraphImage() {
  return new ImageResponse(
    (
      <div style={{ width: "100%", height: "100%", display: "flex", flexDirection: "column", justifyContent: "space-between", padding: 80, background: "#f8f6f1", color: "#152840" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 24 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center", width: 96, height: 96, borderRadius: 24, background: "#152840", color: "#ffffff", fontSize: 48 }}>
            F<span style={{ color: "#ef765f" }}>F</span>
          </div>
          <div style={{ fontSize: 30, letterSpacing: 4 }}>FASHION FINDER</div>
        </div>
        <div style={{ display: "flex", flexDirection: "column" }}>
          <div style={{ fontSize: 96, lineHeight: 1, letterSpacing: -3 }}>Find the piece.</div>
          <div style={{ fontSize: 96, lineHeight: 1, letterSpacing: -3, color: "#586776", marginTop: 8 }}>Make it yours.</div>
          <div style={{ fontSize: 30, color: "#586776", marginTop: 40 }}>Search current Hungarian fashion offers by style and budget.</div>
        </div>
      </div>
    ),
    size,
  );
}
