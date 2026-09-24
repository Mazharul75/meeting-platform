// Recording engine (plan Section 9). Records any MediaStream in ~4 minute parts, keeps a crash-safe
// copy in IndexedDB every 5 seconds, uploads each part straight to storage with a signed link,
// and retries forever. Small pieces: format choice, stores, upload queue, wake lock, Recorder.

export const DEFAULTS = {
  partSeconds: 240,
  timesliceMs: 5000,
  maxPartBytes: 35 * 1024 * 1024,
  videoBitsPerSecond: 800_000,
  audioBitsPerSecond: 64_000,
  retryMaxMs: 30_000,
  lowStorageBytes: 500 * 1024 * 1024,
};

// ---------------------------------------------------------------- format selection (Section 9.2)

export const VIDEO_TYPES = [
  "video/mp4;codecs=avc1.42E01E,mp4a.40.2",
  "video/mp4",
  "video/webm;codecs=vp9,opus",
  "video/webm;codecs=vp8,opus",
  "video/webm",
];
export const AUDIO_TYPES = [
  "audio/mp4;codecs=mp4a.40.2",
  "audio/mp4",
  "audio/webm;codecs=opus",
  "audio/webm",
];

// Chrome/Edge/Firefox only hand over MP4 data when the recording stops (the timeslice is ignored),
// which would remove the crash-safe copy. So there WebM is tried first. Safari can only do MP4.
const WEBM_FIRST = (list) => [...list.filter((t) => t.includes("webm")), ...list.filter((t) => !t.includes("webm"))];

/** First supported type in preference order. MP4 first only where the browser streams MP4 chunks (Safari). */
export function pickMimeType(isSupported, audioOnly = false, mp4First = isSafari()) {
  const base = audioOnly ? AUDIO_TYPES : VIDEO_TYPES;
  const list = mp4First ? base : WEBM_FIRST(base);
  for (const type of list) {
    if (isSupported(type)) return type;
  }
  return "";
}

export function isWebm(mime) {
  return mime.split(";")[0].trim().toLowerCase().endsWith("/webm");
}

export function isSafari() {
  return typeof navigator !== "undefined" && /^((?!chrome|android).)*safari/i.test(navigator.userAgent);
}

export function backoffMs(attempt, max = DEFAULTS.retryMaxMs) {
  return Math.min(max, 1000 * 2 ** Math.max(0, attempt));
}

export function roomConstraints({ audioOnly = false, facingMode = "user", videoDeviceId, audioDeviceId } = {}) {
  // Defaults are built for one person at a laptop and can suppress voices across a room.
  const audio = {
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: true,
    sampleRate: 48000,
    channelCount: 1,
  };
  if (audioDeviceId) audio.deviceId = { exact: audioDeviceId };
  if (audioOnly) return { audio, video: false };
  const video = { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 24 }, facingMode };
  if (videoDeviceId) {
    delete video.facingMode;
    video.deviceId = { exact: videoDeviceId };
  }
  return { audio, video };
}

