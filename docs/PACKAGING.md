# Windows sidecar packaging

For a clean environment with networking disabled, see [Sandbox validation](SANDBOX_VALIDATION.md). The preparation command stages an installer-only QA input bundle with optional media tools, and the guest-only runner checks install, installed-service smoke, GUI startup and uninstall while keeping the full-workflow gate explicitly unverified. Windows Sandbox is unavailable on the development host, so the generated configuration has not been run there.

## Current local candidate

The final 2026-10-21 source builds a Windows x64 NSIS installer at `src-tauri/target/release/bundle/nsis/Local Cut_0.1.0_x64-setup.exe`.

- Size: 14,827,971 bytes (14.14 MiB).
- SHA-256: `87cc0b960a2e5d2b0b0d70dff242d48fbfe1f8a492d8bba47c9e993234c89888`.
- Full incremental build: 146.90 seconds; optimized Rust phase: 1 minute 26 seconds.
- Packaged service smoke: passed in 4.66 seconds, including protocol/health, project create/reopen, locally generated sample creation, editable timeline restoration, OTIO export, a normalized 480p30 H.264 preview through the persistent queue, and clean shutdown.

The artifact is an unsigned local release candidate. These checks run the bundled service and build the native installer, but do not establish clean-machine install/uninstall behavior or packaged desktop privacy under operating-system-level network denial. No installed user application was replaced during validation. Hashes apply only to this build and must be regenerated after source changes.

The Tauri release build bundles the local Python service as `localcut-service-x86_64-pc-windows-msvc.exe`. `tauri.conf.json` registers `binaries/localcut-service` as an external binary; the Rust supervisor launches it through Tauri's shell plugin in release builds. Debug builds keep using the source Python module. The service speaks the same newline-delimited JSON protocol in both modes.

`npm run tauri build` runs the frontend build, packages the sidecar with `scripts/build-sidecar.ps1`, and runs `npm run licenses:prepare`. The license preparation traverses the installed production JavaScript dependency graph rather than including test/build-only packages, then fetches and enumerates the locked Windows-target Cargo graph (normal and build dependencies; dev dependencies excluded). It records declared license metadata and includes available source license/notice files in `resources/licenses/JavaScript-THIRD-PARTY.txt` and `resources/licenses/Rust-THIRD-PARTY.txt`, which Tauri embeds with the static notices. Six locked Rust source archives omit separate license texts; `resources/license-overrides/rust-license-overrides.json` supplies texts from the exact upstream Cargo source revisions (or the MPL steward), checks each text's SHA-256, and embeds the result in the generated report. This resolves missing notice text only; distribution obligations and legal suitability still require review, especially source availability for MPL-covered code. The sidecar script creates an isolated Python environment under `service/.sidecar-build-venv`, installs pinned PyInstaller 6.22.3, packages `service/sidecar_entry.py` as a one-file console executable, and stages it under `src-tauri/binaries/`. It also stages the selected Python runtime's `LICENSE.txt` and PyInstaller's exact `COPYING.txt`; Tauri embeds these alongside `resources/THIRD_PARTY_NOTICES.md`. Generated license copies, the staged binary, and intermediate output are ignored by Git and regenerated during packaging. Building requires Windows x64, Python 3.11 or newer, Node/npm, Rust stable for `x86_64-pc-windows-msvc`, the Visual C++ linker and runtime libraries, the Windows SDK desktop headers/libraries (including `kernel32.lib`), and network access for missing PyPI/crates.io build dependencies. The current Windows host now has the required SDK/linker and completes the optimized release and NSIS build. Runtime editing does not require Python or a network connection. FFmpeg and model files remain separately acquired.

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

On 2026-10-21, the Windows x64 optimized Tauri application and NSIS installer built successfully. The initial full build's Rust release phase took 1 minute 31 seconds and produced `src-tauri/target/release/bundle/nsis/Local Cut_0.1.0_x64-setup.exe` (14.09 MiB before the final release-control additions). `npm run service:smoke` passed protocol v1, local health, project creation, last-project reopening, and clean shutdown against the generated executable. Rebuild after any source change; the artifact is not a substitute for clean-machine installer validation.

The installer is unsigned. Clean Windows install/uninstall, SmartScreen behavior, WebView2 bootstrap without internet, non-admin installation, and full packaged-app network-deny acceptance remain release gates. Runtime FFmpeg/model dependencies remain separately acquired. The generated notice inventories cover JavaScript, Python dependencies/Python/PyInstaller, and Windows Cargo graphs; supplemental texts address missing license files, while legal obligations review and source availability remain open.
