import asyncio
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

        # 最大分段数（<=0 表示不限制）
        self.max_count = self.cfg.max_count

        # 最大文本长度归一化，用于映射到 min/max
        self._max_len_for_delay = 150

        # 末尾标点清除正则
        tail_punc = ".,，。、;；:："
        self.tail_punc_re = re.compile(f"[{re.escape(tail_punc)}]+$")
        self._pair_map = {
            "“": "”",
            "《": "》",
            "（": "）",
            "(": ")",
            "[": "]",
            "{": "}",
            "‘": "’",
            "【": "】",
            "<": ">",
        }
        self._quote_chars = {'"', "'", "`"}

    async def handle(self, ctx: OutContext) -> StepResult:
        """
        对消息进行拆分并发送。
        最后一段会回填到原 chain 中。
        """
        segments = self._split_chain(ctx.chain)

        if len(segments) <= 1:
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
                delay = self._calc_delay(len(seg.text))
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


    def _calc_delay(self, text_len: int) -> float:
        """
        根据文本长度计算延迟（线性映射到 min_delay ~ max_delay）：
        - 短文本 → 接近 min_delay
        - 长文本 → 接近 max_delay
        """
        if text_len <= 0:
            return 0.0
        min_delay = self.cfg._min_delay
        max_delay = self.cfg._max_delay
        ratio = min(text_len / self._max_len_for_delay, 1.0)
        delay = min_delay + (max_delay - min_delay) * ratio
        return delay

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
        拆分核心逻辑
        """
        segments: list[Segment] = []
        current = Segment()

        # 用于存放“必须绑定到下一个 segment 的组件”
        # 例如：Reply / At
        pending_prefix: list[BaseMessageComponent] = []

        def push(seg: Segment):
            """将 segment 推入列表，并处理 max_count 限制"""
            if not seg.components:
                return

            if self.max_count > 0 and len(segments) >= self.max_count:
                # 超出限制则合并到最后一个 segment
                # 在合并处强制添加一个空格，确保文本不粘连
                if segments[-1].components and seg.components:
                    if isinstance(seg.components[0], Plain):
                        seg.components[0].text = " " + seg.components[0].text
                segments[-1].extend(seg.components)
            else:
                segments.append(seg)

        def flush():
            """提交当前 segment"""
            nonlocal current
            if current.components:
                push(current)
                current = Segment()

        for comp in chain:
            # Reply / At：必须与“后一个 segment”绑定
            if isinstance(comp, Reply | At):
                pending_prefix.append(comp)
                continue

            # Plain 组件
            if isinstance(comp, Plain):
                text = comp.text or ""
                if not text:
                    continue
                # 智能分段
                if self.cfg.smart:
                    stack: list[str] = []
                    pattern = re.compile(self.cfg._split_pattern)
                    i = 0
                    n = len(text)
                    buf = ""

                    while i < n:
                        ch = text[i]
                        is_opener = ch in self._pair_map

                        if ch in self._quote_chars:
                            if stack and stack[-1] == ch:
                                stack.pop()
                            else:
                                stack.append(ch)
                            buf += ch
                            i += 1
                            continue

                        if stack:
                            expected_closer = self._pair_map.get(stack[-1])
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
                            buf += delim
                            if buf:
                                if pending_prefix:
                                    current.extend(pending_prefix)
                                    pending_prefix.clear()
                                current.append(Plain(buf))
                                flush()
                                buf = ""
                            i += len(delim)
                        else:
                            buf += ch
                            i += 1

                    if buf:
                        if pending_prefix:
                            current.extend(pending_prefix)
                            pending_prefix.clear()
                        current.append(Plain(buf))
                # 普通分段
                else:
                    parts = re.split(f"({self.cfg._split_pattern})", text)
                    buf = ""

                    for part in parts:
                        if not part:
                            continue

                        if re.fullmatch(self.cfg._split_pattern, part):
                            buf += part
                            if buf:
                                if pending_prefix:
                                    current.extend(pending_prefix)
                                    pending_prefix.clear()

                                current.append(Plain(buf))
                                flush()
                                buf = ""
                        else:
                            if buf:
                                if pending_prefix:
                                    current.extend(pending_prefix)
                                    pending_prefix.clear()
                                current.append(Plain(buf))
                                buf = ""

                            if pending_prefix:
                                current.extend(pending_prefix)
                                pending_prefix.clear()

                            current.append(Plain(part))

                    if buf:
                        if pending_prefix:
                            current.extend(pending_prefix)
                            pending_prefix.clear()
                        current.append(Plain(buf))

                continue

            # Image / Face：跟随上一个 segment
            if isinstance(comp, Image | Face):
                if current.components:
                    current.append(comp)
                elif segments:
                    segments[-1].append(comp)
                else:
                    push(Segment([comp]))
                continue

            # 其他组件：必须独立成段
            flush()
            if pending_prefix:
                push(Segment(pending_prefix[:]))
                pending_prefix.clear()
            push(Segment([comp]))

        if current.components:
            push(current)

        # 后处理：去除所有 Plain 组件的末尾空白字符， 去除最后一个 Plain 组件的末尾符号
        for seg in segments:
            for comp in seg.components:
                if isinstance(comp, Plain):
                    comp.text = comp.text.rstrip()
            for comp in reversed(seg.components):
                if isinstance(comp, Plain) and comp.text.strip():
                    comp.text = self.tail_punc_re.sub("", comp.text)
                    break
        return segments



