/**
 * Zero Agent UI enhancements
 *
 * 1. Copy button — per-message "📋 העתק" via Clipboard API
 * 2. Sidebar folders — groups threads by emoji prefix into
 *    collapsible sections: 🔬 AI Research / 🤖 Sub-agents / 💬 שיחות
 */
(function () {
  "use strict";

  /* ── 1. COPY BUTTON ─────────────────────────────────────────────────── */

  var BTN_CLASS = "za-copy-btn";
  var MSG_SELECTOR = '[data-step-type="assistant_message"]';

  function textOf(container) {
    var clone = container.cloneNode(true);
    clone.querySelectorAll("." + BTN_CLASS).forEach(function (b) { b.remove(); });
    return (clone.innerText || "").trim();
  }

  function makeCopyButton(container) {
    var btn = document.createElement("button");
    btn.className = BTN_CLASS;
    btn.type = "button";
    btn.textContent = "📋 העתק";
    btn.title = "העתק את התשובה";
    btn.style.cssText = [
      "display:inline-flex","align-items:center","gap:4px","margin-top:8px",
      "font-size:12px","line-height:1.4","padding:2px 10px","border-radius:6px",
      "border:1px solid rgba(127,127,127,0.35)","background:transparent",
      "color:inherit","cursor:pointer","opacity:0.65","transition:opacity 0.15s",
    ].join(";");
    btn.addEventListener("mouseenter", function () { btn.style.opacity = "1"; });
    btn.addEventListener("mouseleave", function () { btn.style.opacity = "0.65"; });
    btn.addEventListener("click", function () {
      navigator.clipboard.writeText(textOf(container)).then(
        function () {
          var prev = btn.textContent;
          btn.textContent = "✅ הועתק";
          setTimeout(function () { btn.textContent = prev; }, 1500);
        },
        function () { btn.textContent = "❌ נכשל"; }
      );
    });
    return btn;
  }

  function enhanceCopy() {
    document.querySelectorAll(MSG_SELECTOR).forEach(function (el) {
      if (el.dataset.zaCopy) return;
      if (!(el.innerText || "").trim()) return;
      el.dataset.zaCopy = "1";
      el.appendChild(makeCopyButton(el));
    });
  }

  /* ── 2. SIDEBAR FOLDERS ─────────────────────────────────────────────── */

  var FOLDERS = [
    { prefix: "🔬", id: "za-folder-research", label: "🔬 AI Research",   color: "#2dd4bf" },
    { prefix: "🤖", id: "za-folder-subagent", label: "🤖 Sub-agents",    color: "#a78bfa" },
  ];
  var DEFAULT_FOLDER = { id: "za-folder-chats", label: "💬 שיחות", color: "#94a3b8" };

  var FOLDER_STYLE = [
    "display:flex","align-items:center","gap:6px",
    "font-size:11px","font-weight:600","letter-spacing:0.06em",
    "text-transform:uppercase","padding:10px 12px 4px",
    "opacity:0.7","cursor:pointer","user-select:none",
  ].join(";");

  var INJECT_STYLE = `
    .za-folder-section { margin-bottom: 2px; }
    .za-folder-header  { display:flex; align-items:center; gap:6px;
                         font-size:11px; font-weight:600; letter-spacing:.06em;
                         text-transform:uppercase; padding:10px 12px 4px;
                         opacity:.7; cursor:pointer; user-select:none; }
    .za-folder-header:hover { opacity:1; }
    .za-folder-arrow   { transition:transform .2s; display:inline-block; }
    .za-folder-body    { overflow:hidden; transition:max-height .25s ease; }
    .za-folder-body.collapsed { max-height:0 !important; }
    .za-folder-divider { border:none; border-top:1px solid rgba(255,255,255,.08);
                         margin:4px 10px; }
  `;

  function injectFolderStyles() {
    if (document.getElementById("za-folder-style")) return;
    var s = document.createElement("style");
    s.id = "za-folder-style";
    s.textContent = INJECT_STYLE;
    document.head.appendChild(s);
  }

  function makeFolder(def) {
    var section = document.createElement("div");
    section.className = "za-folder-section";
    section.dataset.zaFolder = def.id;

    var header = document.createElement("div");
    header.className = "za-folder-header";
    header.style.color = def.color || "inherit";

    var arrow = document.createElement("span");
    arrow.className = "za-folder-arrow";
    arrow.textContent = "▾";

    var label = document.createElement("span");
    label.textContent = def.label;

    header.appendChild(arrow);
    header.appendChild(label);

    var body = document.createElement("div");
    body.className = "za-folder-body";
    body.style.maxHeight = "2000px";

    // Toggle collapse
    var collapsed = false;
    header.addEventListener("click", function () {
      collapsed = !collapsed;
      if (collapsed) {
        body.classList.add("collapsed");
        arrow.style.transform = "rotate(-90deg)";
      } else {
        body.style.maxHeight = "2000px";
        body.classList.remove("collapsed");
        arrow.style.transform = "";
      }
    });

    section.appendChild(header);
    section.appendChild(body);
    return section;
  }

  function organizeSidebar() {
    // Find Chainlit's thread list — it's a nav or aside with thread links.
    // Thread items have class "relative h-9 group/thread"
    var threadItems = document.querySelectorAll('[class*="group/thread"]');
    if (!threadItems.length) return;

    // Find the common parent container
    var container = threadItems[0].parentElement;
    if (!container) return;

    // Avoid re-processing if already organized this exact snapshot
    var currentCount = threadItems.length;
    if (container.dataset.zaCount === String(currentCount)) return;
    container.dataset.zaCount = String(currentCount);

    injectFolderStyles();

    // Remove existing folder wrappers (stale after re-render)
    container.querySelectorAll(".za-folder-section, .za-folder-divider")
      .forEach(function (el) { el.remove(); });

    // Categorize threads
    var buckets = {};
    FOLDERS.forEach(function (f) { buckets[f.id] = []; });
    buckets[DEFAULT_FOLDER.id] = [];

    threadItems.forEach(function (item) {
      var text = (item.textContent || "").trim();
      var matched = false;
      for (var i = 0; i < FOLDERS.length; i++) {
        if (text.startsWith(FOLDERS[i].prefix)) {
          buckets[FOLDERS[i].id].push(item);
          matched = true;
          break;
        }
      }
      if (!matched) buckets[DEFAULT_FOLDER.id].push(item);
    });

    // Only render folders that have items
    var allDefs = FOLDERS.concat([DEFAULT_FOLDER]);
    allDefs.forEach(function (def) {
      var items = buckets[def.id];
      if (!items || !items.length) return;

      var section = makeFolder(def);
      var body = section.querySelector(".za-folder-body");
      items.forEach(function (item) { body.appendChild(item); });
      container.appendChild(section);
    });
  }

  /* ── Bootstrap ──────────────────────────────────────────────────────── */

  var _rafPending = false;

  function onMutation() {
    if (_rafPending) return;
    _rafPending = true;
    window.requestAnimationFrame(function () {
      _rafPending = false;
      enhanceCopy();
      organizeSidebar();
    });
  }

  function start() {
    enhanceCopy();
    organizeSidebar();
    new MutationObserver(onMutation)
      .observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
