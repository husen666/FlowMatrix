import sys, json, os, datetime
sys.stdout.reconfigure(encoding='utf-8')

# 1. 检查上次成功发布时间
print("=== 上次成功发布记录 ===")
base = r'D:\project\aineoo.com\code\output'
for root, dirs, files in os.walk(base):
    for f in files:
        if f == 'publish_results.json':
            fp = os.path.join(root, f)
            mtime = os.path.getmtime(fp)
            dt = datetime.datetime.fromtimestamp(mtime)
            data = json.loads(open(fp, encoding='utf-8').read())
            rel = os.path.relpath(root, base)
            ok = sum(1 for v in data.values() if v == 'OK')
            total = len(data)
            print(f"  {dt:%Y-%m-%d %H:%M} | {rel} | {ok}/{total} OK")

# 2. 检查 Python 版本
print(f"\n=== Python ===")
print(f"  版本: {sys.version}")
print(f"  路径: {sys.executable}")

# 3. 检查 pip 最近安装/更新记录
print(f"\n=== 关键依赖版本 ===")
import importlib.metadata
for pkg in ['playwright', 'greenlet', 'pyee', 'typing-extensions']:
    try:
        v = importlib.metadata.version(pkg)
        print(f"  {pkg}: {v}")
    except:
        print(f"  {pkg}: 未安装")

# 4. 检查是否有多个 Python
print(f"\n=== 检查 Playwright greenlet 兼容性 ===")
import greenlet
print(f"  greenlet C extension: {hasattr(greenlet, '_C_API')}")
print(f"  greenlet compiled for: {getattr(greenlet, '__file__', 'N/A')}")

# 5. 检查 publisher 文件是否被修改
print(f"\n=== publisher.py 最近修改时间 ===")
code_base = r'D:\project\aineoo.com\code'
for sub in ['xiaohongshu', 'douyin', 'channels', 'zhihu', 'toutiao', 'weibo']:
    fp = os.path.join(code_base, sub, 'publisher.py')
    if os.path.exists(fp):
        mtime = os.path.getmtime(fp)
        dt = datetime.datetime.fromtimestamp(mtime)
        print(f"  {sub}/publisher.py: {dt:%Y-%m-%d %H:%M}")

# 6. 检查 shared/publisher_base.py
fp = os.path.join(code_base, 'shared', 'publisher_base.py')
if os.path.exists(fp):
    mtime = os.path.getmtime(fp)
    dt = datetime.datetime.fromtimestamp(mtime)
    print(f"  shared/publisher_base.py: {dt:%Y-%m-%d %H:%M}")
