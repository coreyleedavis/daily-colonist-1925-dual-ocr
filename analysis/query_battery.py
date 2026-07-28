import urllib.request, urllib.parse, json, csv, re

SOLR = "http://localhost:8983/solr/colonist/select"

QUERIES = [
    ("person_place", "beecham", "prior known match, both arms"),
    ("person_place", "arrowsmith", "prior known match, both arms"),
    ("person_place", "plimleys", "real Victoria business, flagged AI-suspect"),
    ("person_place", "cathcarts", "possible real name, flagged AI-suspect"),
    ("person_place", "stanfields", "real Canadian brand, flagged AI-suspect"),
    ("person_place", "pinkhams", "real brand, flagged AI-suspect"),
    ("place", "esquimalt", "common local place name"),
    ("place", "saanich", "common local place name"),
    ("place", "nanaimo", "note: 'nanamo' typo in AI-suspect list"),
    ("place", "metchosin", "less common local place name"),
    ("common_word", "regatta", "prior-validated ~57pp summer 1925"),
    ("common_word", "council", "high-frequency common word"),
    ("common_word", "steamer", "high-frequency, period-appropriate"),
    ("common_word", "harbour", "high-frequency common word"),
    ("booby_trap", "typewriter", "known AI misdescription source"),
    ("booby_trap", "phonograph", "known AI misdescription source"),
    ("booby_trap", "toaster", "known AI misdescription source"),
    ("phrase", '"daily colonist"', "masthead phrase, should be near-universal"),
    ("phrase", '"city council"', "common two-word phrase"),
    ("degraded_stress", "shanghai", "garbled as 'bhanghal' in tess suspects"),
    ("degraded_stress", "church", "garbled as 'chureh' in tess suspects"),
    ("degraded_stress", "william", "garbled as 'willlam' in tess suspects"),
    ("degraded_stress", "association", "garbled as 'assoclation' in tess suspects"),
    ("negative_control", "zzqxplorf", "nonsense token, sanity check for over-matching"),
]

def solr_query(q, source, rows=7000):
    params = {"q": f"ocr_text:{q}", "fq": f"source:{source}", "rows": rows, "fl": "page_id", "wt": "json"}
    url = SOLR + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url) as resp:
        data = json.load(resp)
    docs = data["response"]["docs"]
    return data["response"]["numFound"], set(d["page_id"] for d in docs)

def main():
    results = []
    for category, q, note in QUERIES:
        tess_n, tess_ids = solr_query(q, "tesseract")
        ai_n, ai_ids = solr_query(q, "paddleocr-vl")
        overlap = tess_ids & ai_ids
        tess_only = tess_ids - ai_ids
        ai_only = ai_ids - tess_ids
        union = tess_ids | ai_ids
        jaccard = round(len(overlap) / len(union), 3) if union else 1.0
        results.append({"category": category, "query": q, "tess_numFound": tess_n, "ai_numFound": ai_n,
                         "overlap": len(overlap), "tess_only": len(tess_only), "ai_only": len(ai_only),
                         "jaccard": jaccard, "note": note})
        fname = re.sub(r'[^a-zA-Z0-9]+', '_', q).strip('_')
        with open(f"battery_diffs_{category}_{fname}.json", "w") as f:
            json.dump({"tess_only": sorted(tess_only), "ai_only": sorted(ai_only)}, f, indent=2)

    with open("query_battery_results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["category","query","tess_numFound","ai_numFound","overlap","tess_only","ai_only","jaccard","note"])
        w.writeheader(); w.writerows(results)

    print(f"{'category':<16}{'query':<20}{'tess':>7}{'ai':>7}{'overlap':>9}{'t-only':>8}{'ai-only':>8}{'jaccard':>9}")
    for r in results:
        print(f"{r['category']:<16}{r['query']:<20}{r['tess_numFound']:>7}{r['ai_numFound']:>7}{r['overlap']:>9}{r['tess_only']:>8}{r['ai_only']:>8}{r['jaccard']:>9}")

if __name__ == "__main__":
    main()
