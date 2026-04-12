import asyncio
import random
import re
from dataclasses import dataclass, field

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import (
    At,
    BaseMessageComponent,
    Face,
    Image,
    Plain,
    Reply,
)

from ..config import PluginConfig
from ..model import OutContext, StepName, StepResult
from .base import BaseStep

INVISIBLE_CHAR_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u2060]+")
KAOMOJI_PATTERN = re.compile(
    r"("
    r"[(\[\uFF08\u3010<]"
    r"[^()\[\]\uFF08\uFF09\u3010\u3011<>]*?"
    r"[^\u4e00-\u9fffA-Za-z0-9\s]"
    r"[^()\[\]\uFF08\uFF09\u3010\u3011<>]*?"
    r"[)\]\uFF09\u3011>]"
    r")"
    r"|"
    r"([\u25b3\u25a6\u30fb\u89e6\u8805\u301c\u30ef\u7b2f\^><\u2267\u2665\uff5e\uff40\u7c32\u2764\u20ac\u304c\u5065\u279f\u2190\u25ba\u25c4\u2754]{2,15})"
)
SMART_SPLIT_BRACKET_RE = re.compile(r"[(\[\uFF08\u3010](?=.*[\u4e00-\u9fff]).*?[)\]\uFF09\u3011]")
SMART_SPLIT_QUOTES = {
    '"',
    "'",
    "\u201c",
    "\u201d",
    "\u2018",
    "\u2019",
    "\u300c",
    "\u300d",
    "\u300e",
    "\u300f",
}
SMART_SPLIT_SEPARATORS = {"\uff0c", ",", " ", "\u3002", ";", "\n"}


def _normalize_plain_text(text: str) -> str:
    return INVISIBLE_CHAR_RE.sub("", text or "")


def _is_english_letter(char: str) -> bool:
    return "a" <= char.lower() <= "z"


