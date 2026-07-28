import json, os, re, csv

QUERIES = [
    ("person_place", "beecham", r"beecham"),
    ("person_place", "arrowsmith", r"arrowsmith"),
    ("place", "esquimalt", r"esquimalt"),
    ("place", "saanich", r"saanich"),
    ("place", "nanaimo", r"nanaimo"),
    ("place", "metchosin", r"metchosin"),
    ("common_word", "regatta", r"regatta"),
    ("common_word", "council", r"council"),
    ("common_word", "steamer", r"steamer"),
    ("common_word", "harbour", r"harbour"),
    ("booby_trap", "typewriter", r"typewriter"),
    ("booby_trap", "phonograph", r"phonograph"),
    ("booby_trap", "toaster", r"toaster"),
    ("phrase", '"city council"', r"city\s+council"),
    ("degraded_stress", "shanghai", r"shanghai"),
    ("degraded_stress", "church", r"church"),
    ("degraded_stress", "william", r"william"),
    ("degraded_stress", "association", r"association"),
]

def fname_for(category, query):
    fname = re.sub(r'[^a-zA-Z0-9]+', '_', query).strip('_')
    return f"battery_diffs_{category}_{fname}.json"

def ai_full_text(page_id):
    path = os.path.expanduser(f"~/solr-bridge/ocr-data/vlm/{page_id}.miniocr.xml")
    if not os.path.exists(path):
        return None
    content = open(path, encoding="utf-8").read()
    return " ".join(re.findall(r"<w[^>]*>([^<]*)</w>", content))

# loose match: term with any/no separators between letters, to catch hyphen/space tokenization diffs
def loose_pattern(term):
    letters = re.sub(r"[^a-z]", "", term.lower())
    return re.compile(r"[\s\-]*".join(list(letters)), re.I)

results = []
for category, query, exact_pattern_str in QUERIES:
    fname = fname_for(category, query)
    if not os.path.exists(fname):
        print(f"MISSING: {fname}")
        continue
    d = json.load(open(fname))
    tess_only = d.get("tess_only")
    if tess_only is None:
        continue
    if not tess_only:
        continue

    exact_pat = re.compile(exact_pattern_str, re.I)
    true_miss, found_differently, no_ai_file = 0, 0, 0
    for page_id in tess_only:
        text = ai_full_text(page_id)
        if text is None:
            no_ai_file += 1
            continue
        if exact_pat.search(text):
            # shouldn't happen given fq filtering, but sanity check
            found_differently += 1
        else:
            true_miss += 1

    results.append({"category": category, "query": query, "tess_only_pages": len(tess_only),
                     "true_miss": true_miss, "found_but_not_indexed_match": found_differently, "no_ai_file": no_ai_file})
    print(f"{category:16}{query:16} tess_only={len(tess_only):5} true_miss={true_miss:5} unexpected_match={found_differently:4} no_ai_file={no_ai_file:4}")

with open("tess_only_summary.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["category","query","tess_only_pages","true_miss","found_but_not_indexed_match","no_ai_file"])
    w.writeheader(); w.writerows(results)
