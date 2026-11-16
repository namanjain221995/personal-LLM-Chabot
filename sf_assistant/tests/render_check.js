/* Runs web/static/render.js (SafeMarkdown) in node against a fake document and prints, per case,
 * the element tree it built. tests/test_web.py checks that model text with HTML or JavaScript in it
 * only ever ends up in text nodes. The fake nodes are sealed: render.js runs in strict mode, so an
 * assignment to any other property (innerHTML, onclick, href, ...) would throw here. */
"use strict";
const path = require("path");
const md = require(path.join(__dirname, "..", "web", "static", "render.js"));

class FakeNode {
  constructor(kind, tag, text) {
    this.kind = kind;
    this.tag = tag;
    this.text = text === undefined ? "" : text;
    this.children = [];
    this.className = "";
    this.start = undefined;
    Object.seal(this);
  }
  appendChild(child) {
    if (!(child instanceof FakeNode)) { throw new Error("appendChild got a non-node: " + typeof child); }
    this.children.push(child);
    return child;
  }
}

const doc = {
  createElement(tag) { return new FakeNode("el", String(tag).toLowerCase()); },
  createTextNode(text) { return new FakeNode("text", null, String(text)); },
  createDocumentFragment() { return new FakeNode("frag", null); },
};

function walk(node, out) {
  if (node.kind === "text") { out.texts.push(node.text); return node.text === "" ? "" : JSON.stringify(node.text); }
  if (node.kind === "el") {
    out.tags.add(node.tag);
    if (node.className) { out.classes.add(node.className); }
    if (node.start !== undefined) { out.starts.push(node.start); }
  }
  const inner = node.children.map((c) => walk(c, out)).join(",");
  return node.kind === "frag" ? inner : node.tag + "(" + inner + ")";
}

const cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
const result = {};
for (const [name, text] of Object.entries(cases)) {
  const out = { tags: new Set(), texts: [], classes: new Set(), starts: [] };
  const tree = walk(md.render(text, doc), out);
  result[name] = { tree, tags: [...out.tags].sort(), texts: out.texts, classes: [...out.classes], starts: out.starts };
}
result.__allowed = md.ALLOWED_TAGS;
process.stdout.write(JSON.stringify(result));
