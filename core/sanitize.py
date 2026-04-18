from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

import emoji

from astrbot.core.message.components import (
    BaseMessageComponent,
    Location,
    Music,
    Node,
    Nodes,
    Plain,
    Record,
    Reply,
    Share,
    Unknown,
)

from .config import CleanConfig, ReplaceConfig

SQUARE_BRACKET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("square", re.compile(r"\[[^\[\]\r\n]{0,120}\]")),
    ("square_cn", re.compile(r"\u3010[^\u3010\u3011\r\n]{0,120}\u3011")),
    ("square_alt", re.compile(r"\u3014[^\u3014\u3015\r\n]{0,120}\u3015")),
)

PARENTHESIS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("paren", re.compile(r"\([^()\r\n]{0,120}\)")),
    ("paren_cn", re.compile(r"\uFF08[^\uFF08\uFF09\r\n]{0,120}\uFF09")),
    ("brace", re.compile(r"\{[^{}\r\n]{0,120}\}")),
    ("brace_cn", re.compile(r"\uFF5B[^\uFF5B\uFF5D\r\n]{0,120}\uFF5D")),
)

GENERIC_TAG_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])"
    r"(?P<tag>(?P<delim>(?P<sym>[&@#~|=+%^])(?P=sym){1,2})(?P<body>[^\r\n]{0,40}?)(?P=delim))"
    r"(?![0-9A-Za-z])"
)
TTS_SPEED_TAG_PATTERN = re.compile(r"<#\s*\d+(?:\.\d{1,2})?\s*#>")
ASCII_WRAPPED_TAG_RE = re.compile(
    r"(?:EMO(?:\s*[:：-]\s*[A-Za-z_-]{1,24})?|[A-Za-z][A-Za-z0-9_-]{0,23})",
    re.IGNORECASE,
)

MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
LINE_SPACE_RE = re.compile(r"[ \t]*\n[ \t]*")

TEXT_ATTRS: dict[type[BaseMessageComponent], tuple[str, ...]] = {
    Plain: ("text",),
    Unknown: ("text",),
    Record: ("text",),
    Reply: ("text", "message_str"),
    Share: ("title", "content"),
    Music: ("title", "content"),
    Location: ("title", "content"),
}


@dataclass(slots=True)
class TextProcessReport:
    removed: dict[str, list[str]] = field(
        default_factory=lambda: defaultdict(list),
    )
    replacements: list[tuple[str, str]] = field(default_factory=list)

    def add_removed(self, label: str, values: list[str]) -> None:
        if values:
            self.removed[label].extend(values)

    def extend(self, other: "TextProcessReport") -> None:
        for label, values in other.removed.items():
            self.removed[label].extend(values)
        self.replacements.extend(other.replacements)

    def has_removed(self) -> bool:
        return any(self.removed.values())


def _append_unique(target: list[tuple[str, str]], pair: tuple[str, str]) -> None:
    if pair not in target:
        target.append(pair)


def _should_apply_cosmetic_cleanup(text: str, cfg: CleanConfig) -> bool:
    if cfg.text_threshold <= 0:
        return False
    return len(text) < cfg.text_threshold


def _extract_wrapped_inner_text(wrapped: str) -> str:
    if len(wrapped) < 2:
        return wrapped.strip()
    return wrapped[1:-1].strip()


def _looks_like_wrapped_markup(inner: str) -> bool:
    if not inner or len(inner) > 24:
        return False

    if any(ch in inner for ch in "\r\n\t"):
        return False

    if inner.isdigit():
        return False

    if re.search(r"[\u4e00-\u9fff]", inner):
        return False

    if re.search(r"[，。！？；：,.!?;:/\\\\]", inner):
        return False

    if " " in inner:
        return False

    return bool(ASCII_WRAPPED_TAG_RE.fullmatch(inner))


def _remove_patterns(
    text: str,
    patterns: tuple[tuple[str, re.Pattern[str]], ...],
    label: str,
    report: TextProcessReport,
) -> str:
    current = text
    for _ in range(8):
        changed = False
        for _, pattern in patterns:
            removed: list[str] = []

            def _replace(match: re.Match[str]) -> str:
                wrapped = match.group(0)
                if not _looks_like_wrapped_markup(_extract_wrapped_inner_text(wrapped)):
                    return wrapped
                removed.append(wrapped)
                return ""

            updated = pattern.sub(_replace, current)
            if not removed:
                continue

            report.add_removed(label, removed)
            current = updated
            changed = True

        if not changed:
            break
    return current


