import shutil
from pathlib import Path

from astrbot.api import logger
from astrbot.core.message.components import Image, Plain

from ..config import PluginConfig
from ..model import OutContext, StepName, StepResult
from .base import BaseStep


class T2IStep(BaseStep):
    name = StepName.T2I

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.t2i
        self.image_cache_dir = config.data_dir / "image_cache"
        self.image_cache_dir.mkdir(parents=True, exist_ok=True)
        self.style = None

    async def _load_style(self):
        try:
            import pillowmd

            style_path = Path(self.cfg.pillowmd_style_dir).resolve()
            self.style = pillowmd.LoadMarkdownStyles(style_path)
            return self.style
        except Exception as e:
            logger.error(f"加载 pillowmd 失败: {e}")

    async def handle(self, ctx: OutContext) -> StepResult:
        if (
            isinstance(ctx.chain[-1], Plain)
            and len(ctx.chain[-1].text) > self.cfg.threshold
        ):
            style = self.style or await self._load_style()
            if style:
                text = ctx.chain[-1].text
                img = await style.AioRender(
                    text=text,
                    useImageUrl=True,
                    autoPage=self.cfg.auto_page,
                )
                path = img.Save(self.image_cache_dir)
                ctx.chain[-1] = Image.fromFileSystem(str(path))
                return StepResult(msg=f"已将文本消息({text[:10]})转化为图片消息")
        return StepResult()

    async def terminate(self):
        if self.cfg.clean_cache and self.image_cache_dir.exists():
            try:
                shutil.rmtree(self.image_cache_dir)
            except Exception as e:
                logger.error(f"清理缓存失败: {e}")
            self.image_cache_dir.mkdir(parents=True, exist_ok=True)
