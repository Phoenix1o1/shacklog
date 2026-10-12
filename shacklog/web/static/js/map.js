/*
  The map page (templates/map/index.html).

  Reads its settings from the JSON block #map-config (written by views/map.py), then for
  each layer fetches /map/data.json?layer=<key>&<that section's filters> and draws the
  points with Leaflet's canvas renderer (fast with thousands of points, also on phones).

  - Layer checkboxes show/hide layers; data is fetched once per layer, on first show.
  - "Lines from my position" draws a line from where I was to each QSO / station heard.
  - "List what's in this area" opens that section's list with bbox=minlat,minlon,maxlat,maxlon.
  - Popups are built with DOM nodes (textContent), never from HTML strings, so a callsign
    or a repeater name can't inject markup.
*/
(function () {
  "use strict";

  var configEl = document.getElementById("map-config");
  var mapEl = document.getElementById("map");
  if (!configEl || !mapEl || typeof L === "undefined") return;
  var config = JSON.parse(configEl.textContent);
  var statusEl = document.getElementById("map-status");

  // Colours from the stylesheet tokens, so dark mode and the legend agree.
  var css = getComputedStyle(document.documentElement);
  function token(name, fallback) {
    var v = css.getPropertyValue(name).trim();
    return v || fallback;
  }
  var COLORS = {
    qso: token("--accent", "#0d6b59"),
    swl: token("--amber", "#f3b733"),
    repeater: token("--margin", "#cf3f37"),
    station: token("--ink", "#18302a")
  };

  var renderer = L.canvas({ padding: 0.5 });
  var map = L.map(mapEl, { preferCanvas: true, worldCopyJump: true });
  L.tileLayer(config.tiles.url, {
    maxZoom: config.tiles.maxZoom || 19,
    attribution: config.tiles.attribution
  }).addTo(map);
  L.control.scale({ imperial: false }).addTo(map);

  if (config.focus) {
    map.setView([config.focus.lat, config.focus.lon], 9);
  } else if (config.home) {
    map.setView([config.home.lat, config.home.lon], 8);
  } else {
    map.setView([45.5, 9.2], 5);
  }

  if (config.home) {
    L.circleMarker([config.home.lat, config.home.lon], {
      renderer: renderer, radius: 7, color: COLORS.station, weight: 3, fillOpacity: 0
    }).addTo(map).bindTooltip(function () { return textNode("Default station: " + config.home.name); });
  }
  if (config.focus && config.focus.radius_km > 0) {
    L.circle([config.focus.lat, config.focus.lon], {
      radius: config.focus.radius_km * 1000, color: COLORS.qso, weight: 1,
      fill: false, dashArray: "4 4", interactive: false
    }).addTo(map);
  }

  // ---- popups -----------------------------------------------------------------
  // Leaflet inserts string content with innerHTML: always hand it DOM nodes instead.
  function textNode(text) {
    var span = document.createElement("span");
    span.textContent = text || "";
    return span;
  }

  function popupFor(point) {
    var box = document.createElement("div");
    var title = document.createElement(point.url ? "a" : "span");
    title.className = "map-popup-title";
    title.textContent = point.title || "(no name)";
    if (point.url) title.href = point.url;
    box.appendChild(title);
    if (point.sub) {
      var sub = document.createElement("div");
      sub.className = "map-popup-sub";
      sub.textContent = point.sub;
      box.appendChild(sub);
    }
    return box;
  }

  // ---- layers -------------------------------------------------------------------
  var state = {};  // key -> {spec, group, lines, loaded, info, bounds}
  config.layers.forEach(function (spec) {
    state[spec.key] = {
      spec: spec, group: L.featureGroup(), lines: L.layerGroup(),
      loaded: false, loading: false, info: null
    };
  });
  var linesBox = document.getElementById("map-lines");
  var fittedOnce = !!config.focus;

  function markerFor(key, point) {
    var opts = {
      renderer: renderer, radius: key === "station" ? 7 : 5, color: COLORS[key],
      weight: key === "repeater" ? 2 : 1, fillColor: COLORS[key], fillOpacity: 0.75
    };
    if (key === "repeater" && point.status === "inactive") opts.fillOpacity = 0.15;
    var m = L.circleMarker([point.lat, point.lon], opts);
    m.bindPopup(function () { return popupFor(point); });
    m.bindTooltip(function () { return textNode(point.title); }, { direction: "top", offset: [0, -4] });
    return m;
  }

  function load(key) {
    var st = state[key];
    if (st.loaded || st.loading) return;
    st.loading = true;
    updateStatus();
    var url = config.dataUrl + "?layer=" + encodeURIComponent(key) +
      (st.spec.filters ? "&" + st.spec.filters : "");
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        st.info = data;
        data.points.forEach(function (p) {
          st.group.addLayer(markerFor(key, p));
          if (p.my && st.spec.lines) {
            st.lines.addLayer(L.polyline([p.my, [p.lat, p.lon]], {
              renderer: renderer, color: COLORS[key], weight: 1, opacity: 0.45, interactive: false
            }));
          }
        });
        st.loaded = true;
        st.loading = false;
        if (isOn(key)) show(key);
        fitIfFirst();
        updateStatus();
      })
      .catch(function (err) {
        st.loading = false;
        st.info = { error: String(err.message || err) };
        updateStatus();
      });
  }

  function isOn(key) {
    var box = document.querySelector('input[data-layer="' + key + '"]');
    return !!(box && box.checked);
  }

  function show(key) {
    var st = state[key];
    if (!st.loaded) { load(key); return; }
    st.group.addTo(map);
    if (linesBox && linesBox.checked && st.spec.lines) st.lines.addTo(map);
    else map.removeLayer(st.lines);
  }

  function hide(key) {
    map.removeLayer(state[key].group);
    map.removeLayer(state[key].lines);
  }

  function fitIfFirst() {
    // Fit to the points once, when everything requested has arrived.
    if (fittedOnce) return;
    var pending = config.layers.some(function (s) { return isOn(s.key) && !state[s.key].loaded && !state[s.key].info; });
    if (pending) return;
    var bounds = null;
    config.layers.forEach(function (s) {
      if (!isOn(s.key) || !state[s.key].loaded) return;
      var b = state[s.key].group.getBounds();
      if (b.isValid()) bounds = bounds ? bounds.extend(b) : L.latLngBounds(b.getSouthWest(), b.getNorthEast());
    });
    if (bounds) {
      if (config.home) bounds.extend([config.home.lat, config.home.lon]);
      map.fitBounds(bounds, { padding: [24, 24], maxZoom: 11 });
    }
    fittedOnce = true;
  }

  function updateStatus() {
    if (!statusEl) return;
    var parts = [];
    config.layers.forEach(function (s) {
      var st = state[s.key];
      if (!isOn(s.key)) return;
      if (st.loading) { parts.push(s.label + ": loading…"); return; }
      if (!st.info) return;
      if (st.info.error) { parts.push(s.label + ": couldn't load (" + st.info.error + ")"); return; }
      var text = s.label + ": " + st.info.shown;
      if (st.info.capped) text += " (only the newest " + st.info.max + " are drawn; filter the list to narrow it)";
      if (st.info.without_position) text += ", " + st.info.without_position + " without a position";
      parts.push(text);
    });
    statusEl.textContent = parts.join(" · ");
  }

  document.querySelectorAll("input[data-layer]").forEach(function (box) {
    box.addEventListener("change", function () {
      var key = box.getAttribute("data-layer");
      if (box.checked) show(key); else hide(key);
      updateStatus();
    });
  });
  if (linesBox) {
    linesBox.addEventListener("change", function () {
      config.layers.forEach(function (s) { if (isOn(s.key) && state[s.key].loaded) show(s.key); });
    });
  }

  // ---- list this area ------------------------------------------------------------
  var listBtn = document.getElementById("map-list-area");
  var listSel = document.getElementById("map-list-layer");
  if (listBtn && listSel) {
    listBtn.addEventListener("click", function () {
      var spec = state[listSel.value] && state[listSel.value].spec;
      if (!spec || !spec.listUrl) return;
      var b = map.getBounds();
      var clamp = function (v, lo, hi) { return Math.min(hi, Math.max(lo, v)); };
      var bbox = [
        clamp(b.getSouth(), -90, 90).toFixed(4), clamp(b.getWest(), -180, 180).toFixed(4),
        clamp(b.getNorth(), -90, 90).toFixed(4), clamp(b.getEast(), -180, 180).toFixed(4)
      ].join(",");
      // Keep the filters the layer arrived with, replacing any older map area.
      var params = new URLSearchParams(spec.filters || "");
      params.set("bbox", bbox);
      window.location.href = spec.listUrl + "?" + params.toString();
    });
  }

  config.layers.forEach(function (s) { if (isOn(s.key)) load(s.key); });
  updateStatus();

  // The map sits in a flex layout; recompute its size once fonts and layout settle.
  window.addEventListener("load", function () { map.invalidateSize(); });
})();
