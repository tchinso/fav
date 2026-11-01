#!/usr/bin/env python3
"""Validate fav.ju.mp HTML and a converted wget mirror before publishing it."""

from __future__ import annotations

import argparse
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import unquote, urljoin, urlsplit


SITE_URL = "https://fav.ju.mp/"
CSS_URL = re.compile(r"url\(\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s)]*))\s*\)", re.I)
CSS_IMPORT = re.compile(r"@import\s+(?:\"([^\"]+)\"|'([^']+)')", re.I)
RENDERING_LINKS = {"stylesheet", "icon", "apple-touch-icon", "mask-icon", "modulepreload"}
PRELOAD_TYPES = {"style", "script", "image", "font", "audio", "video", "fetch"}


def css_references(css: str) -> list[str]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [next(group for group in match.groups() if group is not None)
            for pattern in (CSS_URL, CSS_IMPORT) for match in pattern.finditer(css)]


def is_asset(value: str) -> bool:
    return bool(value) and not value.lower().startswith(("data:", "#", "blob:"))


class Document(HTMLParser):
    def __init__(self, content: str):
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.text: list[str] = []
        self.anchors: list[str] = []
        self.ids: Counter[str] = Counter()
        self.assets: list[tuple[str, str]] = []
        self.css: list[str] = []
        self.scripts: list[str] = []
        self.wrapper = False
        self.main = False
        self.html_open = False
        self.html_closed = False
        self.body_open = False
        self.body_closed = False
        self.challenge = False
        self._title = False
        self._ignored: list[str] = []
        self._style: list[str] | None = None
        self._script: list[str] | None = None
        self.feed(content)
        self.close()

    def handle_starttag(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        attrs = {key: value or "" for key, value in attributes}
        classes = set(attrs.get("class", "").split())
        identifier = attrs.get("id", "")
        if identifier:
            self.ids[identifier] += 1
        self.wrapper |= identifier == "wrapper" or "site-wrapper" in classes
        self.main |= identifier == "main" or "site-main" in classes or tag == "main"
        self.challenge |= identifier in {"challenge-form", "cf-error-details", "cf-challenge-running"}
        self.html_open |= tag == "html"
        self.body_open |= tag == "body"
        if tag == "title":
            self._title = True
        if tag in {"script", "style"}:
            self._ignored.append(tag)
        if tag == "style":
            self._style = []
        if tag == "script":
            self._script = []
        if "style" in attrs:
            self.css.append(attrs["style"])
        if tag == "a" and "href" in attrs:
            self.anchors.append(attrs["href"])
        if "src" in attrs:
            self.assets.append((tag + ":src", attrs["src"]))
        if tag == "object" and attrs.get("data"):
            self.assets.append(("object:data", attrs["data"]))
        if attrs.get("poster"):
            self.assets.append((tag + ":poster", attrs["poster"]))
        if "srcset" in attrs and not attrs["srcset"].lstrip().startswith("data:"):
            for candidate in attrs["srcset"].split(","):
                if candidate.strip():
                    self.assets.append((tag + ":srcset", candidate.strip().split()[0]))
        if tag == "link":
            relations = set(attrs.get("rel", "").lower().split())
            relevant = relations & RENDERING_LINKS
            if "preload" in relations and attrs.get("as", "").lower() in PRELOAD_TYPES:
                relevant.add("preload")
            if relevant and attrs.get("href"):
                self.assets.append(("link:" + " ".join(sorted(relevant)), attrs["href"]))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        self.html_closed |= tag == "html"
        self.body_closed |= tag == "body"
        if tag == "title":
            self._title = False
        if tag == "style" and self._style is not None:
            self.css.append("".join(self._style))
            self._style = None
        if tag == "script" and self._script is not None:
            script = "".join(self._script).strip()
            if script:
                self.scripts.append(script)
            self._script = None
        if self._ignored and self._ignored[-1] == tag:
            self._ignored.pop()

    def handle_data(self, value: str) -> None:
        if self._title:
            self.title.append(value)
        if self._style is not None:
            self._style.append(value)
        if self._script is not None:
            self._script.append(value)
        if not self._ignored:
            self.text.append(value)


def read_document(path: Path) -> Document:
    try:
        document = Document(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError) as error:
        raise ValueError(f"Cannot read HTML {path}: {error}") from error
    if "".join(document.title).strip() != "Favorites!":
        raise ValueError(f"Unexpected page title in {path}; expected Favorites!")
    if not all((document.html_open, document.html_closed, document.body_open, document.body_closed)):
        raise ValueError(f"Incomplete HTML document in {path}")
    if not document.wrapper or not document.main or not document.anchors:
        raise ValueError(f"Missing site structure or bookmarks in {path}")
    text = " ".join("".join(document.text).split()).lower()
    if document.challenge or any(phrase in text for phrase in (
        "checking your browser", "enable javascript and cookies to continue",
        "sorry, you have been blocked", "cloudflare ray id",
    )):
        raise ValueError(f"Server error or interstitial page in {path}")
    return document


def normalized_link(value: str) -> tuple[str, str, str, str, str]:
    parsed = urlsplit(urljoin(SITE_URL, value))
    path = unquote(parsed.path)
    if parsed.netloc.lower() == "fav.ju.mp" and path.endswith("/index.html"):
        path = path[:-len("index.html")]
    return parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, parsed.fragment


def local_asset(value: str, parent: Path, mirror: Path) -> Path | None:
    if not is_asset(value):
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        raise ValueError(f"Rendering resource still requires the network: {value}")
    relative = unquote(parsed.path)
    path = (mirror / relative.lstrip("/") if relative.startswith("/") else parent / relative).resolve()
    try:
        path.relative_to(mirror)
    except ValueError as error:
        raise ValueError(f"Rendering resource escapes the mirror: {value}") from error
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"Missing or empty rendering resource: {value}")
    return path


