"""Report writers: HTML, PDF, CSV, JSON.

All four read from the store rather than from memory, so a report over a 20,000
page comparison costs the same RAM as one over 20 pages.  The only bounded
resource is thumbnails, which are capped and streamed.
"""
from __future__ import annotations

import base64
import csv
import html
import io
import json
import os
from dataclasses import dataclass, field
from datetime import datetime

from .changes import group_changes
from .models import (
    ALL_CATEGORIES, CATEGORY_LABELS, DocumentInfo, FLAG_LABELS, KIND_ADDED,
    KIND_REMOVED, PageStat, Summary,
)
from .render import HIGHLIGHT_COLOURS, PageRenderer, render_pair_image
from .store import FILTER_ALL, FILTER_CHANGED, ResultStore

FMT_HTML = "html"
FMT_PDF = "pdf"
FMT_CSV = "csv"
FMT_JSON = "json"
ALL_FORMATS = (FMT_HTML, FMT_PDF, FMT_CSV, FMT_JSON)


@dataclass
class ReportOptions:
    title: str = "PDF Comparison Report"
    author: str = ""
    formats: list[str] = field(default_factory=lambda: [FMT_HTML])
    include_identical: bool = False
    include_thumbnails: bool = True
    max_thumbnails: int = 250
    thumbnail_width: int = 1100
    thumbnail_scale: float = 1.35     # ~98 dpi before downscale
    include_change_text: bool = True
    max_changes_listed: int = 40


