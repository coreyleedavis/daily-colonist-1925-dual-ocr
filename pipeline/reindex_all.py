#!/usr/bin/env python3
"""Re-post all docs (both arms) so index tokens reflect the current text_ocr schema."""
import glob, json, os, requests

DATES = {}
try:
    DATES = json.load(open(os.path.expanduser("~/solr-bridge/.issue_dates.json")))
except Exception:
    pass

def issue_date(page):
    d = DATES.get(page.rsplit("_p", 1)[0], {}).get("date")
    return f"{d}T00:00:00Z" if d else None

SOLR_UPDATE = "http://localhost:8983/solr/colonist/update"

def post(batch):
    r = requests.post(SOLR_UPDATE, json=batch)
    if r.status_code != 200:
        print("ERROR:", r.text[:400]); r.raise_for_status()

docs = []
for f in sorted(glob.glob(os.path.expanduser("~/solr-bridge/ocr-data/vlm/*.miniocr.xml"))):
    page = os.path.basename(f).replace(".miniocr.xml", "")
    doc = {"id": f"{page}_paddle", "page_id": page, "source": "paddleocr-vl",
           "ocr_text": f"/ocr-data/vlm/{page}.miniocr.xml"}
    if issue_date(page): doc["issue_date"] = issue_date(page)
    docs.append(doc)
for f in sorted(glob.glob(os.path.expanduser("~/tess5-1925-full/*/*.xml"))):
    page = os.path.basename(f).replace(".xml", "")
    issue = page.rsplit("_p", 1)[0]
    doc = {"id": f"{page}_tess", "page_id": page, "source": "tesseract",
           "ocr_text": f"/alto-year/{issue}/{page}.xml"}
    if issue_date(page): doc["issue_date"] = issue_date(page)
    docs.append(doc)
print(f"{len(docs)} docs to post")
for i in range(0, len(docs), 500):
    post(docs[i:i+500])
    print(f"{min(i+500, len(docs))}/{len(docs)}", flush=True)
requests.post(SOLR_UPDATE + "?commit=true", json=[]).raise_for_status()
print("DONE, committed")
