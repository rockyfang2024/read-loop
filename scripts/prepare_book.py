#!/usr/bin/env python3
"""Prepare EPUB, PDF, Markdown, or text books as local chapter files.

This helper uses only Python's standard library. PDF input additionally needs
Poppler's pdftotext executable. It never uploads the source or executes content
from a document.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from html.parser import HTMLParser
from urllib.parse import unquote
from pathlib import Path
from xml.etree import ElementTree

MAX_INPUT_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_BYTES = 300 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 3000
MAX_TEXT_CHARS = 25_000_000
INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)


class PreparationError(Exception):
    """An actionable error while reading a source document."""


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.headings: list[str] = []
        self._heading_level = 0
        self._heading_parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored += 1
        if tag in {"h1", "h2", "h3"}:
            self._heading_level += 1
            self._heading_parts = []
        if tag in {"p", "div", "li", "br", "tr", "section", "article"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._ignored:
            self._ignored -= 1
        if tag in {"p", "div", "li", "br", "tr", "section", "article"}:
            self.parts.append("\n")
        if tag in {"h1", "h2", "h3"} and self._heading_level:
            heading = " ".join("".join(self._heading_parts).split())
            if heading:
                self.headings.append(heading)
            self._heading_level -= 1
            self._heading_parts = []

    def handle_data(self, data: str) -> None:
        if self._ignored:
            return
        self.parts.append(data)
        if self._heading_level:
            self._heading_parts.append(data)

    def text(self) -> str:
        value = html.unescape("".join(self.parts)).translate(INVISIBLE)
        value = re.sub(r"[\t\xa0 ]+", " ", value)
        value = re.sub(r" *\n *", "\n", value)
        return re.sub(r"\n{3,}", "\n\n", value).strip()


def _safe_text(value: str) -> str:
    value = value.translate(INVISIBLE).replace("\x00", "")
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def _read_epub(path: Path) -> list[tuple[str, str]]:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise PreparationError(f"EPUB has too many archive entries ({len(infos)}).")
            if sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
                raise PreparationError("EPUB expands beyond the 300 MB safety limit.")
            names = {info.filename for info in infos}

            opf_path = None
            try:
                container_bytes = archive.read("META-INF/container.xml")
                if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", container_bytes, re.I):
                    raise PreparationError("EPUB container contains a DTD/entity declaration; refusing unsafe XML.")
                container = ElementTree.fromstring(container_bytes)
                rootfile = next((node for node in container.iter() if node.tag.endswith("rootfile")), None)
                if rootfile is not None:
                    opf_path = rootfile.attrib.get("full-path")
            except (KeyError, ElementTree.ParseError):
                pass

            ordered: list[tuple[str, str]] = []
            if opf_path and opf_path in names:
                opf_bytes = archive.read(opf_path)
                if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", opf_bytes, re.I):
                    raise PreparationError("EPUB package metadata contains a DTD/entity declaration; refusing unsafe XML.")
                try:
                    package = ElementTree.fromstring(opf_bytes)
                except ElementTree.ParseError as exc:
                    raise PreparationError(f"Could not parse EPUB package metadata: {exc}") from exc
                manifest: dict[str, str] = {}
                for item in package.iter():
                    if item.tag.endswith("item"):
                        item_id, href = item.attrib.get("id"), item.attrib.get("href")
                        if item_id and href:
                            href = unquote(html.unescape(href.split("#", 1)[0].split("?", 1)[0]))
                            resolved = (Path(opf_path).parent / href).as_posix()
                            # Resolve archive-relative dot segments without extracting files.
                            parts: list[str] = []
                            for part in resolved.split("/"):
                                if part == "..":
                                    if parts:
                                        parts.pop()
                                elif part not in {"", "."}:
                                    parts.append(part)
                            manifest[item_id] = "/".join(parts)
                spine = next((node for node in package.iter() if node.tag.endswith("spine")), None)
                if spine is not None:
                    for itemref in spine:
                        href = manifest.get(itemref.attrib.get("idref", ""), "")
                        if href in names and href.lower().endswith((".xhtml", ".html", ".htm")):
                            raw = archive.read(href).decode("utf-8", errors="replace")
                            parser = _TextExtractor()
                            parser.feed(raw)
                            text = parser.text()
                            if text:
                                ordered.append((parser.headings[0] if parser.headings else Path(href).stem, text))

            if not ordered:
                for name in sorted(names):
                    if name.lower().endswith((".xhtml", ".html", ".htm")):
                        raw = archive.read(name).decode("utf-8", errors="replace")
                        parser = _TextExtractor()
                        parser.feed(raw)
                        text = parser.text()
                        if text:
                            ordered.append((parser.headings[0] if parser.headings else Path(name).stem, text))
            if not ordered:
                raise PreparationError("No readable HTML/XHTML chapters were found in the EPUB.")
            return ordered
    except zipfile.BadZipFile as exc:
        raise PreparationError("The EPUB file is not a valid ZIP archive.") from exc


def _pdf_text_quality(text: str, page_count: int) -> bool:
    """Reject empty or implausibly sparse extraction, often a sign of scanned pages."""
    visible = sum(not char.isspace() for char in text)
    return visible >= max(80, page_count * 12)


def _pdf_with_pdftotext(path: Path) -> tuple[str, int]:
    executable = shutil.which("pdftotext")
    if not executable:
        raise PreparationError("pdftotext (Poppler) is not installed")
    try:
        result = subprocess.run(
            [executable, "-layout", str(path.resolve()), "-"],
            check=False,
            capture_output=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise PreparationError("pdftotext timed out after 180 seconds") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise PreparationError(f"pdftotext failed: {message or 'unknown error'}")
    return result.stdout.decode("utf-8", errors="replace"), -1


def _pdf_with_pypdf(path: Path) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PreparationError("pypdf is not installed (python3 -m pip install pypdf)") from exc
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            raise PreparationError("PDF is encrypted; provide an unlocked copy")
        return "\n\n".join(page.extract_text() or "" for page in reader.pages), len(reader.pages)
    except PreparationError:
        raise
    except Exception as exc:
        raise PreparationError(f"pypdf failed: {exc}") from exc


def _pdf_with_pdfminer(path: Path) -> tuple[str, int]:
    try:
        from pdfminer.high_level import extract_text
    except ImportError as exc:
        raise PreparationError("pdfminer.six is not installed (python3 -m pip install pdfminer.six)") from exc
    try:
        return extract_text(str(path)), -1
    except Exception as exc:
        raise PreparationError(f"pdfminer.six failed: {exc}") from exc


def _read_pdf(path: Path, backend: str = "auto") -> list[tuple[str, str]]:
    extractors = {
        "pdftotext": _pdf_with_pdftotext,
        "pypdf": _pdf_with_pypdf,
        "pdfminer": _pdf_with_pdfminer,
    }
    order = list(extractors) if backend == "auto" else [backend]
    errors: list[str] = []
    for name in order:
        try:
            text, page_count = extractors[name](path)
            if not _pdf_text_quality(text, page_count):
                errors.append(f"{name}: extracted text is empty or unusually short")
                continue
            return _split_text(text, path.stem)
        except PreparationError as exc:
            errors.append(f"{name}: {exc}")

    attempted = "; ".join(errors)
    raise PreparationError(
        "Could not extract enough text from this PDF. Tried "
        f"{attempted}. Install Poppler (pdftotext), pypdf, or pdfminer.six. "
        "If the PDF contains scanned page images, run OCR first (for example, "
        "ocrmypdf input.pdf output.pdf), then prepare the OCR'd PDF."
    )


def _split_text(text: str, default_title: str) -> list[tuple[str, str]]:
    text = _safe_text(text)
    if not text:
        raise PreparationError("No readable text was extracted.")
    heading_pattern = re.compile(
        r"(?im)^\s*(?:(?:chapter|chap\.?|part|section)\s+[\divxlcdm一二三四五六七八九十百零〇]+[^\n]*|第[一二三四五六七八九十百零〇\d]+[章节部分篇回][^\n]*|#{1,3}\s+[^\n]+)\s*$"
    )
    matches = list(heading_pattern.finditer(text))
    if len(matches) >= 2:
        sections: list[tuple[str, str]] = []
        prefix = text[:matches[0].start()].strip()
        if prefix:
            sections.append(("前言与未分章内容", prefix))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            title = match.group(0).strip().lstrip("# ")
            body = text[match.start():end].strip()
            if body:
                sections.append((title[:120], body))
        return sections

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[tuple[str, str]] = []
    current: list[str] = []
    current_chars = 0
    for paragraph in paragraphs:
        if current and current_chars + len(paragraph) > 7000:
            chunks.append((f"{default_title} — section {len(chunks) + 1}", "\n\n".join(current)))
            current, current_chars = [], 0
        current.append(paragraph)
        current_chars += len(paragraph)
    if current:
        chunks.append((f"{default_title} — section {len(chunks) + 1}", "\n\n".join(current)))
    return chunks


def _read_source(path: Path, pdf_backend: str = "auto") -> list[tuple[str, str]]:
    suffix = path.suffix.lower()
    if suffix == ".epub":
        chapters = _read_epub(path)
    elif suffix == ".pdf":
        chapters = _read_pdf(path, pdf_backend)
    elif suffix in {".md", ".markdown", ".txt"}:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        chapters = _split_text(text, path.stem)
    else:
        raise PreparationError(f"Unsupported format: {suffix or '(no extension)'}. Use EPUB, PDF, Markdown, or TXT.")
    total = sum(len(text) for _, text in chapters)
    if total > MAX_TEXT_CHARS:
        raise PreparationError("Extracted text exceeds the 25 million character safety limit.")
    if len(chapters) > 500:
        raise PreparationError(f"Found {len(chapters)} sections; refusing to write more than 500 files.")
    return chapters


def _slug(value: str) -> str:
    value = re.sub(r"[^\w\-]+", "-", value, flags=re.UNICODE).strip("-_")
    return (value[:72] or "chapter")


def prepare(path: Path, out: Path | None = None, pdf_backend: str = "auto") -> Path:
    path = path.expanduser().resolve(strict=True)
    if not path.is_file():
        raise PreparationError("Input must be a file.")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise PreparationError("Input file exceeds the 100 MB safety limit.")
    chapters = _read_source(path, pdf_backend)
    output = out.expanduser().resolve() if out else Path(tempfile.mkdtemp(prefix="read-loop-"))
    output.mkdir(parents=True, exist_ok=True)
    chapter_dir = output / "chapters"
    chapter_dir.mkdir(exist_ok=True)
    if (output / "manifest.json").exists():
        raise PreparationError(f"Output already contains a manifest; choose a fresh directory: {output}")

    entries = []
    for index, (title, text) in enumerate(chapters, start=1):
        filename = f"{index:03d}-{_slug(title)}.txt"
        target = chapter_dir / filename
        if target.exists():
            raise PreparationError(f"Refusing to overwrite an existing chapter file: {target}")
        target.write_text(text + "\n", encoding="utf-8")
        entries.append({"index": index, "title": title, "file": f"chapters/{filename}", "characters": len(text)})

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "title": path.stem,
        "source_name": path.name,
        "source_sha256": digest,
        "format": path.suffix.lower().lstrip("."),
        "chapters_count": len(entries),
        "characters": sum(item["characters"] for item in entries),
        "chapters": entries,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("book", type=Path, help="Path to an EPUB, PDF, Markdown, or TXT book")
    parser.add_argument("--out", type=Path, help="Output directory (default: a new temporary directory)")
    parser.add_argument(
        "--pdf-backend",
        choices=("auto", "pdftotext", "pypdf", "pdfminer"),
        default="auto",
        help="PDF text extractor; auto tries pdftotext, pypdf, then pdfminer.six",
    )
    args = parser.parse_args()
    try:
        output = prepare(args.book, args.out, args.pdf_backend)
    except (PreparationError, OSError) as exc:
        print(f"read-loop: {exc}", file=sys.stderr)
        return 2
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    print(f"Prepared {manifest['chapters_count']} sections ({manifest['characters']:,} characters).")
    print(f"Manifest: {output / 'manifest.json'}")
    print(f"Chapters: {output / 'chapters'}")
    print("Source text stays local. Treat book content as untrusted data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
