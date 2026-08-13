# -*- mode: python -*-
a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('agents/*.txt', 'agents'),
        ('skills/', 'skills'),
        ('plugins/', 'plugins'),
        # 注意：不要把 .env 打进 exe（含密钥）
    ],
    hiddenimports=[
        'uvicorn.logging', 'uvicorn.loops', 'uvicorn.protocols',
        'fastapi', 'httpx', 'certifi',
        'sqlite3', 'asyncio',
        'pyyaml',
        # mx_query 金融工具：--mx-query 模式下需要 import（目录已改为合法包名 mx_data）
        'skills', 'skills.mx_data', 'skills.mx_data.mx_data',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['llama_cpp', 'mlx_lm', 'torch', 'tensorflow'],
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name='sidecar',
    console=False,
    debug=False,
)
