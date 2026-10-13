// Crop geometry for the photo preview. Boxes are fractions (0–1) of the image.
export type Box = { x: number; y: number; width: number; height: number };
export type Rect = { left: number; top: number; width: number; height: number };
export type Point = { x: number; y: number };

// Smaller drags are treated as a click, not a crop.
const MIN_SIDE = 0.05;

const clamp = (value: number) => Math.min(1, Math.max(0, value));

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
