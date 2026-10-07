from __future__ import annotations

import re
from astrbot.core.message.components import Face, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ..config import PluginConfig
from ..model import OutContext, StepName, StepResult
from .base import BaseStep


class FaceStep(BaseStep):
    name = StepName.FACE

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.face

    async def handle(self, ctx: OutContext) -> StepResult:
        # 非支持 QQ 平台（如非 aiocqhttp 协议）直接跳过
        if not isinstance(ctx.event, AiocqhttpMessageEvent):
            return StepResult()

        if not self.cfg.custom_mapping:
            return StepResult()

        mapping: dict[str, int] = {}
        for item in self.cfg.custom_mapping:
            item = item.strip()
            if not item:
                continue
            parts = item.split()
            if len(parts) >= 2 and parts[1].isdigit():
                mapping[parts[0]] = int(parts[1])

        if not mapping:
            return StepResult()

        # 按长度降序排 key，优先匹配长组合 emoji
        sorted_emojis = sorted(mapping.keys(), key=lambda s: len(s), reverse=True)
        pattern = re.compile("|".join(re.escape(k) for k in sorted_emojis))

        converted_count = 0
        new_chain = []

        for seg in ctx.chain:
            if not isinstance(seg, Plain):
                new_chain.append(seg)
                continue

            text = seg.text
            matches = list(pattern.finditer(text))
            if not matches:
                new_chain.append(seg)
                continue

            last_idx = 0
            for m in matches:
                start, end = m.span()
                matched_emoji = m.group(0)
                face_id = mapping[matched_emoji]

                if start > last_idx:
                    plain_text = text[last_idx:start]
                    if plain_text:
                        new_chain.append(Plain(text=plain_text))

                new_chain.append(Face(id=face_id))
                converted_count += 1
                last_idx = end

            if last_idx < len(text):
                rem_text = text[last_idx:]
                if rem_text:
                    new_chain.append(Plain(text=rem_text))

        if converted_count > 0:
            ctx.chain.clear()
            ctx.chain.extend(new_chain)
            return StepResult(msg=f"已将 {converted_count} 个 Emoji 转换为 QQ 表情")

        return StepResult()
