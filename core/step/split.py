import asyncio
import re
from collections.abc import Iterator
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
    """段数据结构"""

    components: list[BaseMessageComponent] = field(default_factory=list)

    def append(self, comp: BaseMessageComponent):
        self.components.append(comp)

    def extend(self, comps: list[BaseMessageComponent]):
        self.components.extend(comps)

    @property
    def text(self) -> str:
        return "".join(c.text for c in self.components if isinstance(c, Plain))

    @property
    def has_media(self) -> bool:
        return any(not isinstance(c, Plain) for c in self.components)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip() and not self.has_media

    def rstrip_plain(self):
        """去除 Plain 末尾空白"""
        for c in self.components:
            if isinstance(c, Plain):
                c.text = c.text.rstrip()

    def strip_tail_punc(self, pattern):
        """去掉尾部标点（仅最后一个 Plain 生效）"""
        for c in reversed(self.components):
            if isinstance(c, Plain) and c.text.strip():
                c.text = pattern.sub("", c.text)
                break


@dataclass
class Token:
    text: str
    is_split: bool  # 是否触发 flush


class TextTokenizer:
    """
    Tokenizer：负责“语法安全分段”:
    在“括号 / 引号安全”的前提下，识别分段点
    + 内部集成 Kaomoji 保护（不可拆语义块）
    """

    # Kaomoji 匹配
    KAOMOJI_PATTERN = re.compile(
        r"("
        r"[(\[\uFF08\u3010<]"
        r"[^()\[\]\uFF08\uFF09\u3010\u3011<>]*?"
        r"[^\u4e00-\u9fffA-Za-z0-9\s]"
        r"[^()\[\]\uFF08\uFF09\u3010\u3011<>]*?"
        r"[)\]\uFF09\u3011>]"
        r")"
        r"|"
        r"([\u25b3\u25a6\u30fb\u30ef\^><\u2267\u2665\uff5e\uff40\u7c32\u2764]{2,15})"
    )

    def __init__(self, pattern: re.Pattern[str]):
        self.pattern = pattern
        self.quote_chars = {'"', "'", "`"}
        self.pair_map = {
            "“": "”",
            "《": "》",
            "（": "）",
            "(": ")",
            "[": "]",
            "{": "}",
            "‘": "’",
            "【": "】",
            "<": ">",
            "「": "」",
        }

    # Kaomoji 内部处理
    def _protect_kaomoji(self, text: str):
        protected = text
        mapping: dict[str, str] = {}

        for idx, match in enumerate(self.KAOMOJI_PATTERN.findall(text)):
            kaomoji = match[0] or match[1]
            if not kaomoji:
                continue

            # 使用零宽字符保护
            placeholder = f"\u200bKAOMOJI_{idx}\u200b"
            protected = protected.replace(kaomoji, placeholder, 1)
            mapping[placeholder] = kaomoji

        return protected, mapping

    def _restore_kaomoji(self, text: str, mapping: dict[str, str]):
        for placeholder, kaomoji in mapping.items():
            text = text.replace(placeholder, kaomoji)
        return text

    # tokenize（核心）
    def tokenize(self, text: str) -> Iterator[Token]:
        # 先保护 Kaomoji
        text, mapping = self._protect_kaomoji(text)

        stack: list[str] = []
        buf = ""
        i = 0

        while i < len(text):
            ch = text[i]
            is_opener = ch in self.pair_map

            # 英文单词空格保护
            if ch == " ":
                prev = text[i - 1] if i > 0 else ""
                next_ = text[i + 1] if i + 1 < len(text) else ""
                if prev.isalnum() and next_.isalnum():
                    buf += ch
                    i += 1
                    continue

            # 引号处理
            if ch in self.quote_chars:
                if stack and stack[-1] == ch:
                    stack.pop()
                else:
                    stack.append(ch)
                buf += ch
                i += 1
                continue

            # 括号内部
            if stack:
                expected = self.pair_map.get(stack[-1])
                if ch == expected:
                    stack.pop()
                elif is_opener:
                    stack.append(ch)
                buf += ch
                i += 1
                continue

            # 新括号
            if is_opener:
                stack.append(ch)
                buf += ch
                i += 1
                continue

            # 分隔符命中
            m = self.pattern.match(text, i)
            if m:
                seg = m.group()
                if seg.strip() == "":
                    buf += seg
                    i += len(seg)
                    continue
                buf += seg
                yield Token(self._restore_kaomoji(buf, mapping), True)
                buf = ""
                i += len(seg)
                continue

            buf += ch
            i += 1

        if buf:
            yield Token(self._restore_kaomoji(buf, mapping), False)


