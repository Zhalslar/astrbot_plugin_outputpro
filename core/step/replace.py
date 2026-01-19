from astrbot.core.message.components import Plain

from ..config import PluginConfig
from ..model import OutContext, StepName, StepResult
from .base import BaseStep


class ReplaceStep(BaseStep):
    name = StepName.REPLACE

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.replace

    def _unescape(self, s: str) -> str:
        """
        简单转义处理：将 \\n, \\r, \\t, \\s 等转换为实际字符
        """
        return (s
                .replace("\\n", "\n")   # 换行符
                .replace("\\r", "\r")   # 回车符
                .replace("\\t", "\t")   # 制表符
                .replace("\\s", " ")    # 空格
                .replace("\\\\", "\\")  # 反斜杠本身（放最后）
        )

    async def handle(self, ctx: OutContext) -> StepResult:
        changes: list[tuple[str, str]] = []

        for seg in ctx.chain:
            if isinstance(seg, Plain):
                for word in self.cfg.words:
                    if not word.strip():
                        continue
                        
                    raw_old, sep, raw_new = word.partition(" ")
                    old = self._unescape(raw_old)
                    
                    if not sep:
                        new = self.cfg.default_new_word * len(old)
                    else:
                        new = self._unescape(raw_new)
                    

                    if old in seg.text:
                        seg.text = seg.text.replace(old, new)
                        changes.append((repr(old), repr(new)))

        if changes:
            msg = "已替换：\n" + "\n".join(f"{old} -> {new}" for old, new in changes)
            return StepResult(msg=msg)

        return StepResult()
