import random
import re
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeAlias

import emoji

from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import (
    At,
    Face,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Reply,
)
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.star_tools import StarTools

from .core.at_policy import AtPolicy
from .core.model import GroupState, OutContext, StateManager
from .core.recall import Recaller
from .core.split import MessageSplitter

# ============================================================
# Typing
# ============================================================

StepResult: TypeAlias = bool | None
StepHandler: TypeAlias = Callable[[OutContext], Awaitable[StepResult]]

# ============================================================
# Pipeline Core
# ============================================================


class Step:
    """单个流水线步骤"""

    def __init__(self, name: str, handler: StepHandler):
        self.name = name
        self.handler = handler


class Pipeline:
    """顺序执行流水线"""

    def __init__(self, steps: list[Step], llm_steps: list[str]):
        self.steps = steps
        self.llm_steps = llm_steps

    def llm_allow(self, step_name: str, is_llm: bool) -> bool:
        if not self.llm_steps:
            return True
        return step_name not in self.llm_steps or is_llm

    async def run(self, ctx: OutContext) -> bool:
        for step in self.steps:
            if not self.llm_allow(step.name, ctx.is_llm):
                continue
            ret = await step.handler(ctx)
            if ret is False:
                return False

        return True


# ============================================================
# Plugin
# ============================================================


class OutputPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.context = context

        self.data_dir = StarTools.get_data_dir("astrbot_plugin_outputpro")
        self.image_cache_dir = self.data_dir / "image_cache"
        self.image_cache_dir.mkdir(parents=True, exist_ok=True)

        admins_id: list[str] = context.get_config().get("admins_id", [])
        self.admin_id: str | None = admins_id[0] if admins_id else None

        self.at_policy = AtPolicy(config)
        self.splitter = MessageSplitter(context, config)
        self.style = None

        self._enabled_steps: list[str] = [
            name.split("(", 1)[0].strip() for name in config["pipeline"]["steps"]
        ]

        self._llm_steps: list[str] = [
            name.split("(", 1)[0].strip() for name in config["pipeline"]["llm_steps"]
        ]

        self.pipeline = self._build_pipeline()

    # ============================================================
    # Helpers
    # ============================================================

    def is_step_enabled(self, name: str) -> bool:
        """判断步骤是否启用"""
        return name in self._enabled_steps

    async def _ensure_node_name(self, event: AstrMessageEvent) -> str:
        """确保消息节点名称"""
        fconf = self.conf["forward"]

        if fconf.get("node_name"):
            return fconf["node_name"]

        new_name = "AstrBot"
        if isinstance(event, AiocqhttpMessageEvent):
            try:
                info = await event.bot.get_login_info()
                if info.get("nickname"):
                    new_name = str(info["nickname"])
            except Exception:
                pass

        fconf["node_name"] = new_name
        self.conf.save_config()
        return new_name

    # ============================================================
    # Lifecycle
    # ============================================================

    async def initialize(self):
        self.recaller = Recaller(self.conf)

        if self.is_step_enabled("t2i"):
            try:
                import pillowmd

                style_path = Path(self.conf["t2i"]["pillowmd_style_dir"]).resolve()
                self.style = pillowmd.LoadMarkdownStyles(style_path)
            except Exception as e:
                logger.error(f"加载 pillowmd 失败: {e}")

    async def terminate(self):
        await self.recaller.terminate()
        if self.conf["t2i"]["clean_cache"] and self.image_cache_dir.exists():
            try:
                shutil.rmtree(self.image_cache_dir)
            except Exception as e:
                logger.error(f"清理缓存失败: {e}")
            self.image_cache_dir.mkdir(parents=True, exist_ok=True)

    # ============================================================
    # Pipeline Builder
    # ============================================================
    def _register_steps(self) -> dict[str, Step]:
        return {
            "summary": Step("summary", self._step_summary),
            "error": Step("error", self._step_error),
            "dedup": Step("dedup", self._step_dedup),
            "block_ai": Step("block_ai", self._step_block_ai),
            "parse_at": Step("parse_at", self._step_parse_at),
            "clean": Step("clean", self._step_clean),
            "tts": Step("tts", self._step_tts),
            "t2i": Step("t2i", self._step_t2i),
            "reply": Step("reply", self._step_reply),
            "forward": Step("forward", self._step_forward),
            "recall": Step("recall", self._step_recall),
            "split": Step("split", self._step_split),
        }

    def _build_pipeline(self) -> Pipeline:
        step_map = self._register_steps()
        steps: list[Step] = []

        if self.conf["pipeline"]["lock_order"]:
            # 锁定顺序：按注册顺序，但只执行启用的
            for name, step in step_map.items():
                if name in self._enabled_steps:
                    steps.append(step)
        else:
            # 自定义顺序
            for name in self._enabled_steps:
                step = step_map.get(name)
                if not step:
                    raise ValueError(f"Unknown pipeline step: {name}")
                steps.append(step)

        return Pipeline(steps=steps, llm_steps=self._llm_steps)

    # ============================================================
    # Steps
    # ============================================================

    async def _step_summary(self, ctx: OutContext) -> StepResult:
        """图片外显（直接发送并中断流水线）"""
        if (
            not isinstance(ctx.event, AiocqhttpMessageEvent)
            or len(ctx.chain) != 1
            or not isinstance(ctx.chain[0], Image)
        ):
            return None

        obmsg = await ctx.event._parse_onebot_json(MessageChain(ctx.chain))
        obmsg[0]["data"]["summary"] = random.choice(self.conf["summary"]["quotes"])

        await ctx.event.bot.send(ctx.event.message_obj.raw_message, obmsg)  # type: ignore
        ctx.event.should_call_llm(True)
        ctx.chain.clear()

        return False

    async def _step_error(self, ctx: OutContext) -> StepResult:
        econf = self.conf.get("error", {})
        emode = econf.get("mode", "ignore")

        if emode == "ignore":
            return None

        for word in econf.get("keywords", []):
            if word not in ctx.plain:
                continue

            # 获取自定义消息
            custom_msg = econf.get("custom_message", "").strip()

            if emode == "forward":
                if self.admin_id:
                    # 如果有自定义消息且是 aiocqhttp 平台，先发送给管理员，然后在原对话回复自定义消息
                    if custom_msg and isinstance(ctx.event, AiocqhttpMessageEvent):
                        try:
                            # 先将报错信息发送给管理员
                            error_msg = f"[报错拦截] 来自 {ctx.gid or ctx.uid}\n{ctx.plain}"
                            await ctx.event.bot.send_private_msg(
                                user_id=int(self.admin_id),
                                message=error_msg
                            )
                            logger.debug(f"已将报错消息转发给管理员（{self.admin_id}）私聊")
                            
                            # 在原对话回复自定义消息
                            ctx.event.set_result(ctx.event.plain_result(custom_msg))
                            logger.debug(f"已在原对话发送自定义消息: {custom_msg}")
                            return False
                        except Exception as e:
                            logger.error(f"转发报错失败: {e}")
                    
                    # 如果没有自定义消息或不是 aiocqhttp 平台，使用原逻辑（将消息转发给管理员）
                    ctx.event.message_obj.group_id = ""
                    ctx.event.message_obj.sender.user_id = self.admin_id
                    logger.debug(f"已将消息发送目标改为管理员（{self.admin_id}）私聊")
                    return False
                else:
                    logger.warning("未配置管理员ID，无法转发错误信息")
                    # 如果没有配置管理员且有自定义消息，则拦截并显示自定义消息
                    if custom_msg:
                        ctx.event.set_result(ctx.event.plain_result(custom_msg))
                        logger.warning(f"已拦截报错并发送自定义消息: {custom_msg}")
                        return False
                    # 如果没有自定义消息，不发送任何内容到原对话，直接拦截
                    logger.warning(f"已阻止发送报错提示：{ctx.plain}")
                    return False

            elif emode == "block":
                # 拦截模式：如果有自定义消息则发送，否则不发送任何内容
                if custom_msg:
                    ctx.event.set_result(ctx.event.plain_result(custom_msg))
                    logger.warning(f"已拦截报错并发送自定义消息: {custom_msg}")
                else:
                    logger.warning(f"已拦截报错（不发送消息）：{ctx.plain}")
                return False

        return None

    async def _step_dedup(self, ctx: OutContext) -> StepResult:

        if ctx.plain in ctx.group.bot_msgs:
            ctx.event.set_result(ctx.event.plain_result(""))
            logger.warning(f"已阻止重复消息: {ctx.plain}")
            return False

        if ctx.is_llm:
            ctx.group.bot_msgs.append(ctx.plain)

        return None

    async def _step_block_ai(self, ctx: OutContext) -> StepResult:
        for word in self.conf["block_ai"]["keywords"]:
            if word in ctx.plain:
                ctx.event.set_result(ctx.event.plain_result(""))
                logger.warning(f"已阻止人机话术: {ctx.plain}")
                return False

        return None

    async def _step_parse_at(self, ctx: OutContext) -> StepResult:
        self.at_policy.handle(ctx)
        return None

    async def _step_clean(self, ctx: OutContext) -> StepResult:
        cconf = self.conf["clean"]

        for seg in ctx.chain:
            if not isinstance(seg, Plain):
                continue
            if len(seg.text) >= cconf["text_threshold"]:
                continue

            if cconf["bracket"]:
                seg.text = re.sub(r"\[.*?\]", "", seg.text)
            if cconf["parenthesis"]:
                seg.text = re.sub(r"[（(].*?[）)]", "", seg.text)
            if cconf["emotion_tag"]:
                seg.text = re.sub(r"&&.*?&&", "", seg.text)
            if cconf["emoji"]:
                seg.text = emoji.replace_emoji(seg.text, replace="")
            if cconf["lead"]:
                for s in cconf["lead"]:
                    if seg.text.startswith(s):
                        seg.text = seg.text[len(s) :]
            if cconf["tail"]:
                for s in cconf["tail"]:
                    if seg.text.endswith(s):
                        seg.text = seg.text[: -len(s)]
            if cconf["punctuation"]:
                seg.text = re.sub(cconf["punctuation"], "", seg.text)

        return None

    async def _step_tts(self, ctx: OutContext) -> StepResult:
        if not isinstance(ctx.event, AiocqhttpMessageEvent):
            return None

        tconf = self.conf["tts"]
        if (
            len(ctx.chain) != 1
            or not isinstance(ctx.chain[0], Plain)
            or len(ctx.chain[0].text) >= tconf["threshold"]
            or random.random() >= tconf["prob"]
        ):
            return None

        try:
            char_id = tconf["character"].split("（", 1)[1][:-1]
            audio = await ctx.event.bot.get_ai_record(
                character=char_id,
                group_id=int(tconf["group_id"]),
                text=ctx.chain[0].text,
            )
            ctx.chain[:] = [Record.fromURL(audio)]
        except Exception as e:
            logger.error(f"TTS 失败: {e}")

        return None

    async def _step_t2i(self, ctx: OutContext) -> StepResult:
        if not self.style:
            return None

        iconf = self.conf["t2i"]

        if (
            isinstance(ctx.chain[-1], Plain)
            and len(ctx.chain[-1].text) > iconf["threshold"]
        ):
            img = await self.style.AioRender(
                text=ctx.chain[-1].text,
                useImageUrl=True,
                autoPage=iconf["auto_page"],
            )
            path = img.Save(self.image_cache_dir)
            ctx.chain[-1] = Image.fromFileSystem(str(path))

        return None

    async def _step_reply(self, ctx: OutContext) -> StepResult:
        if self.conf["reply"]["threshold"] <= 0:
            return None

        if not all(isinstance(x, Plain | Image | Face | At) for x in ctx.chain):
            return None

        msg_id = ctx.event.message_obj.message_id
        queue = ctx.group.msg_queue
        if msg_id not in queue:
            return None

        pushed = len(queue) - queue.index(msg_id) - 1
        if pushed >= self.conf["reply"]["threshold"]:
            ctx.chain.insert(0, Reply(id=msg_id))
            queue.clear()

        return None

    async def _step_forward(self, ctx: OutContext) -> StepResult:
        if not isinstance(ctx.event, AiocqhttpMessageEvent):
            return None
        if not isinstance(ctx.chain[-1], Plain):
            return None
        if len(ctx.chain[-1].text) <= self.conf["forward"]["threshold"]:
            return None

        nodes = Nodes([])
        name = await self._ensure_node_name(ctx.event)
        content = list(ctx.chain.copy())
        nodes.nodes.append(Node(uin=ctx.bid, name=name, content=content))
        ctx.chain[:] = [nodes]
        return None

    async def _step_recall(self, ctx: OutContext) -> StepResult:
        if isinstance(ctx.event, AiocqhttpMessageEvent):
            await self.recaller.send_and_recall(ctx)
        return None

    async def _step_split(self, ctx: OutContext) -> StepResult:
        await self.splitter.split(ctx)
        return None

    # ============================================================
    # Event Hooks
    # ============================================================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message(self, event: AstrMessageEvent):
        """收到群消息后"""
        gid = event.get_group_id()
        sender_id = event.get_sender_id()
        self_id = event.get_self_id()

        g = StateManager.get_group(gid)

        if self.conf["reply"]["threshold"] > 0 and sender_id != self_id:
            g.msg_queue.append(event.message_obj.message_id)

        if self.is_step_enabled("parse_at") and not self.conf["parse_at"]["at_str"]:
            name = event.get_sender_name()
            if len(g.name_to_qq) >= 100:
                g.name_to_qq.popitem(last=False)
            g.name_to_qq[name] = sender_id

    @filter.on_decorating_result(priority=15)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """发送消息前"""
        result = event.get_result()
        if not result or not result.chain:
            return
        plain = result.get_plain_text()
        gid = event.get_group_id()
        uid = event.get_sender_id()
        bid = event.get_self_id()
        group: GroupState = StateManager.get_group(gid)
        ctx = OutContext(
            event=event,
            chain=result.chain,
            is_llm=result.is_llm_result(),
            plain=plain,
            gid=gid,
            uid=uid,
            bid=bid,
            group=group,
        )

        await self.pipeline.run(ctx)
