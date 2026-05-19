"""Command-line interface for photobucket-dl."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

from photobucket_dl import __version__
from photobucket_dl.client import Album, AuthError, Client, Media

AUTH_HELP = """\
photobucket-dl needs the value of the `app_auth` cookie from app.photobucket.com.

How to grab it (Firefox or Chromium-based browsers):

  1. Open https://app.photobucket.com and log in.
  2. Open DevTools (F12) -> Application/Storage -> Cookies
     -> https://app.photobucket.com.
  3. Find the cookie named  app_auth  and copy its value (a long string
     starting with `eyJ...`).
  4. Pass it via one of:
       photobucket-dl --cookie '<value>'
       photobucket-dl --cookie-file ./auth.txt
       PHOTOBUCKET_AUTH='<value>' photobucket-dl

The cookie expires roughly one hour after sign-in. If you see an
authentication error, refresh the page, grab a new value, and re-run.
Already-downloaded files are skipped on retry.

If browser-cookie3 is installed (pip install browser-cookie3) the tool
will try to read the cookie directly from your default browser.
"""


def decode_jwt_unverified(token: str) -> dict:
    """Decode a JWT payload without verifying the signature.

    Verification is the server's job; we just need the user_id and expiry
    embedded in the claims.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("not a JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def try_browser_cookie() -> str | None:
    try:
        import browser_cookie3
    except ImportError:
        return None
    for loader_name in ("firefox", "chrome", "chromium", "edge", "brave"):
        loader = getattr(browser_cookie3, loader_name, None)
        if loader is None:
            continue
        try:
            jar = loader(domain_name="photobucket.com")
        except Exception:
            continue
        for cookie in jar:
            if cookie.name == "app_auth" and cookie.value:
                return cookie.value
    return None


def resolve_auth(args: argparse.Namespace) -> tuple[str, str]:
    jwt: str | None = None
    source = ""
    if args.cookie:
        jwt, source = args.cookie.strip(), "--cookie"
    elif args.cookie_file:
        jwt, source = Path(args.cookie_file).read_text().strip(), str(args.cookie_file)
    elif os.environ.get("PHOTOBUCKET_AUTH"):
        jwt, source = os.environ["PHOTOBUCKET_AUTH"].strip(), "PHOTOBUCKET_AUTH"
    else:
        jwt = try_browser_cookie()
        if jwt:
            source = "browser-cookie3"

    if not jwt:
        sys.stderr.write(AUTH_HELP)
        sys.exit(2)

    try:
        claims = decode_jwt_unverified(jwt)
    except Exception as e:
        sys.exit(f"could not decode auth cookie ({source}): {e}")
    exp = claims.get("exp", 0)
    if exp and exp < time.time():
        sys.exit(
            "auth cookie expired "
            f"({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exp))}). "
            "Grab a fresh value from app.photobucket.com and re-run."
        )
    user_id = claims.get("sub") or claims.get("user_id")
    if not user_id:
        sys.exit("auth cookie has no `sub`/`user_id` claim")
    return jwt, user_id


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(s: str) -> str:
    s = _UNSAFE.sub("_", s).strip().rstrip(".")
    return s[:120] or "_"


def album_path(by_id: dict[str, Album], album: Album) -> str:
    parts: list[str] = []
    cur: Album | None = album
    while cur is not None:
        parts.append(safe_name(cur.title or cur.id))
        cur = by_id.get(cur.parent_id) if cur.parent_id else None
    return "/".join(reversed(parts)) if parts else ""


def pick_filename(media: dict, fallback_url: str | None) -> str:
    name = (
        media.get("originalFilename")
        or media.get("filename")
        or media.get("title")
        or media["id"]
    )
    name = safe_name(name)
    if "." not in name and fallback_url:
        ext = os.path.splitext(urllib.parse.urlparse(fallback_url).path)[1]
        if ext and len(ext) <= 6:
            name += ext
    return name


def enumerate_bucket(client: Client, bucket_id: str, log) -> tuple[list[dict], list[Album]]:
    """Walk every album under the bucket, return (all_media_records, albums).

    Each media record has its `_album_path` set; duplicates may exist because
    the API returns root-level listings that overlap with sub-album listings.
    """
    albums = client.list_albums(bucket_id)
    log(f"  {len(albums)} album(s)")
    by_id = {a.id: a for a in albums}
    media: list[dict] = []
    # root listing returns everything in the bucket, including sub-album media
    try:
        for m in client.list_media(bucket_id, None):
            m["_album_path"] = ""
            media.append(m)
        log(f"  root: {len(media)} media")
    except Exception as e:
        log(f"  root media listing failed: {e}")
    for a in albums:
        try:
            items = client.list_media(bucket_id, a.id)
        except Exception as e:
            log(f"  album {a.title!r}: ERROR {e}")
            continue
        path = album_path(by_id, a)
        for m in items:
            m["_album_path"] = path
        media.extend(items)
    return media, albums


def dedupe_media(media: list[dict]) -> dict[str, dict]:
    best: dict[str, dict] = {}
    for m in media:
        prev = best.get(m["id"])
        if prev is None or (not prev.get("_album_path") and m.get("_album_path")):
            best[m["id"]] = m
    return best


def run_download(client: Client, user_id: str, output: Path, workers: int, verbose: bool) -> int:
    def log(msg: str) -> None:
        print(msg, flush=True)

    log(f"listing buckets for user {user_id}")
    buckets = client.list_buckets(user_id)
    if not buckets:
        log("no buckets found for this account")
        return 0
    log(f"  found {len(buckets)} bucket(s)")

    manifest = {"user_id": user_id, "buckets": []}
    jobs: list[tuple[Path, str, int | None]] = []
    for b in buckets:
        log(f"\nbucket {b.id} '{b.title}' (type={b.bucket_type}, media={b.total_media})")
        media, _albums = enumerate_bucket(client, b.id, log)
        unique = dedupe_media(media)
        log(f"  {len(unique)} unique media items")

        log(f"  fetching {len(unique)} signed URL(s) for originals")
        signed = client.fetch_signed_urls(b.id, list(unique.keys()))
        log(f"  got {len(signed)} signed URL(s)")

        bucket_dir = output / safe_name(b.title or b.id)
        for mid, m in unique.items():
            url = signed.get(mid)
            if not url:
                log(f"  WARN: no signed URL for media {mid}; skipping")
                continue
            sub = m.get("_album_path") or "_root"
            target = bucket_dir / sub / pick_filename(m, url)
            jobs.append((target, url, m.get("fileSize")))
        manifest["buckets"].append({
            "id": b.id,
            "title": b.title,
            "media_count": len(unique),
        })

    manifest_path = output / "manifest.json"
    output.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2))

    log(f"\n{len(jobs)} files to download into {output}")
    ok = skipped = failed = 0
    started = time.time()

    def worker(target: Path, url: str, sz: int | None) -> tuple[str, str]:
        try:
            status = client.download(url, target, sz)
            return status, str(target)
        except Exception as e:
            return "fail", f"{target}: {e}"

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(worker, t, u, sz) for t, u, sz in jobs]
        for i, fut in enumerate(cf.as_completed(futures), 1):
            status, msg = fut.result()
            if status == "ok":
                ok += 1
                if verbose:
                    log(f"  OK  {msg}")
            elif status == "skip":
                skipped += 1
                if verbose:
                    log(f"  -   {msg}")
            else:
                failed += 1
                log(f"  FAIL {msg}")
            if i % 25 == 0 or i == len(jobs):
                rate = i / max(0.001, time.time() - started)
                log(f"  [{i}/{len(jobs)}] ok={ok} skip={skipped} fail={failed} ({rate:.1f}/s)")

    log(f"\ndone. ok={ok} skip={skipped} fail={failed}")
    return 0 if failed == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="photobucket-dl",
        description=(
            "Download all media from your own Photobucket account. "
            "Use only on accounts you own."
        ),
        epilog=AUTH_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("photobucket"),
        help="output directory (default: ./photobucket)",
    )
    p.add_argument(
        "--cookie",
        help="auth cookie value (the `app_auth` cookie from app.photobucket.com)",
    )
    p.add_argument(
        "--cookie-file",
        type=Path,
        help="file containing the auth cookie value (one line)",
    )
    p.add_argument(
        "-j", "--workers",
        type=int,
        default=6,
        help="parallel download workers (default: 6)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="print every file")
    p.add_argument("--version", action="version", version=f"photobucket-dl {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    jwt, user_id = resolve_auth(args)
    client = Client(jwt)
    try:
        return run_download(client, user_id, args.output.resolve(), args.workers, args.verbose)
    except AuthError as e:
        sys.stderr.write(
            f"\nauthentication failed: {e}\n\n"
            "Your cookie may have expired (they last about an hour). "
            "Grab a fresh `app_auth` cookie from app.photobucket.com and re-run; "
            "already-downloaded files will be skipped.\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
