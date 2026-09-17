"""Client + automated checks for the QA baseline.

The client does what the browser does against an orchestrator (the isolated
stack, never production): sign in, upload a CSV (purpose=dataset), POST /chat
and follow the SSE stream (token / reasoning / step / meta), then download
every file the artifact ref lists and read the version's spec.json (through
`docker exec` into the ISOLATED container only) for the charts.

The checks are pure functions over what came back; cases.py holds the rubric.
"""
from __future__ import annotations

import colorsys
import csv
import io
import json
import os
import re
import subprocess
import time
import zipfile
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

PROD_PORTS = {"8080", "3000"}
CHART_TYPES = {
    "bar", "horizontal_bar", "stacked_bar", "stacked_horizontal_bar", "percent_stacked_bar", "line", "area",
    "stacked_area", "pie", "donut", "scatter", "histogram", "combo", "box", "heatmap", "waterfall", "funnel", "gantt",
    "radar", "bubble", "pareto", "treemap", "violin", "candlestick", "sunburst", "bullet",
}


# =============================================================== client ==

class Client:
    def __init__(self, base: str, email: str, password: str, container: Optional[str] = None):
        port = base.rsplit(":", 1)[-1].split("/")[0]
        if port in PROD_PORTS or "techsarasolutions.com" in base:
            raise SystemExit(f"refusing to send test traffic to what looks like production: {base}")
        if container and not ("e2e" in container and "sf-local-ai" not in container):
            raise SystemExit(f"refusing docker exec into a non-e2e container: {container}")
        self.base, self.container = base.rstrip("/"), container
        self.http = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=3600.0))
        r = self.http.post(f"{self.base}/auth/login", json={"email": email, "password": password, "remember": True})
        if r.status_code != 200:
            raise SystemExit(f"login failed: {r.status_code} {r.text[:200]}")
        pair = r.headers.get("set-cookie", "").split(";", 1)[0].strip()
        if "=" in pair:  # the cookie is Secure; pin it as a header over plain http
            self.http.headers["Cookie"] = pair
        me = self.http.get(f"{self.base}/auth/me")
        self.me = me.json() if me.status_code == 200 else {}
        self.user_id = self.me.get("id") or (self.me.get("user") or {}).get("id")

    def upload(self, conversation_id: str, path: str) -> dict:
        with open(path, "rb") as fh:
            r = self.http.post(f"{self.base}/uploads", data={"conversation_id": conversation_id, "purpose": "dataset"},
                               files={"file": (os.path.basename(path), fh, "text/csv")}, timeout=300.0)
        if r.status_code != 200:
            raise RuntimeError(f"upload {r.status_code}: {r.text[:300]}")
        return r.json()

    def chat(self, conversation_id: str, message: str, history: List[dict], effort: str,
             attachments: Optional[List[dict]] = None, web_search: str = "off") -> dict:
        body = {"message": message, "messages": [*history, {"role": "user", "content": message}],
                "session_id": conversation_id, "conversation_id": conversation_id, "mode": "assistant",
                "model": "smart", "effort": effort, "web_search": web_search}
        tokens: List[str] = []
        reasoning: List[str] = []
        steps: Dict[int, dict] = {}
        meta: dict = {}
        errors: List[dict] = []
        started = time.perf_counter()
        first_token = None
        with self.http.stream("POST", f"{self.base}/chat", json=body) as r:
            if r.status_code != 200:
                return {"http": r.status_code, "answer": "", "meta": {}, "error": r.read()[:400].decode("utf-8", "replace"),
                        "reasoning_events": 0, "reasoning_chars": 0, "steps": {}, "seconds": 0}
            event = None
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                try:
                    data = json.loads(line[5:].strip() or "{}")
                except json.JSONDecodeError:
                    continue
                if event == "token":
                    if first_token is None:
                        first_token = time.perf_counter() - started
                    tokens.append(str(data.get("text", "")))
                elif event == "reasoning":
                    reasoning.append(str(data.get("text", "")))
                elif event == "step":
                    sid = int(data.get("id", 0) or 0)
                    steps[sid] = {**steps.get(sid, {}), **data}
                elif event == "meta":
                    meta = {**meta, **data} if isinstance(data, dict) else meta
                elif event == "error":
                    errors.append(data)
        return {"http": 200, "answer": "".join(tokens), "meta": meta, "steps": steps, "errors": errors,
                "reasoning_events": len(reasoning), "reasoning_chars": sum(len(x) for x in reasoning),
                "seconds": round(time.perf_counter() - started, 1),
                "ttft": round(first_token, 2) if first_token is not None else None}

    def trace(self, trace_id: str) -> Optional[dict]:
        if not trace_id:
            return None
        r = self.http.get(f"{self.base}/chat/trace/{trace_id}")
        return r.json() if r.status_code == 200 else None

    def wait_job(self, ref: dict, limit_s: float = 1800) -> dict:
        """A ref whose job is still running is polled until it publishes."""
        deadline = time.time() + limit_s
        while ref.get("status") not in ("completed", "completed_with_warnings", "failed", "cancelled") and time.time() < deadline:
            time.sleep(5)
            r = self.http.get(f"{self.base}/artifacts/{ref['artifact_id']}/v/{ref['version']}")
            if r.status_code == 200:
                ref = {**ref, **r.json()}
        return ref

    def download(self, ref: dict, f: dict) -> Optional[bytes]:
        url = f.get("download_url") or f.get("url") or f"/artifacts/{ref['artifact_id']}/v/{ref['version']}/file/{f['format']}?disposition=attachment"
        if url.startswith("/api/"):
            url = url[4:]
        r = self.http.get(url if url.startswith("http") else f"{self.base}{url}")
        return r.content if r.status_code == 200 else None

    def spec(self, ref: dict) -> Optional[dict]:
        if not self.container or not self.user_id:
            return None
        path = f"/reports/artifacts/{self.user_id}/{ref['artifact_id']}/v{ref['version']}/spec.json"
        p = subprocess.run(["docker", "exec", self.container, "cat", path], capture_output=True, timeout=30)
        if p.returncode != 0:
            return None
        try:
            return json.loads(p.stdout)
        except json.JSONDecodeError:
            return None


