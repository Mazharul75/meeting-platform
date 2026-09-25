// Online meeting room (plan Section 10). Connects to LiveKit with end-to-end encryption,
// shows a tile per participant, and lets the host record the whole call with composite.js.
import { buildComposite } from "/static/js/composite.js";
import { Recorder, createApi, createStore, findUnfinished, recoverRecording } from "/static/js/recorder.js";

const LK = window.LivekitClient;
const $ = (id) => document.getElementById(id);

function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]').content;
}

async function fetchJson(url, opts = {}) {
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": csrfToken() },
    ...opts,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

class RoomController {
  constructor({ tokenUrl, participantsUrl, endUrl, isHost, meetingId, statusEl }) {
    this.tokenUrl = tokenUrl;
    this.participantsUrl = participantsUrl;
    this.endUrl = endUrl;
    this.isHost = isHost;
    this.meetingId = meetingId;
    this.statusEl = statusEl;
    this.names = new Map();
    this.room = null;
    this.recorder = null;
    this.recording = false;
  }

  setStatus(text) {
    if (this.statusEl) this.statusEl.textContent = text;
  }

  async connect() {
    const { url, token, e2ee_key: key, identity, display_name: name } = await fetchJson(this.tokenUrl);
    this.names.set(identity, name);
    const keyProvider = new LK.ExternalE2EEKeyProvider();
    await keyProvider.setKey(key);
    const worker = new Worker("/static/vendor/livekit-client.e2ee.worker.js");
    this.room = new LK.Room({
      adaptiveStream: true,
      dynacast: true,
      e2ee: { keyProvider, worker },
    });
    this.bindEvents();
    this.pollNames();
    this.setStatus("Connecting...");
    await this.room.connect(url, token);
    await this.room.localParticipant.setCameraEnabled(true);
    await this.room.localParticipant.setMicrophoneEnabled(true);
    this.addTile(this.room.localParticipant, true);
    this.setStatus("Connected");
  }

  bindEvents() {
    const { RoomEvent } = LK;
    this.room.on(RoomEvent.ParticipantConnected, (p) => this.addTile(p, false));
    this.room.on(RoomEvent.ParticipantDisconnected, (p) => this.removeTile(p));
    this.room.on(RoomEvent.TrackSubscribed, (track, pub, p) => this.attachTrack(track, p));
    this.room.on(RoomEvent.TrackUnsubscribed, (track, pub, p) => {
      track.detach().forEach((el) => el.remove());
      if (track.kind === "video") {
        document.getElementById("tile-" + p.identity)?.querySelector(".room-tile-avatar")?.classList.remove("d-none");
      }
    });
    this.room.on(RoomEvent.LocalTrackPublished, (pub, p) => {
      if (pub.track) this.attachTrack(pub.track, p);
    });
    this.room.on(RoomEvent.TrackMuted, (pub, p) => this.updateChips(p));
    this.room.on(RoomEvent.TrackUnmuted, (pub, p) => this.updateChips(p));
    this.room.on(RoomEvent.Reconnecting, () => this.setStatus("Reconnecting..."));
    this.room.on(RoomEvent.Reconnected, () => this.setStatus("Connected"));
    this.room.on(RoomEvent.Disconnected, (reason) => {
      this.setStatus(reason === LK.DisconnectReason.SERVER_SHUTDOWN ? "The meeting was ended" : "Disconnected");
      $("tiles").replaceChildren();
      if (window.__roomTest) window.__roomTest.disconnected = true;
    });
  }

  initials(name) {
    return (name || "?").trim().split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
  }

  tileFor(identity) {
    let tile = document.getElementById("tile-" + identity);
    if (!tile) {
      tile = document.createElement("div");
      tile.className = "room-tile";
      tile.id = "tile-" + identity;
      tile.innerHTML =
        '<span class="room-tile-avatar"></span>' +
        '<span class="room-tile-chips"></span>' +
        '<span class="room-tile-name"></span>';
      $("tiles").appendChild(tile);
      this.updateCount();
    }
    return tile;
  }

  updateCount() {
    const n = $("tiles").children.length;
    const el = $("room-count");
    if (el) el.textContent = `${n} in the meeting`;
  }

  addTile(participant, isLocal) {
    const tile = this.tileFor(participant.identity);
    tile.dataset.identity = participant.identity;
    const name = (isLocal ? "You" : this.names.get(participant.identity)) || "Connecting…";
    tile.querySelector(".room-tile-name").textContent = isLocal ? `${name} (Host)` : name;
    tile.querySelector(".room-tile-avatar").textContent = this.initials(isLocal ? "You" : name);
  }

  updateChips(participant) {
    const tile = document.getElementById("tile-" + participant.identity);
    const chips = tile?.querySelector(".room-tile-chips");
    if (!chips) return;
    const bits = [];
    if (!participant.isMicrophoneEnabled) bits.push("Mic off");
    if (!participant.isCameraEnabled) bits.push("Cam off");
    chips.replaceChildren(
      ...bits.map((text) => {
        const span = document.createElement("span");
        span.className = "chip";
        span.textContent = text;
        return span;
      }),
    );
  }

  removeTile(participant) {
    document.getElementById("tile-" + participant.identity)?.remove();
    this.updateCount();
  }

  attachTrack(track, participant) {
    const tile = this.tileFor(participant.identity);
    const el = track.attach();
    if (track.kind === "video") {
      tile.querySelector("video")?.remove();
      el.autoplay = true;
      el.playsInline = true;
      tile.prepend(el);
      tile.querySelector(".room-tile-avatar")?.classList.add("d-none");
    }
    // Audio elements need no DOM placement: track.attach() already plays them once attached.
  }

  async pollNames() {
    try {
      const res = await fetch(this.participantsUrl, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (res.ok) {
        const data = await res.json();
        for (const [id, name] of Object.entries(data.participants)) {
          this.names.set(id, name);
          const el = document.querySelector(`#tile-${id} .room-tile-name`);
          if (el && id !== this.room?.localParticipant.identity) el.textContent = name;
        }
      }
    } catch (_) {
      /* best effort */
    }
    this._namesTimer = setTimeout(() => this.pollNames(), 4000);
  }

  toggleMic() {
    const enabled = this.room.localParticipant.isMicrophoneEnabled;
    this.room.localParticipant.setMicrophoneEnabled(!enabled);
    return !enabled;
  }

  toggleCamera() {
    const enabled = this.room.localParticipant.isCameraEnabled;
    this.room.localParticipant.setCameraEnabled(!enabled);
    return !enabled;
  }

  async leave() {
    clearTimeout(this._namesTimer);
    if (this.recording) await this.stopRecording();
    await this.room?.disconnect();
  }

  async endForEveryone() {
    if (this.recording) await this.stopRecording();
    await fetchJson(this.endUrl);
  }

  async startRecording(consent) {
    const composite = buildComposite(this.room);
    this.recorder = new Recorder({
      stream: composite.stream,
      meetingId: this.meetingId,
      api: createApi(csrfToken()),
      store: await createStore(),
    });
    this.compositeStop = composite.stop;
    this.recorder.on("status", (s) => this.onRecorderStatus(s));
    this.recorder.on("warning", (w) => this.setStatus(w.message));
    this.blockUnload = (e) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", this.blockUnload);
    await this.recorder.start(consent);
    this.recording = true;
  }

  onRecorderStatus(s) {
    if (s.state === "uploading") {
      this.setStatus(`Saving the recording (${s.partsUploaded}/${s.partsStarted} parts) - keep this tab open…`);
    } else if (s.state === "done") {
      this.setStatus("Connected");
    }
  }

  /** Resolves only once the recording is fully uploaded and confirmed by the server, so it is
   *  safe to disconnect or navigate away right after this returns (plan AC-06 / AC-07). */
  async stopRecording() {
    if (!this.recorder) return;
    this.setStatus("Saving the recording - keep this tab open…");
    try {
      await this.recorder.stop();
    } finally {
      this.compositeStop?.();
      this.recording = false;
      if (this.blockUnload) window.removeEventListener("beforeunload", this.blockUnload);
    }
  }

  /** Uploads anything left on this device from a recording that never finished last time -
   *  a refresh, a crash or a closed tab right after "Stop recording" (plan AC-07). */
  async recoverLeftoverRecording() {
    try {
      const store = await createStore();
      const found = await findUnfinished(store);
      const mine = found.filter((f) => f.meta.meetingId === this.meetingId);
      if (!mine.length) return;
      this.setStatus("Finishing a recording left over from last time…");
      const api = createApi(csrfToken());
      for (const f of mine) await recoverRecording({ store, api, meta: f.meta });
      this.setStatus("Connected");
    } catch (_) {
      /* best effort: nothing local to lose if this fails, the parts stay in IndexedDB */
    }
  }
}

function bindControls(controller) {
  $("btn-mic").addEventListener("click", () => {
    const on = controller.toggleMic();
    $("btn-mic").querySelector(".lbl").textContent = on ? "Mute" : "Unmute";
    $("mic-icon").setAttribute("href", on ? "#i-mic" : "#i-mic-off");
    $("btn-mic").setAttribute("aria-pressed", String(!on));
    controller.updateChips(controller.room.localParticipant);
  });
  $("btn-cam").addEventListener("click", () => {
    const on = controller.toggleCamera();
    $("btn-cam").querySelector(".lbl").textContent = on ? "Camera off" : "Camera on";
    $("cam-icon").setAttribute("href", on ? "#i-cam" : "#i-cam-off");
    $("btn-cam").setAttribute("aria-pressed", String(!on));
    controller.updateChips(controller.room.localParticipant);
  });
  $("btn-leave").addEventListener("click", async () => {
    $("btn-leave").disabled = true;
    if (controller.recording) $("btn-leave").querySelector(".lbl")?.replaceChildren("Saving…");
    await controller.leave();
    window.location.href = "/";
  });
  const endBtn = $("btn-end");
  if (endBtn) {
    endBtn.addEventListener("click", async () => {
      if (!window.confirm("End this meeting for everyone?")) return;
      endBtn.disabled = true;
      await controller.endForEveryone();
      endBtn.disabled = false;
    });
  }
  const recBtn = $("btn-record");
  if (recBtn) {
    recBtn.addEventListener("click", async () => {
      if (!controller.recording) {
        if (!$("consent").checked) {
          $("consent-error").classList.remove("d-none");
          return;
        }
        recBtn.disabled = true;
        await controller.startRecording(true);
        recBtn.disabled = false;
        recBtn.querySelector(".lbl").textContent = "Stop recording";
        $("rec-badge").classList.remove("d-none");
      } else {
        recBtn.disabled = true;
        await controller.stopRecording();
        recBtn.disabled = false;
        recBtn.querySelector(".lbl").textContent = "Start recording";
        $("rec-badge").classList.add("d-none");
      }
    });
  }
}

async function init() {
  const cfgEl = $("room-config");
  if (!cfgEl || !LK) return;
  const cfg = JSON.parse(cfgEl.textContent);
  const controller = new RoomController({
    tokenUrl: cfg.tokenUrl,
    participantsUrl: cfg.participantsUrl,
    endUrl: cfg.endUrl,
    isHost: cfg.isHost,
    meetingId: cfg.meetingId,
    statusEl: $("room-status"),
  });
  window.__roomTest = controller;
  bindControls(controller);
  try {
    await controller.connect();
  } catch (err) {
    controller.setStatus("Could not join: " + err.message);
    return;
  }
  controller.recoverLeftoverRecording();
}

if (typeof document !== "undefined") init();

export { RoomController };
