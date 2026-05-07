# 便携运行环境

这个项目不要把 `.conda`、`.venv` 或 `runtime` 提交进 Git。Git 仓库只保存代码、依赖清单和脚本。

开发者 clone/fork 后可以运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup-runtime.ps1
```

这个脚本会检查并补齐本地 `.conda\python.exe`，安装 `torch` 等依赖，并用 `maturin` 编译安装 `libriichi`。

构建便携包的机器需要先准备好 `.conda\python.exe` 和 Rust/Cargo。普通用户使用 release zip 时不需要装 Rust。

给普通用户分发时，先在你的机器上运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package-portable.ps1
```

打包脚本会把本地 `.conda` 复制成发布包里的 `runtime`。生成的 `dist\mortal-paipu-analyzer-portable.zip` 才是给普通用户下载的便携版。用户解压后双击 `启动雀魂分析器.cmd` 即可。

不要用普通 `python -m venv runtime` 生成发布包。venv 会在 `pyvenv.cfg` 里记录构建机器上的 Python 路径，移动到用户电脑后可能报类似 `No Python at 'D:\...\ .conda\python.exe'` 的错误。

如果没有 `runtime`，服务会尝试 Docker 的 `mortal:latest` 镜像作为回退。
