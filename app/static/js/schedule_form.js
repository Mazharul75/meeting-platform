// Combines the separate Date and Time inputs into the single "start" field the server expects.
// A plain script (not a module) so it works without any inline code, keeping the CSP strict.
(function () {
  var form = document.getElementById("schedule-form");
  var date = document.getElementById("date");
  var time = document.getElementById("time");
  var combined = document.getElementById("start-combined");
  if (!form || !date || !time || !combined) return;
  if (combined.value) {
    var parts = combined.value.split("T");
    if (parts.length === 2) {
      date.value = parts[0];
      time.value = parts[1];
    }
  }
  form.addEventListener("submit", function () {
    combined.value = date.value && time.value ? date.value + "T" + time.value : "";
  });
})();
