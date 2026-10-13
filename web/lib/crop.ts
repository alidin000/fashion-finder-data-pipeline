// Crop geometry for the photo preview. Boxes are fractions (0–1) of the image.
export type Box = { x: number; y: number; width: number; height: number };
export type Rect = { left: number; top: number; width: number; height: number };
export type Point = { x: number; y: number };

// Smaller drags are treated as a click, not a crop.
const MIN_SIDE = 0.05;

const clamp = (value: number, low = 0, high = 1) => Math.min(high, Math.max(low, value));

/** Where an `object-fit: contain` image actually sits inside its frame. */
export function containedRect(frameWidth: number, frameHeight: number, imageWidth: number, imageHeight: number): Rect {
  if (frameWidth <= 0 || frameHeight <= 0 || imageWidth <= 0 || imageHeight <= 0) {
    return { left: 0, top: 0, width: 0, height: 0 };
  }
  const scale = Math.min(frameWidth / imageWidth, frameHeight / imageHeight);
  const width = imageWidth * scale;
  const height = imageHeight * scale;
  return { left: (frameWidth - width) / 2, top: (frameHeight - height) / 2, width, height };
}

/** A point in frame pixels as a position on the image, clamped to its edges. */
export function imagePoint(x: number, y: number, image: Rect): Point {
  return {
    x: image.width > 0 ? clamp((x - image.left) / image.width) : 0,
    y: image.height > 0 ? clamp((y - image.top) / image.height) : 0,
  };
}

/** The box spanned by two drag points, or null when it is too small to be a crop. */
export function boxFromPoints(start: Point, end: Point): Box | null {
  const width = Math.abs(start.x - end.x);
  const height = Math.abs(start.y - end.y);
  if (width < MIN_SIDE || height < MIN_SIDE) return null;
  return { x: Math.min(start.x, end.x), y: Math.min(start.y, end.y), width, height };
}

/** Whole-pixel source rectangle for `drawImage`, always inside the image. */
export function pixelBox(box: Box, imageWidth: number, imageHeight: number) {
  const sx = Math.min(imageWidth - 1, Math.max(0, Math.floor(box.x * imageWidth)));
  const sy = Math.min(imageHeight - 1, Math.max(0, Math.floor(box.y * imageHeight)));
  const sw = Math.max(1, Math.min(imageWidth - sx, Math.round(box.width * imageWidth)));
  const sh = Math.max(1, Math.min(imageHeight - sy, Math.round(box.height * imageHeight)));
  return { sx, sy, sw, sh };
}

/** The box drawn over the preview, in frame pixels. */
export function overlayRect(box: Box, image: Rect): Rect {
  return {
    left: image.left + box.x * image.width,
    top: image.top + box.y * image.height,
    width: box.width * image.width,
    height: box.height * image.height,
  };
}

/** The same box kept inside the image and no smaller than the minimum side. */
export function fitBox(box: Box): Box {
  const width = clamp(box.width, MIN_SIDE, 1);
  const height = clamp(box.height, MIN_SIDE, 1);
  return { x: clamp(box.x, 0, 1 - width), y: clamp(box.y, 0, 1 - height), width, height };
}

/** Shift a box without changing its size; it stops at the image edge. */
export function moveBox(box: Box, dx: number, dy: number): Box {
  const fitted = fitBox(box);
  return fitBox({ ...fitted, x: fitted.x + dx, y: fitted.y + dy });
}

/** Grow or shrink a box from its top-left corner; it stops at the image edge and the minimum side. */
export function resizeBox(box: Box, dw: number, dh: number): Box {
  const { x, y, width, height } = fitBox(box);
  return { x, y, width: clamp(width + dw, MIN_SIDE, 1 - x), height: clamp(height + dh, MIN_SIDE, 1 - y) };
}

/** One-tap crops for people who cannot, or prefer not to, drag. */
export const CROP_PRESETS = {
  top: { x: 0, y: 0, width: 1, height: 0.5 },
  bottom: { x: 0, y: 0.5, width: 1, height: 0.5 },
  centre: { x: 0.2, y: 0.2, width: 0.6, height: 0.6 },
} satisfies Record<string, Box>;

const ARROWS = new Map<string, [number, number]>([["ArrowLeft", [-1, 0]], ["ArrowRight", [1, 0]], ["ArrowUp", [0, -1]], ["ArrowDown", [0, 1]]]);

/** Arrow keys move the box and Shift+arrows resize it; null for any other key. */
export function keyBox(box: Box, key: string, shift: boolean, step = 0.02): Box | null {
  const direction = ARROWS.get(key);
  if (!direction) return null;
  const [dx, dy] = direction.map((value) => value * step);
  return shift ? resizeBox(box, dx, dy) : moveBox(box, dx, dy);
}

/** Output size with the long side at most `maxSide`; never upscales. */
export function fitSide(width: number, height: number, maxSide: number) {
  const scale = Math.min(1, maxSide / Math.max(width, height));
  return { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)) };
}

/** True when both are null or hold the same numbers, so React state can keep the old object. */
export function sameGeometry<T extends Box | Rect>(a: T | null, b: T | null): boolean {
  if (!a || !b) return a === b;
  return (Object.keys(a) as (keyof T)[]).every((key) => a[key] === b[key]);
}
