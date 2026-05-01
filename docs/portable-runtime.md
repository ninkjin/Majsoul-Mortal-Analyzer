# 便携运行环境

这个项目不要把 `.conda`、`.venv` 或 `runtime` 提交进 Git。Git 仓库只保存代码、依赖清单和脚本。

开发者 clone/fork 后可以运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup-runtime.ps1
```

这个脚本会创建 `runtime\python.exe`，安装 `torch` 等依赖，并用 `maturin` 编译安装 `libriichi`。完成后，`启动雀魂分析器.cmd` 会优先使用 `runtime\python.exe`，不需要 Docker Desktop。

构建 runtime 的机器需要先装好 Python 3.12 和 Rust/Cargo。普通用户使用 release zip 时不需要装 Rust。

给普通用户分发时，先在你的机器上运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package-portable.ps1
```

生成的 `dist\mortal-paipu-analyzer-portable.zip` 才是给普通用户下载的便携版。用户解压后双击 `启动雀魂分析器.cmd` 即可。

如果没有 `runtime`，服务会尝试 Docker 的 `mortal:latest` 镜像作为回退。
