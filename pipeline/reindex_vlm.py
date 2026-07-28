#!/usr/bin/env python3
"""Re-post all VLM MiniOCR docs to Solr so index tokens match the current files."""
import glob, json, os, requests

SOLR_UPDATE = "http://localhost:8983/solr/colonist/update"
files = sorted(glob.glob(os.path.expanduser("~/solr-bridge/ocr-data/vlm/*.miniocr.xml")))
print(f"{len(files)} docs to post")
batch, posted = [], 0
for f in files:
    page = os.path.basename(f).replace(".miniocr.xml", "")
    batch.append({"id": f"{page}_paddle", "page_id": page,
                  "source": "paddleocr-vl",
                  "ocr_text": f"/ocr-data/vlm/{page}.miniocr.xml"})
    if len(batch) == 500:
        r = requests.post(SOLR_UPDATE, json=batch); r.raise_for_status()
        posted += len(batch); batch = []
        print(f"{posted}/{len(files)}", flush=True)
if batch:
    r = requests.post(SOLR_UPDATE, json=batch); r.raise_for_status()
    posted += len(batch)
r = requests.post(SOLR_UPDATE + "?commit=true", json=[]); r.raise_for_status()
print(f"DONE: {posted} posted and committed")