class SegmentBuilder:
    """Builder：负责段拼接 + 状态"""

    def __init__(self, max_count: int):
        self.max_count = max_count
        self.segments: list[Segment] = []
        self.current = Segment()
        self.pending_prefix: list[BaseMessageComponent] = []
        self.exhausted = False

    def add_prefix(self, comp: BaseMessageComponent):
        """缓存 Reply / At"""
        self.pending_prefix.append(comp)

    def attach_pending(self, target: Segment):
        """挂载 prefix"""
        if self.pending_prefix:
            target.extend(self.pending_prefix)
            self.pending_prefix.clear()

    def append(self, comps: list[BaseMessageComponent]):
        """追加到当前段"""
        if self.exhausted:
            self.append_tail(comps)
            return

        self.attach_pending(self.current)
        self.current.extend(comps)

    def append_tail(self, comps: list[BaseMessageComponent]):
        """拼到最后一段（超限时使用）"""
        if self.segments:
            self._merge(self.segments[-1], comps)
            self.segments[-1].extend(comps)
        else:
            self.current.extend(comps)

    def flush(self):
        """提交当前段"""
        if not self.current.components:
            return

        if self.max_count > 0 and len(self.segments) >= self.max_count:
            self.exhausted = True
            self.append_tail(self.current.components)
        else:
            self.segments.append(self.current)

            if self.max_count > 0 and len(self.segments) >= self.max_count:
                self.exhausted = True

        self.current = Segment()

    def _merge(self, target: Segment, comps):
        """必要时补空格"""
        if target.components and comps and isinstance(comps[0], Plain):
            comps[0].text = " " + comps[0].text

    def finalize(self):
        if self.current.components:
            self.flush()
        return self.segments


class TypingController:
    """统一控制输入状态显示"""

    supported_platforms = {"telegram", "weixin_oc", "aiocqhttp"}

    async def _show_once(self, ctx: OutContext):
        platform = ctx.event.get_platform_name()

        try:
            if platform in {"telegram", "weixin_oc"}:
                await ctx.event.send_typing()
                return

            if platform == "aiocqhttp" and not ctx.gid:
                bot = getattr(ctx.event, "bot", None)
                api = getattr(bot, "api", None)
                if api:
                    await api.call_action(
                        "set_input_status", user_id=ctx.uid, event_type=1
                    )
        except Exception:
            logger.debug("[Typing] 发送 typing 失败", exc_info=True)

    async def sleep(self, ctx: OutContext, delay: float):
        """带 typing 的 sleep"""
        if delay <= 0:
            return
        platform_name = ctx.event.get_platform_name()
        if platform_name not in self.supported_platforms:
            await asyncio.sleep(delay)
            return

        # 小延迟：只触发一次
        if delay <= 1.0:
            await self._show_once(ctx)
            await asyncio.sleep(delay)
            return

        # 大延迟：循环触发
        interval = min(2.5, max(1.0, delay / 3))
        remaining = delay

        while remaining > 0:
            await self._show_once(ctx)
            sleep_time = min(interval, remaining)
            await asyncio.sleep(sleep_time)
            remaining -= sleep_time


