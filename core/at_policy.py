import random
import re

from astrbot.core.message.components import (
    At,
    BaseMessageComponent,
    Face,
    Image,
    Plain,
    Reply,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .state import GroupState


class AtPolicy:
    def __init__(self, config: dict):
        self.conf = config
        # 假艾特正则
        self.at_head_regex = re.compile(
            r"^\s*(?:"
            r"\[at[:：]\s*(\d+)\]"  # [at:123]
            r"|\[at[:：]\s*([^\]]+)\]"  # [at:nick]
            r"|@(\d{5,12})"  # @123456
            r"|@([\u4e00-\u9fa5\w-]{2,20})"  # @昵称
            r")\s*",
            re.IGNORECASE,
        )

    def parse_fake_at(
        self,
        chain: list[BaseMessageComponent],
        gstate: GroupState,
    ) -> tuple[int | None, str | None, str | None]:
        """
        解析假 At（纯函数）
        返回:
            index, qq, nickname
        """
        for i, seg in enumerate(chain):
            if not isinstance(seg, Plain) or not seg.text:
                continue

            m = self.at_head_regex.match(seg.text)
            if not m:
                return None, None, None

            qq = m.group(1) or m.group(3)
            nickname = m.group(2) or m.group(4)

            if not qq and nickname:
                qq = gstate.name_to_qq.get(nickname)

            return i, qq, nickname

        return None, None, None

    def apply_at(
        self,
        chain: list[BaseMessageComponent],
        idx: int | None,
        qq: str | None,
        nickname: str | None,
    ):
        """应用 At（唯一修改点）"""
        if idx is None:
            return

        seg = chain[idx]
        if not isinstance(seg, Plain):
            return

        # 移除假 at 前缀
        seg.text = self.at_head_regex.sub("", seg.text, count=1)

        if not seg.text:
            chain.pop(idx)

        if not self.conf["parse_at"]["enable"] or not qq:
            return

        if self.conf["parse_at"]["at_str"]:
            display = nickname or qq
            seg.text = f"@{display} " + seg.text
        else:
            chain.insert(idx, At(qq=qq))
            chain.insert(idx + 1, Plain("\u200b"))

    # -------------------------
    # 统一入口
    # -------------------------
    def handle(
        self,
        event: AstrMessageEvent,
        chain: list[BaseMessageComponent],
        gstate: GroupState,
    ):
        """统一入口"""
        # 解析假艾特
        idx, qq, nickname = self.parse_fake_at(chain, gstate)

        # 应用 At
        self.apply_at(chain, idx, qq, nickname)

        # 概率艾特
        if not (
            all(isinstance(c, Plain | Image | Face | At | Reply) for c in chain)
            and self.conf["at_prob"] > 0
        ):
            return

        has_at = any(
            isinstance(c, At)
            or (isinstance(c, Plain) and c.text.lstrip().startswith("@"))
            for c in chain
        )

        hit = random.random() < self.conf["at_prob"]

        # 命中 → 必须有 @
        if hit and not has_at and chain and isinstance(chain[0], Plain):
            chain.insert(0, At(qq=event.get_sender_id()))

        # 未命中 → 清空所有 @
        elif not hit and has_at:
            new_chain = []
            for c in chain:
                if isinstance(c, At):
                    continue
                if isinstance(c, Plain):
                    c.text = re.sub(r"^\s*@[\u4e00-\u9fa5\w-]+\s*", "", c.text)
                    if not c.text:
                        continue
                new_chain.append(c)
            chain[:] = new_chain
