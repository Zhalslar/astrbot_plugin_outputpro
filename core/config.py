# config.py
from __future__ import annotations

import re
from collections.abc import MutableMapping
from typing import Any, get_type_hints

from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.star.star_tools import StarTools

# ==================================================
# 基础 Section（所有子配置的基类）
# ==================================================


class Section:
    """
    强类型配置节点基类
    - 字段由类型注解声明
    - 数据直接写回原 dict
    """

    __slots__ = ("_data",)

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)

        # 必填字段校验
        hints = get_type_hints(self.__class__)
        for key in hints:
            if key not in data:
                raise KeyError(f"缺少配置字段: {key}")

    def __getattr__(self, key: str) -> Any:
        try:
            value = self._data[key]
        except KeyError:
            raise AttributeError(key) from None
        return self._wrap(value)

    def __setattr__(self, key: str, value: Any) -> None:
        if key.startswith("_"):
            object.__setattr__(self, key, value)
        else:
            self._data[key] = value

    def raw(self) -> MutableMapping[str, Any]:
        return self._data

    @staticmethod
    def _wrap(value: Any) -> Any:
        if isinstance(value, MutableMapping):
            return value
        return value


# ==================================================
# 各 Section 类型定义（与 JSON 一一对应）
# ==================================================


class PipelineConfig(Section):
    lock_order: bool
    steps: list[str]
    llm_steps: list[str]

    def __init__(self, data: MutableMapping[str, Any]):
        super().__init__(data)
        self._steps = [name.split("(", 1)[0].strip() for name in self.steps]
        self._llm_steps = [name.split("(", 1)[0].strip() for name in self.llm_steps]

    def is_enabled_step(self, step_name: str) -> bool:
        return step_name in self._steps

    def is_llm_step(self, step_name: str) -> bool:
        return step_name in self._llm_steps


class SummaryConfig(Section):
    quotes: list[str]
    quotes_files: list[str]


class ErrorConfig(Section):
    keywords: list[str]
    custom_msg: str
    forward_umo: str


class BlockConfig(Section):
    timeout: int
    block_reread: bool
    ai_words: list[str]


class AtConfig(Section):
    at_str: bool
    at_prob: float


class CleanConfig(Section):
    text_threshold: int
    bracket: bool
    parenthesis: bool
    emotion_tag: bool
    emoji: bool
    lead: list[str]
    tail: list[str]
    punctuation: str


class ReplaceConfig(Section):
    words: list[str]
    default_new_word: str


class TTSConfig(Section):
    group_id: str
    character: str
    threshold: int
    prob: float

    def __init__(self, data: MutableMapping[str, Any]):
        super().__init__(data)
        self._character_id = self.character.split("（", 1)[1][:-1]


class T2IConfig(Section):
    threshold: int
    pillowmd_style_dir: str
    auto_page: bool
    clean_cache: bool


class ReplyConfig(Section):
    threshold: int


class ForwardConfig(Section):
    threshold: int
    node_name: str


class RecallConfig(Section):
    keywords: list[str]
    delay: int


class SplitConfig(Section):
    char_list: list[str]
    max_count: int
    typing_delay: str

    def __init__(self, data: MutableMapping[str, Any]):
        super().__init__(data)
        self._min_delay, self._max_delay = map(float, self.typing_delay.split(","))
        self._split_pattern = self._build_split_pattern()

    def _build_split_pattern(self) -> str:
        tokens = []
        for ch in self.char_list:
            if ch == "\\n":
                tokens.append("\n")
            elif ch == "\\s":
                tokens.append(r"\s")
            else:
                tokens.append(re.escape(ch))
        return f"[{''.join(tokens)}]+"

# ==================================================
# AstrBotConfig Facade（第一层）
# ==================================================


class TypedConfigFacade:
    __annotations__: dict[str, type]

    def __init__(self, cfg: AstrBotConfig):
        object.__setattr__(self, "_cfg", cfg)

        hints = get_type_hints(self.__class__)
        for key, tp in hints.items():
            if key.startswith("_"):
                continue
            if not isinstance(tp, type) or not issubclass(tp, Section):
                continue
            if key not in cfg:
                raise KeyError(f"缺少配置段: {key}")

            section = tp(cfg[key])
            object.__setattr__(self, key, section)

    def __getattr__(self, key: str) -> Any:
        return self._cfg[key]

    def save(self):
        self._cfg.save_config()

# ==================================================
# 插件配置入口
# ==================================================


class PluginConfig(TypedConfigFacade):
    # ===== JSON 配置 =====
    pipeline: PipelineConfig
    summary: SummaryConfig
    error: ErrorConfig
    block: BlockConfig
    at: AtConfig
    clean: CleanConfig
    replace: ReplaceConfig
    tts: TTSConfig
    t2i: T2IConfig
    reply: ReplyConfig
    forward: ForwardConfig
    recall: RecallConfig
    split: SplitConfig

    def __init__(self, cfg: AstrBotConfig, *, context: Context):
        super().__init__(cfg)
        # ===== 内置配置 =====
        self.context = context
        self.admins_id: list[str] = context.get_config().get("admins_id", [])
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_outputpro")