def _fmt_int(n) -> str:
    return f"{n:,}"


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def _iter_report_rows(store: ResultStore, options: ReportOptions):
    flt = FILTER_ALL if options.include_identical else FILTER_CHANGED
    yield from store.iter_stats(flt)


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------
_CSS = """
:root{
  --bg:#0e1116; --panel:#161b22; --panel-2:#1c2230; --line:#252d3a;
  --txt:#e6edf3; --muted:#8b98a9; --accent:#4f8cff;
  --removed:#e83e4a; --added:#22b26e; --changed:#f09e1e;
  --moved:#5680e9; --format:#a75fe0;
  --radius:14px;
  --mono:ui-monospace,"Cascadia Code",Consolas,monospace;
  --sans:"Inter","Segoe UI",system-ui,-apple-system,sans-serif;
}
@media (prefers-color-scheme: light){
  :root{ --bg:#f4f6fa; --panel:#ffffff; --panel-2:#f7f9fc; --line:#e2e7ef;
         --txt:#111827; --muted:#5b6676; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans);
     font-size:14px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding:32px 24px 80px}
header.top{display:flex;justify-content:space-between;align-items:flex-start;
     gap:24px;flex-wrap:wrap;margin-bottom:28px}
h1{font-size:26px;font-weight:650;margin:0 0 6px;letter-spacing:-.02em}
.sub{color:var(--muted);font-size:13px}
.badge{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;
     border-radius:999px;font-size:12px;font-weight:600;letter-spacing:.01em}
.badge.ok{background:rgba(34,178,110,.15);color:#22b26e}
.badge.warn{background:rgba(240,158,30,.15);color:#f09e1e}
.badge.bad{background:rgba(232,62,74,.15);color:#e83e4a}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));
     gap:12px;margin-bottom:24px}
.card{background:var(--panel);border:1px solid var(--line);
     border-radius:var(--radius);padding:16px 18px}
.card .k{font-size:11px;text-transform:uppercase;letter-spacing:.07em;
     color:var(--muted);font-weight:600}
.card .v{font-size:27px;font-weight:640;margin-top:6px;letter-spacing:-.02em;
     font-variant-numeric:tabular-nums}
.card .v.sm{font-size:19px}

.docs{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:24px}
@media(max-width:760px){.docs{grid-template-columns:1fr}}
.doc{background:var(--panel);border:1px solid var(--line);
     border-radius:var(--radius);padding:16px 18px;min-width:0}
.doc .tag{font-size:11px;font-weight:700;letter-spacing:.08em;
     color:var(--accent);text-transform:uppercase}
.doc .name{font-weight:600;margin:6px 0 2px;word-break:break-all}
.doc dl{display:grid;grid-template-columns:auto 1fr;gap:2px 14px;margin:10px 0 0;
     font-size:12.5px}
.doc dt{color:var(--muted)}
.doc dd{margin:0;font-variant-numeric:tabular-nums;word-break:break-all}

.legend{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px}
.chip{display:inline-flex;align-items:center;gap:7px;background:var(--panel);
     border:1px solid var(--line);border-radius:999px;padding:5px 12px;
     font-size:12.5px}
.chip i{width:11px;height:11px;border-radius:3px;display:block}
.chip b{font-variant-numeric:tabular-nums}

.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
     margin-bottom:14px}
.toolbar input,.toolbar select{background:var(--panel);color:var(--txt);
     border:1px solid var(--line);border-radius:9px;padding:8px 11px;
     font-size:13px;font-family:inherit;outline:none}
.toolbar input:focus,.toolbar select:focus{border-color:var(--accent)}
.toolbar input[type=search]{min-width:230px}
.count{color:var(--muted);font-size:12.5px;margin-left:auto}

table{width:100%;border-collapse:separate;border-spacing:0;
     background:var(--panel);border:1px solid var(--line);
     border-radius:var(--radius);overflow:hidden}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid var(--line);
     font-size:13px}
th{background:var(--panel-2);font-size:11px;text-transform:uppercase;
   letter-spacing:.06em;color:var(--muted);font-weight:650;cursor:pointer;
   user-select:none;white-space:nowrap;position:sticky;top:0;z-index:2}
th:hover{color:var(--txt)}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--panel-2)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;
      font-weight:650}
.pill.added{background:rgba(34,178,110,.16);color:#22b26e}
.pill.removed{background:rgba(232,62,74,.16);color:#e83e4a}
.pill.changed{background:rgba(240,158,30,.16);color:#f09e1e}
.pill.identical{background:rgba(139,152,169,.16);color:var(--muted)}
.flags{color:var(--muted);font-size:11.5px}
.bar{height:5px;background:var(--line);border-radius:3px;overflow:hidden;
     min-width:60px}
.bar>span{display:block;height:100%;background:var(--accent)}

.pages{margin-top:36px}
.page{background:var(--panel);border:1px solid var(--line);
      border-radius:var(--radius);margin-bottom:18px;overflow:hidden}
.page>summary{padding:13px 18px;cursor:pointer;display:flex;
      align-items:center;gap:12px;font-weight:600;list-style:none}
.page>summary::-webkit-details-marker{display:none}
.page>summary::before{content:"▸";color:var(--muted);font-size:12px;
      transition:transform .15s}
.page[open]>summary::before{transform:rotate(90deg)}
.page .body{padding:0 18px 18px}
.page img{width:100%;height:auto;border:1px solid var(--line);
      border-radius:9px;display:block;background:#fff}
.changes{margin:0 0 14px;padding:0;list-style:none;display:grid;
      grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:6px}
.changes li{font-size:12.5px;padding:5px 10px;border-radius:7px;
      background:var(--panel-2);border-left:3px solid var(--muted);
      font-family:var(--mono);word-break:break-word}
footer{margin-top:44px;padding-top:18px;border-top:1px solid var(--line);
      color:var(--muted);font-size:12px;display:flex;
      justify-content:space-between;gap:16px;flex-wrap:wrap}
.empty{padding:44px;text-align:center;color:var(--muted)}
"""

_JS = """
(function(){
  var tbody=document.querySelector('#rows');
  if(!tbody) return;
  var rows=Array.prototype.slice.call(tbody.querySelectorAll('tr'));
  var q=document.getElementById('q'), f=document.getElementById('f'),
      c=document.getElementById('count');
  function apply(){
    var term=(q.value||'').toLowerCase(), kind=f.value, n=0;
    rows.forEach(function(r){
      var okKind = kind==='all' || r.dataset.kind===kind ||
                   (kind==='text' && +r.dataset.text>0) ||
                   (kind==='image' && +r.dataset.image>0);
      var okTerm = !term || r.dataset.search.indexOf(term)>-1;
      var show = okKind && okTerm;
      r.style.display = show ? '' : 'none';
      if(show) n++;
    });
    c.textContent = n + ' of ' + rows.length + ' pages';
  }
  q.addEventListener('input', apply);
  f.addEventListener('change', apply);
  apply();

  var dir={};
  document.querySelectorAll('th[data-sort]').forEach(function(th){
    th.addEventListener('click', function(){
      var key=th.dataset.sort, asc=!(dir[key]=!dir[key]);
      rows.sort(function(a,b){
        var av=a.dataset[key], bv=b.dataset[key];
        var an=parseFloat(av), bn=parseFloat(bv);
        if(!isNaN(an)&&!isNaN(bn)) return asc?an-bn:bn-an;
        return asc ? String(av).localeCompare(bv) : String(bv).localeCompare(av);
      });
      rows.forEach(function(r){tbody.appendChild(r);});
    });
  });
})();
"""


