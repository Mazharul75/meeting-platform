// Unit tests for the composite grid layout (run: node --test tests/js).
import assert from "node:assert/strict";
import test from "node:test";

const { gridLayout } = await import("../../app/static/js/composite.js");

function overlaps(a, b) {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

test("one participant fills the whole frame", () => {
  const rects = gridLayout(1, 1280, 720);
  assert.deepEqual(rects, [{ x: 0, y: 0, w: 1280, h: 720 }]);
});

test("two participants are side by side", () => {
  const rects = gridLayout(2, 1280, 720);
  assert.equal(rects.length, 2);
  assert.equal(rects[0].y, rects[1].y);
  assert.ok(rects[0].x < rects[1].x);
  assert.equal(rects[0].w, 640);
});

test("three and four participants form a 2x2 grid", () => {
  for (const n of [3, 4]) {
    const rects = gridLayout(n, 1280, 720);
    assert.equal(rects.length, n);
    const rows = new Set(rects.map((r) => r.y));
    assert.equal(rows.size, 2);
  }
});

test("up to nine participants form a 3x3 grid", () => {
  const rects = gridLayout(9, 1280, 720);
  assert.equal(rects.length, 9);
  const cols = new Set(rects.map((r) => r.x));
  assert.equal(cols.size, 3);
});

test("no two tiles ever overlap, for 1 to 9 participants", () => {
  for (let n = 1; n <= 9; n++) {
    const rects = gridLayout(n, 1280, 720);
    for (let i = 0; i < rects.length; i++) {
      for (let j = i + 1; j < rects.length; j++) {
        assert.ok(!overlaps(rects[i], rects[j]), `n=${n} tiles ${i},${j} overlap`);
      }
    }
    for (const r of rects) {
      assert.ok(r.x >= 0 && r.y >= 0 && r.x + r.w <= 1280 && r.y + r.h <= 720, `n=${n} tile out of frame`);
    }
  }
});

test("zero participants draws nothing", () => {
  assert.deepEqual(gridLayout(0), []);
});

const { stageSplit, fitContain } = await import("../../app/static/js/composite.js");

test("stageSplit: stage sits on top, strip fills the remaining height, both full width", () => {
  const { stage, strip } = stageSplit(1280, 720);
  assert.equal(stage.x, 0);
  assert.equal(stage.y, 0);
  assert.equal(stage.w, 1280);
  assert.equal(strip.w, 1280);
  assert.equal(strip.y, stage.h);
  assert.equal(stage.h + strip.h, 720);
  assert.ok(stage.h > strip.h, "the shared screen gets most of the frame");
});

test("fitContain: a wide screen share is letterboxed top/bottom, not cropped or stretched", () => {
  const box = { x: 0, y: 0, w: 1280, h: 533 };
  const r = fitContain(1920, 1080, box); // 16:9 source into a wider-but-shorter box
  assert.ok(r.w <= box.w + 0.01 && r.h <= box.h + 0.01, "fits inside the box");
  assert.ok(Math.abs(r.w / r.h - 1920 / 1080) < 0.01, "keeps the source aspect ratio");
  assert.ok(r.x >= 0 && r.y >= 0, "centred, not pushed outside the box");
});

test("fitContain: a tall source is letterboxed left/right", () => {
  const box = { x: 10, y: 10, w: 1000, h: 1000 };
  const r = fitContain(900, 1800, box); // a portrait phone screen share
  assert.ok(r.h <= box.h + 0.01);
  assert.ok(r.w < box.w, "narrower than the box, centred horizontally");
  assert.ok(Math.abs((r.x - box.x) - (box.x + box.w - (r.x + r.w))) < 1, "horizontally centred");
});

test("fitContain: a missing/zero-sized source falls back to the full box", () => {
  const box = { x: 5, y: 5, w: 200, h: 100 };
  assert.deepEqual(fitContain(0, 0, box), { ...box });
});
