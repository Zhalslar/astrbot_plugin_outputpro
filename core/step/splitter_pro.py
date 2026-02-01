import asyncio
import re
import math
import random
from dataclasses import dataclass, field
from typing import List, Dict

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import (
    At,
    BaseMessageComponent,
    Face,
    Image,
    Plain,
    Reply,
)

from ..config import PluginConfig
from ..model import OutContext, StepName, StepResult
from .base import BaseStep


class SplitterProStep(BaseStep):
    name = StepName.SPLITTER_PRO

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.cfg = config.splitter_pro
        self.context = config.context
        
        self._pair_map = {
            "“": "”", "《": "》", "（": "）", "(": ")",
            "[": "]", "{": "}", "‘": "’", "【": "】", "<": ">",
        }
        self._quote_chars = {"", "'", "`"}

    async def handle(self, ctx: OutContext) -> StepResult:
        """
        对消息进行拆分并发送。
        最后一段会回填到原 chain 中。
        """
        platform_name = ctx.event.get_platform_name()
        # outputpro 限制平台，splitter 没限制但 outputpro 的分段一般用于文本量大的平台
        if platform_name not in {"aiocqhttp", "telegram", "lark"}:
            return StepResult()

        # 1. 作用范围检查 (Split Scope)
        split_scope = self.cfg.split_scope
        if split_scope == "llm_only" and not ctx.is_llm:
            return StepResult()

        # 2. 长度限制检查 (Max Length No Split)
        max_len_no_split = self.cfg.max_length_no_split
        total_text_len = sum(len(c.text) for c in ctx.chain if isinstance(c, Plain))
        if max_len_no_split > 0 and total_text_len < max_len_no_split:
            return StepResult()

        # 3. 获取配置 & 确定分段正则
        split_mode = self.cfg.split_mode
        if split_mode == "simple":
            split_chars = self.cfg.split_chars
            # Escape split chars for regex
            split_pattern = f"[{re.escape(split_chars)}]+"
        else:
            split_pattern = self.cfg.split_regex

        smart_mode = self.cfg.smart
        max_segs = self.cfg.max_count
        clean_pattern = self.cfg.clean_regex
        enable_reply = self.cfg.enable_reply
        
        # 策略映射
        strategies = {
            'image': self.cfg.image_strategy,
            'at': self.cfg.at_strategy,
            'face': self.cfg.face_strategy,
            'default': self.cfg.other_media_strategy
        }

        # 4. 执行分段
        segments = self.split_chain_smart(ctx.chain, split_pattern, smart_mode, strategies, enable_reply)

        # 5. 最大分段数限制
        if len(segments) > max_segs and max_segs > 0:
            logger.warning(f"[SplitterPro] 分段数({len(segments)}) 超过限制({max_segs})，合并剩余段落。")
            merged_last = []
            final_segments = segments[:max_segs-1]
            for seg in segments[max_segs-1:]:
                merged_last.extend(seg)
            final_segments.append(merged_last)
            segments = final_segments

        # 如果只有一段，且不需要清理，直接放行 (不处理)
        if len(segments) <= 1 and not clean_pattern:
            return StepResult()

        # 6. 注入引用 (Reply) - 仅第一段 (Splitter Logic)
        if enable_reply and segments and ctx.event.message_obj.message_id:
            # check if first segment has reply
            has_reply = any(isinstance(c, Reply) for c in segments[0])
            if not has_reply:
                # Add Reply to original message
                segments[0].insert(0, Reply(id=ctx.event.message_obj.message_id))

        logger.info(f"[SplitterPro] 消息被分为 {len(segments)} 段。")

        # 7. 清理正则
        if clean_pattern:
            for seg in segments:
                for comp in seg:
                    if isinstance(comp, Plain) and comp.text:
                        comp.text = re.sub(clean_pattern, "", comp.text)

        # 8. 逐段发送 (前 N-1 段)
        for i in range(len(segments) - 1):
            segment_chain = segments[i]
            
            # 空内容检查
            text_content = "".join([c.text for c in segment_chain if isinstance(c, Plain)])
            has_media = any(not isinstance(c, Plain) for c in segment_chain)
            
            if not text_content.strip() and not has_media:
                continue

            try:
                # 日志
                self._log_segment(i + 1, len(segments), segment_chain, "主动发送")
                
                mc = MessageChain(segment_chain)
                await self.context.send_message(ctx.event.unified_msg_origin, mc)

                # 延迟
                wait_time = self.calculate_delay(text_content)
                await asyncio.sleep(wait_time)

            except Exception as e:
                logger.error(f"[SplitterPro] 发送分段 {i+1} 失败: {e}")

        # 9. 处理最后一段 (回填)
        ctx.chain.clear()
        if segments:
            last_segment = segments[-1]
            last_text = "".join([c.text for c in last_segment if isinstance(c, Plain)])
            last_has_media = any(not isinstance(c, Plain) for c in last_segment)
            
            if not last_text.strip() and not last_has_media:
                pass # 空
            else:
                self._log_segment(len(segments), len(segments), last_segment, "交给框架")
                ctx.chain.extend(last_segment)

        return StepResult(msg="分段回复完成")

    def calculate_delay(self, text: str) -> float:
        strategy = self.cfg.delay_strategy
        text_len = len(text)
        if strategy == "random":
            return random.uniform(self.cfg.random_min, self.cfg.random_max)
        elif strategy == "log":
            base = self.cfg.log_base
            factor = self.cfg.log_factor
            return min(base + factor * math.log(text_len + 1), 5.0)
        elif strategy == "linear":
            return self.cfg.linear_base + (text_len * self.cfg.linear_factor)
        elif strategy == "fixed":
            return self.cfg.fixed_delay
        else:
            return 0.5 + (text_len * 0.1)

    def _log_segment(self, index: int, total: int, chain: List[BaseMessageComponent], method: str):
        content_str = ""
        for comp in chain:
            if isinstance(comp, Plain):
                content_str += comp.text
            else:
                content_str += f"[{type(comp).__name__}]"
        log_content = content_str.replace('\n', '\\n')
        logger.info(f"[SplitterPro] 第 {index}/{total} 段 ({method}): {log_content}")

    def split_chain_smart(self, chain: List[BaseMessageComponent], pattern: str, smart_mode: bool, strategies: Dict[str, str], enable_reply: bool) -> List[List[BaseMessageComponent]]:
        segments = []
        current_chain_buffer = []

        for component in chain:
            if isinstance(component, Plain):
                text = component.text
                if not text: continue
                if not smart_mode:
                    self._process_text_simple(text, pattern, segments, current_chain_buffer)
                else:
                    self._process_text_smart(text, pattern, segments, current_chain_buffer)
            else:
                c_type = type(component).__name__.lower()
                # If reply is found in chain, handle based on enable_reply
                if 'reply' in c_type:
                    if enable_reply:
                        current_chain_buffer.append(component)
                    continue

                if 'image' in c_type: strategy = strategies['image']
                elif 'at' in c_type: strategy = strategies['at']
                elif 'face' in c_type: strategy = strategies['face']
                else: strategy = strategies['default']

                if strategy == "单独":
                    if current_chain_buffer:
                        segments.append(current_chain_buffer[:])
                        current_chain_buffer.clear()
                    segments.append([component])
                elif strategy == "跟随上段":
                    if current_chain_buffer: current_chain_buffer.append(component)
                    elif segments: segments[-1].append(component)
                    else: current_chain_buffer.append(component)
                else: # 跟随下段
                    current_chain_buffer.append(component)

        if current_chain_buffer:
            segments.append(current_chain_buffer)
        return [seg for seg in segments if seg]

    def _process_text_simple(self, text: str, pattern: str, segments: list, buffer: list):
        parts = re.split(f"({pattern})", text)
        temp_text = ""
        for part in parts:
            if not part: continue
            if re.fullmatch(pattern, part):
                temp_text += part
                buffer.append(Plain(temp_text))
                segments.append(buffer[:])
                buffer.clear()
                temp_text = ""
            else:
                if temp_text: buffer.append(Plain(temp_text)); temp_text = ""
                buffer.append(Plain(part))
        if temp_text: buffer.append(Plain(temp_text))

    def _process_text_smart(self, text: str, pattern: str, segments: list, buffer: list):
        stack = []
        try:
            compiled_pattern = re.compile(pattern)
        except re.error:
            # Fallback pattern
            compiled_pattern = re.compile(r"[。？！?!\\n]+")

        i = 0
        n = len(text)
        current_chunk = ""

        while i < n:
            char = text[i]
            is_opener = char in self._pair_map
            if char in self._quote_chars:
                if stack and stack[-1] == char: stack.pop() 
                else: stack.append(char)
                current_chunk += char; i += 1; continue
            if stack:
                expected_closer = self._pair_map.get(stack[-1])
                if char == expected_closer: stack.pop()
                elif is_opener: stack.append(char)
                current_chunk += char; i += 1; continue
            if is_opener:
                stack.append(char); current_chunk += char; i += 1; continue

            match = compiled_pattern.match(text, pos=i)
            if match:
                delimiter = match.group()
                current_chunk += delimiter
                buffer.append(Plain(current_chunk))
                segments.append(buffer[:])
                buffer.clear()
                current_chunk = ""
                i += len(delimiter)
            else:
                current_chunk += char; i += 1
        if current_chunk: buffer.append(Plain(current_chunk))