def _thumb_data_uri(image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=78, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _status_pill(stat: PageStat) -> str:
    if stat.kind == KIND_ADDED:
        return '<span class="pill added">Page added</span>'
    if stat.kind == KIND_REMOVED:
        return '<span class="pill removed">Page removed</span>'
    if stat.status == "identical":
        return '<span class="pill identical">Identical</span>'
    if stat.status == "error":
        return '<span class="pill removed">Error</span>'
    return '<span class="pill changed">Changed</span>'


def generate_html(path: str, store: ResultStore, info_a: DocumentInfo,
                  info_b: DocumentInfo, summary: Summary,
                  options: ReportOptions, renderer: PageRenderer | None = None,
                  progress_cb=None) -> str:
    stats = list(_iter_report_rows(store, options))
    total_rows = len(stats)

    e = html.escape
    parts: list[str] = []
    add = parts.append

    add("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">")
    add('<meta name="viewport" content="width=device-width,initial-scale=1">')
    add(f"<title>{e(options.title)}</title>")
    add(f"<style>{_CSS}</style></head><body><div class=\"wrap\">")

    # -- header ------------------------------------------------------------
    if summary.changed == 0:
        badge = '<span class="badge ok">No differences found</span>'
    elif summary.changed <= max(3, summary.total_pairs * 0.02):
        badge = f'<span class="badge warn">{_fmt_int(summary.changed)} pages differ</span>'
    else:
        badge = f'<span class="badge bad">{_fmt_int(summary.changed)} pages differ</span>'

    add('<header class="top"><div>')
    add(f"<h1>{e(options.title)}</h1>")
    stamp = datetime.now().strftime("%d %B %Y at %H:%M")
    who = f" · {e(options.author)}" if options.author else ""
    add(f'<div class="sub">Generated {stamp}{who}</div>')
    add(f"</div><div>{badge}</div></header>")

    # -- summary cards -----------------------------------------------------
    add('<div class="cards">')
    for key, value, cls in (
            ("Pages compared", _fmt_int(summary.total_pairs), ""),
            ("Identical", _fmt_int(summary.identical), ""),
            ("Changed", _fmt_int(summary.changed), ""),
            ("Pages added", _fmt_int(summary.added_pages), ""),
            ("Pages removed", _fmt_int(summary.removed_pages), ""),
            ("Total changes", _fmt_int(summary.total_changes), ""),
            ("Match rate", _pct(summary.match_rate), ""),
            ("Run time", f"{summary.duration_s:.1f}s", "sm")):
        add(f'<div class="card"><div class="k">{key}</div>'
            f'<div class="v {cls}">{value}</div></div>')
    add("</div>")

    # -- documents ---------------------------------------------------------
    add('<div class="docs">')
    for tag, info in (("Document A — original", info_a),
                      ("Document B — revised", info_b)):
        add('<div class="doc">')
        add(f'<div class="tag">{e(tag)}</div>')
        add(f'<div class="name">{e(os.path.basename(info.path))}</div>')
        add("<dl>")
        add(f"<dt>Pages</dt><dd>{_fmt_int(info.page_count)}</dd>")
        add(f"<dt>Size</dt><dd>{_human_size(info.file_size)}</dd>")
        if info.title:
            add(f"<dt>Title</dt><dd>{e(info.title)}</dd>")
        if info.author:
            add(f"<dt>Author</dt><dd>{e(info.author)}</dd>")
        add(f"<dt>Path</dt><dd>{e(info.path)}</dd>")
        add("</dl></div>")
    add("</div>")

    # -- legend / category totals -----------------------------------------
    if summary.counts:
        add('<div class="legend">')
        for cat in ALL_CATEGORIES:
            n = summary.counts.get(cat, 0)
            if not n:
                continue
            r, g, b, _a = HIGHLIGHT_COLOURS.get(cat, (139, 152, 169, 255))
            add(f'<span class="chip"><i style="background:rgb({r},{g},{b})">'
                f'</i>{e(CATEGORY_LABELS.get(cat, cat))} <b>{_fmt_int(n)}</b>'
                f"</span>")
        add("</div>")

    # -- table -------------------------------------------------------------
    add('<div class="toolbar">')
    add('<input type="search" id="q" placeholder="Search pages…">')
    add('<select id="f"><option value="all">All rows</option>'
        '<option value="matched">Changed pages</option>'
        '<option value="added">Added pages</option>'
        '<option value="removed">Removed pages</option>'
        '<option value="text">Text changes</option>'
        '<option value="image">Image changes</option></select>')
    add('<span class="count" id="count"></span></div>')

    if not stats:
        add('<div class="empty">No differences to report — '
            "the documents match.</div>")
    else:
        add("<table><thead><tr>")
        for label, key in (("Page", "page"), ("Status", "status"),
                           ("Text", "text"), ("Images", "image"),
                           ("Total", "total"), ("Similarity", "sim"),
                           ("Notes", "flags")):
            add(f'<th data-sort="{key}">{label}</th>')
        add('</tr></thead><tbody id="rows">')
        for st in stats:
            search = f"{st.label()} {st.status} {' '.join(st.flag_labels())}".lower()
            add(f'<tr data-kind="{e(st.kind)}" data-page="{st.idx}" '
                f'data-status="{e(st.status)}" data-text="{st.text_changes}" '
                f'data-image="{st.image_changes}" data-total="{st.total_changes}" '
                f'data-sim="{st.similarity:.4f}" '
                f'data-flags="{e(" ".join(st.flag_labels()))}" '
                f'data-search="{e(search)}">')
            add(f"<td><strong>{e(st.label())}</strong></td>")
            add(f"<td>{_status_pill(st)}</td>")
            add(f'<td class="num">{_fmt_int(st.text_changes) if st.text_changes else "—"}</td>')
            add(f'<td class="num">{_fmt_int(st.image_changes) if st.image_changes else "—"}</td>')
            add(f'<td class="num">{_fmt_int(st.total_changes) if st.total_changes else "—"}</td>')
            add(f'<td><div class="bar"><span style="width:'
                f'{st.similarity * 100:.0f}%"></span></div></td>')
            add(f'<td class="flags">{e(", ".join(st.flag_labels())) or "—"}</td>')
            add("</tr>")
        add("</tbody></table>")

    # -- per page detail ---------------------------------------------------
    if options.include_thumbnails and renderer is not None and stats:
        add('<div class="pages"><h2 style="font-size:18px;margin:0 0 14px">'
            "Page detail</h2>")
        shown = 0
        for st in stats:
            if shown >= options.max_thumbnails:
                remaining = total_rows - shown
                if remaining > 0:
                    add(f'<div class="empty">{_fmt_int(remaining)} further '
                        "changed pages are listed in the table above but not "
                        "rendered — raise the thumbnail limit to include them."
                        "</div>")
                break
            if st.status == "identical":
                continue
            boxes = store.get_boxes(st.idx)
            try:
                image = render_pair_image(renderer, st.a_pno, st.b_pno, boxes,
                                          scale=options.thumbnail_scale,
                                          max_width=options.thumbnail_width)
            except Exception:
                image = None
            if image is None:
                continue

            add('<details class="page"' + (" open" if shown < 3 else "") + ">")
            summary_bits = [e(st.label()), _status_pill(st)]
            if st.total_changes:
                summary_bits.append(
                    f'<span class="flags">{_fmt_int(st.total_changes)} changes</span>')
            add(f"<summary>{' '.join(summary_bits)}</summary>")
            add('<div class="body">')

            if options.include_change_text and boxes:
                described = group_changes(boxes)
                if described:
                    add('<ul class="changes">')
                    for change in described[:options.max_changes_listed]:
                        r, g, b_, _a = HIGHLIGHT_COLOURS.get(
                            change.category, (139, 152, 169, 255))
                        add(f'<li style="border-left-color:rgb({r},{g},{b_})">'
                            f"{e(CATEGORY_LABELS.get(change.category, change.category))}"
                            f": {e(change.text[:180])}</li>")
                    if len(described) > options.max_changes_listed:
                        add(f"<li>… {_fmt_int(len(described) - options.max_changes_listed)}"
                            " more</li>")
                    add("</ul>")

            add(f'<img alt="{e(st.label())}" src="{_thumb_data_uri(image)}">')
            add("</div></details>")
            shown += 1
            if progress_cb and shown % 5 == 0:
                progress_cb(shown, min(total_rows, options.max_thumbnails))
        add("</div>")

    add("<footer>")
    add(f"<span>{e(os.path.basename(info_a.path))} ↔ "
        f"{e(os.path.basename(info_b.path))}</span>")
    add("<span>PDF Comparison Studio</span></footer>")
    add(f"</div><script>{_JS}</script></body></html>")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(parts))
    return path


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------
def generate_pdf(path: str, store: ResultStore, info_a: DocumentInfo,
                 info_b: DocumentInfo, summary: Summary,
                 options: ReportOptions, renderer: PageRenderer,
                 progress_cb=None) -> str:
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz

    doc = fitz.open()
    page_w, page_h = 842.0, 595.0          # A4 landscape suits side-by-side
    margin = 36.0

    # -- cover -------------------------------------------------------------
    cover = doc.new_page(width=page_w, height=page_h)
    y = margin + 10
    cover.insert_text((margin, y), options.title, fontsize=24, fontname="hebo")
    y += 26
    cover.insert_text((margin, y),
                      datetime.now().strftime("Generated %d %B %Y at %H:%M"),
                      fontsize=10, color=(0.42, 0.46, 0.52))
    if options.author:
        y += 14
        cover.insert_text((margin, y), options.author, fontsize=10,
                          color=(0.42, 0.46, 0.52))
    y += 30

    cover.draw_line(fitz.Point(margin, y), fitz.Point(page_w - margin, y),
                    color=(0.85, 0.87, 0.9), width=0.8)
    y += 26

    left_x, right_x = margin, page_w / 2 + 10
    for x, tag, info in ((left_x, "DOCUMENT A — ORIGINAL", info_a),
                         (right_x, "DOCUMENT B — REVISED", info_b)):
        cover.insert_text((x, y), tag, fontsize=8, fontname="hebo",
                          color=(0.31, 0.55, 1.0))
        cover.insert_text((x, y + 16), os.path.basename(info.path)[:58],
                          fontsize=11, fontname="hebo")
        cover.insert_text((x, y + 32),
                          f"{info.page_count:,} pages · {_human_size(info.file_size)}",
                          fontsize=9, color=(0.42, 0.46, 0.52))
    y += 62

    stat_rows = [
        ("Pages compared", f"{summary.total_pairs:,}"),
        ("Identical", f"{summary.identical:,}"),
        ("Changed", f"{summary.changed:,}"),
        ("Pages added", f"{summary.added_pages:,}"),
        ("Pages removed", f"{summary.removed_pages:,}"),
        ("Total changes", f"{summary.total_changes:,}"),
        ("Match rate", _pct(summary.match_rate)),
        ("Run time", f"{summary.duration_s:.1f}s"),
    ]
    col_w = (page_w - 2 * margin) / 4
    for i, (k, v) in enumerate(stat_rows):
        cx = margin + (i % 4) * col_w
        cy = y + (i // 4) * 56
        cover.draw_rect(fitz.Rect(cx, cy, cx + col_w - 10, cy + 46),
                        color=(0.89, 0.91, 0.94), fill=(0.975, 0.98, 0.99),
                        width=0.7, radius=0.12)
        cover.insert_text((cx + 10, cy + 17), k.upper(), fontsize=7,
                          fontname="hebo", color=(0.45, 0.5, 0.57))
        cover.insert_text((cx + 10, cy + 37), v, fontsize=15, fontname="hebo")
    y += 2 * 56 + 14

    if summary.counts:
        cover.insert_text((margin, y), "CHANGES BY TYPE", fontsize=8,
                          fontname="hebo", color=(0.45, 0.5, 0.57))
        y += 16
        cx = margin
        for cat in ALL_CATEGORIES:
            n = summary.counts.get(cat, 0)
            if not n:
                continue
            r, g, b, _a = HIGHLIGHT_COLOURS.get(cat, (139, 152, 169, 255))
            cover.draw_rect(fitz.Rect(cx, y - 8, cx + 9, y + 1),
                            fill=(r / 255, g / 255, b / 255), width=0)
            text = f"{CATEGORY_LABELS.get(cat, cat)}  {n:,}"
            cover.insert_text((cx + 14, y), text, fontsize=9)
            cx += 14 + len(text) * 4.9 + 22
            if cx > page_w - margin - 140:
                cx = margin
                y += 16

    # -- change pages ------------------------------------------------------
    stats = [s for s in _iter_report_rows(store, options)
             if s.status != "identical"]
    limit = min(len(stats), options.max_thumbnails)
    for n, st in enumerate(stats[:limit]):
        boxes = store.get_boxes(st.idx)
        try:
            image = render_pair_image(renderer, st.a_pno, st.b_pno, boxes,
                                      scale=options.thumbnail_scale,
                                      max_width=2200)
        except Exception:
            continue
        if image is None:
            continue

        page = doc.new_page(width=page_w, height=page_h)
        header = f"{st.label()}   ·   {st.total_changes:,} changes"
        if st.flag_labels():
            header += "   ·   " + ", ".join(st.flag_labels())
        page.insert_text((margin, margin - 8), header, fontsize=10,
                         fontname="hebo")

        top = margin + 6
        avail = fitz.Rect(margin, top, page_w - margin, page_h - margin + 20)
        scale = min(avail.width / image.width, avail.height / image.height)
        w, h = image.width * scale, image.height * scale
        target = fitz.Rect(avail.x0, avail.y0, avail.x0 + w, avail.y0 + h)

        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=82, optimize=True)
        page.insert_image(target, stream=buf.getvalue())

        if progress_cb and n % 5 == 0:
            progress_cb(n, limit)

    if len(stats) > limit:
        note = doc.new_page(width=page_w, height=page_h)
        note.insert_text((margin, margin + 20),
                         f"{len(stats) - limit:,} further changed pages were "
                         f"omitted from this PDF.", fontsize=12)
        note.insert_text((margin, margin + 40),
                         "Raise the page limit in report options, or use the "
                         "HTML/CSV export for the complete list.",
                         fontsize=10, color=(0.42, 0.46, 0.52))

    doc.set_metadata({"title": options.title, "author": options.author,
                      "producer": "PDF Comparison Studio",
                      "subject": f"{os.path.basename(info_a.path)} vs "
                                 f"{os.path.basename(info_b.path)}"})
    doc.save(path, deflate=True, garbage=3)
    doc.close()
    return path


# --------------------------------------------------------------------------
# CSV / JSON
# --------------------------------------------------------------------------
def generate_csv(path: str, store: ResultStore, options: ReportOptions) -> str:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["row", "doc_a_page", "doc_b_page", "kind", "status",
                    "similarity", "text_changes", "image_changes",
                    "total_changes",
                    *[CATEGORY_LABELS[c] for c in ALL_CATEGORIES], "notes"])
        for st in _iter_report_rows(store, options):
            w.writerow([
                st.idx,
                "" if st.a_pno is None else st.a_pno + 1,
                "" if st.b_pno is None else st.b_pno + 1,
                st.kind, st.status, f"{st.similarity:.4f}",
                st.text_changes, st.image_changes, st.total_changes,
                *[st.counts.get(c, 0) for c in ALL_CATEGORIES],
                "; ".join(st.flag_labels()),
            ])
    return path


