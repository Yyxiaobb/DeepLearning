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

语音识别使用 faster-whisper。可在页面顶部选择“快速（tiny）”“平衡（base）”或“精准（small）”。模型权重存放在项目的 `models/whisper/{tiny,base,small}`，不纳入 Git。选择新模型时后端会释放旧模型并加载新模型；正在转写时可选择等待结束后切换，或协作式取消当前任务后切换。空闲时立即切换。
每份材料的三点菜单提供“重新解析”，会按照当前选择的识别模式重新生成听力稿；旧稿会在新解析完成前保留。

    python -m pip install -r requirements-ml.txt

`requirements-ml.txt` 将 PyAV 固定在 19 以下，这是为了兼容 faster-whisper 的音频解码参数；如果直接安装最新 PyAV，转写可能在模型加载后报 `open() got an unexpected keyword argument 'metadata_errors'`。
Windows 环境还会自动设置 `KMP_DUPLICATE_LIB_OK=TRUE`，避免 ctranslate2 加载时因 OpenMP 运行库重复而退出服务。
    python -m uvicorn server:app --reload

可设置 WHISPER_DEVICE（默认为 cpu）和 WHISPER_COMPUTE_TYPE（默认为 int8）。GPU 用户可根据本机 CUDA 环境选择 cuda。支持英语和日语；页面会显示识别进度。如果本地模型尚未下载或上传后停在“待识别”，先在项目环境完成模型下载，然后回到材料点击“开始识别”。

## 启用翻译

推荐使用百度翻译开放平台的通用文本翻译 API。先在 https://fanyi-api.baidu.com/ 注册并开通通用文本翻译，取得 App ID 和密钥。浏览器使用百度翻译网页不代表已开通 API；具体额度和收费以平台显示为准。

复制项目中的 .env.example 为 .env，在本机填写：

    BAIDU_TRANSLATE_APP_ID=你的AppID
    BAIDU_TRANSLATE_SECRET=你的密钥

然后重启项目。密钥只由后端读取，不会发送给浏览器；.env 已加入 Git 忽略列表。翻译时所选句子或单词会发送给百度。百度通用文本翻译 API 翻译所点击的词本身，不提供利用句子上下文消歧的保证。

也可以使用 LibreTranslate 兼容服务作为替代，配置：

    $env:TRANSLATE_URL = 'http://localhost:5000'
    $env:TRANSLATE_API_KEY = '你的服务密钥（如需要）'
    python -m uvicorn server:app --reload

若服务不需要密钥，可省略 TRANSLATE_API_KEY。没有配置翻译服务时，上传、转写、播放和编辑仍可使用，点击翻译会显示明确的配置提示。

项目不会自动提供第三方翻译密钥。若同时配置百度和 LibreTranslate，优先使用百度。示例课程已有的中文译文和已收藏词义会继续显示缓存结果。

百度翻译网页地址（`fanyi.baidu.com/mtpe-individual/transText`）是网页页面，不是稳定的公开 JSON API。后端可以访问页面 HTML，但网页内部翻译请求依赖动态令牌、Cookie 和风控校验，接口也可能随时变化，因此项目使用百度翻译开放平台 API，不直接模拟网页内部请求。

词语卡片下方的独立词典释义使用 Free Dictionary API 获取英文词性、音标、发音地址和例句，再用当前翻译服务翻译简释；该词典接口不需要密钥，但需要网络连接。我的词汇详情页会显示简明释义、音标和最多三条例句。当前独立词典卡片优先支持英文单词。

## 本地翻译与收藏词汇生成

项目使用本地 Qwen3.5-4B GGUF 量化模型，句子翻译、结合上下文的选词翻译，以及收藏词汇的简明释义和例句生成都会优先调用本地模型。模型文件放在 models/llm/，llama.cpp 运行文件放在 models/llama.cpp/，两者均不提交到 Git。首次运行 `start_llm.ps1` 时，脚本会在模型加载后执行一次短预热，第一次启动需要多等一会儿，但后续翻译响应更快：

    .\start_llm.ps1

随后按“启动”章节启动 FastAPI。模型服务仅监听本机 127.0.0.1:8081；若模型服务不可用，句子翻译会回退到现有百度 / LibreTranslate 配置，收藏词汇会使用词典内容兜底。

收藏单词后，项目会在后台生成并保存简明释义、音标和例句。词汇详情页读取数据库及本地缓存发音，不会每次打开都重新请求词典。

## 当前版本说明

- 单用户本地版，不包含账号、云端同步或多设备共享。
- 句子边界及时间戳由识别结果估算，用户可以编辑、合并和拆分。
- 选词查询会携带当前句子上下文，翻译由所配置的服务提供。
- 音频限制为 200 MB；允许 MP3、WAV、M4A、AAC、OGG、FLAC、WEBM。
- 删除 data/ 和 uploads/ 会清除本地学习数据与音频。
