/* SafeMarkdown: a small, safe subset of markdown for model text.
 *
 * Blocks: paragraphs (single newlines become line breaks), bullet and numbered lists (nested by
 * indentation), fenced code, tables, headings (shown as a short bold heading). Inline: `code`,
 * **bold**, *emphasis*. Everything else, links and HTML included, stays plain text.
 *
 * Model text never reaches an HTML parser: every element comes from document.createElement with a
 * fixed tag from ALLOWED, every piece of text from document.createTextNode, and the only properties
 * set are className (fixed strings) and the numeric start of a numbered list. No string is ever
 * parsed as HTML, no attribute comes from the text, no URL is followed.
 *
 * Browser: window.SafeMarkdown.render(text) -> DocumentFragment.
 * Node (tests): require("./render.js").render(text, fakeDocument).
 */
(function (root) {
  "use strict";

  var ALLOWED = ["p", "ul", "ol", "li", "strong", "em", "code", "pre", "table", "thead", "tbody", "tr", "th",
                 "td", "br", "h3", "div"];
  var LIST_RE = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/;
  var FENCE_RE = /^\s*```/;
  var RULE_RE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
  var HEADING_RE = /^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/;
  var SEPARATOR_RE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

  function el(doc, tag, cls) {
    if (ALLOWED.indexOf(tag) < 0) { throw new Error("tag not allowed: " + tag); }
    var node = doc.createElement(tag);
    if (cls) { node.className = cls; }
    return node;
  }

  function text(doc, parent, value) {
    if (value) { parent.appendChild(doc.createTextNode(value)); }
  }

  // ---- inline: `code`, **bold**, *emphasis*; underscores are never markup (Field__c, Record_Type)
  function inline(doc, parent, src) {
    var i = 0, buf = "";
    function flush() { text(doc, parent, buf); buf = ""; }
    while (i < src.length) {
      var ch = src.charAt(i), j;
      if (ch === "`") {
        j = src.indexOf("`", i + 1);
        if (j > i + 1) {
          flush();
          var code = el(doc, "code");
          text(doc, code, src.slice(i + 1, j));
          parent.appendChild(code);
          i = j + 1;
          continue;
        }
      }
      if (ch === "*" && src.charAt(i + 1) === "*") {
        j = src.indexOf("**", i + 2);
        if (j > i + 2) {
          flush();
          var strong = el(doc, "strong");
          inline(doc, strong, src.slice(i + 2, j));
          parent.appendChild(strong);
          i = j + 2;
          continue;
        }
      }
      if (ch === "*" && /\S/.test(src.charAt(i + 1)) && src.charAt(i + 1) !== "*") {
        j = src.indexOf("*", i + 1);
        if (j > i + 1 && /\S/.test(src.charAt(j - 1))) {
          flush();
          var em = el(doc, "em");
          inline(doc, em, src.slice(i + 1, j));
          parent.appendChild(em);
          i = j + 1;
          continue;
        }
      }
      buf += ch;
      i += 1;
    }
    flush();
  }

  // ---- tables
  function isTableRow(line) {
    var t = line.trim();
    return t.length > 1 && t.indexOf("|") >= 0;
  }

  function cells(line) {
    var t = line.trim();
    if (t.charAt(0) === "|") { t = t.slice(1); }
    if (t.charAt(t.length - 1) === "|") { t = t.slice(0, -1); }
    return t.split("|").map(function (c) { return c.trim(); });
  }

  function table(doc, parent, lines, i) {
    var wrap = el(doc, "div", "md-table");
    var tbl = el(doc, "table");
    var thead = el(doc, "thead");
    var head = el(doc, "tr");
    var width = 0;
    cells(lines[i]).forEach(function (c) {
      var th = el(doc, "th");
      inline(doc, th, c);
      head.appendChild(th);
      width += 1;
    });
    thead.appendChild(head);
    tbl.appendChild(thead);
    var body = el(doc, "tbody");
    i += 2;
    while (i < lines.length && lines[i].trim() && isTableRow(lines[i])) {
      var tr = el(doc, "tr");
      var row = cells(lines[i]);
      for (var k = 0; k < Math.max(width, row.length); k++) {
        var td = el(doc, "td");
        inline(doc, td, row[k] || "");
        tr.appendChild(td);
      }
      body.appendChild(tr);
      i += 1;
    }
    tbl.appendChild(body);
    wrap.appendChild(tbl);
    parent.appendChild(wrap);
    return i;
  }

  // ---- lists (nested by indentation; a blank line inside a list keeps it going when a list item follows)
  function nextNonBlank(lines, i) {
    while (i < lines.length && !lines[i].trim()) { i += 1; }
    return i;
  }

  function list(doc, parent, lines, i) {
    var stack = [];   // one entry per nesting level: {indent, node, ordered, lastLi}
    while (i < lines.length) {
      var line = lines[i];
      var top = stack.length ? stack[stack.length - 1] : null;
      if (!line.trim()) {
        var n = nextNonBlank(lines, i);
        if (n < lines.length && LIST_RE.test(lines[n])) { i = n; continue; }
        break;
      }
      var m = LIST_RE.exec(line);
      if (!m) {
        if (top && top.lastLi && /^\s{2,}\S/.test(line)) {   // an indented continuation line of the last item
          top.lastLi.appendChild(el(doc, "br"));
          inline(doc, top.lastLi, line.trim());
          i += 1;
          continue;
        }
        break;
      }
      var indent = m[1].replace(/\t/g, "    ").length;
      var ordered = /\d/.test(m[2]);
      while (stack.length && indent < stack[stack.length - 1].indent) { stack.pop(); }
      top = stack.length ? stack[stack.length - 1] : null;
      if (top && indent === top.indent && top.ordered !== ordered) {   // bullets after numbers: a new list
        stack.pop();
        top = stack.length ? stack[stack.length - 1] : null;
      }
      if (!top || indent > top.indent) {
        var node = el(doc, ordered ? "ol" : "ul");
        if (ordered) {
          var start = parseInt(m[2], 10);
          if (start > 1) { node.start = start; }
        }
        (top && top.lastLi ? top.lastLi : parent).appendChild(node);
        stack.push({ indent: indent, node: node, ordered: ordered, lastLi: null });
        top = stack[stack.length - 1];
      }
      var li = el(doc, "li");
      inline(doc, li, m[3]);
      top.node.appendChild(li);
      top.lastLi = li;
      i += 1;
    }
    return i;
  }

  function render(src, doc) {
    doc = doc || root.document;
    var frag = doc.createDocumentFragment();
    var lines = String(src == null ? "" : src).replace(/\r\n?/g, "\n").split("\n");
    var para = null;
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      if (FENCE_RE.test(line)) {
        para = null;
        var body = [];
        i += 1;
        while (i < lines.length && !FENCE_RE.test(lines[i])) { body.push(lines[i]); i += 1; }
        i += 1;
        var pre = el(doc, "pre");
        var code = el(doc, "code");
        text(doc, code, body.join("\n"));
        pre.appendChild(code);
        frag.appendChild(pre);
        continue;
      }
      if (!line.trim()) { para = null; i += 1; continue; }
      if (isTableRow(line) && i + 1 < lines.length && SEPARATOR_RE.test(lines[i + 1]) &&
          lines[i + 1].indexOf("-") >= 0) {
        para = null;
        i = table(doc, frag, lines, i);
        continue;
      }
      var h = HEADING_RE.exec(line);
      if (h) {
        para = null;
        var head = el(doc, "h3");
        inline(doc, head, h[1]);
        frag.appendChild(head);
        i += 1;
        continue;
      }
      if (RULE_RE.test(line)) { para = null; i += 1; continue; }
      if (LIST_RE.test(line)) { para = null; i = list(doc, frag, lines, i); continue; }
      var t = line.replace(/^\s{0,3}>\s?/, "").trim();
      if (para) { para.appendChild(el(doc, "br")); } else { para = el(doc, "p"); frag.appendChild(para); }
      inline(doc, para, t);
      i += 1;
    }
    return frag;
  }

  var api = { render: render, ALLOWED_TAGS: ALLOWED.slice() };
  if (typeof module !== "undefined" && module.exports) { module.exports = api; } else { root.SafeMarkdown = api; }
})(typeof window !== "undefined" ? window : this);
