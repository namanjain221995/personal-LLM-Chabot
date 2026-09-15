#!/usr/bin/env python3
"""Independent scorer for the artifact self-check: the FALSE-SATISFIED rate.

A claim is an item selfcheck.json marks `pass` — something the answer
sentence is allowed to say was done (false_claim_guard.claimable). This
script re-checks every claim with readers written SEPARATELY from
app/artifacts/inspect_files.py and shares no code with it:

  * DOCX/PPTX/XLSX are converted to PDF by LibreOffice (soffice --headless),
    a different renderer from the one that produced the files;
  * page geometry comes from `pdfinfo`, fonts from `pdffonts`, words and
    their boxes from `pdftotext -bbox-layout`, pixels from `pdftoppm`
    (text colour = the most saturated/dark pixels inside a word's box,
    background = the most common pixel in the box);
  * DOCX margins, XLSX rows/headers/charts and CSV rows are read from the
    raw package XML / text with ElementTree and the csv module.

Each claim scores confirmed | refuted | unscorable. The headline number is
refuted / (confirmed + refuted). A claim this script has no independent
reader for is `unscorable`, never confirmed.

Usage:
    python scripts/as3_selfcheck_score.py <jobs_dir> [--json out.json]

<jobs_dir>/<job>/ holds the version's files, selfcheck.json and request.json
({"instruction": ..., "expect_unmet": bool}).
"""
from __future__ import annotations

import argparse
import colorsys
import csv
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SKIP_CATEGORIES = {"security", "house_style", "preservation", "language"}


def run(cmd: List[str], timeout: int = 120) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False).stdout


def to_pdf(path: Path, tmp: Path) -> Optional[Path]:
    if path.suffix.lower() == ".pdf":
        return path
    out = tmp / (path.stem + ".pdf")
    if not out.exists():
        profile = tmp / "lo-profile"
        run(["soffice", f"-env:UserInstallation=file://{profile}", "--headless", "--convert-to", "pdf", "--outdir", str(tmp), str(path)], timeout=180)
    return out if out.exists() else None


def pages(pdf: Path) -> List[Tuple[float, float]]:
    info = run(["pdfinfo", "-f", "1", "-l", "999", str(pdf)])
    sizes = [(float(a), float(b)) for a, b in re.findall(r"Page\s+\d+\s+size:\s+([\d.]+)\s+x\s+([\d.]+)", info)]
    if not sizes:
        m = re.search(r"Page size:\s+([\d.]+)\s+x\s+([\d.]+)", info)
        sizes = [(float(m.group(1)), float(m.group(2)))] if m else []
    return sizes


def words(pdf: Path) -> List[dict]:
    out = run(["pdftotext", "-bbox-layout", str(pdf), "-"])
    result: List[dict] = []
    page = -1
    for line in out.splitlines():
        if "<page " in line:
            page += 1
        m = re.search(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*)</word>', line)
        if m:
            result.append({"page": page, "box": tuple(float(m.group(i)) for i in range(1, 5)), "text": html.unescape(m.group(5))})
    return result


def text_of(pdf: Path) -> str:
    return run(["pdftotext", "-layout", str(pdf), "-"])


def fold(s: str) -> str:
    s = re.sub(r"[*_`#|]+", " ", s or "").replace(chr(0xA0), " ")
    s = re.sub(r"(?<=\d),(?=\d)", "", s)
    return " ".join(s.split()).casefold()


def raster(pdf: Path, page: int, tmp: Path, dpi: int = 110):
    from PIL import Image

    stem = tmp / f"{pdf.stem}-p{page}"
    png = Path(str(stem) + ".png")
    if not png.exists():
        run(["pdftoppm", "-r", str(dpi), "-f", str(page + 1), "-l", str(page + 1), "-singlefile", "-png", str(pdf), str(stem)])
    return Image.open(png).convert("RGB") if png.exists() else None


