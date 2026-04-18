from ..config import PluginConfig
from ..model import OutContext, StepName, StepResult
from ..sanitize import collect_visible_text, format_removed_summary, sanitize_chain
from .base import BaseStep


class CleanStep(BaseStep):
    name = StepName.CLEAN

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.clean

    async def handle(self, ctx: OutContext) -> StepResult:
        report = sanitize_chain(ctx.chain, self.cfg)
        ctx.plain = collect_visible_text(ctx.chain)
        return StepResult(msg=format_removed_summary(report))
