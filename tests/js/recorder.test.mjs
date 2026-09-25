// Unit tests for the recorder engine (run: node --test tests/js).
import assert from "node:assert/strict";
import test from "node:test";

Object.defineProperty(globalThis, "navigator", { value: { onLine: true, userAgent: "Chrome" }, configurable: true });
globalThis.document = { addEventListener() {}, removeEventListener() {}, visibilityState: "visible" };

class FakeRecorder {
  static isTypeSupported() {
    return true;
  }
  constructor(stream, options) {
    this.stream = stream;
    this.options = options;
    this.state = "inactive";
    FakeRecorder.all.push(this);
  }
  start(slice) {
    this.state = "recording";
    this.timer = setInterval(() => this.ondataavailable({ data: new Blob([new Uint8Array(100)]) }), slice);
  }
  stop() {
    clearInterval(this.timer);
    this.state = "inactive";
    this.ondataavailable({ data: new Blob([new Uint8Array(50)]) });
    setTimeout(() => this.onstop(), 1);
  }
}
FakeRecorder.all = [];
globalThis.MediaRecorder = FakeRecorder;

const mod = await import("../../app/static/js/recorder.js");
const { pickMimeType, backoffMs, MemoryStore, UploadQueue, Emitter, Recorder, ApiError, recoverRecording, findUnfinished, isWebm } = mod;

const stream = { getTracks: () => [], getVideoTracks: () => [{}], getAudioTracks: () => [{}] };
const buf = (n) => new Uint8Array(n).buffer;
const nosleep = () => Promise.resolve();

test("format choice: Safari prefers MP4; other browsers prefer WebM so chunks stream for crash safety", () => {
  const safari = (f, a) => pickMimeType(f, a, true);
  assert.equal(safari(() => true), "video/mp4;codecs=avc1.42E01E,mp4a.40.2");
  assert.equal(safari((t) => t === "video/mp4"), "video/mp4");
  assert.equal(safari((t) => t.startsWith("video/webm")), "video/webm;codecs=vp9,opus");
  assert.equal(pickMimeType(() => true, false, false), "video/webm;codecs=vp9,opus");
  assert.equal(pickMimeType((t) => t === "video/webm;codecs=vp8,opus" || t.startsWith("video/mp4"), false, false), "video/webm;codecs=vp8,opus");
  assert.equal(pickMimeType((t) => t === "video/webm", false, false), "video/webm");
  assert.equal(pickMimeType((t) => t.startsWith("video/mp4"), false, false), "video/mp4;codecs=avc1.42E01E,mp4a.40.2");
  assert.equal(pickMimeType(() => false), "");
  assert.equal(pickMimeType((t) => t.startsWith("audio/webm"), true, false), "audio/webm;codecs=opus");
  assert.ok(isWebm("video/webm;codecs=vp9,opus") && !isWebm("video/mp4"));
});

test("retry backoff doubles and is capped at 30 seconds", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 6, 20].map((n) => backoffMs(n)), [1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]);
});

test("memory store keeps ordered chunks per part and lists unfinished parts", async () => {
  const s = new MemoryStore();
  await s.putChunk("r", 1, 1, buf(2));
  await s.putChunk("r", 1, 0, buf(1));
  await s.putChunk("r", 2, 0, buf(5));
  await s.closePart("r", 1, { durationMs: 4000 });
  const p1 = await s.getPart("r", 1);
  assert.deepEqual(p1.chunks.map((c) => c.byteLength), [1, 2]);
  assert.equal(p1.durationMs, 4000);
  const parts = await s.listParts("r");
  assert.deepEqual(parts.map((p) => [p.part, p.closed, p.chunkCount]), [[1, true, 2], [2, false, 1]]);
  await s.deletePart("r", 1);
  assert.equal((await s.getPart("r", 1)).chunks.length, 0);
});

function fakeApi(over = {}) {
  const calls = [];
  const api = {
    calls,
    uploadUrl: async (rid, n) => (calls.push(["url", n]), { url: "u", method: "PUT", body_kind: "raw", headers: {} }),
    complete: async (rid, n, body) => (calls.push(["complete", n, body.size_bytes]), { status: "verified" }),
    finish: async (rid, n) => (calls.push(["finish", n]), { status: "ready", missing: [] }),
    startRecording: async () => ({ recording_id: "rid1", mime_type: "video/webm", part_seconds: 0.3, timeslice_ms: 50, max_part_bytes: 1e9 }),
    note: async () => null,
    ...over,
  };
  return api;
}

const noFix = async (b) => b;

