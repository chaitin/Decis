/* The playground's i18n: detection, lookup, the language switch, and nothing else.
 *
 * Loaded in `<head>` by every page, before the page's own script, so that `I18N` exists
 * by the time a page calls `I18N.add(...)`. The mechanism lives here once; each page
 * carries its own strings and declares them with `add()`, which keeps a page's copy next
 * to the markup that uses it instead of in a fourth place (AGENTS.md §2).
 *
 * What is translated is the *interface*. What is sent to the model is not: the state,
 * the instructions and the criteria stay English, because that is the language the
 * prompts are written in and because translating them would change the request and the
 * token budget the pages were sized against (see README, "the three games"). A page that
 * wants to show a localised label for a wire value maps it for display only -- the value
 * on the wire is unchanged.
 *
 * Usage in a page:
 *
 *   <link rel="stylesheet" href="/theme.css">
 *   <script src="/i18n.js"></script>
 *   ...
 *   <span data-i18n="score">Score</span>            <!-- static text -->
 *   <button data-i18n-title="btn.reset.title">…</button>
 *   <div data-lang-switch></div>                    <!-- the switch mounts itself -->
 *   <script>
 *     I18N.add({ en: { "score": "Score" }, zh: { "score": "得分" } });
 *     I18N.onChange(() => renderLabels());          // re-render dynamic text
 *     el.textContent = I18N.t("status.ready", { engine: "laya-multilingual" });
 *   </script>
 *
 * For a language with no entry the lookup falls back to English, then to the key itself,
 * so a missing string shows up as an obvious `dotted.key` rather than as a blank.
 */

