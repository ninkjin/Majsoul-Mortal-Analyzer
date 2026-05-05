# Unity 内置 Claude Code 终端 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Unity 编辑器里增加一个可停靠的 Claude Code 终端面板，启动命令默认为 `claude`，让用户不用切换到外部 cmd。

**Architecture:** Unity 侧只负责 EditorWindow、设置、滚动输出和输入发送；本地 .NET bridge 负责通过 Windows ConPTY 启动交互式命令，并通过 `127.0.0.1` TCP JSON Lines 与 Unity 通信。第一版直接使用伪终端，不用普通 stdin/stdout 重定向冒充终端。

**Tech Stack:** Unity Editor IMGUI、C#、.NET 8 console app、TCP、JSON Lines、NUnit/Unity Test Framework。

---

## 文件结构

Unity 工程路径：`C:\Users\Administrator\Graduation project`

- Create: `C:\Users\Administrator\Graduation project\Tools\ClaudeTerminalBridge\ClaudeTerminalBridge.csproj`
- Create: `C:\Users\Administrator\Graduation project\Tools\ClaudeTerminalBridge\Program.cs`
- Create: `C:\Users\Administrator\Graduation project\Assets\Editor\ClaudeTerminal\ClaudeTerminalMessage.cs`
- Create: `C:\Users\Administrator\Graduation project\Assets\Editor\ClaudeTerminal\ClaudeTerminalClient.cs`
- Create: `C:\Users\Administrator\Graduation project\Assets\Editor\ClaudeTerminal\ClaudeTerminalWindow.cs`
- Create: `C:\Users\Administrator\Graduation project\Assets\Editor\ClaudeTerminal\Tests\ClaudeTerminalMessageTests.cs`

Bridge 源码放在 Unity 工程根目录的 `Tools` 下，不放进 `Assets`，避免 Unity 把 .NET console app 当成编辑器脚本编译。

### Task 1: Bridge 最小可运行服务

**Files:**
- Create: `C:\Users\Administrator\Graduation project\Tools\ClaudeTerminalBridge\ClaudeTerminalBridge.csproj`
- Create: `C:\Users\Administrator\Graduation project\Tools\ClaudeTerminalBridge\Program.cs`

- [ ] **Step 1: 创建 bridge 项目文件**

写入 `ClaudeTerminalBridge.csproj`：

```xml
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <ImplicitUsings>enable</ImplicitUsings>
    <Nullable>enable</Nullable>
  </PropertyGroup>
</Project>
```

- [ ] **Step 2: 写入 bridge 主程序**

`Program.cs` 提供 TCP 服务、启动命令、JSON Lines 输入输出。消息协议：

```json
{"type":"output","data":"text"}
{"type":"status","data":"running"}
{"type":"error","data":"message"}
{"type":"input","data":"user text\n"}
{"type":"stop"}
```

实现需要包含：

- `ConPtySession`：封装 `CreatePseudoConsole`、`CreateProcess`、PTY 输入管道、PTY 输出管道和进程清理。
- `BridgeServer`：监听 `127.0.0.1:<port>`，接受一个 Unity 客户端连接。
- `BridgeMessage`：处理 JSON Lines 消息。
- `CommandLine`：把 `--port`、`--command`、`--working-directory` 参数解析出来。

`ConPtySession` 对外只暴露 `Start()`、`WriteInputAsync()`、`Stop()` 和 `OutputReceived`，让 Unity 侧协议不依赖 Windows API 细节。

- [ ] **Step 3: 编译 bridge**

Run:

```powershell
dotnet build "C:\Users\Administrator\Graduation project\Tools\ClaudeTerminalBridge\ClaudeTerminalBridge.csproj"
```

Expected: build succeeds with 0 errors.

- [ ] **Step 4: 手动 smoke test bridge**

Run:

```powershell
dotnet run --project "C:\Users\Administrator\Graduation project\Tools\ClaudeTerminalBridge\ClaudeTerminalBridge.csproj" -- --port 50557 --command "cmd.exe /k" --working-directory "C:\Users\Administrator\Graduation project"
```

Expected: console prints that it is listening on `127.0.0.1:50557`. A TCP client can send `{"type":"input","data":"echo bridge-ok\n"}` and receives an output event containing `bridge-ok`.

### Task 2: Unity 消息模型和测试

**Files:**
- Create: `C:\Users\Administrator\Graduation project\Assets\Editor\ClaudeTerminal\ClaudeTerminalMessage.cs`
- Create: `C:\Users\Administrator\Graduation project\Assets\Editor\ClaudeTerminal\Tests\ClaudeTerminalMessageTests.cs`

- [ ] **Step 1: 写消息模型测试**

测试覆盖输入、输出、错误三种消息的 JSON Lines 序列化和反序列化：

