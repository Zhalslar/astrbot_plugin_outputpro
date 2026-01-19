# config.py
from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping
from types import MappingProxyType
from typing import Any, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.star.star_tools import StarTools


class ConfigNode:
    """
    配置节点, 把 dict 变成强类型对象。

    规则：
    - schema 来自子类类型注解
    - 声明字段：读写，写回底层 dict
    - 未声明字段和下划线字段：仅挂载属性，不写回
    - 支持 ConfigNode 多层嵌套（lazy + cache）
    """

    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    # ---------- schema ----------

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key in self._fields():
            if key not in data and not hasattr(self.__class__, key):
                logger.warning(f"[config:{self.__class__.__name__}] 缺少字段: {key}")
                continue

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] "
                            f"字段 {key} 期望 dict，实际是 {type(value).__name__}"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        """
        底层配置 dict 的只读视图
        """
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        """
        保存配置到磁盘（仅允许在根节点调用）
        """
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() 只能在根配置节点上调用"
            )
        self._data.save_config()


# ============ 插件自定义配置 ==================


class PipelineConfig(ConfigNode):
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


class SummaryConfig(ConfigNode):
    quotes: list[str]
    quotes_files: list[str]


class ErrorConfig(ConfigNode):
    keywords: list[str]
    custom_msg: str
    forward_umo: str


class BlockConfig(ConfigNode):
    timeout: int
    block_reread: bool
    ai_words: list[str]


class AtConfig(ConfigNode):
    at_str: bool
    at_prob: float


class CleanConfig(ConfigNode):
    text_threshold: int
    bracket: bool
    parenthesis: bool
    emotion_tag: bool
    emoji: bool
    lead: list[str]
    tail: list[str]
    punctuation: str


class ReplaceConfig(ConfigNode):
    words: list[str]
    default_new_word: str


class TTSConfig(ConfigNode):
    group_id: str
    character: str
    threshold: int
    prob: float

    def __init__(self, data: MutableMapping[str, Any]):
        super().__init__(data)
        self._character_id = self.character.split("（", 1)[1][:-1]


class T2IConfig(ConfigNode):
    threshold: int
    pillowmd_style_dir: str
    auto_page: bool
    clean_cache: bool


class ReplyConfig(ConfigNode):
    threshold: int


class ForwardConfig(ConfigNode):
    threshold: int
    node_name: str


class RecallConfig(ConfigNode):
    keywords: list[str]
    delay: int


class SplitConfig(ConfigNode):
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


class PluginConfig(ConfigNode):
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
        self.context = context
        self.admins_id: list[str] = context.get_config().get("admins_id", [])
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_outputpro")
