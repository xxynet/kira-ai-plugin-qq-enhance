import asyncio
import json
import random
from core.plugin import BasePlugin, PluginContext, logger, on, Priority, register
from core.chat.message_utils import KiraMessageBatchEvent, KiraMessageEvent
from core.chat.message_elements import Text, Sticker, Reply
from core.chat import MessageChain, Session
from core.provider import LLMRequest, LLMResponse
from core.utils.tool_utils import BaseTool


# ==================== 原有工具类（贴表情、点赞、撤回、禁言） ====================
class SetEmojiTool(BaseTool):
    name = "set_qq_emoji"
    description = "给QQ消息贴表情"
    parameters = {
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "description": "QQ消息ID"},
            "emoji_id": {"type": "string", "description": "表情ID，和<emoji>标签的表情ID相同"}
        },
        "required": ["message_id", "emoji_id"]
    }

    def __init__(self, ctx: PluginContext):
        super().__init__(ctx=ctx)

    async def execute(self, event: KiraMessageBatchEvent, *args, message_id: str, emoji_id: str, **kwargs) -> str:
        ada_name = event.session.adapter_name
        ada = self.ctx.adapter_mgr.get_adapter(ada_name)
        client = ada.get_client()
        params = {
            "message_id": message_id,
            "emoji_id": emoji_id
        }
        res = await client.send_action("set_msg_emoji_like", params)
        return res


