"""ffmpeg / ffprobe 探测与媒体处理（抽音轨、抽帧、九宫格拼图）。"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

from .config import Settings

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _playwright_ffmpeg() -> str:
    """Playwright 自带的 ffmpeg（Windows 下 ms-playwright/ffmpeg-*/ffmpeg-win64.exe）。"""
    local = os.environ.get("LOCALAPPDATA") or ""
    if not local:
        return ""
    root = Path(local) / "ms-playwright"
    if not root.is_dir():
        return ""
    candidates: list[Path] = []
    for child in sorted(root.glob("ffmpeg-*"), reverse=True):
        candidates.extend(child.glob("ffmpeg-win64.exe"))
        candidates.extend(child.glob("ffmpeg-linux"))
        candidates.extend(child.glob("ffmpeg-mac*"))
    for path in candidates:
        if path.is_file():
            return str(path)
    return ""


def _usable(path: str | Path) -> bool:
    """真实可执行文件才算数。

    Windows 下 `shutil.which("ffmpeg")` 常命中
    `%LOCALAPPDATA%\\Microsoft\\WindowsApps\\ffmpeg.exe` —— 那是应用商店的 0 字节占位符，
    真跑起来只会弹商店，所以要排除掉。
    """
    try:
        p = Path(path)
    except TypeError:
        return False
    if not p.is_file():
        return False
    if "windowsapps" in str(p).lower():
        return False
    try:
        return p.stat().st_size > 64 * 1024
    except OSError:
        return False


def find_ffmpeg(settings: Settings) -> str:
    """按 配置 → 环境变量 → ffprobe 同目录 → PATH → Playwright → imageio_ffmpeg 找 ffmpeg。"""
    explicit = (settings.ffmpeg_path or "").strip().strip('"')
    if explicit:
        path = Path(explicit)
        if _usable(path):
            return str(path)
        found = shutil.which(explicit)
        if found and _usable(found):
            return found

    env = os.environ.get("FFMPEG_PATH") or os.environ.get("FFMPEG_BINARY") or ""
    if env.strip() and _usable(env.strip().strip('"')):
        return env.strip().strip('"')

    # ffprobe 常在 PATH 上（本机是 C:\ffmpeg\bin\ffprobe.exe），同目录的 ffmpeg 才是真身
    probe = shutil.which("ffprobe")
    if probe:
        sibling = Path(probe).with_name("ffmpeg.exe" if probe.lower().endswith(".exe") else "ffmpeg")
        if _usable(sibling):
            return str(sibling)

    found = shutil.which("ffmpeg")
    if found and _usable(found):
        return found

    bundled = _playwright_ffmpeg()
    if bundled and _usable(bundled):
        return bundled

    try:  # 最后兜底：imageio-ffmpeg 常随依赖被装进来
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and _usable(exe):
            return exe
    except Exception:
        pass
    return ""


def find_ffprobe(settings: Settings) -> str:
    """ffprobe 一般和 ffmpeg 同目录；找不到时返回空串（时长会改用容器信息兜底）。"""
    explicit = (settings.ffmpeg_path or "").strip().strip('"')
    if explicit:
        path = Path(explicit)
        probe = path.with_name("ffprobe" + (".exe" if path.suffix.lower() == ".exe" else ""))
        if _usable(probe):
            return str(probe)

    found = shutil.which("ffprobe")
    if found and _usable(found):
        return found

    ffmpeg = find_ffmpeg(settings)
    if ffmpeg:
        path = Path(ffmpeg)
        probe = path.with_name("ffprobe.exe" if path.suffix.lower() == ".exe" else "ffprobe")
        if _usable(probe):
            return str(probe)
    return ""


async def _run(args: list[str], timeout: float = 180.0) -> tuple[int, bytes, bytes]:
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise RuntimeError(f"命令超时：{args[0]}")
    return proc.returncode or 0, out or b"", err or b""


async def probe_duration(path: Path, settings: Settings) -> float:
    """返回秒数；ffprobe 不可用或失败时返回 0。"""
    probe = find_ffprobe(settings)
    if not probe:
        return 0.0
    rc, out, _ = await _run(
        [
            probe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout=60.0,
    )
    if rc != 0 or not out:
        return 0.0
    try:
        return max(0.0, float(out.decode("utf-8", "ignore").strip()))
    except ValueError:
        return 0.0


async def extract_audio(
    video: Path,
    out_wav: Path,
    settings: Settings,
    max_seconds: int,
    ffmpeg: str | None = None,
) -> Path | None:
    """抽 16k 单声道 PCM 音轨；超过 max_seconds 只取前段。失败返回 None。"""
    exe = ffmpeg or find_ffmpeg(settings)
    if not exe:
        return None
    args = [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video)]
    if max_seconds > 0:
        args += ["-t", str(max_seconds)]
    args += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_wav)]
    try:
        rc, _, _ = await _run(args, timeout=300.0)
    except Exception:
        return None
    return out_wav if rc == 0 and out_wav.exists() and out_wav.stat().st_size > 1024 else None


async def extract_frame(
    video: Path,
    timestamp: float,
    out_png: Path,
    width: int,
    settings: Settings,
    ffmpeg: str | None = None,
) -> Path | None:
    exe = ffmpeg or find_ffmpeg(settings)
    if not exe:
        return None
    args = [
        exe, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, timestamp):.3f}",
        "-i", str(video),
        "-frames:v", "1",
        "-vf", f"scale={width}:-2",
        str(out_png),
    ]
    try:
        rc, _, _ = await _run(args, timeout=120.0)
    except Exception:
        return None
    return out_png if rc == 0 and out_png.exists() and out_png.stat().st_size > 512 else None


def _load_font(size: int):
    from PIL import ImageFont

    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "DejaVuSans.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def build_montage(frames: list[Path], out_png: Path, labels: list[str] | None = None) -> Path | None:
    """把 N 张帧拼成一张九宫格（含时间标签）。同步函数，调用方自行丢线程池。"""
    if not frames:
        return None
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None

    images: list = []
    for path in frames:
        try:
            img = Image.open(path).convert("RGB")
        except Exception:
            continue
        images.append(img)
    if not images:
        return None

    count = len(images)
    cols = 3 if count > 4 else 2
    rows = (count + cols - 1) // cols
    cell_w = max(img.width for img in images)
    cell_h = max(img.height for img in images)
    pad = 6
    label_h = 26

    canvas = Image.new(
        "RGB",
        (cols * cell_w + pad * (cols + 1), rows * (cell_h + label_h) + pad * (rows + 1)),
        (18, 18, 20),
    )
    draw = ImageDraw.Draw(canvas)
    font = _load_font(18)

    for idx, img in enumerate(images):
        row, col = divmod(idx, cols)
        x = pad + col * (cell_w + pad) + (cell_w - img.width) // 2
        y = pad + row * (cell_h + label_h + pad)
        canvas.paste(img, (x, y))
        text = labels[idx] if labels and idx < len(labels) else f"#{idx + 1}"
        draw.text((pad + col * (cell_w + pad) + 4, y + cell_h + 3), text, fill=(235, 235, 235), font=font)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    if out_png.suffix.lower() in {".jpg", ".jpeg"}:
        canvas.save(out_png, format="JPEG", quality=88, optimize=True)
    else:
        canvas.save(out_png, format="PNG", optimize=True)
    return out_png
