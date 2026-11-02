import { ImageResponse } from "next/og";

// The same mark as icon.svg, as the PNG that iOS home screens need. Rendered at build time.
export const size = { width: 180, height: 180 };
export const contentType = "image/png";

// Letter shapes from icon.svg (32 unit grid), scaled to the 180 px tile.
const unit = 180 / 32;
const bars = (left: number) => [
  { left, top: 8, width: 3, height: 16 },
  { left, top: 8, width: 8, height: 3 },
  { left, top: 14.5, width: 7, height: 3 },
];

export default function AppleIcon() {
  return new ImageResponse(
    (
      <div style={{ width: "100%", height: "100%", display: "flex", position: "relative", background: "#152840" }}>
        {[...bars(7).map((bar) => ({ ...bar, color: "#ffffff" })), ...bars(17).map((bar) => ({ ...bar, color: "#ef765f" }))].map((bar, index) => (
          <div key={index} style={{ position: "absolute", left: bar.left * unit, top: bar.top * unit, width: bar.width * unit, height: bar.height * unit, background: bar.color }} />
        ))}
      </div>
    ),
    size,
  );
}