test("upload queue: uploads in order, deletes local chunks only after the server confirms", async () => {
  const store = new MemoryStore();
  await store.putChunk("r", 1, 0, buf(10));
  await store.putChunk("r", 2, 0, buf(20));
  const api = fakeApi();
  const puts = [];
  const q = new UploadQueue({ api, store, emitter: new Emitter(), serverMime: "video/mp4", sleep: nosleep, put: async (up, blob) => puts.push(blob.size) });
  q.enqueue("r", 1, 1000);
  q.enqueue("r", 2, 1000);
  q.enqueue("r", 2, 1000); // duplicate ignored
  await q.idle();
  assert.deepEqual(puts, [10, 20]);
  assert.deepEqual(api.calls.filter((c) => c[0] === "complete").map((c) => c[1]), [1, 2]);
  assert.equal((await store.getPart("r", 1)).chunks.length, 0);
  assert.equal(q.uploaded, 2);
});

test("upload queue: network and server failures are retried and nothing is lost", async () => {
  const store = new MemoryStore();
  await store.putChunk("r", 1, 0, buf(10));
  let failures = 3;
  const api = fakeApi({
    complete: async (rid, n, body) => {
      if (failures-- > 0) throw new ApiError(500);
      return { status: "verified" };
    },
  });
  const em = new Emitter();
  const warnings = [];
  em.on("warning", (w) => warnings.push(w.code));
  let putCount = 0;
  const q = new UploadQueue({ api, store, emitter: em, serverMime: "video/mp4", sleep: nosleep, put: async () => putCount++ });
  q.enqueue("r", 1, 1000);
  // While failing, the chunks must still be on the device.
  await new Promise((r) => setTimeout(r, 5));
  await q.idle();
  assert.equal(putCount, 4);
  assert.ok(warnings.includes("upload_failing"));
  assert.equal((await store.getPart("r", 1)).chunks.length, 0);
});

test("upload queue: chunks stay on the device while uploads keep failing", async () => {
  const store = new MemoryStore();
  await store.putChunk("r", 1, 0, buf(10));
  let calls = 0;
  const api = fakeApi({ uploadUrl: async () => { calls++; throw new TypeError("offline"); } });
  const q = new UploadQueue({ api, store, emitter: new Emitter(), serverMime: "video/mp4", sleep: () => new Promise((r) => setTimeout(r, 2)), put: async () => {} });
  q.enqueue("r", 1, 1000);
  await new Promise((r) => setTimeout(r, 60));
  assert.ok(calls > 3);
  assert.equal((await store.getPart("r", 1)).chunks.length, 1);
  assert.equal(q.uploaded, 0);
});

test("upload queue: a part the server already verified is not uploaded twice", async () => {
  const store = new MemoryStore();
  await store.putChunk("r", 1, 0, buf(10));
  const api = fakeApi({ uploadUrl: async () => ({ alreadyVerified: true }) });
  let put = 0;
  const q = new UploadQueue({ api, store, emitter: new Emitter(), serverMime: "video/mp4", sleep: nosleep, put: async () => put++ });
  q.enqueue("r", 1, 1000);
  await q.idle();
  assert.equal(put, 0);
  assert.equal((await store.getPart("r", 1)).chunks.length, 0);
});

test("upload queue: webm parts get their duration fixed before upload", async () => {
  const store = new MemoryStore();
  await store.putChunk("r", 1, 0, buf(10));
  let fixedWith = null;
  const q = new UploadQueue({
    api: fakeApi(), store, emitter: new Emitter(), serverMime: "video/webm", sleep: nosleep,
    fix: async (b, d) => ((fixedWith = d), b), put: async () => {},
  });
  q.enqueue("r", 1, 4321);
  await q.idle();
  assert.equal(fixedWith, 4321);
});

test("recorder: rotates parts, stores chunks, uploads every part, then finishes", async () => {
  FakeRecorder.all.length = 0;
  const store = new MemoryStore();
  const api = fakeApi();
  const puts = [];
  const rec = new Recorder({ stream, meetingId: "m", api, store, settings: {} });
  const rotations = [];
  rec.on("rotation", (r) => rotations.push(r));
  const origStart = rec.start.bind(rec);
  await origStart(true);
  // speed up: replace the queue's network with the fake
  rec.queue.put = async (up, blob) => puts.push(blob.size);
  rec.queue.fix = noFix;
  rec.queue.sleep = nosleep;
  await new Promise((r) => setTimeout(r, 1000)); // ~3 parts of 0.3 s
  await rec.stop(); // must not resolve until the recording is fully saved - see the test below
  assert.ok(rec.partsStarted >= 3, `parts=${rec.partsStarted}`);
  assert.ok(rotations.length >= 2 && rotations.every((r) => r.gapMs < 500));
  assert.equal(rec.state, "done");
  const finish = api.calls.find((c) => c[0] === "finish");
  assert.equal(finish[1], rec.partsWithData);
  assert.ok(rec.partsWithData >= 3);
  assert.equal(api.calls.filter((c) => c[0] === "complete").length, rec.partsWithData);
  for (let n = 1; n <= rec.partsWithData; n++) assert.equal((await store.getPart("rid1", n)).chunks.length, 0);
  assert.equal((await store.listRecordings()).length, 0);
  assert.equal(FakeRecorder.all[0].options.mimeType.startsWith("video/webm"), true);  // non-Safari user agent
});

