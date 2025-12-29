import asyncio
import re
from dataclasses import dataclass, field

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import (
    At,
    BaseMessageComponent,
    Face,
    Image,
    Plain,
    Reply,
)
from astrbot.api.star import Context

# ============================================================
# Segment：逻辑分段单元
# ============================================================
# 一个 Segment 表示“一段可以被单独发送的消息”
# 它不关心如何发送，只负责：
#   - 保存组件
#   - 判断是否为空
#   - 提供文本 / 媒体判断能力
# ============================================================


@dataclass
class Segment:
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

    def strip_tail_punctuation(self):
        """
        移除末尾 Plain 文本中的句尾符号
        只处理最后一个 Plain，不影响中间结构
        """
        for comp in reversed(self.components):
            if isinstance(comp, Plain):
                # 去掉末尾标点（中英文）
                comp.text = re.sub(r"[,，。.、；;：:]+$", "", comp.text)
                break

# ============================================================
# MessageSplitter
# ============================================================
# 职责：
#   1. 根据规则拆分消息
#   2. 控制分段发送
#   3. 不参与任何业务逻辑判断
# ============================================================


class MessageSplitter:
    def __init__(self, context: Context, config: AstrBotConfig):
        self.context = context
        sconf = config["split"]

        # 用于 Plain 文本分割
        self.split_pattern = f"[{re.escape(sconf['chars'])}]+"

        # 最大分段数（<=0 表示不限制）
        self.max_count = sconf["max_count"]

        # 解析字符串配置 "min,max" → [min, max]
        try:
            self.min_delay, self.max_delay = map(
                float, sconf.get("typing_delay", "1.5,3.5").split(",")
            )
        except Exception as e:
            logger.warning(f"解析 typing_delay 失败，使用默认值 1.5,3.5: {e}")
            self.min_delay, self.max_delay = 1.5, 3.5

        # 最大文本长度归一化，用于映射到 min/max
        self._max_len_for_delay = 150

    def calc_delay(self, text_len: int) -> float:
        """
        根据文本长度计算延迟（线性映射到 min_delay ~ max_delay）：
        - 短文本 → 接近 min_delay
        - 长文本 → 接近 max_delay
        """
        if text_len <= 0:
            return 0.0

        ratio = min(text_len / self._max_len_for_delay, 1.0)
        delay = self.min_delay + (self.max_delay - self.min_delay) * ratio
        return delay


    async def split(self, umo: str, chain: list[BaseMessageComponent]):
        """
        对消息进行拆分并发送。
        最后一段会回填到原 chain 中。
        """
        segments = self.split_chain(chain)

        if len(segments) <= 1:
            return

        logger.debug(f"[Splitter] 消息被分为 {len(segments)} 段")

        # 逐段发送（最后一段不立即发）
        for i in range(len(segments) - 1):
            seg = segments[i]

            if seg.is_empty:
                continue

            try:
                await self.context.send_message(
                    umo,
                    MessageChain(seg.components),
                )
                delay = self.calc_delay(len(seg.text))
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"[Splitter] 发送分段 {i + 1} 失败: {e}")

        # 最后一段回填给主流程继续处理
        chain.clear()
        if not segments[-1].is_empty:
            chain.extend(segments[-1].components)

    # ========================================================
    # 拆分核心逻辑
    # ========================================================

    def split_chain(self, chain: list[BaseMessageComponent]) -> list[Segment]:
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
            # ====================================================
            # Reply / At：
            #   - 必须与“后一个 segment”绑定
            # ====================================================
            if isinstance(comp, Reply | At):
                pending_prefix.append(comp)
                continue

            # ====================================================
            # Plain：唯一允许触发分段的组件
            # ====================================================
            if isinstance(comp, Plain):
                text = comp.text or ""
                if not text:
                    continue

                # 按分隔符拆分
                parts = re.split(f"({self.split_pattern})", text)
                buf = ""

                for part in parts:
                    if not part:
                        continue

                    # 命中分隔符：形成一个完整 segment
                    if re.fullmatch(self.split_pattern, part):
                        buf += part
                        if buf:
                            if pending_prefix:
                                current.extend(pending_prefix)
                                pending_prefix.clear()

                            current.append(Plain(buf))
                            flush()
                            buf = ""
                    else:
                        # 普通文本
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

                # 剩余文本
                if buf:
                    if pending_prefix:
                        current.extend(pending_prefix)
                        pending_prefix.clear()
                    current.append(Plain(buf))

                continue

            # ====================================================
            # Image / Face：跟随上一个 segment
            # ====================================================
            if isinstance(comp, Image | Face):
                if current.components:
                    current.append(comp)
                elif segments:
                    segments[-1].append(comp)
                else:
                    push(Segment([comp]))
                continue

            # ====================================================
            # 其他组件：必须独立成段
            # ====================================================
            flush()
            if pending_prefix:
                push(Segment(pending_prefix[:]))
                pending_prefix.clear()
            push(Segment([comp]))

        if current.components:
            push(current)

        for seg in segments:
            seg.strip_tail_punctuation()

        return segments
