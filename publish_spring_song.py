"""
《二零二六春风暖》歌曲视频 — 全平台发布 / 重试失败平台

用法:
  python publish_spring_song.py              # 发布全部平台
  python publish_spring_song.py --retry      # 重试失败平台
  python publish_spring_song.py --platform channels  # 只发布指定平台
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
from shared.utils.logger import get_logger
logger = get_logger("spring_song")

OUTPUT_DIR = PROJECT_ROOT / "output" / "二零二六春风暖"

def _write_json(p, d):
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

def _get_platform_config():
    from xiaohongshu.publisher import publish_video_note
    from douyin.publisher import publish_douyin_video
    from channels.publisher import publish_channels_video
    from weibo.publisher import publish_weibo_video
    return [
        ("XiaoHongShu", "xhs_content.json",
         lambda d, vp: publish_video_note(vp, d["title"], d["full_text"], headless=False)),
        ("Douyin", "dy_content.json",
         lambda d, vp: publish_douyin_video(vp, d["title"], d["full_text"], headless=False)),
        ("Channels", "channels_content.json",
         lambda d, vp: publish_channels_video(vp, d["full_text"], headless=False)),
        ("Weibo", "weibo_content.json",
         lambda d, vp: publish_weibo_video(vp, d["title"], d["full_text"], headless=False)),
    ]

def _run_publish(platforms, video_path, results):
    for name, loader, publisher in platforms:
        cf = OUTPUT_DIR / loader
        if not cf.exists():
            results[name] = "SKIP"
            continue
        try:
            data = json.loads(cf.read_text(encoding="utf-8"))
            logger.info("正在发布到 %s ...", name)
            ok = publisher(data, video_path)
            results[name] = "OK" if ok else "UNCERTAIN"
            logger.info("%s -> %s", name, results[name])
        except Exception as e:
            results[name] = f"FAIL: {e}"
            logger.error("%s 失败: %s", name, e)
        time.sleep(5)
    _write_json(OUTPUT_DIR / "publish_results.json", results)
    _print(results)

def _print(results):
    logger.info("=" * 50)
    for name, status in results.items():
        icon = "[OK]" if status == "OK" else "[??]" if "UNCERTAIN" in str(status) else "[!!]"
        logger.info("  %s %-15s %s", icon, name, status)
    ok = sum(1 for s in results.values() if s == "OK")
    logger.info("  %d/%d 成功", ok, len(results))
    logger.info("=" * 50)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--platform", type=str, default=None)
    args = parser.parse_args()

    video = None
    for f in OUTPUT_DIR.iterdir():
        if f.suffix == ".mp4":
            video = str(f); break
    if not video:
        logger.error("未找到 MP4: %s", OUTPUT_DIR); sys.exit(1)

    logger.info("视频: %s (%.1f MB)", Path(video).name, Path(video).stat().st_size/1024/1024)

    all_platforms = _get_platform_config()
    rf = OUTPUT_DIR / "publish_results.json"

    if args.platform:
        platforms = [(n,l,f) for n,l,f in all_platforms if n.lower() == args.platform.lower()]
        if not platforms:
            logger.error("未知平台: %s", args.platform); sys.exit(1)
        results = json.loads(rf.read_text(encoding="utf-8")) if rf.exists() else {}
        _run_publish(platforms, video, results)
    elif args.retry:
        if not rf.exists():
            logger.info("无历史记录，发布全部")
            _run_publish(all_platforms, video, {})
            return
        last = json.loads(rf.read_text(encoding="utf-8"))
        failed = {k:v for k,v in last.items() if v != "OK"}
        if not failed:
            logger.info("所有平台已成功!"); return
        retry = [(n,l,f) for n,l,f in all_platforms if n in failed]
        logger.info("重试: %s", ", ".join(failed.keys()))
        _run_publish(retry, video, dict(last))
    else:
        _run_publish(all_platforms, video, {})

if __name__ == "__main__":
    main()
