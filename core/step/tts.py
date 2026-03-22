import os
import random
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from astrbot.core.message.components import Plain, Record
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.provider.provider import TTSProvider
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.io import download_file
from astrbot.core.utils.media_utils import convert_audio_format, convert_audio_to_wav
from astrbot.core.utils.tencent_record_helper import tencent_silk_to_wav

from ..config import PluginConfig
from ..model import OutContext, StepName, StepResult
from .base import BaseStep


class TTSStep(BaseStep):
    name = StepName.TTS

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.tts
        self.style = None

    def _build_record_from_audio(self, audio: str, text: str) -> Record:
        audio = (audio or "").strip()
        if audio.startswith(("http://", "https://")):
            return Record.fromURL(audio, text=text)
        if audio.startswith("file:///"):
            return Record(file=audio, url=audio, text=text)
        return Record.fromFileSystem(audio, text=text, url=audio)

    def _get_selected_tts_provider(self, ctx: OutContext) -> TTSProvider | None:
        provider_id = (self.cfg.tts_provider_id or "").strip()
        if not provider_id:
            return None
        provider = self.plugin_config.context.get_provider_by_id(provider_id)
        if not provider:
            raise ValueError(f"未找到 TTS 提供商: {provider_id}")
        if not isinstance(provider, TTSProvider):
            raise ValueError(
                f"提供商 {provider_id} 不是 TTS 类型，实际类型: {type(provider)}"
            )
        return provider

    def _get_qq_relay_bot(self) -> Any | None:
        """获取可用的 aiocqhttp bot（用于跨平台中转生成语音）。"""
        for platform in self.plugin_config.context.platform_manager.platform_insts:
            try:
                meta = platform.meta()
            except Exception:
                continue
            if getattr(meta, "name", "") != "aiocqhttp":
                continue
            bot = getattr(platform, "bot", None)
            if bot:
                return bot
        return None

    async def _build_relay_record_for_platform(
        self,
        audio_url: str,
        ctx: OutContext,
        text: str,
    ) -> Record:
        """按目标平台构造中转语音记录。

        - aiocqhttp: 保持原始 URL（QQ 原生最兼容）
        - telegram: 优先转码为 ogg/opus
        - 其他平台: 优先转码为 wav（更通用）
        """
        platform_name = str(ctx.event.get_platform_name() or "")
        if platform_name == "aiocqhttp":
            return Record.fromURL(audio_url)

        try:
            temp_dir = get_astrbot_temp_path()
            os.makedirs(temp_dir, exist_ok=True)
            parsed = urlparse(audio_url)
            suffix = os.path.splitext(parsed.path)[1] or ".audio"
            raw_path = os.path.join(temp_dir, f"outputpro_relay_{uuid.uuid4().hex}{suffix}")
            await download_file(audio_url, raw_path)

            with open(raw_path, "rb") as f:
                probe_head = f.read(16)
            if probe_head.startswith(b"INVALID"):
                raise ValueError("relay audio payload is INVALID")

            # 优先识别 QQ 常见语音封装（silk/amr）并转为 wav
            normalized_input = raw_path
            with open(raw_path, "rb") as f:
                head = f.read(64)
            upper_head = head.upper()

            is_silk = b"SILK" in upper_head
            is_amr = b"#!AMR" in upper_head
            if is_silk:
                with open(raw_path, "rb") as f:
                    raw_bytes = f.read()
                if raw_bytes and raw_bytes[0] in (0x02, 0x03) and b"#!SILK" in raw_bytes[:16]:
                    normalized_silk = bytes([0x02]) + raw_bytes[1:]
                    silk_path = str(Path(temp_dir) / f"outputpro_relay_{uuid.uuid4().hex}.silk")
                    with open(silk_path, "wb") as f:
                        f.write(normalized_silk)
                    normalized_input = silk_path

                wav_path = os.path.join(
                    temp_dir, f"outputpro_relay_{uuid.uuid4().hex}.wav"
                )
                await tencent_silk_to_wav(normalized_input, wav_path)
                normalized_input = wav_path
            elif is_amr:
                # amr 转 wav，便于后续统一处理
                normalized_input = await convert_audio_to_wav(raw_path)

            if platform_name == "telegram":
                converted_path = await convert_audio_format(
                    normalized_input, output_format="ogg"
                )
            else:
                converted_path = await convert_audio_to_wav(normalized_input)

            return Record.fromFileSystem(converted_path, text=text, url=converted_path)
        except Exception:
            return Record.fromURL(audio_url, text=text)

    async def handle(self, ctx: OutContext) -> StepResult:
        if not (
            len(ctx.chain) == 1
            and isinstance(ctx.chain[0], Plain)
            and len(ctx.chain[0].text) < self.cfg.threshold
            and random.random() < self.cfg.prob
        ):
            return StepResult()

        text = ctx.chain[0].text
        try:
            provider = self._get_selected_tts_provider(ctx)
            if provider:
                audio = await provider.get_audio(text)
                ctx.chain[:] = [self._build_record_from_audio(audio, text)]
                return StepResult(
                    msg=f"已使用配置的 TTS 模型将文本消息{text[:10]}转为语音"
                )

            if isinstance(ctx.event, AiocqhttpMessageEvent):
                audio = await ctx.event.bot.get_ai_record(
                    character=self.cfg.character_id,
                    group_id=int(self.cfg.group_id),
                    text=text,
                )
                ctx.chain[:] = [Record.fromURL(audio)]
                return StepResult(msg=f"已将文本消息{text[:10]}转化为语音消息")

            relay_bot = self._get_qq_relay_bot()
            if relay_bot and self.cfg.group_id:
                audio = await relay_bot.get_ai_record(
                    character=self.cfg.character_id,
                    group_id=int(self.cfg.group_id),
                    text=text,
                )
                ctx.chain[:] = [
                    await self._build_relay_record_for_platform(
                        audio_url=audio,
                        ctx=ctx,
                        text=text,
                    )
                ]
                return StepResult(
                    msg=f"已通过QQ中转将文本消息{text[:10]}转化为语音消息"
                )
        except Exception as e:
            return StepResult(ok=False, msg=str(e))

        return StepResult()
