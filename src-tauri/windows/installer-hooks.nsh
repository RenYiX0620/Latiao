; Latiao NSIS 安装钩子
;
; 背景（09-13 用户实测报错）：
;   Error opening file for writing:
;   ...\Latiao\sidecar\sidecar.exe
;   Windows 版 sidecar.exe 与本地引擎（llama-server.exe）都是**独立子进程**；
;   更新安装时安装程序只强杀主程序 Latiao.exe，退出钩子 kill_sidecar_on_exit
;   来不及执行 → 残留 sidecar 进程锁住自身文件 → 覆盖写入失败。
;
; 处理：拷贝文件前先结束这些残留进程（/T 连同子进程），并留出句柄释放时间。

!macro _LatiaoKill name
  nsExec::Exec 'taskkill /F /IM ${name} /T'
  Pop $0
!macroend

!macro NSIS_HOOK_PREINSTALL
  DetailPrint "Latiao: stopping running sidecar and local engine..."
  !insertmacro _LatiaoKill "sidecar.exe"
  !insertmacro _LatiaoKill "llama-server.exe"
  !insertmacro _LatiaoKill "Latiao.exe"
  Sleep 800
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  DetailPrint "Latiao: stopping processes before uninstall..."
  !insertmacro _LatiaoKill "sidecar.exe"
  !insertmacro _LatiaoKill "llama-server.exe"
  Sleep 500
!macroend
