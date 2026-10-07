from __future__ import annotations

import re
from astrbot.core.message.components import Face, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ..config import PluginConfig
from ..model import OutContext, StepName, StepResult
from .base import BaseStep

# 常用 emoji 到 QQ 表情 face id 的精细映射表
DEFAULT_EMOJI_TO_FACE: dict[str, int] = {
    # 常用笑脸/情绪
    "😀": 14,
    "😃": 14,
    "😄": 13,
    "😁": 13,
    "😆": 11,
    "😂": 178,
    "🤣": 178,
    "😊": 21,
    "😇": 202,
    "🙂": 14,
    "🙃": 179,
    "😉": 12,
    "😌": 21,
    "😍": 2,
    "🥰": 2,
    "😘": 85,
    "😗": 85,
    "😙": 85,
    "😚": 85,
    "😋": 12,
    "😛": 12,
    "😝": 12,
    "😜": 12,
    "🤪": 179,
    "🤨": 272,
    "🧐": 273,
    "🤓": 277,
    "😎": 16,
    "🤩": 182,
    "🥳": 182,
    "😏": 32,
    "😒": 1,
    "😞": 1,
    "😔": 3,
    "😟": 3,
    "😕": 3,
    "🙁": 3,
    "😣": 10,
    "😖": 10,
    "😫": 10,
    "🥺": 277,
    "😢": 5,
    "😭": 9,
    "😤": 33,
    "😠": 11,
    "😡": 11,
    "🤬": 264,
    "🤯": 263,
    "😳": 6,
    "🥵": 267,
    "🥶": 268,
    "😱": 26,
    "😨": 26,
    "😰": 10,
    "😥": 10,
    "😓": 10,
    "🤗": 181,
    "🤔": 32,
    "🤭": 20,
    "🤫": 7,
    "🤥": 262,
    "😶": 7,
    "😐": 3,
    "😑": 3,
    "😬": 13,
    "🙄": 274,
    "😯": 14,
    "😦": 14,
    "😧": 14,
    "😮": 14,
    "😲": 14,
    "🥱": 278,
    "😴": 8,
    "🤤": 261,
    "😪": 8,
    "😵": 34,
    "🤐": 7,
    "🥴": 275,
    "🤢": 38,
    "🤮": 38,
    "🤧": 38,
    "😷": 38,
    "🤒": 38,
    "🤕": 38,
    "🤑": 266,
    "🤠": 182,
    "😈": 11,
    "👿": 11,
    "🤡": 179,
    "💩": 59,
    "👻": 60,
    "💀": 37,
    "☠️": 37,
    "👽": 60,
    "👾": 60,
    "🤖": 60,
    "🎃": 60,
    # 猫咪
    "😺": 13,
    "😸": 13,
    "😹": 178,
    "😻": 2,
    "😼": 32,
    "😽": 85,
    "🙀": 26,
    "😿": 5,
    "😾": 11,
    # 手势与身体
    "👍": 76,
    "👎": 77,
    "👏": 99,
    "🙌": 99,
    "👐": 99,
    "🤲": 99,
    "🤝": 78,
    "🙏": 80,
    "💪": 79,
    "👀": 3,
    "👅": 12,
    "👄": 85,
    "💋": 85,
    # 常用符号与爱心
    "❤️": 66,
    "💔": 67,
    "💕": 66,
    "💖": 66,
    "💗": 66,
    "💘": 66,
    "🎉": 182,
    "✨": 54,
    "🔥": 177,
    "💯": 76,
    "💢": 11,
    "💤": 8,
    "💨": 11,
}


class FaceStep(BaseStep):
    name = StepName.FACE

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.face

    async def handle(self, ctx: OutContext) -> StepResult:
        # 非支持 QQ 平台（如非 aiocqhttp 协议）直接跳过
        if not isinstance(ctx.event, AiocqhttpMessageEvent):
            return StepResult()

        mapping = {}
        if self.cfg.custom_mapping:
            for item in self.cfg.custom_mapping:
                item = item.strip()
                if not item:
                    continue
                parts = item.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    mapping[parts[0]] = int(parts[1])
        else:
            mapping = dict(DEFAULT_EMOJI_TO_FACE)

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
