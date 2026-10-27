# Claude Science 原生程序放置位置

请把当前操作系统对应的原生程序放入本目录。CSswitch 会把这个目录作为最高优先级
发现路径，放入后即可直接运行，无需再修改 `settings.science_bin`。

## Windows

放入以下任一文件名：

```text
runtime\claude-science\claude-science.exe
runtime\claude-science\Claude Science.exe
```

## macOS / Linux

放入：

```text
runtime/claude-science/claude-science
```

并赋予执行权限：

```bash
chmod +x runtime/claude-science/claude-science
runtime/claude-science/claude-science --version
```

## 跨平台注意事项

- 必须使用与 CPU、操作系统匹配的原生构建。
- 不要把 Windows `.exe` 放到 macOS 或 Linux 机器上使用。
- 不要把 macOS / Linux 的无扩展名二进制复制到 Windows 上使用。
- 如果暂时不想把程序放在这里，也可以安装到系统常见位置，或在面板「路径设置」中
  手动指定 `science_bin`。

本仓库不会跟踪这里的可执行文件，只跟踪本说明。
