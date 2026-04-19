#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import tkinter as tk
import urllib.error
from tkinter import filedialog, messagebox, ttk

from torrent_batch_cli import (
    TorrentItem,
    app_base_dir,
    clear_source_cache,
    download_file,
    item_history_key,
    load_download_history,
    load_items_auto,
    mark_downloaded,
    normalize_feed_url,
    normalize_url,
    save_download_history,
    sanitize_filename,
    user_data_dir,
)

DEFAULT_FEED_URL = "https://sukebei.nyaa.si/"
DEFAULT_LIMIT = "5000"
DEFAULT_PAGES = "50"
MAX_RECENT_KEYWORDS = 28


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Torrent Batch Downloader")
        self.root.geometry("1520x760")

        self.items: list[TorrentItem] = []
        self.filtered_items: list[TorrentItem] = []
        self.item_by_iid: dict[str, TorrentItem] = {}
        self.sort_col = "idx"
        self.sort_desc = False
        self.current_limit = 1000
        self.history_keys: set[str] = set()
        self.recent_keywords: list[str] = []
        self.is_loading = False

        self.url_var = tk.StringVar(value=DEFAULT_FEED_URL)
        self.out_var = tk.StringVar(value=self._default_output_dir())
        self.limit_var = tk.StringVar(value=DEFAULT_LIMIT)
        self.pages_var = tk.StringVar(value=DEFAULT_PAGES)
        self.search_var = tk.StringVar(value="")
        self.show_downloaded_only_var = tk.BooleanVar(value=False)
        self.selected_count_var = tk.StringVar(value="Selected: 0")
        self.status_var = tk.StringVar(value="Ready")
        self.progress_var = tk.DoubleVar(value=0.0)

        self._load_settings()
        self._build_ui()
        self.root.after(100, self.load_feed)

    def _default_output_dir(self) -> str:
        downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        if os.path.isdir(downloads_dir):
            return os.path.join(downloads_dir, "TorrentBatchDownloader")
        return os.path.join(os.path.expanduser("~"), "TorrentBatchDownloader", "downloads")

    def _resolve_output_dir(self, raw_path: str) -> str:
        path = (raw_path or "").strip()
        if not path:
            return self._default_output_dir()

        path = os.path.expandvars(os.path.expanduser(path))
        if os.name == "nt":
            # Reject POSIX-rooted paths like /Users/name that often come from other machines.
            if path.startswith(("/", "\\")) and not re.match(r"^[A-Za-z]:[\\/]", path):
                return self._default_output_dir()

        return os.path.abspath(path)

    def _current_output_dir(self) -> str:
        resolved = self._resolve_output_dir(self.out_var.get())
        if self.out_var.get().strip() != resolved:
            self.out_var.set(resolved)
        return resolved

    def _settings_path(self) -> str:
        return os.path.join(user_data_dir(), "app_settings.json")

    def _legacy_settings_path(self) -> str:
        return os.path.join(app_base_dir(), "app_settings.json")

    def _load_settings_file(self, path: str) -> dict[str, object] | None:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _extract_recent_keywords(self, data: dict[str, object]) -> list[str]:
        raw_recent = data.get("recent_keywords", [])
        if isinstance(raw_recent, list):
            return [
                str(x).strip()
                for x in raw_recent
                if str(x).strip()
            ][:MAX_RECENT_KEYWORDS]
        if isinstance(data.get("favorite_keywords", {}), dict):
            ranked = sorted(
                data["favorite_keywords"].items(),
                key=lambda kv: (-int(kv[1]), str(kv[0]).lower()),
            )
            return [str(k).strip() for k, _v in ranked if str(k).strip()][:MAX_RECENT_KEYWORDS]
        return []

    def _load_settings(self) -> None:
        settings_path = self._settings_path()
        legacy_path = self._legacy_settings_path()

        data = self._load_settings_file(settings_path)
        legacy_data = self._load_settings_file(legacy_path)

        if data is None and legacy_data is None:
            return

        primary = data or legacy_data or {}
        self.url_var.set(str(primary.get("feed_url", self.url_var.get())))
        self.out_var.set(self._resolve_output_dir(str(primary.get("output", self.out_var.get()))))
        self.limit_var.set(str(primary.get("limit", self.limit_var.get())))
        self.pages_var.set(str(primary.get("pages", self.pages_var.get())))

        self.recent_keywords = self._extract_recent_keywords(primary)
        if not self.recent_keywords and legacy_data is not None:
            self.recent_keywords = self._extract_recent_keywords(legacy_data)

        if data is None or (
            legacy_data is not None and self._extract_recent_keywords(data or {}) != self.recent_keywords
        ):
            self._save_settings()

    def _save_settings(self) -> None:
        payload = {
            "feed_url": self.url_var.get().strip(),
            "output": self.out_var.get().strip(),
            "limit": self.limit_var.get().strip(),
            "pages": self.pages_var.get().strip(),
            "recent_keywords": self.recent_keywords,
        }
        try:
            os.makedirs(os.path.dirname(self._settings_path()), exist_ok=True)
            with open(self._settings_path(), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _top_keywords(self, limit: int = MAX_RECENT_KEYWORDS) -> list[str]:
        return self.recent_keywords[:limit]

    def _remember_keyword(self, keyword: str) -> None:
        cleaned = " ".join((keyword or "").strip().lower().split())
        if not cleaned:
            return
        self.recent_keywords = [x for x in self.recent_keywords if x != cleaned]
        self.recent_keywords.insert(0, cleaned)
        self.recent_keywords = self.recent_keywords[:MAX_RECENT_KEYWORDS]
        self._save_settings()
        self._refresh_keyword_buttons()

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Feed URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.url_var, width=95).grid(row=0, column=1, columnspan=4, sticky="ew", padx=(8, 8))
        self.load_btn = ttk.Button(top, text="Load", command=self.load_feed)
        self.load_btn.grid(row=0, column=5, sticky="ew")
        self.force_refresh_btn = ttk.Button(top, text="Force Refresh", command=self.force_refresh_feed)
        self.force_refresh_btn.grid(row=0, column=6, sticky="ew", padx=(8, 0))
        self.clear_cache_btn = ttk.Button(top, text="Clear Cache", command=self.clear_feed_cache)
        self.clear_cache_btn.grid(row=0, column=7, sticky="ew", padx=(8, 0))
        ttk.Checkbutton(
            top,
            text="Show Downloaded Only",
            variable=self.show_downloaded_only_var,
            command=self.apply_filter_and_refresh,
        ).grid(row=1, column=6, columnspan=2, sticky="w", padx=(8, 0), pady=(8, 0))

        ttk.Label(top, text="Output").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(top, textvariable=self.out_var, width=95).grid(row=1, column=1, columnspan=3, sticky="ew", padx=(8, 8), pady=(8, 0))
        ttk.Button(top, text="Browse", command=self.pick_output).grid(row=1, column=4, sticky="ew", pady=(8, 0))

        right = ttk.Frame(top)
        right.grid(row=1, column=5, sticky="e", pady=(8, 0))
        ttk.Label(right, text="Limit").grid(row=0, column=0, sticky="w")
        ttk.Entry(right, textvariable=self.limit_var, width=6).grid(row=0, column=1, padx=(6, 10))
        ttk.Label(right, text="Pages").grid(row=0, column=2, sticky="w")
        ttk.Entry(right, textvariable=self.pages_var, width=6).grid(row=0, column=3, padx=(6, 0))

        ttk.Label(top, text="Filter").grid(row=2, column=0, sticky="w", pady=(8, 0))
        search_entry = ttk.Entry(top, textvariable=self.search_var)
        search_entry.grid(row=2, column=1, columnspan=4, sticky="ew", padx=(8, 8), pady=(8, 0))
        search_entry.bind("<KeyRelease>", lambda _e: self.apply_filter_and_refresh())
        search_entry.bind("<Return>", self.apply_filter_keyword)
        ttk.Button(top, text="Clear", command=self.clear_filter).grid(row=2, column=5, sticky="ew", pady=(8, 0))
        ttk.Button(top, text="Copy Names", command=self.copy_selected_names).grid(row=2, column=6, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Button(top, text="Open Output", command=self.open_output_folder).grid(row=2, column=7, sticky="ew", padx=(8, 0), pady=(8, 0))

        ttk.Label(top, text="Keywords").grid(row=3, column=0, sticky="nw", pady=(8, 0))
        self.keyword_frame = ttk.Frame(top)
        self.keyword_frame.grid(row=3, column=1, columnspan=7, sticky="ew", padx=(8, 0), pady=(8, 0))

        for i in range(8):
            top.grid_columnconfigure(i, weight=1 if i in (1, 2, 3) else 0)

        mid = ttk.Frame(self.root, padding=(10, 0, 10, 0))
        mid.pack(fill=tk.BOTH, expand=True)

        cols = ("idx", "name", "date", "size", "seed", "leech", "dl", "done")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("idx", text="#", command=lambda: self.sort_by("idx"))
        self.tree.heading("name", text="Name", command=lambda: self.sort_by("name"))
        self.tree.heading("date", text="Date", command=lambda: self.sort_by("date"))
        self.tree.heading("size", text="Size", command=lambda: self.sort_by("size"))
        self.tree.heading("seed", text="Seeders", command=lambda: self.sort_by("seed"))
        self.tree.heading("leech", text="Leechers", command=lambda: self.sort_by("leech"))
        self.tree.heading("dl", text="Downloads", command=lambda: self.sort_by("dl"))
        self.tree.heading("done", text="Downloaded", command=lambda: self.sort_by("done"))
        self.tree.column("idx", width=55, anchor="center")
        self.tree.column("name", width=800, stretch=True)
        self.tree.column("date", width=150, anchor="center")
        self.tree.column("size", width=115, anchor="center")
        self.tree.column("seed", width=80, anchor="center")
        self.tree.column("leech", width=80, anchor="center")
        self.tree.column("dl", width=90, anchor="center")
        self.tree.column("done", width=95, anchor="center")
        self.tree.tag_configure("warn", foreground="#cc0000")
        self.tree.tag_configure("downloaded", foreground="#0057cc")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.update_selected_count())

        y_scroll = ttk.Scrollbar(mid, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=y_scroll.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        mid.grid_rowconfigure(0, weight=1)
        mid.grid_columnconfigure(0, weight=1)

        bottom = ttk.Frame(self.root, padding=10)
        bottom.pack(fill=tk.X)
        self.progress = ttk.Progressbar(bottom, orient=tk.HORIZONTAL, mode="determinate", maximum=100, variable=self.progress_var)
        self.progress.pack(fill=tk.X, pady=(0, 10))

        ttk.Button(bottom, text="Select All", command=self.select_all).pack(side=tk.LEFT)
        ttk.Button(bottom, text="Clear Selection", command=self.clear_selection).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(bottom, textvariable=self.selected_count_var).pack(side=tk.LEFT, padx=(12, 0))
        self.download_btn = ttk.Button(bottom, text="Download Selected", command=self.download_selected)
        self.download_btn.pack(side=tk.RIGHT)
        ttk.Label(bottom, textvariable=self.status_var).pack(side=tk.LEFT, padx=(12, 0))

        self._refresh_keyword_buttons()

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def set_progress(self, percent: float) -> None:
        self.progress_var.set(max(0.0, min(100.0, percent)))

    def set_downloading(self, is_downloading: bool) -> None:
        state = tk.DISABLED if is_downloading else tk.NORMAL
        self.download_btn.configure(state=state)

    def set_loading(self, is_loading: bool) -> None:
        self.is_loading = is_loading
        state = tk.DISABLED if is_loading else tk.NORMAL
        self.load_btn.configure(state=state)
        self.force_refresh_btn.configure(state=state)
        self.clear_cache_btn.configure(state=state)

    def pick_output(self) -> None:
        folder = filedialog.askdirectory(initialdir=self._current_output_dir())
        if folder:
            self.out_var.set(self._resolve_output_dir(folder))
            self.history_keys = load_download_history(self._current_output_dir())
            self._save_settings()
            if self.items:
                mark_downloaded(self.items, self._current_output_dir(), self.history_keys)
                self.apply_filter_and_refresh()

    def clear_table(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.item_by_iid.clear()
        self.update_selected_count()

    def _refresh_keyword_buttons(self) -> None:
        for child in self.keyword_frame.winfo_children():
            child.destroy()

        keywords = self._top_keywords()
        if not keywords:
            ttk.Label(self.keyword_frame, text="No recent keywords yet. Press Enter in Filter to save one.").grid(row=0, column=0, sticky="w")
            return

        max_cols = 14
        for idx, keyword in enumerate(keywords):
            row = idx // max_cols
            col = idx % max_cols
            ttk.Button(
                self.keyword_frame,
                text=keyword,
                command=lambda kw=keyword: self.use_saved_keyword(kw),
            ).grid(row=row, column=col, sticky="w", padx=(0, 4), pady=(0, 4))

    def update_selected_count(self) -> None:
        self.selected_count_var.set(f"Selected: {len(self.tree.selection())}")

    def apply_filter_keyword(self, _event: tk.Event | None = None) -> str | None:
        keyword = self.search_var.get().strip()
        self.apply_filter_and_refresh()
        self._remember_keyword(keyword)
        return "break"

    def use_saved_keyword(self, keyword: str) -> None:
        self.search_var.set(keyword)
        self.apply_filter_and_refresh()
        self._remember_keyword(keyword)

    def _to_int(self, value: str) -> int:
        m = re.search(r"-?\d+", value or "")
        return int(m.group(0)) if m else 0

    def _to_size_bytes(self, value: str) -> int:
        text = (value or "").strip().lower()
        m = re.match(r"^\s*([\d.]+)\s*([kmgtp]?i?b)\s*$", text)
        if not m:
            return 0
        num = float(m.group(1))
        unit = m.group(2)
        scale = {
            "b": 1,
            "kib": 1024,
            "mib": 1024**2,
            "gib": 1024**3,
            "tib": 1024**4,
            "pib": 1024**5,
            "kb": 1000,
            "mb": 1000**2,
            "gb": 1000**3,
            "tb": 1000**4,
            "pb": 1000**5,
        }
        return int(num * scale.get(unit, 1))

    def _sorted_items(self, items: list[TorrentItem]) -> list[TorrentItem]:
        if self.sort_col == "name":
            def key_fn(it: TorrentItem) -> str:
                return it.name.lower()
        elif self.sort_col == "date":
            def key_fn(it: TorrentItem) -> tuple[int, str]:
                return it.timestamp, it.date.lower()
        elif self.sort_col == "size":
            def key_fn(it: TorrentItem) -> int:
                return self._to_size_bytes(it.size)
        elif self.sort_col == "seed":
            def key_fn(it: TorrentItem) -> int:
                return self._to_int(it.seeders)
        elif self.sort_col == "leech":
            def key_fn(it: TorrentItem) -> int:
                return self._to_int(it.leechers)
        elif self.sort_col == "dl":
            def key_fn(it: TorrentItem) -> int:
                return self._to_int(it.downloads)
        elif self.sort_col == "done":
            def key_fn(it: TorrentItem) -> int:
                return 1 if it.downloaded == "Yes" else 0
        else:
            def key_fn(it: TorrentItem) -> int:
                return it.idx
        return sorted(items, key=key_fn, reverse=self.sort_desc)

    def clear_filter(self) -> None:
        self.search_var.set("")
        self.apply_filter_and_refresh()

    def apply_filter_and_refresh(self) -> None:
        q = self.search_var.get().strip().lower()
        base = self.items
        if self.show_downloaded_only_var.get():
            base = [it for it in base if it.downloaded == "Yes"]
        if q:
            base = [
                it
                for it in self.items
                if (not self.show_downloaded_only_var.get() or it.downloaded == "Yes")
                and (q in it.name.lower() or q in it.date.lower() or q in it.downloaded.lower())
            ]
        self.filtered_items = self._sorted_items(base)
        self.populate_table(self.filtered_items, self.current_limit, preserve_status=True)

    def sort_by(self, col: str) -> None:
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col = col
            self.sort_desc = False
        self.apply_filter_and_refresh()

    def populate_table(self, items: list[TorrentItem], limit: int, preserve_status: bool = False) -> None:
        self.clear_table()
        shown = items[: max(1, limit)]
        for it in shown:
            iid = str(it.idx)
            warn = self._to_size_bytes(it.size) > 3 * 1024**3
            if it.downloaded == "Yes":
                tags = ("downloaded",)
            elif warn:
                tags = ("warn",)
            else:
                tags = ()
            self.tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(it.idx, it.name, it.date, it.size, it.seeders, it.leechers, it.downloads, it.downloaded),
                tags=tags,
            )
            self.item_by_iid[iid] = it
        self.update_selected_count()
        if not preserve_status:
            self.set_status(f"Loaded {len(shown)} item(s)")

    def load_feed(self) -> None:
        self._start_load_feed(refresh_all=False, clear_cache=False)

    def force_refresh_feed(self) -> None:
        self._start_load_feed(refresh_all=True, clear_cache=False)

    def clear_feed_cache(self) -> None:
        self._start_load_feed(refresh_all=True, clear_cache=True)

    def _start_load_feed(self, refresh_all: bool, clear_cache: bool) -> None:
        if self.is_loading:
            messagebox.showinfo("Info", "A feed is already loading. Please wait for it to finish.")
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("Error", "Please enter feed URL.")
            return
        try:
            limit = int(self.limit_var.get().strip())
            if limit < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Limit must be an integer >= 1.")
            return
        try:
            pages = int(self.pages_var.get().strip())
            if pages < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Pages must be an integer >= 1.")
            return

        self.current_limit = limit
        self._save_settings()
        self.set_loading(True)
        if clear_cache:
            self.set_status("Clearing cache and loading feed...")
        elif refresh_all:
            self.set_status("Force refreshing feed...")
        else:
            self.set_status("Loading feed...")
        out_dir = self._current_output_dir()
        threading.Thread(
            target=self._load_feed_worker,
            args=(url, limit, pages, refresh_all, clear_cache, out_dir),
            daemon=True,
        ).start()

    def _load_feed_worker(
        self,
        url: str,
        limit: int,
        pages: int,
        refresh_all: bool,
        clear_cache: bool,
        out_dir: str,
    ) -> None:
        try:
            if clear_cache:
                normalized_html_url = normalize_url(url)
                normalized_feed_url = normalize_url(normalize_feed_url(url))
                removed = clear_source_cache(normalized_url=normalized_html_url, source_kind="html")
                removed += clear_source_cache(normalized_url=normalized_feed_url, source_kind="feed")
                self.root.after(0, lambda: self.set_status(f"Cleared {removed} cache entries, loading..."))

            items, pages_loaded, normalized = load_items_auto(url, pages, refresh_all=refresh_all)

            if not items:
                raise ValueError("No torrent items found in this feed.")

            self.history_keys = load_download_history(out_dir)
            mark_downloaded(items, out_dir, self.history_keys)
            self.items = items
            self.root.after(0, self.apply_filter_and_refresh)

            status = f"Loaded {min(len(items), max(1, limit))} shown / {len(items)} total from {pages_loaded} page(s)"
            if normalized != url:
                status += f" (normalized: {normalized})"
            self.root.after(0, lambda: self.set_status(status))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                msg = (
                    "HTTP 404 from target URL.\n\n"
                    "Likely network-path/client restriction for this environment. "
                    "If curl also returns 404, this is not a parser bug."
                )
            else:
                msg = f"HTTP {e.code}: {e.reason}"
            self.root.after(0, lambda: messagebox.showerror("Load failed", msg))
            self.root.after(0, lambda: self.set_status("Load failed"))
        except Exception as e:  # noqa: BLE001
            error_text = str(e)
            self.root.after(0, lambda msg=error_text: messagebox.showerror("Load failed", msg))
            self.root.after(0, lambda: self.set_status("Load failed"))
        finally:
            self.root.after(0, lambda: self.set_loading(False))

    def select_all(self) -> None:
        iids = self.tree.get_children()
        self.tree.selection_set(iids)
        self.update_selected_count()
        self.set_status(f"Selected {len(iids)} item(s)")

    def clear_selection(self) -> None:
        self.tree.selection_remove(self.tree.selection())
        self.update_selected_count()
        self.set_status("Selection cleared")

    def copy_selected_names(self) -> None:
        selected_iids = list(self.tree.selection())
        selected_names = [
            self.item_by_iid[iid].name
            for iid in selected_iids
            if iid in self.item_by_iid
        ]
        if not selected_names:
            messagebox.showinfo("Info", "Please select at least one item.")
            return
        text = "\n".join(selected_names)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        self.set_status(f"Copied {len(selected_names)} name(s)")

    def open_output_folder(self) -> None:
        out_dir = self._current_output_dir()
        try:
            os.makedirs(out_dir, exist_ok=True)
            if os.name == "nt":
                os.startfile(out_dir)
            else:
                subprocess.Popen(["xdg-open", out_dir])
            self.set_status("Opened output folder")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Error", f"Cannot open output folder:\n{out_dir}\n\n{e}")
            self.set_status("Open output folder failed")

    def download_selected(self) -> None:
        selected_iids = list(self.tree.selection())
        if not selected_iids:
            messagebox.showinfo("Info", "Please select at least one item.")
            return

        out_dir = self._current_output_dir()
        try:
            os.makedirs(out_dir, exist_ok=True)
        except PermissionError:
            messagebox.showerror("Error", f"Cannot create output folder:\n{out_dir}")
            self.set_status("Invalid output folder")
            return
        if not self.history_keys:
            self.history_keys = load_download_history(out_dir)

        selected = [self.item_by_iid[iid] for iid in selected_iids if iid in self.item_by_iid]
        self.set_status(f"Downloading {len(selected)} item(s)...")
        self.set_progress(0)
        self.set_downloading(True)
        threading.Thread(target=self._download_worker, args=(selected, out_dir), daemon=True).start()

    def _download_worker(self, selected: list[TorrentItem], out_dir: str) -> None:
        ok = 0
        fail = 0
        errors: list[str] = []
        total = len(selected)

        for i, it in enumerate(selected, start=1):
            self.root.after(0, lambda i=i, total=total, it=it: self.set_status(f"Downloading {i}/{total}: {it.name}"))
            out_path = os.path.join(out_dir, f"{sanitize_filename(it.name)}.torrent")

            try:
                download_file(it.torrent_url, out_path)
                it.downloaded = "Yes"
                self.history_keys.add(item_history_key(it))
                ok += 1
            except Exception as e:  # noqa: BLE001
                fail += 1
                errors.append(f"#{it.idx} {it.name}: {e}")

            self.root.after(0, lambda i=i, total=total: self.set_progress((i / total) * 100.0))

        history_error: str | None = None
        try:
            save_download_history(out_dir, self.history_keys)
        except Exception as e:  # noqa: BLE001
            history_error = str(e)
            fail += 1
            errors.append(f"Failed to save download history: {e}")
        finally:
            msg = f"Done. success={ok}, failed={fail}"
            if history_error:
                msg += " (history save failed)"
            self.root.after(0, self.apply_filter_and_refresh)
            self.root.after(0, lambda: self.set_status(msg))
            self.root.after(0, lambda: self.set_progress(100.0))
            self.root.after(0, lambda: self.set_downloading(False))

        if errors:
            details = "\n".join(errors[:8])
            if len(errors) > 8:
                details += f"\n... and {len(errors) - 8} more"
            self.root.after(0, lambda: messagebox.showwarning("Download finished with errors", f"{msg}\n\n{details}"))
        else:
            self.root.after(0, lambda: messagebox.showinfo("Download finished", msg))


def main() -> int:
    root = tk.Tk()
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

