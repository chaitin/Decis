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
 *   <a data-back-link></a>                          <!-- "back to the playground" -->
 *   <a data-repo-link></a>                          <!-- this project on GitHub -->
 *   <a data-docs-link></a>                          <!-- the API reference, in the repo -->
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

  //: The project this playground ships with. The navigation link is built here rather
  //: than written into four pages, so the repository URL has one home (AGENTS.md §2) --
  //: the same reason the palette lives in theme.css.
  var REPO_URL = "https://github.com/chaitin/Decis";

  //: The links every page shows, and the string that labels each one. `back` is the one
  //: page-specific exception: the index has nothing to go back to, so it omits the
  //: element and this mount finds nothing.
  var LINKS = [
    { attribute: "data-back-link", href: "/", key: "nav.back", className: "link-back" },
    { attribute: "data-repo-link", href: REPO_URL, key: "nav.repo", className: "link-repo", external: true },
    {
      attribute: "data-docs-link",
      href: REPO_URL + "/blob/master/docs/api.md",
      key: "nav.docs",
      className: "link-docs",
      external: true
    }
  ];
  var LANGS = [
    { code: "en", label: "EN", title: "English" },
    { code: "zh", label: "\u4e2d\u6587", title: "\u7b80\u4f53\u4e2d\u6587" }
  ];

  // The shell: the app bar and the status chip, the game bar with its manual/AI switch,
  // the inference panel and the I/O console. Everything the three game pages say in common
  // is here; a page's own strings go in its `I18N.add(...)` call.
  var SHELL = {
    en: {
      "brand.sub": "playground",
      "nav.snake": "Snake",
      "nav.dino": "Dino",
      "nav.tetris": "Tetris",
      "nav.api": "API",
      "status.searching": "Looking for the engine\u2026",
      "status.ready": "Connected to {engine}",
      "status.unreachable": "Playground unreachable: {message}",
      "status.loading": "The engine is still loading\u2026",
      "status.idle": "Idle",
      "switch.label": "Language",
      "mode.label": "Play mode",
      "mode.title": "Manual play or the model (M)",
      "mode.manual": "Manual",
      "mode.ai": "AI",
      "btn.start": "Start",
      "btn.pause": "Pause",
      "btn.reset": "Reset",
      "btn.start.title": "Start or pause the game",
      "btn.reset.title": "New game",
      "tele.title": "Inference",
      "tele.idle": "no call yet",
      "tele.latency": "Latency",
      "tele.p50": "p50",
      "tele.p95": "p95",
      "tele.tokens": "Tokens in \u2192 out",
      "tele.rate": "Throughput",
      "tele.rate.title": "Total tokens \u00f7 round-trip time, so it includes queueing and the network",
      "tele.calls": "Calls",
      "tele.ms": "{value} ms",
      "tele.rate.val": "{value} tok/s",
      "tele.tokens.val": "{input} \u2192 {output}",
      "tele.calls.val": "{calls} \u00b7 {failed} failed",
      "tele.failure": "failed call: {message}",
      "io.title": "Last /v1/systemone call",
      "io.idle": "No call yet \u2014 start the game, or switch to AI.",
      "io.request": "Request",
      "io.response": "Response",
      "io.error": "error",
      "io.meta": "{status} \u00b7 {ms} ms \u00b7 {tokens} tok",
      "nav.back": "Back to playground",
      "nav.repo": "Decis",
      "nav.repo.title": "Decis on GitHub",
      "nav.repo.aria": "Decis on GitHub (opens in a new tab)",
      "nav.docs": "API reference",
      "nav.docs.title": "The full API reference in the repository",
      "nav.docs.aria": "The full API reference in the repository (opens in a new tab)",
      "foot.adapted": "Games adapted from",
    },
    zh: {
      "brand.sub": "\u6f14\u793a\u573a",
      "nav.snake": "\u8d2a\u5403\u86c7",
      "nav.dino": "\u6050\u9f99",
      "nav.tetris": "\u4fc4\u7f57\u65af\u65b9\u5757",
      "nav.api": "API",
      "status.searching": "\u6b63\u5728\u5bfb\u627e\u5f15\u64ce\u2026",
      "status.ready": "\u5df2\u8fde\u63a5 {engine}",
      "status.unreachable": "\u65e0\u6cd5\u8bbf\u95ee playground\uff1a{message}",
      "status.loading": "\u5f15\u64ce\u8fd8\u5728\u52a0\u8f7d\u4e2d\u2026",
      "status.idle": "\u7a7a\u95f2",
      "switch.label": "\u8bed\u8a00",
      "mode.label": "\u73a9\u6cd5",
      "mode.title": "\u624b\u52a8\u73a9\uff0c\u6216\u4ea4\u7ed9\u6a21\u578b\uff08M\uff09",
      "mode.manual": "\u624b\u52a8",
      "mode.ai": "AI \u6258\u7ba1",
      "btn.start": "\u5f00\u59cb",
      "btn.pause": "\u6682\u505c",
      "btn.reset": "\u91cd\u7f6e",
      "btn.start.title": "\u5f00\u59cb\u6216\u6682\u505c\u6e38\u620f",
      "btn.reset.title": "\u91cd\u65b0\u5f00\u59cb",
      "tele.title": "\u63a8\u7406\u72b6\u6001",
      "tele.idle": "\u8fd8\u6ca1\u6709\u8c03\u7528",
      "tele.latency": "\u5ef6\u8fdf",
      "tele.p50": "p50",
      "tele.p95": "p95",
      "tele.tokens": "Token \u8f93\u5165 \u2192 \u8f93\u51fa",
      "tele.rate": "\u541e\u5410",
      "tele.rate.title": "\u603b token \u00f7 \u5f80\u8fd4\u8017\u65f6\uff0c\u56e0\u6b64\u5305\u542b\u6392\u961f\u4e0e\u7f51\u7edc",
      "tele.calls": "\u8c03\u7528",
      "tele.ms": "{value} \u6beb\u79d2",
      "tele.rate.val": "{value} token/\u79d2",
      "tele.tokens.val": "\u8f93\u5165 {input} \u2192 \u8f93\u51fa {output}",
      "tele.calls.val": "{calls} \u00b7 \u5931\u8d25 {failed}",
      "tele.failure": "\u5931\u8d25\u7684\u8c03\u7528\uff1a{message}",
      "io.title": "\u6700\u8fd1\u4e00\u6b21 /v1/systemone \u8c03\u7528",
      "io.idle": "\u8fd8\u6ca1\u6709\u8c03\u7528 \u2014\u2014 \u5f00\u59cb\u6e38\u620f\uff0c\u6216\u5207\u5230 AI\u3002",
      "io.request": "\u8bf7\u6c42",
      "io.response": "\u54cd\u5e94",
      "io.error": "\u51fa\u9519",
      "io.meta": "{status} \u00b7 {ms} \u6beb\u79d2 \u00b7 {tokens} tok",
      "nav.back": "\u8fd4\u56de\u6f14\u793a\u573a",
      "nav.repo": "Decis",
      "nav.repo.title": "Decis \u7684 GitHub \u4ed3\u5e93",
      "nav.repo.aria": "Decis \u7684 GitHub \u4ed3\u5e93\uff08\u65b0\u6807\u7b7e\u9875\u6253\u5f00\uff09",
      "nav.docs": "API \u8bf4\u660e",
      "nav.docs.title": "\u4ed3\u5e93\u91cc\u7684\u5b8c\u6574 API \u53c2\u8003",
      "nav.docs.aria": "\u4ed3\u5e93\u91cc\u7684\u5b8c\u6574 API \u53c2\u8003\uff08\u65b0\u6807\u7b7e\u9875\u6253\u5f00\uff09",
      "foot.adapted": "\u6e38\u620f\u6539\u7f16\u81ea",
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
   * `data-i18n` sets the text, `data-i18n-title` / `-aria-label` / `-placeholder` / `-alt` set
   * that attribute (the last one is what a screen reader reads out for the screenshots on the
   * index). Text is assigned with `textContent`, never `innerHTML`: strings come from this
   * file, but a translation is still not markup.
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
      ["data-i18n-placeholder", "placeholder"],
      ["data-i18n-alt", "alt"]
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

  /** Fill in the shared navigation links: the way back, and the repository. */
  function mountLinks(root) {
    var scope = root || document;
    for (var s = 0; s < LINKS.length; s++) {
      var spec = LINKS[s];
      var nodes = scope.querySelectorAll("a[" + spec.attribute + "]");
      for (var i = 0; i < nodes.length; i++) {
        var node = nodes[i];
        node.setAttribute("href", spec.href);
        node.classList.add(spec.className);
        if (spec.external) {
          node.setAttribute("target", "_blank");
          node.setAttribute("rel", "noopener");
          node.setAttribute("title", t(spec.key + ".title"));
          node.setAttribute("aria-label", t(spec.key + ".aria"));
        }
        node.textContent = t(spec.key);
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
    mountLinks(document);
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
    mountLinks(document);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ready);
  } else {
    ready();
  }
})();