export async function sha256Hex(blob) {
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

// ------------------------------------------------------------------------------------ emitter

export class Emitter {
  constructor() {
    this.handlers = new Map();
  }
  on(name, fn) {
    if (!this.handlers.has(name)) this.handlers.set(name, new Set());
    this.handlers.get(name).add(fn);
    return () => this.handlers.get(name).delete(fn);
  }
  emit(name, data) {
    for (const fn of this.handlers.get(name) || []) {
      try {
        fn(data);
      } catch (err) {
        console.error(err);
      }
    }
  }
}

// -------------------------------------------------------------------------------------- stores

/** In-memory fallback (also used by tests). Same interface as IdbStore. */
export class MemoryStore {
  constructor() {
    this.meta = new Map();
    this.chunks = new Map(); // "rid|part" -> [ArrayBuffer]
    this.parts = new Map(); // "rid|part" -> {part, durationMs, closed}
    this.persistent = false;
  }
  async putMeta(meta) {
    this.meta.set(meta.recordingId, { ...meta });
  }
  async getMeta(rid) {
    return this.meta.get(rid) || null;
  }
  async deleteMeta(rid) {
    this.meta.delete(rid);
  }
  async listRecordings() {
    return [...this.meta.values()];
  }
  async putChunk(rid, part, idx, buffer) {
    const key = `${rid}|${part}`;
    if (!this.chunks.has(key)) this.chunks.set(key, []);
    this.chunks.get(key)[idx] = buffer;
  }
  async closePart(rid, part, info) {
    this.parts.set(`${rid}|${part}`, { part, closed: true, ...info });
  }
  async getPart(rid, part) {
    const list = (this.chunks.get(`${rid}|${part}`) || []).filter(Boolean);
    const info = this.parts.get(`${rid}|${part}`);
    return { chunks: list, durationMs: info ? info.durationMs : null, closed: !!info };
  }
  async deletePart(rid, part) {
    this.chunks.delete(`${rid}|${part}`);
    this.parts.delete(`${rid}|${part}`);
  }
  async listParts(rid) {
    const out = [];
    for (const [key, list] of this.chunks) {
      if (!key.startsWith(rid + "|")) continue;
      const part = Number(key.split("|")[1]);
      const info = this.parts.get(key);
      out.push({ part, chunkCount: list.filter(Boolean).length, closed: !!info, durationMs: info ? info.durationMs : null });
    }
    return out.sort((a, b) => a.part - b.part);
  }
}

const DB_NAME = "meeting-recorder";

function req(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

/** IndexedDB store: chunks survive a refresh, a crash or a closed tab. */
export class IdbStore {
  constructor(db) {
    this.db = db;
    this.persistent = true;
  }
  static open() {
    return new Promise((resolve, reject) => {
      const open = indexedDB.open(DB_NAME, 1);
      open.onupgradeneeded = () => {
        const db = open.result;
        db.createObjectStore("meta", { keyPath: "recordingId" });
        db.createObjectStore("chunks", { keyPath: ["recordingId", "part", "idx"] });
        db.createObjectStore("parts", { keyPath: ["recordingId", "part"] });
      };
      open.onsuccess = () => resolve(new IdbStore(open.result));
      open.onerror = () => reject(open.error);
    });
  }
  tx(stores, mode = "readonly") {
    return this.db.transaction(stores, mode);
  }
  async putMeta(meta) {
    await req(this.tx(["meta"], "readwrite").objectStore("meta").put(meta));
  }
  async getMeta(rid) {
    return (await req(this.tx(["meta"]).objectStore("meta").get(rid))) || null;
  }
  async deleteMeta(rid) {
    await req(this.tx(["meta"], "readwrite").objectStore("meta").delete(rid));
  }
  async listRecordings() {
    return req(this.tx(["meta"]).objectStore("meta").getAll());
  }
  async putChunk(rid, part, idx, buffer) {
    await req(this.tx(["chunks"], "readwrite").objectStore("chunks").put({ recordingId: rid, part, idx, buffer }));
  }
  async closePart(rid, part, info) {
    await req(this.tx(["parts"], "readwrite").objectStore("parts").put({ recordingId: rid, part, closed: true, ...info }));
  }
  async getPart(rid, part) {
    const range = IDBKeyRange.bound([rid, part, 0], [rid, part, Infinity]);
    const rows = await req(this.tx(["chunks"]).objectStore("chunks").getAll(range));
    const info = await req(this.tx(["parts"]).objectStore("parts").get([rid, part]));
    return { chunks: rows.map((r) => r.buffer), durationMs: info ? info.durationMs : null, closed: !!info };
  }
  async deletePart(rid, part) {
    const t = this.tx(["chunks", "parts"], "readwrite");
    await req(t.objectStore("chunks").delete(IDBKeyRange.bound([rid, part, 0], [rid, part, Infinity])));
    await req(t.objectStore("parts").delete([rid, part]));
  }
  async listParts(rid) {
    const range = IDBKeyRange.bound([rid, 0, 0], [rid, Infinity, Infinity]);
    const keys = await req(this.tx(["chunks"]).objectStore("chunks").getAllKeys(range));
    const counts = new Map();
    for (const [, part] of keys) counts.set(part, (counts.get(part) || 0) + 1);
    const infos = await req(this.tx(["parts"]).objectStore("parts").getAll(IDBKeyRange.bound([rid, 0], [rid, Infinity])));
    const byPart = new Map(infos.map((i) => [i.part, i]));
    return [...counts.entries()]
      .map(([part, chunkCount]) => ({
        part,
        chunkCount,
        closed: byPart.has(part),
        durationMs: byPart.has(part) ? byPart.get(part).durationMs : null,
      }))
      .sort((a, b) => a.part - b.part);
  }
}

export async function createStore(emitter) {
  try {
    if (typeof indexedDB === "undefined") throw new Error("no indexedDB");
    return await IdbStore.open();
  } catch (err) {
    if (emitter) {
      emitter.emit("warning", {
        code: "no_local_copy",
        message: "This browser cannot keep a safety copy. Do not refresh or close this page while recording.",
      });
    }
    return new MemoryStore();
  }
}

export async function localPartBlob(store, rid, part, mime) {
  const { chunks } = await store.getPart(rid, part);
  return new Blob(chunks, { type: mime });
}

// ---------------------------------------------------------------------------------------- API

export class ApiError extends Error {
  constructor(status, code) {
    super(`HTTP ${status}${code ? " " + code : ""}`);
    this.status = status;
    this.code = code;
  }
}

export function createApi(csrfToken) {
  async function call(method, url, body) {
    const res = await fetch(url, {
      method,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": csrfToken },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try {
      data = await res.json();
    } catch (_) {
      /* not JSON */
    }
    if (!res.ok) throw new ApiError(res.status, data && data.error);
    return data;
  }
  return {
    startRecording: (meetingId, mimeType, consent) =>
      call("POST", `/api/meetings/${meetingId}/recordings`, { mime_type: mimeType, consent }),
    async uploadUrl(rid, n) {
      try {
        return await call("POST", `/api/recordings/${rid}/parts/${n}/upload-url`);
      } catch (err) {
        if (err.status === 409 && err.code === "already_verified") return { alreadyVerified: true };
        throw err;
      }
    },
    complete: (rid, n, body) => call("POST", `/api/recordings/${rid}/parts/${n}/complete`, body),
    finish: (rid, partsExpected) => call("POST", `/api/recordings/${rid}/finish`, { parts_expected: partsExpected }),
    note: (rid, code) => call("POST", `/api/recordings/${rid}/note`, { code }).catch(() => null),
  };
}

async function putToStorage(up, blob, mime) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5 * 60 * 1000);
  try {
    let res;
    if (up.body_kind === "multipart") {
      const form = new FormData();
      form.append("cacheControl", "3600");
      form.append("", blob);
      res = await fetch(up.url, { method: up.method, headers: up.headers || {}, body: form, signal: controller.signal });
    } else {
      res = await fetch(up.url, {
        method: up.method,
        headers: { "Content-Type": mime, ...(up.headers || {}) },
        body: blob,
        signal: controller.signal,
      });
    }
    if (!res.ok && res.status !== 409) throw new ApiError(res.status, "storage");
  } finally {
    clearTimeout(timer);
  }
}

export function fixWebmDuration(blob, durationMs) {
  const fixer = globalThis.ysFixWebmDuration;
  if (!fixer) return Promise.resolve(blob);
  return new Promise((resolve) => {
    try {
      fixer(blob, durationMs, (fixed) => resolve(fixed || blob), { logger: false });
    } catch (_) {
      resolve(blob);
    }
  });
}

// --------------------------------------------------------------------------------- upload queue

/** Uploads parts one at a time. Retries forever with backoff; deletes local chunks only after the
 *  server confirms the part is verified. */
export class UploadQueue {
  constructor({ api, store, emitter, serverMime, sleep, fix = fixWebmDuration, put = putToStorage }) {
    this.api = api;
    this.store = store;
    this.emitter = emitter;
    this.serverMime = serverMime;
    this.fix = fix;
    this.put = put;
    this.items = [];
    this.running = false;
    this.uploaded = 0;
    this.failures = 0;
    this.idleWaiters = [];
    this.wake = null;
    this.sleep =
      sleep ||
      ((ms) =>
        new Promise((resolve) => {
          const t = setTimeout(resolve, ms);
          this.wake = () => {
            clearTimeout(t);
            resolve();
          };
        }));
    if (typeof window !== "undefined") window.addEventListener("online", () => this.kick());
  }
  get pending() {
    return this.items.length;
  }
  kick() {
    if (this.wake) this.wake();
  }
  enqueue(rid, part, durationMs) {
    if (this.items.some((i) => i.rid === rid && i.part === part)) return;
    this.items.push({ rid, part, durationMs });
    this.run();
  }
  idle() {
    if (!this.running && this.items.length === 0) return Promise.resolve();
    return new Promise((resolve) => this.idleWaiters.push(resolve));
  }
  async run() {
    if (this.running) return;
    this.running = true;
    let attempt = 0;
    while (this.items.length) {
      const item = this.items[0];
      try {
        await this.uploadOne(item);
        this.items.shift();
        this.uploaded += 1;
        attempt = 0;
        this.failures = 0;
        this.emitter.emit("part_uploaded", { part: item.part, uploaded: this.uploaded });
      } catch (err) {
        this.failures += 1;
        const offline = typeof navigator !== "undefined" && navigator.onLine === false;
        this.emitter.emit("upload_error", { part: item.part, error: err, offline, failures: this.failures });
        if (this.failures >= 3 && !offline) {
          this.emitter.emit("warning", {
            code: "upload_failing",
            part: item.part,
            message: "Uploading is failing. Your recording is safe on this device - use Download to this device if it keeps failing.",
          });
        }
        await this.sleep(backoffMs(attempt));
        attempt += 1;
      }
    }
    this.running = false;
    for (const resolve of this.idleWaiters.splice(0)) resolve();
  }
  async uploadOne(item) {
    const { rid, part } = item;
    const local = await this.store.getPart(rid, part);
    if (!local.chunks.length) return; // already uploaded and cleaned up
    const durationMs = item.durationMs ?? local.durationMs ?? 0;
    if (!item.prepared) {
      let blob = new Blob(local.chunks, { type: this.serverMime });
      if (isWebm(this.serverMime)) blob = await this.fix(blob, durationMs);
      item.prepared = { blob, size: blob.size, sha: await sha256Hex(blob) };
    }
    const { blob, size, sha } = item.prepared;
    const up = await this.api.uploadUrl(rid, part);
    if (!up.alreadyVerified) {
      await this.put(up, blob, this.serverMime);
      await this.api.complete(rid, part, { size_bytes: size, duration_ms: Math.round(durationMs), sha256: sha });
    }
    item.prepared = null;
    await this.store.deletePart(rid, part);
  }
}

// ---------------------------------------------------------------------------------- wake lock

export class WakeLockHelper {
  constructor() {
    this.sentinel = null;
    this.wanted = false;
    this.onVisible = () => {
      if (this.wanted && document.visibilityState === "visible") this.acquire();
    };
  }
  async acquire() {
    if (!("wakeLock" in navigator)) return false;
    try {
      this.sentinel = await navigator.wakeLock.request("screen");
      return true;
    } catch (_) {
      return false;
    }
  }
  async start() {
    this.wanted = true;
    document.addEventListener("visibilitychange", this.onVisible);
    return this.acquire();
  }
  async stop() {
    this.wanted = false;
    document.removeEventListener("visibilitychange", this.onVisible);
    if (this.sentinel) {
      try {
        await this.sentinel.release();
      } catch (_) {
        /* ignore */
      }
      this.sentinel = null;
    }
  }
}

// ------------------------------------------------------------------------------------ recorder

export class Recorder {
  /**
   * @param {object} o
   * @param {MediaStream} o.stream       what to record (camera+mic, or a composite stream)
   * @param {string} o.meetingId
   * @param {object} o.api               createApi(...) or a fake in tests
   * @param {object} [o.store]           MemoryStore / IdbStore
   * @param {object} [o.settings]        overrides for DEFAULTS
   * @param {boolean} [o.audioOnly]
   * @param {() => Promise<MediaStream>} [o.reacquire]  called to get a new stream after a device is lost
   */
  constructor(o) {
    this.stream = o.stream;
    this.meetingId = o.meetingId;
    this.api = o.api;
    this.store = o.store || null;
    this.settings = { ...DEFAULTS, ...(o.settings || {}) };
    this.audioOnly = !!o.audioOnly;
    this.reacquire = o.reacquire || null;
    this.events = new Emitter();
    this.state = "idle"; // idle | recording | stopping | uploading | done | error
    this.rid = null;
    this.current = null;
    this.partsStarted = 0;
    this.partsWithData = 0; // highest part number that actually holds recorded data
    this.startedAt = 0;
    this.rotating = false;
    this.wakeLock = new WakeLockHelper();
    this.statusTimer = null;
    this.finishing = false;
    this.hiddenAt = null;
    this.settingsOverride = o.settingsOverride || null;
  }

  on(name, fn) {
    return this.events.on(name, fn);
  }

  async start(consent) {
    if (typeof MediaRecorder === "undefined") {
      throw new Error("This browser cannot record. Please open the site in Chrome, Edge, Firefox or Safari.");
    }
    if (!this.store) this.store = await createStore(this.events);
    const mime = pickMimeType((t) => MediaRecorder.isTypeSupported(t), this.audioOnly);
    if (!mime) throw new Error("This browser has no supported recording format.");
    await this.checkDevice();
    const started = await this.api.startRecording(this.meetingId, mime, consent);
    this.rid = started.recording_id;
    this.mime = mime;
    this.serverMime = started.mime_type;
    this.settings.partSeconds = started.part_seconds || this.settings.partSeconds;
    this.settings.timesliceMs = started.timeslice_ms || this.settings.timesliceMs;
    this.settings.maxPartBytes = started.max_part_bytes || this.settings.maxPartBytes;
    if (this.settingsOverride) Object.assign(this.settings, this.settingsOverride);
    this.queue = new UploadQueue({
      api: this.api,
      store: this.store,
      emitter: this.events,
      serverMime: this.serverMime,
    });
    this.queue.emitter.on("part_uploaded", () => this.emitStatus());
    this.queue.emitter.on("upload_error", () => this.emitStatus());
    await this.saveMeta(0);
    this.startedAt = performance.now();
    this.state = "recording";
    await this.wakeLock.start();
    this.watchStream(this.stream);
    this.onVisibility = () => this.handleVisibility();
    document.addEventListener("visibilitychange", this.onVisibility);
    this.beginPart(1);
    this.statusTimer = setInterval(() => this.emitStatus(), 1000);
    this.emitStatus();
    return this.rid;
  }

  async checkDevice() {
    try {
      if (navigator.storage && navigator.storage.estimate) {
        const { quota = 0, usage = 0 } = await navigator.storage.estimate();
        if (quota && quota - usage < this.settings.lowStorageBytes) {
          this.events.emit("warning", {
            code: "low_storage",
            message: "This device is low on free space. Consider audio-only mode.",
          });
        }
      }
      if (navigator.getBattery) {
        const b = await navigator.getBattery();
        if (b.level < 0.1 && !b.charging) {
          this.events.emit("warning", { code: "low_battery", message: "Battery is low. Plug in the charger." });
        }
      }
    } catch (_) {
      /* best effort only */
    }
  }

  watchStream(stream) {
    stream.getTracks().forEach((track) => {
      track.addEventListener("ended", () => {
        if (this.state !== "recording") return;
        this.events.emit("warning", {
          code: "device_lost",
          message: "A camera or microphone was disconnected. Recording continues with what is left.",
        });
        this.api.note && this.api.note(this.rid, "device_lost");
        this.tryReacquire();
      });
    });
  }

  async tryReacquire() {
    if (!this.reacquire || this.reacquiring) return;
    this.reacquiring = true;
    while (this.state === "recording") {
      await new Promise((r) => setTimeout(r, 5000));
      try {
        const next = await this.reacquire();
        await this.replaceStream(next);
        this.events.emit("warning", { code: "device_back", message: "The device is connected again." });
        break;
      } catch (_) {
        /* keep trying */
      }
    }
    this.reacquiring = false;
  }

  /** Switch to a new stream: the current part is finished and a new part starts on the new stream. */
  async replaceStream(stream) {
    this.stream = stream;
    this.watchStream(stream);
    await this.rotate();
  }

  handleVisibility() {
    if (this.state !== "recording") return;
    if (document.visibilityState === "hidden") {
      this.hiddenAt = Date.now();
      this.events.emit("warning", {
        code: "hidden",
        message: "Keep this screen on and this tab open. Recording can stop when the screen locks.",
      });
    } else if (this.hiddenAt) {
      const secs = Math.round((Date.now() - this.hiddenAt) / 1000);
      const alive = this.current && this.current.rec.state === "recording";
      this.hiddenAt = null;
      this.events.emit("warning", {
        code: alive ? "returned_ok" : "returned_stopped",
        message: alive
          ? `Welcome back. Recording continued (away ${secs}s).`
          : "Recording stopped while the page was hidden. Data before that is safe. Start a new recording.",
      });
      this.api.note && this.api.note(this.rid, alive ? "tab_hidden" : "tab_hidden_stopped");
    }
  }

  beginPart(n) {
    const options = { mimeType: this.mime, audioBitsPerSecond: this.settings.audioBitsPerSecond };
    if (this.stream.getVideoTracks().length) options.videoBitsPerSecond = this.settings.videoBitsPerSecond;
    const rec = new MediaRecorder(this.stream, options);
    const part = {
      n,
      rec,
      idx: 0,
      bytes: 0,
      writes: Promise.resolve(),
      startedAt: performance.now(),
      timer: null,
      stopped: null,
      stoppedAt: 0,
    };
    part.stopped = new Promise((resolve) => {
      rec.onstop = () => {
        part.stoppedAt = performance.now();
        resolve();
      };
    });
    rec.ondataavailable = (e) => {
      if (!e.data || !e.data.size) return;
      const idx = part.idx++;
      part.bytes += e.data.size;
      part.writes = part.writes.then(async () => {
        try {
          await this.store.putChunk(this.rid, n, idx, await e.data.arrayBuffer());
          if (idx === 0) await this.saveMeta(n); // recovery only expects parts that hold data
        } catch (err) {
          this.events.emit("warning", { code: "local_write_failed", message: "Could not save a safety copy on this device." });
        }
      });
      if (part.bytes >= this.settings.maxPartBytes && this.current === part) this.rotate();
    };
    rec.onerror = (e) => this.events.emit("warning", { code: "recorder_error", message: String((e.error && e.error.message) || "Recorder error") });
    this.current = part;
    this.partsStarted = n;
    rec.start(this.settings.timesliceMs);
    part.timer = setTimeout(() => this.rotate(), this.settings.partSeconds * 1000);
    return part;
  }

  saveMeta(partsWithData) {
    return this.store.putMeta({
      recordingId: this.rid,
      meetingId: this.meetingId,
      mimeType: this.mime,
      serverMime: this.serverMime,
      timesliceMs: this.settings.timesliceMs,
      partsStarted: partsWithData,
      startedAt: Date.now(),
    });
  }

  async finishPart(part) {
    clearTimeout(part.timer);
    if (part.rec.state !== "inactive") part.rec.stop();
    await part.stopped;
    await part.writes;
    if (part.idx === 0) return; // stopped before any data arrived: nothing to upload or expect
    this.partsWithData = Math.max(this.partsWithData, part.n);
    const durationMs = part.stoppedAt - part.startedAt;
    await this.store.closePart(this.rid, part.n, { durationMs, chunkCount: part.idx });
    this.queue.enqueue(this.rid, part.n, durationMs);
  }

  /** Start the next part first, then stop the old one, so no audio is lost between parts. */
  async rotate() {
    if (this.rotating || this.state !== "recording") return;
    this.rotating = true;
    try {
      const old = this.current;
      // Two recorders on one stream overlap slightly (no gap). Safari is safer one after the other.
      const overlap = this.settings.overlapRotation ?? !isSafari();
      if (!overlap) old.rec.stop();
      const next = this.beginPart(old.n + 1);
      await this.finishPart(old);
      this.events.emit("rotation", { part: old.n, gapMs: Math.max(0, next.startedAt - old.stoppedAt) });
      this.emitStatus();
    } finally {
      this.rotating = false;
    }
  }

  async stop() {
    if (this.state !== "recording") return;
    this.state = "stopping";
    clearInterval(this.statusTimer);
    document.removeEventListener("visibilitychange", this.onVisibility);
    while (this.rotating) await new Promise((r) => setTimeout(r, 20));
    await this.finishPart(this.current);
    this.current = null;
    this.state = "uploading";
    this.emitStatus();
    await this.wakeLock.stop();
    this.completeWhenUploaded();
  }

  async completeWhenUploaded() {
    if (this.finishing) return;
    this.finishing = true;
    await this.queue.idle();
    let attempt = 0;
    for (;;) {
      try {
        const result = await this.api.finish(this.rid, this.partsWithData);
        await this.store.deleteMeta(this.rid);
        this.state = "done";
        this.events.emit("finished", result);
        this.emitStatus();
        return;
      } catch (err) {
        if (err.status === 404) {
          this.state = "error";
          this.events.emit("warning", { code: "finish_refused", message: "The server refused to finish this recording." });
          return;
        }
        await new Promise((r) => setTimeout(r, backoffMs(attempt++)));
        await this.queue.idle();
      }
    }
  }

  emitStatus() {
    this.events.emit("status", {
      state: this.state,
      elapsedMs: this.state === "recording" ? performance.now() - this.startedAt : 0,
      partsStarted: this.partsStarted,
      partsUploaded: this.queue ? this.queue.uploaded : 0,
      pending: this.queue ? this.queue.pending : 0,
      online: typeof navigator === "undefined" ? true : navigator.onLine !== false,
    });
  }
}

// ----------------------------------------------------------------------------------- recovery

/** Look for recordings that never finished (refresh, crash, closed tab). */
export async function findUnfinished(store) {
  const metas = await store.listRecordings();
  const out = [];
  for (const meta of metas) {
    const parts = await store.listParts(meta.recordingId);
    if (parts.length || meta.partsStarted) out.push({ meta, parts });
  }
  return out;
}

/** Upload everything left on this device and finish the recording. */
export async function recoverRecording({ store, api, emitter, meta, queueOptions = {} }) {
  const emit = emitter || new Emitter();
  const queue = new UploadQueue({ api, store, emitter: emit, serverMime: meta.serverMime, ...queueOptions });
  const parts = await store.listParts(meta.recordingId);
  for (const p of parts) {
    const dur = p.durationMs ?? p.chunkCount * (meta.timesliceMs || DEFAULTS.timesliceMs);
    queue.enqueue(meta.recordingId, p.part, dur);
  }
  await queue.idle();
  const expected = Math.max(meta.partsStarted || 0, ...parts.map((p) => p.part), 0);
  let attempt = 0;
  for (;;) {
    try {
      const result = await api.finish(meta.recordingId, expected);
      await store.deleteMeta(meta.recordingId);
      emit.emit("finished", result);
      return result;
    } catch (err) {
      if (err.status === 404) throw err;
      await new Promise((r) => setTimeout(r, backoffMs(attempt++)));
    }
  }
}
