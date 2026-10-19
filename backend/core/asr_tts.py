"""语音识别 + 语音合成"""
from funasr import AutoModel
import os
import asyncio
from edge_tts import Communicate  # 导入放在顶部

class ASRService:
    def __init__(self):
        print("⏳ 加载 ASR...")
        self.model = AutoModel(
            model="iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
            device="cpu",  # 修改为 CPU
            disable_update=True
        )
        print("✅ ASR 就绪")
    
    def transcribe(self, audio_path):
        result = self.model.generate(input=audio_path)
        if result and len(result) > 0:
            return result[0].get('text', '')
        return ""

class TTSService:
    def __init__(self):
        self.voice = "zh-CN-XiaoxiaoNeural"  # 温柔女声
        print("✅ Edge TTS 就绪 (Xiaoxiao)")
    
    # 注意：这是一个独立的异步类方法
    async def synthesize(self, text: str, output_path: str) -> None:
        """使用edge-tts的异步API合成语音"""
        try:
            communicate = Communicate(text, voice=self.voice)
            await communicate.save(output_path)
            print(f"✅ 语音合成完成: {output_path}")
        except Exception as e:
            print(f"❌ 语音合成失败: {e}")
            raise
