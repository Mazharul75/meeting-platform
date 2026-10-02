// Sitewide "Copy" button: <button data-copy="#some-input"><span class="copy-label">Copy</span></button>
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const el = document.querySelector(btn.getAttribute("data-copy"));
  if (!el) return;
  const text = "value" in el ? el.value : el.textContent;
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    if (el.select) {
      el.select();
      el.setSelectionRange?.(0, text.length);
    }
  }
  const label = btn.querySelector(".copy-label");
  if (label) {
    const original = label.textContent;
    label.textContent = "Copied";
    setTimeout(() => {
      label.textContent = original;
    }, 1500);
  }
});
