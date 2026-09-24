// Plays the parts of a recording one after another and refreshes signed links before they expire.

export function nextIndex(current, total) {
  return current + 1 < total ? current + 1 : -1;
}

function init() {
  const cfgEl = document.getElementById("player-config");
  const video = document.getElementById("player");
  if (!cfgEl || !video) return;
  const cfg = JSON.parse(cfgEl.textContent);
  const status = document.getElementById("player-status");
  const buttons = document.querySelectorAll("[data-play]");
  let parts = cfg.parts;
  let index = 0;
  let refreshing = null;

  function label() {
    if (status) status.textContent = `Playing part ${parts[index].n} of ${parts.length}`;
    document.querySelectorAll("#part-list li").forEach((li, i) => li.classList.toggle("active", i === index));
  }

  function load(i, autoplay, atTime = 0) {
    index = i;
    video.src = parts[i].url;
    video.addEventListener(
      "loadedmetadata",
      () => {
        if (atTime > 0) video.currentTime = atTime;
        if (autoplay) video.play().catch(() => {});
      },
      { once: true },
    );
    label();
  }

  async function refreshLinks() {
    if (refreshing) return refreshing;
    refreshing = fetch(cfg.refreshUrl, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("refresh failed"))))
      .then((data) => {
        if (data.parts && data.parts.length) parts = data.parts;
      })
      .finally(() => {
        refreshing = null;
      });
    return refreshing;
  }

  video.addEventListener("ended", () => {
    const n = nextIndex(index, parts.length);
    if (n >= 0) load(n, true);
    else if (status) status.textContent = "Finished.";
  });

  // A link that expired while the page stayed open: get new links and continue where we were.
  video.addEventListener("error", async () => {
    const at = video.currentTime;
    const wasPlaying = !video.paused;
    try {
      await refreshLinks();
      load(index, wasPlaying, at);
    } catch (_) {
      if (status) status.textContent = "Could not load this part. Please reload the page.";
    }
  });

  buttons.forEach((b) =>
    b.addEventListener("click", () => {
      const n = Number(b.dataset.play);
      const i = parts.findIndex((p) => p.n === n);
      if (i >= 0) load(i, true);
    }),
  );

  // Refresh well before the 10 minute link lifetime ends. Only the URLs change.
  setInterval(() => refreshLinks().catch(() => {}), cfg.refreshSeconds * 1000);
  load(0, false);
}

if (typeof document !== "undefined") init();
