import assert from "node:assert/strict";
import test from "node:test";
import { boxFromPoints, containedRect, imagePoint, overlayRect, pixelBox } from "./crop.ts";

test("finds the letterboxed image inside a wide preview frame", () => {
  // A 600×900 portrait photo shown in a 300×220 frame is centred and 220 px tall.
  const image = containedRect(300, 220, 600, 900);
  assert.equal(image.height, 220);
  assert.ok(Math.abs(image.width - 146.667) < 0.001);
  assert.ok(Math.abs(image.left - 76.667) < 0.001);
  assert.equal(image.top, 0);
});

test("maps a drag in the letterbox margin to the image edge", () => {
  const image = containedRect(300, 220, 600, 900);
  assert.deepEqual(imagePoint(10, 110, image), { x: 0, y: 0.5 });
  const box = boxFromPoints(imagePoint(10, 0, image), imagePoint(150, 110, image));
  assert.ok(box);
  assert.equal(box.x, 0);
  assert.ok(Math.abs(box.width - 0.5) < 0.001);
  assert.deepEqual(pixelBox(box, 600, 900), { sx: 0, sy: 0, sw: 300, sh: 450 });
  const overlay = overlayRect(box, image);
  assert.ok(Math.abs(overlay.left - image.left) < 0.001);
});

test("ignores a click and clamps a crop to the image", () => {
  assert.equal(boxFromPoints({ x: 0.5, y: 0.5 }, { x: 0.51, y: 0.6 }), null);
  assert.deepEqual(pixelBox({ x: 0.9, y: 0.9, width: 0.5, height: 0.5 }, 100, 100), {
    sx: 90,
    sy: 90,
    sw: 10,
    sh: 10,
  });
});