# ============================================================ markdown ==

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_BULLET = re.compile(r"^\s*[-*+•]\s+\S")
_NUMBERED = re.compile(r"^\s*\d{1,3}[.)]\s+\S")
_BOLD = re.compile(r"\*\*[^*\n]{1,120}\*\*|__[^_\n]{1,120}__")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


def md_metrics(text: str) -> dict:
    lines = text.splitlines()
    out = {"headings": 0, "bullets": 0, "numbered": 0, "bold": len(_BOLD.findall(text)), "code_blocks": 0,
           "tables": 0, "table_max_rows": 0, "table_max_cols": 0, "prose_lines": 0, "runon_lines": 0, "chars": len(text)}
    in_code = False
    prose_block: List[str] = []

    def flush():
        nonlocal prose_block
        if len(prose_block) >= 2:
            out["runon_lines"] += len(prose_block)
        prose_block = []

    i = 0
    while i < len(lines):
        ln = lines[i]
        if _FENCE.match(ln):
            flush()
            if not in_code:
                out["code_blocks"] += 1
            in_code = not in_code
            i += 1
            continue
        if in_code:
            i += 1
            continue
        if i + 1 < len(lines) and "|" in ln and _TABLE_SEP.match(lines[i + 1]):
            flush()
            cols = len([c for c in ln.strip().strip("|").split("|")])
            j = i + 2
            while j < len(lines) and (lines[j].strip().startswith("|") or (lines[j].count("|") >= 2 and bool(lines[j].strip()))):
                j += 1
            out["tables"] += 1
            out["table_max_rows"] = max(out["table_max_rows"], j - (i + 2))
            out["table_max_cols"] = max(out["table_max_cols"], cols)
            i = j
            continue
        s = ln.strip()
        if not s:
            flush()
        elif _HEADING.match(ln):
            flush(); out["headings"] += 1
        elif _BULLET.match(ln):
            flush(); out["bullets"] += 1
        elif _NUMBERED.match(ln):
            flush(); out["numbered"] += 1
        elif s.startswith(">") or s.startswith("|"):
            flush()
        else:
            out["prose_lines"] += 1
            # a line that ends with two spaces or a backslash is a deliberate hard break
            if ln.endswith("  ") or ln.endswith("\\"):
                flush()
            else:
                prose_block.append(s)
        i += 1
    flush()
    out["runon_ratio"] = round(out["runon_lines"] / out["prose_lines"], 2) if out["prose_lines"] else 0.0
    return out


def first_positions(text: str, groups: List[List[str]]) -> List[int]:
    low = text.lower()
    pos = []
    for g in groups:
        hits = [low.find(a.lower()) for a in g]
        hits = [h for h in hits if h >= 0]
        pos.append(min(hits) if hits else -1)
    return pos


# ================================================================ files ==

