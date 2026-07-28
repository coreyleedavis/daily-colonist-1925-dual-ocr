import glob, html, json, os, re, time
from collections import Counter

HOME = os.path.expanduser('~')
OUT = f"{HOME}/solr-bridge/corpus_stats.json"
lex = set()
for line in open(f"{HOME}/solr-bridge/lexicon_1925.tsv"):
    w, c = line.rstrip('\n').split('\t')
    if int(c) >= 3: lex.add(w)

def tally(word_iter):
    freq = Counter()
    total = 0
    for w in word_iter:
        total += 1
        w = html.unescape(html.unescape(w))
        a = re.sub(r"[^A-Za-z'-]", "", w).lower()
        if len(a) >= 2: freq[a] += 1
    checkable = {w for w in freq if len(w) >= 4}
    rec_tokens = sum(freq[w] for w in checkable if w in lex)
    chk_tokens = sum(freq[w] for w in checkable)
    suspects = Counter({w: freq[w] for w in checkable if w not in lex})
    return freq, {"total_words": total, "unique": len(freq),
                  "recognized_pct": round(100.0 * rec_tokens / max(1, chk_tokens), 2),
                  "suspect_distinct": len(suspects),
                  "suspect_tokens": sum(suspects.values())}, suspects

def alto_words(files, label):
    print(f"{label}: {len(files)} pages")
    for i, f in enumerate(files):
        if i % 500 == 0: print(" ", i, flush=True)
        for m in re.finditer(r'CONTENT="([^"]*)"',
                             open(f, encoding="utf-8", errors="ignore").read()):
            yield m.group(1)

def vlm_words(files):
    print(f"vlm: {len(files)} pages")
    for i, f in enumerate(files):
        if i % 500 == 0: print(" ", i, flush=True)
        for m in re.finditer(r"<w[^>]*>([^<]*)</w>",
                             open(f, encoding="utf-8", errors="ignore").read()):
            yield m.group(1)

t0 = time.time()

DONE = {os.path.basename(os.path.dirname(p))
        for p in glob.glob(f"{HOME}/paddle-year/*/.done")}
print(f"AI-processed issues (.done): {len(DONE)}")

tess_all = glob.glob(f"{HOME}/tess5-1925-full/*/*.xml")
tess_matched = [f for f in tess_all
                if os.path.basename(os.path.dirname(f)) in DONE]

# NOTE: ocr-data/*_desc.miniocr.xml (page-number-only names, no issue id)
# are orphans from an early single-issue test and are excluded here.
orphans = glob.glob(f"{HOME}/solr-bridge/ocr-data/*_desc.miniocr.xml")
if orphans:
    print(f"excluding {len(orphans)} orphan *_desc.miniocr.xml files from stats")
vlm_files = glob.glob(f"{HOME}/solr-bridge/ocr-data/vlm/*.miniocr.xml")

tf, tstats, tsusp = tally(alto_words(tess_all, "tesseract (all)"))
tstats["pages"] = len(tess_all)
tmf, tmstats, tmsusp = tally(alto_words(tess_matched, "tesseract (matched)"))
tmstats["pages"] = len(tess_matched)
tmstats["issues"] = len(DONE)
vf, vstats, vsusp = tally(vlm_words(vlm_files))
vstats["pages"] = len(vlm_files)

tset, vset = set(tf), set(vf)
out = {
  "generated": time.strftime("%Y-%m-%d %H:%M"),
  "elapsed_s": round(time.time() - t0),
  "tesseract": tstats, "tesseract_matched": tmstats, "vlm": vstats,
  "shared_unique": len(tset & vset),
  "only_tesseract": len(tset - vset), "only_vlm": len(vset - tset),
  "top_suspects_tesseract": tsusp.most_common(300),
  "top_suspects_vlm": vsusp.most_common(300),
  "top_only_vlm": sorted(((w, vf[w]) for w in (vset - tset)), key=lambda x: -x[1])[:300],
  "top_only_tesseract": sorted(((w, tf[w]) for w in (tset - vset)), key=lambda x: -x[1])[:300],
}
json.dump(out, open(OUT, 'w'))
print(f"wrote {OUT} in {out['elapsed_s']}s")
print(json.dumps({k: v for k, v in out.items() if not k.startswith('top_')}, indent=1))