test("recorder: a part that reaches the byte limit rotates early", async () => {
  FakeRecorder.all.length = 0;
  const store = new MemoryStore();
  const api = fakeApi({ startRecording: async () => ({ recording_id: "rid2", mime_type: "video/webm", part_seconds: 60, timeslice_ms: 20, max_part_bytes: 250 }) });
  const rec = new Recorder({ stream, meetingId: "m", api, store });
  await rec.start(true);
  rec.queue.put = async () => {};
  rec.queue.fix = noFix;
  rec.queue.sleep = nosleep;
  await new Promise((r) => setTimeout(r, 200));
  assert.ok(rec.partsStarted >= 2, "rotated long before the 60 second timer");
  await rec.stop();
});

test("recorder: stop() does not resolve until upload and finish are fully confirmed", async () => {
  // Regression test: a caller (e.g. the online room, on Leave/End) awaits stop() and then
  // immediately navigates away or disconnects. If stop() resolved early, that navigation could
  // cut the still-in-flight upload short, leaving the recording stuck at status "recording"
  // with 0 bytes forever - exactly what happened before this fix.
  FakeRecorder.all.length = 0;
  const store = new MemoryStore();
  let finishCalls = 0;
  let resolveFinish;
  const finishGate = new Promise((r) => (resolveFinish = r));
  const api = fakeApi({
    finish: async (rid, n) => {
      finishCalls++;
      await finishGate; // finish() only completes once the test lets it
      return { status: "ready", missing: [] };
    },
  });
  const rec = new Recorder({ stream, meetingId: "m", api, store, settings: { partSeconds: 60 } });
  await rec.start(true);
  rec.queue.put = async () => {};
  rec.queue.fix = noFix;
  rec.queue.sleep = nosleep;
  await new Promise((r) => setTimeout(r, 60));

  let stopped = false;
  const stopPromise = rec.stop().then(() => {
    stopped = true;
  });
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(finishCalls, 1, "finish() should already have been called");
  assert.equal(stopped, false, "stop() must still be waiting for finish() to complete");

  resolveFinish();
  await stopPromise;
  assert.equal(stopped, true);
  assert.equal(rec.state, "done");
});

test("recovery after a crash uploads leftover parts and finishes the recording", async () => {
  const store = new MemoryStore();
  await store.putMeta({ recordingId: "rc", meetingId: "m", serverMime: "video/webm", mimeType: "video/webm", partsStarted: 3, timesliceMs: 5000 });
  await store.putChunk("rc", 2, 0, buf(30));
  await store.putChunk("rc", 2, 1, buf(30));
  await store.putChunk("rc", 3, 0, buf(10)); // crashed mid-part: never closed
  await store.closePart("rc", 2, { durationMs: 10000 });
  const unfinished = await findUnfinished(store);
  assert.equal(unfinished.length, 1);
  assert.deepEqual(unfinished[0].parts.map((p) => p.part), [2, 3]);
  const api = fakeApi();
  const done = [];
  const result = await recoverRecording({ store, api, meta: unfinished[0].meta, queueOptions: { put: async () => {}, fix: noFix, sleep: nosleep } });
  assert.equal(result.status, "ready");
  assert.deepEqual(api.calls.filter((c) => c[0] === "complete").map((c) => c[1]), [2, 3]);
  assert.deepEqual(api.calls.find((c) => c[0] === "finish"), ["finish", 3]);
  assert.equal((await findUnfinished(store)).length, 0);
  assert.ok(done);
});

test("recorder: a trailing part that never received data is not counted (no false 'partial')", async () => {
  FakeRecorder.all.length = 0;
  const store = new MemoryStore();
  const api = fakeApi();
  const rec = new Recorder({ stream, meetingId: "m", api, store });
  await rec.start(true);
  rec.queue.put = async () => {};
  rec.queue.fix = noFix;
  rec.queue.sleep = nosleep;
  await new Promise((r) => setTimeout(r, 400)); // one rotation happens at ~300 ms
  const last = rec.current;
  last.rec.stop = function () { clearInterval(this.timer); this.state = "inactive"; setTimeout(() => this.onstop(), 1); };
  last.rec.ondataavailable = () => {};
  last.idx = 0;
  await rec.stop();
  await new Promise((r) => setTimeout(r, 50));
  const finish = api.calls.find((c) => c[0] === "finish");
  assert.equal(finish[1], rec.partsStarted - 1);
});
