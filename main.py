import os
import uuid
import asyncio
import aiohttp
import json
import time
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.event_message_type import EventMessageType


# 注册插件：名字、作者、描述、版本号
@register("astrbot_plugin_fishaudio_tts", "xiaoxi2760", "基于 FishAudio API 的 TTS 插件，支持多音色、代理和频率限制", "2.1.0")
class FishAudioTTS(Star):
    """
    FishAudio TTS 插件
    - 管理员通过 /tts 开关控制自然语言 TTS
    - 支持通过“音色名说 文本”将文本转为语音
    - 提供 tts_speak LLM 工具生成纯语音回复
    - 可配置多个音色，通过中文名称切换
    - 支持 HTTP 代理
    - 支持频率限制（管理员豁免）
    """

    SUPPORTED_EMOTION_TAGS = {
        "happy",
        "sad",
        "angry",
        "whisper",
        "excited",
        "neutral",
        "fearful",
        "surprised",
    }  # 官方文档确认的常用标签；其他标签也会以 [标签] 形式原样透传


    def __init__(self, context: Context, config: AstrBotConfig):
        """
        初始化插件：
        1. 从配置中读取各项参数
        2. 解析音色列表
        3. 创建临时文件目录
        4. 初始化频率限制记录字典
        """
        super().__init__(context)
        self.config = config

        # ---- 基本设置 ----
        self.api_key = config.get("api_key", "")
        self.api_base_url = config.get("api_base_url", "https://api.fish.audio")
        self.model = config.get("model", "s2.1-pro-free")
        self.proxy = config.get("proxy", "").strip() or None  # 空字符串转为 None
        self.rate_limit_seconds = config.get("rate_limit_seconds", 5)

        # ---- 可配置限制（字数、并发、重试、超时、异步轮询） ----
        self.max_chars = max(1, int(config.get("max_chars", 500) or 500))      # 字数上限，超出直接提示字数超限
        self.max_concurrent_requests = max(1, int(config.get("max_concurrent_requests", 5) or 5))
        self.max_retries = max(1, int(config.get("max_retries", 3) or 3))
        self.timeout_total = float(config.get("timeout_total", 60) or 60)
        self.timeout_connect = float(config.get("timeout_connect", 10) or 10)
        self.timeout_sock_read = float(config.get("timeout_sock_read", 30) or 30)
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=self.timeout_total,
                connect=self.timeout_connect,
                sock_read=self.timeout_sock_read,
            )
        )
        # 并发信号量：限制同时进行的 TTS 请求数（官方 Starter 套餐并发上限为 5）
        self._semaphore = asyncio.Semaphore(self.max_concurrent_requests)

        # ---- 解析音色配置（名称 -> ID 映射） ----
        self.voices = {}          # 字典：{中文名称: 音色ID}
        self.default_voice_name = None   # 默认使用的音色名称
        self._init_voices()

        # ---- 音频临时存储目录 ----
        self.output_dir = os.path.join("data", "temp", "astrbot_plugin_fishaudio_tts")
        os.makedirs(self.output_dir, exist_ok=True)
        # ---- 清理超过 24 小时的旧音频缓存，防止磁盘膨胀 ----
        now = time.time()
        for fname in os.listdir(self.output_dir):
            fpath = os.path.join(self.output_dir, fname)
            if fname == "default_voice.json":
                continue  # 默认音色状态文件不参与缓存清理
            try:
                if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > 86400:
                    os.remove(fpath)
            except OSError as e:
                logger.warning(f"清理旧音频缓存失败 {fpath}: {e}")

        # ---- 频率限制记录：记录每个用户最近一次调用的时间戳 ----
        self.user_last_call = {}  # 键：unified_msg_origin，值：时间戳（秒）

        # ---- 自然语言 TTS 开关：默认开启，重启后恢复开启 ----
        self.enabled = True

        # 长名称优先匹配用的预排序列表，避免每次匹配都重新排序
        self._voices_sorted = sorted(self.voices, key=len, reverse=True)
        # 管理员可通过「语音默认」切换的默认音色（持久化，重启后仍生效）
        self._default_voice_override = None
        self._load_default_voice_override()

    # ------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------

    def _init_voices(self):
        """
        从配置的文本框 voice_names 和 voice_ids 中解析音色。
        每行对应一个音色，名称与 ID 一一对应。
        如果用户未使用新文本框，则兼容旧版的 reference_id 配置。
        """
        names_text = self.config.get("voice_names", "").strip()
        ids_text = self.config.get("voice_ids", "").strip()

        # 如果两个新文本框都为空，尝试使用旧的 reference_id（向下兼容）
        if not names_text and not ids_text:
            old_id = self.config.get("reference_id", "").strip()
            if old_id:
                self.voices["默认音色"] = old_id
                self.default_voice_name = "默认音色"
            return

        # 按行分割，去掉空白行
        name_lines = [n.strip() for n in names_text.splitlines() if n.strip()]
        id_lines = [i.strip() for i in ids_text.splitlines() if i.strip()]

        # 名称行数和 ID 行数不一致时，按较少的一方截断，并发出警告
        if len(name_lines) != len(id_lines):
            logger.warning(f"音色名称数量({len(name_lines)})与ID数量({len(id_lines)})不一致，按较少的一方截断")
            min_len = min(len(name_lines), len(id_lines))
            name_lines = name_lines[:min_len]
            id_lines = id_lines[:min_len]

        # 构建映射字典
        for name, vid in zip(name_lines, id_lines):
            if name and vid:
                self.voices[name] = vid

        # 设置第一个音色为默认
        if self.voices:
            self.default_voice_name = list(self.voices.keys())[0]

    def _effective_default_voice_name(self) -> str | None:
        """返回当前生效的默认音色名（优先管理员切换的覆盖值）。"""
        if self._default_voice_override and self._default_voice_override in self.voices:
            return self._default_voice_override
        return self.default_voice_name

    def _default_voice_state_path(self) -> str:
        """默认音色状态文件路径。"""
        return os.path.join(self.output_dir, "default_voice.json")

    def _load_default_voice_override(self):
        """从本地状态文件恢复管理员切换的默认音色。"""
        try:
            with open(self._default_voice_state_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            name = (data or {}).get("voice_name")
            if name and name in self.voices:
                self._default_voice_override = name
        except (OSError, ValueError, TypeError):
            pass

    def _save_default_voice_override(self, voice_name: str):
        """持久化管理员切换的默认音色。"""
        try:
            with open(self._default_voice_state_path(), "w", encoding="utf-8") as f:
                json.dump({"voice_name": voice_name}, f, ensure_ascii=False)
        except OSError as e:
            logger.warning(f"保存默认音色失败：{e}")

    def _match_voice_say(self, message: str):
        """匹配“音色名说 文本”，返回音色名和待合成文本。"""
        message = (message or "").strip()
        if not message or not self.voices:
            return None

        # 长名称优先，避免配置了“爱”和“小爱”时误匹配。
        for voice_name in self._voices_sorted:
            marker = f"{voice_name}说"
            if not message.startswith(marker):
                continue

            remainder = message[len(marker):]
            if not remainder or not remainder[0].isspace():
                continue

            text = remainder.strip()
            if text:
                return voice_name, text
        return None

    @staticmethod
    def _is_tts_command(message: str) -> bool:
        """判断消息是否为 /tts 开关命令或其带参数形式。"""
        message = (message or "").strip()
        return (
            message.startswith("/tts")
            and len(message) >= 4
            and (len(message) == 4 or message[4].isspace())
        )

    @staticmethod
    def _voice_failure_message(voice_name: str) -> str:
        return f"{voice_name}说累啦，让她休息一会儿吧"

    def _help_text(self) -> str:
        """生成语音功能使用帮助文本。"""
        return "\n".join([
            "🎙️ 语音合成使用帮助",
            "",
            "【语音合成】发送：音色名说 文本",
            "例如：小爱说 今天天气真不错",
            "      小爱说 [happy]今天是个好日子",
            "",
            "【查看音色】发送：语音音色",
            "【默认音色】管理员发送：语音默认（可跟音色名，如：语音默认 小爱）",
            "【语音状态】管理员发送：语音状态",
            "【使用帮助】发送：语音帮助",
            "【功能开关】管理员发送：/tts",
            "",
            "【情感标签】[happy] [sad] [angry] [whisper] [excited] [neutral] [fearful] [surprised]",
            "其他标签不一定生效，也可尝试。",
        ])

    def _voice_list_text(self) -> str:
        """生成当前可用音色列表文本。"""
        if not self.voices:
            return "还没有配置音色，请在插件配置里填写 voice_names 和 voice_ids。"
        lines = ["🎙️ 可用音色："]
        for i, name in enumerate(self.voices, 1):
            suffix = "（默认）" if name == self._effective_default_voice_name() else ""
            lines.append(f"{i}. {name}{suffix}")
        lines.append("")
        lines.append("发送「音色名说 文本」即可用对应音色合成语音。")
        return "\n".join(lines)

    async def _check_rate_limit(self, event: AstrMessageEvent) -> bool:
        """
        检查用户是否触发频率限制。
        返回 True 表示允许调用，False 表示被限制。
        - 使用 AstrBot 官方推荐的 unified_msg_origin 作为用户唯一标识。
        - 管理员直接放行。
        - 同时清理超过 120 秒的旧记录，防止内存泄漏。
        """
        # 管理员不受限
        if event.is_admin():
            return True

        user_id = event.unified_msg_origin
        now = time.monotonic()
        last = self.user_last_call.get(user_id, 0)

        if now - last < self.rate_limit_seconds:
            return False

        # 更新记录
        self.user_last_call[user_id] = now

        # 清理过期记录（超过 120 秒未活动的）
        self.user_last_call = {
            k: v for k, v in self.user_last_call.items() if now - v < 120
        }
        return True

    async def _tts_request(self, text: str, voice_id: str, filepath: str) -> None:
        """向 FishAudio API 发送 TTS 请求（同步接口），流式写入 WAV 文件。"""
        start = time.perf_counter()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "model": self.model,            # 用户配置的模型，放在 Header
        }
        payload = {
            "text": text,
            "format": "wav",
        }
        if voice_id:
            payload["reference_id"] = voice_id

        async with self._semaphore:
            for attempt in range(self.max_retries):
                try:
                    async with self._session.post(
                        f"{self.api_base_url}/v1/tts",
                        headers=headers,
                        json=payload,
                        proxy=self.proxy,
                    ) as resp:
                        if resp.status == 200:
                            bytes_written = 0
                            with open(filepath, "wb") as f:
                                async for chunk in resp.content.iter_chunked(64 * 1024):
                                    f.write(chunk)
                                    bytes_written += len(chunk)
                            logger.info(
                                f"FishAudio TTS 合成成功：{len(text)} 字，"
                                f"{bytes_written} 字节，耗时 {time.perf_counter() - start:.2f}s"
                            )
                            return
                        error_text = (await resp.text())[:200]
                        # 429 或 5xx 可重试；其他状态码直接失败
                        if resp.status in (429, 500, 502, 503, 504) and attempt < self.max_retries - 1:
                            logger.warning(
                                f"FishAudio TTS 返回 HTTP {resp.status}，"
                                f"第 {attempt + 1}/{self.max_retries} 次重试"
                            )
                            await asyncio.sleep(min(2 ** attempt, 8))
                            continue
                        raise Exception(f"API 返回 HTTP {resp.status}: {error_text}")
                except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
                    if attempt < self.max_retries - 1:
                        logger.warning(
                            f"FishAudio TTS 网络/超时错误：{e}，"
                            f"第 {attempt + 1}/{self.max_retries} 次重试"
                        )
                        await asyncio.sleep(min(2 ** attempt, 8))
                        continue
                    raise

    def _new_audio_path(self) -> str:
        """生成随机命名的临时 WAV 文件路径。"""
        filename = f"{uuid.uuid4().hex}.wav"
        return os.path.join(self.output_dir, filename)

    # ------------------------------------------------------------
    # 自然语言触发：音色名说 <文本>
    # ------------------------------------------------------------

    @filter.event_message_type(EventMessageType.ALL)
    async def voice_say_trigger(self, event: AstrMessageEvent):
        """识别“音色名说 文本”，例如“小爱说 今天天气不错”。"""
        # 非管理员的 /tts 及其带参数形式不得继续进入默认 LLM。
        if self._is_tts_command(event.message_str) and not event.is_admin():
            event.stop_event()
            return

        # 语音帮助 / 语音音色：与小爱说同机制，前缀匹配即触发
        msg = (event.message_str or "").strip()
        for keyword in ("语音帮助", "语音音色", "语音状态", "语音默认"):
            if msg == keyword or msg.startswith(keyword + " ") or msg.startswith(keyword + "　"):
                event.stop_event()
                if keyword in ("语音状态", "语音默认") and not event.is_admin():
                    return  # 仅管理员可用
                if keyword == "语音帮助":
                    yield event.plain_result(self._help_text())
                elif keyword == "语音音色":
                    yield event.plain_result(self._voice_list_text())
                elif keyword == "语音状态":
                    yield event.plain_result(self._voice_status_text())
                else:  # 语音默认
                    voice_arg = msg[len(keyword):].strip()
                    yield event.plain_result(self._set_default_voice(voice_arg, event))
                return

        matched = self._match_voice_say(event.message_str)
        if not matched:
            return

        # 命中后立即终止事件，阻止后续插件和 LLM 继续处理此消息。
        event.stop_event()

        voice_name, text = matched
        if not self.enabled:
            return

        if not self.api_key or voice_name not in self.voices:
            yield event.plain_result(self._voice_failure_message(voice_name))
            return

        if len(text) > self.max_chars:
            yield event.plain_result(f"字数超限啦，最多 {self.max_chars} 字，当前 {len(text)} 字。")
            return

        if not await self._check_rate_limit(event):
            yield event.plain_result(self._voice_failure_message(voice_name))
            return

        try:
            filepath = self._new_audio_path()
            await self._tts_request(text, self.voices[voice_name], filepath)
            yield event.chain_result([
                Comp.Plain(f"{voice_name}: "),
                Comp.Record(file=filepath),
            ])
        except Exception as e:
            logger.error(f"FishAudio TTS 失败（音色：{voice_name}）：{e}")
            yield event.plain_result(self._voice_failure_message(voice_name))

    # ------------------------------------------------------------
    # LLM 工具：生成纯语音，不输出 text 参数中的文字
    # ------------------------------------------------------------

    @filter.llm_tool(name="tts_speak")
    async def tts_speak(
        self,
        event: AstrMessageEvent,
        text: str,
        voice_name: str | None = None,
        emotion: str | None = None,
    ):
        """
让 AI 用语音和用户说话，这是插件提供的“开口说话”能力。

当用户要求你说话、朗读、念台词、用声音回复时，必须调用本工具。
在角色扮演、陪伴聊天中，只要你想开口说话、表达情绪或互动，就优先调用本工具生成语音，而不是只发文字。
当回复带有强烈情绪（开心、生气、撒娇、困倦、惊讶等）时，适合用语音表达。
客观知识问答和普通闲聊不需要每次调用。

emotion 支持 happy、sad、angry、whisper、excited、neutral、fearful、surprised 等标签；
其他情绪标签也会以 [标签] 形式原样拼接到文本开头透传给 FishAudio，不会导致调用失败。

调用本工具后，仍必须输出一小段文字回复（例如语气词、旁白，或对语音内容的简短回应/总结），不要只发语音不说话；只是不要把 text 参数的内容整段重复成文字。

Args:
    text(string): 需要朗读的完整内容。该内容会作为音频发送；文字回复中不要整段重复它，但可以简短提及或回应。
    voice_name(string): 可选的已配置音色名称。用户明确指定音色时填写，否则使用配置列表中的第一个音色。
    emotion(string): 可选的情绪名称，例如 happy、sad、angry、whisper、excited、neutral、fearful、surprised。插件会自动添加对应的 [emotion] 标签并透传给 FishAudio。
"""
        if self._is_tts_command(event.message_str):
            return
        if not self.enabled or not self.api_key or not self.voices:
            return

        text = (text or "").strip()
        if not text:
            yield event.plain_result("🎤")
            return

        if len(text) > self.max_chars:
            yield event.plain_result(f"字数超限啦，最多 {self.max_chars} 字，当前 {len(text)} 字。")
            return

        if emotion:
            emotion_name = emotion.strip().lower().strip("[]")
            if emotion_name:
                if emotion_name not in self.SUPPORTED_EMOTION_TAGS:
                    logger.warning(
                        f"LLM 请求的情绪标签未在官方列表中，仍将原样透传：{emotion_name}"
                    )
                emotion_tag = f"[{emotion_name}]"
                if not text.lower().startswith(emotion_tag):
                    text = f"{emotion_tag}{text}"

        used_voice_name = self._effective_default_voice_name()
        if voice_name:
            requested_voice = voice_name.strip()
            if requested_voice in self.voices:
                used_voice_name = requested_voice
            else:
                logger.warning(f"LLM 请求的音色不存在：{requested_voice}")
                yield event.plain_result(self._voice_failure_message(requested_voice))
                return

        if not used_voice_name:
            return
        if not await self._check_rate_limit(event):
            yield event.plain_result(self._voice_failure_message(used_voice_name))
            return

        try:
            filepath = self._new_audio_path()
            await self._tts_request(text, self.voices[used_voice_name], filepath)

            logger.info(
                f"FishAudio LLM TTS 成功：{os.path.getsize(filepath)} 字节 -> {filepath}"
                f"（音色：{used_voice_name}）"
            )

            # 语音附带简短文字标记，避免主消息链只剩 Reply/At 时被框架跳过。
            # 该标记不包含朗读内容本身，不会把 text 重复输出为普通文字。
            yield event.chain_result([
                Comp.Plain("🎤"),
                Comp.Record(file=filepath),
            ])
        except Exception as e:
            logger.error(f"FishAudio LLM TTS 失败（音色：{used_voice_name}）：{e}")
            yield event.plain_result(self._voice_failure_message(used_voice_name))

    # ------------------------------------------------------------
    # 提高 LLM 调用语音工具的积极性：请求前注入系统提示
    # ------------------------------------------------------------

    @filter.on_llm_request()
    async def on_llm_request_boost(self, event: AstrMessageEvent, req: ProviderRequest):
        """在 LLM 请求发出前注入语音工具使用提示，提高 tts_speak 调用积极性。"""
        if not self.enabled or not self.api_key or not self.voices:
            return
        marker = "[FishAudio TTS 工具提醒]"
        if marker in (req.system_prompt or ""):
            return
        voice_names = "、".join(self.voices.keys())
        req.system_prompt += (
            f"\n\n{marker}\n"
            "你现在拥有语音合成能力：可以调用 tts_speak 工具，把想说的话合成语音直接发送。\n"
            "以下情况请主动调用 tts_speak，用语音开口说话：\n"
            "1. 用户要求你说话、朗读、唱歌或念台词时；\n"
            "2. 角色扮演或日常互动中你想开口说话、表达情绪时，优先用语音而不是纯文字；\n"
            "3. 回复带有强烈情绪（开心、生气、撒娇、困倦、惊讶等）时，适合用语音表达。\n"
            "4. 只要调用了 tts_speak，就必须同时输出一小段文字回复（语气词、旁白或简短回应），不能只发语音不说话。\n"
            "普通客观问答可以不用调用。\n"
            f"当前可用的音色名称：{voice_names}。用户明确指定音色时用 voice_name 参数传入，否则用默认音色。\n"
            "emotion 参数可传 happy、sad、angry、whisper、excited、neutral、fearful、surprised 等标签，其他情绪标签也会原样透传。\n"
            "调用 tts_speak 后，仍必须输出一小段文字回复（例如语气词、旁白或简短回应），不能只发语音不说话；只是不要把 text 参数的内容整段重复成文字。"
        )

    # ------------------------------------------------------------
    # 管理员开关：/tts
    # ------------------------------------------------------------

    @filter.command("tts")
    async def tts_toggle(self, event: AstrMessageEvent):
        """管理员切换自然语言 TTS 开关。"""
        event.stop_event()
        if not event.is_admin():
            return

        self.enabled = not self.enabled
        if self.enabled:
            yield event.plain_result("想听我说什么呀？")
        else:
            yield event.plain_result("喔，那我闭嘴咯。")

    def _voice_status_text(self) -> str:
        """生成语音功能状态文本。"""
        lines = [
            "🎙️ 语音功能状态",
            f"功能开关：{'开启' if self.enabled else '关闭'}",
            f"默认音色：{self._effective_default_voice_name() or '未配置'}",
            f"可用音色：{len(self.voices)} 个",
            f"并发上限：{self.max_concurrent_requests}",
            f"失败重试：{self.max_retries} 次",
            f"调用间隔限制：{self.rate_limit_seconds} 秒" if self.rate_limit_seconds else "调用间隔限制：不限制",
            f"API 地址：{self.api_base_url}",
            f"模型：{self.model or '默认'}",
            f"API Key：{'已配置' if self.api_key else '未配置'}",
        ]
        return "\n".join(lines)

    def _set_default_voice(self, voice_arg: str, event: AstrMessageEvent) -> str:
        """处理「语音默认」指令（仅管理员）：无参数时显示当前默认。"""
        if not event.is_admin():
            return "仅管理员可用该指令。"
        if not voice_arg:
            current = self._effective_default_voice_name() or "未配置"
            return f"当前默认音色：{current}\n发送「语音默认 音色名」可切换。"
        if voice_arg not in self.voices:
            return f"音色「{voice_arg}」不存在，可用「语音音色」查看。"
        self._default_voice_override = voice_arg
        self._save_default_voice_override(voice_arg)
        logger.info(f"管理员已将默认音色切换为：{voice_arg}")
        return f"已把默认音色切换为：{voice_arg}"

    @filter.command("语音帮助")
    async def voice_help_cmd(self, event: AstrMessageEvent):
        """语音功能使用帮助。"""
        event.stop_event()
        yield event.plain_result(self._help_text())

    @filter.command("语音音色")
    async def voice_list_cmd(self, event: AstrMessageEvent):
        """查看当前可用音色。"""
        event.stop_event()
        yield event.plain_result(self._voice_list_text())

    @filter.command("语音默认")
    async def voice_default_cmd(self, event: AstrMessageEvent):
        """查看/切换默认音色（仅管理员）。"""
        event.stop_event()
        if not event.is_admin():
            return
        msg = (event.message_str or "").strip()
        arg = msg
        for prefix in ("/语音默认", "语音默认"):
            if arg.startswith(prefix):
                arg = arg[len(prefix):].strip()
                break
        yield event.plain_result(self._set_default_voice(arg, event))

    @filter.command("语音状态")
    async def voice_status_cmd(self, event: AstrMessageEvent):
        """查看语音功能状态（仅管理员）。"""
        event.stop_event()
        if not event.is_admin():
            return
        yield event.plain_result(self._voice_status_text())

    async def terminate(self):
        """插件卸载时关闭 aiohttp 会话，避免资源泄漏。"""
        if self._session and not self._session.closed:
            await self._session.close()