def box_colours(pdf: Path, word: dict, tmp: Path, dpi: int = 110) -> Tuple[Optional[Tuple[int, int, int]], Optional[Tuple[int, int, int]]]:
    img = raster(pdf, word["page"], tmp, dpi)
    if img is None:
        return None, None
    k = dpi / 72.0
    x0, y0, x1, y1 = [int(v * k) for v in word["box"]]
    crop = img.crop((max(0, x0), max(0, y0), max(x0 + 1, x1), max(y0 + 1, y1)))
    counts = crop.getcolors(maxcolors=1 << 20) or []
    if not counts:
        return None, None
    counts.sort(reverse=True)
    background = counts[0][1]

    def distance(c):
        return sum(abs(a - b) for a, b in zip(c, background))

    ink = [c for n, c in counts if distance(c) > 120]
    text = max(ink, key=distance) if ink else None
    return text, background


def hls(rgb):
    return colorsys.rgb_to_hls(*(v / 255.0 for v in rgb))


def colour_ok(observed: Optional[Tuple[int, int, int]], expected_hex: str, named: bool) -> bool:
    if observed is None:
        return False
    e = tuple(int(expected_hex.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    if not named:
        return all(abs(a - b) <= 40 for a, b in zip(observed, e))  # raster anti-aliasing tolerance
    h1, l1, s1 = hls(observed)
    h2, l2, s2 = hls(e)
    if s2 < 0.12 or l2 in (0.0, 1.0):
        return abs(l1 - l2) <= 0.3
    diff = abs(h1 - h2) * 360
    return s1 >= 0.1 and min(diff, 360 - diff) <= 30


def find_words(ws: List[dict], phrase: str) -> List[dict]:
    """First words of every occurrence of `phrase` as CONSECUTIVE words on
    one line (a heading's first word also appears in body text)."""
    parts = fold(phrase).split(" ") if phrase else []
    hits = []
    for i, w in enumerate(ws):
        seq = ws[i:i + len(parts)]
        if len(seq) == len(parts) and all(fold(s["text"]) == p for s, p in zip(seq, parts)) and all(abs(s["box"][1] - w["box"][1]) < 3 for s in seq):
            hits.append(w)
    return hits


def header_row_words(ws: List[dict], headers: List[str]) -> List[dict]:
    """One occurrence per header phrase, all on the same line: the row."""
    occurrences = [find_words(ws, h) for h in headers if h]
    best: List[dict] = []
    for cand in (occurrences[0] if occurrences else []):
        row = [cand]
        for other in occurrences[1:]:
            near = [o for o in other if o["page"] == cand["page"] and abs(o["box"][1] - cand["box"][1]) < 3]
            if near:
                row.append(near[0])
        if len(row) > len(best):
            best = row
    return best


def docx_xml(path: Path, part: str):
    with zipfile.ZipFile(path) as z:
        return ET.fromstring(z.read(part))


def docx_headings(path: Path) -> List[str]:
    root = docx_xml(path, "word/document.xml")
    styles = docx_xml(path, "word/styles.xml")
    names = {s.get(W + "styleId"): (s.find(W + "name").get(W + "val") if s.find(W + "name") is not None else "") for s in styles.iter(W + "style")}
    out = []
    for p in root.iter(W + "p"):
        ps = p.find(f"{W}pPr/{W}pStyle")
        if ps is not None and names.get(ps.get(W + "val"), "").lower().startswith("heading"):
            out.append("".join(t.text or "" for t in p.iter(W + "t")))
    return out


def score_claim(item: dict, files: Dict[str, Path], tmp: Path, request: dict) -> Tuple[str, str]:
    cat, target, prop, expected = item["category"], item["target"], item["property"], item["expected"]
    loc = item.get("locator") or {}
    docs = [p for p in files.values() if p.suffix.lower() in (".docx", ".pdf", ".pptx")]
    if cat == "format":
        want = "." + str(expected)
        match = [p for p in files.values() if p.suffix.lower() == want]
        if not match:
            return "refuted", "no file"
        p = match[0]
        ok = p.read_bytes()[:4] == b"%PDF" if want == ".pdf" else (zipfile.is_zipfile(p) if want in (".docx", ".xlsx", ".pptx") else p.stat().st_size > 0)
        return ("confirmed" if ok else "refuted"), p.name
    if cat == "layout" and prop in ("orientation", "page_size"):
        verdicts = []
        for p in docs:
            pdf = to_pdf(p, tmp)
            if pdf is None:
                continue
            for w, h in pages(pdf):
                if prop == "orientation":
                    verdicts.append(("landscape" if w > h else "portrait") == expected)
                else:
                    short, long_ = sorted((w, h))
                    name = {"A4": (595, 842), "Letter": (612, 792), "Legal": (612, 1008)}.get(expected)
                    verdicts.append(bool(name) and abs(short - name[0]) < 6 and abs(long_ - name[1]) < 6)
        if not verdicts:
            return "unscorable", "no pages"
        return ("confirmed" if all(verdicts) else "refuted"), f"{sum(verdicts)}/{len(verdicts)} pages"
    if cat == "layout" and prop == "page_numbers":
        for p in docs:
            pdf = to_pdf(p, tmp)
            if pdf is None:
                continue
            ws = words(pdf)
            sizes = pages(pdf)
            numbered = {w["page"] for w in ws if re.fullmatch(r"\d{1,4}", w["text"]) and (w["box"][1] > sizes[min(w["page"], len(sizes) - 1)][1] * 0.88 or w["box"][3] < sizes[0][1] * 0.1)}
            return ("confirmed" if numbered else "refuted"), f"{len(numbered)} numbered pages"
        return "unscorable", ""
    if cat == "layout" and prop == "margins":
        docx = [p for p in files.values() if p.suffix == ".docx"]
        if not docx:
            return "unscorable", ""
        mar = docx_xml(docx[0], "word/document.xml").find(f".//{W}sectPr/{W}pgMar")
        mm = {k: int(mar.get(W + k)) / 1440 * 25.4 for k in ("left", "right", "top", "bottom")}
        ok = max(mm.values()) <= 14 if expected == "narrow" else min(mm["left"], mm["right"]) >= 24
        return ("confirmed" if ok else "refuted"), str({k: round(v) for k, v in mm.items()})
    if cat == "content":
        name = target.split(":", 1)[-1]
        for p in docs:
            pdf = to_pdf(p, tmp)
            if pdf is not None:
                return ("confirmed" if fold(name) in fold(text_of(pdf)) else "refuted"), p.name
        return "unscorable", ""
    if cat == "data" and prop == "row_count":
        for p in files.values():
            if p.suffix == ".csv":
                rows = [r for r in csv.reader(p.read_text(encoding="utf-8-sig").splitlines()) if any(c.strip() for c in r)]
                return ("confirmed" if len(rows) - 1 == int(expected) else "refuted"), f"{len(rows) - 1} csv rows"
            if p.suffix == ".xlsx":
                with zipfile.ZipFile(p) as z:
                    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
                ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
                rows = [r for r in sheet.iter(ns + "row") if r.findall(ns + "c")]
                data_rows = len(rows) - 1 - sum(1 for r in rows if any((c.find(ns + "f") is not None) for c in r.findall(ns + "c")))
                return ("confirmed" if data_rows == int(expected) else "refuted"), f"{data_rows} xlsx rows"
        return "unscorable", ""
    if cat == "faithfulness":
        return "unscorable", "scored separately against the source answer"
    if cat == "chart" and prop == "type":
        for p in files.values():
            if p.suffix in (".xlsx", ".pptx"):
                with zipfile.ZipFile(p) as z:
                    xml = " ".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist() if "/charts/chart" in n)
                tag = {"bar": "barChart", "line": "lineChart", "pie": "pieChart", "scatter": "scatterChart", "area": "areaChart"}.get(str(expected))
                if tag:
                    return ("confirmed" if tag in xml else "refuted"), p.name
        return "unscorable", ""
    if cat == "style" and target in ("title", "heading", "heading1", "heading2", "table_header", "slide_title") and prop in ("color", "background"):
        for p in files.values():
            if p.suffix.lower() not in (".docx", ".pdf", ".pptx"):
                continue
            pdf = to_pdf(p, tmp)
            if pdf is None:
                continue
            ws = words(pdf)
            phrases: List[str] = []
            if target.startswith("heading") and p.suffix == ".docx":
                phrases = docx_headings(p)[:3]
            elif target == "title":
                phrases = [request.get("title") or ""]
            elif target == "table_header":
                phrases = request.get("table_headers") or []
            hits = header_row_words(ws, phrases) if target == "table_header" else [w for ph in phrases if ph for w in find_words(ws, ph)[:1]]
            if not hits:
                continue
            verdicts = []
            for w in hits:
                text, bg = box_colours(pdf, w, tmp)
                verdicts.append(colour_ok(text if prop == "color" else bg, str(expected), bool(loc.get("color_name"))))
            return ("confirmed" if all(verdicts) else "refuted"), f"{sum(verdicts)}/{len(verdicts)} words via {p.suffix}"
        return "unscorable", "no words located"
    if cat == "style" and prop == "font_family":
        for p in docs:
            pdf = to_pdf(p, tmp)
            if pdf is None:
                continue
            fonts = fold(run(["pdffonts", str(pdf)]))
            fam = fold(str(expected)).replace(" ", "")
            subs = {"calibri": "carlito", "cambria": "caladea", "arial": "liberationsans", "timesnewroman": "liberationserif", "georgia": "gelasio"}
            ok = fam in fonts.replace(" ", "") or subs.get(fam, "\0") in fonts.replace(" ", "")
            return ("confirmed" if ok else "refuted"), p.name
        return "unscorable", ""
    return "unscorable", f"no independent reader for {cat}/{prop}"


def score_job(job_dir: Path) -> dict:
    report = json.loads((job_dir / "selfcheck.json").read_text())
    request = json.loads((job_dir / "request.json").read_text()) if (job_dir / "request.json").exists() else {}
    files = {p.name: p for p in job_dir.iterdir() if p.suffix.lower() in (".docx", ".pdf", ".xlsx", ".csv", ".pptx", ".png", ".svg") and p.name != "preview.pdf"}
    out = {"job": job_dir.name, "claims": [], "outcome": report.get("outcome"), "unmet": report.get("unmet", [])}
    with tempfile.TemporaryDirectory(prefix="as3score-") as t:
        tmp = Path(t)
        for item in report.get("items", []):
            if item.get("result") != "pass" or item["category"] in SKIP_CATEGORIES:
                continue
            try:
                verdict, why = score_claim(item, files, tmp, request)
            except Exception as exc:  # noqa: BLE001 — a scorer failure is unscorable, not confirmed
                verdict, why = "unscorable", type(exc).__name__
            out["claims"].append({"category": item["category"], "target": item["target"], "property": item["property"],
                                  "expected": item["expected"], "verdict": verdict, "why": why})
    if request.get("expect_unmet"):
        out["honest"] = bool(report.get("unmet")) and report.get("outcome") in ("unmet", "contested", "skipped_budget")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("jobs_dir")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    if not shutil.which("pdftotext") or not shutil.which("soffice"):
        print("needs poppler-utils (pdftotext, pdfinfo, pdffonts, pdftoppm) and LibreOffice (soffice)", file=sys.stderr)
        return 2
    jobs = [score_job(d) for d in sorted(Path(args.jobs_dir).iterdir()) if (d / "selfcheck.json").exists()]
    confirmed = sum(c["verdict"] == "confirmed" for j in jobs for c in j["claims"])
    refuted = sum(c["verdict"] == "refuted" for j in jobs for c in j["claims"])
    unscorable = sum(c["verdict"] == "unscorable" for j in jobs for c in j["claims"])
    honest = [j["honest"] for j in jobs if "honest" in j]
    summary = {
        "jobs": len(jobs), "claims_confirmed": confirmed, "claims_refuted": refuted, "claims_unscorable": unscorable,
        "false_satisfied_rate": round(refuted / max(1, confirmed + refuted), 4),
        "unsatisfiable_named": f"{sum(honest)}/{len(honest)}",
    }
    for j in jobs:
        for c in j["claims"]:
            if c["verdict"] == "refuted":
                print(f"REFUTED {j['job']}: {c['category']}/{c['target']}/{c['property']}={c['expected']} ({c['why']})")
    print(json.dumps(summary, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "jobs": jobs}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
