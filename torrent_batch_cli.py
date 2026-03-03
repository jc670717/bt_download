#!/usr/bin/env python3
"""
Interactive torrent selector and batch downloader for legal/public RSS feeds.

Features:
- Load entries from an RSS/Atom feed URL
- Show: name, size, seeders, leechers, downloads
- Let user select items by index/range
- Batch download .torrent files to a local folder
"""

from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import ssl
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from typing import Iterable, List, Optional


DEFAULT_UA = "TorrentBatchCLI/1.0 (+https://example.invalid)"
TLS_VERIFY = True
TLS_CA_BUNDLE: Optional[str] = None
PROXY_URL: Optional[str] = None
DEFAULT_TIMEOUT = 45
DEFAULT_RETRIES = 3


@dataclass
class TorrentItem:
    idx: int
    name: str
    date: str
    size: str
    seeders: str
    leechers: str
    downloads: str
    torrent_url: str
    timestamp: int = 0
    downloaded: str = "No"


def configure_tls(verify: bool = True, ca_bundle: Optional[str] = None) -> None:
    global TLS_VERIFY, TLS_CA_BUNDLE
    TLS_VERIFY = verify
    TLS_CA_BUNDLE = ca_bundle


def configure_network(proxy_url: Optional[str] = None) -> None:
    global PROXY_URL
    PROXY_URL = proxy_url.strip() if proxy_url else None


def _ssl_context() -> ssl.SSLContext:
    if not TLS_VERIFY:
        return ssl._create_unverified_context()  # noqa: SLF001
    if TLS_CA_BUNDLE:
        return ssl.create_default_context(cafile=TLS_CA_BUNDLE)
    return ssl.create_default_context()


def _is_timeout_error(e: Exception) -> bool:
    msg = str(e).lower()
    if "timed out" in msg or "timeout" in msg:
        return True
    reason = getattr(e, "reason", None)
    if isinstance(reason, TimeoutError | socket.timeout):
        return True
    if isinstance(e, TimeoutError | socket.timeout):
        return True
    return False


def _is_cert_verify_error(e: Exception) -> bool:
    return "certificate_verify_failed" in str(e).lower()


def _open_request(req: urllib.request.Request, timeout: int, context: ssl.SSLContext):
    if PROXY_URL:
        proxy_handler = urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL})
        https_handler = urllib.request.HTTPSHandler(context=context)
        opener = urllib.request.build_opener(proxy_handler, https_handler)
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout, context=context)


def _urlopen_with_retry(req: urllib.request.Request, timeout: int, retries: int):
    last_err: Optional[Exception] = None
    context = _ssl_context()
    cert_fallback_used = False
    for attempt in range(1, max(1, retries) + 1):
        try:
            return _open_request(req, timeout=timeout, context=context)
        except urllib.error.HTTPError:
            # HTTP status errors should fail fast.
            raise
        except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError) as e:
            last_err = e
            # In locked-down Windows/proxy environments, cert verify can fail even for valid sites.
            # Fallback once to unverified TLS to improve exe compatibility.
            if _is_cert_verify_error(e) and not cert_fallback_used:
                context = ssl._create_unverified_context()  # noqa: SLF001
                cert_fallback_used = True
                continue
            if attempt >= retries or not _is_timeout_error(e):
                raise
            time.sleep(min(2 ** (attempt - 1), 4))
    if last_err is not None:
        raise last_err
    raise TimeoutError("Request failed")


def item_history_key(item: TorrentItem) -> str:
    url = (item.torrent_url or "").strip()
    if url:
        return f"url:{url}"
    return f"name:{item.name.strip().lower()}"


def history_file_path(out_dir: str) -> str:
    return os.path.join(out_dir, "download_history.json")


def load_download_history(out_dir: str) -> set[str]:
    path = history_file_path(out_dir)
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        items = payload.get("items", [])
        if isinstance(items, list):
            return {str(x) for x in items}
    except Exception:  # noqa: BLE001
        return set()
    return set()


