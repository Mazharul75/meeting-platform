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
