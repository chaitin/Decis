/* The playground's game shell: the chrome the three games share.
 *
 * snake, dino and tetris are three boards around one API call, so the things that are not
 * the board are the same on all three and live here once (AGENTS.md §2):
 *
 *   * the **manual / AI switch** -- one control, the same two words, on every page;
 *   * the **inference panel** -- latency, tokens, throughput and the call count of the
 *     session, fed by `observe()` after every `/v1/systemone` call;
 *   * the **I/O console** -- the last request and the last response as JSON, side by side
 *     behind a summary that starts open: the call is the thing this playground exists to
 *     show, and the page puts it under the board rather than beside it for that reason.
 *     Collapsing it is one click for someone who would rather just watch the game;
 *   * the **status dot/text** in the game bar, the **engine chip** in the app bar, and the
 *     keyboard shortcuts (space, R, M) that go with them.
 *
 * What stays in a page is the game: its rules, its canvas, its questions, its own status
 * strings and its own `state`/`instructions`/`criteria`. This file never touches a payload.
 *
 * The telemetry is measured, not modelled. `latencyMs` is wall clock around the `fetch` --
 * so it includes queueing on the engine and the network, and it is labelled "latency"
 * rather than "model time" for that reason. Throughput is `(input + output) tokens / that
 * round trip`; it is an end-to-end number, not a decode rate, and the tooltip says so.
 * `usage` comes from the response, so an error carries no token count.
 *
 * Usage in a page:
 *
 *   <div data-ai-switch></div>                        <!-- filled here -->
 *   <section class="panel" data-telemetry></section>  <!-- filled here -->
 *   <details class="panel io-console" data-io-console></details>
 *   ...
 *   <script src="/game.js"></script>
 *   <script>
 *     GameShell.init({
 *       game: "snake",
 *       onStart: toggle, onReset: resetGame, onMode: applyMode
 *     });
 *     GameShell.observe({ request: body, response: data, status: 200, latencyMs: 512 });
 *   </script>
 *
 * `init` also paints `#btn-start`, `#btn-reset`, `#game-dot` and `#game-status-text`, so a
 * page only has to provide the elements and the state strings.
 */

