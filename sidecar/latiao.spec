# -*- mode: python -*-
a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('agents/*.txt', 'agents'),
        ('skills/', 'skills'),
        ('plugins/', 'plugins'),
        ('.env', '.'),
    ],
    hiddenimports=[
        'uvicorn.logging', 'uvicorn.loops', 'uvicorn.protocols',
        'fastapi', 'httpx', 'certifi',
        'sqlite3', 'asyncio',
        'pyyaml',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['llama_cpp', 'mlx_lm', 'torch', 'numpy', 'tensorflow'],
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
