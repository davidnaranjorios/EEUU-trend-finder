// Tooltip de los gráficos (cruz + valor) y explorador de series. Sin dependencias.
(function () {
  "use strict";

  function fmt(v, f) {
    if (v === null || v === undefined) return "—";
    if (f === "share") return (v * 100).toFixed(2) + " %";
    var a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(2) + " B";
    if (a >= 1e6) return (v / 1e6).toFixed(2) + " M";
    if (a >= 1e3) return v.toLocaleString("es", { maximumFractionDigits: 1 });
    return v.toPrecision(4);
  }

  var tip = document.createElement("div");
  tip.className = "tip";
  tip.hidden = true;
  document.body.appendChild(tip);

  document.querySelectorAll("svg.chart[data-points]").forEach(function (svg) {
    var data;
    try { data = JSON.parse(svg.getAttribute("data-points")); } catch (e) { return; }
    var pts = data.p, f = data.f;
    var g = svg.querySelector(".hover"), cross = g.querySelector(".cross"), dot = g.querySelector(".dot");
    function move(ev) {
      var r = svg.getBoundingClientRect();
      var vb = svg.viewBox.baseVal;
      var x = (ev.clientX - r.left) * vb.width / r.width;
      var lo = 0, hi = pts.length - 1;
      while (hi - lo > 1) { var mid = (lo + hi) >> 1; if (pts[mid][0] < x) lo = mid; else hi = mid; }
      var p = Math.abs(pts[lo][0] - x) <= Math.abs(pts[hi][0] - x) ? pts[lo] : pts[hi];
      cross.setAttribute("x1", p[0]); cross.setAttribute("x2", p[0]);
      dot.setAttribute("cx", p[0]); dot.setAttribute("cy", p[1]);
      g.setAttribute("visibility", "visible");
      tip.textContent = p[2] + " · " + fmt(p[3], f);
      tip.hidden = false;
      tip.style.left = Math.min(ev.clientX + 12, window.innerWidth - tip.offsetWidth - 8) + "px";
      tip.style.top = (ev.clientY - 32) + "px";
    }
    svg.addEventListener("pointermove", move);
    svg.addEventListener("pointerleave", function () { g.setAttribute("visibility", "hidden"); tip.hidden = true; });
  });

  // --- Ranking: filtro y orden por el estado de los últimos 12 meses (no cambia el score) ---
  var rc = document.getElementById("rank-controls"), list = document.getElementById("rank-list");
  if (rc && list) {
    var order = ["acelera", "continúa", "se frena", "revierte", "mixto", "dato insuficiente"];
    var fSel = rc.querySelector('[data-role="state"]'), sSel = rc.querySelector('[data-role="sort"]');
    var apply = function () {
      var cards = Array.prototype.slice.call(list.querySelectorAll("section.cand"));
      cards.forEach(function (c) { c.hidden = !!fSel.value && c.dataset.state !== fSel.value; });
      cards.sort(function (a, b) {
        if (sSel.value === "state") {
          var d = order.indexOf(a.dataset.state) - order.indexOf(b.dataset.state);
          if (d) return d;
        }
        return Number(a.dataset.rank) - Number(b.dataset.rank);
      });
      cards.forEach(function (c) { list.appendChild(c); });
    };
    fSel.addEventListener("change", apply);
    sSel.addEventListener("change", apply);
  }

  // --- Ficha del fenómeno: mostrar u ocultar relacionadas en el panel apilado ---
  document.querySelectorAll(".stack").forEach(function (panel) {
    panel.querySelectorAll('.stack-pick input[type="checkbox"]').forEach(function (box) {
      box.addEventListener("change", function () {
        panel.querySelectorAll(".stack-item").forEach(function (fig) {
          if (fig.dataset.key === box.dataset.key) fig.hidden = !box.checked;
        });
      });
    });
  });

  // --- Explorador ---------------------------------------------------------------------------
  var root = document.getElementById("explorer");
  if (!root) return;
  var input = root.querySelector("input"), sel = root.querySelector("select");
  var status = root.querySelector(".status"), body = root.querySelector("tbody");
  var cards = [], loaded = false, loading = null, MAX = 200;

  function load() {
    if (loading) return loading;
    status.textContent = "Cargando fichas…";
    loading = fetch("data/explorer/index.json").then(function (r) { return r.json(); }).then(function (idx) {
      var done = 0;
      return Promise.all(idx.buckets.map(function (b) {
        var name = "data/explorer/" + String(b).padStart(2, "0") + ".json";
        return fetch(name).then(function (r) { return r.json(); }).then(function (d) {
          d.rows.forEach(function (row) {
            row.push((row[1] + " " + row[2]).toLowerCase());
            cards.push(row);
          });
          done += 1;
          status.textContent = "Cargando fichas… " + done + "/" + idx.buckets.length;
        });
      })).then(function () { loaded = true; });
    });
    return loading;
  }

  function td(text, cls) {
    var c = document.createElement("td");
    if (cls) c.className = cls;
    c.textContent = text;
    return c;
  }

  function render() {
    var q = input.value.trim().toLowerCase(), src = sel.value;
    if (q.length < 2 && !src) { body.innerHTML = ""; status.textContent = "Escribe al menos 2 caracteres."; return; }
    var toks = q.split(/\s+/).filter(Boolean), out = [], n = 0;
    for (var i = 0; i < cards.length; i++) {
      var c = cards[i];
      if (src && c[0] !== src) continue;
      var ok = true;
      for (var j = 0; j < toks.length; j++) if (c[9].indexOf(toks[j]) < 0) { ok = false; break; }
      if (!ok) continue;
      n += 1;
      if (out.length < MAX) out.push(c);
    }
    out.sort(function (a, b) { return (a[5] === null) - (b[5] === null) || (a[5] || 0) - (b[5] || 0); });
    body.innerHTML = "";
    out.forEach(function (c) {
      var tr = document.createElement("tr");
      tr.appendChild(td(c[0])); tr.appendChild(td(c[1])); tr.appendChild(td(c[2]));
      tr.appendChild(td(c[3])); tr.appendChild(td(c[4]));
      tr.appendChild(td(c[5] === null ? "—" : String(c[5]), "num"));
      tr.appendChild(td(c[6] || "—")); tr.appendChild(td(String(c[7]), "num"));
      var cell = document.createElement("td");
      if (c[8]) { var a = document.createElement("a"); a.href = "index.html#" + c[8]; a.textContent = "ver"; cell.appendChild(a); }
      else cell.textContent = "—";
      tr.appendChild(cell);
      body.appendChild(tr);
    });
    status.textContent = n.toLocaleString("es") + " series" + (n > MAX ? " (se muestran las " + MAX + " de menor q)" : "");
  }

  var timer = null;
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(function () { (loaded ? Promise.resolve() : load()).then(render); }, 200);
  }
  input.addEventListener("input", schedule);
  sel.addEventListener("change", schedule);
})();
