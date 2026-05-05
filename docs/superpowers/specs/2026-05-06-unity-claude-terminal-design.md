# Unity Claude Terminal Design

## Goal

Add a Unity EditorWindow that lets the user interact with Claude Code from inside the Unity Editor, avoiding the need to switch to an external cmd window. The first supported startup command is:

```text
claude
```

The terminal is intended for editor tooling, not for runtime gameplay UI.

## Scope

The first version should provide:

- A Unity editor menu entry such as `Window/Claude Code Terminal`.
- A dockable EditorWindow with a scrollable terminal output area.
- A single-line input field that sends text when Enter is pressed.
- Start, Stop, Restart, and Clear controls.
- Session status display: idle, starting, running, exited, or error.
- UTF-8 text handling for Chinese and English output.
- A configurable launch command, defaulting to `claude`.

The first version does not need:

- Full ANSI color and cursor-position rendering.
- Mouse selection behavior matching Windows Terminal.
- Full-screen terminal programs such as vim or nano.
- Multiple terminal tabs.
- Complete shell keybindings or command history navigation.

## Architecture

Use a two-process design:

```text
Unity EditorWindow
  <-> localhost TCP JSON Lines
Claude Terminal Bridge
  <-> Windows ConPTY
cmd.exe / claude
```

Unity owns the editor UI, user input, settings, and display state. The bridge owns the real pseudo-terminal process, launches the command, reads PTY output, and forwards input to the PTY.

This separation avoids relying on `System.Diagnostics.Process` alone for interactive terminal behavior. Claude Code expects terminal semantics that plain redirected stdin/stdout may not provide reliably.

## Components

### ClaudeTerminalWindow

Unity editor window responsible for:

- Rendering terminal output.
- Capturing typed commands.
- Sending user input to the bridge.
- Showing connection and process state.
- Exposing basic settings such as launch command.

### ClaudeTerminalClient

Unity-side transport client responsible for:

- Connecting to the local bridge over `127.0.0.1` TCP.
- Reconnecting or reporting errors when the bridge exits.
- Sending typed input and control messages.
- Receiving output chunks and process status events.

Messages should use newline-delimited JSON. Output text is carried as JSON string data so regular terminal newlines do not conflict with message framing.

### ClaudeTerminalBridge

Local helper process responsible for:

- Starting the terminal command using Windows ConPTY.
- Forwarding PTY output to Unity.
- Writing Unity input back to the PTY.
- Handling process termination.
- Preserving UTF-8 text.

The bridge should be implemented as a small .NET console app. It should be distributed beside the Unity editor tooling or referenced from a configurable path.

## Data Flow

1. User opens `Window/Claude Code Terminal`.
2. User presses Start.
3. Unity starts or connects to the bridge.
4. Bridge launches `claude` inside a PTY.
5. PTY output streams to the bridge.
6. Bridge sends output chunks to Unity.
7. Unity appends output to the scrollback buffer.
8. User types input and presses Enter.
9. Unity sends input plus newline to the bridge.
10. Bridge writes the input to the PTY.

## Error Handling

Unity should show clear errors for:

- Bridge executable missing.
- Bridge failed to start.
- Command `claude` not found.
- Connection lost.
- Terminal process exited.

The Stop control should terminate the Claude session cleanly where possible. Restart should stop the current session, clear transient connection state, then start a new session.

## Settings

Minimum settings:

- Launch command: default `claude`.
- Working directory: default Unity project root.
- Scrollback limit: default a bounded number of lines or characters to avoid editor slowdown.

Settings may be stored in `EditorPrefs` for machine-local preferences.

## Testing

Manual verification for the first version:

- Open the Unity editor window.
- Start a session and confirm `claude` launches.
- Send a simple prompt.
- Confirm streamed output appears without switching windows.
- Confirm Chinese input and output render correctly.
- Stop and restart the session.
- Confirm missing command and bridge failure display readable errors.

Automated tests can cover transport message parsing and scrollback trimming if those parts are separated from Unity GUI rendering.