def save_download_history(out_dir: str, keys: set[str]) -> None:
    path = history_file_path(out_dir)
    payload = {"version": 1, "items": sorted(keys)}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def fetch_xml(url: str, timeout: int = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_UA,
            "Accept-Encoding": "gzip, deflate",
        },
    )
    with _urlopen_with_retry(req, timeout=timeout, retries=retries) as resp:
        content_type = resp.headers.get("Content-Type", "")
        content_encoding = resp.headers.get("Content-Encoding", "").lower()
        raw = resp.read()

        if "gzip" in content_encoding:
            raw = gzip.decompress(raw)
        elif "deflate" in content_encoding:
            raw = zlib.decompress(raw)

        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";")[0].strip()
            return raw.decode(charset, errors="replace")
        return raw.decode("utf-8", errors="replace")


def looks_like_html(text: str) -> bool:
    probe = text.lstrip()[:500].lower()
    return probe.startswith("<!doctype html") or probe.startswith("<html") or "<head" in probe


def looks_like_xml(text: str) -> bool:
    probe = text.lstrip()[:200]
    return probe.startswith("<?xml") or probe.startswith("<rss") or probe.startswith("<feed")


def _text(node: Optional[ET.Element]) -> str:
    if node is None or node.text is None:
        return ""
    return html.unescape(node.text.strip())


def _find_first(entry: ET.Element, names: Iterable[str]) -> Optional[ET.Element]:
    for n in names:
        found = entry.find(n)
        if found is not None:
            return found
    return None


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    if ":" in tag:
        return tag.rsplit(":", 1)[1]
    return tag


def _find_child_by_local_names(entry: ET.Element, local_names: Iterable[str]) -> Optional[ET.Element]:
    wanted = {n.lower() for n in local_names}
    for child in entry.iter():
        if child is entry:
            continue
        if _local_name(child.tag).lower() in wanted:
            return child
    return None


