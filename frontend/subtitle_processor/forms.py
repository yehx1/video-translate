from django import forms
from .models import Language
from .seed_data import DEFAULT_LANGUAGES

class VideoUploadForm(forms.Form):
    title = forms.CharField(
        label="任务名称",
        max_length=255,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "请输入任务名称"}),
    )

    target_language = forms.ChoiceField(
        label="目标语言",
        choices=[],  # 运行时填充
        initial="zh-CN",
        widget=forms.Select(attrs={"class": "form-select"}),
        required=True,
    )

    video_file = forms.FileField(
        label="上传视频",
        required=True,
        widget=forms.ClearableFileInput(attrs={"class": "form-control", "accept": "video/*"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Language.objects.all().values_list("target_language", "target_language_display")
        choices = list(qs) or DEFAULT_LANGUAGES  # 数据库为空时兜底
        self.fields["target_language"].choices = choices