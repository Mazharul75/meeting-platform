// Draws every participant's video into one canvas grid and mixes every microphone into one
// audio track, so the host's browser can record the whole online call (plan Section 10.3).
// Drawing is driven by a Web Worker timer because browsers slow down rAF/setInterval in a
// hidden tab, and the host is warned in the room page to keep the tab visible either way.

const WIDTH = 1280;
const HEIGHT = 720;
const FPS = 24;

/** Rectangles for 1..9 tiles: 1 full, 2 side by side, 3-4 in a 2x2 grid, up to 9 in 3x3. */
export function gridLayout(count, width = WIDTH, height = HEIGHT) {
  if (count <= 0) return [];
  const cols = count === 1 ? 1 : count === 2 ? 2 : count <= 4 ? 2 : 3;
  const rows = Math.ceil(count / cols);
  const cellW = Math.floor(width / cols);
  const cellH = Math.floor(height / rows);
  const rects = [];
  for (let i = 0; i < count; i++) {
    const col = i % cols;
    const row = Math.floor(i / cols);
    // Centre the last, shorter row (e.g. 5 tiles: 3 on top, 2 centred below).
    const inLastRow = row === rows - 1 ? count - (rows - 1) * cols : cols;
    const rowOffset = Math.floor((cols - inLastRow) * cellW) / 2;
    rects.push({ x: col * cellW + (row === rows - 1 ? rowOffset : 0), y: row * cellH, w: cellW, h: cellH });
  }
  return rects;
}

/** Splits the frame into a big "stage" area (for a shared screen) and a thumbnail strip
 *  underneath for everyone's camera - the same layout the room page itself uses while a
 *  screen is being shared. */
export function stageSplit(width = WIDTH, height = HEIGHT) {
  const stageH = Math.round(height * 0.74);
  return {
    stage: { x: 0, y: 0, w: width, h: stageH },
    strip: { x: 0, y: stageH, w: width, h: height - stageH },
  };
}

/** The largest rectangle that fits `srcW`x`srcH` inside `box` without cropping (letterboxed),
 *  the way a shared screen should be drawn - never stretched or cut off. */
export function fitContain(srcW, srcH, box) {
  if (!srcW || !srcH) return { ...box };
  const scale = Math.min(box.w / srcW, box.h / srcH);
  const w = srcW * scale;
  const h = srcH * scale;
  return { x: box.x + (box.w - w) / 2, y: box.y + (box.h - h) / 2, w, h };
}

function makeWorkerTimer(intervalMs, onTick) {
  const code = `let t; onmessage = (e) => { if (e.data === 'stop') { clearInterval(t); return; } clearInterval(t); t = setInterval(() => postMessage(1), e.data); };`;
  const worker = new Worker(URL.createObjectURL(new Blob([code], { type: "application/javascript" })));
  worker.onmessage = onTick;
  worker.postMessage(intervalMs);
  return worker;
}

/**
 * @param {object} room - a connected LiveKit Room (from room.js)
 * @returns {{stream: MediaStream, stop: () => void, videoFailed: boolean}}
 */
export function buildComposite(room) {
  const canvas = document.createElement("canvas");
  canvas.width = WIDTH;
  canvas.height = HEIGHT;
  const ctx = canvas.getContext("2d");
  const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const destination = audioCtx.createMediaStreamDestination();
  const connectedAudio = new Map(); // trackSid -> {source, track}
  let videoFailed = false;

  function participants() {
    const list = [room.localParticipant];
    for (const p of room.remoteParticipants.values()) list.push(p);
    return list;
  }

  function nameFor(participant) {
    return document.querySelector(`#tile-${participant.identity} .room-tile-name`)?.textContent || "";
  }

  function drawTile(p, rect) {
    const video = document.querySelector(`#tile-${p.identity} video`);
    if (video && video.readyState >= 2) {
      ctx.drawImage(video, rect.x, rect.y, rect.w, rect.h);
    } else {
      ctx.fillStyle = "#1b1f3b";
      ctx.fillRect(rect.x, rect.y, rect.w, rect.h);
    }
    const labelH = Math.max(18, Math.round(rect.h * 0.11));
    ctx.fillStyle = "rgba(0,0,0,0.55)";
    ctx.fillRect(rect.x, rect.y + rect.h - labelH, rect.w, labelH);
    ctx.fillStyle = "#fff";
    ctx.font = `${Math.round(labelH * 0.62)}px sans-serif`;
    ctx.fillText(nameFor(p), rect.x + 8, rect.y + rect.h - labelH * 0.32, rect.w - 16);
  }

  function drawFrame() {
    ctx.fillStyle = "#10132b";
    ctx.fillRect(0, 0, WIDTH, HEIGHT);
    if (videoFailed) return;
    try {
      const people = participants();
      // While someone is sharing their screen, it takes the big "stage" area and everyone's
      // camera shrinks into a thumbnail strip underneath - matching what the room page shows live.
      const stageVideo = document.getElementById("stage-video");
      const sharing = stageVideo && stageVideo.readyState >= 2 && !stageVideo.paused;
      if (sharing) {
        const { stage, strip } = stageSplit(WIDTH, HEIGHT);
        ctx.fillStyle = "#000";
        ctx.fillRect(stage.x, stage.y, stage.w, stage.h);
        const fit = fitContain(stageVideo.videoWidth, stageVideo.videoHeight, stage);
        ctx.drawImage(stageVideo, fit.x, fit.y, fit.w, fit.h);
        const rects = gridLayout(people.length, strip.w, strip.h);
        people.forEach((p, i) => drawTile(p, { x: rects[i].x + strip.x, y: rects[i].y + strip.y, w: rects[i].w, h: rects[i].h }));
      } else {
        const rects = gridLayout(people.length);
        people.forEach((p, i) => drawTile(p, rects[i]));
      }
    } catch (err) {
      // Canvas drawing failed (e.g. a track error): fall back to audio-only, keep recording.
      videoFailed = true;
      console.error("composite drawing failed, continuing audio-only", err);
    }
  }

  function reconcileAudio() {
    const wanted = new Map();
    for (const p of participants()) {
      for (const pub of p.audioTrackPublications.values()) {
        if (pub.track?.mediaStreamTrack) wanted.set(pub.trackSid, pub.track.mediaStreamTrack);
      }
    }
    for (const [sid, entry] of connectedAudio) {
      if (!wanted.has(sid)) {
        entry.source.disconnect();
        connectedAudio.delete(sid);
      }
    }
    for (const [sid, mediaTrack] of wanted) {
      if (!connectedAudio.has(sid)) {
        const source = audioCtx.createMediaStreamSource(new MediaStream([mediaTrack]));
        source.connect(destination);
        connectedAudio.set(sid, { source, track: mediaTrack });
      }
    }
  }

  const worker = makeWorkerTimer(1000 / FPS, drawFrame);
  const audioTimer = setInterval(reconcileAudio, 1000);
  reconcileAudio();
  drawFrame();

  const canvasStream = canvas.captureStream(FPS);
  const tracks = videoFailed ? [] : canvasStream.getVideoTracks();
  const stream = new MediaStream([...tracks, ...destination.stream.getAudioTracks()]);

  function stop() {
    worker.postMessage("stop");
    worker.terminate();
    clearInterval(audioTimer);
    for (const { source } of connectedAudio.values()) source.disconnect();
    connectedAudio.clear();
    canvasStream.getTracks().forEach((t) => t.stop());
    audioCtx.close();
  }

  return { stream, stop, get videoFailed() { return videoFailed; } };
}
