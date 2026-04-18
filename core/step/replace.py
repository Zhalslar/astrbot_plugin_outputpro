from ..config import PluginConfig
from ..model import OutContext, StepName, StepResult
from ..sanitize import collect_visible_text, replace_in_chain
from .base import BaseStep


class ReplaceStep(BaseStep):
    name = StepName.REPLACE

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.replace

    async def handle(self, ctx: OutContext) -> StepResult:
        changes = replace_in_chain(ctx.chain, self.cfg)
        ctx.plain = collect_visible_text(ctx.chain)

        if changes:
            msg = "replaced ->\n" + "\n".join(f"{old} -> {new}" for old, new in changes)
            return StepResult(msg=msg)

        return StepResult()
