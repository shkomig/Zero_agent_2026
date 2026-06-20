/* Zero Agent — per-message "copy" button.
 *
 * Chainlit's cl.Action callbacks run in Python and can't reach the browser
 * clipboard, so we add the button on the frontend and copy via the Clipboard
 * API. This works because the app is served on localhost, which counts as a
 * secure context. A MutationObserver re-runs on every DOM change so new
 * messages (including streamed ones) get a button too.
 *
 * Targets `[data-step-type="assistant_message"]` — the stable data attribute
 * Chainlit puts on each assistant message step (verified in the 2.11.1 build).
 * If a future Chainlit version changes it, only MSG_SELECTOR below needs an
 * update.
 */
(function () {
  "use strict";

  var BTN_CLASS = "za-copy-btn";
  var MSG_SELECTOR = '[data-step-type="assistant_message"]';

  function textOf(container) {
    // Read the message text WITHOUT our own button(s).
    var clone = container.cloneNode(true);
    clone.querySelectorAll("." + BTN_CLASS).forEach(function (b) {
      b.remove();
    });
    return (clone.innerText || "").trim();
  }

  function makeButton(container) {
    var btn = document.createElement("button");
    btn.className = BTN_CLASS;
    btn.type = "button";
    btn.textContent = "📋 העתק";
    btn.title = "העתק את התשובה";
    btn.style.cssText = [
      "display:inline-flex",
      "align-items:center",
      "gap:4px",
      "margin-top:8px",
      "font-size:12px",
      "line-height:1.4",
      "padding:2px 10px",
      "border-radius:6px",
      "border:1px solid rgba(127,127,127,0.35)",
      "background:transparent",
      "color:inherit",
      "cursor:pointer",
      "opacity:0.65",
      "transition:opacity 0.15s",
    ].join(";");
    btn.addEventListener("mouseenter", function () {
      btn.style.opacity = "1";
    });
    btn.addEventListener("mouseleave", function () {
      btn.style.opacity = "0.65";
    });
    btn.addEventListener("click", function () {
      var text = textOf(container);
      navigator.clipboard.writeText(text).then(
        function () {
          var prev = btn.textContent;
          btn.textContent = "✅ הועתק";
          setTimeout(function () {
            btn.textContent = prev;
          }, 1500);
        },
        function () {
          btn.textContent = "❌ נכשל";
        }
      );
    });
    return btn;
  }

  function enhance() {
    document.querySelectorAll(MSG_SELECTOR).forEach(function (el) {
      if (el.dataset.zaCopy) return; // already has a button
      // Skip empty / still-streaming-empty containers.
      if (!(el.innerText || "").trim()) return;
      el.dataset.zaCopy = "1";
      el.appendChild(makeButton(el));
    });
  }

  function start() {
    enhance();
    var observer = new MutationObserver(function () {
      // Cheap debounce: schedule one enhance per animation frame.
      window.requestAnimationFrame(enhance);
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
