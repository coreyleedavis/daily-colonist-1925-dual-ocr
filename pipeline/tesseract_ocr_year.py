#!/usr/bin/env python3
"""
tesseract_ocr_year.py
=====================
Measurement-valid Tesseract OCR pass over a year of Internet Archive newspaper
issues (e.g. the 1925 Daily Colonist), producing coordinate-bearing output
(ALTO XML + hOCR) and the cost numbers a procurement decision actually needs:
per-page wall-clock, throughput, and energy (CPU+RAM via RAPL, GPU via NVML).

Design decisions (see conversation for rationale):
  * ONE recognition pass per page. Tesseract runs OCR once and serializes to
    every requested format (alto/hocr/txt/tsv), so multi-format output is free.
  * PSM 3 = Tesseract's native full-page layout analysis (its column detector).
    This IS the baseline's "layout step." No separate column cropper.
  * OEM 1 = LSTM engine only.
  * JP2 -> PNG conversion is explicit (Leptonica often lacks JPEG2000 support).
  * Page-level parallelism across all cores, with OMP_THREAD_LIMIT=1 so each
    Tesseract worker is single-threaded -> clean, deploy-mode throughput.
  * The whole batch is wrapped in one CodeCarbon tracker. Run it ALONE on the
    box (RAPL is whole-socket) and capture an idle baseline (--measure-idle)
    to subtract.

Prereqs (you have root):
  sudo add-apt-repository -y ppa:alex-p/tesseract-ocr5
  sudo apt update && sudo apt install -y tesseract-ocr libopenjp2-tools
  pip install codecarbon
  # (optional, higher accuracy) swap in the 'best' English LSTM model:
  #   sudo wget -O <tessdata>/eng.traineddata \
  #     https://github.com/tesseract-ocr/tessdata_best/raw/main/eng.traineddata
  # RAPL read permission (else CPU energy reads as 0):
  #   sudo chmod -R a+r /sys/class/powercap/intel-rapl   (or run with sudo)

Usage:
  # smoke test on 1 issue, keep the PNGs so you can eyeball them:
  python tesseract_ocr_year.py --jp2-dir ~/dailycolonist-1925-jp2z \
      --out-dir ~/tess5-1925 --limit 1 --keep-images

  # full year, all cores:
  python tesseract_ocr_year.py --jp2-dir ~/dailycolonist-1925-jp2z \
      --out-dir ~/tess5-1925

  # idle baseline (run on an otherwise-quiet box, then subtract):
  python tesseract_ocr_year.py --measure-idle 120 --out-dir ~/tess5-1925
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
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# Each worker process: pin Tesseract/OpenMP to a single thread so we get
# page-level parallelism instead of threads fighting each other.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")

OCR_FORMATS = ["alto", "hocr", "txt", "tsv"]  # one recognition pass, four serializations


# --------------------------------------------------------------------------- #
# Environment checks
# --------------------------------------------------------------------------- #
def check_tesseract():
    try:
        out = subprocess.run(["tesseract", "--version"], capture_output=True,
                             text=True, check=True).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        sys.exit("ERROR: tesseract not found on PATH.")
    first = out.splitlines()[0].strip()
    ver = first.split()[1] if len(first.split()) > 1 else "?"
    major = ver.split(".")[0]
    if not major.isdigit() or int(major) < 5:
        print(f"WARNING: {first}. This study needs Tesseract 5.x to be a fair "
              f"baseline; 4.x will undersell it. Install the 5.x PPA first.",
              file=sys.stderr)
    else:
        print(f"Using {first}")
    return ver


def detect_converter():
    """Return a callable name for JP2->PNG conversion, trying tools in order."""
    for tool in ("opj_decompress", "vips", "magick", "convert"):
        if shutil.which(tool):
            return tool
    sys.exit("ERROR: no JP2 decoder found. Install one:\n"
             "  sudo apt install -y libopenjp2-tools   # provides opj_decompress")


def convert_jp2(src: str, dst_png: str, tool: str):
    """Decode a single JPEG2000 page to PNG. Raises on failure (no silent skips)."""
    if tool == "opj_decompress":
        cmd = ["opj_decompress", "-i", src, "-o", dst_png]
    elif tool == "vips":
        cmd = ["vips", "copy", src, dst_png]
    else:  # ImageMagick
        cmd = [tool, src, dst_png]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(dst_png):
        raise RuntimeError(f"JP2 convert failed ({tool}): {r.stderr.strip()[:200]}")


# --------------------------------------------------------------------------- #
# Per-page work (runs in a worker process)
# --------------------------------------------------------------------------- #
def _parse_tsv(tsv_path: str):
    """Return (n_words, mean_conf) from a Tesseract TSV. Proxy signals only --
    without ground truth these are NOT accuracy, but they're useful for spotting
    failures (near-zero words) and comparing engines' confidence distributions."""
    n, conf_sum = 0, 0.0
    try:
        with open(tsv_path, newline="", encoding="utf-8", errors="replace") as f:
            r = csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
            next(r, None)  # header
            for row in r:
                if len(row) < 12:
                    continue
                if row[0] == "5" and row[11].strip():  # level 5 = word, non-empty
                    try:
                        c = float(row[10])
                    except ValueError:
                        continue
                    if c >= 0:
                        n += 1
                        conf_sum += c
    except FileNotFoundError:
        pass
    return n, (conf_sum / n if n else 0.0)


