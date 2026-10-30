#!/usr/bin/env python3
"""Persistent local backend for the Vicinae MDict extension.

The MDict container format is handled exclusively by mdict-mquery. This module
adds directory discovery, cache placement, search, sanitization, and JSON IPC.
"""

from __future__ import annotations

import argparse
import colorsys
import gzip
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import unquote


# mdict-mquery ships a pure-Python lzo.py next to its package but imports it as
# a top-level module. Adding that directory first enables MDict 1.2/LZO files
# without a compiler or system Python package.
for site_dir in list(sys.path):
    candidate = Path(site_dir) / "mdict_mquery"
    if candidate.is_dir():
        sys.path.insert(0, str(candidate))
        break

from bs4 import BeautifulSoup, NavigableString, Tag  # noqa: E402
import mdict_mquery as mdict_backend  # noqa: E402
from mdict_mquery import IndexBuilder, MDD  # noqa: E402
from rapidfuzz import fuzz, process  # noqa: E402
import soupsieve  # noqa: E402


PARSER_VERSION = "mdict-mquery-2.1.0"
RENDER_CACHE_VERSION = "full-css-v7-new-oriental-links"
# mdict-mquery 2.1.0's index writer references a module-level version value
# that the wheel omits. Supplying the package version restores its metadata
# write and keeps the cache self-describing.
mdict_backend.version = PARSER_VERSION
ACTIVE_TAGS = {"script", "noscript", "iframe", "frame", "frameset", "object", "embed", "applet", "form", "base", "meta"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".spx"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}
CSS_EXTENSIONS = {".css"}
RESOURCE_SCHEMES = ("sound://", "file://", "res://")
ENTRY_SCHEMES = ("entry://", "bword://", "mdict://")


def json_line(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def file_signature(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def safe_component(value: str) -> str:
    visible = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip("-.")[:48] or "dictionary"
    return visible


def normalized_resource_key(value: str) -> str:
    value = html.unescape(value).strip().strip('"\'')
    lower = value.lower()
    for scheme in RESOURCE_SCHEMES:
        if lower.startswith(scheme):
            value = value[len(scheme):]
            break
    value = value.split("?", 1)[0].split("#", 1)[0]
    value = value.replace("/", "\\")
    value = re.sub(r"^\\+", "", value)
    return "\\" + value


def resource_candidates(value: str) -> list[str]:
    canonical = normalized_resource_key(value)
    bare = canonical.lstrip("\\")
    variants = [canonical, "\\" + bare, bare, "/" + bare.replace("\\", "/"), bare.replace("\\", "/")]
    result: list[str] = []
    for item in variants:
        if item not in result:
            result.append(item)
    return result


def internal_entry_target(value: str) -> str | None:
    decoded = html.unescape(value).strip().strip('"\'')
    lower = decoded.casefold()
    for scheme in ENTRY_SCHEMES:
        if lower.startswith(scheme):
            target = unquote(decoded[len(scheme):]).split("#", 1)[0].split("?", 1)[0].strip()
            return target or None
    return None


def discover_mdds(mdx_path: Path) -> list[Path]:
    stem = re.escape(mdx_path.stem)
    pattern = re.compile(rf"^{stem}(?:\.(\d+))?\.mdd$", re.IGNORECASE)
    found: list[tuple[int, str, Path]] = []
    for candidate in mdx_path.parent.iterdir():
        if not candidate.is_file():
            continue
        match = pattern.match(candidate.name)
        if match:
            number = int(match.group(1) or 0)
            found.append((number, candidate.name.casefold(), candidate.resolve()))
    return [item[2] for item in sorted(found)]


class ResourceIndex:
    def __init__(self, source: Path, db_path: Path, force: bool = False):
        self.source = source
        self.db_path = db_path
        if force or not db_path.exists():
            self._build()

    def _build(self) -> None:
        temporary = self.db_path.with_suffix(self.db_path.suffix + ".tmp")
        temporary.unlink(missing_ok=True)
        parsed = MDD(str(self.source)).get_index(check_block=False)
        connection = sqlite3.connect(temporary)
        try:
            connection.execute(
                "CREATE TABLE resources (key_text TEXT NOT NULL COLLATE NOCASE, file_pos INTEGER, "
                "compressed_size INTEGER, decompressed_size INTEGER, record_block_type INTEGER, "
                "record_start INTEGER, record_end INTEGER, offset INTEGER)"
            )
            connection.executemany(
                "INSERT INTO resources VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        row["key_text"], row["file_pos"], row["compressed_size"],
                        row["decompressed_size"], row["record_block_type"], row["record_start"],
                        row["record_end"], row["offset"],
                    )
                    for row in parsed
                ],
            )
            connection.execute("CREATE INDEX resource_key ON resources(key_text COLLATE NOCASE)")
            connection.commit()
        finally:
            connection.close()
        temporary.replace(self.db_path)

    def lookup(self, key: str) -> bytes | None:
        connection = sqlite3.connect(self.db_path)
        try:
            for candidate in resource_candidates(key):
                row = connection.execute(
                    "SELECT file_pos, compressed_size, decompressed_size, record_block_type, "
                    "record_start, record_end, offset FROM resources WHERE key_text = ? COLLATE NOCASE LIMIT 1",
                    (candidate,),
                ).fetchone()
                if row:
                    index = {
                        "file_pos": row[0], "compressed_size": row[1], "decompressed_size": row[2],
                        "record_block_type": row[3], "record_start": row[4], "record_end": row[5], "offset": row[6],
                    }
                    with self.source.open("rb") as handle:
                        return IndexBuilder.get_data_by_index(handle, index)
        finally:
            connection.close()
        return None


