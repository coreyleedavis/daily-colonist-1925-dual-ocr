import json, os, re, csv

QUERIES = [
    ("person_place", "beecham", "beecham"),
    ("person_place", "arrowsmith", "arrowsmith"),
    ("place", "esquimalt", "esquimalt"),
    ("place", "saanich", "saanich"),
    ("place", "nanaimo", "nanaimo"),
    ("place", "metchosin", "metchosin"),
    ("common_word", "regatta", "regatta"),
    ("common_word", "council", "council"),
    ("common_word", "steamer", "steamer"),
    ("common_word", "harbour", "harbour"),
    ("booby_trap", "typewriter", "typewriter"),
    ("booby_trap", "phonograph", "phonograph"),
    ("booby_trap", "toaster", "toaster"),
    ("degraded_stress", "shanghai", "shanghai"),
    ("degraded_stress", "church", "church"),
    ("degraded_stress", "william", "william"),
    ("degraded_stress", "association", "association"),
]
# "city council" phrase excluded here — needs two-token exact adjacency, handled separately if needed

def fname_for(category, query):
    fname = re.sub(r'[^a-zA-Z0-9]+', '_', query).strip('_')
    return f"battery_diffs_{category}_{fname}.json"

def ai_tokens(page_id):
    path = os.path.expanduser(f"~/solr-bridge/ocr-data/vlm/{page_id}.miniocr.xml")
    if not os.path.exists(path):
        return None
    content = open(path, encoding="utf-8").read()
    words = re.findall(r"<w[^>]*>([^<]*)</w>", content)
    # strip trailing punctuation for exact-token comparison, same rough idea as Solr's tokenizer
    return set(re.sub(r"[^a-z']", "", w.lower()) for w in words)

results = []
for category, query, exact_term in QUERIES:
    fname = fname_for(category, query)
    if not os.path.exists(fname):
        print(f"MISSING: {fname}")
        continue
    d = json.load(open(fname))
    tess_only = d.get("tess_only")
    if not tess_only:
        continue

    true_miss, exact_present_not_indexed, no_ai_file = 0, 0, 0
    for page_id in tess_only:
        toks = ai_tokens(page_id)
        if toks is None:
            no_ai_file += 1
            continue
        if exact_term.lower() in toks:
            exact_present_not_indexed += 1
        else:
            true_miss += 1

    results.append({"category": category, "query": query, "tess_only_pages": len(tess_only),
                     "true_miss": true_miss, "exact_word_present_but_solr_missed": exact_present_not_indexed,
                     "no_ai_file": no_ai_file})
    print(f"{category:16}{query:16} tess_only={len(tess_only):5} true_miss={true_miss:5} exact_present_but_solr_missed={exact_present_not_indexed:4} no_ai_file={no_ai_file:4}")

with open("tess_only_summary_v2.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["category","query","tess_only_pages","true_miss","exact_word_present_but_solr_missed","no_ai_file"])
    w.writeheader(); w.writerows(results)
