// Device test lobby: camera preview, microphone level meter, device pickers, clear errors.
const $ = (id) => document.getElementById(id);

export function describeMediaError(err) {
  const name = err && err.name;
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "Permission was denied. Allow camera and microphone for this site in your browser settings, then press the button again.";
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") {
    return "No camera or microphone was found. Plug one in or choose another device.";
  }
  if (name === "NotReadableError" || name === "AbortError") {
    return "The camera or microphone is being used by another app. Close that app and try again.";
  }
  return "Could not start the camera or microphone. " + ((err && err.message) || "");
}

export function level(analyser, buffer) {
  analyser.getByteTimeDomainData(buffer);
  let peak = 0;
  for (let i = 0; i < buffer.length; i++) peak = Math.max(peak, Math.abs(buffer[i] - 128) / 128);
  return Math.min(1, peak * 2);
}

function init() {
  const preview = $("preview");
  const btn = $("start-test");
  if (!preview || !btn) return;
  const errBox = $("lobby-error");
  const cam = $("cam-select");
  const mic = $("mic-select");
  const meter = $("level");
  let stream = null;
  let audioCtx = null;
  let raf = 0;

  function showError(msg) {
    errBox.textContent = msg;
    errBox.classList.toggle("d-none", !msg);
  }

  async function fillDevices() {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const fill = (select, kind, current) => {
      select.replaceChildren();
      devices
        .filter((d) => d.kind === kind)
        .forEach((d, i) => {
          const o = document.createElement("option");
          o.value = d.deviceId;
          o.textContent = d.label || (kind === "videoinput" ? "Camera " : "Microphone ") + (i + 1);
          if (d.deviceId === current) o.selected = true;
          select.appendChild(o);
        });
    };
    const vTrack = stream && stream.getVideoTracks()[0];
    const aTrack = stream && stream.getAudioTracks()[0];
    fill(cam, "videoinput", vTrack && vTrack.getSettings().deviceId);
    fill(mic, "audioinput", aTrack && aTrack.getSettings().deviceId);
  }

  function stop() {
    cancelAnimationFrame(raf);
    if (stream) stream.getTracks().forEach((t) => t.stop());
    if (audioCtx) audioCtx.close();
    stream = null;
    audioCtx = null;
  }

  async function start() {
    showError("");
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showError("This browser cannot use the camera. Please open the site in Chrome, Edge, Firefox or Safari over https.");
      return;
    }
    stop();
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: cam.value ? { deviceId: { exact: cam.value } } : true,
        audio: mic.value ? { deviceId: { exact: mic.value } } : true,
      });
    } catch (err) {
      showError(describeMediaError(err));
      return;
    }
    preview.srcObject = stream;
    // The AudioContext is created from a click, otherwise browsers keep it silent.
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const analyser = audioCtx.createAnalyser();
    analyser.fftSize = 512;
    audioCtx.createMediaStreamSource(stream).connect(analyser);
    const buffer = new Uint8Array(analyser.fftSize);
    const tick = () => {
      meter.value = level(analyser, buffer);
      raf = requestAnimationFrame(tick);
    };
    tick();
    await fillDevices();
    btn.textContent = "Restart test";
  }

  btn.addEventListener("click", start);
  cam.addEventListener("change", start);
  mic.addEventListener("change", start);
  window.addEventListener("pagehide", stop);
}

if (typeof document !== "undefined") init();