(function () {
  "use strict";

  var STORAGE_KEY = "decis-playground-lang";
  var LANGS = [
    { code: "en", label: "EN", title: "English" },
    { code: "zh", label: "\u4e2d\u6587", title: "\u7b80\u4f53\u4e2d\u6587" }
  ];

  // The shell: the app bar and the status chip, which every page shows. A page's own
  // strings go in its `I18N.add(...)` call.
  var SHELL = {
    en: {
      "brand.sub": "playground",
      "nav.snake": "Snake",
      "nav.dino": "Dino",
      "nav.tetris": "Tetris",
      "status.searching": "Looking for the engine\u2026",
      "status.ready": "Connected to {engine}",
      "status.unreachable": "Playground unreachable: {message}",
      "status.loading": "The engine is still loading\u2026",
      "switch.label": "Language",
      "foot.adapted": "Games adapted from",
      "foot.docs": "The API is documented in the",
      "foot.repo": "Decis",
      "foot.proxied": "is proxied, and",
      "foot.readyz": "reports the engine."
    },
    zh: {
      "brand.sub": "\u6f14\u793a\u573a",
      "nav.snake": "\u8d2a\u5403\u86c7",
      "nav.dino": "\u6050\u9f99",
      "nav.tetris": "\u4fc4\u7f57\u65af\u65b9\u5757",
      "status.searching": "\u6b63\u5728\u5bfb\u627e\u5f15\u64ce\u2026",
      "status.ready": "\u5df2\u8fde\u63a5 {engine}",
      "status.unreachable": "\u65e0\u6cd5\u8bbf\u95ee playground\uff1a{message}",
      "status.loading": "\u5f15\u64ce\u8fd8\u5728\u52a0\u8f7d\u4e2d\u2026",
      "switch.label": "\u8bed\u8a00",
      "foot.adapted": "\u6e38\u620f\u6539\u7f16\u81ea",
      "foot.docs": "API \u6587\u6863\u5728",
      "foot.repo": "Decis",
      "foot.proxied": "\u4f1a\u88ab\u4ee3\u7406\uff0c",
      "foot.readyz": "\u4f1a\u62a5\u544a\u5f15\u64ce\u72b6\u6001\u3002"
    }
  };

  var dict = { en: assign({}, SHELL.en), zh: assign({}, SHELL.zh) };
  var listeners = [];
  var warned = {};

  function assign(target, source) {
    for (var key in source) {
      if (Object.prototype.hasOwnProperty.call(source, key)) target[key] = source[key];
    }
    return target;
  }

  /** "zh-CN", "zh-Hans-CN", "zh-TW" -> "zh"; anything else the browser says -> "en". */
  function normalise(tag) {
    if (!tag) return null;
    var lower = String(tag).toLowerCase();
    if (lower.indexOf("zh") === 0) return "zh";
    if (lower.indexOf("en") === 0) return "en";
    return null;
  }

  function detect() {
    var fromQuery = null;
    try {
      fromQuery = normalise(new URLSearchParams(window.location.search).get("lang"));
    } catch (error) {
      fromQuery = null;
    }
    if (fromQuery) return fromQuery;

    try {
      var stored = normalise(window.localStorage.getItem(STORAGE_KEY));
      if (stored) return stored;
    } catch (error) {
      /* private mode, or storage disabled: fall through to the browser's preference */
    }

    var tags = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language];
    for (var i = 0; i < tags.length; i++) {
      var found = normalise(tags[i]);
      if (found) return found;
    }
    return "en";
  }

  var current = detect();

  document.documentElement.lang = current === "zh" ? "zh-CN" : "en";

  function lookup(key, lang) {
    if (dict[lang] && Object.prototype.hasOwnProperty.call(dict[lang], key)) return dict[lang][key];
    if (Object.prototype.hasOwnProperty.call(dict.en, key)) return dict.en[key];
    return null;
  }

  /** `t("status.ready", {engine: "laya"})` -> "Connected to laya". */
  function t(key, vars) {
    var value = lookup(key, current);
    if (value === null) {
      if (!warned[key]) {
        warned[key] = true;
        if (window.console && console.warn) console.warn("i18n: no string for " + JSON.stringify(key));
      }
      return key;
    }
    if (!vars) return value;
    return value.replace(/\{(\w+)\}/g, function (match, name) {
      return Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match;
    });
  }

  function add(strings) {
    var langs = Object.keys(strings || {});
    for (var i = 0; i < langs.length; i++) {
      var lang = langs[i];
      if (!dict[lang]) dict[lang] = {};
      assign(dict[lang], strings[lang]);
    }
  }

  /**
   * Fill in every tagged node under `root`.
   *
   * `data-i18n` sets the text, `data-i18n-title` / `-aria-label` / `-placeholder` set that
   * attribute. Text is assigned with `textContent`, never `innerHTML`: strings come from
   * this file, but a translation is still not markup.
   */
  function apply(root) {
    var scope = root || document;
    var nodes = scope.querySelectorAll("[data-i18n]");
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].textContent = t(nodes[i].getAttribute("data-i18n"));
    }
    var attributes = [
      ["data-i18n-title", "title"],
      ["data-i18n-aria-label", "aria-label"],
      ["data-i18n-placeholder", "placeholder"]
    ];
    for (var a = 0; a < attributes.length; a++) {
      var found = scope.querySelectorAll("[" + attributes[a][0] + "]");
      for (var j = 0; j < found.length; j++) {
        found[j].setAttribute(attributes[a][1], t(found[j].getAttribute(attributes[a][0])));
      }
    }
  }

  function mount(root) {
    var scope = root || document;
    var hosts = scope.querySelectorAll("[data-lang-switch]");
    for (var i = 0; i < hosts.length; i++) {
      var host = hosts[i];
      if (host.getAttribute("data-lang-mounted") === "1") continue;
      host.setAttribute("data-lang-mounted", "1");
      host.setAttribute("role", "group");
      host.setAttribute("aria-label", t("switch.label"));
      host.className = host.className ? host.className + " lang-switch" : "lang-switch";
      for (var l = 0; l < LANGS.length; l++) {
        (function (spec) {
          var button = document.createElement("button");
          button.type = "button";
          button.className = "lang-btn";
          button.textContent = spec.label;
          button.title = spec.title;
          button.setAttribute("aria-pressed", String(spec.code === current));
          button.addEventListener("click", function () {
            set(spec.code);
          });
          host.appendChild(button);
        })(LANGS[l]);
      }
    }
  }

  /** Repaint every mount point: called after the language changes. */
  function repaint() {
    var hosts = document.querySelectorAll("[data-lang-switch]");
    for (var i = 0; i < hosts.length; i++) {
      var buttons = hosts[i].querySelectorAll(".lang-btn");
      for (var b = 0; b < buttons.length; b++) {
        var code = LANGS[b] && LANGS[b].code;
        buttons[b].setAttribute("aria-pressed", String(code === current));
      }
      hosts[i].setAttribute("aria-label", t("switch.label"));
    }
  }

  function notify() {
    for (var i = 0; i < listeners.length; i++) {
      try {
        listeners[i](current);
      } catch (error) {
        if (window.console && console.error) console.error("i18n: listener failed", error);
      }
    }
  }

  function set(lang) {
    var next = normalise(lang) || "en";
    if (next === current) {
      repaint();
      return;
    }
    current = next;
    try {
      window.localStorage.setItem(STORAGE_KEY, current);
    } catch (error) {
      /* the choice then lasts for this page only, which is better than failing */
    }
    document.documentElement.lang = current === "zh" ? "zh-CN" : "en";
    // `apply` first, so a listener that reads the DOM sees the new text.
    apply(document);
    repaint();
    notify();
  }

  function onChange(fn) {
    if (typeof fn === "function") listeners.push(fn);
  }

  window.I18N = {
    t: t,
    add: add,
    apply: apply,
    mount: mount,
    onChange: onChange,
    set: set,
    languages: LANGS,
    get lang() {
      return current;
    }
  };

  function ready() {
    apply(document);
    mount(document);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ready);
  } else {
    ready();
  }
})();
