import asyncio
import copy

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from ..config import PluginConfig
from ..model import OutContext, StepName, StepResult
from .base import BaseStep


class ErrorStep(BaseStep):
    name = StepName.ERROR

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.error
        self.admins_id = config.admins_id

    def _find_hit_keyword(self, text: str) -> str | None:
        for word in self.cfg.keywords:
            if word in text:
                return word
        return None

    async def _forward_to_admin(self, ctx: OutContext) -> str:
        """
        转发消息给设定的会话
        返回反馈信息（字符串）
        """
        chain = MessageChain([Plain(ctx.plain)])
        context = self.plugin_config.context

        if self.cfg.forward_umo == "admin":
            if not self.admins_id:
                logger.warning("未配置管理员ID，无法转发报错信息")
                return "未配置管理员ID，无法转发报错信息"

            failed = []
            for admin_id in self.admins_id:
                try:
                    session = copy.copy(ctx.event.session)
                    session.session_id = admin_id
                    
                    # 强制转换为私聊上下文，防止因复用群聊Session导致发送失败
                    if hasattr(session, 'message_type'):
                        session.message_type = 'private' 
                    if hasattr(session, 'group_id'):
                        session.group_id = None
                    if hasattr(session, 'detail_type'): # onebot v12
                         session.detail_type = 'private'
                    await context.send_message(session, chain)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"转发给 admin {admin_id} 失败：{e}")
                    failed.append(str(admin_id))

            if failed:
                return f"转发失败，失败 admin: {','.join(failed)}"
            return "转发成功"

        try:
            await context.send_message(self.cfg.forward_umo, chain)
            return "转发成功"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"转发给 {self.cfg.forward_umo} 失败：{e}")
            return f"转发失败：{e}"

    async def handle(self, ctx: OutContext) -> StepResult:
        hit_word = self._find_hit_keyword(ctx.plain)
        if not hit_word:
            return StepResult()

        msg = f"命中报错关键词 {hit_word}"

        if self.cfg.forward_umo:
            forward_msg = await self._forward_to_admin(ctx)
            msg += f"，{forward_msg}"

        ctx.event.set_result(ctx.event.plain_result(self.cfg.custom_msg))
        msg += f"，原消息替换为 {self.cfg.custom_msg}"

        return StepResult(msg=msg)
