from __future__ import annotations

from astrbot.api import logger

from .config import PluginConfig
from .model import OutContext
from .step import (
    AtStep,
    BaseStep,
    BlockStep,
    CleanStep,
    ErrorStep,
    ForwardStep,
    RecallStep,
    ReplaceStep,
    ReplyStep,
    SplitStep,
    SplitterProStep,
    SummaryStep,
    T2IStep,
    TTSStep,
)


class Pipeline:
    """
    生产级 Pipeline：

    - 构建 step 实例
    - 统一 initialize / terminate
    - 对外唯一接口：run
    """

    # 默认顺序
    STEP_REGISTRY: list[tuple[str, type[BaseStep]]] = [
        ("summary", SummaryStep),
        ("error", ErrorStep),
        ("block", BlockStep),
        ("at", AtStep),
        ("clean", CleanStep),
        ("replace", ReplaceStep),
        ("tts", TTSStep),
        ("t2i", T2IStep),
        ("reply", ReplyStep),
        ("forward", ForwardStep),
        ("recall", RecallStep),
        ("split", SplitStep),
        ("splitter_pro", SplitterProStep),
    ]

    def __init__(self, config: PluginConfig):
        self.plugin_config = config
        self.cfg = config.pipeline
        self._steps: list[BaseStep] = []

        self._build_steps()

    def _build_steps(self) -> None:
        """
        根据配置构建步骤实例（默认顺序或自定义顺序）
        """
        if self.cfg.lock_order:
            for name, cls in self.STEP_REGISTRY:
                if name in self.cfg._steps:
                    step = cls(self.plugin_config)
                    self._steps.append(step)
        else:
            step_map = dict(self.STEP_REGISTRY)
            for name in self.cfg._steps:
                cls = step_map.get(name)
                if not cls:
                    logger.warning(f"未知的步骤: {name}")
                    continue
                step = cls(self.plugin_config)
                self._steps.append(step)

    # =================== Lifecycle =======================

    async def initialize(self) -> None:
        """初始化所有步骤"""
        for step in self._steps:
            await step.initialize()

    async def terminate(self) -> None:
        """终止所有步骤"""
        for step in self._steps:
            await step.terminate()

    # ==================== run =====================

    def _llm_allow(self, step: BaseStep, is_llm: bool) -> bool:
        return not self.cfg.is_llm_step(step.name) or is_llm

    async def run(self, ctx: OutContext) -> bool:
        """
        运行 pipeline
        """
        for step in self._steps:
            if not self._llm_allow(step, ctx.is_llm):
                continue

            result = await step.handle(ctx)
            if result.msg:
                if result.ok:
                    logger.debug(result.msg)
                else:
                    logger.warning(result.msg)

            if result.abort:
                return False

        return True
