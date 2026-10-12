/*
  Small progressive enhancements. Every page works without this file.

  - UTC clock in the footer.
  - "Now (UTC)" buttons next to date/time inputs marked data-now.
  - Keep the open/closed state of the mobile menu tidy when navigating.
*/
(function () {
  "use strict";

  function pad(n) { return String(n).padStart(2, "0"); }

  function utcStamp(d) {
    return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) +
      "T" + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
  }

  // Footer clock: hams log in UTC, so keep it in sight.
  var clock = document.getElementById("utc-clock");
  function tick() {
    if (!clock) return;
    var d = new Date();
    clock.textContent = pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
    clock.setAttribute("datetime", d.toISOString());
  }
  tick();
  setInterval(tick, 15000);

  // Inputs with data-now get a button that fills the current UTC time.
  document.querySelectorAll("input[data-now]").forEach(function (input) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-small";
    btn.textContent = "Now (UTC)";
    btn.addEventListener("click", function () {
      input.value = utcStamp(new Date());
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    input.insertAdjacentElement("afterend", btn);
  });

  // Close the menu when clicking outside it.
  document.addEventListener("click", function (e) {
    var menu = document.querySelector("details.menu[open]");
    if (menu && !menu.contains(e.target)) menu.removeAttribute("open");
  });
})();
