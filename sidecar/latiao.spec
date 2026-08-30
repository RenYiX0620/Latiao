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
        'pyyaml', 'yaml',
        # 统一能力模型 + 生态市场（多处函数内动态 import，PyInstaller 静态分析
        # 收集不到，必须显式列出，否则 sidecar.exe 运行时报 ModuleNotFoundError）
        'capability_registry', 'discovery', 'adapters',
        'mcp_client', 'extension_manager',
        'cron', 'identity', 'memory', 'local_llm', 'db', 'config',
        'tool_system', 'tool_executor',
        # mx_query 金融工具：--mx-query 模式下需要 import（目录已改为合法包名 mx_data）
        'skills', 'skills.mx_data', 'skills.mx_data.mx_data',
        # 控制类插件（plugins/ 下由 tool_system 动态加载，需随包）
        '_control_common', '_control_mouse_common',
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