def validate(source: Path, mirror_dir: Path | None = None) -> None:
    original = read_document(source)
    if mirror_dir is None:
        return
    mirror = mirror_dir.resolve()
    mirrored = read_document(mirror / "index.html")
    missing_links = Counter(map(normalized_link, original.anchors)) - Counter(map(normalized_link, mirrored.anchors))
    if missing_links:
        raise ValueError(f"Mirror lost or changed {sum(missing_links.values())} bookmark link(s)")
    if original.ids - mirrored.ids:
        raise ValueError("Mirror lost site elements")
    if " ".join("".join(original.text).split()) != " ".join("".join(mirrored.text).split()):
        raise ValueError("Mirror changed visible site content")
    if original.scripts != mirrored.scripts:
        raise ValueError("Mirror changed inline site scripts")
    if Counter(kind for kind, _ in original.assets) != Counter(kind for kind, _ in mirrored.assets):
        raise ValueError("Mirror lost or changed rendering resource elements")
    original_css = [value for css in original.css for value in css_references(css) if is_asset(value)]
    mirrored_css = [value for css in mirrored.css for value in css_references(css) if is_asset(value)]
    if len(original_css) != len(mirrored_css):
        raise ValueError("Mirror lost CSS rendering resources")
    pending: list[Path] = []
    for _, value in mirrored.assets:
        path = local_asset(value, mirror, mirror)
        if path is not None and path.suffix.lower() == ".css":
            pending.append(path)
    for value in mirrored_css:
        path = local_asset(value, mirror, mirror)
        if path is not None and path.suffix.lower() == ".css":
            pending.append(path)
    visited: set[Path] = set()
    while pending:
        stylesheet = pending.pop()
        if stylesheet in visited:
            continue
        visited.add(stylesheet)
        css = stylesheet.read_text(encoding="utf-8-sig")
        references = css_references(css)
        original_css_path = Path(str(stylesheet) + ".orig")
        if original_css_path.is_file():
            before = css_references(original_css_path.read_text(encoding="utf-8-sig"))
            if sum(map(is_asset, before)) != sum(map(is_asset, references)):
                raise ValueError(f"Mirror lost stylesheet resources: {stylesheet.name}")
        for value in references:
            path = local_asset(value, stylesheet.parent, mirror)
            if path is not None and path.suffix.lower() == ".css":
                pending.append(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("mirror_dir", nargs="?", type=Path)
    args = parser.parse_args()
    try:
        validate(args.source, args.mirror_dir)
    except (ValueError, OSError, UnicodeError) as error:
        parser.exit(1, f"Backup validation failed: {error}\n")
    print("Backup HTML validation passed.")


if __name__ == "__main__":
    main()