def _remove_generic_tags(
    text: str,
    label: str,
    report: TextProcessReport,
) -> str:
    current = text
    for _ in range(8):
        matches = [
            match.group("tag") for match in GENERIC_TAG_PATTERN.finditer(current)
        ]
        if not matches:
            break
        report.add_removed(label, matches)
        current = GENERIC_TAG_PATTERN.sub("", current)
    return current


def _tidy_spacing(text: str) -> str:
    text = LINE_SPACE_RE.sub("\n", text)
    return MULTI_SPACE_RE.sub(" ", text)


def clean_text(
    text: str,
    cfg: CleanConfig,
    *,
    emotion_tag: bool | None = None,
) -> tuple[str, TextProcessReport]:
    report = TextProcessReport()
    if not text:
        return text, report

    cleaned = text
    apply_emotion_tag = cfg.emotion_tag if emotion_tag is None else emotion_tag

    if cfg.bracket:
        cleaned = _remove_patterns(
            cleaned,
            SQUARE_BRACKET_PATTERNS,
            "bracket_content",
            report,
        )

    if cfg.parenthesis:
        cleaned = _remove_patterns(
            cleaned,
            PARENTHESIS_PATTERNS,
            "parenthetical_content",
            report,
        )

    if apply_emotion_tag:
        cleaned = _remove_generic_tags(cleaned, "wrapped_tag", report)
        speed_tags = TTS_SPEED_TAG_PATTERN.findall(cleaned)
        if speed_tags:
            report.add_removed("tts_speed_tag", speed_tags)
            cleaned = TTS_SPEED_TAG_PATTERN.sub("", cleaned)

    if cleaned != text:
        cleaned = _tidy_spacing(cleaned)

    if _should_apply_cosmetic_cleanup(cleaned, cfg):
        if cfg.emoji:
            emojis = [char for char in cleaned if char in emoji.EMOJI_DATA]
            if emojis:
                report.add_removed("emoji", emojis)
                cleaned = emoji.replace_emoji(cleaned, replace="")

        if cfg.lead:
            for prefix in cfg.lead:
                if prefix and cleaned.startswith(prefix):
                    report.add_removed("lead", [prefix])
                    cleaned = cleaned[len(prefix) :]
                    break

        if cfg.tail:
            for suffix in cfg.tail:
                if suffix and cleaned.endswith(suffix):
                    report.add_removed("tail", [suffix])
                    cleaned = cleaned[: -len(suffix)]
                    break

        if cfg.punctuation:
            matches = re.findall(cfg.punctuation, cleaned)
            if matches:
                report.add_removed("punctuation", matches)
                cleaned = re.sub(cfg.punctuation, "", cleaned)

    return cleaned, report


def _iter_text_attrs(component: BaseMessageComponent):
    for component_type, attrs in TEXT_ATTRS.items():
        if isinstance(component, component_type):
            for attr in attrs:
                value = getattr(component, attr, None)
                if isinstance(value, str) and value:
                    yield attr, value
            break


def _prune_empty_components(chain: list[BaseMessageComponent]) -> None:
    kept: list[BaseMessageComponent] = []
    for component in chain:
        if isinstance(component, Node):
            _prune_empty_components(component.content)
            if not component.content:
                continue
        elif isinstance(component, Nodes):
            for node in component.nodes:
                _prune_empty_components(node.content)
            component.nodes = [node for node in component.nodes if node.content]
            if not component.nodes:
                continue
        elif isinstance(component, Reply) and component.chain:
            _prune_empty_components(component.chain)

        if isinstance(component, (Plain, Unknown)):
            text = getattr(component, "text", "")
            if not text or not text.strip():
                continue

        kept.append(component)

    chain[:] = kept


def sanitize_chain(
    chain: list[BaseMessageComponent],
    cfg: CleanConfig,
    *,
    emotion_tag: bool | None = None,
) -> TextProcessReport:
    report = TextProcessReport()
    for component in chain:
        if isinstance(component, Node):
            report.extend(
                sanitize_chain(component.content, cfg, emotion_tag=emotion_tag)
            )
        elif isinstance(component, Nodes):
            for node in component.nodes:
                report.extend(
                    sanitize_chain(node.content, cfg, emotion_tag=emotion_tag)
                )
        elif isinstance(component, Reply) and component.chain:
            report.extend(sanitize_chain(component.chain, cfg, emotion_tag=emotion_tag))

        for attr, value in _iter_text_attrs(component):
            cleaned, local_report = clean_text(
                value,
                cfg,
                emotion_tag=emotion_tag,
            )
            if cleaned != value:
                setattr(component, attr, cleaned)
            report.extend(local_report)

    _prune_empty_components(chain)
    return report


