# 言间 · 语言精听工作台

一个面向中文学习者的本地优先英语 / 日语精听学习网站。第一版包括音频上传、逐句转写、时间轴播放、句子编辑、逐句翻译和选词查询。

## 启动

需要 Python 3.10 或更新版本。

    python -m venv .venv
    .venv\\Scripts\\Activate.ps1
    python -m pip install -r requirements.txt
    python -m uvicorn server:app --reload

浏览器打开 http://127.0.0.1:8000。数据保存在项目的 data/ 目录，音频保存在 uploads/。本地运行时，文件不会自动上传到第三方存储。

## 启用本地语音识别

语音模型是可选安装项，体积较大。安装后首次转写会下载所选模型；建议先用 small 模型体验。

    python -m pip install -r requirements-ml.txt
    $env:WHISPER_MODEL = 'small'
    python -m uvicorn server:app --reload

可设置 WHISPER_DEVICE（默认为 cpu）和 WHISPER_COMPUTE_TYPE（默认为 int8）。GPU 用户可根据本机 CUDA 环境选择 cuda。支持英语和日语；转写任务在后台运行，首次启动模型会较慢。

## 启用翻译

翻译 API 使用 LibreTranslate 兼容格式。可以自托管服务，再配置：

    $env:TRANSLATE_URL = 'http://localhost:5000'
    $env:TRANSLATE_API_KEY = '你的服务密钥（如需要）'
    python -m uvicorn server:app --reload

若服务不需要密钥，可省略 TRANSLATE_API_KEY。没有配置翻译服务时，上传、转写、播放和编辑仍可使用，点击翻译会显示明确的配置提示。

## 当前版本说明

- 单用户本地版，不包含账号、云端同步或多设备共享。
- 句子边界及时间戳由识别结果估算，用户可以编辑、合并和拆分。
- 选词查询会携带当前句子上下文，翻译由所配置的服务提供。
- 音频限制为 200 MB；允许 MP3、WAV、M4A、AAC、OGG、FLAC、WEBM。
- 删除 data/ 和 uploads/ 会清除本地学习数据与音频。
