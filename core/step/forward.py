from astrbot.core.message.components import (
    Node,
    Nodes,
    Plain,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ..config import PluginConfig
from ..model import OutContext, StepName, StepResult
from .base import BaseStep


class ForwardStep(BaseStep):
    name = StepName.FORWARD

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.forward
        self._configured_node_name: str = str(self.cfg.node_name or "").strip()
        self._bot_name_cache: dict[str, str] = {}
        self._tg_single_message_limit = 3500

    async def _ensure_node_name(self, event: AstrMessageEvent) -> str:
        if self._configured_node_name:
            return self._configured_node_name

        bot_id = str(event.get_self_id() or "").strip()
        if bot_id and bot_id in self._bot_name_cache:
            return self._bot_name_cache[bot_id]

        node_name = ""
        if isinstance(event, AiocqhttpMessageEvent):
            try:
                info = await event.bot.get_login_info()
                if nickname := info.get("nickname"):
                    node_name = str(nickname).strip()
            except Exception:
                pass

        if not node_name:
            node_name = "AstrBot"

        if bot_id:
            self._bot_name_cache[bot_id] = node_name
        return node_name

    def _get_platform_name(self, event: AstrMessageEvent) -> str:
        return str(event.get_platform_name() or "")

    def _is_tg_platform(self, event: AstrMessageEvent) -> bool:
        return self._get_platform_name(event) == "telegram"

    def _tg_utf16_len(self, text: str) -> int:
        if not text:
            return 0
        return len(text.encode("utf-16-le")) // 2

    def _tg_split_by_utf16(self, text: str, max_len: int) -> list[str]:
        if not text:
            return [""]
        if max_len <= 0:
            return [text]
        chunks = []
        buf = []
        buf_len = 0
        for ch in text:
            ch_len = self._tg_utf16_len(ch)
            if buf and buf_len + ch_len > max_len:
                chunks.append("".join(buf))
                buf = [ch]
                buf_len = ch_len
            else:
                buf.append(ch)
                buf_len += ch_len
        if buf:
            chunks.append("".join(buf))
        return chunks

    async def _send_tg_expandable_blocks(
        self, event: AstrMessageEvent, messages: list[str]
    ) -> bool:
        from telegram import MessageEntity
        from telegram.ext import ExtBot

        if not messages:
            return False

        tg_bot = getattr(event, "client", None)
        if not tg_bot or not isinstance(tg_bot, ExtBot):
            return False

        chat_id = str(event.get_group_id() or event.get_sender_id())
        message_thread_id = None

        if "#" in chat_id:
            chat_id, thread = chat_id.split("#", 1)
            if thread.isdigit():
                message_thread_id = int(thread)

        max_len = max(200, int(self._tg_single_message_limit))
        groups = []

        def flush_group(text: str, entities: list[MessageEntity]):
            if not text:
                return
            groups.append((text, entities))

        current_text = ""
        current_entities: list[MessageEntity] = []
        current_len = self._tg_utf16_len(current_text)

        expanded_blocks = []
        for block in messages:
            block = (block or "").strip()
            if not block:
                continue
            if self._tg_utf16_len(block) > max_len:
                expanded_blocks.extend(self._tg_split_by_utf16(block, max_len))
            else:
                expanded_blocks.append(block)

        for block in expanded_blocks:
            block = (block or "").strip()
            if not block:
                continue
            if not current_text:
                current_text = block
                current_entities = [
                    MessageEntity(
                        type="expandable_blockquote",
                        offset=0,
                        length=self._tg_utf16_len(block),
                    )
                ]
                current_len = self._tg_utf16_len(block)
                continue
            prefix = "\n\n"
            add_text = prefix + block
            add_len = self._tg_utf16_len(add_text)
            if current_len + add_len > max_len:
                flush_group(current_text, current_entities)
                current_text = block
                current_entities = [
                    MessageEntity(
                        type="expandable_blockquote",
                        offset=0,
                        length=self._tg_utf16_len(block),
                    )
                ]
                current_len = self._tg_utf16_len(block)
                continue
            offset = self._tg_utf16_len(current_text + prefix)
            current_text += add_text
            current_entities.append(
                MessageEntity(
                    type="expandable_blockquote",
                    offset=offset,
                    length=self._tg_utf16_len(block),
                )
            )
            current_len += add_len

        flush_group(current_text, current_entities)

        sent = 0
        for text, entities in groups:
            await tg_bot.send_message(
                chat_id=chat_id,
                text=text,
                entities=entities or None,
                message_thread_id=message_thread_id,
                parse_mode=None,
            )
            sent += 1
        return sent > 0

    async def handle(self, ctx: OutContext) -> StepResult:
        if (
            not isinstance(ctx.chain[-1], Plain)
            or len(ctx.chain[-1].text) <= self.cfg.threshold
        ):
            return StepResult()

        if isinstance(ctx.event, AiocqhttpMessageEvent):
            nodes = Nodes([])
            name = await self._ensure_node_name(ctx.event)
            uin = str(ctx.event.get_self_id() or ctx.bid)
            content = list(ctx.chain.copy())
            nodes.nodes.append(Node(uin=uin, name=name, content=content))
            ctx.chain[:] = [nodes]
            return StepResult(msg="已将消息转换为转发节点")

        if self._is_tg_platform(ctx.event):
            text = ctx.chain[-1].text
            messages = self._tg_split_by_utf16(text, self._tg_single_message_limit)
            success = await self._send_tg_expandable_blocks(ctx.event, messages)
            if success:
                ctx.event.stop_event()
                ctx.chain.clear()
                return StepResult(msg="已使用 Telegram 折叠引用发送")

        return StepResult()
