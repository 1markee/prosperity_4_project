#!/usr/bin/env python3
import csv
import sys
from pathlib import Path

def detect_delimiter(sample):
    # simple heuristic: prefer ; if present more than ,
    if sample.count(';') > sample.count(','):
        return ';'
    if sample.count(',') > sample.count(';'):
        return ','
    # fallback to csv.Sniffer
    try:
        return csv.Sniffer().sniff(sample, delimiters=';,\t').delimiter
    except Exception:
        return ','


def convert_file(path: Path, out_path: Path):
    with path.open('r', encoding='utf-8', newline='') as f:
        sample = f.read(8192)
        f.seek(0)
        delim = detect_delimiter(sample)
        reader = csv.reader(f, delimiter=delim)
        rows = list(reader)

    with out_path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f, delimiter=',', quoting=csv.QUOTE_MINIMAL)
        writer.writerows(rows)


def main():
    if len(sys.argv) > 1:
        root = Path(sys.argv[1])
    else:
        root = Path('data')

    if not root.exists():
        print('Directory not found:', root)
        sys.exit(1)

    for p in root.rglob('*.csv'):
        if p.name.endswith('_fixed.csv') or p.name.endswith('_comma.csv'):
            continue
        out = p.with_name(p.stem + '_fixed.csv')
        try:
            convert_file(p, out)
            print('Converted', p, '->', out)
        except Exception as e:
            print('Failed', p, e)

if __name__ == '__main__':
    main()
