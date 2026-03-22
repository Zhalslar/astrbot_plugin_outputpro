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


def _normalize_plain_text(text: str) -> str:
    return INVISIBLE_CHAR_RE.sub("", text or "")


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

    async def handle(self, ctx: OutContext) -> StepResult:
        """
        对消息进行拆分并发送。
        最后一段会回填到原 chain 中。
        """
        platform_name = ctx.event.get_platform_name()
        if platform_name not in {"aiocqhttp", "telegram", "lark"}:
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
                delay = self._calc_delay(seg.text)
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"[Splitter] 发送分段 {i + 1} 失败: {e}")

        # 最后一段回填给主流程继续处理
        ctx.chain.clear()
        if not segments[-1].is_empty:
            last_seg = segments[-1]
            last_comps = self._wrap_plain_with_zwsp(last_seg.components)
            ctx.chain.extend(last_comps)

        return StepResult(msg="分段回复完成")

    def _calc_delay(self, text: str) -> float:
        """计算延迟(拟人打字)"""
        if not text:
            return 0.0

        cfg = self.cfg
        n = len(text)
        base_char_time = 1.0 / max(cfg.typing_cps, 1e-3)

        jitter = random.uniform(
            1.0 - cfg.typing_jitter,
            1.0 + cfg.typing_jitter,
        )
        char_time = base_char_time * jitter
        char_time = min(max(char_time, 0.02), 0.20)

        delay = n * char_time

        # 标点 / 换行停顿
        delay += (
            text.count("，") * 0.06
            + text.count("。") * 0.18
            + text.count("！") * 0.14
            + text.count("？") * 0.16
            + text.count("…") * 0.22
            + text.count("\n") * 0.35
        )

        # 短停顿
        if random.random() < cfg.pause_prob:
            delay += random.uniform(*cfg.pause_range)

        # 长停顿（少量）
        if random.random() < cfg.long_pause_prob:
            delay += random.uniform(*cfg.long_pause_range)

        return min(delay, cfg.max_delay_cap)

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

                stack: list[str] = []
                pattern = self.cfg.split_re
                i = 0
                n = len(text)
                buf = ""

                while i < n:
                    ch = text[i]
                    is_opener = ch in self.cfg.pair_map

                    if ch in self.cfg.quote_chars:
                        if stack and stack[-1] == ch:
                            stack.pop()
                        else:
                            stack.append(ch)
                        buf += ch
                        i += 1
                        continue

                    if stack:
                        expected_closer = self.cfg.pair_map.get(stack[-1])
                        if ch == expected_closer:
                            stack.pop()
                        elif is_opener:
                            stack.append(ch)
                        buf += ch
                        i += 1
                        continue

                    if is_opener:
                        stack.append(ch)
                        buf += ch
                        i += 1
                        continue

                    m = pattern.match(text, i)
                    if m:
                        delim = m.group()

                        if not buf and not current.components:
                            i += len(delim)
                            continue

                        buf += delim
                        if buf:
                            _attach_pending(current)
                            current.append(Plain(buf))
                            flush()
                            buf = ""
                            if exhausted:
                                remaining = text[i + len(delim) :]
                                if remaining:
                                    _append_to_tail([Plain(remaining)])
                                break
                        i += len(delim)
                    else:
                        buf += ch
                        i += 1

                if buf and not exhausted:
                    _attach_pending(current)
                    current.append(Plain(buf))
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
