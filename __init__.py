"""Serves a simplified Chinese video-generation page at http://127.0.0.1:8188/easy.

The page queues jobs through ComfyUI's own API (/prompt, /upload/image, /ws). This
extension adds the page itself plus a few helpers that ComfyUI has no API for:
listing finished videos with their settings, grabbing a video's last frame, and
joining two clips with ffmpeg (used by "continue the video").
"""
import asyncio
import json
import math
import os
import re
import sys
import tempfile
import time

import av
import folder_paths
import imageio_ffmpeg
from aiohttp import web
from server import PromptServer

PAGE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "page", "index.html")
SUBFOLDER = "easy"
HISTORY_EXTENSIONS = {SUBFOLDER: (".mp4",), f"{SUBFOLDER}/edit": (".png", ".jpg", ".jpeg", ".webp")}
routes = PromptServer.instance.routes


def out_dir():
    d = os.path.join(folder_paths.get_output_directory(), SUBFOLDER)
    os.makedirs(d, exist_ok=True)
    return d


def resolve_output(subfolder, filename):
    """Path of a file under ComfyUI/output, refusing anything that escapes it."""
    root = os.path.realpath(folder_paths.get_output_directory())
    path = os.path.realpath(os.path.join(root, subfolder or "", filename))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        raise web.HTTPBadRequest(text="file not found")
    return path


def meta_path(video_path):
    return os.path.splitext(video_path)[0] + ".json"


