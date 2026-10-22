"""Stage installed Python distribution license metadata/texts for packaging."""
from importlib import metadata
from pathlib import Path
import sys


def main():
    destination = Path(sys.argv[1])
    sections = ['Local Cut Python dependency notices', 'Generated from the exact sidecar build environment. Includes build tools conservatively; inclusion is not a legal obligations review.']
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata.get('Name', '').casefold()):
        name = dist.metadata.get('Name', 'unknown')
        sections += ['', '=' * 72, f'{name} {dist.version}', f"License: {dist.metadata.get('License-Expression') or dist.metadata.get('License') or 'See bundled license text'}"]
        if dist.metadata.get('Home-page'):
            sections.append(f"Project: {dist.metadata['Home-page']}")
        found = False
        for item in sorted(dist.files or [], key=str):
            relative = Path(str(item))
            if any(part in ('..', '') for part in relative.parts):
                continue
            basename = relative.name.casefold()
            if not (basename.startswith(('license', 'copying', 'notice', 'copyright')) or 'licenses' in relative.parts):
                continue
            source = Path(dist.locate_file(item))
            if source.is_file() and source.stat().st_size < 2_000_000:
                sections += ['', f'--- {item} ---', source.read_text(encoding='utf-8', errors='replace')]
                found = True
        if not found:
            sections.append('No separate license file found in installed package; review declared license and upstream source before public distribution.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text('\n'.join(sections) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
