import assert from "node:assert/strict";
import test from "node:test";
import { boxFromPoints, containedRect, CROP_PRESETS, fitBox, fitSide, imagePoint, keyBox, moveBox, overlayRect, pixelBox, resizeBox, sameGeometry } from "./crop.ts";

const near = (actual: number, expected: number) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} is not ${expected}`);

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

test("moves a crop box but stops it at the image edge without shrinking it", () => {
  const box = { x: 0.7, y: 0.1, width: 0.25, height: 0.4 };
  const moved = moveBox(box, 0.2, -0.3);
  near(moved.x, 0.75);
  assert.equal(moved.y, 0);
  assert.equal(moved.width, 0.25);
  assert.equal(moved.height, 0.4);
  assert.deepEqual(moveBox(box, 0, 0), box);
});

test("resizes a crop box within the image and never below the minimum side", () => {
  const box = { x: 0.6, y: 0.2, width: 0.3, height: 0.3 };
  const grown = resizeBox(box, 0.5, 0.1);
  near(grown.width, 0.4);
  near(grown.height, 0.4);
  assert.equal(grown.x, 0.6);
  const shrunk = resizeBox(box, -1, -1);
  assert.equal(shrunk.width, 0.05);
  assert.equal(shrunk.height, 0.05);
  // A minimum-size box in the far corner can still grow back without leaving the image.
  const corner = resizeBox({ x: 1, y: 1, width: 0.01, height: 0.01 }, 0.1, 0.1);
  near(corner.x, 0.95);
  near(corner.x + corner.width, 1);
});

test("keeps any box inside the image", () => {
  const fitted = fitBox({ x: -0.2, y: 0.9, width: 1.4, height: 0.3 });
  assert.equal(fitted.x, 0);
  assert.equal(fitted.width, 1);
  near(fitted.y, 0.7);
  for (const preset of Object.values(CROP_PRESETS)) assert.deepEqual(fitBox(preset), preset);
});

test("arrow keys move the box, Shift+arrows resize it, other keys do nothing", () => {
  const box = { x: 0.4, y: 0.4, width: 0.2, height: 0.2 };
  near(keyBox(box, "ArrowRight", false)?.x ?? NaN, 0.42);
  near(keyBox(box, "ArrowUp", false)?.y ?? NaN, 0.38);
  const wider = keyBox(box, "ArrowRight", true);
  near(wider?.width ?? NaN, 0.22);
  assert.equal(wider?.x, 0.4);
  near(keyBox(box, "ArrowUp", true)?.height ?? NaN, 0.18);
  assert.equal(keyBox(box, "Enter", false), null);
  assert.equal(keyBox(box, "constructor", true), null);
});

test("downscales the long side to the limit and never upscales", () => {
  assert.deepEqual(fitSide(4032, 3024, 1024), { width: 1024, height: 768 });
  assert.deepEqual(fitSide(3000, 6000, 1024), { width: 512, height: 1024 });
  assert.deepEqual(fitSide(800, 600, 1024), { width: 800, height: 600 });
  assert.deepEqual(fitSide(20000, 10, 1024), { width: 1024, height: 1 });
});

test("compares geometry by value so unchanged state can be kept", () => {
  assert.equal(sameGeometry({ left: 1, top: 2, width: 3, height: 4 }, { left: 1, top: 2, width: 3, height: 4 }), true);
  assert.equal(sameGeometry({ x: 0, y: 0, width: 0.5, height: 0.5 }, { x: 0, y: 0, width: 0.5, height: 0.6 }), false);
  assert.equal(sameGeometry(null, null), true);
  assert.equal(sameGeometry(null, { x: 0, y: 0, width: 1, height: 1 }), false);
});