def _words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][\w'’.-]*", text))


def inspect_file(fmt: str, data: bytes) -> dict:
    out: Dict[str, Any] = {"format": fmt, "bytes": len(data), "text": "", "pages": None, "headings": 0, "tables": 0, "images": []}
    try:
        if fmt == "docx":
            from docx import Document
            d = Document(io.BytesIO(data))
            parts = []
            for p in d.paragraphs:
                parts.append(p.text)
                sn = (p.style.name or "").lower() if p.style is not None else ""
                if (sn.startswith("heading") or sn == "title") and p.text.strip():
                    out["headings"] += 1
            for t in d.tables:
                for row in t.rows:
                    parts.append(" | ".join(c.text for c in row.cells))
            out["tables"] = len(d.tables)
            out["text"] = "\n".join(parts)
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                out["images"] = [z.read(n) for n in z.namelist() if n.startswith("word/media/") and n.lower().endswith((".png", ".jpg", ".jpeg"))]
        elif fmt == "pptx":
            from pptx import Presentation
            p = Presentation(io.BytesIO(data))
            parts = []
            for s in p.slides:
                for sh in s.shapes:
                    if sh.has_text_frame:
                        parts.append(sh.text_frame.text)
                    if getattr(sh, "has_table", False) and sh.has_table:
                        out["tables"] += 1
                    if getattr(sh, "has_chart", False) and sh.has_chart:
                        out.setdefault("native_charts", 0)
                        out["native_charts"] += 1
            out["pages"] = len(p.slides)
            out["text"] = "\n".join(parts)
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                out["images"] = [z.read(n) for n in z.namelist() if n.startswith("ppt/media/") and n.lower().endswith((".png", ".jpg", ".jpeg"))]
        elif fmt == "pdf":
            import pypdfium2 as pdfium
            import pypdfium2.raw as pdfium_c
            doc = pdfium.PdfDocument(data)
            try:
                out["pages"] = len(doc)
                texts = []
                for i in range(len(doc)):
                    page = doc[i]
                    texts.append(page.get_textpage().get_text_range())
                    for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=3):
                        try:
                            img = obj.get_bitmap().to_pil()
                            if img.width >= 200 and img.height >= 120:
                                buf = io.BytesIO(); img.save(buf, "PNG"); out["images"].append(buf.getvalue())
                        except Exception:
                            pass
                out["text"] = "\n".join(texts)
            finally:
                doc.close()
        elif fmt in ("png", "jpg", "jpeg"):
            out["images"] = [data]
        elif fmt == "xlsx":
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(data))
            out["native_charts"] = sum(len(getattr(ws, "_charts", [])) for ws in wb.worksheets)
            out["text"] = "\n".join(str(c.value) for ws in wb.worksheets for row in ws.iter_rows() for c in row if c.value is not None)[:200000]
        elif fmt in ("csv", "md", "txt", "html", "svg"):
            out["text"] = data.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — an unreadable file is a finding
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["words"] = _words(out["text"])
    return out


def hue_profile(png: bytes, min_saturation: float = 0.35) -> dict:
    """Distinct saturated hues in an image (18 bins of 20°, a bin counts at >= 4% of the coloured pixels).

    `min_saturation` is the floor a pixel must clear to count as coloured.
    The default is what the baseline scored with, so the 40 baseline cases
    re-score unchanged; the status-colour check asks for a lower floor,
    because a status fill may be a pale tint (style.STATUS_PAIRS) rather
    than a saturated series colour.
    """
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGB")
    im.thumbnail((320, 320))
    px = list(im.getdata())
    bins = [0] * 18
    coloured = 0
    for r, g, b in px:
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s >= min_saturation and v >= 0.25:
            coloured += 1
            bins[int(h * 18) % 18] += 1
    total = len(px) or 1
    if coloured < 0.005 * total:
        return {"hues": 0, "dominant": None, "bins": [], "coloured_share": round(coloured / total, 3)}
    # merge the wrap-around red bins
    kept = [i for i, n in enumerate(bins) if n >= 0.04 * coloured]
    return {"hues": len(kept), "dominant": max(range(18), key=lambda i: bins[i]) * 20,
            "bins": [i * 20 for i in kept], "coloured_share": round(coloured / total, 3)}


#: Hue bands (degrees, inclusive) the status tokens land in: success is green,
#: warning amber/yellow, danger red. A status chart must show green AND red —
#: the two ends — whatever exact tokens the renderer picked.
STATUS_BANDS = {"success": (75, 165), "warning": (25, 70), "danger": (-25, 25)}


def _in_band(hue: int, band: Tuple[int, int]) -> bool:
    lo, hi = band
    return lo <= hue <= hi or (lo < 0 and hue >= 360 + lo)


