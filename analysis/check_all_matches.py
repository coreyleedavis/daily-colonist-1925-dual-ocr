import json, glob, os, re, csv

def find_described(page_id):
    issue = page_id.rsplit("_p", 1)[0]
    path = os.path.expanduser(f"~/paddle-year/{issue}/{page_id}_described.json")
    return path if os.path.exists(path) else None

def load_battery_meta():
    meta = {}
    with open("query_battery_results.csv") as f:
        for row in csv.DictReader(f):
            meta[row["query"]] = row
    return meta

def main():
    meta = load_battery_meta()
    files = sorted(glob.glob("battery_diffs_*.json"))
    results = []
    for fname in files:
        # recover the query text from the CSV by matching ai_only counts (safer than re-parsing filename)
        d = json.load(open(fname))
        ai_only = d["ai_only"]
        if not ai_only:
            continue
        # find matching query by ai_only count from CSV
        query = None
        for q, row in meta.items():
            if int(row["ai_only"]) == len(ai_only):
                query = q
                break
        term_guess = fname.replace("battery_diffs_", "").replace(".json", "")
        pattern_str = term_guess.split("_", 1)[-1].replace("_", r"\s*")
        try:
            pattern = re.compile(pattern_str, re.I)
        except re.error:
            continue

        image_hits, text_hits, other_hits, missing = 0, 0, 0, 0
        for page_id in ai_only:
            path = find_described(page_id)
            if not path:
                missing += 1
                continue
            data = json.load(open(path))
            matched_this_page = False
            for block in data.get("parsing_res_list", []):
                content = block.get("block_content", "") or ""
                if pattern.search(content):
                    matched_this_page = True
                    bl = block.get("block_label", "UNKNOWN")
                    if bl == "image":
                        image_hits += 1
                    elif bl in ("text", "paragraph_title", "doc_title"):
                        text_hits += 1
                    else:
                        other_hits += 1
            if not matched_this_page:
                other_hits += 1  # term matched via tokenization we didn't reconstruct; count separately

        total = image_hits + text_hits + other_hits
        pct_image = round(100 * image_hits / total, 1) if total else 0.0
        results.append({
            "file": fname, "ai_only_pages": len(ai_only), "block_matches_found": total,
            "text_blocks": text_hits, "image_blocks": image_hits, "other_unmatched": other_hits,
            "pct_image_block": pct_image
        })
        print(f"{fname:55} ai_only={len(ai_only):5} text={text_hits:5} image={image_hits:5} other={other_hits:5} pct_image={pct_image:5}%")

    with open("battery_block_label_summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["file","ai_only_pages","block_matches_found","text_blocks","image_blocks","other_unmatched","pct_image_block"])
        w.writeheader(); w.writerows(results)

if __name__ == "__main__":
    main()
