// Developer test page for the recorder engine. Works without a meeting (parts stay on the device
// and can be downloaded) or with ?meeting=<id> (parts upload for real).
import {
  Recorder,
  createApi,
  createStore,
  findUnfinished,
  localPartBlob,
  recoverRecording,
  roomConstraints,
} from "/static/js/recorder.js";

const $ = (id) => document.getElementById(id);
const config = JSON.parse($("recorder-config").textContent);
const params = new URLSearchParams(location.search);
const csrf = document.querySelector('meta[name="csrf-token"]').content;

const state = { recorder: null, stream: null, store: null };
window.__recorderTest = state; // handy for automated tests

// A fake API is used when no meeting is chosen, so the engine can be exercised offline.
function fakeApi() {
  let rid = "local-" + Math.random().toString(16).slice(2, 10);
  return {
    startRecording: async (_m, mime) => ({
      recording_id: rid,
      mime_type: mime.startsWith("video/webm") || mime.startsWith("audio/webm") ? "video/webm" : "video/mp4",
      part_seconds: config.partSeconds,
      timeslice_ms: config.timesliceMs,
      max_part_bytes: config.maxPartBytes,
    }),
    uploadUrl: async () => {
      throw new Error("local test: no upload");
    },
    complete: async () => {},
    finish: async () => ({ status: "local" }),
    note: async () => null,
  };
}

async function refreshLocalParts() {
  const list = $("local-parts");
  list.replaceChildren();
  const store = state.store || (state.store = await createStore());
  for (const { meta, parts } of await findUnfinished(store)) {
    for (const p of parts) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.textContent = `Download part ${p.part} (${p.chunkCount} chunks)`;
      a.href = "#";
      a.addEventListener("click", async (e) => {
        e.preventDefault();
        const blob = await localPartBlob(store, meta.recordingId, p.part, meta.serverMime || meta.mimeType);
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = `part-${String(p.part).padStart(4, "0")}.${blob.type.includes("mp4") ? "mp4" : "webm"}`;
        link.click();
        setTimeout(() => URL.revokeObjectURL(link.href), 10000);
      });
      li.appendChild(a);
      list.appendChild(li);
    }
  }
}

async function showRecoverBanner() {
  const store = state.store || (state.store = await createStore());
  const found = await findUnfinished(store);
  const banner = $("recover");
  if (!found.length || !config.meetingId) {
    banner.classList.add("d-none");
    return;
  }
  const total = found.reduce((n, f) => n + f.parts.length, 0);
  $("recover-text").textContent = `An unfinished recording was found on this device (${total} part${total === 1 ? "" : "s"}).`;
  banner.classList.remove("d-none");
  $("btn-recover").onclick = async () => {
    $("recover-text").textContent = "Uploading the saved parts...";
    const api = createApi(csrf);
    for (const f of found) await recoverRecording({ store, api, meta: f.meta });
    banner.classList.add("d-none");
    $("st-line").textContent = "Recovered and finished.";
    refreshLocalParts();
  };
}

function bind(recorder) {
  recorder.on("status", (s) => {
    const secs = Math.floor(s.elapsedMs / 1000);
    const hh = String(Math.floor(secs / 3600)).padStart(2, "0");
    const mm = String(Math.floor((secs % 3600) / 60)).padStart(2, "0");
    const ss = String(secs % 60).padStart(2, "0");
    $("st-line").textContent = s.state === "recording" ? `Recording ${hh}:${mm}:${ss}` : `State: ${s.state}`;
    $("st-parts").textContent = `Parts uploaded ${s.partsUploaded}/${s.partsStarted}`;
    $("st-net").textContent = s.online ? "Network OK" : "Offline - will upload automatically";
  });
  recorder.on("warning", (w) => {
    $("st-net").textContent = w.message;
  });
  recorder.on("rotation", () => refreshLocalParts());
  recorder.on("finished", () => refreshLocalParts());
}

async function start() {
  const audioOnly = $("audio-only").checked;
  try {
    state.stream = await navigator.mediaDevices.getUserMedia(roomConstraints({ audioOnly }));
  } catch (err) {
    $("st-line").textContent = "Could not open the camera or microphone: " + err.name;
    return;
  }
  $("preview").srcObject = state.stream;
  const settingsOverride = {};
  if (params.get("part")) settingsOverride.partSeconds = Number(params.get("part"));
  if (params.get("slice")) settingsOverride.timesliceMs = Number(params.get("slice"));
  const recorder = new Recorder({
    stream: state.stream,
    meetingId: config.meetingId,
    api: config.meetingId ? createApi(csrf) : fakeApi(),
    store: state.store || undefined,
    audioOnly,
    settingsOverride,
    reacquire: () => navigator.mediaDevices.getUserMedia(roomConstraints({ audioOnly })),
  });
  state.recorder = recorder;
  bind(recorder);
  try {
    await recorder.start($("consent").checked);
  } catch (err) {
    $("st-line").textContent = err.message || String(err);
    return;
  }
  $("btn-start").disabled = true;
  $("btn-stop").disabled = false;
  $("keep-awake").classList.remove("d-none");
}

$("btn-start").addEventListener("click", start);
$("btn-stop").addEventListener("click", async () => {
  await state.recorder.stop();
  $("btn-stop").disabled = true;
  $("btn-start").disabled = false;
  $("keep-awake").classList.add("d-none");
  if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
  refreshLocalParts();
});

showRecoverBanner();
refreshLocalParts();
