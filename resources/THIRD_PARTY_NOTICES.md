# Third-party runtime notices

Local Cut's Windows service sidecar includes the CPython runtime selected by the Windows packaging build and the PyInstaller one-file bootloader. The corresponding license texts are distributed in `licenses/Python-LICENSE.txt` and `licenses/PyInstaller-COPYING.txt` inside the installed application's resources.

PyInstaller is distributed under GNU GPL version 2 or later with a bootloader exception. The exact license text used by the pinned PyInstaller build is included with the installer. The Python Software Foundation license bundled with the selected CPython runtime is also included verbatim.

The Tauri application also bundles production JavaScript dependencies. `licenses/JavaScript-THIRD-PARTY.txt` is generated from the locked, installed production dependency graph before each Tauri build and includes package versions, declared license identifiers, and the license/notice files shipped by each package.

The Windows Tauri application bundles the locked Rust dependency graph. `licenses/Rust-THIRD-PARTY.txt` is generated from the Windows-target Cargo dependency tree and includes each crate's version, declared license expression, and available license/notice text. For the six locked crates whose published source archives omit separate license files, version-pinned upstream or license-steward texts are included from `license-overrides/rust-license-overrides.json`; each override checks the exact Cargo source revision and a checked-in SHA-256 digest. These supplements provide notice text but do not replace review of attribution, source-availability, or other license obligations.

FFmpeg, speech-recognition weights, and creator-reference media are not included in the installer.

`licenses/Python-THIRD-PARTY.txt` is generated from the exact sidecar build environment and includes installed Python dependency names, versions, declared license metadata, and shipped license/notice files. Build-tool notices are included conservatively as well. This covers packages such as jsonschema, attrs, referencing, and rpds-py; packages without separate installed license files are identified for review.

The optional sample project is generated locally from FFmpeg test patterns and a sine-wave tone, with Local Cut-authored demonstration transcript text and captions. These demonstration additions are offered under CC0-1.0; they contain no downloaded footage, music, speech model, or third-party creator assets. The tone is not speech and the demo transcript is not an inference result.

This inventory does not replace review of the Rust/Cargo dependency licenses or separately acquired FFmpeg and model licenses. Do not treat it as legal approval for distribution.
