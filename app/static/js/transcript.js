// Free transcript generation on the recording page (host/admin only). The server runs
// open-source Whisper on the recording's own parts - no third-party API, no live-mic
// sensitivity problem, since it transcribes the actual recorded audio.
const $ = (id) => document.getElementById(id);

async function post(url) {
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content,
    },
  });
  let data = null;
  try {
    data = await res.json();
  } catch (_) {
    /* no body */
  }
  return { ok: res.ok, data };
}

function download(text, filename) {
  const blob = new Blob([text], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function init() {
  const cfgEl = $("transcript-config");
  if (!cfgEl) return;
  const cfg = JSON.parse(cfgEl.textContent);
  const btn = $("btn-generate-transcript");
  const status = $("transcript-status");

  btn?.addEventListener("click", async () => {
    btn.disabled = true;
    if (status) status.textContent = "Transcribing - this can take a little while…";
    const { ok, data } = await post(cfg.url);
    if (ok && data?.status === "ready") {
      if (status) status.textContent = "";
      const textEl = $("transcript-text");
      if (textEl) textEl.textContent = data.text;
      $("transcript-result")?.classList.remove("d-none");
      btn.classList.add("d-none");
    } else {
      btn.disabled = false;
      if (status) status.textContent = "Could not generate the transcript. Please try again.";
    }
  });

  $("btn-download-transcript")?.addEventListener("click", () => {
    download($("transcript-text")?.textContent || "", `transcript-${cfg.recordingId}.txt`);
  });
}

if (typeof document !== "undefined") init();
