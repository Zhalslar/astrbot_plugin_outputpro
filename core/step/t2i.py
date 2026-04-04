import shutil
from pathlib import Path
import asyncio

from astrbot import logger
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
        """
        加载 pillowmd 样式
        """
        try:
            import pillowmd

            style_path = Path(self.cfg.pillowmd_style_dir).resolve()
            self.style = pillowmd.LoadMarkdownStyles(style_path)
            return self.style
        except Exception as e:
            logger.error(f"加载 pillowmd 失败: {e}")

    async def handle(self, ctx: OutContext) -> StepResult:

        # model.py 中定义的 OutContext 里，已经有一个 plain 字段了，
        # 而且是直接把文本消息内容提取出来的字符串，所以这里直接用 ctx.plain 来判断比较明了简单了
        if not ctx.plain or len(ctx.plain) <= self.cfg.threshold:
            return StepResult()
        style = self.style or await self._load_style()
        if style:
            text = ctx.plain
            img = await style.AioRender(
                text=text,
                useImageUrl=True,
                autoPage=self.cfg.auto_page,
            )

            #这个pillowmd库的Save方法是阻塞的，所以放到线程池里执行，避免阻塞主线程
            path = await asyncio.to_thread(img.Save, self.image_cache_dir)
            ctx.chain[-1] = Image.fromFileSystem(str(path))
            return StepResult(msg=f"已将文本消息({text[:10]})转化为图片消息")
        else:
            logger.error("无法加载 pillowmd 样式，无法执行文本转图片")
            return StepResult(ok=False, msg="pillowmd 样式加载失败")
    

    async def terminate(self):
        if self.cfg.clean_cache and self.image_cache_dir.exists():
            try:
                shutil.rmtree(self.image_cache_dir)
            except Exception as e:
                logger.error(f"清理缓存失败: {e}")
            self.image_cache_dir.mkdir(parents=True, exist_ok=True)