def _is_chinese_char(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


def _protect_kaomoji(text: str) -> tuple[str, dict[str, str]]:
    protected = text
    placeholder_to_kaomoji: dict[str, str] = {}
    matches = KAOMOJI_PATTERN.findall(text)

    for idx, match in enumerate(matches):
        kaomoji = match[0] or match[1]
        if not kaomoji:
            continue
        placeholder = f"__OUTPUTPRO_KAOMOJI_{idx}__"
        protected = protected.replace(kaomoji, placeholder, 1)
        placeholder_to_kaomoji[placeholder] = kaomoji

    return protected, placeholder_to_kaomoji


def _recover_kaomoji(sentences: list[str], placeholder_to_kaomoji: dict[str, str]) -> list[str]:
    recovered: list[str] = []
    for sentence in sentences:
        restored = sentence
        for placeholder, kaomoji in placeholder_to_kaomoji.items():
            restored = restored.replace(placeholder, kaomoji)
        recovered.append(restored)
    return recovered


def _smart_split_sentences(text: str, separators: set[str] | None = None) -> list[str]:
    active_separators = separators or SMART_SPLIT_SEPARATORS
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = re.sub(r"\n\s*([\uff0c\u3002\s])", r"\n\1", text)
    text = re.sub(r"([\uff0c\u3002\s])\s*\n", r"\1\n", text)

    text_len = len(text)
    if text_len < 3:
        return list(text) if random.random() < 0.01 else [text]

    inside_quote = [False] * text_len
    in_quote = False
    current_quote_char = ""
    for idx, ch in enumerate(text):
        if ch in SMART_SPLIT_QUOTES:
            if not in_quote:
                in_quote = True
                current_quote_char = ch
            elif ch == current_quote_char or (
                ch in {'"', "'"} and current_quote_char in {'"', "'"}
            ):
                in_quote = False
                current_quote_char = ""
            inside_quote[idx] = False
        else:
            inside_quote[idx] = in_quote

    segments: list[tuple[str, str]] = []
    current_segment = ""
    idx = 0
    while idx < len(text):
        char = text[idx]
        if char in active_separators:
            if inside_quote[idx]:
                can_split = False
            elif char == "\n":
                can_split = True
            else:
                can_split = True
                if idx > 0 and text[idx - 1] in {":", "\uff1a"}:
                    can_split = False
                if idx < len(text) - 1 and text[idx + 1] in {":", "\uff1a"}:
                    can_split = False
                if can_split and char == " " and 0 < idx < len(text) - 1:
                    prev_char = text[idx - 1]
                    next_char = text[idx + 1]
                    prev_is_alnum = prev_char.isdigit() or _is_english_letter(prev_char)
                    next_is_alnum = next_char.isdigit() or _is_english_letter(next_char)
                    if prev_is_alnum and next_is_alnum:
                        can_split = False

            if can_split:
                if current_segment:
                    segments.append((current_segment, char))
                elif char in {" ", "\n"}:
                    segments.append(("", char))
                current_segment = ""
            else:
                current_segment += char
        else:
            current_segment += char
        idx += 1

    if current_segment:
        segments.append((current_segment, ""))

    segments = [(content, sep) for content, sep in segments if content or sep]
    if not segments:
        return [text] if text else []

    if text_len < 12:
        split_strength = 0.2
    elif text_len < 32:
        split_strength = 0.6
    else:
        split_strength = 0.7
    merge_probability = 1.0 - split_strength

    merged_segments: list[tuple[str, str]] = []
    idx = 0
    while idx < len(segments):
        current_content, current_sep = segments[idx]
        if (
            idx + 1 < len(segments)
            and current_sep != "\n"
            and random.random() < merge_probability
            and current_content
        ):
            next_content, next_sep = segments[idx + 1]
            if next_content:
                merged_segments.append((current_content + current_sep + next_content, next_sep))
            else:
                merged_segments.append((current_content, next_sep))
            idx += 2
            continue
        merged_segments.append((current_content, current_sep))
        idx += 1

    final_sentences = [
        content for content, _sep in merged_segments if content and content.strip()
    ]
    return final_sentences or ([text] if text else [])


def _split_plain_text_maibot_style(text: str, separators: set[str] | None = None) -> list[str]:
    protected_text, kaomoji_mapping = _protect_kaomoji(text)
    cleaned_text = SMART_SPLIT_BRACKET_RE.sub("", protected_text)

    if cleaned_text == "":
        return ["\u545c\u5463"]

    sentences = _smart_split_sentences(cleaned_text, separators=separators)
    return _recover_kaomoji(sentences, kaomoji_mapping)


def _is_leading_empty_segment(seg: "Segment") -> bool:
    for comp in seg.components:
        if isinstance(comp, Plain):
            if _normalize_plain_text(comp.text).strip():
                return False
            continue
        if isinstance(comp, At | Reply):
            continue
        return False
    return True


@dataclass
class Segment:
    """逻辑分段单元"""

    components: list[BaseMessageComponent] = field(default_factory=list)

    def append(self, comp: BaseMessageComponent):
        self.components.append(comp)

    def extend(self, comps: list[BaseMessageComponent]):
        self.components.extend(comps)

    @property
    def text(self) -> str:
        """仅提取文本内容（用于延迟计算）"""
        return "".join(c.text for c in self.components if isinstance(c, Plain))

    @property
    def has_media(self) -> bool:
        """是否包含非文本组件（图片 / 表情 / 其他）"""
        return any(not isinstance(c, Plain) for c in self.components)

    @property
    def is_empty(self) -> bool:
        """是否为空段（无文本、无媒体）"""
        return not self.text.strip() and not self.has_media


class SplitStep(BaseStep):
    name = StepName.SPLIT

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.split
        self.context = config.context

    @staticmethod
    def _supports_typing(ctx: OutContext) -> bool:
        platform_name = ctx.event.get_platform_name()
        if platform_name == "telegram":
            return True
        if platform_name != "aiocqhttp":
            return False
        if ctx.event.get_group_id():
            return False
        user_id = str(ctx.event.get_sender_id() or "").strip()
        if not user_id:
            return False
        bot = getattr(ctx.event, "bot", None)
        api = getattr(bot, "api", None)
        return bool(api and hasattr(api, "call_action"))

    @staticmethod
    async def _show_typing_once(ctx: OutContext) -> bool:
        if not SplitStep._supports_typing(ctx):
            return False
        if ctx.event.get_platform_name() == "telegram":
            try:
                await ctx.event.send_typing()
                return True
            except Exception:
                logger.debug("[Splitter] 调用 Telegram 输入状态失败", exc_info=True)
                return False
        user_id = str(ctx.event.get_sender_id() or "").strip()
        bot = getattr(ctx.event, "bot", None)
        api = getattr(bot, "api", None)
        try:
            await api.call_action(
                "set_input_status",
                user_id=user_id,
                event_type=1,
            )
            return True
        except Exception:
            logger.debug(
                f"[Splitter] 调用 QQ 私聊输入状态失败: user_id={user_id}",
                exc_info=True,
            )
            return False

    async def _sleep_with_typing(self, ctx: OutContext, delay: float) -> None:
        if delay <= 0:
            return
        if not bool(getattr(self.cfg, "simulate_typing", True)):
            await asyncio.sleep(delay)
            return
        if not self._supports_typing(ctx):
            await asyncio.sleep(delay)
            return

        lead_in = min(0.15, delay * 0.1)
        tail_out = min(0.25, delay * 0.15)
        active_window = max(0.0, delay - lead_in - tail_out)

        if lead_in > 0:
            await asyncio.sleep(lead_in)

        if active_window <= 0.35:
            await self._show_typing_once(ctx)
            if active_window > 0:
                await asyncio.sleep(active_window)
        else:
            interval = min(2.5, max(0.9, active_window / 3))
            remaining = active_window
            while remaining > 0:
                await self._show_typing_once(ctx)
                sleep_time = min(interval, remaining)
                await asyncio.sleep(sleep_time)
                remaining -= sleep_time

        if tail_out > 0:
            await asyncio.sleep(tail_out)

    async def handle(self, ctx: OutContext) -> StepResult:
        """
        对消息进行拆分并发送。
        最后一段会回填到原 chain 中。
        """
        platform_name = ctx.event.get_platform_name()
        if platform_name not in {"aiocqhttp", "telegram", "lark"}:
            return StepResult()

        max_length = int(getattr(self.cfg, "max_length", 0) or 0)
        if max_length > 0:
            chain_length = self._get_chain_text_length(ctx.chain)
            if chain_length > max_length:
                logger.debug(f"[Splitter] 超过分段字数上限，跳过分段：len={chain_length}, limit={max_length}")
                return StepResult()

        segments = self._split_chain(ctx.chain)

        # 后处理
        for seg in segments:
            for comp in seg.components:
                if isinstance(comp, Plain):
                    comp.text = comp.text.rstrip()
            for comp in reversed(seg.components):
                if isinstance(comp, Plain) and comp.text.strip():
                    comp.text = self.cfg.tail_punc_re.sub("", comp.text)
                    break

        dropped_leading_empty = False
        leading_header: list[BaseMessageComponent] = []
        while segments and _is_leading_empty_segment(segments[0]):
            dropped = segments.pop(0)
            for comp in dropped.components:
                if isinstance(comp, Reply | At):
                    leading_header.append(comp)
            dropped_leading_empty = True

        if not segments:
            ctx.chain.clear()
            return StepResult(msg="分段结果为空，已丢弃")

        if leading_header:
            segments[0].components = [*leading_header, *segments[0].components]

        if len(segments) <= 1:
            if dropped_leading_empty:
                ctx.chain.clear()
                ctx.chain.extend(segments[0].components)
            return StepResult()

        logger.debug(f"[Splitter] 消息被分为 {len(segments)} 段")

        # 逐段发送（最后一段不立即发）
        for i in range(len(segments) - 1):
            seg = segments[i]

            if seg.is_empty:
                continue

            try:
                send_comps = self._wrap_plain_with_zwsp(seg.components)
                await self.plugin_config.context.send_message(
                    ctx.event.unified_msg_origin,
                    MessageChain(send_comps),
                )
                delay = self._calc_smart_split_delay(seg.text)
                await self._sleep_with_typing(ctx, delay)
            except Exception as e:
                logger.error(f"[Splitter] 发送分段 {i + 1} 失败: {e}")

        # 最后一段回填给主流程继续处理
        ctx.chain.clear()
        if not segments[-1].is_empty:
            last_seg = segments[-1]
            last_comps = self._wrap_plain_with_zwsp(last_seg.components)
            ctx.chain.extend(last_comps)

        return StepResult(msg="分段回复完成")

    @staticmethod
    def _get_chain_text_length(chain: list[BaseMessageComponent]) -> int:
        return sum(len(comp.text) for comp in chain if isinstance(comp, Plain))

    def _get_smart_split_separators(self) -> set[str]:
        separators = set(SMART_SPLIT_SEPARATORS)
        for token in getattr(self.cfg, "extra_separators", []) or []:
            text = str(token or "").strip()
            if text:
                separators.add(text[0])
        return separators

    def _calc_smart_split_delay(self, text: str) -> float:
        """MaiBot 风格的智能分段发送延迟"""
        if not text:
            return 0.0

        stripped_text = text.strip()
        chinese_chars = sum(_is_chinese_char(char) for char in text)
        if chinese_chars == 1 and len(stripped_text) == 1:
            return 1.2

        total_time = 0.0
        for char in text:
            total_time += 0.3 if _is_chinese_char(char) else 0.15

        return total_time

    def _wrap_plain_with_zwsp(
        self, comps: list[BaseMessageComponent]
    ) -> list[BaseMessageComponent]:
        wrapped: list[BaseMessageComponent] = []
        for comp in comps:
            if isinstance(comp, Plain) and comp.text:
                text = comp.text
                if not text.startswith("\u200b"):
                    text = "\u200b" + text
                if not text.endswith("\u200b"):
                    text = text + "\u200b"
                wrapped.append(Plain(text))
            else:
                wrapped.append(comp)
        return wrapped

    def _split_chain(self, chain: list[BaseMessageComponent]) -> list[Segment]:
        """
        核心分段逻辑
        """
        smart_split_separators = self._get_smart_split_separators()
        segments: list[Segment] = []
        current = Segment()
        exhausted = False
        # Reply / At
        pending_prefix: list[BaseMessageComponent] = []

        # 占位符逻辑
        PLACEHOLDER = "__OUTPUTPRO_SPACING_PLACEHOLDER__"
        MIXED_SPACE_PLACEHOLDER = "__OUTPUTPRO_MIXED_SPACE_PLACEHOLDER__"
        PROTECTED_SPACE = "\u200b \u200b"
        has_protected = False
        has_mixed_protected = False

        def _is_cjk_context(ch: str) -> bool:
            """判断是否为中文字符或中文标点"""
            return (
                "\u4e00" <= ch <= "\u9fa5"  # 汉字
                or "\u3000" <= ch <= "\u303f"  # 中文标点
                or "\uff00" <= ch <= "\uffef"  # 全角符号
            )

        def _merge_space_if_needed(target: Segment, comps: list[BaseMessageComponent]):
            if target.components and comps:
                if isinstance(comps[0], Plain):
                    comps[0].text = " " + comps[0].text

        def _attach_pending(target: Segment):
            if pending_prefix:
                target.extend(pending_prefix)
                pending_prefix.clear()

        def _append_to_tail(comps: list[BaseMessageComponent]):
            nonlocal current
            if exhausted and segments:
                _merge_space_if_needed(segments[-1], comps)
                segments[-1].extend(comps)
                return
            if current.components:
                _merge_space_if_needed(current, comps)
                current.extend(comps)
            elif segments:
                _merge_space_if_needed(segments[-1], comps)
                segments[-1].extend(comps)
            else:
                segments.append(Segment(comps))

        def push(seg: Segment):
            nonlocal exhausted
            if not seg.components:
                return

            count = self.cfg.max_count
            if count > 0:
                if len(segments) < count:
                    segments.append(seg)
                    if len(segments) >= count:
                        exhausted = True
                else:
                    exhausted = True
                    _append_to_tail(seg.components)
            else:
                segments.append(seg)

        def flush():
            nonlocal current
            if current.components:
                push(current)
                current = Segment()

        for comp in chain:
            # Reply / At
            if isinstance(comp, Reply | At):
                pending_prefix.append(comp)
                continue

            # Plain
            if isinstance(comp, Plain):
                text = comp.text or ""
                if not text:
                    continue

                # 1. 保护 Reply + At 后的等宽空格
                if (
                    not has_protected
                    and len(pending_prefix) >= 2
                    and isinstance(pending_prefix[0], Reply)
                    and isinstance(pending_prefix[1], At)
                    and text.startswith(PROTECTED_SPACE)
                ):
                    text = text.replace(PROTECTED_SPACE, PLACEHOLDER, 1)
                    has_protected = True

                # 2. 空格保护
                if " " in text:

                    def replace_mixed(m):
                        start = m.start()
                        end = m.end()
                        prev_ch = text[start - 1] if start > 0 else ""
                        next_ch = text[end] if end < len(text) else ""

                        # 只有当两侧都是中文(CJK)时，才返回原空格
                        if (
                            prev_ch
                            and next_ch
                            and _is_cjk_context(prev_ch)
                            and _is_cjk_context(next_ch)
                        ):
                            return m.group()

                        # 其余情况均视为需要保护的间距
                        return MIXED_SPACE_PLACEHOLDER * (end - start)

                    original_text = text
                    text = re.sub(r" +", replace_mixed, text)
                    if text != original_text:
                        has_mixed_protected = True

                if exhausted:
                    if segments:
                        _attach_pending(segments[-1])
                    else:
                        _attach_pending(current)
                    _append_to_tail([Plain(text)])
                    continue

                split_parts = _split_plain_text_maibot_style(
                    text,
                    separators=smart_split_separators,
                )
                for idx, part in enumerate(split_parts):
                    if not part:
                        continue
                    _attach_pending(current)
                    current.append(Plain(part))
                    if idx < len(split_parts) - 1:
                        flush()
                continue

            # Image / Face
            if isinstance(comp, Image | Face):
                if current.components:
                    _attach_pending(current)
                    current.append(comp)
                elif segments:
                    _attach_pending(segments[-1])
                    segments[-1].append(comp)
                else:
                    _attach_pending(current)
                    current.append(comp)
                continue

            # 其他
            if exhausted:
                if segments:
                    _attach_pending(segments[-1])
                else:
                    _attach_pending(current)
                _append_to_tail([comp])
                continue

            flush()
            seg = Segment()
            _attach_pending(seg)
            seg.append(comp)
            push(seg)

        if current.components:
            push(current)

        def _restore_placeholders(comp: BaseMessageComponent):
            if isinstance(comp, Plain):
                if has_protected:
                    comp.text = comp.text.replace(PLACEHOLDER, PROTECTED_SPACE)
                if has_mixed_protected:
                    comp.text = comp.text.replace(MIXED_SPACE_PLACEHOLDER, " ")

        for seg in segments:
            for comp in seg.components:
                _restore_placeholders(comp)
        for comp in current.components:
            _restore_placeholders(comp)

        return segments
