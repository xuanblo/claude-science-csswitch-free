# `runtime/` 运行时占位目录

这个仓库只跟踪本目录及下级目录里的 `README.md` 占位说明，**不跟踪大型可执行文件**。
这样 Git 仓库保持轻量，也不会把 Windows、macOS、Linux 三种平台二进制混在一起。

完整目录结构：

```text
runtime/
├─ README.md                  # 本说明，仓库跟踪
├─ claude-science/
│  ├─ README.md               # 仓库跟踪的放置说明
│  └─ claude-science(.exe)    # 由你下载后放入，仓库不跟踪
└─ python/
   ├─ README.md               # 仓库跟踪的放置说明
   └─ Python 便携运行时        # 由你下载或从完整便携包复制，仓库不跟踪
```

## 下载 Claude Science 后直接运行

1. 下载与你当前操作系统匹配的**原生 Claude Science**。
2. 按 [claude-science/README.md](claude-science/README.md) 指定文件名放入：

   - Windows：

     ```text
     runtime\claude-science\claude-science.exe
     ```

   - macOS / Linux：

     ```text
     runtime/claude-science/claude-science
     ```

     然后执行：

     ```bash
     chmod +x runtime/claude-science/claude-science
     ```

3. 准备便携 Python。Windows 启动器固定使用：

   ```text
   runtime\python\python.exe
   ```

   macOS / Linux 启动器会优先使用：

   ```text
   runtime/python/bin/python3
   runtime/python/bin/python
   ```

4. 直接运行对应启动器：

   - Windows：双击 `便携启动.bat`
   - macOS / Linux：`./启动.sh`

启动器会自动优先发现 `runtime/claude-science/` 里的原生程序。Windows 程序
`.exe` 不能在 macOS 或 Linux 上运行，反之 Unix 无扩展名二进制也不能复制到
Windows 使用。

> 如果你只是拿到一个 Git 仓库，本目录里的 `README.md` 只是占位说明，并不会随
> `git clone` 自动获得 Python 和 Claude Science 二进制。请从完整便携发行包复制
> 这两个运行时，或按照各子目录说明自行放入。
