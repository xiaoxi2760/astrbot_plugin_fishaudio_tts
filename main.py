import os
import uuid
import asyncio
import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.event_message_type import EventMessageType


# 娉ㄥ唽鎻掍欢锛氬悕瀛椼€佷綔鑰呫€佹弿杩般€佺増鏈彿
@register("astrbot_plugin_fishaudio_tts", "YourName", "鍩轰簬 FishAudio API 鐨?TTS 鎻掍欢锛屾敮鎸佸闊宠壊銆佷唬鐞嗗拰棰戠巼闄愬埗", "2.0.0")
class FishAudioTTS(Star):
    """
    FishAudio TTS 鎻掍欢
    - 绠＄悊鍛橀€氳繃 /tts 寮€鍏虫帶鍒惰嚜鐒惰瑷€ TTS
    - 鏀寔閫氳繃鈥滈煶鑹插悕璇?鏂囨湰鈥濆皢鏂囨湰杞负璇煶
    - 鎻愪緵 tts_speak LLM 宸ュ叿鐢熸垚绾闊冲洖澶?    - 鍙厤缃涓煶鑹诧紝閫氳繃涓枃鍚嶇О鍒囨崲
    - 鏀寔 HTTP 浠ｇ悊
    - 鏀寔棰戠巼闄愬埗锛堢鐞嗗憳璞佸厤锛?    """

    SUPPORTED_EMOTION_TAGS = {
        "happy",
        "sad",
        "angry",
        "whisper",
        "excited",
        "neutral",
        "fearful",
        "surprised",
    }  # 瀹樻柟鏂囨。纭鐨勫父鐢ㄦ爣绛撅紱鍏朵粬鏍囩涔熶細浠?[鏍囩] 褰㈠紡鍘熸牱閫忎紶

    def __init__(self, context: Context, config: AstrBotConfig):
        """
        鍒濆鍖栨彃浠讹細
        1. 浠庨厤缃腑璇诲彇鍚勯」鍙傛暟
        2. 瑙ｆ瀽闊宠壊鍒楄〃
        3. 鍒涘缓涓存椂鏂囦欢鐩綍
        4. 鍒濆鍖栭鐜囬檺鍒惰褰曞瓧鍏?        """
        super().__init__(context)
        self.config = config

        # ---- 鍩烘湰璁剧疆 ----
        self.api_key = config.get("api_key", "")
        self.api_base_url = config.get("api_base_url", "https://api.fish.audio")
        self.model = config.get("model", "s2.1-pro-free")
        self.proxy = config.get("proxy", "").strip() or None  # 绌哄瓧绗︿覆杞负 None
        self.rate_limit_seconds = config.get("rate_limit_seconds", 5)

        # ---- 瑙ｆ瀽闊宠壊閰嶇疆锛堝悕绉?-> ID 鏄犲皠锛?----
        self.voices = {}          # 瀛楀吀锛歿涓枃鍚嶇О: 闊宠壊ID}
        self.default_voice_name = None   # 榛樿浣跨敤鐨勯煶鑹插悕绉?        self._init_voices()

        # ---- 闊抽涓存椂瀛樺偍鐩綍 ----
        self.output_dir = os.path.join("data", "temp", "astrbot_plugin_fishaudio_tts")
        os.makedirs(self.output_dir, exist_ok=True)

        # ---- 棰戠巼闄愬埗璁板綍锛氳褰曟瘡涓敤鎴锋渶杩戜竴娆¤皟鐢ㄧ殑鏃堕棿鎴?----
        self.user_last_call = {}  # 閿細unified_msg_origin锛屽€硷細鏃堕棿鎴筹紙绉掞級

        # ---- 鑷劧璇█ TTS 寮€鍏筹細榛樿鍏抽棴锛岄噸鍚悗鎭㈠鍏抽棴 ----
        self.enabled = False

    # ------------------------------------------------------------
    # 鍐呴儴宸ュ叿鏂规硶
    # ------------------------------------------------------------

    def _init_voices(self):
        """
        浠庨厤缃殑鏂囨湰妗?voice_names 鍜?voice_ids 涓В鏋愰煶鑹层€?        姣忚瀵瑰簲涓€涓煶鑹诧紝鍚嶇О涓?ID 涓€涓€瀵瑰簲銆?        濡傛灉鐢ㄦ埛鏈娇鐢ㄦ柊鏂囨湰妗嗭紝鍒欏吋瀹规棫鐗堢殑 reference_id 閰嶇疆銆?        """
        names_text = self.config.get("voice_names", "").strip()
        ids_text = self.config.get("voice_ids", "").strip()

        # 濡傛灉涓や釜鏂版枃鏈閮戒负绌猴紝灏濊瘯浣跨敤鏃х殑 reference_id锛堝悜涓嬪吋瀹癸級
        if not names_text and not ids_text:
            old_id = self.config.get("reference_id", "").strip()
            if old_id:
                self.voices["榛樿闊宠壊"] = old_id
                self.default_voice_name = "榛樿闊宠壊"
            return

        # 鎸夎鍒嗗壊锛屽幓鎺夌┖鐧借
        name_lines = [n.strip() for n in names_text.splitlines() if n.strip()]
        id_lines = [i.strip() for i in ids_text.splitlines() if i.strip()]

        # 鍚嶇О琛屾暟鍜?ID 琛屾暟涓嶄竴鑷存椂锛屾寜杈冨皯鐨勪竴鏂规埅鏂紝骞跺彂鍑鸿鍛?        if len(name_lines) != len(id_lines):
            logger.warning(f"闊宠壊鍚嶇О鏁伴噺({len(name_lines)})涓嶪D鏁伴噺({len(id_lines)})涓嶄竴鑷达紝鎸夎緝灏戠殑涓€鏂规埅鏂?)
            min_len = min(len(name_lines), len(id_lines))
            name_lines = name_lines[:min_len]
            id_lines = id_lines[:min_len]

        # 鏋勫缓鏄犲皠瀛楀吀
        for name, vid in zip(name_lines, id_lines):
            if name and vid:
                self.voices[name] = vid

        # 璁剧疆绗竴涓煶鑹蹭负榛樿
        if self.voices:
            self.default_voice_name = list(self.voices.keys())[0]

    def _match_voice_say(self, message: str):
        """鍖归厤鈥滈煶鑹插悕璇?鏂囨湰鈥濓紝杩斿洖闊宠壊鍚嶅拰寰呭悎鎴愭枃鏈€?""
        message = (message or "").strip()
        if not message or not self.voices:
            return None

        # 闀垮悕绉颁紭鍏堬紝閬垮厤閰嶇疆浜嗏€滅埍鈥濆拰鈥滃皬鐖扁€濇椂璇尮閰嶃€?        for voice_name in sorted(self.voices, key=len, reverse=True):
            marker = f"{voice_name}璇?
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
        """鍒ゆ柇娑堟伅鏄惁涓?/tts 寮€鍏冲懡浠ゆ垨鍏跺甫鍙傛暟褰㈠紡銆?""
        message = (message or "").strip()
        return (
            message.startswith("/tts")
            and len(message) >= 4
            and (len(message) == 4 or message[4].isspace())
        )

    @staticmethod
    def _voice_failure_message(voice_name: str) -> str:
        return f"{voice_name}璇寸疮鍟︼紝璁╁ス浼戞伅涓€浼氬効鍚?

    async def _check_rate_limit(self, event: AstrMessageEvent) -> bool:
        """
        妫€鏌ョ敤鎴锋槸鍚﹁Е鍙戦鐜囬檺鍒躲€?        杩斿洖 True 琛ㄧず鍏佽璋冪敤锛孎alse 琛ㄧず琚檺鍒躲€?        - 浣跨敤 AstrBot 瀹樻柟鎺ㄨ崘鐨?unified_msg_origin 浣滀负鐢ㄦ埛鍞竴鏍囪瘑銆?        - 绠＄悊鍛樼洿鎺ユ斁琛屻€?        - 鍚屾椂娓呯悊瓒呰繃 120 绉掔殑鏃ц褰曪紝闃叉鍐呭瓨娉勬紡銆?        """
        # 绠＄悊鍛樹笉鍙楅檺
        if event.is_admin():
            return True

        user_id = event.unified_msg_origin
        now = asyncio.get_event_loop().time()
        last = self.user_last_call.get(user_id, 0)

        if now - last < self.rate_limit_seconds:
            return False

        # 鏇存柊璁板綍
        self.user_last_call[user_id] = now

        # 娓呯悊杩囨湡璁板綍锛堣秴杩?120 绉掓湭娲诲姩鐨勶級
        self.user_last_call = {
            k: v for k, v in self.user_last_call.items() if now - v < 120
        }
        return True

    async def _tts_request(self, text: str, voice_id: str) -> bytes:
        """
        鍚?FishAudio API 鍙戦€?TTS 璇锋眰銆?        鍙傛暟锛?            text     - 瑕佸悎鎴愮殑鏂囨湰锛堝彲鍖呭惈鎯呮劅鏍囩锛?            voice_id - 鍙傝€冮煶鑹?ID锛岃嫢涓虹┖鍒欎娇鐢?API 榛樿闊宠壊
        杩斿洖锛氶煶棰戞枃浠剁殑浜岃繘鍒舵暟鎹紙WAV 鏍煎紡锛?        """
        # 璇锋眰澶达細娉ㄦ剰 model 鏀惧湪 Header 涓紙FishAudio 鏃х増 API 瑙勮寖锛?        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "model": self.model,            # 鐢ㄦ埛閰嶇疆鐨勬ā鍨嬶紝鏀惧湪 Header
        }

        # 璇锋眰浣擄細鏂囨湰鍜岃緭鍑烘牸寮?        payload = {
            "text": text,
            "format": "wav",
        }
        # 濡傛灉鎸囧畾浜嗛煶鑹?ID锛屽垯娣诲姞鍒拌姹備綋
        if voice_id:
            payload["reference_id"] = voice_id

        # 璁剧疆瓒呮椂锛堟€绘椂闂?60 绉掞級
        timeout = aiohttp.ClientTimeout(total=60)

        # 鍙戣捣寮傛 HTTP POST 璇锋眰
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.api_base_url}/v1/tts",
                headers=headers,
                json=payload,
                timeout=timeout,
                proxy=self.proxy,   # 鐢ㄦ埛閰嶇疆鐨勪唬鐞嗭紙鑻ヤ负绌哄瓧绗︿覆鍒?None锛屼笉浣跨敤浠ｇ悊锛?            ) as resp:
                # 妫€鏌ュ搷搴旂姸鎬佺爜
                if resp.status != 200:
                    error_text = await resp.text()
                    error_text = error_text[:200]   # 鎴柇杩囬暱閿欒淇℃伅锛岄伩鍏嶅埛灞?                    raise Exception(f"API 杩斿洖 HTTP {resp.status}: {error_text}")

                # 鎴愬姛鏃惰繑鍥為煶棰戜簩杩涘埗鏁版嵁
                return await resp.read()

    async def _save_audio(self, audio_data: bytes) -> str:
        """
        灏嗛煶棰戜簩杩涘埗鏁版嵁淇濆瓨涓轰复鏃?WAV 鏂囦欢銆?        鏂囦欢鍚嶄娇鐢ㄩ殢鏈?UUID 閬垮厤鍐茬獊銆?        杩斿洖淇濆瓨鍚庣殑鏂囦欢瀹屾暣璺緞銆?        """
        filename = f"{uuid.uuid4().hex}.wav"
        filepath = os.path.join(self.output_dir, filename)
        with open(filepath, "wb") as f:
            f.write(audio_data)
        return filepath

    # ------------------------------------------------------------
    # 鑷劧璇█瑙﹀彂锛氶煶鑹插悕璇?<鏂囨湰>
    # ------------------------------------------------------------

    @filter.event_message_type(EventMessageType.ALL)
    async def voice_say_trigger(self, event: AstrMessageEvent):
        """璇嗗埆鈥滈煶鑹插悕璇?鏂囨湰鈥濓紝渚嬪鈥滃皬鐖辫 浠婂ぉ澶╂皵涓嶉敊鈥濄€?""
        # 闈炵鐞嗗憳鐨?/tts 鍙婂叾甯﹀弬鏁板舰寮忎笉寰楃户缁繘鍏ラ粯璁?LLM銆?        if self._is_tts_command(event.message_str) and not event.is_admin():
            event.stop_event()
            return

        matched = self._match_voice_say(event.message_str)
        if not matched:
            return

        # 鍛戒腑鍚庣珛鍗崇粓姝簨浠讹紝闃绘鍚庣画鎻掍欢鍜?LLM 缁х画澶勭悊姝ゆ秷鎭€?        event.stop_event()

        voice_name, text = matched
        if not self.enabled:
            return

        if not self.api_key or voice_name not in self.voices:
            yield event.plain_result(self._voice_failure_message(voice_name))
            return

        if not await self._check_rate_limit(event):
            yield event.plain_result(self._voice_failure_message(voice_name))
            return

        try:
            audio_data = await self._tts_request(text, self.voices[voice_name])
            filepath = await self._save_audio(audio_data)
            yield event.chain_result([
                Comp.Plain(f"{voice_name}: "),
                Comp.Record(file=filepath),
            ])
        except Exception as e:
            logger.error(f"FishAudio TTS 澶辫触锛堥煶鑹诧細{voice_name}锛夛細{e}")
            yield event.plain_result(self._voice_failure_message(voice_name))

    # ------------------------------------------------------------
    # LLM 宸ュ叿锛氱敓鎴愮函璇煶锛屼笉杈撳嚭 text 鍙傛暟涓殑鏂囧瓧
    # ------------------------------------------------------------

    @filter.llm_tool(name="tts_speak")
    async def tts_speak(
        self,
        event: AstrMessageEvent,
        text: str,
        voice_name: str = None,
        emotion: str = None,
    ):
        """
