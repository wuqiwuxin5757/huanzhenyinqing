# 幻帧引擎 · Huanzhen Engine

面向本地创作的 ComfyUI 视频工作台。通过 `/easy` 生成单镜头或编排剧本、逐段接拍、拼接和加声音，并在作品历史中预览、下载和管理成果。

- 支持文字和照片起始。长视频可为每段填写不同动作。
- 多段生成可逐段预览，重试当前段或保存已通过的段落。成片页面可核对每段实际使用的描述。
- 支持从成片指定时间截断并接拍，另存结果。
- 可选旁白、人物对口型、配乐、音效、高清放大和补帧。
- 可保存分阶段校验素材。历史中的删除会将作品移到本地回收目录。

## 安装

项目是 ComfyUI 的扩展。开发和验证环境为 Windows 便携版 [ComfyUI v0.43.0](https://github.com/Comfy-Org/ComfyUI/tree/426f5eea52a7ece1dd599fff4cb6cd1f352ddd24)，使用本机模型。可先安装基础图生视频，其他功能按需配置。

1. 在 ComfyUI 根目录运行 `git clone https://github.com/wuqiwuxin5757/huanzhenyinqing.git custom_nodes/huanzhen-engine`。用 ZIP 下载时，将内容解压到同一位置，确保该目录直接包含 `__init__.py`。
2. 在 ComfyUI 的 Python 环境中安装此仓库的 `requirements.txt`。便携版可从便携根目录运行 `python_embeded\python.exe -m pip install -r ComfyUI\custom_nodes\huanzhen-engine\requirements.txt`。
3. 安装 [ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF)，本机使用的提交为 `6ea2651`。遵循该扩展的依赖安装说明，把 Wan 2.2 相关文件按 [模型清单](models.required.json) 放进 `ComfyUI/models`。图生和文生分别使用 `i2v` 和 `t2v` 组。
4. 在便携根目录运行 `python_embeded\python.exe ComfyUI\custom_nodes\huanzhen-engine\check_setup.py --comfy-root ComfyUI --feature i2v`。启动 ComfyUI 后追加 `--url http://127.0.0.1:8188`，还可检查扩展入口与所需节点是否加载。
5. 打开 `http://127.0.0.1:8188/easy`。上传一张自备照片，选择 3 秒，填写简单动作，例如「镜头缓缓推近，树叶随风轻摆」。首次验证可以先关闭旁白、配乐、音效、放大和补帧。

`check_setup.py` 会列出缺失的文件或节点，并返回非零退出码。使用文生视频时改为 `--feature t2v`，可以多次传入 `--feature` 检查不同功能。检查不运行推理，显存需求和成片质量仍取决于模型、分辨率及机器配置。

可选功能使用对应的本地模型和节点。旁白使用 [ComfyUI-Qwen-TTS](https://github.com/flybirdxx/ComfyUI-Qwen-TTS) 的 Qwen3-TTS VoiceDesign，对口型还需要 InfiniteTalk，音效使用 [ComfyUI-MMAudio](https://github.com/kijai/ComfyUI-MMAudio)，配乐使用 ACE-Step，放大使用 SeedVR2，补帧使用 FILM，照片编辑使用 Qwen-Image。请按照各第三方项目文档安装其附加依赖及权重。整套能力依赖的具体节点可以在 [工作流代码](page/index.html) 中核对。

剧本编排和自动描述照片需要放在便携根目录旁的 `llama.cpp`。当前配置从它的 `bin` 目录启动 `llama-server.exe`，从 `models` 目录加载 `Qwen3VL-8B-Instruct-Q8_0.gguf` 和 `mmproj-Qwen3VL-8B-Instruct-F16.gguf`。没有配置编剧模型时，仍可用「用原文建镜头」手动编排。

如需 Windows 桌面启动方式，把 [portable-launcher](portable-launcher) 里的文件复制到便携根目录下的 `easy_app`，双击 `launcher.vbs`。也可以创建指向它的桌面快捷方式并选用随附图标。

模型、第三方扩展和 ComfyUI 需分别准备。作品和临时参考素材存放在安装环境的输入、输出目录。作品回收目录为 `ComfyUI/output/easy/.trash`。
