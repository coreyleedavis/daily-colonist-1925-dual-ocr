#!/usr/bin/env python3
"""Re-convert all VLM described JSONs to MiniOCR with the fixed converter.
Writes to ocr-data/vlm-clean; verify then swap with ocr-data/vlm."""
import glob, os, struct, subprocess, sys, time

HOME = os.path.expanduser("~")
OUT = f"{HOME}/solr-bridge/ocr-data/vlm-clean"
CONVERTER = f"{HOME}/solr-bridge/paddle_to_miniocr.py"
os.makedirs(OUT, exist_ok=True)

def png_width(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"not a PNG: {path}")
    return struct.unpack(">I", head[16:20])[0]

jsons = sorted(glob.glob(f"{HOME}/paddle-year/*/*_described.json"))
print(f"{len(jsons)} pages to convert", flush=True)
t0, ok, fail = time.time(), 0, 0
for i, dj in enumerate(jsons, 1):
    page = os.path.basename(dj).replace("_described.json", "")
    issue = page.rsplit("_p", 1)[0]
    png = f"{HOME}/colonist-images/{issue}/{page}.png"
    xml = f"{OUT}/{page}.miniocr.xml"
    try:
        scale = png_width(png) / 2560.0
        r = subprocess.run([sys.executable, CONVERTER, dj, page, xml, str(scale)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-200:])
        ok += 1
    except Exception as e:
        fail += 1
        print(f"FAIL {page}: {e}", flush=True)
    if i % 500 == 0:
        rate = i / (time.time() - t0)
        print(f"{i}/{len(jsons)} ({rate:.1f} p/s, ETA {(len(jsons)-i)/rate/60:.0f}m)", flush=True)
print(f"DONE: {ok} ok, {fail} failed in {(time.time()-t0)/60:.1f}m", flush=True)
