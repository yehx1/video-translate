from django.db import models
from django.conf import settings

# 定义一个模型用于存储语言代码与展示信息
class Language(models.Model):
    target_language = models.CharField(max_length=10, unique=True)
    target_language_display = models.CharField(max_length=50)

    # class Meta:
    #     ordering = ["target_language"]

    def __str__(self):
        return f"{self.target_language} - {self.target_language_display}"

VOICE_GENDER_CHOICES = [
    ("female", "女声"),
    ("male", "男声"),
    ("auto", "原声/克隆"),
]

class VoiceProfile(models.Model):
    """与目标语言绑定的可选音色"""
    # 为了简单稳妥，用 code 存语种，不强制外键；也可用 ForeignKey(Language, to_field="target_language")
    language_code = models.CharField(max_length=16, db_index=True)  # 如 "zh-CN" / "en" ...
    code = models.CharField(max_length=64)                          # 如 "zh-f-001" / "auto"
    tts_name = models.CharField(max_length=64)                      # 音色名
    name = models.CharField(max_length=100)                         # 展示名
    enname = models.CharField(max_length=100)                       # 英文展示名
    gender = models.CharField(max_length=10, choices=VOICE_GENDER_CHOICES)
    sample_url = models.CharField(max_length=255, blank=True, default="")  # 示例音频URL
    enabled = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        unique_together = ("language_code", "code")
        ordering = ["language_code", "sort_order", "code"]

    def __str__(self):
        return f"[{self.language_code}] {self.code} - {self.name}"