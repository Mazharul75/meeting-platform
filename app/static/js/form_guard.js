// Prevents double-submitting a form (e.g. double-clicking "Cancel meeting") by disabling its
// submit buttons once it is actually submitted. Opt out with data-no-guard on forms that stay
// on the page and are submitted more than once (e.g. a chat box).
//
// Also asks for confirmation before a destructive plain-form submit, via data-confirm="...".
// A real confirm() dialog needs no inline script (CSP forbids onsubmit="...") - this is the
// same job hx-confirm does for htmx actions elsewhere in the app.
document.addEventListener("submit", (e) => {
  const form = e.target;
  if (!(form instanceof HTMLFormElement) || form.hasAttribute("data-no-guard")) return;
  const confirmMessage = form.getAttribute("data-confirm");
  if (confirmMessage && !window.confirm(confirmMessage)) {
    e.preventDefault();
    return;
  }
  const buttons = form.querySelectorAll('button[type="submit"], input[type="submit"]');
  buttons.forEach((btn) => {
    btn.disabled = true;
  });
  // Safety net: re-enable if we are somehow still on this page after a while (e.g. the
  // browser blocked navigation, or the user came back via the back/forward cache).
  setTimeout(() => {
    buttons.forEach((btn) => {
      btn.disabled = false;
    });
  }, 15000);
});

window.addEventListener("pageshow", (e) => {
  if (!e.persisted) return;
  document.querySelectorAll('button[disabled][type="submit"]').forEach((btn) => {
    btn.disabled = false;
  });
});