def ocr_page(task):
    """task = (issue, page_idx, jp2_path, out_dir, conv_tool, args_dict)"""
    issue, page_idx, jp2_path, out_dir, conv_tool, a = task
    base = os.path.join(out_dir, f"{issue}_p{page_idx:03d}")
    png = base + ".png"
    rec = {"issue": issue, "page": page_idx, "status": "ok",
           "convert_s": 0.0, "ocr_s": 0.0, "n_words": 0, "mean_conf": 0.0,
           "alto": base + ".xml"}
    try:
        t0 = time.perf_counter()
        convert_jp2(jp2_path, png, conv_tool)
        rec["convert_s"] = time.perf_counter() - t0

        cmd = ["tesseract", png, base,
               "--oem", str(a["oem"]), "--psm", str(a["psm"]),
               "-l", a["lang"], "--dpi", str(a["dpi"]),
               "-c", "preserve_interword_spaces=1"] + OCR_FORMATS
        t1 = time.perf_counter()
        r = subprocess.run(cmd, capture_output=True, text=True)
        rec["ocr_s"] = time.perf_counter() - t1
        if r.returncode != 0:
            rec["status"] = "tesseract_error"
            rec["err"] = r.stderr.strip()[:300]
            return rec

        rec["n_words"], rec["mean_conf"] = _parse_tsv(base + ".tsv")
        if rec["n_words"] == 0:
            rec["status"] = "empty_output"
    except Exception as e:
        rec["status"] = "convert_error"
        rec["err"] = str(e)[:300]
    finally:
        if not a["keep_images"] and os.path.exists(png):
            os.remove(png)
    return rec


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def iter_issue_zips(jp2_dir: Path, limit=None):
    zips = sorted(p for p in jp2_dir.rglob("*jp2.zip"))
    if limit:
        zips = zips[:limit]
    return zips


def run_year(args):
    check_tesseract()
    conv_tool = detect_converter()
    print(f"JP2 decoder: {conv_tool}")

    jp2_dir = Path(args.jp2_dir).expanduser()
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    zips = iter_issue_zips(jp2_dir, args.limit)
    if not zips:
        sys.exit(f"No *jp2.zip found under {jp2_dir}")
    print(f"Found {len(zips)} issue(s). Workers={args.workers}. "
          f"PSM={args.psm} OEM={args.oem} lang={args.lang} dpi={args.dpi}")

    adict = {"oem": args.oem, "psm": args.psm, "lang": args.lang,
             "dpi": args.dpi, "keep_images": args.keep_images}

    tracker = _start_tracker(args)
    results, wall0 = [], time.perf_counter()
    scratch = Path(tempfile.mkdtemp(prefix="jp2_", dir=out_dir))

    try:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            for zp in zips:
                issue = zp.name[:-len("_jp2.zip")] if zp.name.endswith("_jp2.zip") \
                        else zp.stem
                issue_scratch = scratch / issue
                issue_scratch.mkdir(exist_ok=True)
                issue_out = out_dir / issue
                issue_out.mkdir(exist_ok=True)

                # Unzip just this issue (bounds disk usage to one issue at a time).
                try:
                    with zipfile.ZipFile(zp) as zf:
                        zf.extractall(issue_scratch)
                except zipfile.BadZipFile:
                    results.append({"issue": issue, "page": 0,
                                    "status": "bad_zip"})
                    continue

                pages = sorted(issue_scratch.rglob("*.jp2"))
                tasks = [(issue, i + 1, str(p), str(issue_out), conv_tool, adict)
                         for i, p in enumerate(pages)]

                futs = [ex.submit(ocr_page, t) for t in tasks]
                for fut in as_completed(futs):
                    results.append(fut.result())

                shutil.rmtree(issue_scratch, ignore_errors=True)
                done = len([r for r in results if r.get("page")])
                print(f"  {issue}: {len(pages)} pages  (total pages done: {done})")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    wall = time.perf_counter() - wall0
    energy = _stop_tracker(tracker, out_dir)
    _write_reports(out_dir, results, wall, energy, args)


def _start_tracker(args):
    if args.no_energy:
        return None
    try:
        from codecarbon import EmissionsTracker
    except ImportError:
        print("WARNING: codecarbon not installed; recording time only. "
              "`pip install codecarbon` for energy.", file=sys.stderr)
        return None
    t = EmissionsTracker(project_name="tesseract_year",
                         output_dir=str(Path(args.out_dir).expanduser()),
                         measure_power_secs=args.power_secs, log_level="error",
                         save_to_file=True)
    t.start()
    return t


