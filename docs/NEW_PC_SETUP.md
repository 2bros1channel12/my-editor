# Work on Local Cut from another PC

This repository contains the application source, documentation, dependency lockfiles, and tests. It does not contain installed dependencies, build output, credentials, model weights, source footage, or private editing-project databases.

## Windows development setup

The first release targets Windows 11 x64. Install Git, Node.js 22 with npm, Python 3.11 or newer, Rust stable with the MSVC toolchain, Visual Studio Build Tools with Desktop development with C++ and a Windows SDK, and Microsoft Edge WebView2 Runtime. Restart your terminal after installing prerequisites. FFmpeg and FFprobe are needed for importing and processing media; keep them on PATH or configure them in Local Cut. Whisper-compatible transcription and local generation/embedding providers are optional, separately installed features; see TRANSCRIPTION.md and MODEL_RUNTIME_AND_INDEX.md.

1. Sign in to the GitHub account with access to the private repository, then clone it using its green Code button (GitHub Desktop or Git).
2. Open PowerShell in the cloned repository root.
3. Install the JavaScript/Python dependencies and start the desktop app:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ./scripts/dev.ps1 -Install
```

On subsequent runs:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ./scripts/dev.ps1
```

The launcher selects this checkout's virtual environment and service source automatically. Tauri starts Vite through its existing development configuration. If Rust cannot locate the MSVC linker, run from the Visual Studio Developer PowerShell or repair the C++ build tools installation. Browser-only `npm run dev` can show the UI but does not provide the native Tauri service bridge.

## Keep source work synchronized

Before editing on a PC, pull the latest commits. Commit and push finished work before switching PCs. Do not copy node_modules, virtual environments, or Rust target folders between machines; recreate them using the lockfiles and commands above. Installing dependencies does not download AI model weights automatically.

## Move an actual video-editing project

GitHub source synchronization does not transfer imported videos, native `.localcut` projects, transcripts, cached proxies, models, or exports. Close Local Cut before copying a project folder with its database and recovery files using your chosen private transfer method. Linked source files must be copied separately and relinked in Projects on the other PC. Project-managed sources under the native project's media folder travel with the folder. Keep the original copy until the transferred project opens successfully. Regenerate caches and acquire local models as needed. Do not edit the same native project simultaneously from two PCs.

## Development checks

```powershell
npm test
npm run build
./service/.venv/Scripts/python.exe -m pytest service/tests
cargo test --manifest-path src-tauri/Cargo.toml --lib
```

For packaging and remaining release limits, see PACKAGING.md. The repository currently provides a development app, not a verified ready-to-install release.
