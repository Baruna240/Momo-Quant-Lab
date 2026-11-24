/* Bar navigasi bersama untuk SEMUA halaman dashboard.
   - Pasar (Meme coin, Altcoin, Copy trade, Saham BEI) selalu di posisi yang sama, apa pun temanya.
   - Tema, ukuran tampilan dan animasi ada di satu tempat: tombol Pengaturan.
   - Pilihan disimpan di browser ini; tema yang dipilih dipakai di setiap pasar yang punya tema itu.
   Dimuat paling atas di <body>: <script src="/nav.js"></script>
   Ikon digambar sendiri (SVG garis 24x24, stroke 1.75) supaya seragam dan tajam di semua layar. */
(function () {
  "use strict";
  // ---------------------------------------------------------------- ikon
  var ICON = {
    meme: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="5.6" stroke-opacity=".45"/><path d="M9.6 14.4V9.8l2.4 2.4 2.4-2.4v4.6"/>',
    alt: '<path d="M7 3.5v3M7 16v4.5M17 3.5v5M17 15v5.5"/><rect x="4.8" y="6.5" width="4.4" height="9.5" rx="1"/><rect x="14.8" y="8.5" width="4.4" height="6.5" rx="1"/>',
    copy: '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19.5c.5-3.2 2.7-5 5.5-5s5 1.8 5.5 5"/><path d="M15.2 5.1a3.1 3.1 0 0 1 0 5.8"/><path d="M17.3 14.8c1.7.7 2.8 2.3 3.2 4.7"/>',
    saham: '<path d="M3.5 9.2 12 4.5l8.5 4.7"/><path d="M4.5 9.5h15"/><path d="M6.5 12v5.5M10.2 12v5.5M13.8 12v5.5M17.5 12v5.5"/><path d="M3.5 20.5h17"/>',
    settings: '<path d="M4 6.5h9M17 6.5h3M4 12h3M11 12h9M4 17.5h11M19 17.5h1"/><circle cx="15" cy="6.5" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="17.5" r="2"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
    check: '<path d="m5 12.5 4.2 4.2L19 7"/>',
    logo: '<path d="M4.5 16.5 9 11.5l3.5 3.2 7-8"/><path d="M15.5 6.7h4v4"/>'
  };
  function svg(name, size) {
    var px = (size || 18) + "px";   // ukuran dikunci inline: aturan svg {width:100%} di halaman tidak boleh membesarkannya
    return '<svg viewBox="0 0 24 24" width="' + (size || 18) + '" height="' + (size || 18) + '" style="width:' + px + ";height:" + px + '" fill="none" stroke="currentColor"' +
      ' stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICON[name] + "</svg>";
  }
  window.MOMO_ICON = svg;   // dipakai halaman lain (mis. agent di /saham)

  var MARKETS = [
    { id: "meme", label: "Meme coin", pages: { terminal: "/", abyss: "/lab", prism: "/prism" } },
    { id: "alt", label: "Altcoin", pages: { terminal: "/alt", abyss: "/alt-abyss", prism: "/prism-alt" } },
    { id: "copy", label: "Copy trade", pages: { terminal: "/copy" } },
    { id: "saham", label: "Saham BEI", pages: { terminal: "/saham" } }
  ];
  // spesifikasi ditulis dari isi halaman masing-masing tema (warna, huruf, animasi), supaya pengguna tahu sebelum memilih
  var THEMES = [
    { id: "terminal", label: "Terminal", tagline: "Seperti layar trading profesional",
      pv: { bg: "#07090c", panel: "#0e1217", line: "#1d2530", a: "#22d3ee", b: "#f5c542", font: "ui-monospace,Consolas,monospace", radius: 3 },
      spec: [["Warna", "Hitam pekat, aksen cyan & kuning"], ["Huruf", "Monospace (angka rata, mudah dibandingkan)"],
             ["Kepadatan", "Tinggi: paling banyak data dalam satu layar"], ["Animasi", "Minim, paling ringan untuk laptop"],
             ["Cocok untuk", "Memantau angka cepat dan detail"]] },
    { id: "abyss", label: "Abyss", tagline: "Pusat kendali bertema laut dalam",
      pv: { bg: "#031024", panel: "rgba(8,32,62,.75)", line: "rgba(94,242,255,.25)", a: "#5ef2ff", b: "#a78bfa", font: "'Chakra Petch',monospace", radius: 8, bubbles: true },
      spec: [["Warna", "Biru laut dalam, kartu kaca transparan"], ["Huruf", "Chakra Petch (judul) + JetBrains Mono"],
             ["Kepadatan", "Sedang, ringkasan meme & altcoin di satu layar"], ["Animasi", "Tinggi: gelembung, cahaya & partikel bergerak"],
             ["Cocok untuk", "Layar besar / dipajang. Lebih berat di laptop lemah"]] },
    { id: "prism", label: "Prism", tagline: "Modern, fokus ke alur kerja agent",
      pv: { bg: "#0b0a10", panel: "#14131c", line: "rgba(255,255,255,.1)", a: "#2dd4bf", b: "#a78bfa", font: "'Space Grotesk',system-ui,sans-serif", radius: 10, grad: true },
      spec: [["Warna", "Hitam keunguan, aksen gradasi pelangi"], ["Huruf", "Space Grotesk (modern, mudah dibaca)"],
             ["Kepadatan", "Lega, dibagi per bagian (Ringkasan, Leaderboard…)"], ["Animasi", "Sedang: peta konstelasi agent yang mengalir"],
             ["Cocok untuk", "Memahami cara kerja sistem & hasil riset"]] }
  ];
  var SIZES = [{ id: "90", label: "Kecil" }, { id: "100", label: "Normal" }, { id: "115", label: "Besar" }];
  var ALIAS = { "/index.html": "/", "/lab.html": "/lab", "/alt.html": "/alt", "/alt_abyss.html": "/alt-abyss",
    "/prism.html": "/prism", "/copy.html": "/copy", "/saham.html": "/saham" };

  function get(k, d) { try { var v = localStorage.getItem("momo." + k); return v == null ? d : v; } catch (e) { return d; } }
  function put(k, v) { try { localStorage.setItem("momo." + k, v); } catch (e) { /* mode privat: pilihan tidak diingat */ } }

  var path = ALIAS[location.pathname] || location.pathname.replace(/\/+$/, "") || "/";
  var here = { market: MARKETS[0], theme: "terminal" };
  MARKETS.forEach(function (m) {
    Object.keys(m.pages).forEach(function (t) { if (m.pages[t] === path) here = { market: m, theme: t }; });
  });

  // tema yang dipilih di Pengaturan berlaku di semua pasar yang punya tema itu
  var chosen = get("theme", null);
  if (chosen && chosen !== here.theme && here.market.pages[chosen]) {
    location.replace(here.market.pages[chosen] + location.search + location.hash);
    return;
  }

  var html = document.documentElement;
  function applyPrefs() {
    var z = get("size", "100");
    html.style.zoom = z === "100" ? "" : (Number(z) / 100);
    html.classList.toggle("momo-still", get("motion", "on") === "off");
  }
  applyPrefs();

  var F = "font:500 13px/1.35 'Segoe UI',system-ui,-apple-system,Roboto,sans-serif";
  var css = [
    ".momo-nav{position:relative;z-index:1000;display:flex;align-items:center;gap:12px;padding:0 14px;height:48px;",
    "background:#0c1015;border-bottom:1px solid #222b37;" + F + ";color:#e6edf3}",
    ".momo-nav *{box-sizing:border-box}.momo-nav svg{flex-shrink:0;display:block}",
    ".momo-brand{display:flex;align-items:center;gap:9px;white-space:nowrap;color:#e6edf3;text-decoration:none;padding-right:6px}",
    ".momo-brand i{width:26px;height:26px;border-radius:7px;display:grid;place-items:center;color:#06131a;",
    "background:linear-gradient(140deg,#5eead4,#38bdf8 55%,#818cf8)}",
    ".momo-brand b{font-weight:700;font-size:13.5px;letter-spacing:.01em}.momo-brand small{color:#7d8a99;font-weight:500;margin-left:3px}",
    ".momo-tabs{display:flex;align-items:stretch;gap:2px;flex:1;min-width:0;height:100%;overflow-x:auto;scrollbar-width:none}",
    ".momo-tabs::-webkit-scrollbar{display:none}",
    ".momo-tab{position:relative;display:flex;align-items:center;gap:8px;padding:0 13px;color:#8d99a6;text-decoration:none;white-space:nowrap}",
    ".momo-tab:hover{color:#e6edf3;background:#121821}",
    ".momo-tab.on{color:#f3f6f9}",
    ".momo-tab.on::after{content:'';position:absolute;left:10px;right:10px;bottom:0;height:2px;border-radius:2px 2px 0 0;background:#38bdf8}",
    ".momo-tab.on svg{color:#38bdf8}",
    ".momo-sim{font-size:10.5px;font-weight:700;letter-spacing:.08em;color:#fdba74;border:1px solid rgba(251,146,60,.45);",
    "background:rgba(251,146,60,.08);padding:3px 8px;border-radius:999px;white-space:nowrap}",
    ".momo-gear{display:flex;align-items:center;gap:7px;height:32px;padding:0 12px;border-radius:8px;border:1px solid #2a3442;",
    "background:#121821;color:#d6dee7;font:inherit;cursor:pointer;white-space:nowrap}",
    ".momo-gear:hover{border-color:#38bdf8;color:#fff}",
    "@media (max-width:760px){.momo-brand b,.momo-brand small,.momo-sim{display:none}}",
    "@media (max-width:640px){.momo-nav{gap:6px;padding:0 8px}.momo-gear span{display:none}.momo-gear{padding:0 8px}",
    ".momo-tab{padding:0 9px;gap:6px;font-size:12px}}",
    ".momo-veil{position:fixed;inset:0;z-index:1001;background:rgba(2,6,12,.6);display:none}",
    ".momo-veil.open{display:block}",
    ".momo-panel{position:fixed;top:0;right:0;bottom:0;width:min(430px,100%);z-index:1002;background:#0c1015;",
    "border-left:1px solid #222b37;color:#e6edf3;" + F.replace("13px", "14px") + ";",
    "transform:translateX(100%);transition:transform .22s ease;display:flex;flex-direction:column}",
    ".momo-panel.open{transform:none}",
    ".momo-panel header{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid #222b37}",
    ".momo-panel h2{margin:0;font-size:16px;font-weight:650;display:flex;align-items:center;gap:9px}",
    ".momo-x{background:none;border:0;color:#8d99a6;cursor:pointer;padding:4px;border-radius:6px}.momo-x:hover{color:#fff;background:#1a222d}",
    ".momo-body{padding:4px 18px 24px;overflow-y:auto}",
    ".momo-body h3{font-size:11px;letter-spacing:.1em;color:#7d8a99;margin:20px 0 10px;font-weight:700;text-transform:uppercase}",
    ".momo-theme{width:100%;text-align:left;margin-bottom:10px;border-radius:12px;border:1px solid #222b37;background:#10151c;",
    "color:#e6edf3;font:inherit;cursor:pointer;padding:0;overflow:hidden;display:block}",
    ".momo-theme:hover{border-color:#36465a}",
    ".momo-theme.on{border-color:#38bdf8;box-shadow:0 0 0 1px #38bdf8}",
    ".momo-theme .momo-tt{display:flex;gap:12px;padding:12px}",
    ".momo-theme .momo-tn{display:flex;align-items:center;gap:8px;font-weight:650;font-size:14.5px}",
    ".momo-theme .momo-tn em{font-style:normal;font-size:11px;font-weight:600;color:#38bdf8;display:flex;align-items:center;gap:3px}",
    ".momo-theme .momo-tg{color:#8d99a6;font-size:12.5px;margin-top:2px}",
    ".momo-theme .momo-ta{margin-top:6px;font-size:11.5px;color:#7d8a99}",
    ".momo-theme .momo-ta b{color:#c3ccd6;font-weight:600}",
    ".momo-theme dl{display:grid;grid-template-columns:92px 1fr;gap:4px 10px;margin:0;padding:10px 12px 12px;border-top:1px solid #1b232e;font-size:12.5px}",
    ".momo-theme dt{color:#7d8a99}.momo-theme dd{margin:0;color:#d0d8e0}",
    ".momo-pv{flex-shrink:0;width:118px;height:76px;border-radius:7px;overflow:hidden;position:relative;border:1px solid rgba(255,255,255,.08)}",
    ".momo-row{display:flex;gap:6px}",
    ".momo-opt{flex:1;padding:9px;border-radius:9px;border:1px solid #222b37;background:#10151c;color:#d6dee7;font:inherit;cursor:pointer}",
    ".momo-opt:hover{border-color:#36465a}.momo-opt.on{border-color:#38bdf8;color:#fff;background:#0f1d27}",
    ".momo-note{font-size:12.5px;color:#8d99a6;margin-top:8px}",
    ".momo-warn{font-size:12.5px;color:#fcd34d;background:rgba(250,204,21,.07);border:1px solid rgba(250,204,21,.25);border-radius:9px;padding:8px 10px;margin:-2px 0 10px}",
    "html.momo-still *,html.momo-still *::before,html.momo-still *::after{animation:none!important;transition:none!important}"
  ].join("");

  // pratinjau mini: kerangka halaman (bar, kartu, grafik) dengan warna & huruf asli tema itu
  function preview(p) {
    var bub = p.bubbles ? '<circle cx="96" cy="58" r="2" fill="none" stroke="' + p.a + '" stroke-opacity=".6"/><circle cx="104" cy="46" r="1.3" fill="none" stroke="' + p.a + '" stroke-opacity=".5"/><circle cx="90" cy="38" r="1" fill="' + p.a + '" fill-opacity=".5"/>' : "";
    var stroke = p.grad ? "url(#g" + p.a.slice(1) + ")" : p.a;
    var defs = p.grad ? '<defs><linearGradient id="g' + p.a.slice(1) + '" x1="0" x2="1"><stop offset="0" stop-color="#a78bfa"/><stop offset=".5" stop-color="#2dd4bf"/><stop offset="1" stop-color="#fbbf24"/></linearGradient></defs>' : "";
    var r = Math.min(p.radius, 6);
    return '<svg class="momo-pv" viewBox="0 0 118 76" style="width:118px;height:76px;background:' + p.bg + '" aria-hidden="true">' + defs +
      '<rect x="0" y="0" width="118" height="11" fill="' + p.panel + '"/>' +
      '<text x="5" y="8" font-size="6" font-family="' + p.font.replace(/"/g, "'") + '" fill="' + p.a + '" font-weight="700">MOMO</text>' +
      '<rect x="5" y="16" width="34" height="20" rx="' + r + '" fill="' + p.panel + '" stroke="' + p.line + '"/>' +
      '<rect x="9" y="21" width="16" height="3" rx="1" fill="' + p.a + '"/><rect x="9" y="28" width="24" height="2.5" rx="1" fill="' + p.line + '"/>' +
      '<rect x="43" y="16" width="34" height="20" rx="' + r + '" fill="' + p.panel + '" stroke="' + p.line + '"/>' +
      '<rect x="47" y="21" width="12" height="3" rx="1" fill="' + p.b + '"/><rect x="47" y="28" width="22" height="2.5" rx="1" fill="' + p.line + '"/>' +
      '<rect x="81" y="16" width="32" height="20" rx="' + r + '" fill="' + p.panel + '" stroke="' + p.line + '"/>' +
      '<rect x="85" y="21" width="14" height="3" rx="1" fill="' + p.a + '" fill-opacity=".7"/>' +
      '<rect x="5" y="40" width="108" height="31" rx="' + r + '" fill="' + p.panel + '" stroke="' + p.line + '"/>' +
      '<polyline points="10,64 22,58 32,61 44,50 56,54 68,45 80,48" fill="none" stroke="' + stroke + '" stroke-width="1.6" stroke-linejoin="round"/>' +
      bub + "</svg>";
  }

  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") e.textContent = attrs[k]; else if (k === "html") e.innerHTML = attrs[k];
      else if (k === "on") e.addEventListener("click", attrs[k]); else e.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (c) { if (c) e.appendChild(c); });
    return e;
  }

  function hrefFor(m) { return m.pages[get("theme", here.theme)] || m.pages[here.theme] || m.pages.terminal; }

  function build() {
    document.head.appendChild(el("style", { text: css }));
    var tabs = el("nav", { "class": "momo-tabs", "aria-label": "Pasar" }, MARKETS.map(function (m) {
      var a = el("a", { "class": "momo-tab" + (m === here.market ? " on" : ""), href: hrefFor(m), title: m.label,
        html: svg(m.id, 17) + "<span>" + m.label + "</span>" });
      if (m === here.market) a.setAttribute("aria-current", "page");
      return a;
    }));
    var gear = el("button", { "class": "momo-gear", type: "button", "aria-haspopup": "dialog", on: open,
      html: svg("settings", 17) + "<span>Pengaturan</span>" });
    var bar = el("div", { "class": "momo-nav", role: "banner" }, [
      el("a", { "class": "momo-brand", href: hrefFor(MARKETS[0]), html: "<i>" + svg("logo", 16) + "</i><b>Momo Quant Lab</b>" }),
      tabs, el("span", { "class": "momo-sim", title: "Semua angka adalah simulasi (paper trading), bukan uang sungguhan", text: "SIMULASI" }), gear
    ]);
    document.body.insertBefore(bar, document.body.firstChild);
    var cur = tabs.querySelector(".on");          // di layar sempit: geser supaya pasar yang aktif terlihat
    if (cur && tabs.scrollWidth > tabs.clientWidth) tabs.scrollLeft = cur.offsetLeft - tabs.offsetLeft - 8;

    var veil = el("div", { "class": "momo-veil", on: close });
    var body = el("div", { "class": "momo-body" });
    var xbtn = el("button", { "class": "momo-x", type: "button", "aria-label": "Tutup", html: svg("close", 20), on: close });
    var panel = el("aside", { "class": "momo-panel", role: "dialog", "aria-label": "Pengaturan", "aria-modal": "true" }, [
      el("header", {}, [el("h2", { html: svg("settings", 18) + "Pengaturan" }), xbtn]), body
    ]);
    document.body.appendChild(veil);
    document.body.appendChild(panel);

    function avail(t) {
      return MARKETS.filter(function (m) { return m.pages[t.id]; }).map(function (m) { return m.label; }).join(", ");
    }

    function render() {
      body.innerHTML = "";
      var theme = get("theme", here.theme);
      body.appendChild(el("h3", { text: "Tema tampilan" }));
      if (!here.market.pages[theme]) {
        body.appendChild(el("div", { "class": "momo-warn", text: here.market.label + " belum punya tema " +
          THEMES.filter(function (t) { return t.id === theme; })[0].label + ", jadi tampil dengan Terminal. Pilihan Anda tetap dipakai di pasar lain." }));
      }
      THEMES.forEach(function (t) {
        var dl = el("dl");
        t.spec.forEach(function (s) { dl.appendChild(el("dt", { text: s[0] })); dl.appendChild(el("dd", { text: s[1] })); });
        var info = el("div", {}, [
          el("div", { "class": "momo-tn", html: t.label + (t.id === theme ? '<em>' + svg("check", 13) + "dipakai</em>" : "") }),
          el("div", { "class": "momo-tg", text: t.tagline }),
          el("div", { "class": "momo-ta", html: "Tersedia di: <b>" + avail(t) + "</b>" })
        ]);
        body.appendChild(el("button", {
          "class": "momo-theme" + (t.id === theme ? " on" : ""), type: "button", "aria-pressed": String(t.id === theme),
          on: function () {
            put("theme", t.id);
            if (here.market.pages[t.id] && t.id !== here.theme) location.href = here.market.pages[t.id] + location.hash;
            else render();
          }
        }, [el("div", { "class": "momo-tt", html: preview(t.pv) }, [info]), dl]));
      });
      body.appendChild(el("h3", { text: "Ukuran tampilan" }));
      var size = get("size", "100");
      body.appendChild(el("div", { "class": "momo-row" }, SIZES.map(function (s) {
        return el("button", { "class": "momo-opt" + (s.id === size ? " on" : ""), type: "button", text: s.label,
          on: function () { put("size", s.id); applyPrefs(); render(); } });
      })));
      body.appendChild(el("h3", { text: "Animasi" }));
      var motion = get("motion", "on");
      body.appendChild(el("div", { "class": "momo-row" }, [["on", "Nyala"], ["off", "Kurangi"]].map(function (o) {
        return el("button", { "class": "momo-opt" + (o[0] === motion ? " on" : ""), type: "button", text: o[1],
          on: function () { put("motion", o[0]); applyPrefs(); render(); } });
      })));
      body.appendChild(el("div", { "class": "momo-note", text: "\"Kurangi\" menghentikan animasi ikon dan kartu. Latar laut tema Abyss tetap bergerak." }));
      body.appendChild(el("h3", { text: "Tentang" }));
      body.appendChild(el("div", { "class": "momo-note", text: "Semua pasar adalah paper trading: tidak ada uang sungguhan. Pengaturan disimpan di browser ini saja." }));
    }

    function open() { render(); veil.classList.add("open"); panel.classList.add("open"); xbtn.focus(); }
    function close() { veil.classList.remove("open"); panel.classList.remove("open"); gear.focus(); }
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && panel.classList.contains("open")) close(); });
  }

  if (document.body) build(); else document.addEventListener("DOMContentLoaded", build);
})();
