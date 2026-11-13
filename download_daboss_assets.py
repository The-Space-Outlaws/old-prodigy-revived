#!/usr/bin/env python3
"""
Download every asset listed in Prodigy.Assets and mirror it into a local folder
using the exact relative paths the client builds at runtime.

The asset table lives in <version>/js/game.min.js:

    GameConstants.Build.ASSETS_LOCATION = "https://cdn.prodigygame.com/game/assets/"
    Prodigy.Assets = function() {
        var a = "images/player/reduced/outfits/male/", ...;
        this._assets = { "boss-12": { type: "atlas", base: x, key: "boss-skywatch", v: "1" }, ... };
    }

Prodigy.Loader sets Phaser's loader baseURL to ASSETS_LOCATION, then appends
relative paths:

    atlas        <base><key>/<v>/<key>.png|.json
    sprite       <base><url>
    spritesheet  <base><url>
    json         <base><url>
    sfx / bgm    v1/audio/<id>/<v>/<id>.ogg|.mp3
    spine        v1/spine/<id>/<v>/<id>.json

A few entries use an absolute `base` (the login atlas points at jsDelivr); those
are fetched from their absolute URL but still written under the matching
images/... path, so the result is a drop-in replacement for the CDN root.

Examples:
    ./download_daboss_assets.py
    ./download_daboss_assets.py --version 1-39-0 --out "daboss assets"
    ./download_daboss_assets.py --dry-run --list-files
    ./download_daboss_assets.py --types atlas,json --jobs 16
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import posixpath
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    sys.exit("error: this script needs 'requests' (pip install requests)")

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_VERSION = "1-39-0"
DEFAULT_OUT = "daboss assets"

ASSET_TYPES = {"atlas", "json", "sprite", "spritesheet", "sfx", "bgm", "spine"}


# --------------------------------------------------------------------------- #
# Minimal JS literal parsing (the table is regular enough for this)
# --------------------------------------------------------------------------- #

def _find_matching_brace(text: str, open_idx: int) -> int:
    """Return the index of the '}' matching text[open_idx], skipping strings/comments."""
    depth = 0
    i = open_idx
    quote = None
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced braces")


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    return text


def _js_string_literal(expr: str) -> str | None:
    """Evaluate a JS expression that is only concatenated double-quoted strings."""
    expr = expr.strip()
    if re.sub(r'"(?:[^"\\]|\\.)*"|\+|\s', "", expr):
        return None
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', expr)
    if not parts:
        return None
    out = []
    for p in parts:
        # Decode the same escapes JSON knows about (\", \\, \n, \uXXXX, ...).
        out.append(json.loads(f'"{p}"'))
    return "".join(out)


def _resolve_value(raw: str, variables: dict[str, str], depth: int = 0) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    literal = _js_string_literal(raw)
    if literal is not None:
        return literal
    if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        return raw
    if depth < 8 and re.fullmatch(r"[A-Za-z_$][\w$]*", raw):
        return variables.get(raw)
    return None


def parse_assets_source(js_path: Path) -> tuple[str, dict[str, dict]]:
    src = js_path.read_text(encoding="utf-8", errors="replace")

    m = re.search(r'ASSETS_LOCATION"?\]?\s*=\s*"([^"]+)"', src)
    if not m:
        raise SystemExit(f"error: could not find GameConstants.Build.ASSETS_LOCATION in {js_path}")
    assets_location = m.group(1)

    fn = re.search(r"Prodigy\.Assets\s*=\s*function\s*\(\)\s*\{", src)
    if not fn:
        raise SystemExit(f"error: could not find the Prodigy.Assets table in {js_path}")
    body_open = fn.end() - 1
    body_close = _find_matching_brace(src, body_open)
    body = src[body_open + 1 : body_close]

    table_at = body.find("this._assets")
    if table_at < 0:
        raise SystemExit("error: Prodigy.Assets has no this._assets table")
    brace_at = body.find("{", table_at)

    # Base-directory variables, e.g. x = "v1/atlases/".
    variables: dict[str, str] = {}
    prelude = _strip_comments(body[:table_at])
    assignments = list(
        re.finditer(
            r'(?:^|[,;{\s])(?:var\s+)?([A-Za-z_$][\w$]*)\s*=\s*'
            r'("(?:[^"\\]|\\.)*"(?:\s*\+\s*"(?:[^"\\]|\\.)*")*)',
            prelude,
        )
    )
    raw_vars = {m.group(1): m.group(2) for m in assignments}
    for _ in range(8):
        changed = False
        for name, expr in raw_vars.items():
            if name in variables:
                continue
            value = _resolve_value(expr, variables)
            if value is not None:
                variables[name] = value
                changed = True
        if not changed:
            break

    table_text = body[brace_at : _find_matching_brace(body, brace_at) + 1]

    entries: dict[str, dict] = {}
    entry_re = re.compile(
        r'\n\t\t(?:"((?:[^"\\]|\\.)*)"|([A-Za-z_$][\w$]*))\s*:\s*\{'
    )
    for em in entry_re.finditer(table_text):
        raw_key = em.group(1)
        asset_id = json.loads(f'"{raw_key}"') if raw_key is not None else em.group(2)
        obj_open = em.end() - 1
        obj_close = _find_matching_brace(table_text, obj_open)
        obj = _strip_comments(table_text[obj_open + 1 : obj_close])

        fields: dict[str, str] = {}
        for fm in re.finditer(
            r'([A-Za-z_$][\w$]*)\s*:\s*("(?:[^"\\]|\\.)*"|[^,\n}]+)', obj
        ):
            name, raw = fm.group(1), fm.group(2).strip()
            value = _resolve_value(raw, variables)
            if value is not None:
                fields[name] = value
        if fields:
            entries[asset_id] = fields

    return assets_location, entries


# --------------------------------------------------------------------------- #
# URL / path construction, matching Prodigy.Loader.processFile
# --------------------------------------------------------------------------- #

def _combine(base: str, tail: str) -> str:
    if not base:
        return tail
    if not base.endswith("/"):
        base += "/"
    return base + tail


def _is_absolute(url: str) -> bool:
    return url.startswith("//") or urlparse(url).scheme in {"http", "https"}


def build_jobs(
    assets_location: str, entries: dict[str, dict], want_types: set[str]
) -> tuple[list[dict], list[str]]:
    jobs: dict[str, dict] = {}
    problems: list[str] = []

    def add(asset_id: str, kind: str, url: str) -> None:
        if _is_absolute(url):
            full = url
        else:
            full = assets_location + url
        rel = full[len(assets_location) :] if full.startswith(assets_location) else None
        if rel is None:
            # Absolute override (jsDelivr etc.) -- keep the meaningful images/ or v1/ path.
            m = re.search(r"/((?:images|v1)/.*)$", full)
            rel = m.group(1) if m else None
        if not rel:
            problems.append(f"{asset_id} ({kind}): cannot derive local path from {full}")
            return
        rel = posixpath.normpath(rel)
        if rel.startswith("../") or rel == ".." or rel.startswith("/"):
            problems.append(f"{asset_id} ({kind}): unsafe local path {rel!r}")
            return
        jobs.setdefault(rel, {"asset": asset_id, "type": kind, "url": full, "path": rel})

    for asset_id, fields in entries.items():
        kind = fields.get("type", "")
        if kind not in want_types:
            if kind and kind not in ASSET_TYPES:
                problems.append(f"{asset_id}: unknown asset type {kind!r}")
            continue
        base = fields.get("base", "")
        v = fields.get("v", "")

        if kind in {"json", "sprite", "spritesheet"}:
            url = fields.get("url")
            if not url:
                problems.append(f"{asset_id} ({kind}): missing url")
                continue
            add(asset_id, kind, _combine(base, url))

        elif kind == "atlas":
            key = fields.get("key", asset_id)
            if not v:
                problems.append(f"{asset_id} (atlas): missing v")
                continue
            prefix = _combine(base, f"{key}/{v}/{key}")
            add(asset_id, kind, prefix + ".png")
            add(asset_id, kind, prefix + ".json")

        elif kind in {"sfx", "bgm"}:
            if not v:
                problems.append(f"{asset_id} ({kind}): missing v")
                continue
            stem = f"v1/audio/{asset_id}/{v}/{asset_id}"
            add(asset_id, kind, stem + ".ogg")
            add(asset_id, kind, stem + ".mp3")

        elif kind == "spine":
            if not v:
                problems.append(f"{asset_id} (spine): missing v")
                continue
            add(asset_id, kind, f"v1/spine/{asset_id}/{v}/{asset_id}.json")

    return sorted(jobs.values(), key=lambda j: j["path"]), problems


# --------------------------------------------------------------------------- #
# Downloading
# --------------------------------------------------------------------------- #

_thread_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        retry = Retry(
            total=4,
            connect=4,
            read=4,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        s.headers.update(
            {
                "User-Agent": "old-prodigy-asset-mirror/1.0",
                "Accept": "*/*",
            }
        )
        _thread_local.session = s
        return s
    return s


def _verify_file(path: Path) -> tuple[bool, str]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return False, str(exc)
    if size == 0:
        return False, "empty file"

    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            with path.open("r", encoding="utf-8") as fh:
                json.load(fh)
        elif suffix == ".png":
            with path.open("rb") as fh:
                if fh.read(8) != b"\x89PNG\r\n\x1a\n":
                    return False, "not a PNG"
        elif suffix == ".ogg":
            with path.open("rb") as fh:
                if fh.read(4) != b"OggS":
                    return False, "not an OGG"
        elif suffix == ".mp3":
            with path.open("rb") as fh:
                head = fh.read(3)
                if head[:3] != b"ID3" and not head[:1] == b"\xff":
                    return False, "not an MP3"
    except (OSError, ValueError) as exc:
        return False, str(exc)
    return True, ""


def download_one(job: dict, out_dir: Path, force: bool, verify: bool) -> dict:
    target = out_dir / job["path"]
    result = dict(job)
    result["status"] = "failed"
    result["error"] = ""

    if target.exists() and not force:
        if target.stat().st_size > 0:
            result["status"] = "skipped"
            return result
        target.unlink(missing_ok=True)

    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")

    try:
        with _session().get(job["url"], stream=True, timeout=(15, 90)) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            expected = resp.headers.get("Content-Length")
            encoded = resp.headers.get("Content-Encoding")
            written = 0
            with part.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if chunk:
                        fh.write(chunk)
                        written += len(chunk)
            # Content-Length describes the wire body; requests transparently
            # decompresses gzip/br, so only compare when nothing was encoded.
            if expected is not None and expected.isdigit() and not encoded and written != int(expected):
                raise RuntimeError(f"short read: {written}/{expected} bytes")
        os.replace(part, target)
    except Exception as exc:  # noqa: BLE001 - report and continue with the rest
        part.unlink(missing_ok=True)
        result["error"] = str(exc)
        return result

    if verify:
        ok, why = _verify_file(target)
        if not ok:
            target.unlink(missing_ok=True)
            result["error"] = f"verification failed: {why}"
            return result

    result["status"] = "downloaded"
    result["bytes"] = target.stat().st_size
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def find_game_js(version: str, explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_file():
            raise SystemExit(f"error: no such file: {p}")
        return p

    cwd = Path.cwd()
    candidates = []
    if cwd.name == version:
        candidates.append(cwd / "js" / "game.min.js")   # inside the matching version folder
    candidates += [
        REPO_ROOT / "oldprodigy" / version / "js" / "game.min.js",
        cwd / "oldprodigy" / version / "js" / "game.min.js",
        cwd / version / "js" / "game.min.js",
        cwd / "js" / "game.min.js",
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()

    found = sorted(p.name for p in (REPO_ROOT / "oldprodigy").glob("*/js/game.min.js"))
    hint = "\n  ".join(str(REPO_ROOT / "oldprodigy" / v / "js" / "game.min.js") for v in found)
    raise SystemExit(f"error: could not find game.min.js for {version}.\nAvailable:\n  {hint}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Mirror every Prodigy.Assets file using the client's own relative paths.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", default=DEFAULT_VERSION, help=f"game version folder (default: {DEFAULT_VERSION})")
    ap.add_argument("--js", help="explicit path to game.min.js (overrides --version)")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f'output directory (default: "{DEFAULT_OUT}")')
    ap.add_argument("--jobs", type=int, default=8, help="parallel downloads (default: 8)")
    ap.add_argument("--types", help="comma-separated subset, e.g. atlas,json,sfx")
    ap.add_argument("--base", help="override ASSETS_LOCATION for relative paths")
    ap.add_argument("--force", action="store_true", help="re-download files that already exist")
    ap.add_argument("--dry-run", action="store_true", help="build the list but download nothing")
    ap.add_argument("--list-files", action="store_true", help="print every source/destination pair")
    ap.add_argument("--no-verify", action="store_true", help="skip magic-byte/JSON validation")
    ap.add_argument("--quiet", action="store_true", help="only print failures and the summary")
    args = ap.parse_args()

    js_path = find_game_js(args.version, args.js)
    assets_location = args.base
    parsed_location, entries = parse_assets_source(js_path)
    if not assets_location:
        assets_location = parsed_location
    if not assets_location.endswith("/"):
        assets_location += "/"

    want_types = ASSET_TYPES
    if args.types:
        want_types = {t.strip() for t in args.types.split(",") if t.strip()}
        unknown = want_types - ASSET_TYPES
        if unknown:
            raise SystemExit(f"error: unknown --types: {', '.join(sorted(unknown))}")

    jobs, problems = build_jobs(assets_location, entries, want_types)
    out_dir = Path(args.out).expanduser()

    print(f"source : {js_path}")
    print(f"base   : {assets_location}")
    print(f"assets : {len(entries)} entries -> {len(jobs)} files in {out_dir.resolve()}")
    if problems:
        print(f"warnings: {len(problems)}")
        for p in problems:
            print(f"  ! {p}")

    if args.list_files:
        for job in jobs:
            print(f"  {job['path']}\n    <- {job['url']}")
    if args.dry_run:
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": args.version,
        "source": str(js_path),
        "assets_location": assets_location,
        "files": jobs,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    total = len(jobs)
    done = 0
    counts = {"downloaded": 0, "skipped": 0, "failed": 0}
    failures: list[dict] = []
    lock = threading.Lock()
    started = time.time()

    def run(job: dict) -> dict:
        return download_one(job, out_dir, args.force, not args.no_verify)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [pool.submit(run, job) for job in jobs]
        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            with lock:
                done += 1
                counts[res["status"]] += 1
                if res["status"] == "failed":
                    failures.append(res)
                    print(f"[{done}/{total}] FAIL {res['path']} ({res['error']})")
                elif res["status"] == "downloaded" and not args.quiet:
                    print(f"[{done}/{total}] ok   {res['path']}")
                elif args.quiet and done % 100 == 0:
                    print(f"[{done}/{total}] ...")

    elapsed = time.time() - started
    if failures:
        failed_path = out_dir / "failed.txt"
        failed_path.write_text(
            "\n".join(f"{f['url']}\t{f['error']}" for f in failures) + "\n",
            encoding="utf-8",
        )

    print(
        f"\ndone in {elapsed:.1f}s: "
        f"{counts['downloaded']} downloaded, {counts['skipped']} skipped, "
        f"{counts['failed']} failed ({total} total)"
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
