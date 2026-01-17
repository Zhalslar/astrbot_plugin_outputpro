import copy

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
        """
        遍历关键词列表，返回第一个命中的词。
        没命中返回 None
        """
        for word in self.cfg.keywords:
            if word in text:
                return word
        return None

    async def _forward_to_admin(self, ctx: OutContext):
        """
        转发消息给设定的会话
        """
        chain = MessageChain([Plain(ctx.plain)])
        context = self.plugin_config.context
        if self.cfg.forward_umo == "admin":
            if not self.admins_id:
                raise Exception("未配置管理员ID, 无法转发报错信息")

            for admin_id in self.admins_id:
                session = copy.copy(ctx.event.session)
                session.session_id = admin_id
                await context.send_message(session, chain)
        else:
            await context.send_message(self.cfg.forward_umo, chain)

    async def handle(self, ctx: OutContext) -> StepResult:
        """
        1. 先检查是否命中关键词
        2. 根据 mode 执行不同逻辑
        """
        hit_word = self._find_hit_keyword(ctx.plain)
        if not hit_word:
            return StepResult()
        msg = f"命中报错关键词 {hit_word}"

        if self.cfg.forward_umo:
            await self._forward_to_admin(ctx)
            msg += f"，已转发至{self.cfg.forward_umo}"

        # 置换原消息，若为空字符框架会自动停止事件
        ctx.event.set_result(ctx.event.plain_result(self.cfg.custom_msg))
        msg += f"，原消息替换为 {self.cfg.custom_msg}"

        return StepResult(msg=msg)
