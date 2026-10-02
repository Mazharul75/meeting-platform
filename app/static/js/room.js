// Online meeting room (plan Section 10, extended). Connects to LiveKit with end-to-end
// encryption, shows a tile per participant, lets the host record the whole call with
// composite.js, and adds the usual meeting-room controls: chat, raise hand, a participant
// list (with host mute), and screen sharing.
import { buildComposite } from "/static/js/composite.js";
import { Recorder, createApi, createStore, findUnfinished, recoverRecording } from "/static/js/recorder.js";

const LK = window.LivekitClient;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

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
  let data = null;
  try {
    data = await res.json();
  } catch (_) {
    /* no body */
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return data;
}

// Data-channel message "protocol" for chat and raise-hand. Sent over LiveKit's own relay
// (the same encrypted connection as audio/video), never touches the server or the recording.
const DATA_ENCODER = new TextEncoder();
const DATA_DECODER = new TextDecoder();

class RoomController {
  constructor({ tokenUrl, participantsUrl, muteUrlBase, endUrl, isHost, meetingId, statusEl }) {
    this.tokenUrl = tokenUrl;
    this.participantsUrl = participantsUrl;
    this.muteUrlBase = muteUrlBase;
    this.endUrl = endUrl;
    this.isHost = isHost;
    this.meetingId = meetingId;
    this.statusEl = statusEl;
    this.names = new Map();
    this.raisedHands = new Set();
    this.room = null;
    this.recorder = null;
    this.recording = false;
    this.sharingScreen = false;
    this.onChatMessage = null;
    this.onParticipantsChanged = null;
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
    const { RoomEvent, Track } = LK;
    this.room.on(RoomEvent.ParticipantConnected, (p) => this.addTile(p, false));
    this.room.on(RoomEvent.ParticipantDisconnected, (p) => {
      this.raisedHands.delete(p.identity);
      this.removeTile(p);
    });
    this.room.on(RoomEvent.TrackSubscribed, (track, pub, p) => this.attachTrack(track, p));
    this.room.on(RoomEvent.TrackUnsubscribed, (track, pub, p) => {
      if (pub.source === Track.Source.ScreenShare) {
        this.setStageVideo(null);
        this.onParticipantsChanged?.();
        return;
      }
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
    this.room.on(RoomEvent.DataReceived, (payload, participant) => this.handleData(payload, participant));
    this.room.on(RoomEvent.Reconnecting, () => this.setStatus("Reconnecting..."));
    this.room.on(RoomEvent.Reconnected, () => this.setStatus("Connected"));
    this.room.on(RoomEvent.Disconnected, (reason) => {
      this.setStatus(reason === LK.DisconnectReason.SERVER_SHUTDOWN ? "The meeting was ended" : "Disconnected");
      $("tiles")?.replaceChildren();
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
    this.onParticipantsChanged?.();
  }

  addTile(participant, isLocal) {
    const tile = this.tileFor(participant.identity);
    tile.dataset.identity = participant.identity;
    // Mirror only our own tile (a normal "look in the mirror" self-view, like Zoom/Meet/Teams).
    // This is a CSS-only flip on screen - the actual track sent to others and the recording
    // (drawn from this same <video> via canvas drawImage) are never mirrored.
    tile.classList.toggle("is-local", isLocal);
    const name = (isLocal ? "You" : this.names.get(participant.identity)) || "Connecting…";
    tile.querySelector(".room-tile-name").textContent = isLocal ? `${name} (Host)` : name;
    tile.querySelector(".room-tile-avatar").textContent = this.initials(isLocal ? "You" : name);
    this.onParticipantsChanged?.();
  }

  updateChips(participant) {
    const tile = document.getElementById("tile-" + participant.identity);
    const chips = tile?.querySelector(".room-tile-chips");
    if (chips) {
      const bits = [];
      if (!participant.isMicrophoneEnabled) bits.push("Mic off");
      if (!participant.isCameraEnabled) bits.push("Cam off");
      if (this.raisedHands.has(participant.identity)) bits.push("✋ Hand up");
      chips.replaceChildren(
        ...bits.map((text) => {
          const span = document.createElement("span");
          span.className = "chip";
          span.textContent = text;
          return span;
        }),
      );
    }
    this.onParticipantsChanged?.();
  }

  removeTile(participant) {
    document.getElementById("tile-" + participant.identity)?.remove();
    this.updateCount();
  }

  setStageVideo(mediaStreamTrack) {
    const stage = $("stage-video");
    const wrap = $("stage-wrap");
    if (!stage || !wrap) return;
    if (mediaStreamTrack) {
      stage.srcObject = new MediaStream([mediaStreamTrack]);
      wrap.classList.remove("d-none");
      $("tiles")?.classList.add("room-tiles-strip");
    } else {
      stage.srcObject = null;
      wrap.classList.add("d-none");
      $("tiles")?.classList.remove("room-tiles-strip");
    }
  }

  attachTrack(track, participant) {
    const { Track } = LK;
    if (track.source === Track.Source.ScreenShare) {
      this.setStageVideo(track.mediaStreamTrack);
      this.onParticipantsChanged?.();
      return;
    }
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
        this.onParticipantsChanged?.();
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

  /** Starts/stops sharing this person's screen. LiveKit publishes it as its own track
   *  (Track.Source.ScreenShare), separate from the camera - see attachTrack/setStageVideo. */
  async toggleScreenShare() {
    this.sharingScreen = !this.sharingScreen;
    try {
      await this.room.localParticipant.setScreenShareEnabled(this.sharingScreen, { audio: true });
    } catch (err) {
      this.sharingScreen = false; // the browser's own share picker was cancelled
      throw err;
    }
    if (this.sharingScreen) {
      const pub = [...this.room.localParticipant.videoTrackPublications.values()].find(
        (p) => p.source === LK.Track.Source.ScreenShare,
      );
      if (pub?.track) this.setStageVideo(pub.track.mediaStreamTrack);
    } else {
      this.setStageVideo(null);
    }
    return this.sharingScreen;
  }

  // ---- data channel: chat + raise hand ---------------------------------------------------

  sendData(message) {
    this.room?.localParticipant.publishData(DATA_ENCODER.encode(JSON.stringify(message)), { reliable: true });
  }

  handleData(payload, participant) {
    let message;
    try {
      message = JSON.parse(DATA_DECODER.decode(payload));
    } catch (_) {
      return;
    }
    if (!participant) return; // our own echo, if any - we already rendered it locally
    if (message.type === "chat" && typeof message.text === "string") {
      const name = this.names.get(participant.identity) || "Someone";
      this.onChatMessage?.({ name, text: message.text.slice(0, 2000), mine: false });
    } else if (message.type === "hand") {
      if (message.raised) this.raisedHands.add(participant.identity);
      else this.raisedHands.delete(participant.identity);
      this.updateChips(participant);
    }
  }

  sendChat(text) {
    text = text.trim().slice(0, 2000);
    if (!text) return;
    this.sendData({ type: "chat", text });
    this.onChatMessage?.({ name: "You", text, mine: true });
  }

  toggleHand() {
    const identity = this.room?.localParticipant.identity;
    const raised = !this.raisedHands.has(identity);
    if (raised) this.raisedHands.add(identity);
    else this.raisedHands.delete(identity);
    this.sendData({ type: "hand", raised });
    this.updateChips(this.room.localParticipant);
    return raised;
  }

  // ---- host moderation ---------------------------------------------------------------------

  /** Host-only. LiveKit lets the server force-mute someone but never force-unmute them -
   *  the muted person has to unmute themselves, same as Zoom. */
  async muteParticipant(identity) {
    await fetchJson(`${this.muteUrlBase}/${identity}/mute`);
  }

  participantList() {
    const localId = this.room?.localParticipant.identity;
    const list = [{ identity: localId, name: "You", participant: this.room.localParticipant, isLocal: true }];
    for (const p of this.room?.remoteParticipants.values() || []) {
      list.push({ identity: p.identity, name: this.names.get(p.identity) || "Connecting…", participant: p, isLocal: false });
    }
    return list;
  }

  // ------------------------------------------------------------------------------------------

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

// --------------------------------------------------------------------------------- UI panels

function renderParticipantPanel(controller) {
  const list = $("participant-list");
  if (!list) return;
  const rows = controller.participantList();
  list.replaceChildren(
    ...rows.map(({ identity, name, participant, isLocal }) => {
      const li = document.createElement("li");
      li.className = "list-group-item d-flex align-items-center justify-content-between gap-2";
      const label = document.createElement("span");
      const bits = [];
      if (!participant.isMicrophoneEnabled) bits.push("mic off");
      if (!participant.isCameraEnabled) bits.push("camera off");
      const hand = controller.raisedHands.has(identity) ? " · ✋" : "";
      label.textContent = `${name}${isLocal ? "" : ""}${bits.length ? " (" + bits.join(", ") + ")" : ""}${hand}`;
      li.appendChild(label);
      if (controller.isHost && !isLocal) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-sm btn-outline-secondary";
        btn.textContent = "Mute";
        btn.disabled = !participant.isMicrophoneEnabled;
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          try {
            await controller.muteParticipant(identity);
          } finally {
            btn.disabled = false;
          }
        });
        li.appendChild(btn);
      }
      return li;
    }),
  );
  const countEl = $("participant-count");
  if (countEl) countEl.textContent = String(rows.length);
}

function appendChatMessage({ name, text, mine }) {
  const log = $("chat-log");
  if (!log) return;
  const row = document.createElement("div");
  row.className = "chat-msg" + (mine ? " chat-msg-mine" : "");
  row.innerHTML = `<span class="chat-msg-name">${esc(name)}</span><span class="chat-msg-text">${esc(text)}</span>`;
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
  if (!mine && $("chat-panel")?.classList.contains("d-none")) {
    $("chat-badge")?.classList.remove("d-none");
  }
}

const PANEL_IDS = ["participants-panel", "chat-panel"];

function closePanel(id) {
  $(id)?.classList.add("d-none");
}

function togglePanel(id, otherIds) {
  const panel = $(id);
  if (!panel) return;
  const hidden = panel.classList.toggle("d-none");
  if (!hidden) {
    for (const other of otherIds) closePanel(other);
    if (id === "chat-panel") $("chat-badge")?.classList.add("d-none");
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
  $("btn-hand")?.addEventListener("click", () => {
    const up = controller.toggleHand();
    $("btn-hand").setAttribute("aria-pressed", String(up));
    $("btn-hand").querySelector(".lbl").textContent = up ? "Lower hand" : "Raise hand";
  });
  $("btn-share")?.addEventListener("click", async () => {
    const btn = $("btn-share");
    btn.disabled = true;
    try {
      const sharing = await controller.toggleScreenShare();
      btn.querySelector(".lbl").textContent = sharing ? "Stop sharing" : "Share screen";
      btn.setAttribute("aria-pressed", String(sharing));
    } catch (_) {
      /* the browser's share picker was cancelled - nothing to do */
    } finally {
      btn.disabled = false;
    }
  });
  $("btn-participants")?.addEventListener("click", () => togglePanel("participants-panel", ["chat-panel"]));
  $("btn-chat")?.addEventListener("click", () => togglePanel("chat-panel", ["participants-panel"]));
  document.querySelectorAll("[data-close-panel]").forEach((btn) => {
    btn.addEventListener("click", () => closePanel(btn.dataset.closePanel));
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") PANEL_IDS.forEach(closePanel);
  });
  const chatForm = $("chat-form");
  chatForm?.addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("chat-input");
    controller.sendChat(input.value);
    input.value = "";
  });
  controller.onChatMessage = appendChatMessage;
  controller.onParticipantsChanged = () => renderParticipantPanel(controller);

  $("btn-leave").addEventListener("click", async () => {
    $("btn-leave").disabled = true;
    if (controller.recording) $("btn-leave").querySelector(".lbl")?.replaceChildren("Saving…");
    try {
      await controller.leave();
      window.location.href = "/";
    } catch (err) {
      controller.setStatus("Could not leave cleanly: " + err.message + " - try again.");
      $("btn-leave").disabled = false;
    }
  });
  const endBtn = $("btn-end");
  if (endBtn) {
    endBtn.addEventListener("click", async () => {
      if (!window.confirm("End this meeting for everyone?")) return;
      endBtn.disabled = true;
      try {
        await controller.endForEveryone();
      } catch (err) {
        controller.setStatus("Could not end the meeting: " + err.message + " - try again.");
      } finally {
        endBtn.disabled = false;
      }
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
        try {
          await controller.startRecording(true);
          recBtn.querySelector(".lbl").textContent = "Stop recording";
          $("rec-badge").classList.remove("d-none");
        } catch (err) {
          controller.setStatus("Could not start recording: " + err.message);
        } finally {
          recBtn.disabled = false;
        }
      } else {
        recBtn.disabled = true;
        try {
          await controller.stopRecording();
          recBtn.querySelector(".lbl").textContent = "Start recording";
          $("rec-badge").classList.add("d-none");
        } catch (err) {
          controller.setStatus("Could not stop recording cleanly: " + err.message + " - try again.");
        } finally {
          recBtn.disabled = false;
        }
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
    muteUrlBase: cfg.muteUrlBase,
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