```csharp
using NUnit.Framework;

namespace ClaudeTerminal.Editor.Tests
{
    public sealed class ClaudeTerminalMessageTests
    {
        [Test]
        public void RoundTripOutputMessageKeepsNewlines()
        {
            var message = new ClaudeTerminalMessage("output", "第一行\nsecond line");
            var json = message.ToJsonLine();
            var parsed = ClaudeTerminalMessage.FromJsonLine(json);

            Assert.AreEqual("output", parsed.Type);
            Assert.AreEqual("第一行\nsecond line", parsed.Data);
        }
    }
}
```

- [ ] **Step 2: 写消息模型实现**

`ClaudeTerminalMessage` 使用 `UnityEngine.JsonUtility`，字段名固定为 `type` 和 `data`，`ToJsonLine()` 保证末尾有一个 `\n`。

- [ ] **Step 3: 刷新 Unity 并确认无编译错误**

Run through Unity MCP: `refresh_unity` with scripts compilation.

Expected: Unity console has no C# compile errors.

### Task 3: Unity TCP 客户端

**Files:**
- Create: `C:\Users\Administrator\Graduation project\Assets\Editor\ClaudeTerminal\ClaudeTerminalClient.cs`

- [ ] **Step 1: 实现客户端状态和事件**

`ClaudeTerminalClient` 暴露：

```csharp
public bool IsConnected { get; }
public string LastError { get; }
public event Action<string> OutputReceived;
public event Action<string> StatusChanged;
public event Action<string> ErrorReceived;
```

- [ ] **Step 2: 实现连接和读循环**

使用 `TcpClient.ConnectAsync("127.0.0.1", port)` 连接 bridge。后台线程按行读取 JSON，解析成 `ClaudeTerminalMessage`，再通过线程安全队列交给 EditorWindow 在主线程消费。

- [ ] **Step 3: 实现发送输入和停止**

`SendInput(string text)` 发送 `{"type":"input","data":text}`；`StopRemoteProcess()` 发送 `{"type":"stop","data":""}`。

- [ ] **Step 4: 刷新 Unity 并确认无编译错误**

Run through Unity MCP: `refresh_unity` with scripts compilation.

Expected: Unity console has no C# compile errors.

### Task 4: Unity EditorWindow 面板

**Files:**
- Create: `C:\Users\Administrator\Graduation project\Assets\Editor\ClaudeTerminal\ClaudeTerminalWindow.cs`

- [ ] **Step 1: 创建菜单和窗口骨架**

菜单路径：

```csharp
[MenuItem("Window/Claude Code Terminal")]
public static void Open()
```

窗口标题为 `Claude Terminal`。

- [ ] **Step 2: 实现设置区**

使用 `EditorPrefs` 保存：

- `ClaudeTerminal.Command`，默认 `claude`
- `ClaudeTerminal.WorkingDirectory`，默认 Unity 项目根目录
- `ClaudeTerminal.BridgeProjectPath`，默认 `Tools/ClaudeTerminalBridge/ClaudeTerminalBridge.csproj`
- `ClaudeTerminal.Port`，默认 `50557`
- `ClaudeTerminal.ScrollbackLimit`，默认 `200000`

- [ ] **Step 3: 实现 Start/Stop/Restart/Clear**

Start 用 `dotnet run --project "<bridge csproj>" -- --port <port> --command "<command>" --working-directory "<working dir>"` 启动 bridge，然后连接 TCP。

Stop 先发送 stop 消息，再关闭本地连接并终止 bridge 进程。

Restart 执行 Stop 后再 Start。

Clear 清空输出缓冲。

- [ ] **Step 4: 实现终端输出和输入框**

输出区使用深色背景和等宽字体；输入框按 Enter 发送当前文本加 `\n`，发送后清空输入框。

- [ ] **Step 5: 刷新 Unity 并确认菜单出现**

Run through Unity MCP: `refresh_unity` with scripts compilation.

Expected: Unity console has no C# compile errors, `Window/Claude Code Terminal` 可用。

### Task 5: 端到端验证

**Files:**
- Modify as needed: Task 1-4 created files only.

- [ ] **Step 1: 打开 Unity 面板**

通过 Unity 菜单打开 `Window/Claude Code Terminal`。

Expected: 面板出现，显示 idle 状态。

- [ ] **Step 2: 测试简单命令**

临时把启动命令设为：

```text
cmd.exe /k
```

Start 后输入：

```text
echo unity-terminal-ok
```

Expected: 输出区出现 `unity-terminal-ok`。

- [ ] **Step 3: 测试 Claude Code**

把启动命令改回：

```text
claude
```

Start 后确认 Claude Code 输出出现在面板内。

- [ ] **Step 4: 测试中文输入输出**

输入：

```text
请回复：中文测试
```

Expected: 面板内能看到中文输入和中文输出。

- [ ] **Step 5: 测试停止和重启**

点击 Stop，然后 Restart。

Expected: 状态从 running 变为 exited/idle，再回到 running；Unity Console 无新编译错误。
