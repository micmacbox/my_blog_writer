"""Validate numbered posting assets and export a manifest; never controls a browser."""
import argparse
import json
import re
import unicodedata
from pathlib import Path


def existing_path(path):
    """Resolve composed/decomposed macOS path segments without renaming files."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        candidate = current / part
        if not candidate.exists():
            candidate = next((p for p in current.iterdir()
                              if unicodedata.normalize('NFC', p.name) ==
                              unicodedata.normalize('NFC', part)), candidate)
        current = candidate
    return current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    source = existing_path(args.source.expanduser().resolve())
    if not source.is_dir():
        parser.error(f'업로드 폴더가 없습니다: {source}')
    manuscript = source / '원고.txt'
    # Directory names exported on macOS may be decomposed Unicode.
    entries = {unicodedata.normalize('NFC', p.name): p for p in source.iterdir()}
    manuscript = entries.get('원고.txt', manuscript)
    photos = entries.get('사진')
    if not manuscript.is_file():
        parser.error(f'원고.txt가 없습니다: {source}')
    text = manuscript.read_text(encoding='utf-8-sig')
    markers = [int(n) for n in re.findall(r'\[사진\s*(\d+)(?:\s*:|\s+첨부\s*위치)', text)]
    files = {}
    for path in sorted(photos.iterdir() if photos else []):
        match = re.match(r'^(\d+)_', path.name)
        if path.is_file() and match and path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.gif', '.webp'}:
            files.setdefault(int(match[1]), []).append(path)
    errors = []
    if not markers:
        errors.append('원고에 사진 번호가 없습니다.')
    if len(set(markers)) != len(markers):
        errors.append('원고 사진 번호가 중복됩니다.')
    for n in markers:
        if len(files.get(n, [])) != 1:
            errors.append(f'사진 {n}: 실제 파일 {len(files.get(n, []))}개 (정확히 1개 필요)')
    if markers and set(markers) != set(range(1, max(markers) + 1)):
        errors.append('원고 사진 번호에 누락이 있습니다.')
    unused = sorted(set(files) - set(markers))
    manifest = {
        'source': str(source), 'manuscript': str(manuscript),
        'title': text.splitlines()[0] if text else '',
        'photos': [{'number': n, 'path': str(files[n][0]), 'name': files[n][0].name}
                   for n in markers if len(files.get(n, [])) == 1],
        'photo_count': len(markers), 'unused_photo_numbers': unused,
        'unverified_information': '[확인 필요]' in text,
        'errors': errors, 'ready': not errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'ready': not errors, 'photos': len(markers), 'errors': errors,
                      'manifest': str(args.output)}, ensure_ascii=False))
    raise SystemExit(1 if errors else 0)


if __name__ == '__main__':
    main()
