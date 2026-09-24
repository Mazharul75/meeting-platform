// Online meeting room (plan Section 10). Connects to LiveKit with end-to-end encryption,
// shows a tile per participant, and lets the host record the whole call with composite.js.
import { buildComposite } from "/static/js/composite.js";
import { Recorder, createApi, createStore } from "/static/js/recorder.js";

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
    this.room.on(RoomEvent.TrackUnsubscribed, (track) => track.detach().forEach((el) => el.remove()));
    this.room.on(RoomEvent.LocalTrackPublished, (pub, p) => {
      if (pub.track) this.attachTrack(pub.track, p);
    });
    this.room.on(RoomEvent.Reconnecting, () => this.setStatus("Reconnecting..."));
    this.room.on(RoomEvent.Reconnected, () => this.setStatus("Connected"));
    this.room.on(RoomEvent.Disconnected, (reason) => {
      this.setStatus(reason === LK.DisconnectReason.SERVER_SHUTDOWN ? "The meeting was ended" : "Disconnected");
      $("tiles").replaceChildren();
      if (window.__roomTest) window.__roomTest.disconnected = true;
    });
  }

  tileFor(identity) {
    let tile = document.getElementById("tile-" + identity);
    if (!tile) {
      tile = document.createElement("div");
      tile.className = "room-tile";
      tile.id = "tile-" + identity;
      tile.innerHTML = '<video autoplay playsinline></video><span class="room-tile-name"></span>';
      $("tiles").appendChild(tile);
    }
    return tile;
  }

  addTile(participant, isLocal) {
    const tile = this.tileFor(participant.identity);
    tile.dataset.identity = participant.identity;
    tile.querySelector(".room-tile-name").textContent =
      (isLocal ? "You" : this.names.get(participant.identity)) || "Connecting...";
  }

  removeTile(participant) {
    document.getElementById("tile-" + participant.identity)?.remove();
  }

  attachTrack(track, participant) {
    const tile = this.tileFor(participant.identity);
    const el = track.attach();
    if (track.kind === "video") {
      tile.querySelector("video")?.remove();
      el.autoplay = true;
      el.playsInline = true;
      tile.prepend(el);
    }
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
    await this.recorder.start(consent);
    this.recording = true;
  }

  async stopRecording() {
    if (!this.recorder) return;
    await this.recorder.stop();
    this.compositeStop?.();
    this.recording = false;
  }
}

function bindControls(controller) {
  $("btn-mic").addEventListener("click", () => {
    const on = controller.toggleMic();
    $("btn-mic").textContent = on ? "Mute" : "Unmute";
    $("btn-mic").setAttribute("aria-pressed", String(!on));
  });
  $("btn-cam").addEventListener("click", () => {
    const on = controller.toggleCamera();
    $("btn-cam").textContent = on ? "Camera off" : "Camera on";
    $("btn-cam").setAttribute("aria-pressed", String(!on));
  });
  $("btn-leave").addEventListener("click", async () => {
    $("btn-leave").disabled = true;
    await controller.leave();
    window.location.href = "/";
  });
  const endBtn = $("btn-end");
  if (endBtn) {
    endBtn.addEventListener("click", async () => {
      if (!window.confirm("End this meeting for everyone?")) return;
      await controller.endForEveryone();
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
        recBtn.textContent = "Stop recording";
        $("rec-badge").classList.remove("d-none");
      } else {
        recBtn.disabled = true;
        await controller.stopRecording();
        recBtn.disabled = false;
        recBtn.textContent = "Start recording";
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
  }
}

if (typeof document !== "undefined") init();

export { RoomController };