@dataclass
class Dictionary:
    source: Path
    cache_root: Path
    force: bool = False
    title: str = ""
    error: str | None = None
    builder: IndexBuilder | None = None
    resources: list[ResourceIndex] = field(default_factory=list)
    entry_count: int = 0

    def __post_init__(self) -> None:
        self.title = self.source.stem
        try:
            self._load()
        except Exception as exc:  # isolate a damaged dictionary from all others
            self.error = f"{type(exc).__name__}: {exc}"
            self.builder = None

    @property
    def identifier(self) -> str:
        return hashlib.sha256(str(self.source.resolve()).encode("utf-8")).hexdigest()[:16]

    @property
    def keys(self) -> list[str]:
        return self.builder.sorted_keys if self.builder else []

    def find_key(self, word: str) -> str | None:
        """Resolve an exact headword without decompressing its record block."""
        if not self.builder or not self.builder._conn:
            return None
        variants = [word, word.lower(), word.title(), word.upper()]
        for variant in dict.fromkeys(variants):
            row = self.builder._conn.execute(
                "SELECT key_text FROM MDX_INDEX WHERE key_text = ? LIMIT 1", (variant,)
            ).fetchone()
            if row:
                return str(row[0])
        folded = word.casefold()
        return next((key for key in self.prefix(word, 64) if key.casefold() == folded), None)

    def _load(self) -> None:
        dictionary_cache = self.cache_root / f"{safe_component(self.source.stem)}-{self.identifier}"
        dictionary_cache.mkdir(parents=True, exist_ok=True)
        mdx_link = dictionary_cache / "dictionary.mdx"
        mdds = discover_mdds(self.source)
        signature = {
            "parser": PARSER_VERSION,
            "mdx": file_signature(self.source),
            "mdds": [file_signature(item) for item in mdds],
        }
        manifest_path = dictionary_cache / "manifest.json"
        old_signature: dict[str, Any] | None = None
        if manifest_path.exists():
            try:
                old_signature = json.loads(manifest_path.read_text("utf-8"))
            except Exception:
                old_signature = None
        stale = self.force or old_signature != signature
        if stale:
            for candidate in dictionary_cache.glob("*.db"):
                candidate.unlink(missing_ok=True)
            for candidate in dictionary_cache.glob("*.cache"):
                candidate.unlink(missing_ok=True)
            shutil.rmtree(dictionary_cache / "resources", ignore_errors=True)
        if mdx_link.is_symlink() or mdx_link.exists():
            if not mdx_link.is_symlink() or mdx_link.resolve() != self.source.resolve():
                mdx_link.unlink()
        if not mdx_link.exists():
            mdx_link.symlink_to(self.source.resolve())
        self._write_compact_key_cache(mdx_link)
        self.builder = IndexBuilder(str(mdx_link), force_rebuild=False)
        if self.builder._conn:
            self.entry_count = int(self.builder._conn.execute("SELECT COUNT(*) FROM MDX_INDEX").fetchone()[0])
        # Exact and prefix search use SQLite directly. Keeping millions of
        # duplicate headwords in a second pickle added 83 MB for TLD alone.
        self.builder.sorted_keys = []
        self.builder.kanji_index = {}
        self.builder.key_set = mdict_backend._SortedKeySet([])
        self._write_compact_key_cache(mdx_link)
        self.title = (getattr(self.builder, "_title", "") or self.source.stem).strip()
        for position, mdd in enumerate(mdds):
            self.resources.append(ResourceIndex(mdd, dictionary_cache / f"resource-{position}.db", force=stale))
        manifest_path.write_text(json.dumps(signature, ensure_ascii=False, indent=2), "utf-8")

    @staticmethod
    def _write_compact_key_cache(mdx_link: Path) -> None:
        """Keep mdict-mquery on its fast cache path without duplicating SQLite keys."""
        import pickle
        import time

        database = Path(str(mdx_link) + ".db")
        target = Path(str(mdx_link) + ".cache")
        if not database.exists():
            return
        if target.exists() and target.stat().st_size < 256 and target.stat().st_mtime > database.stat().st_mtime:
            return
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("wb") as handle:
            pickle.dump({"sk": [], "kx": {}}, handle, protocol=pickle.HIGHEST_PROTOCOL)
        temporary.replace(target)
        timestamp = max(time.time(), database.stat().st_mtime + 1.0)
        os.utime(target, (timestamp, timestamp))

    def exact(self, word: str) -> list[str]:
        if not self.builder:
            return []
        candidate = self.find_key(word)
        result = self.builder.mdx_lookup(candidate) if candidate else []
        hops = 0
        while result and hops < 8:
            redirect = re.fullmatch(r"\s*@@@LINK=(.+?)\s*", result[0], flags=re.IGNORECASE | re.DOTALL)
            if not redirect:
                break
            word = redirect.group(1).strip()
            result = self.builder.mdx_lookup(word, ignorecase=True)
            hops += 1
        return result

    def prefix(self, query: str, limit: int) -> list[str]:
        if not self.builder or not self.builder._conn or not query:
            return []
        folded = query.casefold()
        matches: list[str] = []
        variants = dict.fromkeys((query, query.lower(), query.title(), query.upper()))
        for variant in variants:
            upper = variant + "\U0010ffff"
            rows = self.builder._conn.execute(
                "SELECT DISTINCT key_text FROM MDX_INDEX WHERE key_text >= ? AND key_text < ? ORDER BY key_text LIMIT ?",
                (variant, upper, limit * 8 + 32),
            )
            for row in rows:
                key = str(row[0])
                if key.casefold().startswith(folded) and key not in matches:
                    matches.append(key)
                    if len(matches) >= limit:
                        return matches
        return matches

    def resource(self, reference: str) -> tuple[bytes, str] | None:
        for store in self.resources:
            payload = store.lookup(reference)
            if payload is not None:
                return payload, normalized_resource_key(reference)
        # Many GoldenDict bundles keep CSS, icons, and audio as ordinary files
        # beside the MDX instead of packing them into an MDD. Treat that layout
        # as another resource store, while preventing references from escaping
        # the dictionary's own directory.
        canonical = normalized_resource_key(unquote(reference))
        relative = canonical.lstrip("\\").replace("\\", "/")
        root = self.source.parent.resolve()
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        if candidate.is_file():
            return candidate.read_bytes(), canonical
        return None


