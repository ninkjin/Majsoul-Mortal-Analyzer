# Unity 内置 Claude Code 终端设计

## 目标

在 Unity 编辑器里增加一个 EditorWindow，让用户可以直接在 Unity 内和 Claude Code 交互，避免每次都切换到外部 cmd 窗口。

第一版默认启动命令为：

```text
claude
```

这个终端面板只用于 Unity 编辑器工具，不用于游戏运行时 UI。

## 范围

第一版需要提供：

- Unity 编辑器菜单入口，例如 `Window/Claude Code Terminal`。
- 一个可停靠的 EditorWindow 面板。
- 可滚动的终端输出区域。
- 底部单行输入框，按 Enter 后发送输入。
- Start、Stop、Restart、Clear 控制按钮。
- 会话状态显示：空闲、启动中、运行中、已退出、错误。
- 中文和英文输出的 UTF-8 支持。
- 可配置启动命令，默认值为 `claude`。

第一版暂不做：

- 完整 ANSI 颜色和光标定位渲染。
- 和 Windows Terminal 完全一致的鼠标选择体验。
- vim、nano 这类全屏终端程序支持。
- 多终端标签页。
- 完整 shell 快捷键和命令历史导航。

## 架构

使用两个进程：

```text
Unity EditorWindow
  <-> localhost TCP JSON Lines
Claude Terminal Bridge
  <-> Windows ConPTY
cmd.exe / claude
```

Unity 负责编辑器界面、用户输入、设置项和显示状态。桥接程序负责真正创建伪终端，启动命令，读取 PTY 输出，并把 Unity 的输入写回 PTY。

这样做可以避免只依赖 `System.Diagnostics.Process` 带来的交互式终端问题。Claude Code 这类工具通常需要真实终端语义，单纯重定向 stdin/stdout 不一定可靠。

## 组件

### ClaudeTerminalWindow

Unity 编辑器窗口，负责：

- 渲染终端输出。
- 捕获用户输入。
- 把输入发送给桥接程序。
- 显示连接状态和进程状态。
- 暴露基础设置，例如启动命令。

### ClaudeTerminalClient

Unity 侧本地通信客户端，负责：

- 连接 `127.0.0.1` 上的桥接程序 TCP 服务。
- 桥接程序退出时尝试重连或显示错误。
- 发送用户输入和控制消息。
- 接收输出片段和进程状态事件。

消息格式使用换行分隔 JSON，也就是 JSON Lines。终端输出文本放在 JSON 字符串字段里，这样终端自身的换行不会和消息边界冲突。

### ClaudeTerminalBridge

本地辅助进程，负责：

- 通过 Windows ConPTY 启动终端命令。
- 把 PTY 输出转发给 Unity。
- 把 Unity 输入写回 PTY。
- 处理进程退出。
- 保持 UTF-8 文本正确传输。

桥接程序使用一个小型 .NET console app 实现。它可以和 Unity 编辑器工具一起分发，也可以通过设置项引用外部路径。

## 数据流

1. 用户打开 `Window/Claude Code Terminal`。
2. 用户点击 Start。
3. Unity 启动或连接桥接程序。
4. 桥接程序在 PTY 中启动 `claude`。
5. PTY 输出流入桥接程序。
6. 桥接程序把输出片段发送给 Unity。
7. Unity 把输出追加到终端滚动缓冲区。
8. 用户输入内容并按 Enter。
9. Unity 把输入和换行发送给桥接程序。
10. 桥接程序把输入写入 PTY。

## 错误处理

Unity 需要清楚显示这些错误：

- 找不到桥接程序。
- 桥接程序启动失败。
- 找不到 `claude` 命令。
- 本地连接断开。
- 终端进程已退出。

Stop 按钮应尽量干净地终止当前 Claude 会话。Restart 应先停止当前会话，清理临时连接状态，然后启动新会话。

## 设置

最少需要支持这些设置：

- 启动命令：默认 `claude`。
- 工作目录：默认 Unity 项目根目录。
- 滚动缓冲限制：限制最大行数或字符数，避免 Unity 编辑器变慢。

这些设置可以存储在 `EditorPrefs`，作为当前机器的本地偏好。

## 测试

第一版手动验证：

- 能打开 Unity 编辑器终端窗口。
- 点击 Start 后能启动 `claude`。
- 能发送一个简单提示词。
- 输出能流式显示在 Unity 面板里，不需要切换窗口。
- 中文输入和中文输出能正常显示。
- Stop 和 Restart 能正常工作。
- 命令缺失、桥接程序缺失等错误能显示可读提示。

如果把传输消息解析和滚动缓冲裁剪拆成独立逻辑，可以为这些部分补自动化测试。
