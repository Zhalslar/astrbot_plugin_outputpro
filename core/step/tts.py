import random

from astrbot.core.message.components import Plain, Record
from astrbot.core.provider.provider import TTSProvider
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

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
        except Exception as e:
            return StepResult(ok=False, msg=str(e))

        return StepResult()
