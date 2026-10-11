/*
 * Prefix every *arr tab title with "Monarch · ".
 *
 * The apps set their own title from the route ("Movies - Radarr"), and a
 * stylesheet cannot touch that, so this is a tiny observer that keeps the
 * prefix on top of whatever the SPA writes. Injected beside arr-theme.css by
 * scripts/npm-theme.py; it owns exactly one fact — the browser tab's text.
 */
(function () {
  "use strict";
  var PREFIX = "Monarch \u00b7 ";

  function fix() {
    var title = document.title || "";
    if (title && title.indexOf(PREFIX) !== 0) {
      document.title = PREFIX + title.replace(/^Monarch\s*(?:·|Â·|\.)?\s*/, "");
    }
  }

  fix();
  var el = document.querySelector("title");
  if (el && window.MutationObserver) {
    new MutationObserver(fix).observe(el, { childList: true });
  }
  window.addEventListener("load", fix);
  window.addEventListener("hashchange", fix);
})();
