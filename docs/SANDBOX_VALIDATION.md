# Isolated Windows installer validation

The development host currently has no `WindowsSandbox.exe` and no available current-user code-signing certificate. Installer build and direct bundled-service tests do not prove a clean-machine installation or packaged GUI privacy. This harness is prepared for a suitable Windows 11 host; it has **not been executed in Sandbox**.

From the checkout run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/prepare-sandbox-validation.ps1 -IncludeMediaTools
```

This creates a unique ignored `validation-artifacts/sandbox-<UUID>/` containing only a copy of the built installer, its SHA-256 manifest, validation scripts, optional locally installed FFmpeg/FFprobe, and an empty output directory. Media tools are copied for private QA, not added to the distributable installer. No source footage, model weights, credentials or private editing projects are mapped. DLL-based media-tool distributions may need additional runtime files; missing dependencies must be reported as failures, not silently downloaded.

The generated `.wsb` follows the [Microsoft Sandbox configuration](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file) format: networking, clipboard redirection and vGPU disabled; 16 GiB requested memory; input mapping read-only; only the dedicated output directory writable. If transferred to another host, regenerate from that host's checkout or update both `HostFolder` paths to the transferred directories. Do not map a home directory or project-media folder as writable output.

Open the `.wsb` on a Sandbox-capable host. The guest-only runner refuses to install outside the default Sandbox account and exact mapped input path. It verifies the installer hash, attempts a silent installation into a fresh guest-local directory, runs smoke tests against the **installed** service, attempts native GUI startup, closes that window and uninstalls the app. Results are written to `output/validation.json` and `output/installed-service-smoke.txt`. A missing WebView2 runtime or offline installer bootstrap failure is captured as a failed gate. The runner never enables networking or installs extra prerequisites.

Inspect every result and error. A native window handle proves only that a window appeared, not a functioning WebView or correct playback. The `fullWorkflowValidated` field remains false: human verification of native controls/playback and the full real-inference workflow is still required. Sandbox disables the guest network adapter but this harness does not capture traffic or prove external model-runtime privacy. It does not validate a standard non-administrator account (Sandbox's default account differs), signing/SmartScreen, independent timing accuracy, or user acceptance.

The preparation script and guest runner can be syntax/manifest checked on the current development host without executing an installer. A real Sandbox run, clean non-admin VM, representative corpus and beta participants remain external release requirements.
