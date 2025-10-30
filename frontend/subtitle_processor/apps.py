from __future__ import annotations
from django.apps import AppConfig
from typing import Dict, Iterable, Tuple
from django.db import transaction, connections
from django.db.models.signals import post_migrate
from logs import get_logger
from .seed_data import DEFAULT_LANGUAGES, get_default_voice_bank

log = get_logger(__name__)

class SubtitleProcessorConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "subtitle_processor"

    # ------------ 信号绑定 ------------
    def ready(self) -> None:
        # 仅在本 app 完成 migrate 后触发
        post_migrate.connect(self._on_post_migrate, sender=self)

    # ------------ 信号处理 ------------
    @staticmethod
    def _on_post_migrate(
        sender: AppConfig,
        app_config: AppConfig,
        using: str,
        verbosity: int,
        **kwargs,
    ) -> None:
        """
        在 `migrate subtitle_processor` 之后执行，保证表已创建。
        使用信号传入的 `using`（数据库别名）与 `apps`（历史态 registry）以确保兼容性。
        """
        state_apps = kwargs.get("apps")  # 迁移时的 AppRegistry（历史态）
        if state_apps is None:
            # 理论上 post_migrate 总会带上 `apps`；兜底使用全局 apps 也可工作
            from django.apps import apps as global_apps

            state_apps = global_apps

        Language = state_apps.get_model("subtitle_processor", "Language")
        VoiceProfile = state_apps.get_model("subtitle_processor", "VoiceProfile")

        # 若数据库暂不可用（极少数场景），直接跳过
        try:
            with connections[using].cursor() as cur:
                cur.execute("SELECT 1")
        except Exception:
            return

        # 幂等写入
        try:
            SubtitleProcessorConfig._seed_languages(Language, using)
            SubtitleProcessorConfig._seed_voice_profiles(VoiceProfile, using)
        except Exception as e:
            # 不阻断项目启动；需要时可在 settings.LOGGING 中接入更细日志
            if verbosity:
                log.warning(f"[subtitle_processor] post_migrate seeding skipped due to: {e}")

    # ------------ 具体的写入逻辑（幂等/批量优化） ------------
    @staticmethod
    def _seed_languages(LanguageModel, using: str) -> None:
        """
        使用 bulk_create(ignore_conflicts=True) + 差集，避免逐条 get_or_create 的额外查询。
        """
        wanted: Iterable[Tuple[str, str]] = DEFAULT_LANGUAGES
        with transaction.atomic(using=using):
            existing = set(
                LanguageModel.objects.using(using)
                .values_list("target_language", flat=True)
            )
            to_create = [
                LanguageModel(
                    target_language=code, target_language_display=label
                )
                for code, label in wanted
                if code not in existing
            ]
            if to_create:
                # Django 2.2+ 支持 ignore_conflicts；旧版本可改为 try/except
                LanguageModel.objects.using(using).bulk_create(
                    to_create, ignore_conflicts=True
                )

            # 同时对已存在的项做展示名“对齐更新”（不强制，但保证与默认表一致）
            update_map: Dict[str, str] = {code: label for code, label in wanted}
            rows = (
                LanguageModel.objects.using(using)
                .filter(target_language__in=update_map.keys())
                .only("id", "target_language", "target_language_display")
            )
            dirty = []
            for r in rows:
                new_label = update_map.get(r.target_language)
                if new_label and r.target_language_display != new_label:
                    r.target_language_display = new_label
                    dirty.append(r)
            if dirty:
                LanguageModel.objects.using(using).bulk_update(
                    dirty, ["target_language_display"]
                )

    @staticmethod
    def _seed_voice_profiles(VoiceProfileModel, using: str) -> None:
        """
        逐条 update_or_create，保证代码/名称/示例等可被更新；数量有限影响可忽略。
        """
        with transaction.atomic(using=using):
            voice_bank = get_default_voice_bank()
            for lang_code, voices in voice_bank.items():
                for idx, v in enumerate(voices):
                    code = v["code"]
                    defaults = {
                        "name": v.get("name", code),
                        "enname": v.get("enname", code),
                        "tts_name": v.get("tts_name", code),
                        "gender": v.get("gender", "auto"),
                        "sample_url": v.get("sample", "") or "",
                        "enabled": True,
                        "sort_order": idx,
                    }
                    # unique_together(language_code, code) 保证幂等
                    VoiceProfileModel.objects.using(using).update_or_create(
                        language_code=lang_code, code=code, defaults=defaults
                    )
