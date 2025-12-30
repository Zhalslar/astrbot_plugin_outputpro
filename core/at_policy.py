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

from .model import OutContext


class AtPolicy:
    def __init__(self, config: dict):
        self.conf = config

        self.at_head_regex = re.compile(
            r"^\s*(?:"
            r"\[at[:：]\s*(\d+)\]"
            r"|\[at[:：]\s*([^\]]+)\]"
            r"|@(\d{5,12})"
            r"|@([\u4e00-\u9fa5\w-]{2,20})"
            r")\s*",
            re.IGNORECASE,
        )

    # -------------------------
    # 基础判断
    # -------------------------
    def _has_at(self, chain: list[BaseMessageComponent]) -> bool:
        for seg in chain:
            if isinstance(seg, At):
                return True
            if isinstance(seg, Plain) and self.at_head_regex.match(seg.text):
                return True
        return False

    def _insert_at(self, chain, qq, nickname=None):
        if not self.conf["parse_at"]["enable"]:
            return

        display = nickname or qq

        # 找第一个 Plain
        for i, seg in enumerate(chain):
            if not isinstance(seg, Plain):
                continue

            if self.conf["parse_at"]["at_str"]:
                # 原地修改
                seg.text = f"@{display} " + seg.text
            else:
                # 真 At：插在 Plain 前
                chain.insert(i, At(qq=qq))
                chain.insert(i + 1, Plain("\u200b"))
            return

    # -------------------------
    # 假 at 解析（只读）
    # -------------------------
    def _parse_fake_at(self, ctx: OutContext):
        """
        只识别，不修改
        """
        for idx, seg in enumerate(ctx.chain):
            if not isinstance(seg, Plain) or not seg.text:
                continue

            m = self.at_head_regex.match(seg.text)
            if not m:
                return None, None, None

            qq = m.group(1) or m.group(3)
            nickname = m.group(2) or m.group(4)

            if not qq and nickname and len(ctx.group.name_to_qq) > 0:
                qq = ctx.group.name_to_qq.get(nickname)

            return idx, qq, nickname

        return None, None, None

    # -------------------------
    # 应用假 at（真正修改）
    # -------------------------
    def _apply_fake_at(self, chain, idx, qq, nickname):
        if idx is None:
            return

        seg = chain[idx]
        if not isinstance(seg, Plain):
            return

        # 删除假 at 前缀
        seg.text = self.at_head_regex.sub("", seg.text, count=1)

        if not seg.text:
            chain.pop(idx)

        self._insert_at(
            chain,
            qq=qq,
            nickname=nickname,
        )

    # -------------------------
    # 主入口
    # -------------------------
    def handle(self, ctx: OutContext):
        # ===== 1. 假艾特解析 =====
        idx, qq, nickname = self._parse_fake_at(ctx)
        self._apply_fake_at(ctx.chain, idx, qq, nickname)

        # ===== 2. 智能艾特 =====
        at_prob = self.conf["parse_at"]["at_prob"]
        if not (
            at_prob > 0
            and all(isinstance(c, Plain | Image | Face | At | Reply) for c in ctx.chain)
        ):
            return

        has_at = self._has_at(ctx.chain)
        hit = random.random() < at_prob

        # 命中 → 必须有 at
        if hit and not has_at and ctx.chain and isinstance(ctx.chain[0], Plain):
            self._insert_at(
                ctx.chain,
                qq=ctx.uid,
                nickname=ctx.event.get_sender_name(),
            )

        # 未命中 → 清除所有 at
        elif not hit and has_at:
            new_chain = []
            for c in ctx.chain:
                if isinstance(c, At):
                    continue
                if isinstance(c, Plain):
                    c.text = self.at_head_regex.sub("", c.text, count=1).strip()
                    if not c.text:
                        continue
                new_chain.append(c)
            ctx.chain[:] = new_chain