async def ffmpeg(*args):
    proc = await asyncio.create_subprocess_exec(
        imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise web.HTTPInternalServerError(text=err.decode(errors="replace")[-2000:])


@routes.get("/easy")
async def easy_page(request):
    return web.FileResponse(PAGE, headers={"Cache-Control": "no-cache"})


@routes.post("/easy/shutdown")
async def easy_shutdown(request):
    """Stop ComfyUI completely (the app's "关闭后台" button) so the GPU and memory are freed."""
    asyncio.get_running_loop().call_later(0.5, os._exit, 0)
    return web.json_response({"ok": True})


@routes.get("/easy/list")
async def easy_list(request):
    root = folder_paths.get_output_directory()
    items = []
    for subfolder, extensions in HISTORY_EXTENSIONS.items():
        if subfolder != SUBFOLDER and request.query.get("kind") != "all":
            continue
        directory = os.path.join(root, subfolder)
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if not name.lower().endswith(extensions) or not os.path.isfile(path):
                continue
            meta = {}
            if os.path.isfile(meta_path(path)):
                try:
                    with open(meta_path(path), encoding="utf-8") as f:
                        meta = json.load(f)
                except (OSError, ValueError):
                    pass
            items.append({"filename": name, "subfolder": subfolder, "mtime": os.path.getmtime(path),
                          "kind": "video" if name.lower().endswith(".mp4") else "image",
                          "meta": meta, "can_delete": True})
    items.sort(key=lambda x: (x["mtime"], x["filename"]), reverse=True)
    return web.json_response(items)


@routes.post("/easy/history/delete")
async def easy_delete_history(request):
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    body = await request.json()
    if not isinstance(body, dict):
        return web.json_response({"error": "作品信息无效。"}, status=400)
    name, subfolder = body.get("filename"), body.get("subfolder")
    extensions = HISTORY_EXTENSIONS.get(subfolder, ()) if isinstance(subfolder, str) else ()
    if (not isinstance(name, str) or any(c in name for c in "/\\:\x00")
            or not name.lower().endswith(extensions)):
        return web.json_response({"error": "只能删除作品历史里的图片或视频。"}, status=400)
    if comfy_busy():
        return web.json_response({"error": "视频任务正在运行，请等任务结束后再删除作品。"}, status=409)

    root = os.path.realpath(folder_paths.get_output_directory())
    path = os.path.normpath(os.path.join(root, subfolder, name))
    if os.path.normcase(os.path.realpath(path)) != os.path.normcase(path):
        return web.json_response({"error": "作品路径无效。"}, status=400)
    if not os.path.isfile(path):
        return web.json_response({"error": "找不到这件作品，请刷新历史。"}, status=404)
    if body.get("mtime") != os.path.getmtime(path):
        return web.json_response({"error": "作品文件已变更，请刷新历史后重试。"}, status=409)

    trash = os.path.join(root, SUBFOLDER, ".trash")
    if os.path.normcase(os.path.realpath(trash)) != os.path.normcase(trash):
        return web.json_response({"error": "作品回收目录路径无效。"}, status=400)
    sidecar = meta_path(path)
    stem = os.path.splitext(path)[0]
    shared_meta = any(os.path.normcase(stem + ext) != os.path.normcase(path)
                      and os.path.isfile(stem + ext) for ext in extensions)
    moved_meta = False
    destination = None
    try:
        os.makedirs(trash, exist_ok=True)
        destination = tempfile.mkdtemp(dir=trash)
        if not shared_meta and os.path.isfile(sidecar):
            os.rename(sidecar, os.path.join(destination, os.path.basename(sidecar)))
            moved_meta = True
        os.rename(path, os.path.join(destination, name))
    except OSError:
        if destination:
            if moved_meta:
                os.rename(os.path.join(destination, os.path.basename(sidecar)), sidecar)
            os.rmdir(destination)
        return web.json_response({"error": "无法移动作品，请关闭正在使用该文件的程序后重试。"}, status=409)
    return web.json_response({"ok": True})


@routes.post("/easy/file_info")
async def easy_file_info(request):
    body = await request.json()
    path = resolve_output(body.get("subfolder"), body["filename"])
    return web.json_response({"mtime": os.path.getmtime(path)})


@routes.post("/easy/meta")
async def easy_meta(request):
    body = await request.json()
    path = resolve_output(body.get("subfolder"), body["filename"])
    with open(meta_path(path), "w", encoding="utf-8") as f:
        json.dump(body.get("meta", {}), f, ensure_ascii=False)
    return web.json_response({"ok": True})


@routes.post("/easy/lastframe")
async def easy_lastframe(request):
    """Save the last frame, or the last frame before a requested cut."""
    body = await request.json()
    src = resolve_output(body.get("subfolder"), body["filename"])
    in_dir = os.path.join(folder_paths.get_input_directory(), SUBFOLDER)
    os.makedirs(in_dir, exist_ok=True)
    name = f"last_frame_{int(time.time() * 1000)}.png"
    if body.get("keep_seconds") is not None:
        seconds = body["keep_seconds"]
        fps, _, duration = await probe(src, with_duration=True)
        if (type(seconds) not in (int, float) or not math.isfinite(seconds)
                or not 0 < seconds < duration):
            raise web.HTTPBadRequest(text="截断时间必须在视频时长范围内")
        end_frame = math.ceil(seconds * fps - 1e-7)
        if not 1 <= end_frame < round(duration * fps):
            raise web.HTTPBadRequest(text="请选择能保留至少一帧并剪掉至少一帧的时间")
        await ffmpeg("-i", src, "-vf", f"select=eq(n\\,{end_frame - 1})",
                     "-frames:v", "1", os.path.join(in_dir, name))
        return web.json_response({"image": f"{SUBFOLDER}/{name}", "end_frame": end_frame})
    await ffmpeg("-sseof", "-1", "-i", src, "-update", "1", os.path.join(in_dir, name))
    return web.json_response({"image": f"{SUBFOLDER}/{name}"})


async def probe(path, with_duration=False):
    """(fps, has_audio[, seconds]), using the picture track's exact duration."""
    with av.open(path) as media:
        video = media.streams.video[0]
        fps = float(video.average_rate or video.guessed_rate or 16)
        seconds = float(video.duration * video.time_base) if video.duration is not None else video.frames / fps
        result = fps, bool(media.streams.audio)
    return (*result, seconds) if with_duration else result


@routes.post("/easy/validation")
async def easy_validation(request):
    body = await request.json()
    path = resolve_output(body.get("subfolder"), body["filename"])
    meta = {}
    if os.path.isfile(meta_path(path)):
        try:
            with open(meta_path(path), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            pass
    labels = {"base": "基础生成", "upscale": "高清放大后", "interpolate": "补帧后"}
    report = {"filename": body["filename"], "mtime": os.path.getmtime(path), "parts": [], "notes": []}
    relay = 0
    for index, part in enumerate(meta.get("parts", []), 1):
        kind = part.get("reference", {}).get("kind", "unknown")
        relay = relay + 1 if kind == "continue" else 0
        item = {"index": index, "reference_kind": kind, "reference_image": part.get("reference", {}).get("image"), "stages": []}
        if relay > 2:
            report["notes"].append(f"镜头{index}连续接力{relay}次，存在累积偏差风险")
        for name, ref in part.get("validation", {}).items():
            if name not in labels:
                continue
            stage = {"name": name, "label": labels[name], "file": ref, "available": False}
            try:
                source = resolve_output(ref.get("subfolder"), ref["filename"])
                with av.open(source) as media:
                    stream = media.streams.video[0]
                    fps = float(stream.average_rate or stream.guessed_rate or 16)
                    stage.update(available=True, fps=fps, width=stream.width, height=stream.height,
                                 frames=stream.frames, seconds=float(stream.duration * stream.time_base) if stream.duration is not None else stream.frames / fps)
            except (web.HTTPBadRequest, OSError, ValueError, IndexError, KeyError, TypeError, AttributeError):
                stage["error"] = "文件缺失或无法读取"
            item["stages"].append(stage)
        report["parts"].append(item)
    if not any(p["stages"] for p in report["parts"]):
        report["notes"].append("这份作品未保留分阶段素材，无法从现有成片分离基础生成、放大和补帧的影响。新任务可勾选保留分阶段校验素材。")
    else:
        report["notes"].append("文件用于逐阶段人工对照，不是自动锁脸或动作修复；各阶段文件仅保留画面，可能没有声音。")
    return web.json_response(report)


@routes.post("/easy/video_info")
async def easy_video_info(request):
    body = await request.json()
    path = resolve_output(body.get("subfolder"), body["filename"])
    fps, has_audio, seconds = await probe(path, with_duration=True)
    return web.json_response({"fps": fps, "has_audio": has_audio, "seconds": seconds})


async def join_clips(parts, meta, prefix):
    """Concatenate clips in order. A part with drop_first=True continues the previous clip from its last
    frame, so its own first frame (a repeat of that frame) is cut; other parts are hard cuts.
    If any part has sound, parts without sound get silence so the track stays in sync."""
    paths = [resolve_output(p.get("subfolder"), p["filename"]) for p in parts]
    probes = [await probe(p, with_duration=True) for p in paths]
    with_audio = any(a for _, a, _ in probes)
    # talking shots are 25 fps, Wan shots 16 (or 32 with interpolation): bring all to the highest rate
    out_fps = max(f for f, _, _ in probes)
    inputs, chains, pads = [], [], ""
    starts, clock = [], 0.0
    for i, (p, path, (fps, has_audio, seconds)) in enumerate(zip(parts, paths, probes)):
        start_frame = int(bool(p.get("drop_first")))
        end_frame = p.get("end_frame")
        if end_frame is not None:
            if type(end_frame) is not int or not start_frame < end_frame <= round(seconds * fps):
                raise web.HTTPBadRequest(text="片段截断帧无效")
            seconds = end_frame / fps
        cut = start_frame / fps
        duration = max(1, int((seconds - cut) * out_fps + 0.5)) / out_fps
        starts.append(clock)
        clock += duration
        inputs += ["-i", path]
        trim = ""
        if start_frame or end_frame is not None:
            trim = f"trim=start_frame={start_frame}"
            if end_frame is not None:
                trim += f":end_frame={end_frame}"
            trim += ","
        chains.append(f"[{i}:v]{trim}setpts=PTS-STARTPTS,fps={out_fps:g}[v{i}]")
        pads += f"[v{i}]"
        if with_audio:
            if has_audio:
                # pad/cut each part's sound to its own length so later parts stay in sync
                atrim = f"atrim=start={cut:.5f}," if cut else ""
                chains.append(f"[{i}:a]{atrim}asetpts=PTS-STARTPTS,aresample=44100,aformat=channel_layouts=stereo,"
                              f"apad,atrim=0:{duration:.6f}[a{i}]")
            else:
                chains.append(f"anullsrc=r=44100:cl=stereo,atrim=0:{duration:.6f}[a{i}]")
            pads += f"[a{i}]"
    graph = ";".join(chains) + ";" + pads + \
        f"concat=n={len(parts)}:v=1:a={1 if with_audio else 0}" + ("[vc][a]" if with_audio else "[vc]") + \
        ";[vc]format=yuv420p[v]"
    name = f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    dst = os.path.join(out_dir(), name)
    maps = ["-map", "[v]"] + (["-map", "[a]", "-c:a", "aac", "-b:a", "192k"] if with_audio else [])
    await ffmpeg(*inputs, "-filter_complex", graph, *maps,
                 "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-movflags", "+faststart", dst)
    meta = {**meta, "seconds": (await probe(dst, with_duration=True))[2]}
    with open(meta_path(dst), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    return {"filename": name, "subfolder": SUBFOLDER, "meta": meta, "starts": starts}


@routes.post("/easy/concat")
async def easy_concat(request):
    """Append clip `second` to clip `first` (used by "continue this video")."""
    body = await request.json()
    parts = [body["first"], {**body["second"], "drop_first": True}]
    return web.json_response(await join_clips(parts, body.get("meta", {}), "长视频"))


@routes.post("/easy/concat_many")
async def easy_concat_many(request):
    body = await request.json()
    return web.json_response(await join_clips(body["parts"], body.get("meta", {}), body.get("prefix", "剧本视频")))


@routes.post("/easy/mix_voice")
async def easy_mix_voice(request):
    """Lay voice-over lines (each {filename, subfolder, start} in seconds) over a video, ducking its own sound."""
    body = await request.json()
    video = resolve_output(body["video"].get("subfolder"), body["video"]["filename"])
    items = body.get("items", [])
    if not items:
        return web.json_response({**body["video"], "meta": body.get("meta", {})})
    _, has_audio, seconds = await probe(video, with_duration=True)
    inputs, chains, mix = ["-i", video], [], ""
    for i, it in enumerate(items, 1):
        inputs += ["-i", resolve_output(it.get("subfolder"), it["filename"])]
        delay = max(0, int(float(it.get("start", 0)) * 1000))
        chains.append(f"[{i}:a]aresample=44100,adelay={delay}|{delay}[n{i}]")
        mix += f"[n{i}]"
    n = len(items)
    if has_audio:
        chains.append("[0:a]aresample=44100,volume=0.6[bg]")
        graph = ";".join(chains) + f";[bg]{mix}amix=inputs={n + 1}:duration=longest:normalize=0,apad[a]"
    else:
        # pad the voice track to the video's length
        graph = ";".join(chains) + f";{mix}amix=inputs={n}:duration=longest:normalize=0,apad[a]"
    stem = os.path.splitext(os.path.basename(video))[0]
    name = f"{stem}_人声.mp4"
    dst = os.path.join(os.path.dirname(video), name)
    await ffmpeg(*inputs, "-filter_complex", graph, "-map", "0:v", "-map", "[a]",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", f"{seconds:.6f}", "-movflags", "+faststart", dst)
    meta = {**body.get("meta", {}), "seconds": seconds}
    with open(meta_path(dst), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    # the version without voice is superseded in the history list
    if os.path.dirname(video) == out_dir():
        parts_dir = os.path.join(out_dir(), "parts")
        os.makedirs(parts_dir, exist_ok=True)
        for p in (video, meta_path(video)):
            if os.path.isfile(p):
                os.replace(p, os.path.join(parts_dir, os.path.basename(p)))
    sub = os.path.relpath(os.path.dirname(dst), folder_paths.get_output_directory()).replace("\\", "/")
    return web.json_response({"filename": name, "subfolder": sub, "meta": meta})


@routes.post("/easy/add_music")
async def easy_add_music(request):
    """Lay a music track under a video (mixed under its sound effects if it has any) as a new file."""
    body = await request.json()
    video = resolve_output(body["video"].get("subfolder"), body["video"]["filename"])
    music = resolve_output(body["music"].get("subfolder"), body["music"]["filename"])
    _, has_audio, length = await probe(video, with_duration=True)
    fade_at = max(0.0, length - 2.0)
    vol = float(body.get("volume", 0.35 if has_audio else 0.9))
    music_chain = f"[1:a]apad,atrim=0:{length:.6f},volume={vol},afade=t=in:d=0.8,afade=t=out:st={fade_at:.3f}:d=2,aresample=44100"
    if has_audio:
        graph = f"{music_chain}[m];[0:a]aresample=44100[s];[s][m]amix=inputs=2:duration=longest:normalize=0[a]"
    else:
        graph = f"{music_chain}[a]"
    stem = os.path.splitext(os.path.basename(video))[0]
    name = f"{stem}_配乐.mp4"
    dst = os.path.join(out_dir(), name)
    await ffmpeg("-i", video, "-i", music, "-filter_complex", graph, "-map", "0:v", "-map", "[a]",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", f"{length:.6f}", "-movflags", "+faststart", dst)
    meta = {**body.get("meta", {}), "seconds": length}
    # the version without music is superseded: move it out of the history list into parts/, and remember
    # it as `base` so "continue" can append to it and re-score the whole thing instead of cutting the music
    if body.get("replace") and os.path.dirname(video) == out_dir():
        parts_dir = os.path.join(out_dir(), "parts")
        os.makedirs(parts_dir, exist_ok=True)
        for p in (video, meta_path(video)):
            if os.path.isfile(p):
                os.replace(p, os.path.join(parts_dir, os.path.basename(p)))
        meta["base"] = {"filename": os.path.basename(video), "subfolder": f"{SUBFOLDER}/parts"}
    elif body.get("base"):
        meta["base"] = body["base"]
    with open(meta_path(dst), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    return web.json_response({"filename": name, "subfolder": SUBFOLDER, "meta": meta})


# ---------- storyboard writer: a local Qwen3-VL model run through llama.cpp's llama-server ----------
# llama.cpp lives next to the ComfyUI portable folder, e.g. D:\llama.cpp
LLAMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(folder_paths.base_path))), "llama.cpp")
LLM_MODEL = os.path.join(LLAMA_DIR, "models", "Qwen3VL-8B-Instruct-Q8_0.gguf")
LLM_MMPROJ = os.path.join(LLAMA_DIR, "models", "mmproj-Qwen3VL-8B-Instruct-F16.gguf")
LLM_PORT = 8190
llm_lock = asyncio.Lock()


def kill_stray_llm():
    """A llama-server left over from a crashed or killed ComfyUI would hold ~9 GB of VRAM; stop it."""
    import subprocess
    subprocess.run(["taskkill", "/F", "/IM", "llama-server.exe"], capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


kill_stray_llm()

SCRIPT_SYSTEM = """你是一位擅长短视频的导演兼编剧。用户会给你一个故事想法，可能还附带几张照片（按顺序编号为照片1、照片2……）。
你的任务是把想法编排成一部完整的短片，并拆分成若干个镜头，每个镜头会交给 AI 视频模型单独生成，然后按顺序拼接。

忠实于用户原意是编排的首要要求：
- 保留用户明确给出的人物及关系、地点、动作、事件顺序、结局和基调。用户已确定的细节，以原文为准。
- 扩写以拆镜头、补可见细节和选择机位为主。只补原文未指定且与原意相容的细节。简单场景可以只有一个镜头，无需增加冲突、反转或结局。
- 希望的时长、镜头建议和参考照片用于安排呈现。遇到技术限制时可以分镜或调整镜头长短，保留原事件和顺序。

AI 视频模型的特点（编排时必须遵守）：
- 每个镜头只能是 3 秒或 5 秒，画面里只能发生一个连贯的主要动作，不能在一个镜头内切换场景。
- 每个镜头有三种开始方式：
  - "photo"：从某张照片开始，画面第一帧就是那张照片。适合开场或换到照片里的场景/人物。
  - "continue"：从上一个镜头的最后一帧接着拍，同一场景、动作连续。适合同一场戏里的后续动作。
  - "text"：不用照片，纯靠文字生成，可以有人物或空镜。需要维持照片中的人物外貌时，优先使用合适的照片或接续镜头。
- 连续 "continue" 容易积累外貌和色彩偏差。有合适的照片且符合原场景时，可以切回照片。
- 视频模型看不到照片，也不知道前后剧情，所以每个镜头的画面描述都必须独立完整：写清楚画面里有谁（外貌、服装要和照片一致）、在哪里、做什么动作、表情、光线氛围、镜头运动（推近、拉远、跟随、环绕、固定机位等）。
- 动作要具体、可见、幅度适中，避免抽象的心理描写和需要文字/字幕才能表达的内容。

写作要求：
- summary 用一句中文概括镜头里的主要动作。
- prompt 是给视频模型的具体画面描述。长度以保留该镜头所需细节为准。
- 镜头总时长尽量接近用户要求的时长。
- 根据原场景选择合适的照片。照片可用来补充外观和场景细节。

转场（可选）：一个镜头还可以指定 end_photo，让镜头结束时的画面变成某张照片，模型会自动补出中间的过程
（比如从门外走进屋里坐下）。只在开始方式是 "photo" 或 "continue" 时可以用，不需要就填 0。

声音：
- 每个镜头写一句 sound，用英文描述这个镜头里应该听到的音效和环境声（例如 "footsteps on wet pavement, light rain, distant traffic"），
  不要写音乐和人声台词，音效模型会根据画面和这句话生成同步的声音。
- 整部短片写一个 music，用英文写背景音乐的风格标签（例如 "melancholic piano, soft strings, cinematic, slow"），
  再给一个合适的 bpm（60~140 的整数）。

人声（可选，用户要求或剧情需要时才加，不要每个镜头都加）：
- 旁白 narration：画外音，不需要对口型。一个镜头的旁白不超过 20 个字（约 4 秒），不需要就填空字符串。
- 台词 dialogue：画面里的人物开口说话，系统会自动让嘴型和声音同步。有台词的镜头 start 必须是 "photo" 或 "continue"，
  而且画面里要能清楚看到说话人的脸（近景或特写），prompt 里写清楚是谁在说话、表情和语气。一句台词不超过 15 个字。
  格式 {"speaker": "角色名", "line": "台词"}，不需要就填 null。同一个镜头不要同时有旁白和台词。
- voices：给每个说话的角色和"旁白"写一句中文声音描述，包括性别、年龄感、音色、语气和语速，
  例如 {"旁白": "温柔沉稳的成年女声，语速舒缓", "少年": "清亮又带点倔强的少年男声"}。
- 如果用户写了声音方面的要求，必须遵守（包括对音效、配乐和人声的要求）。

只输出 JSON，格式如下，不要输出其他任何文字：
{"title": "短片标题", "story": "两三句话的故事梗概", "music": "...", "bpm": 80, "voices": {"旁白": "..."},
 "shots": [{"summary": "...", "prompt": "...", "start": "photo|continue|text", "photo": 1, "end_photo": 0, "seconds": 5, "sound": "...",
            "narration": "", "dialogue": null}]}
其中 photo 字段只在 start 为 "photo" 时填写照片编号，其他情况填 0。第一个镜头不能是 "continue"。"""

DESCRIBE_SYSTEM = """你会看到用户上传的几张照片，它们之后会被用来制作短视频。请为每张照片写一句简洁的中文描述（30~60 字），
说清楚：画面里是谁或什么（性别、大致年龄、发型、服装等外观特征）、在什么场景、景别（特写/半身/全身/远景）和画面风格。
只输出 JSON：{"descriptions": ["照片1的描述", "照片2的描述", ...]}，数量和照片数量一致。"""


def comfy_busy():
    return PromptServer.instance.prompt_queue.get_tasks_remaining() > 0


def free_comfy_vram():
    import comfy.model_management as mm
    mm.unload_all_models()
    mm.soft_empty_cache()


def image_data_url(name):
    """Downscale an image for the vision model and return it as a JPEG data URL.
    `name` is a LoadImage-style name: relative to input/, or ending in " [output]" for output/."""
    import base64
    import io
    from PIL import Image, ImageOps
    root = folder_paths.get_input_directory()
    if name.endswith(" [output]"):
        name, root = name[:-len(" [output]")], folder_paths.get_output_directory()
    path = os.path.realpath(os.path.join(root, name))
    if not path.startswith(os.path.realpath(root) + os.sep):
        raise web.HTTPBadRequest(text="bad image path")
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    img.thumbnail((768, 768))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


async def run_llm(messages):
    """Start llama-server, run one chat completion, then stop it so the GPU is free for video again."""
    import aiohttp
    import subprocess
    exe = os.path.join(LLAMA_DIR, "bin", "llama-server.exe")
    for p in (exe, LLM_MODEL, LLM_MMPROJ):
        if not os.path.isfile(p):
            raise web.HTTPServiceUnavailable(text=f"编剧模型还没装好，缺少文件：{p}")
    kill_stray_llm()
    proc = subprocess.Popen(
        [exe, "-m", LLM_MODEL, "--mmproj", LLM_MMPROJ, "-ngl", "99", "-c", "32768", "--jinja",
         "--host", "127.0.0.1", "--port", str(LLM_PORT)],
        cwd=os.path.join(LLAMA_DIR, "bin"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    base = f"http://127.0.0.1:{LLM_PORT}"
    try:
        async with aiohttp.ClientSession() as s:
            for _ in range(180):
                if proc.poll() is not None:
                    raise web.HTTPInternalServerError(text="编剧模型启动失败")
                try:
                    async with s.get(base + "/health") as r:
                        if r.status == 200:
                            break
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(1)
            else:
                raise web.HTTPGatewayTimeout(text="编剧模型启动超时")
            payload = {"messages": messages, "temperature": 0.8, "top_p": 0.9, "max_tokens": 24000,
                       "response_format": {"type": "json_object"}}
            async with s.post(base + "/v1/chat/completions", json=payload,
                              timeout=aiohttp.ClientTimeout(total=1800)) as r:
                data = await r.json()
                if r.status != 200:
                    raise web.HTTPInternalServerError(text=json.dumps(data, ensure_ascii=False)[:1000])
        return data["choices"][0]["message"]["content"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def script_has_voice(script):
    return any(
        bool(str(shot.get("narration") or "").strip()) or
        (shot.get("start") in ("photo", "continue") and isinstance(shot.get("dialogue"), dict)
         and bool(str(shot["dialogue"].get("line") or "").strip()))
        for shot in script.get("shots", []) if isinstance(shot, dict)
    )


@routes.post("/easy/script")
async def easy_script(request):
    body = await request.json()
    if comfy_busy():
        return web.json_response({"error": "视频正在生成中，显卡被占用。请等当前任务结束后再编排剧本。"}, status=409)
    photos = body.get("photos", [])
    orientation = {"480x832": "竖屏", "832x480": "横屏", "624x624": "方形"}.get(body.get("size"), "竖屏")
    seconds = int(body.get("seconds") or 0)
    if seconds > 0:
        length_req = f"总时长必须接近 {seconds} 秒（约 {max(1, round(seconds / 4.5))} 个镜头），按这个篇幅安排剧情的节奏和详略。"
    else:
        length_req = "总时长由你根据剧情需要来决定：剧情简单就短一些，丰富就长一些，不要为了凑时长而拖沓。"
    voice_policy = body.get("voice_policy", "optional")
    audio_req = body.get("audio_req", "").strip()
    if voice_policy == "required":
        audio_req += "；必须至少给一个镜头安排非空的旁白或人物台词，并在 voices 中填写对应声音；音效和纯音乐不算人声。没有人物照片时只安排旁白。"
    elif voice_policy == "off":
        audio_req += "；不要任何旁白或人物台词，所有 narration 为空字符串，dialogue 为 null。"
    audio_req = f"\n\n声音方面的要求：{audio_req}\n" if audio_req else "\n\n"
    content = [{"type": "text", "text":
                f"故事想法：{body['idea']}{audio_req}{length_req}画面是{orientation}。"
                f"{f'附带 {len(photos)} 张照片，按顺序是：' if photos else '没有附带照片。'}"}]
    for i, p in enumerate(photos, 1):
        note = f"（用户备注：{p['note']}）" if p.get("note") else ""
        content.append({"type": "text", "text": f"照片{i}{note}："})
        content.append({"type": "image_url", "image_url": {"url": image_data_url(p["image"])}})
    messages = [{"role": "system", "content": SCRIPT_SYSTEM}, {"role": "user", "content": content}]
    response = await llm_json(messages, "编剧模型返回的格式不对，请再试一次。")
    if response.status != 200:
        return response
    script = json.loads(response.text)
    if not isinstance(script, dict) or not isinstance(script.get("shots"), list) or not script["shots"] or any(
        not isinstance(shot, dict) or not isinstance(shot.get("prompt"), str) or not shot["prompt"].strip()
        for shot in script["shots"]
    ):
        return web.json_response({"error": "编剧模型没有返回有效分镜。可以重试，或点「用原文建镜头」。",
                                  "raw": response.text[:2000]}, status=502)
    if voice_policy == "required" and not script_has_voice(script):
        messages += [{"role": "assistant", "content": json.dumps(script, ensure_ascii=False)},
                     {"role": "user", "content": "刚才遗漏了必须的人声。按原镜头顺序给出等长的 narrations 字符串数组，每个位置填写符合用户原意的简短旁白或空字符串，至少一处非空。只输出 JSON，包含 narrations 和 voices。"}]
        response = await llm_json(messages, "补充人声失败，请重试。")
        if response.status != 200:
            return response
        additions = json.loads(response.text)
        narrations = additions.get("narrations") if isinstance(additions, dict) else None
        if isinstance(narrations, list) and len(narrations) == len(script["shots"]) and all(
            isinstance(line, str) for line in narrations
        ):
            for shot, line in zip(script["shots"], narrations):
                if line.strip():
                    shot["narration"] = line
            voices = additions.get("voices")
            if isinstance(voices, dict) and isinstance(voices.get("旁白"), str):
                script["voices"] = {"旁白": voices["旁白"]}
        if not script_has_voice(script):
            return web.json_response({"error": "已要求人声，但编剧仍未给出旁白或台词。请重新编排，或关闭人声后手动添加。"}, status=502)
    if voice_policy == "off":
        for shot in script.get("shots", []):
            shot["narration"], shot["dialogue"] = "", None
        script["voices"] = {}
    return web.json_response(script)


@routes.post("/easy/describe")
async def easy_describe(request):
    """Describe each photo in one short sentence so the user does not have to label them."""
    body = await request.json()
    images = body.get("images", [])
    if not images:
        return web.json_response({"descriptions": []})
    if comfy_busy():
        return web.json_response({"error": "显卡正在生成视频，等任务结束后再自动识别照片。"}, status=409)
    content = [{"type": "text", "text": f"共 {len(images)} 张照片："}]
    for i, name in enumerate(images, 1):
        content.append({"type": "text", "text": f"照片{i}："})
        content.append({"type": "image_url", "image_url": {"url": image_data_url(name)}})
    messages = [{"role": "system", "content": DESCRIBE_SYSTEM}, {"role": "user", "content": content}]
    return await llm_json(messages, "识别照片失败，请再试一次。")


async def llm_json(messages, bad_format_msg):
    async with llm_lock:
        await asyncio.get_running_loop().run_in_executor(None, free_comfy_vram)
        text = await run_llm(messages)
    # tolerate stray text or a <think> block around the JSON
    start, end = text.find("{"), text.rfind("}")
    try:
        return web.json_response(json.loads(text[start:end + 1]))
    except ValueError:
        return web.json_response({"error": bad_format_msg, "raw": text[:2000]}, status=502)


# ---------- nodes ----------
class EasyMMAudioSampler:
    """MMAudio sampler that takes frames at any frame rate.

    ComfyUI-MMAudio's own sampler slices the frames as if they were already 25 fps, so 16/32 fps Wan
    clips come out with sound out of sync. This resamples to MMAudio's 8 fps (CLIP) and 25 fps
    (Synchformer) streams by time, then runs the same generation."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "mmaudio_model": ("MMAUDIO_MODEL",),
            "feature_utils": ("MMAUDIO_FEATUREUTILS",),
            "images": ("IMAGE",),
            "fps": ("FLOAT", {"default": 16.0, "min": 1.0, "max": 120.0, "step": 0.01}),
            "steps": ("INT", {"default": 25, "min": 1, "max": 200}),
            "cfg": ("FLOAT", {"default": 4.5, "min": 0.0, "max": 20.0, "step": 0.1}),
            "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
            "prompt": ("STRING", {"default": "", "multiline": True}),
            "negative_prompt": ("STRING", {"default": "music, speech, talking, singing", "multiline": True}),
        }}

    RETURN_TYPES = ("AUDIO",)
    FUNCTION = "sample"
    CATEGORY = "easy_video_ui"

    @staticmethod
    def _mmaudio_module():
        # match on the file path: some torch.ops namespaces in sys.modules answer hasattr() with True for anything
        import types
        for mod in list(sys.modules.values()):
            if (isinstance(mod, types.ModuleType) and "ComfyUI-MMAudio" in (getattr(mod, "__file__", None) or "")
                    and isinstance(mod.__dict__.get("MMAudioSampler"), type) and "FlowMatching" in mod.__dict__):
                return mod
        raise RuntimeError("ComfyUI-MMAudio is not installed")

    def sample(self, mmaudio_model, feature_utils, images, fps, steps, cfg, seed, prompt, negative_prompt):
        import torch
        import comfy.model_management as mm
        mod = self._mmaudio_module()
        device = mm.get_torch_device()
        n = images.shape[0]
        duration = n / fps

        def at_rate(rate):
            # resized on the CPU so upscaled clips never put full-size frames on the GPU
            idx = (torch.arange(int(rate * duration)) * fps / rate).long().clamp(max=n - 1)
            return images[idx].float().cpu().permute(0, 3, 1, 2)

        # same preprocessing as ComfyUI-MMAudio, done with plain torch ops (torchvision's v2 transforms
        # call compiled torchvision ops that this PyTorch build does not provide); clamping matches its PIL round trip
        import torch.nn.functional as F
        clip = F.interpolate(at_rate(8.0), size=(384, 384), mode="bicubic", antialias=True, align_corners=False).clamp(0, 1)
        frames25 = at_rate(25.0)
        h, w = frames25.shape[-2:]
        scale = 224 / min(h, w)   # shorter side to 224, then center crop
        nh, nw = max(224, round(h * scale)), max(224, round(w * scale))
        sync = F.interpolate(frames25, size=(nh, nw), mode="bicubic", antialias=True, align_corners=False).clamp(0, 1)
        top, left = (nh - 224) // 2, (nw - 224) // 2
        sync = (sync[:, :, top:top + 224, left:left + 224] - 0.5) / 0.5
        clip_frames = clip.unsqueeze(0).to(device)
        sync_frames = sync.unsqueeze(0).to(device)

        rng = torch.Generator(device=device)
        rng.manual_seed(seed)
        seq_cfg = mmaudio_model.seq_cfg
        seq_cfg.duration = duration
        mmaudio_model.update_seq_lengths(seq_cfg.latent_seq_len, seq_cfg.clip_seq_len, seq_cfg.sync_seq_len)
        feature_utils.to(device)
        mmaudio_model.to(device)
        audios = mod.generate(clip_frames, sync_frames, [prompt], negative_text=[negative_prompt],
                              feature_utils=feature_utils, net=mmaudio_model,
                              fm=mod.FlowMatching(min_sigma=0, inference_mode="euler", num_steps=steps),
                              rng=rng, cfg_strength=cfg)
        offload = mm.unet_offload_device()
        mmaudio_model.to(offload)
        feature_utils.to(offload)
        mm.soft_empty_cache()
        return ({"waveform": audios.float().cpu(), "sample_rate": 44100},)


NODE_CLASS_MAPPINGS = {"EasyMMAudioSampler": EasyMMAudioSampler}
NODE_DISPLAY_NAME_MAPPINGS = {"EasyMMAudioSampler": "MMAudio Sampler (any fps)"}