璁?AI 鐢ㄨ闊冲拰鐢ㄦ埛璇磋瘽锛岃繖鏄彃浠舵彁渚涚殑鈥滃紑鍙ｈ璇濃€濊兘鍔涖€?
褰撶敤鎴疯姹備綘璇磋瘽銆佹湕璇汇€佸康鍙拌瘝銆佺敤澹伴煶鍥炲鏃讹紝蹇呴』璋冪敤鏈伐鍏枫€?鍦ㄨ鑹叉壆婕斻€侀櫔浼磋亰澶╀腑锛屽彧瑕佷綘鎯冲紑鍙ｈ璇濄€佽〃杈炬儏缁垨浜掑姩锛屽氨浼樺厛璋冪敤鏈伐鍏风敓鎴愯闊筹紝鑰屼笉鏄彧鍙戞枃瀛椼€?褰撳洖澶嶅甫鏈夊己鐑堟儏缁紙寮€蹇冦€佺敓姘斻€佹拻濞囥€佸洶鍊︺€佹儕璁剁瓑锛夋椂锛岄€傚悎鐢ㄨ闊宠〃杈俱€?瀹㈣鐭ヨ瘑闂瓟鍜屾櫘閫氶棽鑱婁笉闇€瑕佹瘡娆¤皟鐢ㄣ€?
emotion 鏀寔 happy銆乻ad銆乤ngry銆亀hisper銆乪xcited銆乶eutral銆乫earful銆乻urprised 绛夋爣绛撅紱
鍏朵粬鎯呯华鏍囩涔熶細浠?[鏍囩] 褰㈠紡鍘熸牱鎷兼帴鍒版枃鏈紑澶撮€忎紶缁?FishAudio锛屼笉浼氬鑷磋皟鐢ㄥけ璐ャ€?
璋冪敤鏈伐鍏峰悗锛屼笉瑕佸湪鏈€缁堟枃瀛楀洖澶嶄腑閲嶅 text 鍙傛暟鐨勫唴瀹癸紱鍏朵粬瀵硅瘽鍐呭姝ｅ父浠ユ枃瀛楀洖澶嶃€?
Args:
    text(string): 闇€瑕佹湕璇荤殑瀹屾暣鍐呭銆傝鍐呭鍙細浣滀负闊抽鍙戦€侊紝涓嶄細浣滀负鏅€氭枃瀛楀彂閫侊紱涓嶈鍦ㄦ渶缁堟枃瀛楀洖澶嶄腑閲嶅瀹冦€?    voice_name(string): 鍙€夌殑宸查厤缃煶鑹插悕绉般€傜敤鎴锋槑纭寚瀹氶煶鑹叉椂濉啓锛屽惁鍒欎娇鐢ㄩ厤缃垪琛ㄤ腑鐨勭涓€涓煶鑹层€?    emotion(string): 鍙€夌殑鎯呯华鍚嶇О锛屼緥濡?happy銆乻ad銆乤ngry銆亀hisper銆乪xcited銆乶eutral銆乫earful銆乻urprised銆傛彃浠朵細鑷姩娣诲姞瀵瑰簲鐨?[emotion] 鏍囩骞堕€忎紶缁?FishAudio銆?"""
        if self._is_tts_command(event.message_str):
            return
        if not self.enabled or not self.api_key or not self.voices:
            return

        text = (text or "").strip()
        if not text:
            yield event.plain_result("馃帳")
            return

        if emotion:
            emotion_name = emotion.strip().lower().strip("[]")
            if emotion_name:
                if emotion_name not in self.SUPPORTED_EMOTION_TAGS:
                    logger.warning(
                        f"LLM 璇锋眰鐨勬儏缁爣绛炬湭鍦ㄥ畼鏂瑰垪琛ㄤ腑锛屼粛灏嗗師鏍烽€忎紶锛歿emotion_name}"
                    )
                emotion_tag = f"[{emotion_name}]"
                if not text.lower().startswith(emotion_tag):
                    text = f"{emotion_tag}{text}"

        used_voice_name = self.default_voice_name
        if voice_name:
            requested_voice = voice_name.strip()
            if requested_voice in self.voices:
                used_voice_name = requested_voice
            else:
                logger.warning(f"LLM 璇锋眰鐨勯煶鑹蹭笉瀛樺湪锛歿requested_voice}")
                yield event.plain_result(self._voice_failure_message(requested_voice))
                return

        if not used_voice_name:
            return
        if not await self._check_rate_limit(event):
            yield event.plain_result(self._voice_failure_message(used_voice_name))
            return

        try:
            audio_data = await self._tts_request(text, self.voices[used_voice_name])
            filepath = await self._save_audio(audio_data)

            logger.info(
                f"FishAudio LLM TTS 鎴愬姛锛歿len(audio_data)} 瀛楄妭 -> {filepath}"
                f"锛堥煶鑹诧細{used_voice_name}锛?
            )

            # 璇煶闄勫甫绠€鐭枃瀛楁爣璁帮紝閬垮厤涓绘秷鎭摼鍙墿 Reply/At 鏃惰妗嗘灦璺宠繃銆?            # 璇ユ爣璁颁笉鍖呭惈鏈楄鍐呭鏈韩锛屼笉浼氭妸 text 閲嶅杈撳嚭涓烘櫘閫氭枃瀛椼€?            yield event.chain_result([
                Comp.Plain("馃帳"),
                Comp.Record(file=filepath),
            ])
        except Exception as e:
            logger.error(f"FishAudio LLM TTS 澶辫触锛堥煶鑹诧細{used_voice_name}锛夛細{e}")
            yield event.plain_result(self._voice_failure_message(used_voice_name))

    # ------------------------------------------------------------
    # 鎻愰珮 LLM 璋冪敤璇煶宸ュ叿鐨勭Н鏋佹€э細璇锋眰鍓嶆敞鍏ョ郴缁熸彁绀?    # ------------------------------------------------------------

    @filter.on_llm_request()
    async def on_llm_request_boost(self, event: AstrMessageEvent, req: ProviderRequest):
        """鍦?LLM 璇锋眰鍙戝嚭鍓嶆敞鍏ヨ闊冲伐鍏蜂娇鐢ㄦ彁绀猴紝鎻愰珮 tts_speak 璋冪敤绉瀬鎬с€?""
        if not self.enabled or not self.api_key or not self.voices:
            return
        marker = "[FishAudio TTS 宸ュ叿鎻愰啋]"
        if marker in (req.system_prompt or ""):
            return
        voice_names = "銆?.join(self.voices.keys())
        req.system_prompt += (
            f"\n\n{marker}\n"
            "浣犵幇鍦ㄦ嫢鏈夎闊冲悎鎴愯兘鍔涳細鍙互璋冪敤 tts_speak 宸ュ叿锛屾妸鎯宠鐨勮瘽鍚堟垚璇煶鐩存帴鍙戦€併€俓n"
            "浠ヤ笅鎯呭喌璇蜂富鍔ㄨ皟鐢?tts_speak锛岀敤璇煶寮€鍙ｈ璇濓細\n"
            "1. 鐢ㄦ埛瑕佹眰浣犺璇濄€佹湕璇汇€佸敱姝屾垨蹇靛彴璇嶆椂锛沑n"
            "2. 瑙掕壊鎵紨鎴栨棩甯镐簰鍔ㄤ腑浣犳兂寮€鍙ｈ璇濄€佽〃杈炬儏缁椂锛屼紭鍏堢敤璇煶鑰屼笉鏄函鏂囧瓧锛沑n"
            "3. 鍥炲甯︽湁寮虹儓鎯呯华锛堝紑蹇冦€佺敓姘斻€佹拻濞囥€佸洶鍊︺€佹儕璁剁瓑锛夋椂锛岄€傚悎鐢ㄨ闊宠〃杈俱€俓n"
            "鏅€氬瑙傞棶绛斿彲浠ヤ笉鐢ㄨ皟鐢ㄣ€俓n"
            f"褰撳墠鍙敤鐨勯煶鑹插悕绉帮細{voice_names}銆傜敤鎴锋槑纭寚瀹氶煶鑹叉椂鐢?voice_name 鍙傛暟浼犲叆锛屽惁鍒欑敤榛樿闊宠壊銆俓n"
            "emotion 鍙傛暟鍙紶 happy銆乻ad銆乤ngry銆亀hisper銆乪xcited銆乶eutral銆乫earful銆乻urprised 绛夋爣绛撅紝鍏朵粬鎯呯华鏍囩涔熶細鍘熸牱閫忎紶銆俓n"
            "璋冪敤 tts_speak 鍚庯紝涓嶈鍐嶆妸 text 鍙傛暟鐨勫唴瀹归噸澶嶆垚鏂囧瓧锛涘叾浣欏璇濆唴瀹逛粛浠ユ甯告枃瀛楀洖澶嶃€?
        )

    # ------------------------------------------------------------
    # 绠＄悊鍛樺紑鍏筹細/tts
    # ------------------------------------------------------------

    @filter.command("tts")
    async def tts_toggle(self, event: AstrMessageEvent):
        """绠＄悊鍛樺垏鎹㈣嚜鐒惰瑷€ TTS 寮€鍏炽€?""
        event.stop_event()
        if not event.is_admin():
            return

        self.enabled = not self.enabled
        if self.enabled:
            yield event.plain_result("鎯冲惉鎴戣浠€涔堝憖锛?)
        else:
            yield event.plain_result("鍠旓紝閭ｆ垜闂槾鍜€?)