class Backend:
    def __init__(self, config: dict[str, Any], force: bool = False):
        self.folder = Path(os.path.expandvars(os.path.expanduser(config["dictionaryFolder"]))).resolve()
        self.cache = Path(config["cacheDir"]).resolve()
        self.cache.mkdir(parents=True, exist_ok=True)
        self.max_suggestions = max(1, min(int(config.get("maxSuggestions", 30)), 200))
        self.fuzzy = bool(config.get("fuzzySearch", True))
        self.appearance = str(config.get("appearance", "light")).casefold()
        self.enabled = self._csv(config.get("enabledDictionaries", ""))
        self.order = self._csv(config.get("dictionaryOrder", ""))
        self.disabled_paths = {str(Path(item).resolve()).casefold() for item in config.get("disabledDictionaries", [])}
        self.saved_order = [str(Path(item).resolve()).casefold() for item in config.get("orderedDictionaries", [])]
        self.dictionaries: list[Dictionary] = []
        self.scan_errors: list[dict[str, str]] = []
        self._css_cache: dict[tuple[str, str], tuple[str, list[tuple[str, str]]]] = {}
        self._selector_cache: dict[str, Any] = {}
        self._render_cache: OrderedDict[
            tuple[str, str], tuple[str, str, str | None, list[str], list[dict[str, str]]]
        ] = OrderedDict()
        self._lookup_cache: OrderedDict[tuple[str, str, bool], dict[str, Any]] = OrderedDict()
        self._cache_lock = RLock()
        rendered_base = self.cache / "rendered-html"
        rendered_base.mkdir(parents=True, exist_ok=True)
        for old_version in rendered_base.iterdir():
            if old_version.is_dir() and old_version.name != RENDER_CACHE_VERSION:
                shutil.rmtree(old_version, ignore_errors=True)
        self._render_disk_root = rendered_base / RENDER_CACHE_VERSION
        self._render_disk_dir = self._render_disk_root / self.appearance
        self._render_disk_dir.mkdir(parents=True, exist_ok=True)
        self._prune_render_disk_cache()
        self._scan(force)

    @staticmethod
    def _csv(value: str) -> list[str]:
        return [item.strip().casefold() for item in value.split(",") if item.strip()]

    def _scan(self, force: bool) -> None:
        if not self.folder.is_dir():
            self.scan_errors.append({"dictionary": str(self.folder), "error": "Dictionary folder does not exist"})
            return
        paths = sorted(
            (item.resolve() for item in self.folder.rglob("*") if item.is_file() and item.suffix.casefold() == ".mdx"),
            key=lambda item: str(item).casefold(),
        )
        for path in paths:
            if str(path).casefold() in self.disabled_paths:
                continue
            if self.enabled and path.stem.casefold() not in self.enabled and path.name.casefold() not in self.enabled:
                continue
            dictionary = Dictionary(path, self.cache, force)
            if dictionary.error:
                self.scan_errors.append({"dictionary": path.name, "error": dictionary.error})
            else:
                self.dictionaries.append(dictionary)
        path_rank = {name: position for position, name in enumerate(self.saved_order)}
        rank = {name: position for position, name in enumerate(self.order)}
        self.dictionaries.sort(
            key=lambda item: (
                path_rank.get(str(item.source.resolve()).casefold(), 10_000),
                min(rank.get(item.title.casefold(), 10_000), rank.get(item.source.stem.casefold(), 10_000)),
                item.title.casefold(),
            )
        )

    def status(self) -> dict[str, Any]:
        return {
            "folder": str(self.folder),
            "dictionaries": [
                {"name": item.title, "file": str(item.source), "entries": item.entry_count, "mddCount": len(item.resources)}
                for item in self.dictionaries
            ],
            "errors": self.scan_errors,
        }

    def suggestions(self, query: str) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        groups: list[dict[str, Any]] = []
        for priority, dictionary in enumerate(self.dictionaries, start=1):
            by_word: dict[str, dict[str, Any]] = {}
            exact_key = dictionary.find_key(query)
            if exact_key:
                by_word[exact_key.casefold()] = {"word": exact_key, "exact": True}
            for key in dictionary.prefix(query, self.max_suggestions):
                folded = key.casefold()
                by_word.setdefault(folded, {"word": key, "exact": folded == query.casefold()})
            if self.fuzzy and len(by_word) < self.max_suggestions:
                cutoff = 75 if len(query) >= 4 else 85
                remaining = self.max_suggestions - len(by_word)
                anchor_length = max(1, min(3, len(query) // 2))
                candidates = dictionary.prefix(query[:anchor_length], 5_000)
                for key, score, _ in process.extract(query, candidates, scorer=fuzz.WRatio, limit=remaining * 2, score_cutoff=cutoff):
                    folded = key.casefold()
                    by_word.setdefault(folded, {"word": key, "exact": folded == query.casefold()})
            values = list(by_word.values())
            values.sort(key=lambda item: (not item["exact"], not item["word"].casefold().startswith(query.casefold()), len(item["word"]), item["word"].casefold()))
            if values:
                groups.append({"dictionary": dictionary.title, "priority": priority, "items": values[: self.max_suggestions]})
        return groups

    def _extract_resource(self, dictionary: Dictionary, reference: str) -> str | None:
        found = dictionary.resource(reference)
        if not found:
            return None
        payload, canonical = found
        suffix = Path(canonical.replace("\\", "/")).suffix.lower()
        digest = hashlib.sha256((dictionary.identifier + "\0" + canonical.casefold()).encode("utf-8")).hexdigest()[:24]
        target_dir = self.cache / "rendered-resources" / dictionary.identifier
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{digest}{suffix}"
        if not target.exists() or target.stat().st_size != len(payload):
            target.write_bytes(payload)
        return str(target)

    def _rewrite_css_resources(self, dictionary: Dictionary, css_text: str) -> str:
        css_text = re.sub(r"/\*.*?\*/", "", css_text, flags=re.DOTALL)
        css_text = re.sub(r"@import\s+(?:url\([^)]*\)|[^;]+);?", "", css_text, flags=re.IGNORECASE)
        css_text = re.sub(r"expression\s*\([^)]*\)", "", css_text, flags=re.IGNORECASE)
        css_text = re.sub(r"(?:behavior|-moz-binding)\s*:[^;}]+;?", "", css_text, flags=re.IGNORECASE)

        def replace(match: re.Match[str]) -> str:
            quote = match.group(1) or ""
            reference = match.group(2).strip()
            lower = reference.casefold()
            if lower.startswith("data:") or reference.startswith("#"):
                return match.group(0)
            if lower.startswith(("http://", "https://", "javascript:", "vbscript:")):
                return "url()"
            local = self._extract_resource(dictionary, reference)
            return f"url({quote}{Path(local).as_uri()}{quote})" if local else "url()"

        return re.sub(r"url\(\s*(['\"]?)(.*?)\1\s*\)", replace, css_text, flags=re.IGNORECASE)

    def _prepare_css(self, dictionary: Dictionary, css_text: str) -> tuple[str, list[tuple[str, str]]]:
        digest = hashlib.sha256(css_text.encode("utf-8", errors="ignore")).hexdigest()
        key = (dictionary.identifier, digest)
        with self._cache_lock:
            cached = self._css_cache.get(key)
        if cached is not None:
            return cached
        safe_css = self._rewrite_css_resources(dictionary, css_text)
        rules: list[tuple[str, str]] = []
        for selector_text, declaration in re.findall(r"([^{}]+)\{([^{}]*)\}", safe_css):
            declaration = declaration.strip()
            if not declaration:
                continue
            for selector in selector_text.split(","):
                selector = selector.strip()
                if selector and not selector.startswith("@") and not re.search(r":{1,2}(?:before|after)\b", selector):
                    rules.append((selector, declaration))
        cached = (safe_css, rules)
        with self._cache_lock:
            self._css_cache[key] = cached
        return cached

    def _apply_qt_compatibility(self, soup: BeautifulSoup) -> None:
        """Replace browser-only dictionary widgets with stable rich-text markup."""
        # New Oriental relies on CSS margins between inline links and
        # JavaScript to switch entries. Qt ignores those margins and scripts
        # are deliberately disabled, so materialize readable separators and
        # use a compact native-looking heading.
        for root in soup.select(".MdxNewOrientalRootsAffixes"):
            heading = root.find("h2")
            if heading is not None:
                existing = str(heading.get("style", "")).strip().rstrip(";")
                layout = "text-align:left;font-size:20px;margin:8px 0 10px"
                heading["style"] = f"{existing};{layout}" if existing else layout
            for words in root.select(".head-words"):
                delimiters = words.select(".word-delimiter")
                for delimiter in delimiters:
                    delimiter.decompose()
                existing = str(words.get("style", "")).strip().rstrip(";")
                layout = "line-height:1.8;text-align:left;padding:3px 0"
                words["style"] = f"{existing};{layout}" if existing else layout
                links = words.find_all("a", recursive=False)
                for link in links[1:]:
                    link.insert_before(NavigableString(" · "))
                for link in links:
                    existing_link = str(link.get("style", "")).strip().rstrip(";")
                    link_style = "font-weight:bold;text-decoration:underline"
                    if self.appearance == "dark":
                        link_style += ";color:#8db7ff !important"
                    link["style"] = f"{existing_link};{link_style}" if existing_link else link_style
                for selected in words.select(".key-head-word"):
                    existing_selected = str(selected.get("style", "")).strip().rstrip(";")
                    selected_style = "background-color:transparent !important"
                    if self.appearance == "dark":
                        selected_style += ";color:#ff8585 !important"
                    selected["style"] = f"{existing_selected};{selected_style}"
            for paragraph in root.select("p.meaning, p.derive"):
                existing = str(paragraph.get("style", "")).strip().rstrip(";")
                paragraph["style"] = f"{existing};display:block;margin:3px 0"
        # Longman ships this panel collapsed and relies on JavaScript to open
        # it. JavaScript is intentionally disabled, so do not render its hidden
        # contents as one unspaced line.
        for element in soup.select(".wordfams, .dictionary_intro"):
            element.decompose()

        # Longman's encyclopedic entries repeat their round part-of-speech
        # badge and position the second copy absolutely over the definition.
        # Qt Rich Text ignores that positioning, producing strings such as
        # "Nthe largest...". Keep the header badge and make it a proper row.
        for entry in soup.select(".ldoceEntry.LDOCEVERSION_new"):
            badges = entry.select(".LDOCEVERSIONLOGO_new")
            for duplicate in badges[1:]:
                duplicate.decompose()
            head = entry.find("span", class_="Head", recursive=False)
            if head is not None:
                head.name = "div"
                existing = str(head.get("style", "")).strip().rstrip(";")
                head["style"] = f"{existing};display:block;margin:6px 0 3px" if existing else "display:block;margin:6px 0 3px"

        # The Little Dict builds its frequency rows with floats and :before /
        # :after labels. Qt Rich Text supports neither reliably, so materialize
        # the same information as a compact table.
        pos_names = {"n": "N.", "v": "V.", "j": "ADJ.", "a": "ADJ.", "r": "ADV.", "p": "PREP."}
        for container in soup.select(".coca"):
            rows: list[tuple[str, str, str]] = []
            for position in container.find_all("span", class_="pos", recursive=False):
                rank = position.find_next_sibling("span", class_="rank")
                total = position.find_next_sibling("div", class_="total")
                if rank and total:
                    code = position.get_text(" ", strip=True).casefold()
                    rows.append((pos_names.get(code, code.upper()), rank.get_text(" ", strip=True), total.get_text(" ", strip=True)))
            if not rows:
                continue
            table = soup.new_tag("table")
            table["cellpadding"] = "2"
            table["cellspacing"] = "0"
            table["style"] = "margin:4px 0;border-collapse:collapse"
            for label, rank_text, frequency_text in rows:
                row = soup.new_tag("tr")
                for text, style in (
                    (label, "color:#fff;background-color:#35a3ff;font-weight:bold;padding:1px 5px"),
                    (f"RANK {rank_text}", "padding-left:10px;color:#333"),
                    (f"FREQ {frequency_text}", "padding-left:18px;color:#333"),
                ):
                    cell = soup.new_tag("td")
                    cell["style"] = style
                    cell.string = text
                    row.append(cell)
                table.append(row)
            container.clear()
            container.append(table)

        for frequency in soup.select(".word-frequency"):
            levels = frequency.find_all("div", class_="level", recursive=False)
            for level in levels:
                level.decompose()
            if levels:
                dots = soup.new_tag("span")
                dots["style"] = "color:#f5a623;font-size:16px;margin-left:6px"
                dots.string = "●" * len(levels)
                frequency.append(dots)
            for label in frequency.find_all("span", class_="label", recursive=False):
                label.insert_after(NavigableString(" "))

        colors = ("#e75239", "#ff961c", "#ffcc27", "#fce62f", "#cde35b", "#82cc33", "#41bece", "#049cdb")
        for rule in soup.select(".hwrap hr"):
            table = soup.new_tag("table")
            table["width"] = "100%"
            table["cellpadding"] = "0"
            table["cellspacing"] = "0"
            row = soup.new_tag("tr")
            for color in colors:
                cell = soup.new_tag("td")
                cell["bgcolor"] = color
                cell["height"] = "4"
                cell.string = " "
                row.append(cell)
            table.append(row)
            rule.replace_with(table)

    @staticmethod
    def _parse_css_color(value: str) -> tuple[int, int, int] | None:
        value = re.sub(r"\s*!important\s*$", "", value.strip(), flags=re.IGNORECASE)
        named = {
            "black": (0, 0, 0), "white": (255, 255, 255),
            "blue": (0, 0, 255), "green": (0, 128, 0),
            "darkslategray": (47, 79, 79), "mediumvioletred": (199, 21, 133),
        }
        if value.casefold() in named:
            return named[value.casefold()]
        match = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", value, flags=re.IGNORECASE)
        if match:
            digits = match.group(1)
            if len(digits) == 3:
                digits = "".join(character * 2 for character in digits)
            return tuple(int(digits[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
        match = re.fullmatch(
            r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})(?:\s*,\s*[\d.]+)?\s*\)",
            value,
            flags=re.IGNORECASE,
        )
        if match:
            return tuple(min(255, int(component)) for component in match.groups())  # type: ignore[return-value]
        return None

    @classmethod
    def _dark_foreground(cls, value: str) -> str:
        rgb = cls._parse_css_color(value)
        if rgb is None:
            return value
        red, green, blue = (component / 255 for component in rgb)
        hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
        linear = [component / 12.92 if component <= 0.04045 else ((component + 0.055) / 1.055) ** 2.4 for component in (red, green, blue)]
        luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
        if luminance >= 0.24:
            return value
        if saturation < 0.22:
            return "#f1f1f1"
        # Preserve dictionary accent hues, but lift dark blue/red links so they
        # remain legible on Vicinae's dark canvas.
        red, green, blue = colorsys.hls_to_rgb(hue, max(lightness, 0.68), min(max(saturation, 0.55), 0.88))
        return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"

    @classmethod
    def _adapt_dark_style(cls, style: str) -> str:
        declaration = re.compile(
            r"(?P<prefix>(?:^|[;{])\s*)(?P<name>color|background-color|background)\s*:\s*(?P<value>[^;]+)",
            flags=re.IGNORECASE,
        )

        def replace(match: re.Match[str]) -> str:
            name = match.group("name")
            original = match.group("value").strip()
            value = original
            if name.casefold() == "color":
                value = "#f1f1f1" if re.fullmatch(r"inherit|initial|unset", value, flags=re.IGNORECASE) else cls._dark_foreground(value)
            else:
                rgb = cls._parse_css_color(value)
                if rgb is not None:
                    red, green, blue = (component / 255 for component in rgb)
                    _, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
                    if lightness > 0.88 or (lightness > 0.72 and saturation < 0.20):
                        value = "#454545"
            if value != original and "!important" not in value.casefold():
                value += " !important"
            return f'{match.group("prefix")}{name}:{value}'

        return declaration.sub(replace, style)

    @classmethod
    def _adapt_dark_colors(cls, soup: BeautifulSoup) -> None:
        for element in soup.find_all(True):
            if element.attrs is None:
                continue
            style = str(element.get("style", ""))
            if style:
                element["style"] = cls._adapt_dark_style(style)

        root = soup.new_tag("div")
        root["class"] = ["vicinae-mdict-dark"]
        root["style"] = "color:#f1f1f1;background-color:transparent"
        for child in list(soup.contents):
            root.append(child.extract())
        soup.append(root)

    def _render_disk_path(self, cache_key: tuple[str, str]) -> Path:
        dictionary_id, digest = cache_key
        return self._render_disk_dir / dictionary_id / f"{digest}.json.gz"

    def _read_render_disk_cache(
        self, cache_key: tuple[str, str]
    ) -> tuple[str, str, str | None, list[str], list[dict[str, str]]] | None:
        target = self._render_disk_path(cache_key)
        if not target.is_file():
            return None
        try:
            with gzip.open(target, "rt", encoding="utf-8") as handle:
                value = json.load(handle)
            os.utime(target, None)
            return (value[0], value[1], value[2], value[3], value[4])
        except Exception:
            target.unlink(missing_ok=True)
            return None

    def _write_render_disk_cache(
        self,
        cache_key: tuple[str, str],
        value: tuple[str, str, str | None, list[str], list[dict[str, str]]],
    ) -> None:
        target = self._render_disk_path(cache_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        try:
            with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as handle:
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)

    def _prune_render_disk_cache(self) -> None:
        files = [item for item in self._render_disk_root.rglob("*.json.gz") if item.is_file()]
        total = sum(item.stat().st_size for item in files)
        if total <= 64 * 1024 * 1024:
            return
        for item in sorted(files, key=lambda path: path.stat().st_mtime):
            size = item.stat().st_size
            item.unlink(missing_ok=True)
            total -= size
            if total <= 48 * 1024 * 1024:
                break

    def _sanitize_html(
        self, dictionary: Dictionary, raw: str
    ) -> tuple[str, str, str | None, list[str], list[dict[str, str]]]:
        cache_key = (dictionary.identifier, hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest())
        with self._cache_lock:
            cached_render = self._render_cache.get(cache_key)
            if cached_render is not None:
                self._render_cache.move_to_end(cache_key)
                return cached_render
            cached_render = self._read_render_disk_cache(cache_key)
            if cached_render is not None:
                self._render_cache[cache_key] = cached_render
                return cached_render
        soup = BeautifulSoup(raw, "html.parser")
        for element in soup.find_all(ACTIVE_TAGS):
            element.decompose()
        for element in list(soup.find_all(style=True)):
            style_value = str(element.attrs.get("style", "")) if element.attrs else ""
            if re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", style_value, re.IGNORECASE):
                element.decompose()
        warnings: list[str] = []
        entry_links: list[dict[str, str]] = []
        pronunciation: str | None = None
        css_chunks = [element.get_text(" ", strip=False) for element in soup.find_all("style")]
        for link in soup.find_all("link"):
            rel = " ".join(str(value).lower() for value in link.get("rel", []))
            reference = link.get("href")
            if "stylesheet" in rel and isinstance(reference, str):
                found = dictionary.resource(reference)
                if found:
                    css_chunks.append(found[0].decode("utf-8", errors="ignore"))
        # Qt Rich Text supports only a subset of stylesheet selectors. Apply
        # ordinary rules directly to matching elements as a compatibility
        # layer, while retaining the original style block below. This also
        # lets us remove assets that the dictionary explicitly marks hidden
        # (notably pronunciation-button sprites such as `.pf img`).
        hidden: set[int] = set()
        elements = [element for element in soup.find_all(True) if element.attrs is not None]
        by_class: dict[str, list[Tag]] = {}
        by_id: dict[str, list[Tag]] = {}
        by_tag: dict[str, list[Tag]] = {}
        for element in elements:
            by_tag.setdefault(element.name, []).append(element)
            for name in element.get("class", []):
                by_class.setdefault(str(name), []).append(element)
            identifier = element.get("id")
            if identifier:
                by_id.setdefault(str(identifier), []).append(element)
        present_classes = {
            str(name)
            for element in soup.find_all(True)
            for name in (element.get("class", []) if element.attrs else [])
        }
        present_ids = {str(element.get("id")) for element in soup.find_all(id=True)}
        for css_text in css_chunks:
            _, rules = self._prepare_css(dictionary, css_text)
            for selector, declaration in rules:
                classes = re.findall(r"\.([_a-zA-Z][\w-]*)", selector)
                identifiers = re.findall(r"#([_a-zA-Z][\w-]*)", selector)
                if not set(classes).issubset(present_classes) or not set(identifiers).issubset(present_ids):
                    continue
                tail = re.split(r"\s+|[>+~]", selector.strip())[-1]
                tail_classes = re.findall(r"\.([_a-zA-Z][\w-]*)", tail)
                tail_ids = re.findall(r"#([_a-zA-Z][\w-]*)", tail)
                if tail_classes:
                    candidates = by_class.get(tail_classes[-1], [])
                elif tail_ids:
                    candidates = by_id.get(tail_ids[-1], [])
                else:
                    tag_match = re.match(r"([a-zA-Z][\w-]*)", tail)
                    candidates = by_tag.get(tag_match.group(1).lower(), []) if tag_match else elements
                try:
                    with self._cache_lock:
                        compiled = self._selector_cache.get(selector)
                        if compiled is None:
                            compiled = soupsieve.compile(selector)
                            self._selector_cache[selector] = compiled
                    matches = [candidate for candidate in candidates if compiled.match(candidate)]
                except Exception:
                    continue
                is_hidden = bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", declaration, re.IGNORECASE))
                is_block = bool(re.search(r"display\s*:\s*block", declaration, re.IGNORECASE))
                for matched in matches:
                    if is_hidden:
                        hidden.add(id(matched))
                        continue
                    if is_block and matched.name in {"span", "a"}:
                        matched.name = "div"
                    existing = str(matched.get("style", "")).strip().rstrip(";")
                    matched["style"] = f"{existing};{declaration}" if existing else declaration
        for element in list(soup.find_all(True)):
            if id(element) in hidden:
                element.decompose()
        self._apply_qt_compatibility(soup)
        if self.appearance == "dark":
            self._adapt_dark_colors(soup)
        for element in soup.find_all(True):
            if element.attrs is None:
                continue
            for attribute in list(element.attrs):
                if attribute.lower().startswith("on") or attribute.lower() in {"srcdoc", "formaction"}:
                    del element.attrs[attribute]
            if "style" in element.attrs:
                element["style"] = self._rewrite_css_resources(dictionary, str(element["style"]))
            for attribute in ("href", "src", "poster", "action"):
                value = element.get(attribute)
                if isinstance(value, str) and value.strip().casefold().startswith(
                    ("javascript:", "vbscript:", "http://", "https://", "//")
                ):
                    del element.attrs[attribute]
        # List.Item.Detail exposes no link-click callback. Keep internal MDict
        # targets for an in-process Action submenu and remove the href so Qt
        # does not hand a vicinae:// link to the desktop URL handler, which
        # would destroy this command view and start a fresh instance.
        seen_entry_links: set[str] = set()
        for link in soup.find_all("a", href=True):
            reference = str(link.get("href", ""))
            target = internal_entry_target(reference)
            if target:
                folded = target.casefold()
                if folded not in seen_entry_links:
                    seen_entry_links.add(folded)
                    entry_links.append({"word": target, "label": link.get_text(" ", strip=True) or target})
                del link["href"]
                link["title"] = f"Available in Actions: {target}"
                classes = list(link.get("class", []))
                if "vicinae-mdict-action-link" not in classes:
                    classes.append("vicinae-mdict-action-link")
                link["class"] = classes
                existing = str(link.get("style", "")).strip().rstrip(";")
                accent = "#8db7ff" if self.appearance == "dark" else "#0645ad"
                marker_style = f"color:{accent} !important;text-decoration:underline;text-decoration-style:dotted"
                link["style"] = f"{existing};{marker_style}" if existing else marker_style
        for element in soup.find_all(["img", "audio", "source", "a", "link"]):
            attribute = "src" if element.name in {"img", "audio", "source"} else "href"
            reference = element.get(attribute)
            if not isinstance(reference, str) or reference.startswith(("http://", "https://", "data:")):
                continue
            suffix = Path(normalized_resource_key(reference).replace("\\", "/")).suffix.lower()
            if not suffix and str(reference).lower().startswith("sound://"):
                suffix = ".mp3"
            if suffix in IMAGE_EXTENSIONS | AUDIO_EXTENSIONS | CSS_EXTENSIONS or element.name in {"img", "audio", "source", "link"}:
                local = self._extract_resource(dictionary, reference)
                if local:
                    element[attribute] = Path(local).as_uri()
                    if suffix in AUDIO_EXTENSIONS and pronunciation is None:
                        pronunciation = local
                elif element.name in {"img", "audio", "source"}:
                    element.decompose()
            if element.name == "a" and suffix in AUDIO_EXTENSIONS:
                if pronunciation:
                    element.replace_with(NavigableString("🔊 Pronunciation"))
                else:
                    element.unwrap()
        for element in soup.find_all(["audio", "source", "link", "style"]):
            element.decompose()
        plain = soup.get_text("\n", strip=True)
        css = "".join(self._prepare_css(dictionary, chunk)[0] for chunk in css_chunks)
        if self.appearance == "dark":
            css = self._adapt_dark_style(css)
        # Keep the inline-HTML transport in one CommonMark paragraph. Newlines
        # inside style blocks can otherwise split the CSS from its definition.
        css = css.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        style = f"<style>{css}</style>" if css.strip() else ""
        body = re.sub(r"[\r\n]+", " ", str(soup)).strip()
        rendered = f'{style}<div class="vicinae-mdict-entry">{body}</div>'
        result = (rendered, plain, pronunciation, warnings, entry_links)
        with self._cache_lock:
            self._render_cache[cache_key] = result
            self._render_cache.move_to_end(cache_key)
            while len(self._render_cache) > 128:
                self._render_cache.popitem(last=False)
            self._write_render_disk_cache(cache_key, result)
        return result

    def _lookup_dictionary(self, dictionary: Dictionary, word: str) -> tuple[dict[str, Any], list[str], str | None]:
        try:
            definitions = dictionary.exact(word)
            converted: list[str] = []
            plain_entries: list[str] = []
            warnings: list[str] = []
            entry_links: list[dict[str, str]] = []
            pronunciation: str | None = None
            for raw in definitions:
                rendered_html, plain, audio, item_warnings, item_links = self._sanitize_html(dictionary, raw)
                converted.append(rendered_html)
                if plain:
                    plain_entries.append(f"{dictionary.title}\n{plain}")
                if audio and pronunciation is None:
                    pronunciation = audio
                warnings.extend(item_warnings)
                entry_links.extend(item_links)
            return ({
                "dictionary": dictionary.title,
                "html": '<hr style="margin:1em 0">'.join(converted),
                "found": bool(definitions),
                "warnings": warnings,
                "links": entry_links,
            }, plain_entries, pronunciation)
        except Exception as exc:
            return ({
                "dictionary": dictionary.title,
                "html": "",
                "found": False,
                "error": f"{type(exc).__name__}: {exc}",
            }, [], None)

    def lookup(self, word: str, preferred_dictionary: str = "", preferred_only: bool = False) -> dict[str, Any]:
        word = word.strip()
        lookup_key = (word.casefold(), preferred_dictionary.casefold(), preferred_only)
        with self._cache_lock:
            cached_lookup = self._lookup_cache.get(lookup_key)
            if cached_lookup is not None:
                self._lookup_cache.move_to_end(lookup_key)
                return cached_lookup
        sections: list[dict[str, Any]] = []
        all_plain: list[str] = []
        pronunciation: str | None = None
        dictionaries = list(self.dictionaries)
        if preferred_dictionary:
            dictionaries.sort(key=lambda item: item.title.casefold() != preferred_dictionary.casefold())
        if preferred_only and dictionaries:
            dictionaries = dictionaries[:1]
        workers = min(4, len(dictionaries))
        if workers:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mdict-lookup") as executor:
                outcomes = list(executor.map(lambda dictionary: self._lookup_dictionary(dictionary, word), dictionaries))
            for section, plain_entries, audio in outcomes:
                sections.append(section)
                all_plain.extend(plain_entries)
                if audio and pronunciation is None:
                    pronunciation = audio
        result = {
            "word": word,
            "sections": sections,
            "plain": "\n\n".join(all_plain),
            "pronunciation": pronunciation,
        }
        with self._cache_lock:
            self._lookup_cache[lookup_key] = result
            self._lookup_cache.move_to_end(lookup_key)
            while len(self._lookup_cache) > 128:
                self._lookup_cache.popitem(last=False)
        return result


def serve() -> int:
    backend: Backend | None = None
    for raw_line in sys.stdin:
        request_id: Any = None
        try:
            request = json.loads(raw_line)
            request_id = request.get("id")
            command = request.get("command")
            if command == "initialize":
                backend = Backend(request["config"], force=False)
                result = backend.status()
            elif command == "reload":
                backend = Backend(request["config"], force=True)
                result = backend.status()
            elif backend is None:
                raise RuntimeError("Backend has not been initialized")
            elif command == "suggest":
                result = backend.suggestions(str(request.get("query", "")))
            elif command == "lookup":
                result = backend.lookup(
                    str(request.get("word", "")),
                    str(request.get("preferredDictionary", "")),
                    bool(request.get("preferredOnly", False)),
                )
            elif command == "status":
                result = backend.status()
            else:
                raise ValueError(f"Unknown command: {command}")
            json_line({"id": request_id, "ok": True, "result": result})
        except Exception as exc:
            json_line({"id": request_id, "ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc(limit=8)})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Vicinae local MDict backend")
    parser.add_argument("--serve", action="store_true", help="Use newline-delimited JSON on stdin/stdout")
    args = parser.parse_args()
    if args.serve:
        return serve()
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
