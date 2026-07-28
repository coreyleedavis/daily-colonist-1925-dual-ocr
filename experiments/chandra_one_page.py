#!/usr/bin/env python3
"""
chandra_one_page.py  --  run Chandra in layout mode on ONE page, dump the chunks
================================================================================
Purpose: see Chandra's real per-block output (bbox + label + text) for a single
Daily Colonist page, so we can design the JSON -> ALTO converter against actual
data instead of assumptions. This is the Chandra analogue of the single-issue
Tesseract smoke test.

It uses Chandra's own Python API exactly as its demo app does:
    InferenceManager(method=...).generate([BatchInputItem(image, "ocr_layout")])
and writes the resulting `chunks` and `page_box` to a JSON file.

Run with the Chandra env's python, e.g.:
    ~/chandra/chandra_env/bin/python chandra_one_page.py \
        --image /path/to/page.png --out ~/chandra_probe.json --method hf

Input options:
    --image PATH      a single page image (PNG/JPG). Easiest: reuse a PNG that
                      the Tesseract run already produced, or convert one JP2.
    --pdf PATH --page N
                      alternatively, pull page N (0-based) from a PDF via
                      Chandra's own loader (you already have dailycolonist PDFs
                      in ~/chandra/chandra_input).

Method:
    --method hf       load the model locally via HuggingFace (simplest; no server)
    --method vllm     use a running vLLM server (faster, needs the server up)
"""

import argparse
import json
import sys
from dataclasses import asdict


def load_image(args):
    from PIL import Image
    if args.image:
        return Image.open(args.image).convert("RGB")
    if args.pdf:
        # Use Chandra's own PDF loader so rendering matches its pipeline.
        from chandra.input import load_pdf_images
        imgs = load_pdf_images(args.pdf, [args.page])
        return imgs[0]
    sys.exit("Provide --image PATH, or --pdf PATH --page N")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", help="Single page image (PNG/JPG)")
    ap.add_argument("--pdf", help="PDF to pull a page from (alternative to --image)")
    ap.add_argument("--page", type=int, default=0, help="0-based page index for --pdf")
    ap.add_argument("--out", default="chandra_probe.json", help="Output JSON path")
    ap.add_argument("--method", choices=["hf", "vllm"], default="hf",
                    help="hf = local model in memory; vllm = running vLLM server")
    args = ap.parse_args()

    from chandra.model import InferenceManager
    from chandra.model.schema import BatchInputItem

    print(f"Loading image...")
    img = load_image(args)
    print(f"  page size: {img.width} x {img.height} px")

    print(f"Loading Chandra (method={args.method})... this can take a minute the first time")
    manager = InferenceManager(method=args.method)

    print("Running ocr_layout on the page...")
    batch = [BatchInputItem(image=img, prompt_type="ocr_layout")]
    out = manager.generate(batch)[0]

    if out.error:
        print("WARNING: Chandra reported an error flag on this page.")

    chunks = out.chunks
    # chunks may be a list of dicts already (from parse_chunks -> asdict); normalize
    if chunks and not isinstance(chunks, (list, tuple)):
        try:
            chunks = [asdict(c) for c in chunks]
        except Exception:
            pass

    payload = {
        "page_box": out.page_box,
        "token_count": out.token_count,
        "num_chunks": len(chunks) if chunks else 0,
        "chunks": chunks,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # ---- human-readable summary so we can eyeball the structure ----
    print("\n==== PROBE SUMMARY ====")
    print(f"page_box     : {out.page_box}")
    print(f"token_count  : {out.token_count}")
    print(f"num_chunks   : {payload['num_chunks']}")
    if chunks:
        first = chunks[0]
        print(f"chunk keys   : {list(first.keys())}")
        # label distribution
        labels = {}
        for c in chunks:
            labels[c.get('label', '?')] = labels.get(c.get('label', '?'), 0) + 1
        print(f"label counts : {labels}")
        print("\n-- first 3 chunks (content truncated) --")
        for c in chunks[:3]:
            content = str(c.get("content", ""))
            content = (content[:120] + "...") if len(content) > 120 else content
            print(f"  bbox={c.get('bbox')}  label={c.get('label')!r}")
            print(f"    content: {content!r}")
    print(f"\nFull JSON written to: {args.out}")


if __name__ == "__main__":
    main()
