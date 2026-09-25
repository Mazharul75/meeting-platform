// Filters the recordings table on the client only; the server has already sent just the
// recordings this user may see, so nothing new is fetched or revealed by typing here.
(function () {
  var input = document.getElementById("recording-search");
  if (!input) return;
  function apply() {
    var q = input.value.trim().toLowerCase();
    document.querySelectorAll("#recordings-list tbody tr").forEach(function (row) {
      row.classList.toggle("d-none", q !== "" && !row.textContent.toLowerCase().includes(q));
    });
  }
  input.addEventListener("input", apply);
  document.body.addEventListener("htmx:afterSwap", function (e) {
    if (e.target && e.target.id === "recordings-list") apply();
  });
})();
