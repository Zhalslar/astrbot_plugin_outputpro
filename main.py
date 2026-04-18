import importlib
import re

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .core.config import PluginConfig
from .core.model import OutContext, StateManager, StepName
from .core.pipeline import Pipeline
from .core.runtime_hook import RuntimeOutputHook
from .core.sanitize import (
    collect_visible_text,
    clean_text,
    replace_in_chain,
    replace_text,
    sanitize_chain,
    transform_text_in_chain,
)

TTS_EMOTION_ROUTER_NAME = "TTS 情绪路由"
TTS_EMOTION_ROUTER_ROOT = "astrbot_plugin_tts_emotion_router"
TTS_OUTPUT_MARKER_MODE_EXTRA = "_tts_emotion_router_output_marker_mode"
TTS_OUTPUT_MARKER_MODE_PRESERVE = "preserve_for_tts"
TTS_OUTPUT_MARKER_MODE_STRIP = "strip_visible"
TTS_PAUSE_TAG_RE = re.compile(r"<#\s*\d+(?:\.\d{1,2})?\s*#>")


class OutputPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)
        self.pipeline = Pipeline(self.cfg)
        self.runtime_hook = RuntimeOutputHook(self.cfg)

    async def initialize(self):
        await self.pipeline.initialize()
        self.runtime_hook.install()

    async def terminate(self):
        self.runtime_hook.uninstall()
        await self.pipeline.terminate()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        self.runtime_hook.install()

    @filter.on_platform_loaded()
    async def on_platform_loaded(self):
        self.runtime_hook.install()

    @filter.on_llm_response(priority=2)
    async def on_llm_response(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ):
        """在发送前同步净化 LLM 输出，按需为 TTS 保留情绪标签。"""
        tts_instance = self._get_tts_router_instance(event)
        preserve_emotion_tag, preserve_reason = self._resolve_emotion_tag_policy(
            event,
            tts_instance,
        )
        self._set_event_extra(
            event, "_outputpro_preserve_emotion_tag", preserve_emotion_tag
        )
        self._set_event_extra(event, "_outputpro_preserve_reason", preserve_reason)

        result_chain = getattr(response, "result_chain", None)
        chain = getattr(result_chain, "chain", None)

        if chain:
            before_count = len(chain)
            if preserve_emotion_tag:
                transform_text_in_chain(
                    chain,
                    lambda value: self._sanitize_for_tts_preserve_mode(
                        tts_instance,
                        value,
                    ),
                )
            else:
                if tts_instance is not None:
                    transform_text_in_chain(
                        chain,
                        lambda value: self._sanitize_with_tts_plugin(
                            tts_instance, value
                        ),
                    )
                sanitize_chain(
                    chain,
                    self.cfg.clean,
                    emotion_tag=True,
                )
            replace_in_chain(chain, self.cfg.replace)

            plain = collect_visible_text(chain)
            logger.debug(
                "[outputpro:on_llm_response] preserve_emotion_tag=%s reason=%s components=%d->%d plain=%r",
                preserve_emotion_tag,
                preserve_reason,
                before_count,
                len(chain),
                plain,
            )
            self._block_llm_response_if_needed(response, plain)
            return

        completion_text = getattr(response, "completion_text", None)
        updated_text = completion_text if isinstance(completion_text, str) else ""
        if updated_text and preserve_emotion_tag:
            updated_text = self._sanitize_for_tts_preserve_mode(
                tts_instance,
                updated_text,
            )
        elif updated_text and tts_instance is not None:
            updated_text = self._sanitize_with_tts_plugin(
                tts_instance,
                updated_text,
            )
        if preserve_emotion_tag:
            cleaned_text = updated_text
        else:
            cleaned_text, _ = clean_text(
                updated_text,
                self.cfg.clean,
                emotion_tag=True,
            )
        cleaned_text, _ = replace_text(cleaned_text, self.cfg.replace)
        if isinstance(completion_text, str) and cleaned_text != completion_text:
            response.completion_text = cleaned_text
            try:
                setattr(response, "_completion_text", cleaned_text)
            except Exception:
                pass

        logger.debug(
            "[outputpro:on_llm_response] preserve_emotion_tag=%s reason=%s plain_only=%r",
            preserve_emotion_tag,
            preserve_reason,
            cleaned_text,
        )
        self._block_llm_response_if_needed(response, cleaned_text)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_message(self, event: AstrMessageEvent):
        """收到群消息时"""
        gid = event.get_group_id()
        sender_id = event.get_sender_id()
        self_id = event.get_self_id()

        g = StateManager.get_group(gid)

        if self.cfg.reply.threshold > 0 and sender_id != self_id:
            g.msg_queue.append(event.message_obj.message_id)

        if self.cfg.pipeline.is_enabled_step(StepName.AT) and not self.cfg.at.at_str:
            name = event.get_sender_name()
            if len(g.name_to_qq) >= 100:
                g.name_to_qq.popitem(last=False)
            g.name_to_qq[name] = sender_id

    @filter.on_decorating_result(priority=15)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """发送消息前"""
        result = event.get_result()
        if not result or not result.chain:
            return

        ctx = OutContext(
            event=event,
            chain=result.chain,
            is_llm=result.is_llm_result(),
            plain=result.get_plain_text(),
            gid=event.get_group_id(),
            uid=event.get_sender_id(),
            bid=event.get_self_id(),
            group=StateManager.get_group(event.get_group_id()),
            timestamp=event.message_obj.timestamp,
        )

        await self.pipeline.run(ctx)

    def _block_llm_response_if_needed(self, response: LLMResponse, text: str) -> bool:
        if not text:
            return False

        for word in self.cfg.block.block_words:
            if not word or word not in text:
                continue

            result_chain = getattr(response, "result_chain", None)
            chain = getattr(result_chain, "chain", None)
            if isinstance(chain, list):
                chain.clear()
            else:
                response.completion_text = ""
            try:
                setattr(response, "_completion_text", "")
            except Exception:
                pass
            return True

        return False

    def _resolve_emotion_tag_policy(
        self, event: AstrMessageEvent, tts_instance
    ) -> tuple[bool, str]:
        marker_mode = self._get_tts_output_marker_mode(event)
        if marker_mode == TTS_OUTPUT_MARKER_MODE_PRESERVE:
            return True, "tts_marker_mode_preserve"
        if marker_mode == TTS_OUTPUT_MARKER_MODE_STRIP:
            return False, "tts_marker_mode_strip"

        if tts_instance is None:
            return False, "tts_plugin_missing"

        if not bool(getattr(tts_instance, "emo_marker_enable", False)):
            return False, "tts_marker_disabled"

        umo = self._resolve_tts_umo(event, tts_instance)
        if not umo:
            return False, "tts_umo_unavailable"

        if not self._is_tts_voice_output_enabled(tts_instance, umo):
            return False, "tts_voice_output_disabled"

        return True, "tts_voice_output_enabled"

    def _get_tts_router_instance(self, event: AstrMessageEvent):
        for star in self.context.get_all_stars():
            if not getattr(star, "activated", False):
                continue
            if not self._is_tts_emotion_router(star):
                continue
            if not self._is_star_enabled_for_event(event, star):
                continue

            instance = getattr(star, "star_cls", None)
            if instance is None:
                continue
            return instance

        return None

    @staticmethod
    def _is_tts_emotion_router(star) -> bool:
        names = {
            str(getattr(star, "name", "") or ""),
            str(getattr(star, "display_name", "") or ""),
            str(getattr(star, "root_dir_name", "") or ""),
            str(getattr(star, "module_path", "") or ""),
        }
        return (
            TTS_EMOTION_ROUTER_NAME in names
            or TTS_EMOTION_ROUTER_ROOT in names
            or any(TTS_EMOTION_ROUTER_ROOT in name for name in names if name)
        )

    @staticmethod
    def _is_star_enabled_for_event(event: AstrMessageEvent, star) -> bool:
        plugins_name = getattr(event, "plugins_name", None)
        if plugins_name is None or plugins_name == ["*"]:
            return True

        candidates = {
            str(getattr(star, "name", "") or ""),
            str(getattr(star, "display_name", "") or ""),
            str(getattr(star, "root_dir_name", "") or ""),
        }
        return any(candidate and candidate in plugins_name for candidate in candidates)

    @staticmethod
    def _resolve_tts_umo(event: AstrMessageEvent, instance) -> str:
        get_umo = getattr(instance, "_get_umo", None)
        if callable(get_umo):
            try:
                umo = get_umo(event)
                if isinstance(umo, str) and umo:
                    return umo
            except Exception:
                logger.debug(
                    "[outputpro:on_llm_response] failed to resolve umo via TTS plugin",
                    exc_info=True,
                )

        umo = getattr(event, "unified_msg_origin", None)
        return umo if isinstance(umo, str) else ""

    @staticmethod
    def _is_tts_voice_output_enabled(instance, umo: str) -> bool:
        config = getattr(instance, "config", None)
        checker = getattr(config, "is_voice_output_enabled_for_umo", None)
        if not callable(checker):
            return False

        try:
            return bool(checker(umo))
        except Exception:
            logger.debug(
                "[outputpro:on_llm_response] failed to inspect TTS voice output runtime state",
                exc_info=True,
            )
            return False

    @staticmethod
    def _get_tts_output_marker_mode(event: AstrMessageEvent) -> str | None:
        getter = getattr(event, "get_extra", None)
        if not callable(getter):
            return None

        try:
            marker_mode = getter(TTS_OUTPUT_MARKER_MODE_EXTRA)
        except TypeError:
            marker_mode = getter(TTS_OUTPUT_MARKER_MODE_EXTRA, None)
        except Exception:
            logger.debug(
                "[outputpro:on_llm_response] failed to read TTS marker mode extra",
                exc_info=True,
            )
            return None

        return marker_mode if isinstance(marker_mode, str) and marker_mode else None

    @staticmethod
    def _sanitize_with_tts_plugin(tts_instance, text: str) -> str:
        sanitizer = getattr(tts_instance, "sanitize_visible_output_text", None)
        if not callable(sanitizer):
            return text

        try:
            return sanitizer(text)
        except Exception:
            logger.debug(
                "[outputpro:on_llm_response] failed to sanitize text with TTS plugin helper",
                exc_info=True,
            )
            return text

    def _sanitize_for_tts_preserve_mode(self, tts_instance, text: str) -> str:
        protected_text, placeholders = self._protect_tts_controls(tts_instance, text)
        cleaned_text, _ = clean_text(
            protected_text,
            self.cfg.clean,
            emotion_tag=True,
        )
        return self._restore_tts_controls(cleaned_text, placeholders)

    def _protect_tts_controls(
        self, tts_instance, text: str
    ) -> tuple[str, dict[str, str]]:
        placeholders: dict[str, str] = {}
        counter = 0

        def _stash(raw: str) -> str:
            nonlocal counter
            token = f"__OUTPUTPRO_TTS_KEEP_{counter}__"
            counter += 1
            placeholders[token] = raw
            return token

        protected_text = TTS_PAUSE_TAG_RE.sub(
            lambda match: _stash(match.group(0)), text
        )
        expressive_tags = self._get_tts_expressive_tags(tts_instance)
        if expressive_tags:
            voice_tag_re = re.compile(
                rf"\((?:{'|'.join(re.escape(tag) for tag in expressive_tags)})\)",
                re.IGNORECASE,
            )
            protected_text = voice_tag_re.sub(
                lambda match: _stash(match.group(0)),
                protected_text,
            )

        return protected_text, placeholders

    @staticmethod
    def _restore_tts_controls(text: str, placeholders: dict[str, str]) -> str:
        restored = text
        for token, raw in placeholders.items():
            restored = restored.replace(token, raw)
        return restored

    @staticmethod
    def _get_tts_expressive_tags(tts_instance) -> tuple[str, ...]:
        if tts_instance is None:
            return ()

        try:
            module = importlib.import_module(tts_instance.__class__.__module__)
            tags = getattr(module, "MINIMAX_EXPRESSIVE_TAGS", None)
            if isinstance(tags, (list, tuple, set)):
                normalized = tuple(
                    str(tag).strip().lower() for tag in tags if str(tag).strip()
                )
                return normalized
        except Exception:
            logger.debug(
                "[outputpro:on_llm_response] failed to inspect TTS expressive tags",
                exc_info=True,
            )

        return ()

    @staticmethod
    def _set_event_extra(event: AstrMessageEvent, key: str, value) -> None:
        setter = getattr(event, "set_extra", None)
        if not callable(setter):
            return

        try:
            setter(key, value)
        except Exception:
            logger.debug(
                "[outputpro:on_llm_response] failed to set event extra %s",
                key,
                exc_info=True,
            )