# =========================
# SplitStep
# =========================
class SplitStep(BaseStep):
    name = StepName.SPLIT
    support_platforms = {"aiocqhttp", "telegram", "lark"}

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.split
        self.context = config.context
        self.tokenizer = TextTokenizer(self.cfg.split_re)
        self.typing = TypingController()

    async def handle(self, ctx: OutContext) -> StepResult:
        # 限制平台
        platform = ctx.event.get_platform_name()
        if platform not in self.support_platforms:
            return StepResult()

        # 限制字数
        if self.cfg.max_length > 0:
            total_len = sum(len(c.text) for c in ctx.chain if isinstance(c, Plain))
            if total_len > self.cfg.max_length:
                logger.debug(
                    f"[Splitter] 超过分段字数上限，跳过分段：len={total_len}, limit={self.cfg.max_length}"
                )
                return StepResult()

        # 分段
        segments = self._split_chain(ctx.chain)

        # 后处理
        for seg in segments:
            seg.rstrip_plain()
            if self.cfg.tail_punc_re:
                seg.strip_tail_punc(self.cfg.tail_punc_re)

        if len(segments) <= 1:
            return StepResult()

        logger.debug(f"[Splitter] 消息被分为 {len(segments)} 段")

        # 逐段发送
        for i, seg in enumerate(segments[:-1]):
            if seg.is_empty:
                continue
            try:
                await self.plugin_config.context.send_message(
                    ctx.event.unified_msg_origin,
                    MessageChain(seg.components),
                )
                delay = self._calc_delay(seg.text)
                if self.cfg.show_typing:
                    await self.typing.sleep(ctx, delay)
                else:
                    await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"[Splitter] 第{i + 1}段发送失败: {e}")

        # 最后一段回填
        ctx.chain.clear()
        if not segments[-1].is_empty:
            ctx.chain.extend(segments[-1].components)

        return StepResult(msg="分段完成")

    # =========================
    # 工具方法
    # =========================

    def _calc_delay(self, text: str) -> float:
        if not text:
            return 0.0
        cn = self.cfg.per_char_delay
        en = cn / 2
        delay = sum(cn if "\u4e00" <= c <= "\u9fff" else en for c in text)
        return max(self.cfg.delay_scope_min, min(self.cfg.delay_scope_max, delay))

    # =========================
    # 核心 split
    # =========================
    def _select_split_points(self, tokens: list[Token], min_len: int = 2) -> set[int]:
        """
        选切点（最多 max_count 段 → k = max_count-1 个切点）

        规则：
        1. 优先使用语义切点（is_split）
        2. 按累计长度均分选择切点
        3. 过滤：避免产生过短段
        """

        split_idx = [i for i, t in enumerate(tokens) if t.is_split and t.text.strip()]
        if not split_idx:
            return set()

        max_count = self.cfg.max_count
        if max_count <= 0 or len(split_idx) <= max_count - 1:
            return set(split_idx)

        k = max_count - 1
        if k <= 0:
            return set()

        # 长度均分选点
        lengths = [len(t.text) for t in tokens]
        total = sum(lengths)
        targets = [total * i / max_count for i in range(1, max_count)]

        raw = set()
        acc, ti = 0, 0

        for i, le in enumerate(lengths):
            acc += le
            if tokens[i].is_split:
                while ti < k and acc >= targets[ti]:
                    raw.add(i)
                    ti += 1

        if not raw:
            return set()

        # 最小段长度过滤
        final = set()
        last = -1

        def seg_len(le: int, r: int) -> int:
            return sum(len(t.text) for t in tokens[le:r])

        for idx in sorted(raw):
            if seg_len(last + 1, idx + 1) < min_len:
                continue
            final.add(idx)
            last = idx

        # 修正尾段 ----------
        if final:
            tail_len = seg_len(last + 1, len(tokens))
            if tail_len < min_len:
                final.remove(max(final))

        return final

    def _split_chain(self, chain: list[BaseMessageComponent]) -> list[Segment]:
        """
        流程：
        1. 遍历 chain
        2. Plain → tokenize → 收集切点 → 策略筛选
        3. builder 负责拼段
        """
        builder = SegmentBuilder(self.cfg.max_count)

        for comp in chain:
            # 引用、艾特前置
            if isinstance(comp, (Reply, At)):
                builder.add_prefix(comp)
                continue

            # 文本处理
            if isinstance(comp, Plain):
                text = comp.text or ""
                if not text:
                    continue
                tokens = list(self.tokenizer.tokenize(text))
                selected = self._select_split_points(tokens)
                for i, token in enumerate(tokens):
                    builder.append([Plain(token.text)])
                    if i in selected:
                        builder.flush()

                continue

            # 图片 / 表情：跟随当前段
            if isinstance(comp, (Image, Face)):
                builder.append([comp])
                continue

            # 其他组件：独立成段
            builder.flush()
            seg = Segment()
            builder.attach_pending(seg)
            seg.append(comp)
            builder.current = seg
            builder.flush()

        return builder.finalize()