def _extract_field_text(entry: ET.Element, field: str) -> str:
    node = _find_child_by_local_names(entry, [field])
    value = _text(node)
    if value:
        return value

    # Fallback for unusual namespace/serialization edge cases.
    xml_blob = ET.tostring(entry, encoding="unicode")
    m = re.search(
        rf"<(?:[\w.-]+:)?{re.escape(field)}>\s*(.*?)\s*</(?:[\w.-]+:)?{re.escape(field)}>",
        xml_blob,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        return html.unescape(m.group(1).strip())
    return ""


def _to_abs(base_url: str, link: str) -> str:
    return urllib.parse.urljoin(base_url, link.strip())


def normalize_feed_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    if not parsed.scheme:
        return url

    q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    page = (q.get("page", [""])[0] or "").lower()

    if page == "rss":
        return url

    # If this looks like a web listing URL, convert to RSS endpoint.
    host = parsed.netloc.lower()
    if "nyaa.si" in host:
        q["page"] = ["rss"]
        new_query = urllib.parse.urlencode(q, doseq=True)
        return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", new_query, ""))

    return url


def parse_feed(feed_xml: str, base_url: str) -> List[TorrentItem]:
    root = ET.fromstring(feed_xml)
    items: List[TorrentItem] = []

    rss_items = root.findall(".//item")
    atom_entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    nodes: List[ET.Element] = rss_items if rss_items else atom_entries

    for i, node in enumerate(nodes, start=1):
        title = _text(_find_first(node, ["title", "{http://www.w3.org/2005/Atom}title"])) or f"item-{i}"

        # Parse by local-name so namespace URI/prefix differences don't break field lookup.
        date = (
            _text(_find_first(node, ["pubDate", "{http://www.w3.org/2005/Atom}updated", "{http://www.w3.org/2005/Atom}published"]))
            or "-"
        )
        size = _extract_field_text(node, "size")
        seeders = _extract_field_text(node, "seeders")
        leechers = _extract_field_text(node, "leechers")
        downloads = _extract_field_text(node, "downloads")

        torrent_url = ""
        enclosure = node.find("enclosure")
        if enclosure is not None:
            candidate = enclosure.attrib.get("url", "")
            if candidate:
                torrent_url = _to_abs(base_url, candidate)

        if not torrent_url:
            # RSS link or nyaa's explicit torrent link field.
            torrent_node = _find_child_by_local_names(node, ["torrent"])
            if torrent_node is not None:
                candidate = _text(torrent_node)
                if candidate:
                    torrent_url = _to_abs(base_url, candidate)

        if not torrent_url:
            # Try RSS/Atom link variants.
            for link_node in list(node):
                if _local_name(link_node.tag).lower() != "link":
                    continue

                candidate = link_node.attrib.get("href", "").strip() or _text(link_node)
                if candidate and (candidate.endswith(".torrent") or "download" in candidate):
                    torrent_url = _to_abs(base_url, candidate)
                    break

        if not torrent_url:
            # Atom can have multiple link nodes with rel=enclosure.
            for link_node in node.findall("{http://www.w3.org/2005/Atom}link"):
                href = link_node.attrib.get("href", "")
                rel = link_node.attrib.get("rel", "")
                if href and (rel == "enclosure" or href.endswith(".torrent")):
                    torrent_url = _to_abs(base_url, href)
                    break

        if torrent_url:
            items.append(
                TorrentItem(
                    idx=i,
                    name=title,
                    date=date,
                    size=size or "-",
                    seeders=seeders or "-",
                    leechers=leechers or "-",
                    downloads=downloads or "-",
                    torrent_url=torrent_url,
                )
            )

    return items


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s, flags=re.DOTALL)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_listing_html(listing_html: str, base_url: str) -> List[TorrentItem]:
    items: List[TorrentItem] = []
    row_re = re.compile(r"<tr[^>]*class=\"[^\"]*\bdefault\b[^\"]*\"[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
    td_re = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
    a_re = re.compile(r"<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)

    for i, row in enumerate(row_re.findall(listing_html), start=1):
        tds = td_re.findall(row)
        if len(tds) < 8:
            continue

        name = ""
        torrent_url = ""

        for href, label in a_re.findall(row):
            href_lower = href.lower()
            if "/view/" in href_lower and not name:
                name = _strip_tags(label)
            if href_lower.endswith(".torrent") or "/download/" in href_lower:
                torrent_url = _to_abs(base_url, html.unescape(href))

        if not name or not torrent_url:
            continue

        size = _strip_tags(tds[3]) if len(tds) > 3 else "-"
        date = _strip_tags(tds[4]) if len(tds) > 4 else "-"
        ts_match = re.search(r"data-timestamp=\"(\d+)\"", tds[4], flags=re.IGNORECASE) if len(tds) > 4 else None
        timestamp = int(ts_match.group(1)) if ts_match else 0
        downloads = _strip_tags(tds[5]) if len(tds) > 5 else "-"
        seeders = _strip_tags(tds[6]) if len(tds) > 6 else "-"
        leechers = _strip_tags(tds[7]) if len(tds) > 7 else "-"

        items.append(
                TorrentItem(
                    idx=i,
                    name=name or f"item-{i}",
                    date=date or "-",
                    size=size or "-",
                    seeders=seeders or "-",
                    leechers=leechers or "-",
                    downloads=downloads or "-",
                    torrent_url=torrent_url,
                    timestamp=timestamp,
                )
        )

    return items


def extract_next_feed_url(feed_xml: str, base_url: str) -> Optional[str]:
    root = ET.fromstring(feed_xml)
    for node in root.iter():
        if _local_name(node.tag).lower() != "link":
            continue
        rel = (node.attrib.get("rel", "") or "").strip().lower()
        if rel != "next":
            continue
        href = (node.attrib.get("href", "") or "").strip() or _text(node)
        if href:
            return _to_abs(base_url, href)
    return None


def extract_next_html_url(listing_html: str, base_url: str) -> Optional[str]:
    m = re.search(r"<a[^>]*rel=\"next\"[^>]*href=\"([^\"]+)\"", listing_html, flags=re.IGNORECASE)
    if m:
        return _to_abs(base_url, html.unescape(m.group(1)))

    for href, label in re.findall(r"<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", listing_html, flags=re.IGNORECASE | re.DOTALL):
        text = _strip_tags(label).lower()
        if text in {"next", ">", ">>", "next >", "next >>"}:
            return _to_abs(base_url, html.unescape(href))
    return None


def load_items_from_feed(url: str, max_pages: int = 1) -> tuple[List[TorrentItem], int, str]:
    if max_pages < 1:
        max_pages = 1

    normalized_url = normalize_feed_url(url)
    p = urllib.parse.urlparse(normalized_url)
    if p.scheme and p.netloc and not p.path:
        normalized_url = urllib.parse.urlunparse((p.scheme, p.netloc, "/", "", p.query, p.fragment))
    items: List[TorrentItem] = []
    visited: set[str] = set()
    page_count = 0
    current_url = normalized_url

    while current_url and page_count < max_pages and current_url not in visited:
        visited.add(current_url)
        try:
            xml_text = fetch_xml(current_url)
        except urllib.error.HTTPError:
            # Keep already-collected pages instead of failing the whole load.
            if page_count > 0:
                break
            raise
        if not looks_like_xml(xml_text):
            raise ValueError("URL did not return XML feed content.")
        if looks_like_html(xml_text):
            raise ValueError("URL returned HTML, not RSS/Atom XML.")

        page_items = parse_feed(xml_text, current_url)
        items.extend(page_items)
        page_count += 1

        next_url = extract_next_feed_url(xml_text, current_url)
        if not next_url:
            break
        current_url = next_url

    for i, item in enumerate(items, start=1):
        item.idx = i

    return items, page_count, normalized_url


def load_items_from_html(url: str, max_pages: int = 1) -> tuple[List[TorrentItem], int, str]:
    if max_pages < 1:
        max_pages = 1

    normalized_url = url.strip()
    p = urllib.parse.urlparse(normalized_url)
    if p.scheme and p.netloc and not p.path:
        normalized_url = urllib.parse.urlunparse((p.scheme, p.netloc, "/", "", p.query, p.fragment))
    items: List[TorrentItem] = []
    visited: set[str] = set()
    page_count = 0
    current_url = normalized_url

    while current_url and page_count < max_pages and current_url not in visited:
        visited.add(current_url)
        try:
            text = fetch_xml(current_url)
        except urllib.error.HTTPError:
            # Keep already-collected pages instead of failing the whole load.
            if page_count > 0:
                break
            raise
        if not looks_like_html(text):
            raise ValueError("URL did not return HTML listing content.")

        page_items = parse_listing_html(text, current_url)
        items.extend(page_items)
        page_count += 1

        next_url = extract_next_html_url(text, current_url)
        if not next_url:
            break
        current_url = next_url

    for i, item in enumerate(items, start=1):
        item.idx = i

    return items, page_count, normalized_url


def mark_downloaded(items: List[TorrentItem], out_dir: str, history_keys: Optional[set[str]] = None) -> None:
    keys = history_keys if history_keys is not None else load_download_history(out_dir)
    for it in items:
        out_path = os.path.join(out_dir, sanitize_filename(it.name) + ".torrent")
        in_dir = os.path.exists(out_path)
        in_history = item_history_key(it) in keys
        it.downloaded = "Yes" if (in_dir or in_history) else "No"


def format_table(items: List[TorrentItem]) -> str:
    headers = ["#", "Name", "Date", "Size", "Seed", "Leech", "D/L", "Done"]
    rows = [
        [str(it.idx), it.name, it.date, it.size, it.seeders, it.leechers, it.downloads, it.downloaded]
        for it in items
    ]

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            max_w = 70 if i == 1 else (22 if i == 2 else 20)
            widths[i] = min(max(widths[i], len(cell)), max_w)

    def clip(s: str, w: int) -> str:
        return s if len(s) <= w else s[: max(0, w - 1)] + "…"

    lines = []
    header_line = " | ".join(clip(h, widths[i]).ljust(widths[i]) for i, h in enumerate(headers))
    sep = "-+-".join("-" * widths[i] for i in range(len(headers)))
    lines.append(header_line)
    lines.append(sep)
    for row in rows:
        lines.append(" | ".join(clip(c, widths[i]).ljust(widths[i]) for i, c in enumerate(row)))
    return "\n".join(lines)


def parse_selection(selection: str, valid_indices: set[int]) -> List[int]:
    chosen: set[int] = set()
    tokens = [t.strip() for t in selection.split(",") if t.strip()]

    for token in tokens:
        if "-" in token:
            m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
            if not m:
                raise ValueError(f"Invalid range: {token}")
            start, end = int(m.group(1)), int(m.group(2))
            if start > end:
                start, end = end, start
            for i in range(start, end + 1):
                if i in valid_indices:
                    chosen.add(i)
        else:
            if not token.isdigit():
                raise ValueError(f"Invalid index: {token}")
            i = int(token)
            if i in valid_indices:
                chosen.add(i)

    return sorted(chosen)


def sanitize_filename(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or "torrent"


def download_file(url: str, output_path: str, timeout: int = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
    with _urlopen_with_retry(req, timeout=timeout, retries=retries) as resp, open(output_path, "wb") as f:
        f.write(resp.read())


def run() -> int:
    parser = argparse.ArgumentParser(description="Interactive torrent selector/downloader for authorized sources.")
    parser.add_argument("--url", required=True, help="RSS/Atom feed URL or HTML listing URL")
    parser.add_argument("--out", default="./downloads", help="Output directory")
    parser.add_argument("--limit", type=int, default=1000, help="Display first N items (default: 1000)")
    parser.add_argument("--pages", type=int, default=1, help="Follow rel=next for up to N pages (default: 1)")
    parser.add_argument(
        "--mode",
        choices=["auto", "feed", "html"],
        default="auto",
        help="Input mode: auto-detect, feed(XML), or html listing (default: auto)",
    )
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification (not recommended)")
    parser.add_argument("--ca-bundle", default="", help="Custom CA bundle path for TLS verification")
    parser.add_argument("--proxy", default="", help="Proxy URL, e.g. http://127.0.0.1:7890")
    args = parser.parse_args()
    configure_tls(verify=not args.insecure, ca_bundle=(args.ca_bundle.strip() or None))
    configure_network(proxy_url=(args.proxy.strip() or None))

    try:
        if args.mode == "feed":
            items, pages_loaded, normalized_url = load_items_from_feed(args.url, args.pages)
        elif args.mode == "html":
            items, pages_loaded, normalized_url = load_items_from_html(args.url, args.pages)
        else:
            try:
                items, pages_loaded, normalized_url = load_items_from_html(args.url, args.pages)
            except Exception:
                items, pages_loaded, normalized_url = load_items_from_feed(args.url, args.pages)
        if normalized_url != args.url:
            print(f"[info] normalized URL -> {normalized_url}")
        print(f"[info] loaded pages: {pages_loaded}")
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"[error] Failed to fetch feed: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(
            f"[error] {e}\nPlease pass a valid RSS/Atom feed URL or HTML listing URL.",
            file=sys.stderr,
        )
        return 2
    except ET.ParseError as e:
        print(f"[error] Feed XML parse failed: {e}", file=sys.stderr)
        return 2

    if not items:
        print("[info] No torrent items found in this feed.")
        return 0

    os.makedirs(args.out, exist_ok=True)
    history_keys = load_download_history(args.out)
    mark_downloaded(items, args.out, history_keys)
    shown = items[: max(1, args.limit)]
    print(format_table(shown))
    print()
    print("Choose items by index (example: 1,3,5-8). Empty = cancel.")
    raw = input("Selection: ").strip()
    if not raw:
        print("[info] No selection. Exit.")
        return 0

    valid = {it.idx for it in shown}
    try:
        picked_indices = parse_selection(raw, valid)
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    if not picked_indices:
        print("[info] No valid items selected. Exit.")
        return 0

    pick_map = {it.idx: it for it in shown}
    print(f"[info] Downloading {len(picked_indices)} file(s) to: {os.path.abspath(args.out)}")

    ok = 0
    fail = 0
    for idx in picked_indices:
        it = pick_map[idx]
        filename = sanitize_filename(it.name) + ".torrent"
        out_path = os.path.join(args.out, filename)
        try:
            download_file(it.torrent_url, out_path)
            it.downloaded = "Yes"
            history_keys.add(item_history_key(it))
            ok += 1
            print(f"[ok] #{it.idx} {it.name}")
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"[fail] #{it.idx} {it.name} -> {e}", file=sys.stderr)

    save_download_history(args.out, history_keys)
    print(f"[done] success={ok}, failed={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run())
