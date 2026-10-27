# Python 便携运行时放置位置

本项目的启动器默认使用项目内便携 Python，避免依赖用户系统 Python、Conda 环境或
管理员权限。

## Windows 必须路径

```text
runtime\python\python.exe
```

推荐 Python 3.11，最低要求 Python 3.9。Windows 版 `便携启动.bat` 会固定查找上面
这个程序。

### 方式 A：从完整便携发行包复制（推荐）

把完整便携包中的整个 `runtime\python` 目录复制到仓库的 `runtime\python`，保留
`Lib`、`DLLs`、`Scripts` 与已安装的 `cryptography`。复制后运行：

```powershell
runtime\python\python.exe -c "import sys, cryptography; print(sys.version)"
```

### 方式 B：自己准备 Embeddable Python

1. 下载 Windows 的 Python 3.11 amd64 Embeddable ZIP。
2. 解压到 `runtime\python`。
3. 编辑 `python311._pth`，取消下面一行的注释以启用 `site-packages`：

   ```text
   #import site
   ```

   修改后应为：

   ```text
   import site
   ```

4. 按 Python 官方 Embeddable 包流程安装 `pip`，然后执行：

   ```powershell
   runtime\python\python.exe -m pip install -r requirements.txt
   runtime\python\python.exe -c "import cryptography; print('OK')"
   ```

## macOS / Linux 可选路径

Unix 启动器会优先查找：

```text
runtime/python/bin/python3
runtime/python/bin/python
```

可使用与系统架构匹配的便携 Python 发行包，但必须安装 `cryptography`：

```bash
runtime/python/bin/python3 -m pip install -r requirements.txt
runtime/python/bin/python3 -c "import cryptography; print('OK')"
```

若本机已经有 Python 3.9+，也可以不准备便携 Python；`启动.sh` 会回退使用
`CSSWITCH_PYTHON` 或系统 `python3`。

## 为什么 Git 仓库没有这些文件

Python 解释器、标准库、DLL 与站点包合计体积很大，而且各平台二进制不同；GitHub
普通仓库不适合直接提交这类运行时。仓库只保留目录占位说明，完整便携发行包再通过
网盘、Release 附件或其他分发渠道携带。