def _unescape_replace_text(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\t", "\t")
        .replace("\\s", " ")
        .replace("\\\\", "\\")
    )


def build_replace_pairs(cfg: ReplaceConfig) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in cfg.words:
        if not item or not item.strip():
            continue

        raw_old, sep, raw_new = item.partition(" ")
        old = _unescape_replace_text(raw_old)
        if not old:
            continue

        if not sep:
            new = cfg.default_new_word * len(old)
        else:
            new = _unescape_replace_text(raw_new)

        pairs.append((old, new))

    return pairs


def replace_text(
    text: str,
    cfg: ReplaceConfig,
) -> tuple[str, list[tuple[str, str]]]:
    if not text:
        return text, []

    pairs = build_replace_pairs(cfg)
    if not pairs:
        return text, []

    updated = text
    changes: list[tuple[str, str]] = []
    for old, new in pairs:
        if old in updated:
            updated = updated.replace(old, new)
            _append_unique(changes, (repr(old), repr(new)))

    return updated, changes


def replace_in_chain(
    chain: list[BaseMessageComponent],
    cfg: ReplaceConfig,
) -> list[tuple[str, str]]:
    pairs = build_replace_pairs(cfg)
    if not pairs:
        return []

    changes: list[tuple[str, str]] = []
    for component in chain:
        if isinstance(component, Node):
            for pair in replace_in_chain(component.content, cfg):
                _append_unique(changes, pair)
        elif isinstance(component, Nodes):
            for node in component.nodes:
                for pair in replace_in_chain(node.content, cfg):
                    _append_unique(changes, pair)
        elif isinstance(component, Reply) and component.chain:
            for pair in replace_in_chain(component.chain, cfg):
                _append_unique(changes, pair)

        for attr, value in _iter_text_attrs(component):
            updated = value
            for old, new in pairs:
                if old in updated:
                    updated = updated.replace(old, new)
                    _append_unique(changes, (repr(old), repr(new)))
            if updated != value:
                setattr(component, attr, updated)

    _prune_empty_components(chain)
    return changes


def transform_text_in_chain(
    chain: list[BaseMessageComponent],
    transform,
) -> list[tuple[str, str]]:
    changes: list[tuple[str, str]] = []
    for component in chain:
        if isinstance(component, Node):
            for pair in transform_text_in_chain(component.content, transform):
                _append_unique(changes, pair)
        elif isinstance(component, Nodes):
            for node in component.nodes:
                for pair in transform_text_in_chain(node.content, transform):
                    _append_unique(changes, pair)
        elif isinstance(component, Reply) and component.chain:
            for pair in transform_text_in_chain(component.chain, transform):
                _append_unique(changes, pair)

        for attr, value in _iter_text_attrs(component):
            updated = transform(value)
            if updated == value:
                continue
            setattr(component, attr, updated)
            _append_unique(changes, (value, updated))

    _prune_empty_components(chain)
    return changes


def collect_visible_text(chain: list[BaseMessageComponent]) -> str:
    texts: list[str] = []
    for component in chain:
        if isinstance(component, Node):
            nested = collect_visible_text(component.content)
            if nested:
                texts.append(nested)
            continue

        if isinstance(component, Nodes):
            for node in component.nodes:
                nested = collect_visible_text(node.content)
                if nested:
                    texts.append(nested)
            continue

        if isinstance(component, Reply) and component.chain:
            nested = collect_visible_text(component.chain)
            if nested:
                texts.append(nested)

        for _, value in _iter_text_attrs(component):
            value = value.strip()
            if value:
                texts.append(value)

    return " ".join(texts)


def format_removed_summary(report: TextProcessReport) -> str:
    if not report.has_removed():
        return ""

    parts: list[str] = []
    for label, items in report.removed.items():
        unique_items = list(dict.fromkeys(items))
        if len(unique_items) == 1:
            parts.append(f"{label}: {unique_items[0]}")
            continue

        preview = " / ".join(unique_items[:3])
        suffix = f" (+{len(unique_items) - 3})" if len(unique_items) > 3 else ""
        parts.append(f"{label}: {preview}{suffix}")

    return "cleaned -> " + "; ".join(parts)