def generate_json(path: str, store: ResultStore, info_a: DocumentInfo,
                  info_b: DocumentInfo, summary: Summary,
                  options: ReportOptions, settings_dict: dict | None = None) -> str:
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tool": "PDF Comparison Studio",
        "title": options.title,
        "documents": {"a": info_a.to_dict(), "b": info_b.to_dict()},
        "settings": settings_dict or {},
        "summary": summary.to_dict(),
        "pages": [],
    }
    for st in _iter_report_rows(store, options):
        payload["pages"].append({
            "row": st.idx,
            "a_page": None if st.a_pno is None else st.a_pno + 1,
            "b_page": None if st.b_pno is None else st.b_pno + 1,
            "kind": st.kind,
            "status": st.status,
            "similarity": round(st.similarity, 4),
            "counts": st.counts,
            "flags": st.flags,
        })
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    return path


# --------------------------------------------------------------------------
def generate(output_dir: str, basename: str, store: ResultStore,
             info_a: DocumentInfo, info_b: DocumentInfo, summary: Summary,
             options: ReportOptions, renderer: PageRenderer | None = None,
             settings_dict: dict | None = None, progress_cb=None) -> list[str]:
    """Write every requested format; returns the paths written."""
    os.makedirs(output_dir, exist_ok=True)
    written: list[str] = []
    for fmt in options.formats:
        target = os.path.join(output_dir, f"{basename}.{fmt}")
        if fmt == FMT_HTML:
            written.append(generate_html(target, store, info_a, info_b, summary,
                                         options, renderer, progress_cb))
        elif fmt == FMT_PDF:
            if renderer is None:
                continue
            written.append(generate_pdf(target, store, info_a, info_b, summary,
                                        options, renderer, progress_cb))
        elif fmt == FMT_CSV:
            written.append(generate_csv(target, store, options))
        elif fmt == FMT_JSON:
            written.append(generate_json(target, store, info_a, info_b,
                                         summary, options, settings_dict))
    return written
