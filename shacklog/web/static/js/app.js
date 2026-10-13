/*
  Small progressive enhancements. Every page works without this file.

  - UTC clock in the footer.
  - "Now (UTC)" buttons next to date/time inputs marked data-now.
  - The navigation drawer on narrow screens (works without JS too, via #sidenav).
  - The dark/light switch, remembered in this browser (dark is the default).
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

  // Navigation drawer (narrow screens). Without JS the Menu link jumps to #sidenav,
  // which CSS shows with :target; with JS it toggles a class instead.
  var toggle = document.querySelector("[data-nav-toggle]");
  function setNav(open) {
    document.body.classList.toggle("nav-open", open);
    if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (toggle) toggle.addEventListener("click", function (e) {
    e.preventDefault();
    setNav(!document.body.classList.contains("nav-open"));
  });
  document.querySelectorAll("[data-nav-close]").forEach(function (el) {
    el.addEventListener("click", function (e) { e.preventDefault(); setNav(false); });
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") setNav(false); });

  // A "tick all" box ticks or unticks every checkbox named by data-check-all in its form.
  document.querySelectorAll("[data-check-all]").forEach(function (all) {
    all.addEventListener("change", function () {
      var name = all.getAttribute("data-check-all");
      all.form.querySelectorAll('input[type=checkbox][name="' + name + '"]').forEach(function (box) {
        box.checked = all.checked;
      });
    });
  });

  // Dark / light theme.
  var themeBtn = document.querySelector("[data-theme-toggle]");
  if (themeBtn) themeBtn.addEventListener("click", function () {
    var root = document.documentElement;
    var light = root.getAttribute("data-theme") !== "light";
    if (light) root.setAttribute("data-theme", "light"); else root.removeAttribute("data-theme");
    try { localStorage.setItem("shacklog-theme", light ? "light" : "dark"); } catch (err) { /* private mode */ }
  });
})();
