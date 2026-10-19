# Windows sidecar packaging

The Tauri release build bundles the local Python service as `localcut-service-x86_64-pc-windows-msvc.exe`. `tauri.conf.json` registers `binaries/localcut-service` as an external binary; the Rust supervisor launches it through Tauri's shell plugin in release builds. Debug builds keep using the source Python module. The service speaks the same newline-delimited JSON protocol in both modes.

`npm run tauri build` runs the frontend build, packages the sidecar with `scripts/build-sidecar.ps1`, and runs `npm run licenses:prepare`. The license preparation traverses the installed production JavaScript dependency graph rather than including test/build-only packages, then fetches and enumerates the locked Windows-target Cargo graph (normal and build dependencies; dev dependencies excluded). It records declared license metadata and includes available source license/notice files in `resources/licenses/JavaScript-THIRD-PARTY.txt` and `resources/licenses/Rust-THIRD-PARTY.txt`, which Tauri embeds with the static notices. Six locked Rust source archives omit separate license texts; `resources/license-overrides/rust-license-overrides.json` supplies texts from the exact upstream Cargo source revisions (or the MPL steward), checks each text's SHA-256, and embeds the result in the generated report. This resolves missing notice text only; distribution obligations and legal suitability still require review, especially source availability for MPL-covered code. The sidecar script creates an isolated Python environment under `service/.sidecar-build-venv`, installs pinned PyInstaller 6.22.3, packages `service/sidecar_entry.py` as a one-file console executable, and stages it under `src-tauri/binaries/`. It also stages the selected Python runtime's `LICENSE.txt` and PyInstaller's exact `COPYING.txt`; Tauri embeds these alongside `resources/THIRD_PARTY_NOTICES.md`. Generated license copies, the staged binary, and intermediate output are ignored by Git and regenerated during packaging. Building requires Windows x64, Python 3.11 or newer, Node/npm, Rust stable for `x86_64-pc-windows-msvc`, the Visual C++ linker and runtime libraries, the Windows SDK desktop headers/libraries (including `kernel32.lib`), and network access for missing PyPI/crates.io build dependencies. This machine has Visual Studio's linker but no discoverable Windows SDK libraries; Cargo fails at `LNK1181: cannot open input file 'kernel32.lib'`. Installing the Windows SDK may require elevation. Runtime editing does not require Python or a network connection. FFmpeg and model files remain separately acquired.

To build only the sidecar on the supported Windows host:

```powershell
npm run service:sidecar
```

The standalone protocol smoke check used during development is:

```powershell
$env:LOCAL_CUT_DATA_DIR = Join-Path $env:TEMP 'localcut-sidecar-smoke'
'{"protocolVersion":1,"requestId":1,"method":"ping","params":{}}' | .\src-tauri\binaries\localcut-service-x86_64-pc-windows-msvc.exe
```

Run `npm run service:smoke` to check the packaged binary end-to-end. It creates a unique temporary data directory, verifies protocol compatibility and local health, creates a project, verifies last-project reopening, closes stdin and checks clean shutdown, then removes only its own temporary directory. Run `npm run service:sidecar` first if the staged executable is missing or stale.

The generated executable was verified to start and answer `ping` after a local sidecar rebuild. A Tauri/NSIS release build has been attempted with the Rust toolchain and Visual Studio linker exposed; Rust compilation reaches linking but fails because the Windows SDK `kernel32.lib` is missing. The generated notice inventories cover the JavaScript, Python/PyInstaller, and Windows Cargo dependency graphs; six missing source-archive license files now have version-checked supplemental texts, but a legal obligations review is still needed. Complete installer validation, install/uninstall, FFmpeg and model licensing, offline behavior, and full packaged-app network-deny acceptance remain open; this document does not claim release approval.
