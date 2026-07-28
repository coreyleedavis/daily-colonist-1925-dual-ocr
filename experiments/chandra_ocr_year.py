#!/usr/bin/env python3
"""
chandra_ocr_year.py  --  run Chandra over a full year of IA newspaper issues
=============================================================================
The VLM counterpart to tesseract_ocr_year.py. Streams every page of every
*jp2.zip issue through a RUNNING Chandra vLLM server (started separately) and
writes one JSON per page containing the layout chunks (bbox + label + content)
plus page_box -- the data the JSON->ALTO converter will consume.

Prerequisites:
  * A Chandra vLLM server already running and healthy, e.g. in a tmux window:
      ~/chandra/chandra_env/bin/python -m vllm.entrypoints.openai.api_server \
          --model datalab-to/chandra --served-model-name chandra \
          --dtype bfloat16 --max-num-seqs 32 --max-model-len 32768 \
          --gpu-memory-utilization 0.9 --port 8000
    Confirm with: curl -s http://localhost:8000/v1/models   (should show "chandra")
  * Run THIS script with Chandra's env python:
      ~/chandra/chandra_env/bin/python chandra_ocr_year.py \
          --jp2-dir ~/dailycolonist-1925-jp2z --out-dir ~/chandra-1925-full

Design:
  * Issue-by-issue: unzip one issue, send its pages as one batch to the server
    (Chandra parallelizes the batch internally up to the server's max-num-seqs),
    write results, clean up, move on. Bounds disk and makes resume trivial.
  * Idempotent: a page whose .json already exists is skipped, so an interrupted
    run resumes where it left off -- just rerun the same command.
  * No energy measurement here (by design -- energy is measured later on a
    controlled subset for both engines). This run captures timing + throughput.
  * Concurrency is handled INSIDE Chandra's generate_vllm (do not add a pool).
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path


def detect_converter():
    for tool in ("opj_decompress", "vips", "magick", "convert"):
        if shutil.which(tool):
            return tool
    sys.exit("ERROR: no JP2 decoder found. Install libopenjp2-tools or libvips.")


def convert_jp2(src, dst_png, tool):
    if tool == "opj_decompress":
        cmd = ["opj_decompress", "-i", src, "-o", dst_png]
    elif tool == "vips":
        cmd = ["vips", "copy", src, dst_png]
    else:
        cmd = [tool, src, dst_png]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(dst_png):
        raise RuntimeError(f"JP2 convert failed ({tool}): {r.stderr.strip()[:200]}")


def check_server(api_base):
    """Confirm the vLLM server is up and serving the expected model."""
    import urllib.request
    url = api_base.rstrip("/").replace("/v1", "") + "/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.load(resp)
        ids = [m.get("id") for m in data.get("data", [])]
        print(f"Server OK at {url} -- serving: {ids}")
        return True
    except Exception as e:
        sys.exit(f"ERROR: cannot reach vLLM server at {url}: {e}\n"
                 f"Start it in a tmux window first (see header of this script).")


def iter_issue_zips(jp2_dir, limit=None):
    zips = sorted(p for p in Path(jp2_dir).expanduser().rglob("*jp2.zip"))
    return zips[:limit] if limit else zips


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jp2-dir", required=True, help="Dir of *jp2.zip issues")
    ap.add_argument("--out-dir", required=True, help="Output dir for per-page JSON")
    ap.add_argument("--limit", type=int, help="Process only first N issues (testing)")
    ap.add_argument("--keep-images", action="store_true", help="Keep converted PNGs")
    ap.add_argument("--prompt-type", default="ocr_layout",
                    help="Chandra prompt mode (ocr_layout gives bbox+label+content)")
    args = ap.parse_args()

    # Import Chandra and confirm the server before doing any work.
    from chandra.model import InferenceManager
    from chandra.model.schema import BatchInputItem
    from chandra.settings import settings
    check_server(settings.VLLM_API_BASE)

    conv_tool = detect_converter()
    print(f"JP2 decoder: {conv_tool}")
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    zips = iter_issue_zips(args.jp2_dir, args.limit)
    if not zips:
        sys.exit(f"No *jp2.zip under {args.jp2_dir}")
    print(f"Found {len(zips)} issue(s). prompt_type={args.prompt_type}")

    print("Connecting to Chandra (vLLM client)...")
    manager = InferenceManager(method="vllm")

    results = []
    wall0 = time.perf_counter()
    scratch = Path(tempfile.mkdtemp(prefix="cjp2_", dir=out_dir))

    try:
        for zi, zp in enumerate(zips, 1):
            issue = zp.name[:-len("_jp2.zip")] if zp.name.endswith("_jp2.zip") else zp.stem
            issue_out = out_dir / issue
            issue_out.mkdir(exist_ok=True)
            issue_scratch = scratch / issue
            issue_scratch.mkdir(exist_ok=True)

            try:
                with zipfile.ZipFile(zp) as zf:
                    zf.extractall(issue_scratch)
            except zipfile.BadZipFile:
                results.append({"issue": issue, "page": 0, "status": "bad_zip"})
                continue

            pages = sorted(issue_scratch.rglob("*.jp2"))

            # Build the batch, skipping pages already done (resume support).
            from PIL import Image
            batch, meta, pre_skipped = [], [], 0
            for i, jp2 in enumerate(pages, 1):
                out_json = issue_out / f"{issue}_p{i:03d}.json"
                if out_json.exists():
                    pre_skipped += 1
                    results.append({"issue": issue, "page": i, "status": "skipped"})
                    continue
                png = issue_scratch / f"p{i:03d}.png"
                try:
                    convert_jp2(str(jp2), str(png), conv_tool)
                    img = Image.open(png).convert("RGB")
                except Exception as e:
                    results.append({"issue": issue, "page": i,
                                    "status": "convert_error", "err": str(e)[:200]})
                    continue
                batch.append(BatchInputItem(image=img, prompt_type=args.prompt_type))
                meta.append({"page": i, "out_json": out_json, "png": png})

            t0 = time.perf_counter()
            if batch:
                outs = manager.generate(batch)  # Chandra parallelizes internally
            else:
                outs = []
            issue_s = time.perf_counter() - t0

            for m, out in zip(meta, outs):
                chunks = out.chunks
                payload = {
                    "issue": issue, "page": m["page"],
                    "page_box": out.page_box,
                    "token_count": out.token_count,
                    "num_chunks": len(chunks) if chunks else 0,
                    "chunks": chunks,
                }
                with open(m["out_json"], "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                results.append({
                    "issue": issue, "page": m["page"],
                    "status": "error" if out.error else "ok",
                    "num_chunks": payload["num_chunks"],
                    "token_count": out.token_count,
                    "issue_batch_s": round(issue_s, 2),
                })
                if not args.keep_images and m["png"].exists():
                    m["png"].unlink()

            shutil.rmtree(issue_scratch, ignore_errors=True)
            done = len([r for r in results if r.get("status") == "ok"])
            print(f"  [{zi}/{len(zips)}] {issue}: {len(batch)} new "
                  f"({pre_skipped} skipped) in {issue_s:.1f}s  | ok so far: {done}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    wall = time.perf_counter() - wall0
    _write_reports(out_dir, results, wall)


def _write_reports(out_dir, results, wall):
    csv_path = Path(out_dir) / "per_page.csv"
    cols = ["issue", "page", "status", "num_chunks", "token_count",
            "issue_batch_s", "err"]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(r)

    pages = [r for r in results if r.get("page")]
    ok = [r for r in pages if r["status"] == "ok"]
    n = len(pages)
    summary = {
        "issues": len({r["issue"] for r in pages}),
        "pages_total": n,
        "pages_ok": len(ok),
        "pages_skipped": len([r for r in pages if r["status"] == "skipped"]),
        "pages_failed": len([r for r in pages
                             if r["status"] not in ("ok", "skipped")]),
        "wall_clock_s": round(wall, 1),
        "throughput_pages_per_hour": round(len(ok) / wall * 3600, 1) if wall and ok else None,
        "mean_s_per_page": round(wall / len(ok), 3) if ok else None,
        "total_chunks": sum(r.get("num_chunks", 0) for r in ok),
        "total_tokens": sum(r.get("token_count", 0) for r in ok),
    }
    with open(Path(out_dir) / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print("\n==== RUN SUMMARY ====")
    print(json.dumps(summary, indent=2))
    print(f"\nPer-page detail: {csv_path}")


if __name__ == "__main__":
    main()