(function () {
  "use strict";

  //: How many latencies the p50/p95 describe. One session of a game is a few hundred
  //: calls; a window that never forgets would turn the percentiles into a long-run
  //: average that stops answering "is it slow right now".
  var WINDOW = 200;

  var shell = { game: "", mode: "ai", running: false, options: {} };
  var state = { statusKey: "status.idle", statusVars: null, statusKind: "", noteKey: "", noteVars: null };
  var tele = { calls: 0, failed: 0, latencies: [], last: null, tokensIn: 0, tokensOut: 0 };
  var last = null; // the last call: {request, response, error, status, latencyMs, live}
  var chip = null; // the last /api/config answer, kept so a language switch can repaint it

  function $(id) {
    return document.getElementById(id);
  }

  function setText(node, value) {
    if (node) node.textContent = value;
  }

  function percentile(values, q) {
    if (!values.length) return null;
    var sorted = values.slice().sort(function (a, b) { return a - b; });
    return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))];
  }

  function isNumber(value) {
    return typeof value === "number" && isFinite(value);
  }

  /* ------------------------------------------------------------------ mode -- */

  function mountSwitch() {
    var host = document.querySelector("[data-ai-switch]");
    if (!host) return;
    host.classList.add("seg");
    host.setAttribute("role", "group");
    ["manual", "ai"].forEach(function (mode) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "seg-btn";
      button.setAttribute("data-mode", mode);
      button.addEventListener("click", function () {
        setMode(mode);
      });
      host.appendChild(button);
    });
  }

  function paintSwitch() {
    var host = document.querySelector("[data-ai-switch]");
    if (!host) return;
    host.setAttribute("aria-label", I18N.t("mode.label"));
    host.setAttribute("title", I18N.t("mode.title"));
    var buttons = host.querySelectorAll(".seg-btn");
    for (var i = 0; i < buttons.length; i++) {
      var mode = buttons[i].getAttribute("data-mode");
      buttons[i].textContent = I18N.t("mode." + mode);
      buttons[i].setAttribute("aria-pressed", String(mode === shell.mode));
    }
  }

  function setMode(next) {
    var mode = next === "manual" ? "manual" : "ai";
    if (mode === shell.mode) {
      paintSwitch();
      return;
    }
    shell.mode = mode;
    paintSwitch();
    if (shell.options.onMode) shell.options.onMode(mode);
  }

  /* ------------------------------------------------------- bar and buttons -- */

  function setStatus(key, vars, kind) {
    state.statusKey = key || "status.idle";
    state.statusVars = vars || null;
    state.statusKind = kind || "";
    paintStatus();
  }

  function paintStatus() {
    setText($("game-status-text"), I18N.t(state.statusKey, state.statusVars));
    var dot = $("game-dot");
    if (dot) dot.className = "dot" + (state.statusKind ? " " + state.statusKind : "");
  }

  function setRunning(value) {
    shell.running = Boolean(value);
    paintStartButton();
  }

  function paintStartButton() {
    var button = $("btn-start");
    if (!button) return;
    button.textContent = I18N.t(shell.running ? "btn.pause" : "btn.start");
    button.setAttribute("title", I18N.t("btn.start.title"));
  }

  function paintResetButton() {
    var button = $("btn-reset");
    if (button) button.setAttribute("title", I18N.t("btn.reset.title"));
  }

  /* ------------------------------------------------------------- telemetry -- */

  function buildRow(labelKey, valueId) {
    var item = document.createElement("div");
    item.className = "tele-item";
    var label = document.createElement("span");
    label.className = "metric-label";
    label.setAttribute("data-i18n", labelKey);
    var value = document.createElement("span");
    value.className = "tele-val";
    value.id = valueId;
    item.appendChild(label);
    item.appendChild(value);
    return item;
  }

  function mountTelemetry() {
    var host = document.querySelector("[data-telemetry]");
    if (!host) return;
    var head = document.createElement("div");
    head.className = "panel-head";
    var title = document.createElement("span");
    title.setAttribute("data-i18n", "tele.title");
    var note = document.createElement("span");
    note.className = "panel-sub";
    note.id = "tele-window";
    head.appendChild(title);
    head.appendChild(note);

    var grid = document.createElement("div");
    grid.className = "tele-grid";
    grid.appendChild(buildRow("tele.latency", "tele-latency"));
    grid.appendChild(buildRow("tele.p50", "tele-p50"));
    grid.appendChild(buildRow("tele.p95", "tele-p95"));
    grid.appendChild(buildRow("tele.tokens", "tele-tokens"));
    grid.appendChild(buildRow("tele.rate", "tele-rate"));
    grid.appendChild(buildRow("tele.calls", "tele-calls"));

    var noteLine = document.createElement("div");
    noteLine.className = "tele-note";
    noteLine.id = "tele-note";

    host.appendChild(head);
    host.appendChild(grid);
    host.appendChild(noteLine);
  }

  function rateOf(call) {
    if (!call || !isNumber(call.latencyMs) || call.latencyMs <= 0) return null;
    if (!isNumber(call.inputTokens) || !isNumber(call.outputTokens)) return null;
    return ((call.inputTokens + call.outputTokens) / call.latencyMs) * 1000;
  }

  function paintTelemetry() {
    var host = document.querySelector("[data-telemetry]");
    if (!host) return;
    host.setAttribute("aria-label", I18N.t("tele.title"));
    I18N.apply(host);
    var rateLabel = $("tele-rate");
    if (rateLabel) rateLabel.setAttribute("title", I18N.t("tele.rate.title"));

    var sample = tele.last;
    setText($("tele-latency"), sample && isNumber(sample.latencyMs) ? I18N.t("tele.ms", { value: sample.latencyMs }) : "—");
    var p50 = percentile(tele.latencies, 0.5);
    var p95 = percentile(tele.latencies, 0.95);
    setText($("tele-p50"), p50 === null ? "—" : I18N.t("tele.ms", { value: p50 }));
    setText($("tele-p95"), p95 === null ? "—" : I18N.t("tele.ms", { value: p95 }));
    setText(
      $("tele-tokens"),
      sample && isNumber(sample.inputTokens) && isNumber(sample.outputTokens)
        ? I18N.t("tele.tokens.val", { input: sample.inputTokens, output: sample.outputTokens })
        : "—"
    );
    var rate = rateOf(sample);
    setText($("tele-rate"), rate === null ? "—" : I18N.t("tele.rate.val", { value: Math.round(rate) }));
    setText($("tele-calls"), tele.calls === 0 ? "—" : I18N.t("tele.calls.val", { calls: tele.calls, failed: tele.failed }));
    setText($("tele-window"), tele.calls === 0 ? I18N.t("tele.idle") : "");
    paintNote();
  }

  function paintNote() {
    if (state.noteKey) {
      setText($("tele-note"), I18N.t(state.noteKey, state.noteVars));
      return;
    }
    setText($("tele-note"), tele.last && tele.last.error ? I18N.t("tele.failure", { message: tele.last.error }) : "");
  }

  /** `setNote` is for the game's own line under the numbers: in-flight depth, a fallback
   *  marker. It takes an i18n key so it repaints with the language. */
  function setNote(key, vars) {
    state.noteKey = key || "";
    state.noteVars = vars || null;
    paintNote();
  }

  /**
   * Record one `/v1/systemone` call and repaint the panel and the console.
   *
   * `call.request` is the body that was *sent* (a page that carries a UI-only field must
   * strip it first), `call.response` is the parsed JSON whatever the status, `call.error`
   * is set when the fetch itself failed.
   */
  function observe(call) {
    var usage = call.response && call.response.usage;
    var inputTokens = usage && isNumber(usage.input_tokens) ? usage.input_tokens : null;
    var outputTokens = usage && isNumber(usage.output_tokens) ? usage.output_tokens : null;
    var failed = Boolean(call.error) || (isNumber(call.status) && call.status >= 400);

    tele.calls += 1;
    if (failed) tele.failed += 1;
    if (isNumber(call.latencyMs)) {
      tele.latencies.push(call.latencyMs);
      if (tele.latencies.length > WINDOW) tele.latencies.shift();
    }
    if (inputTokens !== null) tele.tokensIn += inputTokens;
    if (outputTokens !== null) tele.tokensOut += outputTokens;
    tele.last = {
      latencyMs: call.latencyMs,
      inputTokens: inputTokens,
      outputTokens: outputTokens,
      error: call.error ? String(call.error.message || call.error) : null
    };
    last = {
      request: call.request,
      response: call.response,
      error: call.error ? String(call.error.message || call.error) : null,
      status: call.status,
      latencyMs: call.latencyMs,
      live: call.live
    };
    paintTelemetry();
    paintConsole();
  }

  function resetTelemetry() {
    tele = { calls: 0, failed: 0, latencies: [], last: null, tokensIn: 0, tokensOut: 0 };
    last = null;
    paintTelemetry();
    paintConsole();
  }

  /* ---------------------------------------------------------- I/O console -- */

  function mountConsole() {
    var host = document.querySelector("[data-io-console]");
    if (!host) return;
    // Open on load, and opened by the shell rather than by the three pages: the panels
    // under the board are the shell's arrangement (`theme.css` `.game-under`), so their
    // initial state belongs here too -- a page that wanted it closed would be a second
    // opinion about the same thing.
    host.open = true;
    var summary = document.createElement("summary");
    var head = document.createElement("span");
    head.className = "panel-head";
    head.setAttribute("data-i18n", "io.title");
    var meta = document.createElement("span");
    meta.className = "panel-sub";
    meta.id = "io-meta";
    summary.appendChild(head);
    summary.appendChild(meta);

    var body = document.createElement("div");
    body.className = "io-body";
    ["io.request", "io.response"].forEach(function (key) {
      var block = document.createElement("div");
      block.className = "io-block";
      var label = document.createElement("span");
      label.className = "io-label";
      label.setAttribute("data-i18n", key);
      var pre = document.createElement("pre");
      pre.className = "io-pre mono";
      pre.id = key === "io.request" ? "io-request" : "io-response";
      block.appendChild(label);
      block.appendChild(pre);
      body.appendChild(block);
    });

    host.appendChild(summary);
    host.appendChild(body);
  }

  function pretty(payload) {
    if (payload === undefined) return "";
    try {
      return JSON.stringify(payload, null, 2);
    } catch (error) {
      return String(payload);
    }
  }

  function paintConsole() {
    var host = document.querySelector("[data-io-console]");
    if (!host) return;
    I18N.apply(host);
    var request = $("io-request");
    var response = $("io-response");
    var meta = $("io-meta");
    if (!last) {
      if (request) request.textContent = "";
      if (response) response.textContent = I18N.t("io.idle");
      setText(meta, "");
      return;
    }
    if (request) request.textContent = pretty(last.request);
    if (response) {
      response.textContent = last.error ? last.error : pretty(last.response) || I18N.t("io.idle");
    }
    var status = last.error ? I18N.t("io.error") : String(last.status === undefined ? "" : last.status);
    var usage = last.response && last.response.usage;
    var tokens =
      usage && isNumber(usage.input_tokens) && isNumber(usage.output_tokens)
        ? usage.input_tokens + " \u2192 " + usage.output_tokens
        : "—";
    setText(
      meta,
      I18N.t("io.meta", {
        status: status,
        ms: isNumber(last.latencyMs) ? Math.round(last.latencyMs) : "—",
        tokens: tokens
      })
    );
  }

  /* ------------------------------------------------------------ engine chip -- */

  function paintChip() {
    var dot = $("dot");
    var text = $("status-text");
    if (!dot || !text) return;
    if (!chip) {
      dot.className = "dot busy";
      setText(text, I18N.t("status.searching"));
      return;
    }
    if (chip.error) {
      dot.className = "dot err";
      setText(text, I18N.t("status.unreachable", { message: chip.error }));
      return;
    }
    if (chip.ready) {
      dot.className = "dot live";
      setText(text, I18N.t("status.ready", { engine: chip.engine }));
    } else {
      dot.className = "dot warn";
      setText(text, I18N.t("status.searching"));
    }
  }

  async function refreshEngine() {
    try {
      var response = await fetch("/api/config", { cache: "no-store" });
      var config = await response.json();
      chip = { ready: Boolean(config.ready), engine: config.engine, error: null };
    } catch (error) {
      chip = { ready: false, engine: null, error: error.message };
    }
    paintChip();
  }

  /* ---------------------------------------------------------------- keyboard -- */

  function onKey(event) {
    var tag = event.target && event.target.tagName;
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
    if (event.code === "Space") {
      // In manual mode the space bar belongs to the game (dino jumps with it, tetris hard
      // drops), so the shell leaves it alone there; Enter starts and pauses in both modes.
      if (shell.mode === "manual" && shell.options.manualSpace) return;
      event.preventDefault();
      var start = $("btn-start");
      if (start) start.click();
    } else if (event.key === "Enter") {
      event.preventDefault();
      var button = $("btn-start");
      if (button) button.click();
    } else if (event.key === "r" || event.key === "R") {
      event.preventDefault();
      var reset = $("btn-reset");
      if (reset) reset.click();
    } else if (event.key === "m" || event.key === "M") {
      event.preventDefault();
      setMode(shell.mode === "ai" ? "manual" : "ai");
    }
  }

  /* -------------------------------------------------------------------- init -- */

  function paintAll() {
    paintSwitch();
    paintStartButton();
    paintResetButton();
    paintStatus();
    paintTelemetry();
    paintConsole();
    paintChip();
  }

  function init(options) {
    shell.options = options || {};
    shell.game = shell.options.game || "";
    shell.mode = shell.options.mode === "manual" ? "manual" : "ai";

    mountSwitch();
    mountTelemetry();
    mountConsole();

    var start = $("btn-start");
    if (start && shell.options.onStart) start.addEventListener("click", shell.options.onStart);
    var reset = $("btn-reset");
    if (reset && shell.options.onReset) reset.addEventListener("click", shell.options.onReset);
    window.addEventListener("keydown", onKey);

    // `apply` in i18n.js has already run for the tagged nodes; the panel and the console
    // are built here, so they are painted once with everything else.
    I18N.apply(document);
    I18N.onChange(function () {
      I18N.apply(document);
      paintAll();
      if (shell.options.onLanguage) shell.options.onLanguage();
    });
    paintAll();
    refreshEngine();
    setInterval(refreshEngine, 5000);
    if (shell.options.onMode) shell.options.onMode(shell.mode);
  }

  window.GameShell = {
    init: init,
    observe: observe,
    resetTelemetry: resetTelemetry,
    setStatus: setStatus,
    setRunning: setRunning,
    setNote: setNote,
    setMode: setMode,
    mode: function () {
      return shell.mode;
    }
  };
})();