class SendQQLikesTool(BaseTool):
    name = "send_qq_likes"
    description = "给QQ用户资料卡点赞"
    parameters = {
        "type": "object",
        "properties": {
            "qq": {"type": "string", "description": "QQ账号"},
            "times": {"type": "integer", "description": "点赞次数，默认为最大可点赞数，除非用户要求，否则无需改动"}
        },
        "required": ["qq"]
    }

    def __init__(self, ctx: PluginContext):
        super().__init__(ctx=ctx)

    async def execute(self, event: KiraMessageBatchEvent, *args, qq: str, times: int = 50, **kwargs) -> str:
        ada_name = event.session.adapter_name
        ada = self.ctx.adapter_mgr.get_adapter(ada_name)
        client = ada.get_client()
        if not client:
            return "点赞失败，未找到当前QQ适配器客户端"

        chunks = [10] * (times // 10) + ([times % 10] if times % 10 else [])
        state = {"likes_count": 0, "fail_msg": ""}
        try:
            await asyncio.wait_for(self._do_send_likes(client, qq, chunks, state), timeout=15)
        except asyncio.TimeoutError:
            return "点赞超时" + (f"（已点赞 {state['likes_count']} 次）" if state['likes_count'] else "")
        if state["fail_msg"]:
            return f"点赞失败：{state['fail_msg']}" + (f"（已点赞 {state['likes_count']} 次）" if state['likes_count'] else "")
        return f"点赞成功，点了 {state['likes_count']} 个赞"

    @staticmethod
    async def _do_send_likes(client, qq: str, chunks: list[int], state: dict) -> None:
        for chunk in chunks:
            resp = await client.send_action("send_like", {"user_id": qq, "times": chunk})
            if resp.get("status") != "ok":
                state["fail_msg"] = resp.get("message", "未知错误")
                return
            state["likes_count"] += chunk
            await asyncio.sleep(0.1)


class DeleteMsgTool(BaseTool):
    name = "delete_qq_msg"
    description = "撤回QQ消息"
    parameters = {
        "type": "object",
        "properties": {
            "message_id": {"type": "string", "description": "要撤回的QQ消息ID"},
        },
        "required": ["message_id"]
    }

    def __init__(self, ctx: PluginContext):
        super().__init__(ctx=ctx)

    async def execute(self, event: KiraMessageBatchEvent, *args, message_id: str, **kwargs) -> str:
        ada_name = event.session.adapter_name
        ada = self.ctx.adapter_mgr.get_adapter(ada_name)
        client = ada.get_client()
        params = {
            "message_id": message_id,
        }
        res = await client.send_action("delete_msg", params)
        return res


class GroupBanTool(BaseTool):
    name = "set_qq_group_ban"
    description = "禁言QQ群中的成员"
    parameters = {
        "type": "object",
        "properties": {
            "user_id": {"type": "string", "description": "要禁言的成员的QQ号"},
            "duration": {"type": "string", "description": "禁言时长（秒），默认600秒（10分钟），设为0秒即为解除禁言"},
        },
        "required": ["user_id", "duration"]
    }

    def __init__(self, ctx: PluginContext):
        super().__init__(ctx=ctx)

    async def execute(self, event: KiraMessageBatchEvent, *args, user_id: str, duration: str, **kwargs) -> str:
        ada_name = event.session.adapter_name
        ada = self.ctx.adapter_mgr.get_adapter(ada_name)
        client = ada.get_client()
        params = {
            "group_id": event.session.sid,
            "user_id": user_id,
            "duration": duration or 600
        }
        res = await client.send_action("set_group_ban", params)
        return res


# ==================== 主插件类（合并所有功能） ====================
class QQEnhancePlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)

        # ----- 原有配置 -----
        self.emoji_react_enabled = self.plugin_cfg.get("emoji_react_enabled", True)
        self.send_likes_enabled = self.plugin_cfg.get("send_likes_enabled", False)
        self.delete_msg_enabled = self.plugin_cfg.get("delete_msg_enabled", True)
        self.group_ban_enabled = self.plugin_cfg.get("group_ban_enabled", True)
        self.qq_enhance_prompt = self.plugin_cfg.get("qq_enhance_prompt", "")
        self.perceive_group_ban = self.plugin_cfg.get("perceive_group_ban", True)
        self.perceive_group_increase = self.plugin_cfg.get("perceive_group_increase", True)

        # ----- Sticker Control 配置 -----
        self.sticker_control_enabled = self.plugin_cfg.get("sticker_control_enabled", False)
        self.sticker_probability = float(self.plugin_cfg.get("sticker_probability", 0.5))
        self.random_position = bool(self.plugin_cfg.get("random_position", True))

        # ----- Typing Indicator 配置 -----
        self.typing_indicator_enabled = self.plugin_cfg.get("typing_indicator_enabled", True)
        self.typing_delay_seconds = float(self.plugin_cfg.get("typing_delay_seconds", 2.0))
        self.typing_interval_seconds = float(self.plugin_cfg.get("typing_interval_seconds", 2.0))

        # 用于 Typing Indicator 的状态管理
        self._delay_tasks = {}
        self._loop_tasks = {}
        self._typing_running = {}

    async def initialize(self):
        logger.info(f"QQEnhancePlugin initialized: "
                    f"sticker_control={self.sticker_control_enabled}, "
                    f"typing_indicator={self.typing_indicator_enabled}")

    async def terminate(self):
        # 清理 Typing Indicator 任务
        for task in self._delay_tasks.values():
            if not task.done():
                task.cancel()
        for task in self._loop_tasks.values():
            if not task.done():
                task.cancel()
        self._delay_tasks.clear()
        self._loop_tasks.clear()
        self._typing_running.clear()

    @on.im_message(priority=Priority.HIGH + 1)
    async def perceive_notice(self, event: KiraMessageEvent):
        if event.adapter.platform != "QQ":
            return
        if not event.is_notice:
            return

        msg = event.message.raw_message
        if not isinstance(msg, dict):
            return
        message_chain = event.message.chain

        notice_type = msg.get("notice_type")
        sub_type = msg.get("sub_type")
        self_id = msg.get("self_id")
        user_id = msg.get("user_id")
        target_id = msg.get("target_id")
        group_id = msg.get("group_id")

        if notice_type == "group_ban" and self_id == user_id and self.perceive_group_ban:
            event.message.is_mentioned = True

            ban_duration = msg.get("duration")
            ban_operator_id = msg.get("operator_id")
            ban_group_id = msg.get("group_id")
            if sub_type == "ban":
                message_chain.text(f"[System 用户{ban_operator_id}禁言了你{ban_duration}秒]")

            elif sub_type == "lift_ban":  # 人为解除禁言
                # ban_duration 永远是0，invalid
                message_chain.text(f"[System 你之前被禁言了，用户{ban_operator_id}解除了你的禁言]")
            else:
                return

        # --------- 新成员进群 ---------
        elif notice_type == "group_increase" and self.perceive_group_increase:
            # and msg["sub_type"] == "approve"
            if not group_id:
                return

            event.message.is_mentioned = True

            message_chain.text(f"[System 用户{user_id}加入了群聊]")
        else:
            pass

    # ---------- 注入工具 ----------
    @on.llm_request()
    async def inject_qq_enhance_tools(self, event: KiraMessageBatchEvent, req: LLMRequest, *_):
        platform = event.adapter.platform
        if not platform == "QQ":
            return

        if self.emoji_react_enabled:
            req.tool_set.add(SetEmojiTool(ctx=self.ctx))
            # 注入工具说明到 system prompt
            for p in req.system_prompt:
                if p.name == "tools":
                    p.content += f"\n{self.qq_enhance_prompt}"
                    break

        if self.send_likes_enabled:
            req.tool_set.add(SendQQLikesTool(ctx=self.ctx))

        if self.delete_msg_enabled:
            req.tool_set.add(DeleteMsgTool(ctx=self.ctx))

        if self.group_ban_enabled and event.session.session_type == "gm":
            req.tool_set.add(GroupBanTool(ctx=self.ctx))

    # ---------- Sticker Control 功能 ----------
    @on.after_xml_parse(priority=Priority.HIGH)
    async def process_stickers(self, event: KiraMessageBatchEvent, message_chains: list):
        if not self.sticker_control_enabled:
            return
        if not message_chains:
            return

        new_chains = []

        for chain in message_chains:
            elements = chain.message_list
            # 找出所有表情的位置
            sticker_indices = [i for i, e in enumerate(elements) if isinstance(e, Sticker)]

            if not sticker_indices:
                # 没有表情，直接保留原链
                new_chains.append(chain)
                continue

            # 1. 按概率决定哪些表情保留
            keep_indices = []
            for idx in sticker_indices:
                if random.random() < self.sticker_probability:
                    keep_indices.append(idx)
                else:
                    logger.debug(f"删除 sticker: {elements[idx]}")

            # 2. 原链中移除所有表情（无论是否保留），剩下的元素组成一个新链
            remaining_elements = [e for i, e in enumerate(elements) if i not in sticker_indices]

            # 3. 处理剩余链：如果它只包含一个 Reply（即空引用），则丢弃；否则加入
            if remaining_elements:
                if len(remaining_elements) == 1 and isinstance(remaining_elements[0], Reply):
                    logger.debug(f"丢弃只有引用的消息块: {remaining_elements[0]}")
                else:
                    new_chains.append(MessageChain(remaining_elements))

            # 4. 每个保留的表情单独成为一个消息链（独立成行）
            for idx in keep_indices:
                new_chains.append(MessageChain([elements[idx]]))

        # 5. 随机调整表情链的位置（如果启用）
        if self.random_position:
            # 分离出表情链和非表情链
            non_sticker_chains = [c for c in new_chains if not (len(c.message_list) == 1 and isinstance(c.message_list[0], Sticker))]
            sticker_chains = [c for c in new_chains if len(c.message_list) == 1 and isinstance(c.message_list[0], Sticker)]
            new_chains = non_sticker_chains.copy()
            for sc in sticker_chains:
                pos = random.randint(0, len(new_chains))
                new_chains.insert(pos, sc)
        # 如果未启用随机位置，表情链会按顺序放在最后（默认行为）

        message_chains.clear()
        message_chains.extend(new_chains)
        logger.debug(f"Sticker 处理完成，消息块数量: {len(message_chains)}")

    # ---------- Typing Indicator 功能 ----------
    async def _send_typing(self, session: Session):
        if not self.typing_indicator_enabled:
            return

        # 群聊不发送
        if session.session_type == "gm":
            return

        adapter = self.ctx.adapter_mgr.get_adapter(session.adapter_name)
        if not adapter:
            logger.error(f"Adapter '{session.adapter_name}' not found")
            return

        client = adapter.get_client()
        if not client:
            logger.error("Adapter client not available")
            return

        params = {"user_id": int(session.session_id), "event_type": 1}
        action = "set_input_status"

        if hasattr(client, 'send_action') and callable(client.send_action):
            try:
                await client.send_action(action, params)
                logger.debug(f"Typing sent to {session.sid}")
                return
            except Exception as e:
                logger.debug(f"send_action failed: {e}")

        # 尝试 WebSocket 发送
        ws = getattr(client, 'ws', None)
        if ws and hasattr(ws, 'send'):
            payload = json.dumps({"action": action, "params": params})
            try:
                await ws.send(payload)
                logger.debug(f"Typing sent via WebSocket to {session.sid}")
                return
            except Exception as e:
                logger.debug(f"WebSocket send failed: {e}")

        # 尝试其他可能属性
        for attr in ['_ws', '_client', 'websocket']:
            ws_attr = getattr(client, attr, None)
            if ws_attr and hasattr(ws_attr, 'send'):
                payload = json.dumps({"action": action, "params": params})
                try:
                    await ws_attr.send(payload)
                    logger.debug(f"Typing sent via {attr} to {session.sid}")
                    return
                except Exception:
                    continue

        logger.error("No working method to send typing indicator")

    async def _delayed_send_typing(self, session_obj: Session, delay: float):
        session = session_obj.sid
        try:
            await asyncio.sleep(delay)
            await self._send_typing(session_obj)
            if session not in self._loop_tasks or self._loop_tasks[session].done():
                self._typing_running[session] = True
                task = asyncio.create_task(self._typing_loop(session_obj))
                self._loop_tasks[session] = task
        except asyncio.CancelledError:
            logger.debug(f"Typing delayed task cancelled for {session}")

    async def _typing_loop(self, session_obj: Session):
        session = session_obj.sid
        while self._typing_running.get(session, False):
            try:
                await asyncio.sleep(self.typing_interval_seconds)
                if self._typing_running.get(session, False):
                    await self._send_typing(session_obj)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Typing loop error for {session}: {e}")
        logger.debug(f"Typing loop stopped for {session}")

    def _stop_typing_loop(self, session_obj: Session):
        session = session_obj.sid
        if session in self._typing_running:
            self._typing_running[session] = False
        if session in self._loop_tasks and not self._loop_tasks[session].done():
            self._loop_tasks[session].cancel()
        self._loop_tasks.pop(session, None)
        self._typing_running.pop(session, None)

    @on.im_batch_message(priority=Priority.HIGH)
    async def handle_typing_indication(self, event: KiraMessageBatchEvent):
        if not self.typing_indicator_enabled:
            return
        if event.adapter.platform != "QQ":
            return
        # 只处理私聊
        if event.is_group_message():
            return
        sid = event.session.sid

        self._stop_typing_loop(event.session)

        if sid in self._delay_tasks and not self._delay_tasks[sid].done():
            self._delay_tasks[sid].cancel()

        task = asyncio.create_task(self._delayed_send_typing(event.session, self.typing_delay_seconds))
        self._delay_tasks[sid] = task
        task.add_done_callback(lambda t: self._delay_tasks.pop(sid, None))

    @on.llm_response(priority=Priority.HIGH)
    async def on_llm_response(self, event: KiraMessageBatchEvent, resp: LLMResponse):
        if not self.typing_indicator_enabled:
            return
        if event.adapter.platform != "QQ":
            return
        # 只处理私聊
        if event.is_group_message():
            return
        sid = event.sid
        if not resp.tool_calls:
            self._stop_typing_loop(event.session)
            logger.debug(f"Stopped typing loop for {sid} due to final response (no tool calls)")
