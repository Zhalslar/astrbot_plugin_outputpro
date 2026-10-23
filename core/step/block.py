import time

from astrbot.api import logger

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
        if self.cfg.timeout <= 0:
            return None

        # 工具/子代理轮天然会超过 block.timeout，不能按用户消息时间直接判定超时。
        # 以「最后一次工具活动时间」与「用户消息时间」中较晚者为计时起点：
        # 长工具轮不会被误杀，真正卡死、活动早已过期的轮次仍会被拦截。
        reference = float(ctx.timestamp)
        last_activity = ctx.event.get_extra("_agent_last_activity_ts")
        if last_activity is not None:
            try:
                reference = max(reference, float(last_activity))
            except (TypeError, ValueError):
                pass

        if int(time.time()) - reference > self.cfg.timeout:
            # 毫秒级记录，避免 int() 截断把刚超阈值的耗时显示成恰好等于 limit
            elapsed = time.time() - reference
            ctx.event.set_result(ctx.event.plain_result(""))
            logger.info(
                "[outputpro][block] reason=timeout chars=%d elapsed=%.3fs limit=%ds",
                len(ctx.plain),
                elapsed,
                self.cfg.timeout,
            )
            return StepResult(abort=True, msg=f"已拦截超时消息: {ctx.plain}")
        return None

    async def _block_reread(self, ctx: OutContext) -> StepResult | None:
        if not self.cfg.block_reread:
            return None
        if ctx.plain in ctx.group.bot_msgs:
            ctx.event.set_result(ctx.event.plain_result(""))
            logger.info("[outputpro][block] reason=reread chars=%d", len(ctx.plain))
            return StepResult(abort=True, msg=f"已拦截流口水消息: {ctx.plain}")

    async def _block_words(self, ctx: OutContext) -> StepResult | None:
        for word in self.cfg.block_words:
            if word in ctx.plain:
                ctx.event.set_result(ctx.event.plain_result(""))
                logger.info("[outputpro][block] reason=words chars=%d", len(ctx.plain))
                return StepResult(
                    abort=True,
                    msg=f"已拦截人机话术: {ctx.plain}",
                )

    # ================== 主入口 ==================

    async def handle(self, ctx: OutContext) -> StepResult:
        for checker in (
            self._block_timeout,
            self._block_reread,
            self._block_words,
        ):
            result = await checker(ctx)
            if result is not None:
                return result

        if ctx.is_llm:
            ctx.group.bot_msgs.append(ctx.plain)

        return StepResult()
