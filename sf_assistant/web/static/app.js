/* sf-assistant page logic. Text from the server only reaches the page through textContent and
 * createTextNode; model answers go through SafeMarkdown (render.js), which builds DOM nodes the same
 * way. Nothing here parses a string as HTML. A chart comes as a picture for an <img> (a data: URL of the
 * server's checked SVG: in an <img> it runs no script and loads nothing) and rows that go through
 * textContent. The history lives in memory only (nothing is stored in the browser: answers can hold
 * real names).
 *
 * A question is queued by POST /api/ask (202 + ticket) and followed through /api/status; when it is
 * done the answer is fetched from /api/job. The server keeps the question and the last answer, so after
 * a reload or a lost connection the page picks them up again. */
(function () {
  "use strict";

  var IDLE_POLL_MS = 15000;
  var WAIT_POLL_MS = 1500;
  var KIND_LABEL = {
    merged: "records and knowledge", records: "records only", knowledge: "knowledge files only", none: "no answer"
  };
  var MODE_LABEL = { merged: "Records and knowledge", records: "Records only", knowledge: "Knowledge only" };

  var state = {
    role: null, lastRole: null, maxChars: 1000, busy: false, history: [], shownId: null, nextId: 1, seen: {},
    pollTimer: null, tickTimer: null, pending: null, position: null, epoch: 0, emptyNode: null, announced: ""
  };

  function $(id) { return document.getElementById(id); }

  function h(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }

  function setText(node, text) {
    if (node && node.textContent !== text) { node.textContent = text; }
  }

  function fmtSeconds(s) {
    if (typeof s !== "number" || !isFinite(s)) { return ""; }
    if (s < 9.95) { return s.toFixed(1) + " s"; }
    var t = Math.round(s);
    return t < 60 ? t + " s" : Math.floor(t / 60) + " min " + (t % 60) + " s";
  }

  function clock(d) {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function announce(text) {
    if (text && text !== state.announced) {
      state.announced = text;
      setText($("announce"), text);
    }
  }

  async function api(path, body) {
    var init = { method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store", headers: {} };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    var res;
    try {
      res = await fetch(path, init);
    } catch (err) {
      return { ok: false, status: 0, data: { error: "The server cannot be reached. Check the network and try again." } };
    }
    var data = {};
    try { data = await res.json(); } catch (err) { data = {}; }
    return { ok: res.ok, status: res.status, data: data || {} };
  }

  // ---------------------------------------------------------------- views
  function resetStage() {
    var stage = $("stage");
    stage.setAttribute("aria-busy", "false");
    clear(stage);
    if (state.emptyNode) { stage.appendChild(state.emptyNode); }
  }

  /* Forget everything of the signed-in person: a request still on its way is ignored (epoch). */
  function resetView() {
    state.epoch += 1;
    stopTicking();
    stopPolling();
    state.pending = null;
    state.busy = false;
    state.position = null;
    state.history = [];
    state.shownId = null;
    state.seen = {};
    state.announced = "";
    renderHistory();
    resetStage();
    updateCounter();
  }

  function showLogin(message) {
    state.role = null;
    stopPolling();
    $("app-view").hidden = true;
    $("login-view").hidden = false;
    $("login-error").textContent = message || "";
    $("code").focus();
  }

  function endSession(message) {
    resetView();
    showLogin(message);
  }

  function enterApp(status) {
    if (state.lastRole !== status.role) { resetView(); $("question").value = ""; }
    state.lastRole = status.role;
    state.role = status.role;
    $("login-view").hidden = true;
    $("app-view").hidden = false;
    $("modes").hidden = state.role !== "admin";
    showStatus(status, null);
    updateCounter();
    $("question").focus();
    followJob(status.job, state.epoch, null).then(function () { schedulePoll(state.pending ? WAIT_POLL_MS : IDLE_POLL_MS); });
  }

  // ---------------------------------------------------------------- status
  /* ``known``: the pending ticket when this status was requested (an older reply says nothing about it). */
  function showStatus(s, known) {
    if (!s || !s.role) { return; }
    if (typeof s.max_question_chars === "number") { state.maxChars = s.max_question_chars; }
    var dot = $("st-model-dot");
    var text = $("st-model-text");
    if (!s.ready) {
      dot.className = s.failed ? "dot bad" : "dot warn";
      setText(text, s.failed ? "Assistant not available" : "Assistant starting");
    } else if (s.model && s.model.ok) {
      dot.className = "dot ok";
      setText(text, "Model ready");
    } else {
      dot.className = "dot bad";
      setText(text, "Model not responding");
    }
    var rec = s.records || {};
    setText($("st-data"), !s.ready ? "" : rec.ok ? "Data synced " + (rec.data_synced || "at an unknown time")
      : "Record data off");
    var q = s.queue || {};
    setText($("st-queue"), q.length > 0 ? q.length + (q.length === 1 ? " question" : " questions") + " in line" : "");
    var lim = s.limit || {};
    setText($("st-limit"), typeof lim.left === "number"
      ? lim.left + " of " + lim.per_hour + " questions left this hour" : "");
    var line = $("st-admin");
    if (s.admin) {
      var a = s.admin;
      var notes = [
        a.pipeline_error ? "Start-up failed: " + a.pipeline_error : "",
        a.records_error ? "Records: " + a.records_error : "",
        a.running_past_timeout ? "A question is still running past its timeout." : ""
      ].filter(Boolean);
      setText(line, notes.join("  |  "));
      line.hidden = notes.length === 0;
      $("status").title = [a.status_line].concat(notes).filter(Boolean).join("\n");
    } else {
      setText(line, "");
      line.hidden = true;
      $("status").title = "";
    }
    if (state.pending && state.pending.ticket && known === state.pending.ticket && s.job
        && s.job.ticket === known && (s.job.state === "waiting" || s.job.state === "running")) {
      state.position = s.job.position;
      updateWaiting();
    }
    updateCounter();
  }

  function showUnreachable(status) {
    $("st-model-dot").className = "dot bad";
    setText($("st-model-text"), status === 0 ? "Server not reachable" : "Server error");
    setText($("st-queue"), "");
  }

  async function poll() {
    var epoch = state.epoch;
    var known = state.pending ? state.pending.ticket : null;   // what this status reply can know about
    var r = await api("/api/status");
    if (epoch !== state.epoch) { return; }
    if (r.ok && r.data.role) {
      showStatus(r.data, known);
      await followJob(r.data.job, epoch, known);
      if (epoch !== state.epoch) { return; }
    } else if (r.ok && !r.data.role) {
      endSession("Your session ended. Sign in again.");
      return;
    } else {
      showUnreachable(r.status);
    }
    schedulePoll(state.pending ? WAIT_POLL_MS : IDLE_POLL_MS);
  }

  function schedulePoll(ms) {
    stopPolling();
    if (state.role) { state.pollTimer = setTimeout(poll, ms); }
  }

  function stopPolling() {
    if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; }
  }

  // ---------------------------------------------------------------- following a question
  /* ``brief`` is the job line of /api/status: {ticket, state, position} or null. ``known`` is the ticket
   * of the pending question when that status was requested (a reply sent before the ticket existed
   * cannot be about it). */
  async function followJob(brief, epoch, known) {
    var p = state.pending;
    if (p && p.ticket) {
      if (known !== p.ticket) { return; }
      if (!brief || brief.ticket !== p.ticket) {
        finishEntry(p, { state: "failed", error: "This answer is no longer available (the server restarted, or you "
          + "signed out in another window). Ask again." });
        return;
      }
      if (brief.state === "waiting" || brief.state === "running") { return; }
      var r = await api("/api/job");
      if (epoch !== state.epoch) { return; }
      if (r.status === 401) { endSession("Your session ended. Sign in again."); return; }
      if (r.ok && r.data.job && r.data.job.ticket === p.ticket) { finishEntry(p, r.data.job); }
      return;
    }
    if (!p && brief && !state.seen[brief.ticket]) {
      // a question this page does not know yet: asked before a reload, or its response was lost
      var j = await api("/api/job");
      if (epoch !== state.epoch) { return; }
      if (j.ok && j.data.job && !state.seen[j.data.job.ticket] && !state.pending) { adopt(j.data.job); }
    }
  }

  function adopt(job) {
    var asked = Date.now() - Math.max(0, job.age_s || 0) * 1000;
    var entry = { id: state.nextId++, question: job.question, mode: job.mode, askedAt: new Date(asked), started: asked,
                  ticket: job.ticket, status: "waiting", result: null, error: null };
    state.seen[job.ticket] = true;
    state.history.unshift(entry);
    if (job.state === "waiting" || job.state === "running") {
      state.busy = true;
      state.pending = entry;
      state.position = job.position;
      show(entry);
      startTicking();
      renderHistory();
      updateCounter();
    } else {
      applyOutcome(entry, job);
      show(entry);
      renderHistory();
    }
  }

  function applyOutcome(entry, job) {
    if (job.state === "done" && job.result) {
      entry.status = "done";
      entry.result = job.result;
    } else {
      entry.status = "error";
      entry.error = job.error || "The question could not be answered. Try again.";
    }
  }

  function finishEntry(entry, job) {
    stopTicking();
    state.pending = null;
    state.busy = false;
    applyOutcome(entry, job);
    show(entry);
    renderHistory();
    updateCounter();
    announce(entry.status === "done" ? "Answer ready." : entry.error);
  }

  function startTicking() {
    stopTicking();
    $("stage").setAttribute("aria-busy", "true");
    state.tickTimer = setInterval(updateWaiting, 500);
    updateWaiting();
  }

  function stopTicking() {
    if (state.tickTimer) { clearInterval(state.tickTimer); state.tickTimer = null; }
    $("stage").setAttribute("aria-busy", "false");
  }

  // ---------------------------------------------------------------- composer
  function updateCounter() {
    var n = $("question").value.trim().length;
    var counter = $("counter");
    setText(counter, n + " / " + state.maxChars);
    counter.classList.toggle("over", n > state.maxChars);
    $("ask-btn").disabled = state.busy || n === 0 || n > state.maxChars;
  }

  function selectedMode() {
    if (state.role !== "admin") { return undefined; }
    var checked = document.querySelector('input[name="mode"]:checked');
    return checked ? checked.value : "merged";
  }

  // The last two answered questions of this page, oldest first, as {role, content} turns: the server reads a
  // follow-up ("plot this rate") as a whole question with them (agent/followup.py, the ask service's rewrite). Each
  // text is cut to TURN_CHARS, and the oldest pair goes while the request would pass the server's body limit.
  var TURN_CHARS = 1500;
  var MAX_BODY_BYTES = 15000;
  function recentTurns(question) {
    var pairs = state.history.filter(function (e) {
      return e.status === "done" && e.result && typeof e.result.answer === "string" && e.result.answer;
    }).slice(0, 2).reverse().map(function (e) {
      return [{ role: "user", content: e.question.slice(0, TURN_CHARS) },
              { role: "assistant", content: e.result.answer.slice(0, TURN_CHARS) }];
    });
    while (pairs.length) {
      var turns = [].concat.apply([], pairs);
      var size = new Blob([JSON.stringify({ question: question, recent_turns: turns, mode: "records" })]).size;
      if (size <= MAX_BODY_BYTES) { return turns; }
      pairs.shift();
    }
    return [];
  }

  async function send() {
    var box = $("question");
    var question = box.value.trim();
    if (state.busy || !question || question.length > state.maxChars) { return; }
    var epoch = state.epoch;
    var turns = recentTurns(question);   // before this question joins the history
    var entry = { id: state.nextId++, question: question, mode: selectedMode(), askedAt: new Date(),
                  started: Date.now(), status: "waiting", ticket: null, result: null, error: null };
    state.busy = true;
    state.pending = entry;
    state.position = null;
    box.value = "";
    updateCounter();
    state.history.unshift(entry);
    show(entry);
    renderHistory();
    startTicking();

    var body = { question: question };
    if (entry.mode) { body.mode = entry.mode; }
    if (turns.length) { body.recent_turns = turns; }
    var r = await api("/api/ask", body);
    if (epoch !== state.epoch) { return; }   // signed out meanwhile: nothing of it is shown

    if (r.status === 202 && r.data.ticket) {
      entry.ticket = r.data.ticket;
      state.seen[entry.ticket] = true;
      state.position = r.data.position;
      updateWaiting();
      schedulePoll(WAIT_POLL_MS);
      return;
    }
    // refused before it was queued
    stopTicking();
    state.pending = null;
    state.busy = false;
    if (r.status === 401) {
      endSession("Your session ended. Sign in again.");
      box.value = question;
      updateCounter();
      return;
    }
    entry.status = "error";
    entry.error = r.data.error || "The question could not be answered. Try again.";
    if (r.status === 0 || r.status === 400 || r.status === 409 || r.status === 429 || r.status === 503) {
      box.value = question;
    }
    show(entry);
    renderHistory();
    updateCounter();
    announce(entry.error);
    schedulePoll(r.status === 409 || r.status === 0 ? 500 : IDLE_POLL_MS);   // 409 / lost: pick the question up
    box.focus();
  }

  // ---------------------------------------------------------------- cards
  function show(entry) {
    state.shownId = entry.id;
    var stage = $("stage");
    clear(stage);
    stage.appendChild(entry.status === "waiting" ? waitingCard(entry)
      : entry.status === "error" ? errorCard(entry) : answerCard(entry));
  }

  function cardHeading(entry) {
    var h2 = h("h2", "card-q", entry.question);
    h2.tabIndex = -1;
    return h2;
  }

  function waitingCard(entry) {
    var card = h("article", "card waiting");
    var body = h("div", "card-body");
    body.appendChild(cardHeading(entry));
    var line = h("p", "wait-line");
    line.id = "wait-line";
    var what = h("span");
    what.id = "wait-what";
    var secs = h("strong");
    secs.id = "wait-secs";
    secs.setAttribute("aria-hidden", "true");
    line.appendChild(what);
    line.appendChild(secs);
    body.appendChild(line);
    var bar = h("div", "progress");
    bar.appendChild(h("span"));
    body.appendChild(bar);
    card.appendChild(body);
    setTimeout(updateWaiting, 0);
    return card;
  }

  function updateWaiting() {
    var entry = state.pending;
    if (!entry) { return; }
    var p = state.position;
    var what = p === 0 ? "Answering your question" : typeof p === "number" && p > 0
      ? "Waiting in line: " + p + (p === 1 ? " question" : " questions") + " ahead of yours"
      : "Sending your question";
    if (entry.ticket) { announce(what + "."); }
    if (state.shownId !== entry.id) { return; }
    setText($("wait-what"), what + ", ");
    setText($("wait-secs"), Math.floor((Date.now() - entry.started) / 1000) + " s");
  }

  function errorCard(entry) {
    var card = h("article", "card error");
    var body = h("div", "card-body");
    body.appendChild(cardHeading(entry));
    body.appendChild(h("p", "message", entry.error));
    card.appendChild(body);
    return card;
  }

  // A chart of the answer: its picture, and under it the rows it was drawn from unless the answer's text holds them.
  function chartBlock(c) {
    var rows = Array.isArray(c.data) && c.data.length && !c.rows_in_answer;
    if (!c.svg_b64 && !rows) { return null; }   // nothing to show: no empty figure
    var fig = h("figure", "chart");
    if (c.svg_b64) {
      var box = h("div", "chart-scroll");
      var img = document.createElement("img");
      img.alt = c.alt || "Chart";
      img.src = "data:image/svg+xml;base64," + c.svg_b64;
      box.appendChild(img);
      fig.appendChild(box);
    } else {   // no picture to carry them: a cut result is still disclosed
      (c.subtitle || []).concat(c.notes || []).forEach(function (t) { fig.appendChild(h("p", "chart-note", t)); });
    }
    if (rows) { fig.appendChild(rowsTable(c.data)); }
    return fig;
  }

  function rowsTable(rows) {
    var keys = [];
    rows.forEach(function (row) {
      Object.keys(row).forEach(function (k) { if (keys.indexOf(k) < 0) { keys.push(k); } });
    });
    var wrap = h("div", "md-table chart-rows"), table = h("table"), head = h("thead"), line = h("tr"), body = h("tbody");
    keys.forEach(function (k) { line.appendChild(h("th", null, k)); });
    head.appendChild(line);
    rows.forEach(function (row) {
      var tr = h("tr");
      keys.forEach(function (k) {
        var held = Object.prototype.hasOwnProperty.call(row, k);
        tr.appendChild(h("td", null, !held ? "" : row[k] === null ? "(no value)" : row[k]));
      });
      body.appendChild(tr);
    });
    table.appendChild(head); table.appendChild(body); wrap.appendChild(table);
    return wrap;
  }

  function answerCard(entry) {
    var r = entry.result || {};
    var card = h("article", "card");
    var body = h("div", "card-body");
    body.appendChild(cardHeading(entry));
    var text = h("div", "answer-text");
    text.appendChild(window.SafeMarkdown.render(r.answer || ""));
    body.appendChild(text);
    (r.charts || []).forEach(function (c) { var b = chartBlock(c); if (b) { body.appendChild(b); } });
    if (state.role === "admin") {
      body.appendChild(h("p", "meta", "Answered from " + (KIND_LABEL[r.kind] || r.kind || "unknown") + " in "
        + fmtSeconds(r.seconds) + (r.queue_wait_s >= 1 ? ", after " + fmtSeconds(r.queue_wait_s) + " in line" : "")
        + ". Mode: " + (MODE_LABEL[entry.mode] || "Records and knowledge") + "."));
    }
    card.appendChild(body);

    var prov = h("div", "provenance");
    if (r.source_line) { prov.appendChild(h("p", "source", r.source_line)); }
    var usedRecords = r.kind === "merged" || r.kind === "records";
    prov.appendChild(h("p", "synced", r.data_ts ? "Data synced " + r.data_ts
      : usedRecords ? "Data sync time unknown." : "No record data was used for this answer."));
    card.appendChild(prov);

    if (state.role === "admin" && r.trace) { card.appendChild(traceView(entry)); }
    return card;
  }

  // ---------------------------------------------------------------- how it was answered (admin)
  function kvTable(rows) {
    var table = h("table");
    var tbody = h("tbody");
    rows.forEach(function (row) {
      if (row[1] === undefined || row[1] === null || row[1] === "") { return; }
      var tr = h("tr");
      tr.appendChild(h("th", null, row[0]));
      tr.appendChild(h("td", null, row[1]));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    return table;
  }

  function section(title) {
    var div = h("div");
    div.appendChild(h("h4", null, title));
    return div;
  }

  function secs(v) { return typeof v === "number" ? fmtSeconds(v) : null; }

  function traceView(entry) {
    var r = entry.result;
    var t = r.trace || {};
    var details = h("details", "trace");
    details.appendChild(h("summary", null, "How it was answered"));
    var body = h("div", "trace-body");

    var overview = section("Overview");
    overview.appendChild(kvTable([
      ["Mode", MODE_LABEL[t.mode] || t.mode],
      ["Knowledge step", t.knowledge_mode || "none"],
      ["Record queries", t.records],
      ["Knowledge tool calls", t.knowledge_tool_calls],
      ["Record tool calls", t.record_tool_calls],
      ["Number audit", t.audit],
      ["Reopened after a file swap", t.retried_after_swap ? "yes, asked again" : null],
      ["Data", t.status_line]
    ]));
    body.appendChild(overview);

    var tm = t.timings || {};
    var timings = section("Timings");
    timings.appendChild(kvTable([
      ["Waited in line", secs(r.queue_wait_s)], ["Answer, end to end", secs(r.seconds)],
      ["Inside ask()", secs(tm.total_s)], ["Route call", secs(tm.route_s)], ["Knowledge agent", secs(tm.knowledge_s)],
      ["Record agent", secs(tm.records_s)], ["Merge call", secs(tm.merge_s)]
    ]));
    body.appendChild(timings);

    var problems = [t.error, t.record_error, t.merge_error].filter(Boolean);
    (t.field_check || []).forEach(function (line) { problems.push("Field check: " + line); });
    if (problems.length) {
      var notes = section("Notes and errors");
      problems.forEach(function (p) { notes.appendChild(h("p", null, p)); });
      body.appendChild(notes);
    }

    var queries = t.queries || [];
    var qs = section(queries.length ? "Record queries (" + queries.length + ")" : "Record queries: none");
    queries.forEach(function (q, i) {
      var div = h("div", q.ok ? "query" : "query failed");
      var rows = typeof q.row_count === "number" ? q.row_count + (q.row_count === 1 ? " row" : " rows") : "";
      div.appendChild(h("p", null, (i + 1) + ". " + (q.tool || "?") + " on " + (q.object || "no object") + ": "
        + (q.ok ? rows : "failed" + (q.error_code ? " (" + q.error_code + ")" : ""))));
      if (q.summary) { div.appendChild(h("p", null, q.summary)); }
      if (q.error) { div.appendChild(h("p", "err", q.error)); }
      (q.sql || []).forEach(function (sql, k) {
        div.appendChild(h("pre", null, sql));
        var params = (q.params || [])[k];
        if (params && params.length) { div.appendChild(h("pre", null, "params: " + JSON.stringify(params))); }
      });
      qs.appendChild(div);
    });
    body.appendChild(qs);

    [["Record agent's answer", t.record_answer], ["Knowledge agent's answer", t.knowledge_answer]].forEach(function (pair) {
      if (!pair[1]) { return; }
      var s = section(pair[0]);
      var sub = h("div", "sub");
      var text = h("div", "answer-text");
      text.appendChild(window.SafeMarkdown.render(pair[1]));
      sub.appendChild(text);
      s.appendChild(sub);
      body.appendChild(s);
    });

    if (t.transcript) {
      var raw = h("details");
      raw.appendChild(h("summary", null, "Full transcript (ask.py output)"));
      raw.appendChild(h("pre", null, t.transcript));
      body.appendChild(raw);
    }
    details.appendChild(body);
    return details;
  }

  // ---------------------------------------------------------------- history
  function markCurrent() {
    var buttons = $("history-list").querySelectorAll("button");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].setAttribute("aria-current", buttons[i].dataset.entry === String(state.shownId) ? "true" : "false");
    }
  }

  function openFromHistory(entry) {
    show(entry);
    markCurrent();
    if (window.matchMedia && window.matchMedia("(max-width: 959px)").matches) {
      // one column: the card is above the list, out of view
      var heading = $("stage").querySelector(".card-q");
      if (heading) {
        heading.scrollIntoView({ block: "start" });
        heading.focus({ preventScroll: true });
      }
    }
  }

  function renderHistory() {
    var list = $("history-list");
    var active = document.activeElement;
    var focusedId = active && active.parentNode && active.parentNode.parentNode === list ? active.dataset.entry : null;
    clear(list);
    $("history-none").hidden = state.history.length > 0;
    state.history.forEach(function (entry) {
      var li = h("li");
      var btn = h("button");
      btn.type = "button";
      btn.dataset.entry = String(entry.id);
      btn.setAttribute("aria-current", entry.id === state.shownId ? "true" : "false");
      btn.appendChild(h("span", "h-q", entry.question));
      var meta = clock(entry.askedAt) + ", " + (entry.status === "waiting" ? "waiting"
        : entry.status === "error" ? "not answered" : fmtSeconds(entry.result && entry.result.seconds));
      btn.appendChild(h("span", "h-meta", meta));
      btn.addEventListener("click", function () { openFromHistory(entry); });
      li.appendChild(btn);
      list.appendChild(li);
      if (focusedId === String(entry.id)) { btn.focus(); }
    });
  }

  // ---------------------------------------------------------------- wiring
  async function onLogin(ev) {
    ev.preventDefault();
    var input = $("code");
    var code = input.value.trim();
    if (!code) { $("login-error").textContent = "Enter the access code."; return; }
    $("login-btn").disabled = true;
    var r = await api("/api/login", { code: code });
    $("login-btn").disabled = false;
    if (!r.ok) {
      $("login-error").textContent = r.data.error || "Signing in failed. Try again.";
      input.select();
      return;
    }
    input.value = "";
    input.type = "password";
    $("show-code").checked = false;
    var s = await api("/api/status");
    if (s.ok && s.data.role) { enterApp(s.data); } else { showLogin("Signing in failed. Try again."); }
  }

  async function onLogout() {
    resetView();
    $("question").value = "";
    updateCounter();
    showLogin("");
    await api("/api/logout", {});
  }

  async function start() {
    $("login-form").addEventListener("submit", onLogin);
    $("logout-btn").addEventListener("click", onLogout);
    $("show-code").addEventListener("change", function () { $("code").type = $("show-code").checked ? "text" : "password"; });
    $("ask-form").addEventListener("submit", function (ev) { ev.preventDefault(); send(); });
    $("question").addEventListener("input", updateCounter);
    $("question").addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing && !ev.altKey && !ev.ctrlKey && !ev.metaKey) {
        ev.preventDefault();
        send();
      }
    });
    state.emptyNode = $("empty");
    var r = await api("/api/status");
    if (r.ok && r.data.role) { enterApp(r.data); } else { showLogin(""); }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
