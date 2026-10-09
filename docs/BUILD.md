# 在 Windows 上编译绿色 EXE

## 构建环境

- Windows x64；本地验证环境为 Windows 11。
- Python 3.13 x64，安装时包含 Tcl/Tk。先用 `python -m tkinter` 检查 Tk 窗口是否能打开。
- PowerShell 5.1 或 PowerShell 7。
- 首次构建需要访问 Python 包索引；后续可利用本地 pip 缓存。

请在源码仓库根目录运行以下命令。

## 推荐：一键构建

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1
```

也可明确指定 Python：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1 -PythonPath C:\Python313\python.exe
```

脚本会检查 Python 版本/架构，创建 `.venv`，安装 `requirements-build.txt` 的固定依赖，
执行隔离测试，生成 Windows 版本资源，运行 PyInstaller，最后用白名单打包。
所有输出位于仓库内的 `.venv/`、`build/` 和 `dist/`，不会安装到系统 Python。

已经准备好依赖后，可以跳过重新安装：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1 -SkipDependencyInstall
```

开发迭代可使用 `-SkipTests`；公开发布应使用默认完整构建。

## 手动构建

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
$env:PYTHONPATH = (Resolve-Path .\src).Path
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe .\scripts\write_version_info.py .\build\windows-version.txt
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --name FolderFlow --paths src --distpath build/binary --workpath build/pyinstaller --specpath build --manifest packaging/windows.manifest --version-file build/windows-version.txt src/launcher.py
& .\.venv\Scripts\python.exe .\scripts\package_release.py
```

手动构建时，请确认每条命令成功后再执行下一条。自动脚本会在错误处立即停止。

## 输出与绿色行为

- `dist/FolderFlow/`：可直接运行的软件目录。
- `dist/FolderFlow-2.3.0-windows-x64-portable.zip`：干净、可公开分发的压缩包。
- `dist/SHA256SUMS.txt`：ZIP 的 SHA-256 校验值。

成品 EXE 包含 Python/Tk，不要求最终用户安装依赖，不需要管理员权限。
Windows manifest 使用 `asInvoker`，启用长路径支持；构建包含版本号和产品信息。
程序状态只保存在其旁边的 `data/`、`reports/`，启动过程中有单文件运行库的临时解压。

ZIP 只包含 `scripts/package_release.py` 中列出的应用、说明和许可证。
即使你运行过 `dist/FolderFlow/FolderFlow.exe` 并生成个人状态，重新打包也不会把它们加入 ZIP。
不要用“压缩整个运行目录”的方式制作公开 Release。

## 维护构建

版本号维护在 `src/folderflow/__init__.py`，同时更新 `pyproject.toml`、Windows manifest 和发布说明。
更换 Python 或 PyInstaller 时，要更新依赖版本、第三方许可证并重新验证绿色包。
当前提交固定依赖版本，但不同 Python 补丁版本/构建环境的 EXE 不保证字节一致；以 SHA-256 识别具体产物。

PyInstaller 官方文档：[使用方式](https://pyinstaller.org/en/stable/usage.html)、[单文件行为](https://pyinstaller.org/en/stable/operating-mode.html)。
