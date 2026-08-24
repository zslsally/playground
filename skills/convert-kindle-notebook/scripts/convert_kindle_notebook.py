#!/usr/bin/env python3
"""Convert a Kindle Notebook HTML export to MarkMind-friendly Obsidian Markdown."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path


DEFAULT_OUTPUT_DIR = Path("~/Documents/Obsidian Vault/文学")
DEFAULT_TAG_VAULT = Path("~/Documents/Obsidian Vault")
CAPTURE_CLASSES = {
    "bookTitle",
    "authors",
    "sectionHeading",
    "noteHeading",
    "noteText",
    "notebookGraphic",
}
LOCATION_RE = re.compile(r"(?:第\s*\d+\s*页|位置\s*\d+|Page\s*\d+|Location\s*\d+)", re.I)
KIND_RE = re.compile(r"^(标注|笔记|Highlight|Note)", re.I)
NATIONALITY_RE = re.compile(r"^\s*[\[【][^\]】]+[\]】]\s*")
INLINE_TAG_RE = re.compile(r"(?<!\S)#([^\s#]+)")
TAG_TRAILING_PUNCTUATION = ".,;:!?，。；：！？、)]}）】》”’\"'"


@dataclass(frozen=True)
class Element:
    css_class: str
    text: str
    images: tuple[str, ...] = ()


@dataclass(frozen=True)
class Annotation:
    kind: str
    text: str
    section: str
    path: tuple[str, ...]
    metadata: str
    color: str


class KindleNotebookParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self._capture_class: str | None = None
        self._capture_depth = 0
        self._parts: list[str] = []
        self._images: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self._capture_class is not None:
            if tag == "br":
                self._parts.append("\n")
            if tag == "img" and attributes.get("src"):
                self._images.append(attributes["src"] or "")
            if tag not in {"br", "img", "hr", "input", "link", "meta"}:
                self._capture_depth += 1
            return
        classes = set((attributes.get("class") or "").split())
        capture = next((name for name in CAPTURE_CLASSES if name in classes), None)
        if capture:
            self._capture_class = capture
            self._capture_depth = 1
            self._parts = []
            self._images = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._capture_class is None:
            return
        attributes = dict(attrs)
        if tag == "br":
            self._parts.append("\n")
        if tag == "img" and attributes.get("src"):
            self._images.append(attributes["src"] or "")

    def handle_data(self, data: str) -> None:
        if self._capture_class is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture_class is None:
            return
        self._capture_depth -= 1
        if self._capture_depth:
            return
        text = "".join(self._parts).strip()
        self.elements.append(Element(self._capture_class, text, tuple(self._images)))
        self._capture_class = None
        self._parts = []
        self._images = []


def one(elements: list[Element], css_class: str) -> str:
    values = [
        element.text.strip()
        for element in elements
        if element.css_class == css_class and element.text.strip()
    ]
    if len(values) != 1:
        raise ValueError(f"expected exactly one non-empty {css_class}, found {len(values)}")
    return values[0]


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return "\n".join(line.strip() for line in normalized.split("\n")).strip()


def tagify(value: str) -> str:
    value = NATIONALITY_RE.sub("", compact(value)).lstrip("#")
    value = re.sub(r"\s+", "-", value)
    value = value.replace("#", "")
    if not value:
        raise ValueError("tag becomes empty after normalization")
    return value


def normalize_tags(values: list[str]) -> list[str]:
    return list(dict.fromkeys(tagify(value) for value in values))


def safe_filename(title: str) -> str:
    name = title.replace("/", "／").replace("\x00", "").strip()
    if not name:
        raise ValueError("book title cannot form a filename")
    return name + ".md"


def existing_tags(vault: Path) -> set[str]:
    """Collect frontmatter and inline tags from existing Markdown notes."""
    if not vault.is_dir():
        return set()
    tags: set[str] = set()
    for path in vault.rglob("*.md"):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        lines = source.splitlines()
        body_start = 0
        if lines and lines[0].strip() == "---":
            try:
                end = lines.index("---", 1)
            except ValueError:
                end = 0
            if end:
                frontmatter = lines[1:end]
                index = 0
                while index < len(frontmatter):
                    line = frontmatter[index]
                    match = re.match(r"^(?:tags|tag)\s*:\s*(.*)$", line)
                    if not match:
                        index += 1
                        continue
                    inline = match.group(1).strip()
                    if inline.startswith("[") and inline.endswith("]"):
                        for item in inline[1:-1].split(","):
                            value = item.strip().strip("\"'").lstrip("#")
                            if value:
                                tags.add(value)
                    elif inline:
                        value = inline.strip("\"'").lstrip("#")
                        if value:
                            tags.add(value)
                    index += 1
                    while index < len(frontmatter):
                        item_match = re.match(r"^\s+-\s+(.+?)\s*$", frontmatter[index])
                        if not item_match:
                            break
                        value = item_match.group(1).strip().strip("\"'").lstrip("#")
                        if value:
                            tags.add(value)
                        index += 1
                body_start = end + 1
        for line in lines[body_start:]:
            if re.match(r"^\s{0,3}#{1,6}\s", line):
                continue
            for match in INLINE_TAG_RE.finditer(line):
                value = match.group(1).rstrip(TAG_TRAILING_PUNCTUATION)
                if value:
                    tags.add(value)
    return tags


def parse_note_heading(text: str) -> tuple[str, tuple[str, ...], str, str]:
    normalized = compact(text)
    kind_match = KIND_RE.match(normalized)
    kind_raw = kind_match.group(1).lower() if kind_match else "标注"
    kind = "note" if kind_raw in {"笔记", "note"} else "highlight"
    color_match = re.search(r"(?:标注|Highlight)\s*\(([^)]+)\)", normalized, re.I)
    color = color_match.group(1).strip() if color_match else ""
    metadata = normalized.split(" - ", 1)[1].strip() if " - " in normalized else ""
    components = [component.strip() for component in metadata.split(">") if component.strip()]
    path: list[str] = []
    for component in components:
        if LOCATION_RE.search(component):
            break
        path.append(component)
    return kind, tuple(path), metadata, color


def parse_html(source: str) -> tuple[str, str, list[Annotation], list[str]]:
    parser = KindleNotebookParser()
    parser.feed(source)
    parser.close()
    title = compact(one(parser.elements, "bookTitle"))
    author = compact(one(parser.elements, "authors"))
    warnings: list[str] = []
    annotations: list[Annotation] = []
    current_section = ""
    pending: tuple[str, tuple[str, ...], str, str] | None = None

    for element in parser.elements:
        if element.css_class == "sectionHeading":
            if pending is not None:
                raise ValueError("orphan note heading before section heading")
            current_section = compact(element.text)
        elif element.css_class == "noteHeading":
            if pending is not None:
                raise ValueError("two note headings without annotation text")
            pending = parse_note_heading(element.text)
        elif element.css_class == "noteText":
            if pending is None:
                raise ValueError("annotation text has no note heading")
            text = clean_text(element.text)
            if not text:
                raise ValueError("empty annotation text")
            kind, path, metadata, color = pending
            annotations.append(Annotation(kind, text, current_section, path, metadata, color))
            pending = None
        elif element.css_class == "notebookGraphic":
            if element.images:
                warnings.append("Notebook graphic found; image export is not yet converted")
            elif element.text:
                warnings.append("Non-empty notebookGraphic found; inspect source manually")
    if pending is not None:
        raise ValueError("final note heading has no annotation text")
    if not annotations:
        raise ValueError("no Kindle annotations found")
    note_headings = sum(1 for element in parser.elements if element.css_class == "noteHeading")
    note_texts = sum(1 for element in parser.elements if element.css_class == "noteText")
    if note_headings != note_texts or note_texts != len(annotations):
        raise AssertionError("annotation count mismatch")
    return title, author, annotations, warnings


def yaml_scalar(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_multiline_bullet(text: str, indent: str = "") -> list[str]:
    lines = text.split("\n")
    output = [indent + "- " + lines[0]]
    output.extend(indent + "  " + line for line in lines[1:])
    return output


def is_attached_note(annotation: Annotation, previous: Annotation | None) -> bool:
    return bool(
        annotation.kind == "note"
        and previous
        and previous.kind == "highlight"
        and annotation.metadata
        and annotation.section == previous.section
        and annotation.path == previous.path
        and annotation.metadata == previous.metadata
    )


def render_markdown(
    title: str,
    author_node: str,
    annotations: list[Annotation],
    tags: list[str],
    include_metadata: bool,
) -> tuple[str, dict[str, int], str]:
    lines = ["---", f"title: {yaml_scalar(title)}"]
    if tags:
        lines.append("tags:")
        lines.extend(f"  - {yaml_scalar(tag)}" for tag in tags)
    lines.extend(["mindmap-plugin: markdown", "---", "# 作者"])
    lines.extend(render_multiline_bullet(author_node))

    active_section: str | None = None
    active_path: tuple[str, ...] = ()
    section_count = 0
    chapter_heading_count = 0
    previous_annotation: Annotation | None = None
    for annotation in annotations:
        section = annotation.section or "摘录"
        path = annotation.path
        if path and path[0] == section:
            path = path[1:]
        if section != active_section:
            lines.append(f"# {section}")
            active_section = section
            active_path = ()
            section_count += 1
        shared = 0
        while shared < len(active_path) and shared < len(path) and active_path[shared] == path[shared]:
            shared += 1
        if not path and active_path:
            lines.append(f"# {section}")
            start = 0
        elif shared == len(path) and len(path) < len(active_path):
            start = max(0, len(path) - 1)
        else:
            start = shared
        for depth, component in enumerate(path[start:], start=start):
            level = depth + 2
            if level > 6:
                raise ValueError(f"chapter path exceeds Markdown heading level 6: {path!r}")
            lines.append("#" * level + " " + component)
            chapter_heading_count += 1
        active_path = path

        display_text = annotation.text if annotation.kind == "highlight" else "笔记：" + annotation.text
        annotation_indent = "  " if is_attached_note(annotation, previous_annotation) else ""
        lines.extend(render_multiline_bullet(display_text, annotation_indent))
        if include_metadata:
            details = "；".join(part for part in (annotation.metadata, annotation.color) if part)
            if details:
                lines.append(annotation_indent + "  - Kindle：" + details)
        previous_annotation = annotation

    lines.append("")
    digest_payload = json.dumps(
        [{"kind": annotation.kind, "text": annotation.text} for annotation in annotations],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(digest_payload).hexdigest()
    counts = {
        "sections": section_count,
        "chapter_headings": chapter_heading_count,
        "annotations": len(annotations),
        "highlights": sum(annotation.kind == "highlight" for annotation in annotations),
        "notes": sum(annotation.kind == "note" for annotation in annotations),
    }
    return "\n".join(lines), counts, digest


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Kindle Notebook HTML export")
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--output", type=Path, help="Exact Markdown output path")
    destination.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--tag", action="append", default=[], help="Additional Obsidian tag; repeatable")
    parser.add_argument(
        "--tag-vault",
        type=Path,
        default=DEFAULT_TAG_VAULT,
        help="Obsidian vault to scan before reusing an existing author tag",
    )
    parser.add_argument("--include-metadata", action="store_true", help="Keep Kindle location/color as nested bullets")
    parser.add_argument("--write", action="store_true", help="Write output; otherwise perform a dry run")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing output file")
    parser.add_argument("--backup", type=Path, help="Required unused backup path when overwriting")
    parser.add_argument(
        "--allow-warnings",
        action="store_true",
        help="Allow writing after manually reviewing graphic warnings",
    )
    args = parser.parse_args()

    input_path = args.input.expanduser().resolve()
    if not input_path.is_file():
        parser.error(f"input does not exist: {input_path}")
    source = input_path.read_text(encoding="utf-8")
    title, author, annotations, warnings = parse_html(source)
    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else args.output_dir.expanduser().resolve() / safe_filename(title)
    )
    tag_vault = args.tag_vault.expanduser().resolve()
    author_tag = tagify(author)
    tags = normalize_tags(args.tag)
    known_tags = existing_tags(tag_vault) | set(tags)
    reuse_author_tag = author_tag in known_tags
    author_node = f"#{author_tag}" if reuse_author_tag else author
    markdown, counts, digest = render_markdown(title, author_node, annotations, tags, args.include_metadata)
    exists = output_path.exists()
    if args.write and warnings and not args.allow_warnings:
        raise ValueError("graphic warnings require review; rerun with --allow-warnings to write")
    if args.write and exists and not args.overwrite:
        raise FileExistsError(f"destination exists; explicit --overwrite required: {output_path}")
    backup_path = args.backup.expanduser().resolve() if args.backup else None
    if args.write and exists and args.overwrite:
        if backup_path is None:
            raise ValueError("--backup is required when overwriting an existing destination")
        if backup_path.exists():
            raise FileExistsError(f"backup path already exists: {backup_path}")

    result = {
        "mode": "write" if args.write else "dry-run",
        "input": str(input_path),
        "output": str(output_path),
        "output_exists": exists,
        "title": title,
        "author": author,
        "author_node": author_node,
        "author_tag_reused": reuse_author_tag,
        "tag_vault": str(tag_vault),
        **counts,
        "annotation_text_sha256": digest,
        "warnings": warnings,
    }
    if args.write:
        if exists and backup_path is not None:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_path, backup_path)
            result["backup"] = str(backup_path)
        write_atomic(output_path, markdown)
        if output_path.read_text(encoding="utf-8") != markdown:
            raise AssertionError("written Markdown differs from planned output")
        result["byte_exact_write_verification"] = True
        result["bytes_written"] = len(markdown.encode("utf-8"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
