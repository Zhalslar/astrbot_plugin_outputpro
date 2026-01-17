import time

from ..config import PluginConfig
from ..model import OutContext, StepName, StepResult
from .base import BaseStep


class BlockStep(BaseStep):
    name = StepName.BLOCK

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.block

    # ================== 各类拦截 ==================

    async def _block_timeout(self, ctx: OutContext) -> StepResult | None:
        if self.cfg.timeout > 0 and int(time.time()) - ctx.timestamp > self.cfg.timeout:
            ctx.event.set_result(ctx.event.plain_result(""))
            return StepResult(
                abort=True,
                msg=f"已拦截超时消息: {ctx.plain}",
            )

        if ctx.is_llm:
            ctx.group.bot_msgs.append(ctx.plain)

    async def _block_dedup(self, ctx: OutContext) -> StepResult | None:
        if ctx.plain in ctx.group.bot_msgs:
            ctx.event.set_result(ctx.event.plain_result(""))
            return StepResult(
                abort=True,
                msg=f"已拦截超时消息: {ctx.plain}",
            )

        if ctx.is_llm:
            ctx.group.bot_msgs.append(ctx.plain)

    async def _block_ai(self, ctx: OutContext) -> StepResult | None:
        for word in self.cfg.ai_words:
            if word in ctx.plain:
                ctx.event.set_result(ctx.event.plain_result(""))
                return StepResult(
                    abort=True,
                    msg=f"已拦截人机话术: {ctx.plain}",
                )

    # ================== 主入口 ==================

    async def handle(self, ctx: OutContext) -> StepResult:
        for checker in (
            self._block_timeout,
            self._block_dedup,
            self._block_ai,
        ):
            result = await checker(ctx)
            if result is not None:
                return result

        return StepResult()