def status_hue_verdict(profiles: Iterable[dict]) -> Tuple[bool, str]:
    """(ok, detail): one image shows both a success and a danger hue."""
    seen = []
    for p in profiles:
        hues = p.get("bins") or ([p["dominant"]] if p.get("dominant") is not None else [])
        classes = {name for name, band in STATUS_BANDS.items() for h in hues if _in_band(int(h), band)}
        seen.append((sorted(hues), sorted(classes)))
        if {"success", "danger"} <= classes:
            return True, f"hues={sorted(hues)} -> {sorted(classes)}"
    return False, f"no image carries both a success and a danger hue: {seen}"


# =============================================================== charts ==

def find_charts(node: Any) -> List[dict]:
    found: List[dict] = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") in CHART_TYPES and ("series" in n or "data" in n) and "categories" in n:
                found.append(n)
                return
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return found


def block_counts(spec: Any) -> Dict[str, int]:
    """Block types in a spec (heading, paragraph, bullets, table, chart …)."""
    counts: Dict[str, int] = defaultdict(int)

    def walk(n, parent=""):
        if isinstance(n, dict):
            if parent in ("blocks", "slides", "items") and isinstance(n.get("type"), str):
                counts[n["type"]] += 1
            for k, v in n.items():
                walk(v, k)
        elif isinstance(n, list):
            for v in n:
                walk(v, parent)

    walk(spec)
    return dict(counts)


def block_sequence(spec: Any) -> List[dict]:
    """The document's blocks in order, as {type, level, text}.

    A document spec is `{"blocks": [ {"type": "heading", "level": 2, "text": …}, … ]}`
    (app/artifacts/spec.py). A deck keeps its blocks per slide, so the slides'
    blocks are concatenated with the slide title as a level-1 heading.
    """
    out: List[dict] = []

    def one(b: dict) -> dict:
        return {"type": str(b.get("type") or ""), "level": int(b.get("level") or 0),
                "text": str(b.get("text") or (b.get("chart") or {}).get("title") or (b.get("table") or {}).get("title") or "")}

    if not isinstance(spec, dict):
        return out
    for slide in spec.get("slides") or []:
        if isinstance(slide, dict):
            if slide.get("title"):
                out.append({"type": "heading", "level": 1, "text": str(slide["title"])})
            out += [one(b) for b in (slide.get("blocks") or []) if isinstance(b, dict)]
    out += [one(b) for b in (spec.get("blocks") or []) if isinstance(b, dict)]
    return out


def chart_follows_heading(blocks: List[dict], alternatives: List[str]) -> Tuple[bool, str]:
    """(ok, detail): a chart block sits under one of these headings.

    "Under" means after the heading and before the next heading of the same or
    a higher level — which is what "add a chart to the Regional performance
    section" asks for, and what a whole-file rewrite gets wrong.
    """
    lowered = [a.lower() for a in alternatives]
    heads = [(i, b) for i, b in enumerate(blocks) if b["type"] == "heading"]
    for n, (i, b) in enumerate(heads):
        if not any(a in b["text"].lower() for a in lowered):
            continue
        end = len(blocks)
        for j, nxt in heads[n + 1:]:
            if nxt["level"] <= b["level"]:
                end = j
                break
        kinds = [x["type"] for x in blocks[i + 1:end]]
        if "chart" in kinds:
            return True, f"{b['text']!r} (level {b['level']}) is followed by {kinds}"
        return False, f"{b['text']!r} (level {b['level']}) holds {kinds or 'nothing'}"
    return False, f"no heading matching {alternatives} in {[b['text'] for _, b in heads][:12]}"


#: What an assistant says when it believes it added a chart. If the version it
#: produced has no more charts than the previous one, the sentence is false —
#: the owner saw exactly this ("also i want Plots on this docs" answered with a
#: claim and no chart), and the gate requires zero of them.
_CHART_CLAIM = re.compile(
    r"\b(added|inserted|included|placed|created|generated)\b[^.\n]{0,60}\b(chart|graph|plot|visuali[sz]ation)s?\b"
    r"|\b(chart|graph|plot)s?\b[^.\n]{0,40}\b(has|have|was|were)\b[^.\n]{0,20}\b(added|inserted|included)\b",
    re.IGNORECASE)


def claims_chart_added(text: str) -> bool:
    return bool(_CHART_CLAIM.search(text or ""))


