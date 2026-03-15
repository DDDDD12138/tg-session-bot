from __future__ import annotations

import html
import re
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.token import Token


SUPPORTED_TAGS = {"b", "strong", "i", "em", "s", "strike", "del", "code", "pre", "a", "blockquote", "tg-spoiler"}
SELF_CLOSING_TAGS = {"br"}
TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)(?:\s+[^>]*?)?>")
MAX_CHUNK_SAFE = 4000


@dataclass
class _RenderResult:
    text: str
    next_index: int


def markdown_to_telegram_html(text: str) -> str:
    if not text:
        return ""
    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": False})
    md.enable("strikethrough")
    md.enable("table")
    tokens = md.parse(text)
    rendered = _render_blocks(tokens, 0, stop_type=None).text
    rendered = re.sub(r"\n{3,}", "\n\n", rendered).strip()
    return rendered


def split_plain_text(text: str, limit: int) -> list[str]:
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    start = 0
    while start < len(text):
        parts.append(text[start : start + limit])
        start += limit
    return parts


def split_telegram_html_chunks(html_text: str, limit: int = MAX_CHUNK_SAFE) -> list[str]:
    if not html_text:
        return []
    if len(html_text) <= limit:
        return [html_text]
    if limit <= 0:
        return [html_text]

    chunks: list[str] = []
    open_tags: list[str] = []
    current = ""

    def open_prefix() -> str:
        return "".join(open_tags)

    def close_suffix() -> str:
        return "".join(_closing_tag(tag) for tag in reversed(open_tags))

    def has_payload() -> bool:
        return len(current) > len(open_prefix())

    def flush() -> None:
        nonlocal current
        if has_payload():
            chunks.append(f"{current}{close_suffix()}")
            current = open_prefix()

    tokens: list[tuple[str, str]] = []
    last = 0
    for match in TAG_RE.finditer(html_text):
        start, end = match.span()
        if start > last:
            tokens.append(("text", html_text[last:start]))
        tokens.append(("tag", match.group(0)))
        last = end
    if last < len(html_text):
        tokens.append(("text", html_text[last:]))

    for token_type, value in tokens:
        if token_type == "tag":
            if len(current) + len(value) + len(close_suffix()) > limit:
                flush()
            current += value
            parsed = TAG_RE.match(value)
            if not parsed:
                continue
            is_closing = parsed.group(1) == "/"
            name = parsed.group(2).lower()
            if name not in SUPPORTED_TAGS:
                continue
            if is_closing:
                for i in range(len(open_tags) - 1, -1, -1):
                    if _tag_name(open_tags[i]) == name:
                        open_tags.pop(i)
                        break
            elif name not in SELF_CLOSING_TAGS and not value.endswith("/>"):
                open_tags.append(value)
            continue

        remaining = value
        while remaining:
            capacity = limit - len(current) - len(close_suffix())
            if capacity <= 0:
                flush()
                capacity = limit - len(current) - len(close_suffix())
                if capacity <= 0:
                    chunks.append(remaining[:limit])
                    remaining = remaining[limit:]
                    current = open_prefix()
                    continue
            take = min(capacity, len(remaining))
            current += remaining[:take]
            remaining = remaining[take:]
            if remaining:
                flush()

    if has_payload():
        chunks.append(f"{current}{close_suffix()}")
    return chunks


def _render_blocks(tokens: list[Token], start: int, stop_type: str | None) -> _RenderResult:
    i = start
    out: list[str] = []
    while i < len(tokens):
        t = tokens[i]
        if stop_type and t.type == stop_type:
            return _RenderResult("".join(out), i + 1)

        if t.type == "inline":
            out.append(_render_inline(t.children or []))
            i += 1
            continue

        if t.type == "paragraph_open":
            inner = _render_blocks(tokens, i + 1, "paragraph_close")
            out.append(inner.text.strip())
            out.append("\n\n")
            i = inner.next_index
            continue

        if t.type == "heading_open":
            inner = _render_blocks(tokens, i + 1, "heading_close")
            content = inner.text.strip()
            out.append(f"<b>{content}</b>\n\n" if content else "")
            i = inner.next_index
            continue

        if t.type == "blockquote_open":
            inner = _render_blocks(tokens, i + 1, "blockquote_close")
            content = inner.text.strip()
            if content:
                out.append(f"<blockquote>{content}</blockquote>\n\n")
            i = inner.next_index
            continue

        if t.type == "fence" or t.type == "code_block":
            code = html.escape((t.content or "").strip("\n"))
            out.append(f"<pre><code>{code}</code></pre>\n\n")
            i += 1
            continue

        if t.type == "bullet_list_open":
            rendered, next_index = _render_list(tokens, i + 1, ordered=False, start_num=1)
            out.append(rendered)
            i = next_index
            continue

        if t.type == "ordered_list_open":
            start_num = int(t.attrs.get("start", "1")) if t.attrs and "start" in t.attrs else 1
            rendered, next_index = _render_list(tokens, i + 1, ordered=True, start_num=start_num)
            out.append(rendered)
            i = next_index
            continue

        if t.type == "table_open":
            rendered, next_index = _render_table(tokens, i + 1)
            out.append(rendered)
            i = next_index
            continue

        if t.type in {"hr"}:
            out.append("──────────\n\n")
            i += 1
            continue

        # Skip unmatched closers and unknown block tokens safely.
        i += 1

    return _RenderResult("".join(out), i)


