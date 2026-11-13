#!/usr/bin/env python3
"""
Download the 1-39-0 asset references that are NOT part of Prodigy.Assets.

The first script mirrors the Prodigy.Assets table (the "daboss assets" set).
This one finds everything else the client can pull:

  * other Prodigy.Assets tables in the same folder (e.g. unmodded-game.min.js)
  * explicit asset paths in HTML / inline CSS / JS
  * optionally, third-party runtime files referenced by index.html
    (web.archive.org, Google Fonts, prodigy.blob.core.windows.net)

Anything already covered by the primary table (game.min.js) is skipped, so the
two folders never overlap. CDN paths keep the client's layout
(v1/atlases/..., images/..., etc.); external URLs go under
external/<host>/<original path> (use --no-external to skip them).

Examples:
    ./download_leftover_assets.py
    ./download_leftover_assets.py --no-external --out "other assets"
    ./download_leftover_assets.py --dry-run --list-files
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import posixpath
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import download_daboss_assets as base  # noqa: E402  (same directory)

DEFAULT_OUT = "other assets"

ATTR_RE = re.compile(r'(?:src|href)\s*=\s*["\']([^"\']+)["\']', re.I)
CSS_URL_RE = re.compile(r'url\(\s*["\']?([^"\')]+)["\']?\s*\)', re.I)
ASSET_LITERAL_RE = re.compile(
    r'["\']((?:images|v1|fonts|audio)/[^"\']+?\.'
    r'(?:png|jpe?g|gif|json|ogg|mp3|wav|xml|fnt|ttf|woff2?|svg))["\']',
    re.I,
)
ASSET_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".json", ".ogg", ".mp3", ".wav",
              ".xml", ".fnt", ".ttf", ".woff", ".woff2", ".svg", ".css", ".js")

# /web/<timestamp><mod_>/<original-url>
WAYBACK_RE = re.compile(r"^/web/(\d{8,14})(?:[a-z]{2}_)?/(.+)$", re.I)


def clean_url(url: str) -> str:
    return url.split("#", 1)[0].strip()


def is_skippable(raw: str) -> bool:
    low = raw.lower()
    return (
        not raw
        or low.startswith(("data:", "javascript:", "mailto:", "tel:", "#", "blob:"))
    )


def look_like_asset(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path.lower()
    if parts.netloc in {"fonts.googleapis.com", "fonts.gstatic.com"}:
        return True
    return path.endswith(ASSET_EXTS) or any(
        part in path for part in ("/assets/", "/images/", "/fonts/", "/styles/")
    )


def is_css_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.path.lower().endswith(".css") or parts.netloc == "fonts.googleapis.com"


def unwrap_wayback(url: str) -> str:
    parts = urlsplit(url)
    m = WAYBACK_RE.match(parts.path)
    if not m:
        return url
    original = m.group(2)
    if not original.startswith(("http://", "https://")):
        original = "http://" + original
    return original


def external_path(url: str) -> str | None:
    """external/<host>/<original path>, unwrapping web.archive.org URLs."""
    original = unwrap_wayback(url)
    parts = urlsplit(original)
    if not parts.netloc:
        return None
    host = parts.netloc.replace(":", "_")
    path = parts.path or "/"
    name = posixpath.basename(path)
    if parts.query and not posixpath.splitext(name)[1]:
        # e.g. fonts.googleapis.com/css?family=Lato... -> css__family_Lato...
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", parts.query).strip("_")
        path = path.rstrip("/") + "/" + name + ("__" + safe if safe else "")
    rel = posixpath.normpath(f"external/{host}{path}")
    if rel.startswith("../") or rel == "..":
        return None
    return rel


def cdn_relative(url: str, assets_location: str) -> str | None:
    if url.startswith(assets_location):
        rel = url[len(assets_location):]
    else:
        m = re.search(r"/((?:images|v1)/.*)$", url)
        if not m:
            return None
        rel = m.group(1)
    rel = posixpath.normpath(rel)
    if rel.startswith("../") or rel == ".." or rel.startswith("/"):
        return None
    return rel


def add_job(jobs: dict, path: str, url: str, source: str) -> None:
    jobs.setdefault(path, {"asset": source, "type": "leftover", "url": url, "path": path,
                           "source": source})


def collect(
    version_dir: Path, primary_js: Path, assets_location: str,
    covered: set[str], include_external: bool,
) -> tuple[list[dict], list[str]]:
    jobs: dict[str, dict] = {}
    notes: list[str] = []

    # 1. Any other Prodigy.Assets table in the folder (unmodded builds, backups).
    for js_file in sorted((version_dir / "js").glob("*.js")):
        if js_file.resolve() == primary_js.resolve():
            continue
        try:
            loc, entries = base.parse_assets_source(js_file)
            table_jobs, problems = base.build_jobs(loc, entries, base.ASSET_TYPES)
        except (SystemExit, ValueError):
            continue
        for p in problems:
            notes.append(f"{js_file.name}: {p}")
        for job in table_jobs:
            if job["path"] not in covered:
                add_job(jobs, job["path"], job["url"], f"table:{js_file.name}")

    # 2. HTML/CSS attributes, url(...) and explicit asset literals in text files.
    text_files: list[Path] = []
    text_files += sorted(version_dir.glob("*.html"))
    text_files += sorted(version_dir.glob("*.css"))
    text_files += sorted((version_dir / "js").glob("*.js"))

    for file in text_files:
        text = file.read_text(encoding="utf-8", errors="replace")
        refs = list(ATTR_RE.findall(text))
        refs += list(CSS_URL_RE.findall(text))
        if file.suffix.lower() == ".js":
            refs += list(ASSET_LITERAL_RE.findall(text))

        for raw in refs:
            raw = clean_url(raw)
            if is_skippable(raw):
                continue

            if raw.startswith("//"):
                raw = "https:" + raw

            if raw.startswith(("http://", "https://")):
                if raw.startswith(assets_location):
                    rel = cdn_relative(raw, assets_location)
                    if rel and rel not in covered:
                        add_job(jobs, rel, raw, f"html:{file.name}")
                elif include_external and look_like_asset(raw):
                    rel = external_path(raw)
                    if rel and rel not in covered:
                        add_job(jobs, rel, raw, f"external:{file.name}")
                continue

            # Relative reference: skip if it already exists in the version folder.
            local = (version_dir / urlsplit(raw).path.lstrip("/"))
            try:
                if local.is_file():
                    continue
            except OSError:
                pass

            # A bare images/... or v1/... path is resolved against the asset CDN.
            if re.match(r"^(?:images|v1|fonts|audio)/", raw):
                rel = cdn_relative(raw, assets_location)
                if rel and rel not in covered:
                    add_job(jobs, rel, assets_location + raw, f"path:{file.name}")
            elif include_external and look_like_asset(raw):
                notes.append(f"{file.name}: cannot resolve relative reference {raw!r}")

    # 3. Follow url(...) inside mirrored stylesheets so the CSS can resolve its
    #    own images/fonts offline (Font Awesome, Google Fonts, archived main.css).
    if include_external:
        pending = [job for job in jobs.values() if is_css_url(job["url"])]
        seen_css: set[str] = set()
        while pending:
            css = pending.pop(0)
            if css["url"] in seen_css:
                continue
            seen_css.add(css["url"])
            try:
                resp = base._session().get(css["url"], timeout=(10, 40))
                if resp.status_code != 200:
                    notes.append(f"css {css['url']}: HTTP {resp.status_code}")
                    continue
                text = resp.text
            except Exception as exc:  # noqa: BLE001 - a dead stylesheet is not fatal
                notes.append(f"css {css['url']}: {exc}")
                continue

            for ref in CSS_URL_RE.findall(text):
                ref = clean_url(ref)
                if is_skippable(ref):
                    continue
                url = urljoin(css["url"], ref)
                if url.startswith(assets_location):
                    rel = cdn_relative(url, assets_location)
                    if rel and rel not in covered:
                        add_job(jobs, rel, url, f"css:{posixpath.basename(css['path'])}")
                elif look_like_asset(url):
                    rel = external_path(url)
                    if rel and rel not in covered:
                        add_job(jobs, rel, url, f"css:{posixpath.basename(css['path'])}")
                        if is_css_url(url) and url not in seen_css:
                            pending.append(dict(jobs[rel]))

    return sorted(jobs.values(), key=lambda j: j["path"]), notes


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download the 1-39-0 asset references outside Prodigy.Assets.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", default=base.DEFAULT_VERSION,
                    help=f"game version folder (default: {base.DEFAULT_VERSION})")
    ap.add_argument("--js", help="explicit path to the primary game.min.js")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f'output directory (default: "{DEFAULT_OUT}")')
    ap.add_argument("--jobs", type=int, default=8, help="parallel downloads (default: 8)")
    ap.add_argument("--no-external", action="store_true",
                    help="skip third-party index.html scripts/CSS/fonts")
    ap.add_argument("--force", action="store_true", help="re-download existing files")
    ap.add_argument("--dry-run", action="store_true", help="list without downloading")
    ap.add_argument("--list-files", action="store_true", help="print source/destination pairs")
    ap.add_argument("--no-verify", action="store_true", help="skip file validation")
    ap.add_argument("--quiet", action="store_true", help="only print failures and the summary")
    args = ap.parse_args()

    primary_js = base.find_game_js(args.version, args.js)
    version_dir = primary_js.parent.parent

    assets_location, entries = base.parse_assets_source(primary_js)
    primary_jobs, _ = base.build_jobs(assets_location, entries, base.ASSET_TYPES)
    covered = {job["path"] for job in primary_jobs}

    jobs, notes = collect(version_dir, primary_js, assets_location, covered,
                          not args.no_external)
    out_dir = Path(args.out).expanduser()

    print(f"version : {version_dir}")
    print(f"table   : {len(covered)} files already covered by Prodigy.Assets")
    print(f"leftover: {len(jobs)} files to mirror -> {out_dir.resolve()}")
    if notes:
        print(f"notes   : {len(notes)}")
        for note in notes:
            print(f"  ! {note}")

    if args.list_files:
        for job in jobs:
            print(f"  {job['path']}\n    <- {job['url']}")
    if args.dry_run:
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "version": args.version,
                "primary_table": str(primary_js),
                "assets_location": assets_location,
                "external": not args.no_external,
                "files": jobs,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    if not jobs:
        print("nothing to download")
        return 0

    total = len(jobs)
    done = 0
    counts = {"downloaded": 0, "skipped": 0, "failed": 0}
    failures: list[dict] = []
    started = time.time()

    def run(job: dict) -> dict:
        return base.download_one(job, out_dir, args.force, not args.no_verify)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for fut in concurrent.futures.as_completed(pool.submit(run, job) for job in jobs):
            res = fut.result()
            done += 1
            counts[res["status"]] += 1
            if res["status"] == "failed":
                failures.append(res)
                print(f"[{done}/{total}] FAIL {res['path']} ({res['error']})")
            elif res["status"] == "downloaded" and not args.quiet:
                print(f"[{done}/{total}] ok   {res['path']}")
            elif args.quiet and done % 50 == 0:
                print(f"[{done}/{total}] ...")

    elapsed = time.time() - started
    if failures:
        (out_dir / "failed.txt").write_text(
            "\n".join(f"{f['url']}\t{f['error']}" for f in failures) + "\n",
            encoding="utf-8",
        )

    print(
        f"\ndone in {elapsed:.1f}s: {counts['downloaded']} downloaded, "
        f"{counts['skipped']} skipped, {counts['failed']} failed ({total} total)"
    )
    print(f"output: {out_dir.resolve()}")
    if failures:
        print(f"failures: {out_dir / 'failed.txt'}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