def _num(v: str) -> Optional[float]:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _bucket(value: str, bucket: Optional[str]) -> str:
    v = str(value).strip()
    m = re.match(r"(\d{4})-(\d{2})", v)
    if not bucket or not m:
        return v
    y, mo = m.group(1), int(m.group(2))
    return {"year": y, "month": f"{y}-{mo:02d}", "quarter": f"{y}-Q{(mo - 1) // 3 + 1}"}.get(bucket, v)


def recompute(chart: dict, csv_path: str) -> Tuple[Optional[bool], str]:
    """(matches, why). None = not verifiable with a simple recompute."""
    b = chart.get("data") or {}
    if b.get("filters") or b.get("bins") or b.get("group_by") or b.get("y2") or chart.get("type") in ("scatter", "histogram", "box", "violin", "bubble", "gantt", "candlestick", "heatmap"):
        return None, "complex binding"
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    cols = {c.lower().replace(" ", "").replace("_", ""): c for c in rows[0].keys()}

    def col(name):
        return cols.get(str(name or "").lower().replace(" ", "").replace("_", ""))

    x = col(b.get("x"))
    if not x:
        return None, f"x column {b.get('x')!r} not in the CSV"
    agg = b.get("agg") or "sum"
    ys = [col(y) for y in (b.get("y") or [])]
    if any(y is None for y in ys):
        return False, f"y columns {b.get('y')} not all in the CSV"
    groups: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        groups[_bucket(r[x], b.get("date_bucket"))].append(r)
    series = chart.get("series") or []
    cats = [str(c) for c in (chart.get("categories") or [])]
    if not series or not cats:
        return False, "no categories/series"
    checks, bad = 0, []
    for si, s in enumerate(series[: max(1, len(ys))]):
        ycol = ys[si] if ys else None
        for c, v in zip(cats, s.get("values") or []):
            if c.lower() in ("other", "others", "all other", "rest"):
                continue
            g = groups.get(c)
            if g is None:
                # a bucket label the renderer formatted differently ("2022" vs "2022-01")
                keys = [k for k in groups if k.startswith(c) or c.startswith(k)]
                if len(keys) != 1:
                    bad.append(f"{c}: category not in data")
                    continue
                g = groups[keys[0]]
            if agg == "count" or ycol is None:
                exp = float(len(g))
            else:
                vals = [n for n in (_num(r[ycol]) for r in g) if n is not None]
                if not vals:
                    continue
                exp = {"sum": sum(vals), "avg": sum(vals) / len(vals), "min": min(vals), "max": max(vals),
                       "median": sorted(vals)[len(vals) // 2]}.get(agg, sum(vals))
            checks += 1
            if abs(float(v) - exp) > max(0.01, abs(exp) * 0.005):
                bad.append(f"{c}: chart {v} vs data {round(exp, 2)}")
    if not checks:
        return None, "nothing comparable"
    return (len(bad) == 0), ("; ".join(bad[:4]) if bad else f"{checks} values match")



# ======================================================= chat charts ==

_MERMAID = re.compile(r"```mermaid\s*\n(.*?)```", re.S)


def mermaid_charts(text: str) -> List[dict]:
    """Charts drawn in the chat answer as mermaid (pie / xychart-beta)."""
    out = []
    for body in _MERMAID.findall(text or ""):
        head = body.strip().splitlines()[0].strip().lower() if body.strip() else ""
        if head.startswith("pie"):
            pairs = re.findall(r'"([^"]+)"\s*:\s*([-\d.,]+)', body)
            out.append({"type": "pie", "source": "mermaid", "categories": [a for a, _ in pairs],
                        "values": [_num(b) for _, b in pairs]})
        elif head.startswith("xychart"):
            cats = re.search(r"x-axis\s*\[(.*?)\]", body)
            vals = re.search(r"(?:bar|line)\s*\[(.*?)\]", body)
            out.append({"type": "bar" if re.search(r"\bbar\s*\[", body) else "line", "source": "mermaid",
                        "categories": [c.strip().strip('"') for c in cats.group(1).split(",")] if cats else [],
                        "values": [_num(v) for v in vals.group(1).split(",")] if vals else []})
    return out


def truth_match(categories: List[str], values: List[Optional[float]], truth: dict) -> Tuple[Optional[bool], str]:
    """Compare a literal chart with every ground-truth aggregate of the upload whose keys cover the categories."""
    cands = []
    for grp in ("count_by", "sum_by"):
        for dim, d in (truth.get(grp) or {}).items():
            if grp == "count_by":
                cands.append((f"count by {dim}", d))
            else:
                for measure, dd in d.items():
                    cands.append((f"sum of {measure} by {dim}", dd))
    cats = [str(c).strip().lower() for c in categories]
    for name, d in cands:
        low = {str(k).lower(): v for k, v in d.items()}
        if cats and all(c in low for c in cats):
            bad = [f"{c}: {v} vs {low[c]}" for c, v in zip(cats, values) if v is None or abs(v - low[c]) > max(0.01, abs(low[c]) * 0.005)]
            return (not bad), (f"{name}: " + ("; ".join(bad[:4]) if bad else "values match"))
    return None, "categories match no ground-truth aggregate"

# ================================================================ check ==

DIMENSION = {
    "artifact": "deliverable", "formats_any": "deliverable", "kind_any": "deliverable", "same_artifact": "deliverable",
    "chart_type_any": "deliverable", "no_failure_text": "deliverable", "job_completed": "deliverable",
    "min_headings": "structure", "min_bullets": "structure", "min_numbered": "structure", "min_bold": "structure",
    "min_code_blocks": "structure", "min_table": "structure", "no_runon": "structure", "order": "structure",
    "doc_min_headings": "structure", "doc_min_tables": "structure",
    "min_chars": "length", "max_chars": "length", "doc_min_pages": "length", "doc_min_words": "length", "doc_growth": "length",
    "must_contain": "fidelity", "forbidden": "fidelity", "charts_real_values": "fidelity",
    "min_charts": "charts", "charts_multicolour": "charts",
    "thinking_off": "thinking",
    # Added 2026-09-17 with the acceptance cases (C01, E07, R07, P09, F07, K01)
    # and the coding cases. Every one is keyed on an `expect` none of the 40
    # baseline cases carries, so runs/baseline-20260917 re-scores unchanged.
    "doc_max_pages": "length", "doc_max_words": "length", "doc_max_growth": "length",
    "chart_after_heading": "charts", "chart_hues_max": "charts", "chart_subject_hues_min": "charts",
    "status_colours": "charts", "no_chart_claimed": "fidelity",
    "code_present": "code", "code_runs": "code", "code_correct": "code",
}


def check_turn(exp: dict, res: dict, *, effort: str, fail_text: Iterable[str], prev_file: Optional[dict],
               upload_path: Optional[str]) -> List[dict]:
    out: List[dict] = []

    def add(name, ok, detail=""):
        out.append({"check": name, "dimension": DIMENSION.get(name, "other"), "ok": bool(ok), "detail": str(detail)[:300]})

    answer = res.get("answer") or ""
    low = answer.lower()
    md = res["md"]
    art = res.get("artifact")  # {ref, files:[inspect], charts:[...], hues:[...]}
    doc_text = "\n".join(f.get("text", "") for f in (art or {}).get("files", []))
    both = (answer + "\n" + doc_text).lower()

    add("no_failure_text", not any(t in low for t in fail_text), next((t for t in fail_text if t in low), ""))
    if effort == "fast":
        think_trace = res.get("trace_thinks")
        ok = res.get("reasoning_events", 0) == 0 and not (res.get("meta") or {}).get("adaptive_thinking") and not think_trace
        add("thinking_off", ok, f"reasoning_events={res.get('reasoning_events')} chars={res.get('reasoning_chars')} "
                                f"adaptive={bool((res.get('meta') or {}).get('adaptive_thinking'))} trace={think_trace}")
    if "artifact" in exp:
        has = bool(art and art.get("ref"))
        add("artifact", has == exp["artifact"], f"route={(res.get('meta') or {}).get('route')} artifact={has}")
        if has:
            st = art["ref"].get("status")
            add("job_completed", st in ("completed", "completed_with_warnings"), st)
    fmts = [f.get("format") for f in (art or {}).get("files", [])]
    if "formats_any" in exp:
        add("formats_any", any(f in exp["formats_any"] for f in fmts), fmts)
    if "kind_any" in exp:
        add("kind_any", art is not None and art["ref"].get("kind") in exp["kind_any"], (art or {}).get("ref", {}).get("kind"))
    if exp.get("same_artifact"):
        ok = bool(art and prev_file and art["ref"]["artifact_id"] == prev_file["ref"]["artifact_id"]
                  and int(art["ref"]["version"]) > int(prev_file["ref"]["version"]))
        add("same_artifact", ok, f"prev={prev_file and prev_file['ref']['artifact_id'][:8]} v{prev_file and prev_file['ref']['version']} "
                                 f"now={art and art['ref']['artifact_id'][:8]} v{art and art['ref']['version']}")
    for key, metric in (("min_headings", "headings"), ("min_bullets", "bullets"), ("min_numbered", "numbered"),
                        ("min_bold", "bold"), ("min_code_blocks", "code_blocks")):
        if key in exp:
            v = md[metric]
            if key == "min_bullets":
                v = md["bullets"] + md["numbered"]
            add(key, v >= exp[key], f"{metric}={v} need>={exp[key]}")
    if "min_table_rows" in exp or "min_table_cols" in exp:
        ok = md["table_max_rows"] >= exp.get("min_table_rows", 1) and md["table_max_cols"] >= exp.get("min_table_cols", 2)
        add("min_table", ok, f"rows={md['table_max_rows']} cols={md['table_max_cols']}")
    if exp.get("no_runon"):
        ok = not (md["runon_lines"] >= 6 and md["runon_ratio"] >= 0.4)
        add("no_runon", ok, f"runon_lines={md['runon_lines']} ratio={md['runon_ratio']}")
    if "min_chars" in exp:
        add("min_chars", len(answer) >= exp["min_chars"], f"chars={len(answer)} need>={exp['min_chars']}")
    if "max_chars" in exp:
        add("max_chars", len(answer) <= exp["max_chars"], f"chars={len(answer)} max={exp['max_chars']}")
    if "must_contain" in exp:
        missing = [g for g in exp["must_contain"] if not any(a.lower() in both for a in g)]
        add("must_contain", not missing, f"missing={missing}")
    if "forbidden" in exp:
        hits = [f for f in exp["forbidden"] if f.lower() in both]
        add("forbidden", not hits, f"found={hits}")
    if "order" in exp:
        pos = first_positions(answer, exp["order"])
        present = [p for p in pos if p >= 0]
        ok = all(p >= 0 for p in pos) and present == sorted(present)
        add("order", ok, f"positions={pos}")
    if art:
        pages = max([f.get("pages") or 0 for f in art["files"]] + [int(art["ref"].get("preview_pages") or 0)])
        words = max([f.get("words", 0) for f in art["files"]] or [0])
        blocks = art.get("spec_blocks") or {}
        heads = max([f.get("headings", 0) for f in art["files"]] + [int(blocks.get("heading", 0))])
        tables = max([f.get("tables", 0) for f in art["files"]] + [int(blocks.get("table", 0))])
        if "doc_min_pages" in exp:
            add("doc_min_pages", pages >= exp["doc_min_pages"], f"pages={pages} need>={exp['doc_min_pages']}")
        if "doc_min_words" in exp:
            add("doc_min_words", words >= exp["doc_min_words"], f"words={words} need>={exp['doc_min_words']}")
        if "doc_min_headings" in exp:
            add("doc_min_headings", heads >= exp["doc_min_headings"], f"headings={heads}")
        if "doc_min_tables" in exp:
            add("doc_min_tables", tables >= exp["doc_min_tables"], f"tables={tables}")
        if "doc_growth" in exp and prev_file:
            pw = max([f.get("words", 0) for f in prev_file["files"]] or [0])
            add("doc_growth", pw > 0 and words >= exp["doc_growth"] * pw, f"words {pw} -> {words}")
        if "doc_max_pages" in exp:
            add("doc_max_pages", pages <= exp["doc_max_pages"], f"pages={pages} max={exp['doc_max_pages']}")
        if "doc_max_words" in exp:
            add("doc_max_words", words <= exp["doc_max_words"], f"words={words} max={exp['doc_max_words']}")
        if "doc_max_growth" in exp and prev_file:
            pw = max([f.get("words", 0) for f in prev_file["files"]] or [0])
            add("doc_max_growth", pw > 0 and words <= exp["doc_max_growth"] * pw,
                f"words {pw} -> {words} (max x{exp['doc_max_growth']})")
    else:
        for key in ("doc_min_pages", "doc_min_words", "doc_min_headings", "doc_min_tables", "doc_growth"):
            if key in exp:
                add(key, False, "no file")
        # doc_max_* are ceilings: an ask that produced no file at all did not
        # break them, and its own `artifact` check already says what happened.
    charts = (art or {}).get("charts")
    images = (art or {}).get("hues") or []
    chat_charts = mermaid_charts(answer)
    n_charts = (len(charts) if charts is not None else sum(1 for h in images if h["hues"] > 0) + sum(f.get("native_charts", 0) for f in (art or {}).get("files", []))) + len(chat_charts)
    truth = {}
    if upload_path:
        tp = os.path.join(os.path.dirname(upload_path), "truth.json")
        if os.path.exists(tp):
            truth = json.load(open(tp)).get(os.path.basename(upload_path), {})
    if "min_charts" in exp:
        add("min_charts", n_charts >= exp["min_charts"], f"charts={n_charts} (spec={'yes' if charts is not None else 'no'}) images={len(images)} chat_mermaid={len(chat_charts)}")
    if "chart_type_any" in exp:
        types = [c.get("type") for c in (charts or [])] + [c["type"] for c in chat_charts]
        add("chart_type_any", any(t in exp["chart_type_any"] for t in types), types)
    if exp.get("charts_multicolour"):
        multi = [h for h in images if h["hues"] >= 2]
        same = len(images) >= 2 and all(h["hues"] <= 1 for h in images) and len({h["dominant"] for h in images}) == 1
        mermaid_multi = any(c["type"] == "pie" for c in chat_charts)  # mermaid pies draw each slice in its own theme colour
        add("charts_multicolour", (bool(images) and bool(multi) and not same) or (not images and mermaid_multi),
            f"images={len(images)} hues={[h['hues'] for h in images]} dominant={[h['dominant'] for h in images]} mermaid_pie={mermaid_multi}")
    if exp.get("charts_real_values"):
        if not charts and not chat_charts:
            add("charts_real_values", False, "no chart in the spec or the answer")
        else:
            verdicts = []
            for c in chat_charts:
                ok, why = truth_match(c["categories"], c["values"], truth) if truth else (None, "no truth")
                verdicts.append((bool(ok), f"chat {c['type']}: {why}"))
            for c in (charts or []):
                prov = c.get("provenance") or {}
                if c.get("data") is None:
                    verdicts.append((False, "literal numbers typed by the model"))
                    continue
                if upload_path and prov.get("table_provenance") not in ("upload", "sheet", "data"):
                    verdicts.append((False, f"bound to {prov.get('table_provenance')} not the upload"))
                    continue
                if upload_path:
                    ok, why = recompute(c, upload_path)
                    verdicts.append((ok if ok is not None else True, why))
                else:
                    verdicts.append((True, "bound"))
            add("charts_real_values", all(v for v, _ in verdicts), "; ".join(w for _, w in verdicts)[:300])
    if "chart_after_heading" in exp:
        seq = (art or {}).get("block_seq") or []
        if not seq:
            add("chart_after_heading", False, "no document blocks to read (no file, or no spec)")
        else:
            ok, why = chart_follows_heading(seq, exp["chart_after_heading"])
            add("chart_after_heading", ok, why)
    if "chart_hues_max" in exp:
        worst = max([h["hues"] for h in images] or [0])
        add("chart_hues_max", bool(images) and worst <= exp["chart_hues_max"],
            f"images={len(images)} hues={[h['hues'] for h in images]} max={exp['chart_hues_max']}")
    if "chart_subject_hues_min" in exp:
        doms = {h["dominant"] for h in images if h.get("dominant") is not None}
        add("chart_subject_hues_min", len(doms) >= exp["chart_subject_hues_min"],
            f"images={len(images)} dominant={sorted(doms)} need>={exp['chart_subject_hues_min']}")
    if exp.get("status_colours"):
        soft = (art or {}).get("hues_soft") or images
        ok, why = status_hue_verdict(soft)
        add("status_colours", bool(soft) and ok, why if soft else "no chart image")
    if exp.get("no_chart_claimed"):
        prev_charts = chart_count(prev_file)
        claimed = claims_chart_added(answer)
        add("no_chart_claimed", (not claimed) or n_charts > prev_charts,
            f"claim={claimed} charts {prev_charts} -> {n_charts}")
    if exp.get("code"):
        cr = res.get("code_result") or {}
        steps = cr.get("steps") or []
        add("code_present", bool(cr.get("source")), f"lang={exp['code'].get('lang')} blocks={cr.get('blocks', 0)}")
        build = [s for s in steps if s.get("stage") == "build"]
        add("code_runs", bool(build) and all(s["ok"] for s in build),
            "; ".join(f"{s['name']}: rc={s['rc']} {s.get('tail', '')}" for s in build if not s["ok"])[:300] or
            f"{len(build)} build step(s) ok")
        checks = [s for s in steps if s.get("stage") == "check"]
        add("code_correct", bool(checks) and all(s["ok"] for s in checks),
            "; ".join(f"{s['name']}: {s.get('tail', '')}" for s in checks if not s["ok"])[:300] or
            f"{len(checks)} check(s) ok")
    return out


def chart_count(art: Optional[dict]) -> int:
    """Charts in a produced version, counted the way check_turn counts them."""
    if not art:
        return 0
    if art.get("charts") is not None:
        return len(art["charts"])
    return (sum(1 for h in art.get("hues") or [] if h["hues"] > 0)
            + sum(f.get("native_charts", 0) for f in art.get("files") or []))