def _render_inline(tokens: list[Token], start: int = 0, stop_type: str | None = None) -> str:
    i = start
    out: list[str] = []
    while i < len(tokens):
        t = tokens[i]
        if stop_type and t.type == stop_type:
            break

        if t.type == "text":
            out.append(html.escape(t.content or ""))
            i += 1
            continue
        if t.type in {"softbreak", "hardbreak"}:
            out.append("\n")
            i += 1
            continue
        if t.type == "code_inline":
            out.append(f"<code>{html.escape(t.content or '')}</code>")
            i += 1
            continue

        if t.type in {"strong_open", "em_open", "s_open"}:
            close_type = {"strong_open": "strong_close", "em_open": "em_close", "s_open": "s_close"}[t.type]
            tag = {"strong_open": "b", "em_open": "i", "s_open": "s"}[t.type]
            j = _find_closing_token(tokens, i + 1, t.type, close_type)
            inner = _render_inline(tokens[i + 1 : j])
            out.append(f"<{tag}>{inner}</{tag}>")
            i = j + 1
            continue

        if t.type == "link_open":
            j = _find_closing_token(tokens, i + 1, "link_open", "link_close")
            inner = _render_inline(tokens[i + 1 : j])
            href = ""
            if t.attrs and "href" in t.attrs:
                href = t.attrs["href"] or ""
            href = _safe_link(href)
            if href:
                out.append(f'<a href="{html.escape(href, quote=True)}">{inner}</a>')
            else:
                out.append(inner)
            i = j + 1
            continue

        # Unknown inline token: try its literal text fallback.
        out.append(html.escape(t.content or ""))
        i += 1
    return "".join(out)


def _render_list(tokens: list[Token], start: int, ordered: bool, start_num: int) -> tuple[str, int]:
    i = start
    lines: list[str] = []
    number = start_num

    while i < len(tokens):
        t = tokens[i]
        if (ordered and t.type == "ordered_list_close") or (not ordered and t.type == "bullet_list_close"):
            i += 1
            break
        if t.type != "list_item_open":
            i += 1
            continue

        item_result = _render_blocks(tokens, i + 1, "list_item_close")
        item = item_result.text.strip()
        if item:
            prefix = f"{number}. " if ordered else "• "
            item_lines = item.splitlines()
            lines.append(prefix + item_lines[0])
            for extra in item_lines[1:]:
                lines.append("  " + extra)
        if ordered:
            number += 1
        i = item_result.next_index

    if not lines:
        return "", i
    return "\n".join(lines) + "\n\n", i


def _render_table(tokens: list[Token], start: int) -> tuple[str, int]:
    i = start
    rows: list[list[str]] = []
    row: list[str] = []
    while i < len(tokens):
        t = tokens[i]
        if t.type == "table_close":
            i += 1
            break
        if t.type == "tr_open":
            row = []
            i += 1
            continue
        if t.type in {"th_open", "td_open"}:
            close_type = "th_close" if t.type == "th_open" else "td_close"
            inner = _render_blocks(tokens, i + 1, close_type)
            row.append(re.sub(r"<[^>]+>", "", inner.text).strip())
            i = inner.next_index
            continue
        if t.type == "tr_close":
            if row:
                rows.append(row)
            i += 1
            continue
        i += 1

    if not rows:
        return "", i

    lines = [" | ".join(cell for cell in r) for r in rows]
    table_text = html.escape("\n".join(lines))
    return f"<pre><code>{table_text}</code></pre>\n\n", i


def _find_closing_token(tokens: list[Token], start: int, open_type: str, close_type: str) -> int:
    depth = 1
    i = start
    while i < len(tokens):
        if tokens[i].type == open_type:
            depth += 1
        elif tokens[i].type == close_type:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(tokens) - 1


def _safe_link(url: str) -> str:
    lower = url.lower()
    if lower.startswith(("http://", "https://", "mailto:")):
        return url
    return ""


def _tag_name(open_tag: str) -> str:
    m = TAG_RE.match(open_tag)
    return m.group(2).lower() if m else ""


def _closing_tag(open_tag: str) -> str:
    name = _tag_name(open_tag)
    return f"</{name}>" if name else ""