def _stop_tracker(tracker, out_dir):
    if tracker is None:
        return {}
    co2 = tracker.stop()
    info = {"co2_kg": co2}
    csv_path = Path(out_dir) / "emissions.csv"
    if csv_path.exists():
        with open(csv_path) as f:
            rows = list(csv.DictReader(f))
        if rows:
            last = rows[-1]
            for k in ("energy_consumed", "cpu_energy", "gpu_energy",
                      "ram_energy", "duration"):
                if k in last:
                    try:
                        info[k + "_kWh" if k.endswith("energy") else k] = float(last[k])
                    except ValueError:
                        pass
    return info


def _write_reports(out_dir, results, wall, energy, args):
    pages = [r for r in results if r.get("page")]
    ok = [r for r in pages if r["status"] == "ok"]
    csv_path = Path(out_dir) / "per_page.csv"
    cols = ["issue", "page", "status", "convert_s", "ocr_s",
            "n_words", "mean_conf", "alto", "err"]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(r)

    total_ocr_s = sum(r.get("ocr_s", 0) for r in pages)
    n = len(pages)
    summary = {
        "issues": len({r["issue"] for r in pages}),
        "pages_total": n,
        "pages_ok": len(ok),
        "pages_failed": n - len(ok),
        "wall_clock_s": round(wall, 1),
        "sum_ocr_cpu_s": round(total_ocr_s, 1),
        "mean_ocr_s_per_page": round(total_ocr_s / n, 3) if n else None,
        "throughput_pages_per_hour": round(n / wall * 3600, 1) if wall else None,
        "tesseract": {"psm": args.psm, "oem": args.oem,
                      "lang": args.lang, "dpi": args.dpi},
        "energy": energy,
    }
    if energy.get("energy_consumed_kWh") and n:
        wh = energy["energy_consumed_kWh"] * 1000
        summary["Wh_per_page"] = round(wh / n, 4)
        # Rough extrapolation to a full backlog (edit page count to taste):
        for label, pagecount in (("1_year", n), ("150_years_est", n * 150)):
            summary.setdefault("extrapolation", {})[label] = {
                "pages": pagecount,
                "kWh": round(energy["energy_consumed_kWh"] / n * pagecount, 2),
                "hours_wall": round(wall / n * pagecount / 3600, 1),
            }
    with open(Path(out_dir) / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print("\n==== RUN SUMMARY ====")
    print(json.dumps(summary, indent=2))
    print(f"\nPer-page detail: {csv_path}")


def measure_idle(args):
    """Capture idle machine power so you can subtract baseline from OCR energy."""
    tracker = _start_tracker(args)
    if tracker is None:
        sys.exit("Need codecarbon for idle measurement.")
    print(f"Measuring idle power for {args.measure_idle}s. Keep the box quiet...")
    time.sleep(args.measure_idle)
    energy = _stop_tracker(tracker, Path(args.out_dir).expanduser())
    if energy.get("energy_consumed_kWh"):
        watts = energy["energy_consumed_kWh"] * 1000 * 3600 / args.measure_idle
        energy["idle_watts_mean"] = round(watts, 1)
    print(json.dumps(energy, indent=2))
    with open(Path(args.out_dir).expanduser() / "idle_baseline.json", "w") as f:
        json.dump(energy, f, indent=2)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jp2-dir", help="Dir containing *jp2.zip issue archives")
    ap.add_argument("--out-dir", required=True, help="Output dir (ALTO/hOCR + reports)")
    ap.add_argument("--workers", type=int, default=os.cpu_count(),
                    help="Parallel pages (default: all logical CPUs)")
    ap.add_argument("--psm", type=int, default=3, help="Page seg mode (default 3)")
    ap.add_argument("--oem", type=int, default=1, help="Engine: 1=LSTM (default)")
    ap.add_argument("--lang", default="eng")
    ap.add_argument("--dpi", type=int, default=400, help="Source ppi (IA scans=400)")
    ap.add_argument("--limit", type=int, help="Process only first N issues (testing)")
    ap.add_argument("--keep-images", action="store_true",
                    help="Keep converted PNGs (for QA)")
    ap.add_argument("--no-energy", action="store_true", help="Skip CodeCarbon")
    ap.add_argument("--power-secs", type=int, default=15,
                    help="CodeCarbon power sampling interval")
    ap.add_argument("--measure-idle", type=int,
                    help="Just measure idle power for N seconds, then exit")
    args = ap.parse_args()

    if args.measure_idle:
        Path(args.out_dir).expanduser().mkdir(parents=True, exist_ok=True)
        measure_idle(args)
        return
    if not args.jp2_dir:
        ap.error("--jp2-dir is required (unless using --measure-idle)")
    run_year(args)


if __name__ == "__main__":
    main()
