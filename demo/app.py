import glob, os, re
import html as _h
from flask import Flask, jsonify, request
import requests

app = Flask(__name__)
BASE = "http://localhost:8888"
IIIF_IMG = "http://localhost:8182/iiif/2"
SOLR = "http://localhost:8983/solr/colonist/select"

def solr_q(q):
    """Plain words -> exact phrase (historic behavior). Queries containing
    Lucene syntax pass through raw, scoped to ocr_text."""
    import re as _re
    import calendar as _cal
    def _daterange(m):
        a, b = m.group(1), m.group(2)
        def lo(d):
            if len(d) == 7: d += "-01"
            return d + "T00:00:00Z"
        def hi(d):
            if len(d) == 7:
                y, mo = int(d[:4]), int(d[5:7])
                d += f"-{_cal.monthrange(y, mo)[1]:02d}"
            return d + "T23:59:59Z"
        return f"issue_date:[{lo(a)} TO {hi(b)}]"
    q = _re.sub(r'issue_date:\[(\d{4}-\d{2}(?:-\d{2})?) TO (\d{4}-\d{2}(?:-\d{2})?)\]',
                _daterange, q)
    q = _re.sub(r'issue_date:(\d{4}-\d{2}-\d{2})(?![\dT-])',
                lambda m: f"issue_date:[{m.group(1)}T00:00:00Z TO {m.group(1)}T23:59:59Z]", q)
    if _re.search(r'["*?~()\[:]|\b(AND|OR|NOT)\b', q):
        return f'ocr_text:({q})'
    return f'ocr_text:"{q}"'

ALTO_ROOT = os.path.expanduser("~/tess5-1925-full")

ISSUES = sorted(os.path.basename(d) for d in glob.glob(f"{ALTO_ROOT}/dailycolonist*"))

import json as _json
try:
    ISSUE_DATES = _json.load(open(os.path.expanduser("~/solr-bridge/.issue_dates.json")))
except Exception:
    ISSUE_DATES = {}
MONTH_NAMES = ["January","February","March","April","May","June",
               "July","August","September","October","November","December"]
def _ai_done(issue):
    return os.path.exists(os.path.expanduser(f"~/paddle-year/{issue}/.done"))

def _clamp(txt, n=170):
    import html as _html
    txt = (txt or "").strip()
    if len(txt) > n:
        txt = txt[:n].rsplit(" ", 1)[0]
        txt = re.sub(r"<[^>]*$", "", txt)
    # escape any markup the OCR text itself contains (VLM ad transcriptions
    # can include raw table/HTML fragments that break the page layout),
    # then restore only Solr's <em> highlight tags
    txt = _html.escape(txt, quote=False)
    txt = txt.replace("&lt;em&gt;", "<em>").replace("&lt;/em&gt;", "</em>")
    if txt.count("<em>") > txt.count("</em>"):
        txt += "</em>"
    return txt

_dims_cache = {}

def issue_pages(issue):
    xs = sorted(glob.glob(f"{ALTO_ROOT}/{issue}/*.xml"))
    return [os.path.basename(x)[:-4] for x in xs]

def page_dims(issue, page):
    if page not in _dims_cache:
        path = f"{ALTO_ROOT}/{issue}/{page}.xml"
        head = open(path, encoding="utf-8", errors="ignore").read(3000)
        m = re.search(r'<Page[^>]*WIDTH="(\d+)"[^>]*HEIGHT="(\d+)"', head)
        _dims_cache[page] = (int(m.group(1)), int(m.group(2))) if m else (7376, 9559)
    return _dims_cache[page]

def canvas_id(page): return f"{BASE}/canvas/{page}"

@app.route("/manifest/<arm>/<issue>")
def manifest(arm, issue):
    canvases = []
    IMG_ROOT = os.path.expanduser("~/colonist-images")
    for p in issue_pages(issue):
        w, h = page_dims(issue, p)
        png_exists = os.path.exists(f"{IMG_ROOT}/{issue}/{p}.png")
        label = p.split("_p")[-1] + ("" if png_exists else " (image pending)")
        canvases.append({"@id": canvas_id(p), "@type": "sc:Canvas",
            "label": label, "width": w, "height": h,
            "images": [{"@type": "oa:Annotation", "motivation": "sc:painting",
                "on": canvas_id(p),
                "resource": {"@id": f"{IIIF_IMG}/{issue}%2F{p}.png/full/full/0/default.jpg",
                    "@type": "dctypes:Image", "width": w, "height": h,
                    "service": {"@context": "http://iiif.io/api/image/2/context.json",
                        "@id": f"{IIIF_IMG}/{issue}%2F{p}.png",
                        "profile": "http://iiif.io/api/image/2/level2.json"}}}] if png_exists else []})
    return jsonify({"@context": "http://iiif.io/api/presentation/2/context.json",
        "@id": f"{BASE}/manifest/{arm}/{issue}", "@type": "sc:Manifest",
        "label": f"{issue} [{arm}]",
        "service": {"@context": "http://iiif.io/api/search/1/context.json",
                    "@id": f"{BASE}/search/{arm}/{issue}",
                    "profile": "http://iiif.io/api/search/1/search"},
        "sequences": [{"@type": "sc:Sequence", "canvases": canvases}]})

@app.route("/search/<arm>/<issue>")
def search(arm, issue):
    q = request.args.get("q", "").strip()
    src_f = "paddleocr-vl" if arm == "vlm" else "tesseract"
    r = requests.get(SOLR, params={
        "q": solr_q(q) + f' AND source:{src_f} AND page_id:{issue}_p*',
        "hl": "on", "hl.ocr.fl": "ocr_text", "hl.snippets": "20", "rows": "30",
        "fl": "id,page_id", "hl.ocr.absoluteHighlights": "true"}).json()
    page_of = {d["id"]: d["page_id"] for d in r["response"]["docs"]}
    resources, hits = [], []
    for doc_id, hl in r.get("ocrHighlighting", {}).items():
        page = page_of.get(doc_id, "")
        for snip in hl.get("ocr_text", {}).get("snippets", []):
            raw = snip.get("text", "")
            ids_this = []
            for region in snip.get("highlights", [[]]):
                for h in region:
                    x, y = h["ulx"], h["uly"]
                    w, hgt = h["lrx"] - x, h["lry"] - y
                    aid = f"{BASE}/anno/{doc_id}/{len(resources)}"
                    resources.append({"@id": aid, "@type": "oa:Annotation",
                        "motivation": "sc:painting",
                        "resource": {"@type": "cnt:ContentAsText",
                                     "chars": h.get("text", q)},
                        "on": f"{canvas_id(page)}#xywh={x},{y},{w},{hgt}"})
                    ids_this.append(aid)
            if ids_this:
                m = re.search("<em>(.*?)</em>", raw)
                hits.append({"@type": "search:Hit", "annotations": ids_this,
                    "match": m.group(1) if m else q,
                    "before": re.sub("</?em>", "", raw.split("<em>", 1)[0])[-120:],
                    "after": re.sub("</?em>", "", raw.rsplit("</em>", 1)[-1])[:120]})
    return jsonify({"@context": "http://iiif.io/api/search/1/context.json",
        "@id": request.url, "@type": "sc:AnnotationList",
        "resources": resources, "hits": hits})

