from __future__ import annotations

import itertools
import json
import math
import os
import random
from collections import defaultdict
from pathlib import Path

import jieba
from pypinyin import Style, pinyin

from astrbot.api import logger
from astrbot.core.message.components import BaseMessageComponent, Plain

from ..config import PluginConfig
from ..model import OutContext, StepName, StepResult
from .base import BaseStep


class ChineseTypoGenerator:
    _PINYIN_DICT_CACHE: dict[str, list[str]] | None = None
    _WORD_FREQ_CACHE: dict[str, float] | None = None

    def __init__(
        self,
        *,
        cache_dir: str | Path,
        error_rate: float = 0.01,
        min_freq: int = 9,
        tone_error_rate: float = 0.1,
        word_replace_rate: float = 0.01,
        max_freq_diff: int = 200,
    ):
        self.error_rate = error_rate
        self.min_freq = min_freq
        self.tone_error_rate = tone_error_rate
        self.word_replace_rate = word_replace_rate
        self.max_freq_diff = max_freq_diff
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.word_min_length = 2
        self.word_max_length = 4

        self.pinyin_dict = self._create_pinyin_dict()
        self.char_frequency = self._load_or_create_char_frequency()
        self.word_frequency = self._load_word_frequency()

    def _load_or_create_char_frequency(self) -> dict[str, float]:
        cache_file = self.cache_dir / "char_frequency.json"
        if cache_file.exists():
            with cache_file.open("r", encoding="utf-8") as f:
                return json.load(f)

        char_freq: defaultdict[str, int] = defaultdict(int)
        dict_path = Path(os.path.dirname(jieba.__file__)) / "dict.txt"

        with dict_path.open("r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 2:
                    continue
                word, freq = parts[:2]
                for char in word:
                    if self._is_chinese_char(char):
                        char_freq[char] += int(freq)

        if not char_freq:
            return {}

        max_freq = max(char_freq.values())
        normalized_freq = {char: freq / max_freq * 1000 for char, freq in char_freq.items()}

        with cache_file.open("w", encoding="utf-8") as f:
            json.dump(normalized_freq, f, ensure_ascii=False, indent=2)

        return normalized_freq

    @classmethod
    def _create_pinyin_dict(cls) -> dict[str, list[str]]:
        if cls._PINYIN_DICT_CACHE is not None:
            return cls._PINYIN_DICT_CACHE

        chars = [chr(i) for i in range(0x4E00, 0x9FFF)]
        pinyin_dict: defaultdict[str, list[str]] = defaultdict(list)
        for char in chars:
            try:
                py = pinyin(char, style=Style.TONE3)[0][0]
                pinyin_dict[py].append(char)
            except Exception:
                continue

        cls._PINYIN_DICT_CACHE = dict(pinyin_dict)
        return cls._PINYIN_DICT_CACHE

    @classmethod
    def _load_word_frequency(cls) -> dict[str, float]:
        if cls._WORD_FREQ_CACHE is not None:
            return cls._WORD_FREQ_CACHE

        dict_path = Path(os.path.dirname(jieba.__file__)) / "dict.txt"
        valid_words: dict[str, float] = {}
        with dict_path.open("r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 2:
                    valid_words[parts[0]] = float(parts[1])

        cls._WORD_FREQ_CACHE = valid_words
        return cls._WORD_FREQ_CACHE

    @staticmethod
    def _is_chinese_char(char: str) -> bool:
        try:
            return "\u4e00" <= char <= "\u9fff"
        except Exception:
            return False

    @staticmethod
    def _get_similar_tone_pinyin(py: str) -> str:
        if not py:
            return py
        if not py[-1].isdigit():
            return f"{py}1"

        base = py[:-1]
        tone = int(py[-1])
        if tone not in [1, 2, 3, 4]:
            return base + str(random.choice([1, 2, 3, 4]))

        possible_tones = [1, 2, 3, 4]
        possible_tones.remove(tone)
        return base + str(random.choice(possible_tones))

    def _calculate_replacement_probability(self, orig_freq: float, target_freq: float) -> float:
        if target_freq > orig_freq:
            return 1.0

        freq_diff = orig_freq - target_freq
        if freq_diff > self.max_freq_diff:
            return 0.0

        return math.exp(-3 * freq_diff / self.max_freq_diff)

    def _get_similar_frequency_chars(self, char: str, py: str, num_candidates: int = 5) -> list[str] | None:
        homophones: list[str] = []

        if random.random() < self.tone_error_rate:
            wrong_tone_py = self._get_similar_tone_pinyin(py)
            homophones.extend(self.pinyin_dict.get(wrong_tone_py, []))

        homophones.extend(self.pinyin_dict.get(py, []))
        if not homophones:
            return None

        orig_freq = self.char_frequency.get(char, 0)
        freq_diff = [
            (h, self.char_frequency.get(h, 0))
            for h in homophones
            if h != char and self.char_frequency.get(h, 0) >= self.min_freq
        ]
        if not freq_diff:
            return None

        candidates_with_prob: list[tuple[str, float]] = []
        for candidate, freq in freq_diff:
            prob = self._calculate_replacement_probability(orig_freq, freq)
            if prob > 0:
                candidates_with_prob.append((candidate, prob))

        if not candidates_with_prob:
            return None

        candidates_with_prob.sort(key=lambda x: x[1], reverse=True)
        return [char for char, _ in candidates_with_prob[:num_candidates]]

    @staticmethod
    def _get_word_pinyin(word: str) -> list[str]:
        return [py[0] for py in pinyin(word, style=Style.TONE3)]

    @staticmethod
    def _segment_sentence(sentence: str) -> list[str]:
        return list(jieba.cut(sentence))

    def _should_try_word_replacement(self, word: str) -> bool:
        if not (self.word_min_length <= len(word) <= self.word_max_length):
            return False
        if any(not self._is_chinese_char(char) for char in word):
            return False
        return word in self.word_frequency

    def _get_word_homophones(self, word: str) -> list[tuple[str, float]]:
        if not self._should_try_word_replacement(word):
            return []

        word_pinyin = self._get_word_pinyin(word)
        candidates: list[list[str]] = []
        for py in word_pinyin:
            chars = self.pinyin_dict.get(py, [])
            if not chars:
                return []
            candidates.append(chars)

        original_word_freq = self.word_frequency.get(word, 0)
        min_word_freq = max(original_word_freq * 0.05, 50.0)

        homophones: list[tuple[str, float]] = []
        for combo in itertools.product(*candidates):
            new_word = "".join(combo)
            if new_word == word or new_word not in self.word_frequency:
                continue
            new_word_freq = self.word_frequency[new_word]
            if new_word_freq < min_word_freq:
                continue
            char_avg_freq = sum(self.char_frequency.get(c, 0) for c in new_word) / len(new_word)
            combined_score = new_word_freq * 0.7 + char_avg_freq * 0.3
            if combined_score >= self.min_freq:
                homophones.append((new_word, combined_score))

        sorted_homophones = sorted(homophones, key=lambda x: x[1], reverse=True)
        return sorted_homophones[:5]

    def create_typo_sentence(self, sentence: str) -> tuple[str, str | None, list[tuple[str, str]]]:
        result: list[str] = []
        word_typos: list[tuple[str, str]] = []
        char_typos: list[tuple[str, str]] = []
        replacements: list[tuple[str, str]] = []

        words = self._segment_sentence(sentence)
        for word in words:
            if all(not self._is_chinese_char(c) for c in word):
                result.append(word)
                continue

            word_pinyin = self._get_word_pinyin(word)

            if self._should_try_word_replacement(word) and random.random() < self.word_replace_rate:
                word_homophones = self._get_word_homophones(word)
                if word_homophones:
                    candidates = [candidate for candidate, _score in word_homophones]
                    weights = [score for _candidate, score in word_homophones]
                    typo_word = random.choices(candidates, weights=weights, k=1)[0]
                    result.append(typo_word)
                    word_typos.append((typo_word, word))
                    replacements.append((word, typo_word))
                    continue

            if len(word) == 1:
                char = word
                py = word_pinyin[0]
                if random.random() < self.error_rate:
                    similar_chars = self._get_similar_frequency_chars(char, py)
                    if similar_chars:
                        typo_char = random.choice(similar_chars)
                        typo_freq = self.char_frequency.get(typo_char, 0)
                        orig_freq = self.char_frequency.get(char, 0)
                        replace_prob = self._calculate_replacement_probability(orig_freq, typo_freq)
                        if random.random() < replace_prob:
                            result.append(typo_char)
                            char_typos.append((typo_char, char))
                            replacements.append((char, typo_char))
                            continue
                result.append(char)
                continue

            result.append(word)

        correction_suggestion = None
        if word_typos:
            _wrong_word, correct_word = random.choice(word_typos)
            correction_suggestion = correct_word
        elif char_typos:
            _wrong_char, correct_char = random.choice(char_typos)
            correction_suggestion = correct_char

        return "".join(result), correction_suggestion, replacements

    def set_params(self, **kwargs):
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                logger.warning(f"[OutputProTypo] 未知参数: {key}")


class TypoStep(BaseStep):
    name = StepName.TYPO

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.typo
        self._typo_generator: ChineseTypoGenerator | None = None

    def _get_typo_generator(self) -> ChineseTypoGenerator:
        if self._typo_generator is None:
            self._typo_generator = ChineseTypoGenerator(
                cache_dir=self.plugin_config.data_dir,
                error_rate=self.cfg.error_rate,
                tone_error_rate=self.cfg.tone_error_rate,
                word_replace_rate=self.cfg.word_replace_rate,
            )
        else:
            self._typo_generator.set_params(
                error_rate=self.cfg.error_rate,
                tone_error_rate=self.cfg.tone_error_rate,
                word_replace_rate=self.cfg.word_replace_rate,
            )
        return self._typo_generator

    @staticmethod
    def _split_platform_supported(ctx: OutContext) -> bool:
        return ctx.event.get_platform_name() in {"aiocqhttp", "telegram", "lark"}

    def _split_will_run_for_chain(
        self, ctx: OutContext, chain: list[BaseMessageComponent]
    ) -> bool:
        pipeline_cfg = self.plugin_config.pipeline
        if not pipeline_cfg.is_enabled_step(StepName.SPLIT):
            return False
        if pipeline_cfg.is_llm_step(StepName.SPLIT) and not ctx.is_llm:
            return False
        if not self._split_platform_supported(ctx):
            return False
        split_cfg = self.plugin_config.split
        max_length = int(getattr(split_cfg, "max_length", 0) or 0)
        if max_length > 0 and self._get_chain_text_length(chain) > max_length:
            return False
        return True

    def _split_will_run_for_context(self, ctx: OutContext) -> bool:
        return self._split_will_run_for_chain(ctx, ctx.chain)

    def _resolve_append_separator(self, ctx: OutContext) -> str | None:
        if not self._split_will_run_for_context(ctx):
            return None
        return "\n"


    @staticmethod
    def _get_chain_text_length(chain: list[Plain]) -> int:
        return sum(len(seg.text) for seg in chain if isinstance(seg, Plain))

    def _build_preview_chain(
        self,
        ctx: OutContext,
        target_seg: Plain,
        replacement_text: str,
    ) -> list[BaseMessageComponent]:
        preview_chain: list[BaseMessageComponent] = []
        for comp in ctx.chain:
            if comp is target_seg:
                preview_chain.append(Plain(replacement_text))
            elif isinstance(comp, Plain):
                preview_chain.append(Plain(comp.text))
            else:
                preview_chain.append(comp)
        return preview_chain

    def _should_append_correction(
        self,
        ctx: OutContext,
        target_seg: Plain,
        *,
        typoed_text: str,
        correction_text: str,
        append_separator: str | None,
    ) -> bool:
        if not append_separator or not correction_text:
            return False

        preview_text = append_separator.join([typoed_text, correction_text])
        preview_chain = self._build_preview_chain(ctx, target_seg, preview_text)
        if not self._split_will_run_for_chain(ctx, preview_chain):
            return False

        from .split import SplitStep

        split_step = SplitStep(self.plugin_config)
        preview_segments = split_step._split_chain(preview_chain)
        if len(preview_segments) <= 1:
            return False

        correction_text = correction_text.strip()
        for seg in preview_segments[1:]:
            seg_text = "".join(
                comp.text for comp in seg.components if isinstance(comp, Plain)
            ).strip()
            if seg_text == correction_text:
                return True
        return False

    async def handle(self, ctx: OutContext) -> StepResult:
        replacement_summaries: list[str] = []
        append_separator = self._resolve_append_separator(ctx)
        generator = self._get_typo_generator()

        for seg in ctx.chain:
            if not isinstance(seg, Plain):
                continue
            if not seg.text or not seg.text.strip():
                continue

            typoed_text, typo_corrections, replacements = generator.create_typo_sentence(seg.text)
            processed_parts = [typoed_text]
            if typo_corrections and random.random() < self.cfg.correction_append_prob:
                if self._should_append_correction(
                    ctx,
                    seg,
                    typoed_text=typoed_text,
                    correction_text=typo_corrections,
                    append_separator=append_separator,
                ):
                    processed_parts.append(typo_corrections)
            separator = append_separator or ""
            new_text = separator.join(part for part in processed_parts if part)
            if new_text and new_text != seg.text:
                seg.text = new_text
                replacement_summaries.extend(
                    f"{original}->{typo}" for original, typo in replacements
                )

        if replacement_summaries:
            summary_suffix = ""
            preview = ", ".join(replacement_summaries[:5])
            if len(replacement_summaries) > 5:
                preview += ", ..."
            summary_suffix = f"（{preview}）"
            return StepResult(
                msg=f"错字模拟完成，共 {len(replacement_summaries)} 处替换{summary_suffix}"
            )
        return StepResult()
