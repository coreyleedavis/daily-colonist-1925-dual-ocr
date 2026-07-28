import json, os, re, csv

# same list as query_battery.py, category + query + the regex to use for content matching
QUERIES = [
    ("person_place", "beecham", r"beecham"),
    ("person_place", "arrowsmith", r"arrowsmith"),
    ("person_place", "plimleys", r"plimley"),
    ("person_place", "cathcarts", r"cathcart"),
    ("person_place", "stanfields", r"stanfield"),
    ("person_place", "pinkhams", r"pinkham"),
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
    ("phrase", '"daily colonist"', r"daily\s+colonist"),
    ("phrase", '"city council"', r"city\s+council"),
    ("degraded_stress", "shanghai", r"shanghai"),
    ("degraded_stress", "church", r"church"),
    ("degraded_stress", "william", r"william"),
    ("degraded_stress", "association", r"association"),
]

def fname_for(category, query):
    fname = re.sub(r'[^a-zA-Z0-9]+', '_', query).strip('_')
    return f"battery_diffs_{category}_{fname}.json"

def find_described(page_id):
    issue = page_id.rsplit("_p", 1)[0]
    path = os.path.expanduser(f"~/paddle-year/{issue}/{page_id}_described.json")
    return path if os.path.exists(path) else None

def main():
    results = []
    for category, query, pattern_str in QUERIES:
        fname = fname_for(category, query)
        if not os.path.exists(fname):
            print(f"MISSING FILE: {fname}")
            continue
        d = json.load(open(fname))
        ai_only = d["ai_only"]
        if not ai_only:
            continue
        pattern = re.compile(pattern_str, re.I)

        image_hits, text_hits, other_label_hits, no_block_match = 0, 0, 0, 0
        for page_id in ai_only:
            path = find_described(page_id)
            if not path:
                no_block_match += 1
                continue
            data = json.load(open(path))
            page_had_match = False
            for block in data.get("parsing_res_list", []):
                content = block.get("block_content", "") or ""
                if pattern.search(content):
                    page_had_match = True
                    bl = block.get("block_label", "UNKNOWN")
                    if bl == "image":
                        image_hits += 1
                    elif bl in ("text", "paragraph_title", "doc_title"):
                        text_hits += 1
                    else:
                        other_label_hits += 1
            if not page_had_match:
                no_block_match += 1

        total = image_hits + text_hits + other_label_hits
        pct_image = round(100 * image_hits / total, 1) if total else None
        row = {"category": category, "query": query, "ai_only_pages": len(ai_only),
               "text_blocks": text_hits, "image_blocks": image_hits, "other_label": other_label_hits,
               "no_block_match": no_block_match, "pct_image_block": pct_image}
        results.append(row)
        pct_str = f"{pct_image}%" if pct_image is not None else "n/a"
        print(f"{category:16}{query:16} ai_only={len(ai_only):5} text={text_hits:5} image={image_hits:5} other_label={other_label_hits:4} no_match={no_block_match:5} pct_image={pct_str}")

    with open("battery_block_label_summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["category","query","ai_only_pages","text_blocks","image_blocks","other_label","no_block_match","pct_image_block"])
        w.writeheader(); w.writerows(results)

if __name__ == "__main__":
    main()