@app.route("/findall")
def findall():
    q = request.args.get("q", "").strip()
    ARMS = [("tesseract", "Traditional OCR (Tesseract)"),
            ("paddleocr-vl", "AI Vision Pipeline (VLM)")]
    per_issue = {}
    true_pages = {}
    for src_f, label in ARMS:
        r = requests.get(SOLR, params={
            "q": solr_q(q) + f' AND source:{src_f}', "rows": 200,
            "fl": "id,page_id", "hl": "on", "hl.ocr.fl": "ocr_text",
            "hl.snippets": "1"}).json()
        if "response" not in r:
            msg = r.get("error", {}).get("msg", "query could not be parsed")
            return (f'<html><head><style>{CSS}</style></head><body><div class="wrap">'
                    f'<h1><a href="/" class="home">Daily Colonist 1925</a></h1>'
                    f'<p class="meta">Search syntax error for <b>{q}</b>: {msg}</p>'
                    f'<p class="meta">Check quotes are balanced and operators (AND, OR, NOT) '
                    f'are uppercase with terms on both sides. <a href="/">\u2190 back</a></p>'
                    f'</div></body></html>')
        true_pages[src_f] = r["response"]["numFound"]
        page_of = {d["id"]: d["page_id"] for d in r["response"]["docs"]}
        for doc_id, hl in r.get("ocrHighlighting", {}).items():
            pid = page_of.get(doc_id, "")
            issue = pid.rsplit("_p", 1)[0]
            snips = hl.get("ocr_text", {}).get("snippets", [])
            txt = snips[0].get("text", "") if snips else ""
            rec = per_issue.setdefault(issue, {})
            arm = rec.setdefault(src_f, {"pages": set(), "snippet": ""})
            arm["pages"].add(pid.split("_p")[-1])
            if txt and (not arm["snippet"] or len(txt) > len(arm["snippet"])):
                arm["snippet"] = txt
    tpages = true_pages.get("tesseract", 0)
    vpages = true_pages.get("paddleocr-vl", 0)
    sort = request.args.get("sort", "relevance")
    ranked = sorted(per_issue.items(),
        key=lambda kv: -sum(len(a["pages"]) for a in kv[1].values()))
    if sort == "date":
        ranked.sort(key=lambda kv: ISSUE_DATES.get(kv[0], {}).get("date") or "9999-99")
    try:
        pg = max(1, int(request.args.get("p", "1")))
    except ValueError:
        pg = 1
    total = len(ranked)
    npages = max(1, (total + 24) // 25)
    pg = min(pg, npages)
    start = (pg - 1) * 25
    ranked = ranked[start:start + 25]
    from urllib.parse import quote_plus as _qp
    _base = f"/findall?q={_qp(q)}"
    sortbar = ('<p class="meta">Sort: '
        + ("<b>relevance</b>" if sort != "date" else f'<a href="{_base}&sort=relevance">relevance</a>')
        + " \u00b7 "
        + ("<b>date</b>" if sort == "date" else f'<a href="{_base}&sort=date">date</a>') + "</p>")
    if npages > 1:
        segs = []
        if pg > 1:
            segs.append(f'<a href="{_base}&sort={sort}&p={pg - 1}">\u2039 Prev</a>')
        for k in range(1, npages + 1):
            a, b = (k - 1) * 25 + 1, min(k * 25, total)
            lab = f"{a}\u2013{b}"
            segs.append(f"<b>{lab}</b>" if k == pg else
                        f'<a href="{_base}&sort={sort}&p={k}">{lab}</a>')
        if pg < npages:
            segs.append(f'<a href="{_base}&sort={sort}&p={pg + 1}">Next \u203a</a>')
        pagebar = '<p class="meta pgbar">' + " \u00b7 ".join(segs) + "</p>"
    else:
        pagebar = ""
    cards = ""
    for _ci, (issue, arms) in enumerate(ranked):
        e = ISSUE_DATES.get(issue, {})
        d = e.get("date")
        head = (f"{e.get('weekday') or ''}, {MONTH_NAMES[int(d[5:7]) - 1]} {int(d[8:10])}, {d[:4]}"
                if d else issue)
        pt = arms.get("tesseract", {}).get("pages", set())
        pv = arms.get("paddleocr-vl", {}).get("pages", set())
        ai = _ai_done(issue)
        if ai:
            chips = (f'<span class="chip both">both {len(pt & pv)}</span>'
                     f'<span class="chip tessc">Tesseract only {len(pt - pv)}</span>'
                     f'<span class="chip vlmc">AI only {len(pv - pt)}</span>')
            btns = (f' <a class="viewbtn sbs" href="/view/{issue}?q={q}">Compare panels</a>'
                    f' <a class="viewbtn ovl" href="/diff/{issue}?q={q}">Compare on page</a>')
        else:
            chips = (f'<span class="chip tessc">Tesseract {len(pt)}</span>'
                     f'<span class="chip pend" title="AI pipeline has not processed this issue yet">AI pending</span>')
            btns = f' <a class="viewbtn sbs" href="/view/{issue}?q={q}">View issue</a>'
        rows = ""
        for src_f, label, css in [("tesseract", "Traditional OCR (Tesseract)", "tessl"),
                                  ("paddleocr-vl", "AI Vision Pipeline (VLM)", "vlml")]:
            a = arms.get(src_f)
            if a:
                extra = len(a["pages"]) - 1
                more = f' <span class="more">+{extra} more page{"s" if extra != 1 else ""}</span>' if extra > 0 else ""
                rows += (f'<div class="armrow"><span class="armname {css}">{label}</span>'
                         f'<span class="snip">\u2026{_clamp(a["snippet"])}\u2026{more}</span></div>')
            elif src_f == "paddleocr-vl" and not ai:
                rows += (f'<div class="armrow missing"><span class="armname {css}">{label}</span>'
                         f'<span class="snip">not yet processed \u2014 run in progress</span></div>')
            else:
                rows += (f'<div class="armrow missing"><span class="armname {css}">{label}</span>'
                         f'<span class="snip">\u2014 no matches \u2014</span></div>')
        cards += (f'<div class="card" id="r{_ci + 1}"><div class="rhead"><a class="rdate" href="/view/{issue}?q={q}">{head}</a>'
                  f' <span class="ids">{issue}</span> <span class="chipbar">{chips}</span>{btns}</div>{rows}</div>')
    return f"""<html><head><title>Results: {q}</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1><a href="/" class="home">Daily Colonist 1925</a> — results for “{q}”</h1>
<p class="meta">{total} issues matched \u00b7 pages with matches: Tesseract {tpages:,} \u00b7 AI pipeline {vpages:,} — showing issues {start + 1}\u2013{start + len(ranked)}. <em>Matched terms are highlighted; click an issue to open the side-by-side viewer with this search pre-loaded.</em></p>
{sortbar}
{pagebar}
{cards}
{pagebar}<p><a href="/">← back to all issues</a></p></div></body></html>"""

@app.route("/issue_meta/<issue>")
def issue_meta(issue):
    q = request.args.get("q", "").strip()
    e = ISSUE_DATES.get(issue, {})
    d = e.get("date")
    label = (f"{e.get('weekday') or ''}, {MONTH_NAMES[int(d[5:7]) - 1]} {int(d[8:10])}, {d[:4]}"
             if d else issue)
    order = sorted(ISSUES, key=lambda i: (ISSUE_DATES.get(i, {}).get("date") or "9999", i))
    try:
        idx = order.index(issue)
    except ValueError:
        idx = -1
    def brief(i):
        de = ISSUE_DATES.get(i, {}).get("date")
        return {"id": i,
                "label": f"{MONTH_NAMES[int(de[5:7]) - 1]} {int(de[8:10])}" if de else i}
    prev = brief(order[idx - 1]) if idx > 0 else None
    nxt = brief(order[idx + 1]) if 0 <= idx < len(order) - 1 else None
    counts = None
    if q:
        counts = {"ai_done": _ai_done(issue)}
        for key, src_f in (("tesseract", "tesseract"), ("vlm", "paddleocr-vl")):
            try:
                r = requests.get(SOLR, params={
                    "q": solr_q(q) + f' AND source:{src_f} AND page_id:{issue}_p*',
                    "rows": 200, "fl": "page_id"}).json()
                docs = r["response"]["docs"]
                if not docs and r["response"]["numFound"] == 0:
                    r = requests.get(SOLR, params={
                        "q": solr_q(q) + f' AND source:{src_f}',
                        "rows": 500, "fl": "page_id"}).json()
                    docs = r["response"]["docs"]
                counts[key] = len({doc.get("page_id", "") for doc in docs
                                   if doc.get("page_id", "").startswith(issue + "_p")})
            except Exception:
                counts[key] = None
    return jsonify({"label": label, "prev": prev, "next": nxt, "counts": counts})

@app.route("/view/<issue>")
def view(issue):
    return """<!DOCTYPE html><html><head>
<script src="https://unpkg.com/mirador@3/dist/mirador.min.js"></script>
<style>body{margin:0}#m{position:absolute;top:46px;bottom:0;left:0;right:0}
#vhead{position:fixed;top:0;left:0;right:0;height:46px;z-index:9999;background:#fff;border-bottom:2px solid #35619e;display:flex;align-items:center;gap:10px;padding:0 12px;box-sizing:border-box;font-family:-apple-system,Helvetica,sans-serif;font-size:13px}
#vhead a{color:#3b3bb3;text-decoration:none}#vhead a:hover{text-decoration:underline}
#vhead .vt{font-family:Georgia,serif;font-size:15px;color:#191919}
#vhead .ids{font-size:11px;color:#a09d95}
#vhead .navb{border:1px solid #b9b6ae;background:#fff;border-radius:4px;padding:3px 9px;color:#333}
#vhead .sep{color:#a09d95}
#vhead .hc{font-size:11px;font-weight:600;border-radius:999px;padding:2px 8px;white-space:nowrap}
#vhead .hc.t{background:#e9eff8;color:#35619e;border:1px solid #35619e}
#vhead .hc.v{background:#f7efdc;color:#a06f14;border:1px solid #a06f14}
#vhead .hc.p{background:#f0efec;color:#9a9a9a;border:1px dashed #9a9a9a}
#vq{font-family:Georgia,serif;font-size:13px;padding:4px 8px;border:1px solid #b9b6ae;border-radius:4px;width:150px}
#m div[style*="min-width: 235px"]{min-width:210px!important;max-width:210px!important;width:210px!important}
.mirador-companion-window-left p,.mirador-companion-window-left li,.mirador-companion-window-left span{font-size:.85rem!important}
.mirador-companion-window-left input{font-size:.85rem!important}
</style>
</head><body>
<div id="vhead">
 <a href="/">Home</a><span class="sep">\u00b7</span>
 <span id="vtitle" class="vt">\u2026</span> <span id="vid" class="ids"></span>
 <a id="vprev" class="navb" style="visibility:hidden" href="#">\u2039 Prev</a>
 <a id="vnext" class="navb" style="visibility:hidden" href="#">Next \u203a</a>
 <form id="vform" style="margin-left:auto;display:flex;gap:6px;align-items:center">
  <input id="vq" type="text" placeholder="Search both arms\u2026">
  <button class="navb" type="submit" style="cursor:pointer">Search</button>
 </form>
 <span id="vcounts"></span>
 <span id="textlinks"></span>
</div>
<div id="m"></div><script>
(function(){
  var issue = location.pathname.split('/').pop();
  var q = new URLSearchParams(location.search).get('q') || '';
  document.getElementById('vq').value = q;
  document.getElementById('vform').addEventListener('submit', function(ev){
    ev.preventDefault();
    var nq = document.getElementById('vq').value.trim();
    location.href = '/view/' + issue + (nq ? '?q=' + encodeURIComponent(nq) : '');
  });
  fetch('/issue_meta/' + issue + (q ? '?q=' + encodeURIComponent(q) : ''))
    .then(function(r){ return r.json(); })
    .then(function(d){
      document.getElementById('vtitle').textContent = d.label;
      document.getElementById('vid').textContent = issue;
      document.title = d.label + ' \u2014 comparison';
      var qs = q ? '?q=' + encodeURIComponent(q) : '';
      if (d.prev) { var p = document.getElementById('vprev');
        p.href = '/view/' + d.prev.id + qs; p.title = d.prev.label; p.style.visibility = 'visible'; }
      if (d.next) { var n = document.getElementById('vnext');
        n.href = '/view/' + d.next.id + qs; n.title = d.next.label; n.style.visibility = 'visible'; }
      if (d.counts) {
        var h = '<span class="hc t" title="pages with hits">Tesseract ' + d.counts.tesseract + ' pp</span> ';
        h += d.counts.ai_done
          ? '<span class="hc v" title="pages with hits">AI ' + d.counts.vlm + ' pp</span>'
          : '<span class="hc p" title="AI pipeline has not processed this issue yet">AI pending</span>';
        document.getElementById('vcounts').innerHTML = h;
      }
    });
})();
</script><script>
var m = Mirador.viewer({id:'m',
 workspaceControlPanel:{enabled:false},
 workspace:{showZoomControls:true},
 window:{switchCanvasOnSearch:true, sideBarOpen:true, defaultSideBarPanel:'search',
   allowClose:false, allowMaximize:false, allowFullscreen:false,
   panels:{info:false, attribution:false, canvas:false, annotations:false, layers:false, search:true}},
 windows:[
  {manifestId:'BASE/manifest/tesseract/ISSUE', title:'Tesseract'},
  {manifestId:'BASE/manifest/vlm/ISSUE', title:'VLM'}
]});
var q = new URLSearchParams(window.location.search).get('q');
if (q) {
  var filled = 0;
  var timer = setInterval(function() {
    var inputs = document.querySelectorAll('input[id^="search-cw-"]');
    inputs.forEach(function(inp) {
      if (inp.dataset.autofired) return;
      inp.dataset.autofired = "1";
      var setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value').set;
      setter.call(inp, q);
      inp.dispatchEvent(new Event('input', {bubbles: true}));
      setTimeout(function() {
        ['keydown','keypress','keyup'].forEach(function(t) {
          inp.dispatchEvent(new KeyboardEvent(t, {key:'Enter', code:'Enter',
            keyCode:13, which:13, bubbles:true, cancelable:true}));
        });
        var form = inp.closest('form');
        if (form) form.dispatchEvent(new Event('submit',
          {bubbles:true, cancelable:true}));
      }, 150);
      filled++;
    });
    if (filled >= 2) clearInterval(timer);
  }, 600);
  setTimeout(function() { clearInterval(timer); }, 15000);
}
function updateTextLinks() {
  var st = m.store.getState();
  var pages = [];
  Object.keys(st.windows || {}).forEach(function(wid) {
    var w = st.windows[wid];
    var canvasId = (w.canvasId || (w.visibleCanvases || [])[0] || '');
    var page = canvasId.split('/').pop();
    if (page && pages.indexOf(page) === -1) pages.push(page);
  });
  var links = pages.map(function(page) {
    return '<a target="_blank" style="background:#fff;border:1px solid #ccc;' +
      'border-radius:2px;padding:4px 10px;color:#333;text-decoration:none;margin-right:6px;' +
      'box-shadow:0 1px 3px rgba(0,0,0,.15)" href="/text/' + page + '">' +
      'Page text: p' + page.split('_p').pop() + '</a>';
  });
  var el = document.getElementById('textlinks');
  if (el) el.innerHTML = links.join('');
}
m.store.subscribe(updateTextLinks);
setTimeout(updateTextLinks, 1500);
var lastSel = {};
m.store.subscribe(function() {
  var state = m.store.getState();
  Object.keys(state.windows || {}).forEach(function(wid) {
    var w = state.windows[wid];
    var sel = w.selectedAnnotationId ||
              ((w.selectedContentSearchAnnotationIds || [])[0]);
    if (!sel || sel === lastSel[wid]) return;
    lastSel[wid] = sel;
    var xywh = null;
    function scan(obj) {
      if (!obj || typeof obj !== 'object') return;
      if (obj.resources && Array.isArray(obj.resources)) {
        obj.resources.forEach(function(r) {
          if (r['@id'] === sel && r.on) {
            var frag = r.on.split('xywh=')[1];
            if (frag) xywh = frag.split(',').map(Number);
          }
        });
      } else { Object.values(obj).forEach(scan); }
    }
    scan(state.searches && state.searches[wid]);
    if (!xywh) return;
    var cx = xywh[0] + xywh[2]/2, cy = xywh[1] + xywh[3]/2;
    var targetW = Math.max(xywh[2] * 3, 800);
    setTimeout(function() {
      m.store.dispatch(Mirador.actions.updateViewport(wid,
        { x: cx, y: cy, zoom: 1 / targetW }));
    }, 300);
  });
});
</script></body></html>""".replace("BASE", BASE).replace("ISSUE", issue)

CSS = """
.rhead{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px;margin-bottom:4px}
a.rdate{font-size:1.1rem;text-decoration:none}
a.rdate:hover{text-decoration:underline}
.chipbar{display:inline-flex;gap:6px;flex-wrap:wrap}
.chip.both{background:#e6f2ea;color:#2f7d4e;border:1px solid #2f7d4e}
.chip.tessc{background:#e9eff8;color:#35619e;border:1px solid #35619e}
.chip.vlmc{background:#f7efdc;color:#a06f14;border:1px solid #a06f14}
.armname.tessl{color:#35619e}
.armname.vlml{color:#a06f14}
.armrow.missing .snip{color:#9a9a9a;font-style:italic}

.month{display:flex;justify-content:space-between;align-items:baseline;font-family:sans-serif;
 font-size:.75rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#6f6f6f;
 border-bottom:1px solid #e2e0da;padding-bottom:4px;margin:26px 0 8px}
.month .mcount{font-weight:500;letter-spacing:0;text-transform:none}
table.issuetbl{width:100%;border-collapse:collapse}
table.issuetbl td{padding:5px 8px 5px 0;vertical-align:baseline}
table.issuetbl tr:hover{background:#f6f5f0}
td.d a{text-decoration:none} td.d a:hover{text-decoration:underline}
.ids{font-family:sans-serif;font-size:.7rem;color:#a09d95;margin-left:6px}
td.pp{color:#6f6f6f;font-size:.88rem;white-space:nowrap}
td.cov{text-align:right;white-space:nowrap}
.chip{font-family:sans-serif;font-size:.72rem;font-weight:600;border-radius:999px;padding:2px 9px;white-space:nowrap}
.chip.ok{background:#e6f2ea;color:#2f7d4e;border:1px solid #2f7d4e}
.chip.pend{background:#f0efec;color:#9a9a9a;border:1px dashed #9a9a9a;font-weight:500}
tr.dim td.d a{opacity:.62}
.approx{font-family:sans-serif;color:#a09d95;cursor:help}

.calgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:18px;margin-top:1.2em}
table.cal{border-collapse:collapse;width:100%;font-family:-apple-system,Helvetica,sans-serif}
table.cal th{font-size:.8rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#6f6f6f;
 border-bottom:1px solid #e2e0da;padding:4px 0 6px;text-align:left}
table.cal td{width:14.28%;text-align:center;padding:3px 0;font-size:.85rem}
table.cal td.dow{color:#a09d95;font-size:.68rem;font-weight:600}
table.cal td.pub a{display:inline-block;min-width:1.7em;padding:2px 1px;border-radius:4px;
 text-decoration:none;background:#e9eff8;color:#35619e;font-weight:600}
table.cal td.pub a:hover{background:#35619e;color:#fff}
table.cal td.nopub{color:#d5d3cc}
details.tips{margin:.6em 0 1em;font-size:.9rem}
details.tips summary{cursor:pointer;color:#3b3bb3}
table.tipstbl td{padding:2px 12px 2px 0;vertical-align:top}
table.tipstbl code{background:#f4f4f2;padding:0 .3em}
a.viewbtn{font-size:.8em;text-decoration:none;border:1px solid;border-radius:3px;padding:1px 9px;margin-left:6px;vertical-align:middle}
a.viewbtn.sbs{color:#2469c8;border-color:#2469c8} a.viewbtn.sbs:hover{background:#2469c8;color:#fff}
a.viewbtn.ovl{color:#e8801a;border-color:#e8801a} a.viewbtn.ovl:hover{background:#e8801a;color:#fff}
body{font-family:Georgia,'Times New Roman',serif;margin:0;background:#fff;color:#222}
.wrap{max-width:960px;margin:0 auto;padding:3em 1.5em}
h1{font-weight:normal;font-size:1.6em;letter-spacing:.01em;border-bottom:1px solid #ddd;padding-bottom:.5em;color:#111}
h1 .home{color:#111;text-decoration:none}
.meta{color:#777;font-size:.95em}
.intro{color:#333;line-height:1.65;margin:1.4em 0;max-width:44em}
.badge{display:inline-block;border:1px solid #ccc;border-radius:2px;padding:.2em .7em;font-size:.8em;color:#555;margin-left:.5em;font-family:-apple-system,Helvetica,sans-serif}
form.search{margin:1.5em 0}
form.search input{width:340px;padding:.5em .6em;border:1px solid #ccc;border-radius:0;font-size:1em;font-family:inherit}
form.search button{padding:.5em 1.2em;background:#fff;color:#222;border:1px solid #222;font-size:.95em;cursor:pointer;margin-left:.4em}
form.search button:hover{background:#222;color:#fff}
ul.issues{columns:4;list-style:none;padding:0;font-size:.88em;font-family:-apple-system,Helvetica,sans-serif}
ul.issues li{margin:.15em 0}
ul.issues a{color:#345;text-decoration:none}
ul.issues a:hover{text-decoration:underline}
.card{border-top:1px solid #e5e5e5;padding:1.1em 0;margin:0}
.card .issue{font-weight:600;color:#111;text-decoration:none;font-size:1.02em}
.card .issue:hover{text-decoration:underline}
.armrow{display:flex;gap:1.2em;margin:.5em 0;align-items:baseline}
.armname{flex:0 0 210px;font-size:.72em;color:#999;text-transform:uppercase;letter-spacing:.08em;font-family:-apple-system,Helvetica,sans-serif}
.snip{font-size:.97em;line-height:1.5}
.snip em{background:none;font-style:normal;border-bottom:2px solid #c8a45c;padding:0 1px}
.more{color:#999;font-size:.82em;font-family:-apple-system,Helvetica,sans-serif}
.missing .snip{color:#aaa;font-style:italic}
"""


import time as _time
_wc_cache = {"tess": None, "vlm": (0, 0.0)}

def tess_word_total():
    if _wc_cache["tess"] is None:
        cache_f = os.path.expanduser("~/solr-bridge/.tess_wordcount")
        if os.path.exists(cache_f):
            _wc_cache["tess"] = int(open(cache_f).read().strip())
        else:
            import subprocess
            # count ALTO <String> elements across the year (one-time, ~1 min)
            r = subprocess.run(["bash", "-c",
                "grep -o '<String ' " + os.path.expanduser("~/tess5-1925-full")
                + "/*/*.xml | wc -l"], capture_output=True, text=True)
            n = int(r.stdout.strip() or 0)
            open(cache_f, "w").write(str(n))
            _wc_cache["tess"] = n
    return _wc_cache["tess"]

def vlm_word_total():
    n, t = _wc_cache["vlm"]
    if _time.time() - t > 300:
        import glob as _glob, json as _json
        cache_f = os.path.expanduser("~/solr-bridge/.vlm_wordcounts.json")
        counts = {}
        if os.path.exists(cache_f):
            try: counts = _json.load(open(cache_f))
            except Exception: counts = {}
        files = _glob.glob(os.path.expanduser("~/solr-bridge/ocr-data/vlm/*.xml")) + \
                _glob.glob(os.path.expanduser("~/solr-bridge/ocr-data/*_desc.miniocr.xml"))
        new_files = [f for f in files if f not in counts]
        for f in new_files:
            try: counts[f] = open(f, errors="ignore").read().count("<w ")
            except Exception: counts[f] = 0
        if new_files:
            _json.dump(counts, open(cache_f, "w"))
        n = sum(counts.get(f, 0) for f in files)
        _wc_cache["vlm"] = (n, _time.time())
    return n



def _arm_bands(arm, page_id, n_bands, page_h):
    import html as _html
    issue = page_id.rsplit("_p", 1)[0]
    bands = [[] for _ in range(n_bands)]
    def band_of(y):
        return min(n_bands - 1, max(0, int(y / max(1, page_h) * n_bands)))
    if arm == "tesseract":
        path = f"{ALTO_ROOT}/{issue}/{page_id}.xml"
        if os.path.exists(path):
            xml = open(path, encoding="utf-8", errors="ignore").read()
            for m in re.finditer(r'<String\b[^>]*>', xml):
                tag = m.group(0)
                c = re.search(r'CONTENT="([^"]*)"', tag)
                v = re.search(r'VPOS="(\d+)"', tag)
                if c and v:
                    bands[band_of(int(v.group(1)))].append(c.group(1))
        return ["<p>" + _html.escape(" ".join(b)) + "</p>" if b else "" for b in bands]
    for cand in (os.path.expanduser(f"~/solr-bridge/ocr-data/vlm/{page_id}.miniocr.xml"),
                 os.path.expanduser(f"~/solr-bridge/ocr-data/{page_id.split('_')[-1]}_desc.miniocr.xml")):
        if os.path.exists(cand):
            xml = open(cand, encoding="utf-8", errors="ignore").read()
            for b in re.findall(r"<b>(.*?)</b>", xml, re.S):
                ws = re.findall(r'<w x="(\d+) (\d+)[^"]*">([^<]*)</w>', b)
                if not ws:
                    continue
                y0 = int(ws[0][1])
                bands[band_of(y0)].append(" ".join(w[2] for w in ws))
            return ["".join("<p>" + _html.escape(t) + "</p>" for t in b) if b else ""
                    for b in bands]
    return [""] * n_bands



_LEX3 = None
def _lex3():
    global _LEX3
    if _LEX3 is None:
        _LEX3 = set()
        for line in open(os.path.expanduser("~/solr-bridge/lexicon_1925.tsv")):
            w, c = line.rstrip("\n").split("\t")
            if int(c) >= 3: _LEX3.add(w)
    return _LEX3

def _arm_words(arm, page_id):
    issue = page_id.rsplit("_p", 1)[0]
    if arm == "tesseract":
        path = f"{ALTO_ROOT}/{issue}/{page_id}.xml"
        if not os.path.exists(path): return []
        return re.findall(r'CONTENT="([^"]*)"',
                          open(path, encoding="utf-8", errors="ignore").read())
    for cand in (os.path.expanduser(f"~/solr-bridge/ocr-data/vlm/{page_id}.miniocr.xml"),
                 os.path.expanduser(f"~/solr-bridge/ocr-data/{page_id.split('_')[-1]}_desc.miniocr.xml")):
        if os.path.exists(cand):
            return re.findall(r"<w[^>]*>([^<]*)</w>",
                              open(cand, encoding="utf-8", errors="ignore").read())
    return []

def _text_stats(words):
    lex = _lex3()
    alpha = [re.sub(r"[^A-Za-z'-]", "", _h.unescape(_h.unescape(w))).lower() for w in words]
    alpha = [w for w in alpha if len(w) >= 2]
    uniq = set(alpha)
    checkable = [w for w in alpha if len(w) >= 4]
    rec = sum(1 for w in checkable if w in lex)
    return {"total": len(words), "uniq": len(uniq),
            "rate": (100.0 * rec / len(checkable)) if checkable else 0.0,
            "suspect": len(checkable) - rec, "vocab": uniq}


@app.route("/corpus")
def corpus_stats():
    import json as _json, subprocess
    sf = os.path.expanduser("~/solr-bridge/corpus_stats.json")
    lock = os.path.expanduser("~/solr-bridge/.corpus_rebuild.lock")
    if not os.path.exists(sf):
        return "<html><body>No corpus stats yet. <form method=post action=/corpus/rebuild><button>Compute now</button></form></body></html>"
    d = _json.load(open(sf))
    scope = request.args.get("scope", "all")
    has_matched = "tesseract_matched" in d
    matched = scope == "matched" and has_matched
    T = d["tesseract_matched"] if matched else d["tesseract"]
    V = d["vlm"]
    care = "" if matched else "care"
    scopebar = ""
    if has_matched:
        scopebar = ('<p class="meta">Scope: '
            + ("<b>full year</b>" if not matched else '<a href="/corpus">full year</a>')
            + " \u00b7 "
            + ("<b>issues processed by both arms</b>" if matched
               else '<a href="/corpus?scope=matched">issues processed by both arms</a>')
            + (" \u2014 Tesseract column restricted to the "
               + f"{d['tesseract_matched'].get('issues', '?')}"
               + " issues the AI pipeline has completed, so the two columns describe the same pages."
               if matched else "") + "</p>")
    rebuilding = os.path.exists(lock)

    def bar(label, lv, rv, fmt="{:,}", note="", tag="", foot=""):
        mx = max(lv, rv, 1)
        tags = {"up": '<span class="dtag good">higher is better \u2191</span>',
                "down": '<span class="dtag good">lower is better \u2193</span>',
                "care": '<span class="dtag care">unequal coverage \u2014 compare via words per page</span>'}
        t = tags.get(tag, "")
        f = '<div class="mfoot">' + foot + '</div>' if foot else ""
        return ('<div class="metric"><div class="mlabel">' + label + t + note + '</div>'
                '<div class="brow"><span class="bname">Tesseract</span>'
                '<div class="bar" style="width:' + f"{lv/mx*100:.0f}" + '%"></div>'
                '<span class="bval">' + fmt.format(lv) + '</span></div>'
                '<div class="brow"><span class="bname">AI pipeline</span>'
                '<div class="bar ai" style="width:' + f"{rv/mx*100:.0f}" + '%"></div>'
                '<span class="bval">' + fmt.format(rv) + '</span></div>' + f + '</div>')

    def wl(title, pairs):
        lis = "".join('<li>' + w + ' <span class="wc">' + f"{c:,}" + '</span></li>'
                      for w, c in pairs[:250])
        return ('<div class="wl"><h3>' + title + '</h3><ul>' + lis + '</ul></div>')

    charts = (bar("Pages processed", T["pages"], V["pages"], tag=care)
              + bar("Total words", T["total_words"], V["total_words"], tag=care)
              + bar("Words per page", round(T["total_words"]/max(1,T["pages"])),
                    round(V["total_words"]/max(1,V["pages"])))
              + bar("Unique words", T["unique"], V["unique"], tag="down",
                    note=" \u2014 OCR noise inflates unique counts at corpus scale")
              + bar("Recognized vocabulary", T["recognized_pct"], V["recognized_pct"], "{:.2f}%",
                    tag="up",
                    foot="Lexicon caveat: \u201crecognized\u201d is attested \u22653\u00d7 in a lexicon "
                         "built from the Tesseract corpus, so this measure slightly favors Tesseract; "
                         "the AI pipeline leading here is despite that handicap.")
              + bar("Suspect words (distinct)", T["suspect_distinct"], V["suspect_distinct"], tag="down")
              + bar("Suspect tokens", T["suspect_tokens"], V["suspect_tokens"], tag="down"))
    lists = (wl("Most frequent Tesseract suspects (systematic error signatures)",
                d["top_suspects_tesseract"])
             + wl("Most frequent AI-pipeline suspects", d["top_suspects_vlm"])
             + wl("Top vocabulary only in AI pipeline", d["top_only_vlm"])
             + wl("Top vocabulary only in Tesseract", d["top_only_tesseract"]))
    reb = ('<p class="meta"><i>Recomputing now\u2026 refresh in a minute.</i></p>' if rebuilding
           else '<form method="post" action="/corpus/rebuild" style="display:inline">'
                '<button style="padding:.35em 1em;background:#fff;border:1px solid #222;'
                'cursor:pointer;font-size:.85em">Recompute statistics</button></form>')
    style = """
    .metric{margin:1.2em 0;max-width:46em}
    .mlabel{font-size:.8em;color:#777;text-transform:uppercase;letter-spacing:.06em;
      font-family:-apple-system,Helvetica,sans-serif;margin-bottom:.3em}
    .brow{display:flex;align-items:center;gap:.6em;margin:.2em 0}
    .bname{flex:0 0 90px;font-size:.8em;color:#666;font-family:-apple-system,Helvetica,sans-serif}
    .bar{height:14px;background:#999;min-width:2px}
    .bar.ai{background:#c8a45c}
    .dtag{font-size:.85em;text-transform:none;letter-spacing:0;margin-left:.8em;
      border-radius:999px;padding:1px 8px;font-weight:600}
    .dtag.good{background:#e6f2ea;color:#2f7d4e}
    .dtag.care{background:#f7efdc;color:#a06f14}
    .mfoot{font-size:.78em;color:#8a877f;font-family:-apple-system,Helvetica,sans-serif;
      margin-top:.3em;max-width:46em}
    .bval{font-size:.85em;color:#333;font-family:-apple-system,Helvetica,sans-serif}
    .lists{display:flex;flex-wrap:wrap;gap:2em;margin-top:2em}
    .wl{flex:1 1 280px;min-width:260px}
    .wl h3{font-weight:600;font-size:.85em;color:#555;border-bottom:1px solid #eee;padding-bottom:.3em}
    .wl ul{list-style:none;padding:0;font-size:.85em;columns:2;font-family:-apple-system,Helvetica,sans-serif}
    .wl li{margin:.1em 0;overflow-wrap:anywhere;word-break:break-all}
    .wc{color:#aaa;font-size:.85em}"""
    return ("<html><head><title>Corpus statistics</title><style>" + CSS + style
            + "</style></head><body><div class=\"wrap\" style=\"max-width:1300px\">"
            + '<p class="meta"><a href="/">\u2190 Home</a></p>'
            + '<h1><a href="/" class="home">Daily Colonist 1925</a> \u2014 corpus statistics</h1>'
            + '<p class="meta">Generated ' + d["generated"] + ' (took ' + str(d["elapsed_s"])
            + 's). The AI pipeline column covers pages processed so far and grows as the year run '
            + 'proceeds. \u201cRecognized\u201d = attested \u22653\u00d7 in the year corpus lexicon '
            + '(built from Tesseract text, so it favors Tesseract). ' 
            + 'Word counts include AI image descriptions. '
            + '\u00b7 Shared unique vocabulary: ' + f"{d['shared_unique']:,}"
            + ' \u00b7 only Tesseract: ' + f"{d['only_tesseract']:,}"
            + ' \u00b7 only AI: ' + f"{d['only_vlm']:,}" + '</p>'
            + reb + scopebar + charts + '<div class="lists">' + lists + '</div></div></body></html>')

@app.route("/corpus/rebuild", methods=["POST"])
def corpus_rebuild():
    import subprocess
    lock = os.path.expanduser("~/solr-bridge/.corpus_rebuild.lock")
    if not os.path.exists(lock):
        open(lock, "w").write(str(os.getpid()))
        script = os.path.expanduser("~/solr-bridge/build_corpus_stats.py")
        subprocess.Popen(["bash", "-c",
            f"python3 {script} > /tmp/corpus_rebuild.log 2>&1; rm -f {lock}"])
    return '<html><head><meta http-equiv="refresh" content="3;url=/corpus"></head>' \
           '<body>Recomputing\u2026 redirecting.</body></html>'

@app.route("/page/<page_id>")
def single_page(page_id):
    issue = page_id.rsplit("_p", 1)[0]
    return f"""<html><head><title>{page_id}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/openseadragon/4.1.0/openseadragon.min.js"></script>
<style>body{{margin:0;background:#181818}}#osd{{position:absolute;top:0;bottom:0;left:0;right:0}}
.chip{{position:fixed;bottom:10px;left:12px;z-index:9999;background:#fff;border:1px solid #ccc;
border-radius:2px;padding:4px 12px;font-family:-apple-system,Helvetica,sans-serif;font-size:13px;
color:#333;text-decoration:none;box-shadow:0 1px 3px rgba(0,0,0,.3);margin-right:6px}}</style>
</head><body><div id="osd"></div>
<a class="chip" href="/">\u2190 Home</a>
<a class="chip" style="left:110px" href="/text/{page_id}" target="_blank">Page text</a>
<script>OpenSeadragon({{id:"osd", prefixUrl:"https://cdnjs.cloudflare.com/ajax/libs/openseadragon/4.1.0/images/",
tileSources:"{IIIF_IMG}/{issue}%2F{page_id}.png/info.json"}});</script>
</body></html>"""


@app.route("/stats/<page_id>")
def page_stats(page_id):
    from collections import Counter
    issue = page_id.rsplit("_p", 1)[0]
    lex = _lex3()
    def full(arm):
        words = _arm_words(arm, page_id)
        alpha = [re.sub(r"[^A-Za-z'-]", "", _h.unescape(_h.unescape(w))).lower() for w in words]
        alpha = [w for w in alpha if len(w) >= 2]
        freq = Counter(alpha)
        checkable = {w for w in freq if len(w) >= 4}
        suspects = Counter({w: freq[w] for w in checkable if w not in lex})
        return {"total": len(words), "freq": freq, "uniq": set(freq),
                "suspects": suspects,
                "rate": 100.0 * sum(freq[w] for w in checkable if w in lex) /
                        max(1, sum(freq[w] for w in checkable))}
    L, R = full("tesseract"), full("vlm")
    shared = L["uniq"] & R["uniq"]
    only_l = L["uniq"] - R["uniq"]; only_r = R["uniq"] - L["uniq"]

    def bar(label, lv, rv, fmt="{:,}"):
        mx = max(lv, rv, 1)
        return ('<div class="metric"><div class="mlabel">' + label + '</div>'
                '<div class="brow"><span class="bname">Tesseract</span>'
                '<div class="bar" style="width:' + f"{lv/mx*100:.0f}" + '%"></div>'
                '<span class="bval">' + fmt.format(lv) + '</span></div>'
                '<div class="brow"><span class="bname">AI pipeline</span>'
                '<div class="bar ai" style="width:' + f"{rv/mx*100:.0f}" + '%"></div>'
                '<span class="bval">' + fmt.format(rv) + '</span></div></div>')

    def wordlist(title, coll, freq=None, n=250):
        if isinstance(coll, Counter):
            items = coll.most_common(n)
        else:
            f = freq or {}
            items = sorted(((w, f.get(w, 0)) for w in coll), key=lambda x: -x[1])[:n]
        lis = "".join('<li>' + w + ' <span class="wc">' + str(c) + '</span></li>'
                      for w, c in items)
        note = f" (top {n})" if len(coll) > n else ""
        return ('<div class="wl"><h3>' + title + ' \u00b7 ' + f"{len(coll):,}" + note
                + '</h3><ul>' + lis + '</ul></div>')

    charts = (bar("Total words", L["total"], R["total"])
              + bar("Unique words", len(L["uniq"]), len(R["uniq"]))
              + bar("Recognized vocabulary", L["rate"], R["rate"], "{:.1f}%")
              + bar("Suspect words (distinct)", len(L["suspects"]), len(R["suspects"])))
    lists = (wordlist("Tesseract suspect words", L["suspects"])
             + wordlist("AI pipeline suspect words", R["suspects"])
             + wordlist("Vocabulary only in Tesseract", only_l, L["freq"])
             + wordlist("Vocabulary only in AI pipeline", only_r, R["freq"])
             + wordlist("Shared vocabulary (by frequency)", shared,
                        Counter({w: L["freq"][w] + R["freq"][w] for w in shared})))
    style = """
    .metric{margin:1.2em 0;max-width:46em}
    .mlabel{font-size:.8em;color:#777;text-transform:uppercase;letter-spacing:.06em;
      font-family:-apple-system,Helvetica,sans-serif;margin-bottom:.3em}
    .brow{display:flex;align-items:center;gap:.6em;margin:.2em 0}
    .bname{flex:0 0 90px;font-size:.8em;color:#666;font-family:-apple-system,Helvetica,sans-serif}
    .bar{height:14px;background:#999;min-width:2px}
    .bar.ai{background:#c8a45c}
    .bval{font-size:.85em;color:#333;font-family:-apple-system,Helvetica,sans-serif}
    .lists{display:flex;flex-wrap:wrap;gap:2em;margin-top:2em}
    .wl{flex:1 1 260px;min-width:240px}
    .wl h3{font-weight:600;font-size:.85em;color:#555;border-bottom:1px solid #eee;padding-bottom:.3em}
    .wl ul{list-style:none;padding:0;font-size:.85em;columns:2;font-family:-apple-system,Helvetica,sans-serif}
    .wl li{margin:.1em 0;overflow-wrap:anywhere;word-break:break-all}
    .wc{color:#aaa;font-size:.85em}"""
    return ("<html><head><title>" + page_id + " statistics</title><style>" + CSS + style
            + "</style></head><body><div class=\"wrap\" style=\"max-width:1300px\">"
            + '<h1><a href="/" class="home">Daily Colonist 1925</a> \u2014 ' + page_id
            + ': text statistics</h1>'
            + '<p class="meta"><a href="/text/' + page_id + '">text comparison</a> \u00b7 '
            + '<a href="/page/' + page_id + '" target="_blank">page image</a>. '
            + '\u201cRecognized\u201d = attested \u22653\u00d7 in the year corpus lexicon '
            + '(built from Tesseract text, so it slightly favors Tesseract). '
            + '\u201cSuspect\u201d = 4+ letter word not in that lexicon.</p>'
            + charts + '<div class="lists">' + lists + '</div></div></body></html>')

@app.route("/text/<page_id>")
def page_text_split(page_id):
    issue = page_id.rsplit("_p", 1)[0]
    _, page_h = page_dims(issue, page_id)
    N = 6

    def _band_vocab(h):
        t = re.sub(r"<[^>]+>", " ", h or "")
        return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", t)}

    def _mark_only(h, other, cls):
        parts = re.split(r"(<[^>]+>)", h)
        def sub(seg):
            return re.sub(r"[A-Za-z][A-Za-z'-]{2,}",
                lambda m: (f'<span class="od {cls}">{m.group(0)}</span>'
                           if m.group(0).lower() not in other else m.group(0)), seg)
        return "".join(p if p.startswith("<") else sub(p) for p in parts)
    left = _arm_bands("tesseract", page_id, N, page_h)
    right = _arm_bands("vlm", page_id, N, page_h)
    ws_l = _text_stats(_arm_words("tesseract", page_id))
    ws_r = _text_stats(_arm_words("vlm", page_id))
    shared = len(ws_l["vocab"] & ws_r["vocab"])
    only_l = len(ws_l["vocab"] - ws_r["vocab"])
    only_r = len(ws_r["vocab"] - ws_l["vocab"])
    def statcell(s):
        return (f'<b>{s["total"]:,}</b> words \u00b7 <b>{s["uniq"]:,}</b> unique \u00b7 '
                f'<b>{s["rate"]:.1f}%</b> recognized vocabulary \u00b7 '
                f'<b>{s["suspect"]:,}</b> suspect words')
    stats = (f'<div class="bandrow stats"><div>{statcell(ws_l)}</div>'
             f'<div>{statcell(ws_r)}</div></div>'
             f'<p class="meta">Shared unique vocabulary: <b>{shared:,}</b> \u00b7 '
             f'only in Tesseract: <b>{only_l:,}</b> \u00b7 only in AI pipeline: <b>{only_r:,}</b>. '
             f'\u201cRecognized\u201d = attested \u22653\u00d7 in the year\u2019s own text '
             f'(a lexicon built from the Tesseract corpus, so this measure slightly favors Tesseract).</p>')
    stats += "<p class=" + chr(34) + "meta" + chr(34) + "><a target=" + chr(34) + "_blank" + chr(34) + " href=" + chr(34) + "/stats/" + page_id + chr(34) + ">full statistics</a></p>"
    LAB = ["Stratum 1 \u00b7 top of page", "Stratum 2", "Stratum 3",
           "Stratum 4", "Stratum 5", "Stratum 6 \u00b7 bottom of page"]
    rows = stats
    for i in range(N):
        vl, vr = _band_vocab(left[i]), _band_vocab(right[i])
        l = _mark_only(left[i], vr, "tl") if left[i] else '<span class="nix">\u2014</span>'
        r = _mark_only(right[i], vl, "vl") if right[i] else '<span class="nix">\u2014</span>'
        rows += (f'<div class="bandlab">{LAB[i]}</div>'
                 f'<div class="bandrow"><div>{l}</div><div>{r}</div></div>')
    return f"""<html><head><title>{page_id} \u2014 text comparison</title><style>{CSS}
    .cols{{display:flex;gap:2.5em}}
    .cols h2{{flex:1;font-weight:normal;font-size:.78em;color:#999;text-transform:uppercase;
      letter-spacing:.08em;font-family:-apple-system,Helvetica,sans-serif;
      border-bottom:1px solid #eee;padding-bottom:.4em;margin-bottom:0}}
    .bandrow{{display:flex;gap:2.5em;border-bottom:1px dashed #e8e8e8;padding:.8em 0}}
    .bandrow>div{{flex:1;min-width:0}}
    .bandrow p{{line-height:1.6;margin:0 0 .8em;font-size:.93em}}
    .nix{{color:#ccc}}
    .bandlab{{font-family:-apple-system,Helvetica,sans-serif;font-size:.68em;font-weight:700;
      letter-spacing:.08em;text-transform:uppercase;color:#8a877f;margin-top:1.4em}}
    .od.tl{{background:#e9eff8;box-shadow:0 1px 0 #35619e}}
    .od.vl{{background:#f7efdc;box-shadow:0 1px 0 #a06f14}}
    body.nodiff .od{{background:transparent;box-shadow:none}}\n    .stats{{background:#fafaf8;font-family:-apple-system,Helvetica,sans-serif;font-size:.85em;color:#444;border-bottom:1px solid #e5e5e5}}</style></head>
    <body><div class="wrap" style="max-width:1400px">
    <h1><a href="/" class="home">Daily Colonist 1925</a> \u2014 {page_id}: text comparison</h1>
    <p class="meta">Text is grouped into six horizontal strata of the physical page, so both
    columns stay aligned to the same page regions. <a href="/page/{page_id}" target="_blank">view page image</a></p>
    <p class="meta"><label><input type="checkbox" checked
      onchange="document.body.classList.toggle('nodiff', !this.checked)">
      highlight words appearing in only one column within each stratum
      (Tesseract-only tinted blue, AI-only gold)</label></p>
    <div class="cols"><h2>Traditional OCR (Tesseract)</h2><h2>AI Vision Pipeline</h2></div>
    {rows}</div></body></html>"""

@app.route("/")
def index():
    done = len(glob.glob(os.path.expanduser("~/paddle-year/*/.done")))
    import calendar as _cal
    groups = {}
    for i in ISSUES:
        d = ISSUE_DATES.get(i, {}).get("date")
        groups.setdefault(d[:7] if d else "zzz-unknown", []).append(i)
    parts = ['<div class="calgrid">']
    for mk in sorted(groups):
        if mk == "zzz-unknown":
            continue
        y, mo = int(mk[:4]), int(mk[5:7])
        by_day = {}
        for i in groups[mk]:
            e = ISSUE_DATES.get(i, {})
            d = e.get("date")
            if d:
                by_day[int(d[8:10])] = (i, e)
        cells = ["<table class=cal><tr><th colspan=7>"
                 + _cal.month_name[mo] + " " + str(y) + "</th></tr>"
                 "<tr>" + "".join(f"<td class=dow>{w}</td>" for w in
                                  ["S", "M", "T", "W", "T", "F", "S"]) + "</tr>"]
        _cal.setfirstweekday(_cal.SUNDAY)
        for week in _cal.monthcalendar(y, mo):
            row = []
            for day in week:
                if day == 0:
                    row.append("<td></td>")
                elif day in by_day:
                    i, e = by_day[day]
                    pp = len(issue_pages(i))
                    tip = f"{pp} pages \u00b7 {i}"
                    if e.get("source") == "inferred":
                        tip += " \u00b7 date inferred"
                    row.append(f'<td class=pub><a href="/view/{i}" title="{tip}">{day}</a></td>')
                else:
                    row.append(f'<td class=nopub>{day}</td>')
            cells.append("<tr>" + "".join(row) + "</tr>")
        cells.append("</table>")
        parts.append("".join(cells))
    unk = groups.get("zzz-unknown", [])
    parts.append("</div>")
    if unk:
        parts.append('<p class="meta">Date pending: '
                     + " \u00b7 ".join(f'<a href="/view/{i}">{i}</a>' for i in unk) + "</p>")
    items = "".join(parts)
    return f"""<html><head><title>Daily Colonist 1925 — OCR Comparison</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>The Daily Colonist, 1925 — OCR Comparison Testbed</h1>
<div class="intro">This prototype compares two ways of making UVic Libraries’
digitized newspapers searchable: <b>Traditional OCR (Tesseract)</b>, the current
standard, and an <b>AI Vision Pipeline (VLM)</b> that reads page layout, transcribes
text, and describes photographs and illustrations so that images themselves become
searchable. Search across the full year below, or open any issue to compare the two
approaches side by side. Every result links to the exact spot on the page.</div>
<form class="search" action="/findall"><input name="q"
 placeholder="Search all of 1925…"><button>Search year</button></form>
<details class="tips"><summary>Search tips — phrases, AND/OR/NOT, wildcards, fuzzy</summary>
<table class="tipstbl">
<tr><td><code>beecham pills</code></td><td>plain words search as an exact phrase, in order</td></tr>
<tr><td><code>beecham AND liver</code></td><td>both words anywhere on the page (operators must be UPPERCASE)</td></tr>
<tr><td><code>beecham NOT pills</code></td><td>pages with the first word but not the second</td></tr>
<tr><td><code>(logging OR lumber) AND strike</code></td><td>group alternatives with parentheses</td></tr>
<tr><td><code>"beecham pills"~5</code></td><td>the words within 5 words of each other — useful when OCR noise interrupts a phrase</td></tr>
<tr><td><code>esquimal*</code></td><td>wildcard: any ending — catches OCR-damaged word endings</td></tr>
<tr><td><code>beecham~1</code></td><td>fuzzy: within one character of the spelling — finds OCR misreads like "Peecham"</td></tr>
<tr><td><code>regatta AND issue_date:[1925-06-01 TO 1925-08-31]</code></td><td>restrict to a date range \u2014 whole months work too: <code>[1925-06 TO 1925-08]</code>, single day: <code>issue_date:1925-06-01</code></td></tr>
</table>
<p class="meta">Notes: searches match both arms' text including AI image descriptions; possessives
and accents are normalized (beecham = beecham's, montreal = montréal); modern spellings match
1925 forms (tomorrow = to-morrow). Wildcard and fuzzy matches may highlight imperfectly on the page image.</p>
</details>
<p class="meta">312 issues · 6,647 pages
<span class="badge">Traditional OCR: 312/312 issues \u00b7 {tess_word_total():,} words</span>
<span class="badge">AI Pipeline: {done}/312 issues \u00b7 {vlm_word_total():,} words</span></p>
<p class="meta"><a href="/corpus">Corpus statistics: both arms compared across the full year \u2192</a></p><p class="meta"><a href="/about">How this was built — full technical documentation \u2192</a></p>{items}</div></body></html>"""


ABOUT_HTML = """
<div class="wrap doc">
<p class="meta"><a href="/">\u2190 Home</a></p>
<h1><a href="/" class="home">Daily Colonist 1925</a> — How This Was Built</h1>
<p class="meta">Complete technical documentation of the OCR comparison pipeline. UVic Libraries prototype, July 2026.</p>

<h2>1. Overview</h2>
<p>This testbed compares two approaches to making digitized historical newspapers searchable,
using the full 1925 run of Victoria's <i>Daily Colonist</i> (312 issues, 6,647 pages):</p>
<ul>
<li><b>Traditional OCR (Tesseract)</b> — the current standard in library digitization: Tesseract 5 producing word-level ALTO XML.</li>
<li><b>AI Vision Pipeline (VLM)</b> — a vision-language-model pipeline (PaddleOCR-VL) that performs layout analysis, reading-order detection, and transcription, plus a second VLM (Qwen2.5-VL) that writes searchable descriptions of photographs and illustrations, and an optional constrained LLM post-correction stage.</li>
</ul>
<p>Both arms are indexed into one Solr core with coordinate-preserving OCR highlighting, served
through IIIF (Cantaloupe + Mirador), so identical searches can be compared side by side with
hits highlighted on the page image.</p>

<h2>2. Source data</h2>
<ul>
<li><b>Images:</b> JPEG2000 masters from the digitized Daily Colonist collection, one zip per issue
(<code>~/dailycolonist-1925-jp2z/&lt;issue&gt;/&lt;issue&gt;_jp2.zip</code>), ~2 MB/page compressed,
decoding to full-resolution PNGs of roughly 7,300–7,400 px wide (~20 MB each; dimensions vary per page and are read per page, never assumed).</li>
<li><b>Issue list:</b> <code>~/ids-1925.txt</code> — 312 issue identifiers of the form
<code>dailycolonistMMYYuvic_N</code>.</li>
<li><b>Decoder:</b> <code>opj_decompress</code> (OpenJPEG), the same tool used by the original
Tesseract campaign — guaranteeing both arms share one pixel coordinate frame per page.</li>
</ul>

<h2>3. Arm A — Traditional OCR (Tesseract)</h2>
<ul>
<li><b>Engine:</b> Tesseract 5 (<code>~/tesseract_ocr_year.py</code>), run over the decoded PNGs.</li>
<li><b>Output:</b> ALTO v3 XML per page with pixel-unit word coordinates
(<code>~/tess5-1925-full/&lt;issue&gt;/&lt;page&gt;.xml</code>), plus hOCR/TSV/TXT siblings.
19.6M words across the year; per-page stats in <code>~/tess5-1925/per_page.csv</code>.</li>
<li><b>Indexing:</b> ALTO files are indexed directly — the Solr OCR-highlighting plugin reads ALTO
natively, so this arm needs no conversion.</li>
</ul>

<h2>4. Arm B — AI Vision Pipeline</h2>
<h3>4.1 Layout + transcription: PaddleOCR-VL 1.6</h3>
<ul>
<li><b>Models:</b> PP-DocLayoutV3 (132 MB detection model, runs locally in-process) + PaddleOCR-VL-1.6-0.9B
(a ~0.9B ERNIE-based vision-language recognition model), orchestrated by the <code>paddleocr</code>/<code>paddlex</code>
Python pipeline (v3.7).</li>
<li><b>Serving:</b> the 0.9B VL model is served by vLLM in Docker
(<code>vllm/vllm-openai:v0.20.1</code>, container <code>paddlevl</code>, port 8110,
<code>--gpu-memory-utilization 0.30 --max-model-len 32768</code>) because the native Paddle inference
engine fell back to single-core CPU on this machine. The pipeline connects with
<code>vl_rec_backend="vllm-server"</code>.</li>
<li><b>Input sizing:</b> each page is downscaled to 2560 px wide before layout analysis (VLM layout
models degrade badly on 70-megapixel inputs — a documented finding of this project); all output
coordinates are rescaled per page by <code>original_width / 2560</code>.</li>
<li><b>Output:</b> per page, a JSON of semantic blocks in reading order — labels include
<code>text</code>, <code>paragraph_title</code>, <code>doc_title</code>, <code>header</code>,
<code>image</code>, <code>vision_footnote</code> (printed photo captions) — each with bounding box
and transcribed content. Typical page: 100–260 blocks, 2–11 s of GPU time.</li>
</ul>

<h3>4.2 Image descriptions: Qwen2.5-VL-7B</h3>
<ul>
<li><b>Model:</b> Qwen/Qwen2.5-VL-7B-Instruct served by vLLM in Docker (container
<code>describer</code>, port 8120, <code>--gpu-memory-utilization 0.60</code>). Both VLM servers
run simultaneously on one RTX 6000 Ada (48 GB).</li>
<li><b>Script:</b> <code>~/solr-bridge/describe_images.py</code> — crops every <code>image</code>
block from the <i>full-resolution</i> PNG, de-duplicates overlapping boxes (skips boxes ≥70%
contained in an already-described one), pairs any printed caption (<code>vision_footnote</code>
within 120 px below the image) into the prompt, and writes the returned description into the
block's content so it is indexed and highlighted at the illustration's own coordinates.</li>
<li><b>Prompt (verbatim):</b></li>
</ul>
<pre>This is an illustration or photograph from a 1925 Canadian newspaper
(The Daily Colonist, Victoria BC). Describe it in 2-3 sentences for a
search index: the subject, any product or activity shown, and transcribe
any text visible within the image. Be factual and specific.
[if a printed caption exists:]
The printed caption below this image reads: "..."</pre>
<p>This stage is what makes <i>photographs and advertisements searchable</i>: e.g. a query for
"submarine" matches a photo description and highlights the photograph itself — impossible in the
Tesseract arm. ~90 images described per typical issue at ~1–3 s each.</p>

<h3>4.3 Optional post-correction (fourth arm: <code>paddleocr-vl-corrected</code>)</h3>
<ul>
<li><b>Design:</b> constrained candidate selection, never freeform rewriting. A word is a
<i>suspect</i> only if its corpus frequency &lt; 3; candidates come from SymSpell edit-distance ≤ 2
lookups against words attested ≥ 25 times in the year's own text.</li>
<li><b>Lexicon:</b> <code>~/solr-bridge/lexicon_1925.tsv</code> — 713,822 distinct words /
16M tokens built from the year's Tesseract text. The corpus itself acts as the period thesaurus:
local vocabulary self-whitelists (e.g. "Grocerteria": 209 occurrences, "Esquimalt": 2,269).</li>
<li><b>Tier 1 (AUTO, deterministic):</b> applied without the LLM when a suspect has exactly one
candidate, candidate frequency ≥ 50, suspect frequency = 0, and the edit is visually plausible OCR
damage — a hyphenation repair or ≤2 substitutions all drawn from a microfilm confusion set
(i↔l, c↔e, c↔o, e↔o, f↔p, f↔t, v↔c, u↔n, u↔v, h↔b, m↔n, i↔j).</li>
<li><b>Tier 2 (LLM):</b> Qwen2.5-VL chooses from the closed candidate list (with corpus
frequencies as evidence) or answers UNCHANGED. Prompt (verbatim):</li>
</ul>
<pre>You are correcting OCR errors in text from The Daily Colonist, a newspaper
published in Victoria, British Columbia, Canada, in 1925.

Relevant context: The text uses 1920s Canadian English, with British spellings
(colour, honour, centre) and period conventions (to-day, to-morrow, per cent).
Prices are in dollars and cents; measurements are imperial. The paper covers
Victoria and Vancouver Island: local place names (Esquimalt, Saanich, Oak Bay,
Nanaimo, Cadboro Bay), local businesses, shipping news (CPR steamships,
schooners), and British Empire news are common. Many words that look unusual
are real: period brand names, local businesses, and surnames.

A suspect word from this text is given below with its surrounding context and
a list of candidate corrections drawn from words attested in this newspaper.

Rules:
- Answer with EXACTLY one candidate from the list, or UNCHANGED.
- Corpus frequency is given for each candidate: prefer candidates that fit the
  context AND are well-attested.
- If the suspect could plausibly be a surname, business name, or place name
  not in the candidate list, answer UNCHANGED.
- Never choose a candidate that changes the meaning; when in doubt, UNCHANGED.

Context: ...{ctx}...
Suspect word: {word}
Candidates (with frequency in this newspaper): {cands}
Answer:</pre>
<ul>
<li><b>Safety properties:</b> the model cannot introduce a word not offered; token counts are
preserved (one-for-one substitution) so coordinates survive; every change is logged to
<code>~/solr-bridge/corrected/audit.jsonl</code> as (original, correction, tier).</li>
<li><b>Measured result (issue of Jan 1, 1925):</b> 77 corrections (18 AUTO / 59 LLM); 51 corrected
terms became findable on pages where they previously were not (e.g. Kameloops→Kamloops,
STRKE→STRIKE, ratpayers→ratepayers, Sfrott-Shaw→Sprott-Shaw); zero proper-noun damage observed.</li>
<li><b>Scripts:</b> <code>~/solr-bridge/correct_text.py</code> (corrector + audit mode),
<code>~/solr-bridge/correct_apply.py</code> (apply mode producing corrected JSONs).</li>
</ul>

<h2>5. Geometry: from blocks to highlightable words</h2>
<p><code>~/solr-bridge/paddle_to_miniocr.py</code> converts each page's JSON to
<b>MiniOCR</b> (the Solr plugin's compact format), scaled to full resolution:</p>
<ul>
<li>Blocks become MiniOCR blocks; block text is split into lines — explicit line breaks are trusted
when they imply plausible line heights (30–140 px at full resolution); otherwise lines are
synthesized by wrapping words into bands of ~62 px (typical 1925 body type).</li>
<li>Word boxes are synthesized by proportional character-count division within each line — an
approximation adequate for highlight display (the Tesseract arm, by contrast, has true word boxes
from ALTO).</li>
</ul>

<h3>5.1 Text normalization at conversion time</h3>
<p>Three normalization passes run between the raw VLM output and the MiniOCR, all reversible
(raw JSONs are immutable; re-deriving the year takes ~3 min of CPU): table-markup removal
(§11.1), em/en-dash separation (§11.3), and conservative line-break dehyphenation (§11.2).
Order matters: markup removal precedes line assembly so tags-only blocks vanish; joining runs
only on lines whose breaks the VLM asserted explicitly.</p>
<h2>6. Search: Solr + OCR highlighting</h2>
<ul>
<li><b>Solr 9</b> in Docker (container <code>solr-ocr</code>, port 8983, core <code>colonist</code>),
with the <a href="https://github.com/dbmdz/solr-ocrhighlighting">dbmdz solr-ocrhighlighting</a>
plugin v0.9.1.</li>
<li><b>Schema:</b> an <code>ocr_text</code> field whose index-time analyzer chains
<code>ExternalUtf8ContentFilterFactory</code> (documents are indexed as <i>file pointers</i> to
ALTO/MiniOCR on disk) and <code>OcrCharFilterFactory</code>; queries return pixel-coordinate
regions for every hit. Solr runs with <code>SOLR_SECURITY_MANAGER_ENABLED=false</code> to permit
external file loading.</li>
<li><b>Documents:</b> one per page per arm, distinguished by a <code>source</code> field
(<code>tesseract</code> | <code>paddleocr-vl</code> | <code>paddleocr-vl-corrected</code>) — the
mechanism that makes retrieval comparison a simple filter.</li>
</ul>

<h3>6.1 Text analysis chain</h3>
<p>Both arms share one analysis chain on the <code>ocr_text</code> field, so retrieval
differences reflect the OCR, never the analyzer. Beyond tokenization and lowercasing, the
chain normalizes three things user testing exposed (2026-07-09): <b>apostrophe style</b> —
Tesseract preserves 1925 curly apostrophes (’) while the VLM emits straight ones, so
identical names indexed as different tokens until normalized; <b>possessives</b> — a trailing
<code>'s</code> is stripped at index and query time, so "beecham" and "beecham's" (either
apostrophe style) converge on one token (result sets went from three disjoint slices of
21/10/15 pages to one set of 41); and <b>accents</b> — ASCII folding makes "Montreal" match
"Montréal" (5,203 pages, verified equal). A query-time-only synonym layer bridges modern
spelling to 1925 orthography ("tomorrow" → "to-morrow", "percent" → "per cent"), extensible
without re-indexing. Deliberately declined: stopword removal (breaks phrase search),
Porter stemming (damages the proper nouns newspapers are full of), and word-delimiter
splitting (its token-graph rewrites endanger the highlight offsets this whole demo rests
on).</p>
<h2>7. Viewing: IIIF stack</h2>
<ul>
<li><b>Image server:</b> Cantaloupe 5.0.6 in Docker (port 8182) over
<code>~/colonist-images/&lt;issue&gt;/&lt;page&gt;.png</code>, slash-substituted identifiers.</li>
<li><b>Shim:</b> a Flask app (<code>~/solr-bridge/demo/app.py</code>, port 8888) that generates
IIIF Presentation manifests per issue per arm (canvas sizes read from each page's ALTO header),
implements the IIIF Content Search API by translating Solr's coordinate regions into annotations
with <code>#xywh</code> fragments, and serves this site.</li>
<li><b>Viewer:</b> Mirador 3, two windows per issue (one per arm), search panels open on load,
searches auto-populated from year-search links, camera pans/zooms to a clicked hit.</li>
</ul>

<h2>8. Batch machinery</h2>
<ul>
<li><code>~/solr-bridge/decode_year.py</code> — parallel JP2→PNG decode of the whole year
(10 workers, resumable, ~6–8 h for 6,647 pages).</li>
<li><code>~/solr-bridge/run_year.py</code> — the per-issue pipeline (decode-if-needed → Paddle →
describe → convert → index), resumable via <code>.done</code> markers, per-page failure quarantine
to <code>~/paddle-year/quarantine.jsonl</code>. Full-year GPU cost ≈ 7–10 h with both models resident.</li>
</ul>

<h3>8.1 Full-year run, measured</h3>
<p>The production run processed all <b>312 issues / 6,647 pages</b> with <b>zero quarantined
pages</b>, at ~4.5 min per issue on a single GPU (roughly two days wall-clock, resumable, run
unattended). JP2→PNG decoding: 4.8 h for the year at 0.4 pages/s. Downstream, the derived
layers are cheap: full MiniOCR re-conversion of the year takes ~3 min (CPU only, ~34 pages/s),
and corpus statistics rebuild in ~65 s — which is what makes the measure-fix-remeasure loop
of §11 practical.</p>
<h2>9. How the pipeline was chosen (evaluation history)</h2>
<p>Several modern VLM/OCR systems were tested on the same stress pages before PaddleOCR-VL was
selected:</p>
<ul>
<li><b>Chandra</b> (earlier session): hit an 8,192-token output ceiling and empty-generation loops
on dense broadsheet pages.</li>
<li><b>Surya</b>: its layout model collapsed to a near-uniform grid on the 70 MP broadsheets (and
still merged regions at proper input scale); its <i>line detector</i>, however, proved excellent
(670 accurate line boxes in 2 s) and remains a candidate for future line-precision highlighting.
Five hand-rolled attempts to cluster its lines into blocks (projection profiles, XY-cut, etc.)
reproduced known failures of classical layout analysis on tight-guttered newspapers.</li>
<li><b>dots.ocr</b>: excellent on editorial columns, but systematically omitted display
advertisements — disqualifying for a collection where ads are first-class search targets.</li>
<li><b>PaddleOCR-VL 1.6</b>: captured news columns <i>and</i> ads <i>and</i> images with correct
reading order on all five golden-set stress pages (front page, ad page, dense page, pictorial
page, degraded classifieds) at 2–11 s/page — selected as the pipeline core.</li>
</ul>
<p>On the pictorial page (p013 of Jan 1), Tesseract extracted 19 junk words; the VLM arm produced
18 photographs, each with its printed caption transcribed and an AI description — the clearest
single illustration of the difference between the approaches.</p>

<h2>10. Known limitations</h2>
<ul>
<li><b>VLM-arm word coordinates are interpolated, not measured.</b> PaddleOCR-VL reports
block-level geometry; word boxes are synthesized by distributing each line's text
proportionally by character count. Line (vertical) position is reliable; horizontal position
can drift by several word-widths. Consequences: highlight boxes in the VLM pane can sit
beside rather than on the word, and the overlay comparison view cannot test box equality —
its matcher treats a Tesseract and a VLM hit as the same word if they lie on the same line
(vertical tolerance: one box height) within a 600px horizontal window, a rule calibrated
against observed drift.</li>
<li><b>Describer vocabulary is anachronistic by design.</b> Image descriptions are written in
the describer's modern analytical register ("stylized," "showcasing," "sans-serif,"
"black-and-white") — appropriate for search, alien to the 1925 lexicon, so it dominates the
AI arm's residual suspect list. Planned: tag description-derived words per block in the
MiniOCR so corpus statistics can report them as a separate class rather than as suspects.</li>
<li><b>Small line-art can be misidentified by the describer</b> (an electric iron described
as a typewriter, a toaster as a phonograph); descriptions are labeled AI-generated in
provenance, not asserted as fact. Demo queries for such terms will surface these
misdescriptions — they are left in deliberately as an honest exhibit of failure modes.</li>
<li><b>The OCR VLM can substitute modern symbol vocabulary for period glyphs.</b> On one
November page, PaddleOCR-VL "transcribed" a weather table as a run of ~800 emoji (☀️🌧️),
characters that did not exist in 1925 and that Solr's analyzer rejects outright. The converter
now strips astral-plane characters; §11.5 documents the incident. Classical OCR fails toward
noise; VLMs fail toward <i>plausible modern semantics</i> — a categorically different QA problem.</li>
<li><b>The post-corrector currently treats describer text as OCR output.</b> Its lexicon test
flags modern description words as suspects and has "corrected" them (stylized→styled). The
fix — skipping image-description blocks entirely — is designed but not yet applied; the
corrected arm should be read with this caveat until it is.</li>
<li><b>Dehyphenation is deliberately incomplete.</b> The conservative gates (§11.2) leave
fragments wherever joining would risk manufacturing words: line-start hyphens, ALL-CAPS
classified text, and hyphen splits falling across VLM block boundaries.</li>
<li><b>Occasional block merge/reading-order glitches</b> on the densest ad pages (~2% of
regions).</li>
<li><b>Post-correction favors precision over recall by design:</b> it declines many fixable
errors to guarantee it never rewrites names.</li>
<li><b>The lexicon measure favors the baseline</b> (§12): "recognized" is membership in a
Tesseract-derived word list, so the metric understates the AI arm's true lead.</li>
</ul>

<h2>11. Case study: finding and fixing systematic errors at corpus scale</h2>
<p>When the year run completed (312 issues, 6,647 pages, zero quarantined pages), the first
full-corpus statistics exposed error families invisible at single-issue scale. This section
documents the diagnostic loop, because the loop itself is a result: <b>every defect below was
found by reading frequency-ranked suspect lists, fixed in the converter, and verified by
re-measuring — without re-running any model.</b> Raw VLM outputs are immutable; the MiniOCR
layer is derived. Re-deriving all 6,647 pages takes ~3 minutes of CPU.</p>

<h3>11.1 Table-markup leakage</h3>
<p>PaddleOCR-VL represents tables as HTML by design: a stock listing or exchange-rate table
arrives as <code>&lt;table&gt;&lt;tr&gt;&lt;td&gt;…</code> in <code>block_content</code>. The
converter passed these tags through as if they were words; XML-escaping turned
<code>&lt;td&gt;</code> into <code>&amp;lt;td&amp;gt;</code>, and Solr's tokenizer then stripped
the punctuation, leaving <code>tdtd</code>-family tokens in the index. Worse, adjacent cells
fused: the suspect <code>coaltdtdtdtdtdtrtrtdpremier</code> is the words "COAL" and "Premier"
from a mining-stock table with five cells of markup crushed between them. At year scale this
family accounted for tens of thousands of distinct garbage tokens
(<code>tdtd</code>&nbsp;1,937, <code>tabletrtd</code>-variants, hundreds of fused
column-header monsters).</p>
<p><b>Fix:</b> the converter replaces any <code>table/tr/td</code> tag with a space before
line assembly (tags-only blocks then drop out entirely), preserving cell contents as separate
searchable words. Table <i>text</i> — prices, sailing times, exchange rates — remains
indexed; only the structural markup is removed.</p>

<h3>11.2 Line-break hyphenation fragments</h3>
<p>In seven-column broadsheet setting, end-of-line hyphenation is constant, and the VLM
preserves it faithfully — emitting "six-⏎room" as <code>six-</code> plus <code>room</code>.
The result: <code>-room</code>, <code>-inch</code>, <code>-roomed</code> fragments were the
top AI-arm suspects (Tesseract shows the same family at roughly one-sixth the rate; it
reflows more text silently). The converter now joins across explicit line breaks under
conservative gates chosen for column-layout risk:</p>
<ul>
<li>joins occur <b>within a VLM layout block only</b> — never across blocks, where
column-missegmentation could marry unrelated fragments;</li>
<li>the left fragment must be letters ending in a hyphen and the continuation must begin
with a <b>lowercase</b> letter (capitalized continuations — proper-noun compounds, header
collisions — are refused);</li>
<li>the hyphen is <b>kept</b> in the joined form ("six-room", "to-morrow", "co-operative"):
if a gated join is nonetheless wrong, the damage is a hyphenated oddity, not an invented
fused word;</li>
<li>joining applies only to blocks whose line breaks the VLM stated explicitly, never to
lines the converter itself synthesized when reflowing.</li>
</ul>
<p>Residue is expected and accepted: hyphens the VLM placed at line <i>starts</i>, ALL-CAPS
classified text, and splits falling across block boundaries remain as fragments. One honest
measurement artifact: the <code>-room</code> <i>signature count</i> in the suspect table rose
after this fix, because thousands of newly joined compounds ("six-room", "ten-roomed") are
real words the Tesseract-built lexicon has never attested — the fragments fell, but the
signature aggregates everything matching <code>*-room</code>.</p>

<h3>11.3 Em-dash fusions</h3>
<p>Tabular listings of the form "France—Demand 4.66" (foreign exchange), "Makura—Mails
close…" (shipping), and "WANTED—MALE HELP" (classified headers) fused around the em-dash,
producing the <code>francedemand</code> / <code>*mails</code> / <code>wantedmale</code>
families. The converter now normalizes em- and en-dashes to spaces. The same families
persist in the <b>Tesseract</b> column (<code>francedemand</code>&nbsp;33,
<code>swedendemand</code>&nbsp;36) because only the VLM converter was fixed — a natural
control demonstrating the mechanism.</p>

<h3>11.4 Measured effect</h3>
<p>AI-pipeline column across the three remediation steps, full year, same 6,647 pages:</p>
<table style="border-collapse:collapse;margin:.8em 0">
<tr><th style="text-align:left;padding:2px 14px 2px 0">metric</th>
<th style="text-align:right;padding:2px 14px">before</th>
<th style="text-align:right;padding:2px 14px">+ table fix</th>
<th style="text-align:right;padding:2px 14px">+ dehyph &amp; dashes</th>
<th style="text-align:right;padding:2px 0">Tesseract (ref)</th></tr>
<tr><td>total words</td><td style="text-align:right">26,073,872</td>
<td style="text-align:right">26,614,653</td><td style="text-align:right">26,776,734</td>
<td style="text-align:right">19,602,675</td></tr>
<tr><td>unique words</td><td style="text-align:right">351,571</td>
<td style="text-align:right">302,571</td><td style="text-align:right"><b>256,718</b></td>
<td style="text-align:right">747,995</td></tr>
<tr><td>recognized vocabulary</td><td style="text-align:right">96.04%</td>
<td style="text-align:right">96.76%</td><td style="text-align:right"><b>97.41%</b></td>
<td style="text-align:right">92.76%</td></tr>
<tr><td>suspect tokens</td><td style="text-align:right">615,617</td>
<td style="text-align:right">504,514</td><td style="text-align:right"><b>405,923</b></td>
<td style="text-align:right">694,942</td></tr>
<tr><td>vocabulary only in this arm</td><td style="text-align:right">217,086</td>
<td style="text-align:right">166,560</td><td style="text-align:right"><b>130,545</b></td>
<td style="text-align:right">621,822</td></tr>
</table>
<p>Total words <i>rise</i> across the fixes (fused tokens split into their real constituent
words; tags-only blocks no longer crash pages), while unique words fall 27% — the signature
of noise removal rather than content loss. The recognized-vocabulary lead (97.41% vs 92.76%)
is achieved against a lexicon built from Tesseract's own output.</p>

<h3>11.5 A user search finds what corpus statistics cannot</h3>
<p>After the remediation above, a single user search ("arrowsmith") exposed two further
systemic issues at once. The AI arm <i>matched</i> the page in Solr but the viewer showed
no results — match without highlight. Diagnosis: (1) the remediated MiniOCR files had been
swapped in <b>without re-posting the Solr documents</b>; matching runs on index-time tokens
while highlighting re-reads the file, so where content had shifted (dehyphenation, quote
handling) the highlighter could no longer map matches back and silently returned nothing —
counts right, viewers empty. The rule adopted: any MiniOCR regeneration is followed by a
full document re-post (&lt;1 min for the year). (2) The re-index itself then failed on one
page: PaddleOCR-VL had transcribed a weather table as ~800 modern <b>emoji</b> (sun and
rain-cloud glyphs with variation selectors) — astral-plane Unicode that Solr rejects as
unpaired surrogates. That page had therefore never indexed successfully, contributing
<i>nothing</i> to the suspect lists: frequency-ranked QA is blind to pages that vanish
entirely. The converter now strips characters above U+FFFF.</p>
<p>Both defects were invisible to corpus statistics and caught only by a person searching
for one book title — an argument for keeping human retrieval testing in the QA loop
alongside corpus-scale measurement.</p>
<h2>12. Measuring the arms fairly</h2>
<p>Several deliberate choices keep the comparison honest, all of which slightly disadvantage
the AI arm:</p>
<ul>
<li><b>The lexicon is Tesseract-built.</b> "Recognized" means attested ≥3× in a lexicon
derived from the year's Tesseract text (713,822 distinct words). Vocabulary only the VLM
reads correctly is by construction "unrecognized"; vocabulary Tesseract systematically
misreads is enshrined. The AI arm leads on this metric despite the handicap.</li>
<li><b>Unique-word counts are read as lower-is-better.</b> At 19–27M running words of
newspaper English, genuine vocabulary saturates; marginal unique "words" are overwhelmingly
OCR noise. Tesseract's 748K unique forms against the AI arm's 257K is the single clearest
corpus-scale quality signal.</li>
<li><b>Image descriptions are counted, and flagged as such.</b> The AI arm's word totals
include Qwen-generated descriptions of photographs and advertisements — they are searchable
text and counting them is the point — but describer vocabulary ("stylized," "showcasing,"
"sans-serif") is modern analytical English that the 1925 lexicon rightly refuses, so it
surfaces in the suspect lists. These are features scored as defects; §10 discusses the
planned per-block tagging that will let statistics report description text separately.</li>
<li><b>Tesseract's own systematic errors are left untouched.</b> The same em-dash fusions
fixed in the VLM converter persist in the Tesseract column, and its suspect list
(<code>i'he</code> 8,668, <code>lhe</code> 4,680, <code>vou</code> 2,728…) is presented
unedited — the baseline is the format libraries actually receive from standard tooling.</li>
</ul>

</div>
"""

@app.route("/about")
def about():
    return f"<html><head><title>How this was built</title><style>{CSS}"""\
        """.doc h2{font-weight:normal;font-size:1.25em;margin-top:1.8em;border-bottom:1px solid #eee;padding-bottom:.25em}
        .doc h3{font-weight:600;font-size:1.02em;margin-top:1.3em}
        .doc li{margin:.35em 0;line-height:1.55}
        .doc p{line-height:1.6;max-width:52em}
        .doc pre{background:#f7f7f5;border:1px solid #e5e5e5;padding:1em;font-size:.85em;overflow-x:auto;line-height:1.45}
        .doc code{background:#f4f4f2;padding:0 .25em;font-size:.92em}</style></head>""" \
        f"<body>{ABOUT_HTML}</body></html>"



def _diff_boxes(arm, issue, q):
    """Query one arm, return {page: [ (x,y,w,h,text), ... ]} in page pixels."""
    src_f = "paddleocr-vl" if arm == "vlm" else "tesseract"
    r = requests.get(SOLR, params={
        "q": solr_q(q) + f' AND source:{src_f} AND page_id:{issue}_p*',
        "hl": "on", "hl.ocr.fl": "ocr_text", "hl.snippets": "50", "rows": "50",
        "fl": "id,page_id", "hl.ocr.absoluteHighlights": "true"}).json()
    page_of = {d["id"]: d["page_id"] for d in r["response"]["docs"]}
    out = {}
    for doc_id, hl in r.get("ocrHighlighting", {}).items():
        page = page_of.get(doc_id, "")
        for snip in hl.get("ocr_text", {}).get("snippets", []):
            for region in snip.get("highlights", [[]]):
                for h in region:
                    x, y = h["ulx"], h["uly"]
                    out.setdefault(page, []).append(
                        (x, y, h["lrx"] - x, h["lry"] - y, h.get("text", q)))
    return out

def _diff_match(tess, vlm):
    """Greedy center-distance match. Returns (both, tess_only, vlm_only)."""
    both, used = [], set()
    for tb in tess:
        tcx, tcy = tb[0] + tb[2] / 2, tb[1] + tb[3] / 2
        thresh = max(tb[3], 20)  # one tess word-height, floor 20px
        best, best_d = None, None
        for i, vb in enumerate(vlm):
            if i in used: continue
            vcx, vcy = vb[0] + vb[2] / 2, vb[1] + vb[3] / 2
            dy_ok = abs(tcy - vcy) <= max(tb[3], vb[3])
            dx = abs(tcx - vcx)
            d = dx
            if dy_ok and dx <= 600 and (best_d is None or d < best_d):
                best, best_d = i, d
        if best is not None:
            used.add(best); both.append(tb)
    t_only = [tb for tb in tess if tb not in both]
    v_only = [vb for i, vb in enumerate(vlm) if i not in used]
    return both, t_only, v_only

@app.route("/diff/<issue>")
def diff(issue):
    q = request.args.get("q", "").strip()
    if not q:
        return "<p>Add ?q=searchterm</p>"
    tboxes = _diff_boxes("tess", issue, q)
    vboxes = _diff_boxes("vlm", issue, q)
    pages = sorted(set(tboxes) | set(vboxes))
    if not pages:
        return f"<p>No hits in {issue} for <b>{q}</b>. <a href='/view/{issue}?q={q}'>Compare panels</a></p>"
    page = request.args.get("page") or pages[0]
    w, h = page_dims(issue, page)
    both, t_only, v_only = _diff_match(tboxes.get(page, []), vboxes.get(page, []))
    ib = it = iv = 0
    for p in pages:
        b, t, v = _diff_match(tboxes.get(p, []), vboxes.get(p, []))
        ib += len(b); it += len(t); iv += len(v)
    e = ISSUE_DATES.get(issue, {})
    d = e.get("date")
    head = (f"{e.get('weekday') or ''}, {MONTH_NAMES[int(d[5:7]) - 1]} {int(d[8:10])}, {d[:4]}"
            if d else issue)
    disp_w = 1200
    img = f"{IIIF_IMG}/{issue}%2F{page}.png/full/2500,/0/default.jpg"
    def rects(boxes, cls):
        return "".join(f'<rect class="{cls}" x="{b[0]}" y="{b[1]}" '
                       f'width="{b[2]}" height="{b[3]}"><title>{b[4]}</title></rect>'
                       for b in boxes)
    nav = " · ".join(f'<b>{p.split("_p")[-1]}</b>' if p == page else
                     f'<a href="/diff/{issue}?q={q}&page={p}">{p.split("_p")[-1]}</a>'
                     for p in pages)
    return f"""<!DOCTYPE html><html><head><title>{head} \u2014 overlay</title><style>
body{{font-family:-apple-system,Helvetica,sans-serif;margin:16px}}
#vp{{width:{disp_w}px;height:82vh;overflow:hidden;border:1px solid #ccc;cursor:grab;background:#f4f4f2}}
.wrap{{position:relative;width:{disp_w}px;transform-origin:0 0}}
.wrap img{{width:100%;display:block}}
.wrap svg{{position:absolute;top:0;left:0;width:100%;height:100%}}
rect{{fill-opacity:.25;stroke-width:3}}
rect.both{{fill:#2f7d4e;stroke:#2f7d4e}} rect.tess{{fill:#35619e;stroke:#35619e}}
rect.vlm{{fill:#a06f14;stroke:#a06f14}}
.legend span{{padding:2px 10px;margin-right:8px;border-radius:999px;font-size:12px;font-weight:600}}
.legend .lb{{background:#e6f2ea;color:#2f7d4e;border:1px solid #2f7d4e}}
.legend .lt{{background:#e9eff8;color:#35619e;border:1px solid #35619e}}
.legend .lv{{background:#f7efdc;color:#a06f14;border:1px solid #a06f14}}
</style></head><body>
<p><a href="/">Home</a> · <span style="font-family:Georgia,serif;font-size:17px;color:#191919">{head}</span>
<span style="color:#a09d95;font-size:11px">{issue}</span> ·
<a href="/view/{issue}?q={q}">Compare panels</a> — query <b>{q}</b></p>
<p class="legend"><span class="lb">both {len(both)}</span>
<span class="lt">Tesseract only {len(t_only)}</span>
<span class="lv">AI only {len(v_only)}</span></p>
<p class="meta">Whole issue ({len(pages)} pages with hits): both {ib} · Tesseract only {it} · AI only {iv}</p>
<p>Pages with hits: {nav}</p>
<div id="vp"><div class="wrap" id="zw"><img src="{img}">
<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none">
{rects(both,"both")}{rects(t_only,"tess")}{rects(v_only,"vlm")}</svg></div></div>
<p class="meta">Scroll to zoom · drag to pan · double-click to reset</p>
<script>
var s=1,tx=0,ty=0,zw=document.getElementById('zw'),vp=document.getElementById('vp');
function apply(){{zw.style.transform='translate('+tx+'px,'+ty+'px) scale('+s+')';}}
vp.addEventListener('wheel',function(e){{e.preventDefault();
 var r=vp.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;
 var f=e.deltaY<0?1.2:1/1.2,ns=Math.min(Math.max(s*f,1),12);
 tx=mx-(mx-tx)*(ns/s);ty=my-(my-ty)*(ns/s);s=ns;apply();}},{{passive:false}});
var dr=false,lx=0,ly=0;
vp.addEventListener('mousedown',function(e){{dr=true;lx=e.clientX;ly=e.clientY;e.preventDefault();}});
window.addEventListener('mousemove',function(e){{if(!dr)return;tx+=e.clientX-lx;ty+=e.clientY-ly;lx=e.clientX;ly=e.clientY;apply();}});
window.addEventListener('mouseup',function(){{dr=false;}});
vp.addEventListener('dblclick',function(){{s=1;tx=0;ty=0;apply();}});
</script>
</body></html>"""

app.run(host="0.0.0.0", port=8888)
