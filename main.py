import random
import re
import shutil
from pathlib import Path

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
from .core.recall import Recaller
from .core.split import MessageSplitter
from .core.state import GroupState, StateManager


class OutputPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config

        self.data_dir = StarTools.get_data_dir("astrbot_plugin_outputpro")
        self.image_cache_dir = self.data_dir / "image_cache"
        self.image_cache_dir.mkdir(parents=True, exist_ok=True)

        # bot管理员(仅取第一位)
        admins_id: list[str] = context.get_config().get("admins_id", [])
        self.admin_id: str | None = admins_id[0] if admins_id else None

        self.at_policy = AtPolicy(self.conf)

        # 分段回复器
        self.splitter = MessageSplitter(context, config)

        self.style = None

    # ================= 生命周期 ================
    async def initialize(self):
        # 撤回器
        self.recaller = Recaller(self.conf)
        # 渲染器
        if self.conf["t2i"]["enable"]:
            try:
                import pillowmd

                style_path = Path(self.conf["t2i"]["pillowmd_style_dir"]).resolve()
                self.style = pillowmd.LoadMarkdownStyles(style_path)
            except ImportError as e:
                logger.error(
                    f"无法加载 pillowmd：未安装 pillowmd 包。请先安装依赖，例如：pip install pillowmd : {e}"
                )
            except Exception as e:
                logger.error(f"无法加载 pillowmd 样式：{e}")

    async def terminate(self):
        # 终止撤回器
        await self.recaller.terminate()
        # 清空缓存目录
        if (
            self.conf["t2i"]["clean_cache"]
            and self.image_cache_dir
            and self.image_cache_dir.exists()
        ):
            try:
                shutil.rmtree(self.image_cache_dir)
                logger.debug(f"缓存已清空：{self.image_cache_dir}")
            except Exception as e:
                logger.error(f"清空缓存失败：{e}")
            self.image_cache_dir.mkdir(parents=True, exist_ok=True)

    async def _ensure_node_name(self, event: AstrMessageEvent) -> str:
        """确保转发节点昵称不为空"""
        fconf = self.conf["forward"]
        if fconf.get("node_name"):
            return fconf["node_name"]

        new_name = "AstrBot"

        if isinstance(event, AiocqhttpMessageEvent):
            try:
                login_data = await event.bot.get_login_info()
                print(login_data)
                nickname = login_data.get("nickname")
                if nickname:
                    new_name = str(nickname)
            except Exception:
                pass

        fconf["node_name"] = new_name
        self.conf.save_config()

        return new_name

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message(self, event: AstrMessageEvent):
        """接收消息后的处理"""
        if event.get_extra("splitted_event"):
            return

        gid: str = event.get_group_id()
        sender_id = event.get_sender_id()
        self_id = event.get_self_id()

        # 缓存最新消息ID
        g: GroupState = StateManager.get_group(gid)
        if self.conf["reply_threshold"] and sender_id != self_id:
            g.after_bot_count += 1

        # 缓存 “昵称 -> QQ”, 为解析假艾特提供映射
        if self.conf["parse_at"]["enable"]:
            cache_name_num = 100  # 缓存数量默认100
            sender_name = event.get_sender_name()
            if len(g.name_to_qq) >= cache_name_num:
                g.name_to_qq.popitem(last=False)  # FIFO 头删
            g.name_to_qq[sender_name] = sender_id

    @filter.on_decorating_result(priority=15)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """发送消息前的预处理"""
        # 过滤空消息
        result = event.get_result()
        if not result:
            return
        chain = result.chain
        if not chain:
            return

        # 图片外显
        if (
            isinstance(event, AiocqhttpMessageEvent)
            and self.conf["summary"]["enable"]
            and len(chain) == 1
            and isinstance(chain[0], Image)
        ):
            obmsg: list[dict] = await event._parse_onebot_json(MessageChain(chain))
            obmsg[0]["data"]["summary"] = random.choice(self.conf["summary"]["quotes"])
            raw = event.message_obj.raw_message
            await event.bot.send(raw, obmsg)  # type: ignore
            event.should_call_llm(True)
            chain.clear()
            return

        msg = result.get_plain_text()

        # 拦截错误信息
        econf = self.conf["error"]
        emode = econf["mode"]
        if emode != "ignore":
            for word in econf["keywords"]:
                if word in msg:
                    if emode == "forward":
                        if self.admin_id:
                            event.message_obj.group_id = ""
                            event.message_obj.sender.user_id = self.admin_id
                            logger.debug(
                                f"已将消息发送目标改为管理员（{self.admin_id}）私聊"
                            )
                        else:
                            logger.warning("未配置管理员ID，无法转发错误信息")
                    elif emode == "block":
                        event.set_result(event.plain_result(""))
                        logger.info(f"已阻止发送报错提示：{msg}")
                        return

        gid: str = event.get_group_id()
        g: GroupState = StateManager.get_group(gid)

        # 是否允许执行“LLM 专属逻辑”
        allow_llm_logic = not self.conf["only_llm_result"] or result.is_llm_result()

        if allow_llm_logic:
            tconf = self.conf["toobot"]
            # 拦截重复消息
            if tconf["block_reread"] and msg in g.bot_msgs:
                event.set_result(event.plain_result(""))
                logger.info(f"已阻止LLM发送重复消息：{msg}")
                return
            if result.is_llm_result():
                g.bot_msgs.append(msg)

            # 拦截人机发言
            if tconf["block_ai"]:
                for word in tconf["keywords"]:
                    if word in msg:
                        event.set_result(event.plain_result(""))
                        logger.info(f"已阻止LLM过于人机的发言:{msg}")
                        return

            # 解析 At 消息 + 概率At
            if self.conf["parse_at"]["enable"]:
                self.at_policy.handle(event, chain, g)

            # 清洗文本消息
            cconf = self.conf["clean"]
            for seg in chain:
                if isinstance(seg, Plain) and len(seg.text) < cconf["text_threshold"]:
                    # 摘除中括号内容
                    if cconf["bracket"]:
                        seg.text = re.sub(r"\[.*?\]", "", seg.text)
                    # 摘除小括号内容（半角/全角）
                    if cconf["parenthesis"]:
                        seg.text = re.sub(r"[（(].*?[）)]", "", seg.text)
                    # 摘除情绪标签
                    if cconf["emotion_tag"]:
                        seg.text = re.sub(r"&&.*?&&", "", seg.text)
                    # 清洗emoji
                    if cconf["emoji"]:
                        seg.text = emoji.replace_emoji(seg.text, replace="")
                    # 去除指定开头字符
                    if cconf["lead"]:
                        for remove_lead in cconf["lead"]:
                            if seg.text.startswith(remove_lead):
                                seg.text = seg.text[len(remove_lead) :]
                    # 去除指定结尾字符
                    if cconf["tail"]:
                        for remove_tail in cconf["tail"]:
                            if seg.text.endswith(remove_tail):
                                seg.text = seg.text[: -len(remove_tail)]
                    # 整体清洗标点符号
                    if cconf["punctuation"]:
                        seg.text = re.sub(cconf["punctuation"], "", seg.text)

            # 文转语音
            if (
                isinstance(event, AiocqhttpMessageEvent)
                and self.conf["tts"]["enable"]
                and self.conf["tts"]["group_id"]
                and len(chain) == 1
                and isinstance(chain[0], Plain)
                and len(chain[0].text) < self.conf["tts"]["threshold"]
                and random.random() < self.conf["tts"]["prob"]
            ):
                try:
                    character_id = self.conf["tts"]["character"].split("（", 1)[1][:-1]
                    group_id = int(self.conf["tts"]["group_id"])
                    text = chain[0].text
                    logger.debug(f"正在使用角色{character_id}生成语音：{text}")
                    audio_path = await event.bot.get_ai_record(
                        character=character_id,
                        group_id=group_id,
                        text=text,
                    )
                    chain[:] = [Record.fromURL(audio_path)]
                except Exception as e:
                    logger.error(f"语音生成失败：{e}")

        # 文转图
        iconf = self.conf["t2i"]
        if iconf["enable"] and isinstance(chain[-1], Plain) and self.style:
            seg = chain[-1]
            if len(seg.text) > iconf["threshold"]:
                img = await self.style.AioRender(
                    text=seg.text, useImageUrl=True, autoPage=iconf["auto_page"]
                )
                img_path = img.Save(self.image_cache_dir)
                chain[-1] = Image.fromFileSystem(str(img_path))

        # 智能引用
        if (
            all(isinstance(seg, Plain | Image | Face | At) for seg in chain)
            and self.conf["reply_threshold"] > 0
        ):
            # 当前事件也会使 g.after_bot_count 加 1，这里用  -1 表示只统计之前的消息
            if g.after_bot_count - 1 >= self.conf["reply_threshold"]:
                chain.insert(0, Reply(id=event.message_obj.message_id))
                logger.debug("已插入Reply组件")
            # 重置计数器
            g.after_bot_count = 0

        # 自动转发
        if (
            isinstance(event, AiocqhttpMessageEvent)
            and self.conf["forward"]["enable"]
            and isinstance(chain[-1], Plain)
        ):
            seg = chain[-1]
            if len(seg.text) > self.conf["forward"]["threshold"]:
                nodes = Nodes([])
                self_id = event.get_self_id()
                name = await self._ensure_node_name(event)
                for seg in chain:
                    nodes.nodes.append(Node(uin=self_id, name=name, content=[seg]))
                chain[:] = [nodes]

        # 自动撤回
        if isinstance(event, AiocqhttpMessageEvent) and self.conf["recall"]["enable"]:
            await self.recaller.send_and_recall(event)

        # 分段回复
        if self.conf["split"]["enable"]:
            event.set_extra("splitted_event", True)
            umo = event.unified_msg_origin
            await self.splitter.split(umo, chain)
