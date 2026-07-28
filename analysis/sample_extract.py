import json, os, re, csv
import xml.etree.ElementTree as ET

SAMPLE = json.load(open("random_sample_pages.json"))

# load lexicon: word -> count, attested >=3x counts as "recognized"
lexicon = set()
with open(os.path.expanduser("~/solr-bridge/lexicon_1925.tsv")) as f:
    for line in f:
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            continue
        word, count = parts
        try:
            if int(count) >= 3:
                lexicon.add(word.lower())
        except ValueError:
            continue
print(f"lexicon loaded: {len(lexicon)} words attested >=3x")

WORD_RE = re.compile(r"[a-z']+")

def tess_words(page_id):
    issue = page_id.rsplit("_p", 1)[0]
    path = os.path.expanduser(f"~/tess5-1925-full/{issue}/{page_id}.xml")
    if not os.path.exists(path):
        return None
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return None
    ns = {"a": "http://www.loc.gov/standards/alto/ns-v3#"}
    text = []
    root = tree.getroot()
    # namespace-agnostic: strip namespace from tags
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "String":
            c = elem.get("CONTENT")
            if c:
                text.append(c)
    return " ".join(text)

def ai_words_from_miniocr(page_id):
    path = os.path.expanduser(f"~/solr-bridge/ocr-data/vlm/{page_id}.miniocr.xml")
    if not os.path.exists(path):
        return None
    content = open(path, encoding="utf-8").read()
    return " ".join(re.findall(r"<w[^>]*>([^<]*)</w>", content))

def block_label_breakdown(page_id):
    issue = page_id.rsplit("_p", 1)[0]
    path = os.path.expanduser(f"~/paddle-year/{issue}/{page_id}_described.json")
    if not os.path.exists(path):
        return {}
    data = json.load(open(path))
    counts = {}
    for block in data.get("parsing_res_list", []):
        bl = block.get("block_label", "UNKNOWN")
        counts[bl] = counts.get(bl, 0) + 1
    return counts

def analyze(text):
    words = WORD_RE.findall(text.lower())
    total = len(words)
    suspect = sum(1 for w in words if w not in lexicon)
    return total, suspect

rows = []
missing_tess, missing_ai = 0, 0
for page_id in SAMPLE:
    tess_text = tess_words(page_id)
    ai_text = ai_words_from_miniocr(page_id)
    if tess_text is None:
        missing_tess += 1
    if ai_text is None:
        missing_ai += 1
    t_total, t_suspect = analyze(tess_text or "")
    a_total, a_suspect = analyze(ai_text or "")
    labels = block_label_breakdown(page_id)
    rows.append({
        "page_id": page_id,
        "tess_words": t_total, "tess_suspect": t_suspect,
        "tess_suspect_pct": round(100*t_suspect/t_total, 1) if t_total else None,
        "ai_words": a_total, "ai_suspect": a_suspect,
        "ai_suspect_pct": round(100*a_suspect/a_total, 1) if a_total else None,
        "ai_image_blocks": labels.get("image", 0),
        "ai_text_blocks": labels.get("text", 0) + labels.get("paragraph_title", 0) + labels.get("doc_title", 0),
        "ai_other_blocks": sum(v for k, v in labels.items() if k not in ("image", "text", "paragraph_title", "doc_title")),
    })

print(f"missing tesseract ALTO: {missing_tess}, missing AI miniocr: {missing_ai}")

with open("random_sample_stats.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader(); w.writerows(rows)

# quick aggregate summary
import statistics
tw = [r["tess_words"] for r in rows]
aw = [r["ai_words"] for r in rows]
tsp = [r["tess_suspect_pct"] for r in rows if r["tess_suspect_pct"] is not None]
asp = [r["ai_suspect_pct"] for r in rows if r["ai_suspect_pct"] is not None]
print(f"\nAVERAGES across {len(rows)} sampled pages:")
print(f"  tess words/page: {statistics.mean(tw):.0f}   ai words/page: {statistics.mean(aw):.0f}")
print(f"  tess suspect%: {statistics.mean(tsp):.1f}   ai suspect%: {statistics.mean(asp):.1f}")
print(f"  tess suspect% median: {statistics.median(tsp):.1f}   ai suspect% median: {statistics.median(asp):.1f}")
