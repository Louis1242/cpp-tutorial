# -*- coding: utf-8 -*-
"""孤儿术语兜底校验：新账硬、老账软。

孤儿术语 = 已登记在《知识依赖清单.json》里、但既没有被任何知识点在 terms[] 中
引用、自身也没有概念级 points 归属的术语。2026-11-07 起，概念关联统一写在
entry.points 上，因此 points 也算“已入库”。

本脚本规则（见《制作标准.md》§4.7.2）：
  - 新账硬：新增术语，或改动过 type / first_def / full_def / brief / note /
    match 任一项的术语，若仍是孤儿，报错并以非零码退出。
  - 老账软：基线里已有的、指纹未变的孤儿，只按「概念/语法」「名称」分组提示。

用法：
    python tools/check_orphan_terms.py                 # 校验
    python tools/check_orphan_terms.py --update-baseline  # 人工确认后写基线
"""
import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KP_PATH = os.path.join(ROOT, '知识点覆盖清单.json')
DE_PATH = os.path.join(ROOT, '知识依赖清单.json')
BASE_PATH = os.path.join(ROOT, 'tools', 'orphan_terms_baseline.json')
TIERS_PATH = os.path.join(ROOT, 'tools', 'orphan_terms_tiers.json')

FP_FIELDS = ('type', 'first_def', 'full_def', 'brief', 'note', 'match')
GROUP_CONCEPT = '概念/语法'
GROUP_NAME = '名称'


def load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def fingerprint(entry):
    payload = {k: entry.get(k, '') for k in FP_FIELDS}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:16]


def group_of(entry):
    return GROUP_NAME if entry.get('type') == 'name' else GROUP_CONCEPT


def referenced_terms(points, entries):
    refs = set()
    for p in points:
        for t in p.get('terms') or []:
            refs.add(t)
    for e in entries:
        if e.get('points'):
            refs.add(e['term'])
    return refs


def scan():
    kp = load_json(KP_PATH)
    de = load_json(DE_PATH)
    points = kp['knowledge_points']
    entries = de['entries']
    refs = referenced_terms(points, entries)
    orphans = [e for e in entries if e['term'] not in refs]
    return points, entries, refs, orphans


def baseline_terms():
    if not os.path.exists(BASE_PATH):
        return None
    return load_json(BASE_PATH).get('terms', {})


def tier_notes():
    """分档记录：A 档已挂知识点，B/C 档仍留术语表；基线只保留 B/C 及其理由。"""
    if not os.path.exists(TIERS_PATH):
        return {}
    return load_json(TIERS_PATH).get('terms', {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--update-baseline', action='store_true',
                    help='人工确认后，把当前全部孤儿术语写入基线')
    args = ap.parse_args()

    points, entries, refs, orphans = scan()
    print('孤儿术语校验（新账硬 / 老账软）')
    print('  知识点 %d；术语 %d；有知识点引用 %d；孤儿 %d' % (
        len(points), len(entries), len(refs), len(orphans)))

    if args.update_baseline:
        notes = tier_notes()
        data = {
            '_note': '存量孤儿术语基线。2026-11-07 知识索引收全批：16 条 B/C 档孤儿全部补上'
                     '概念级 points，术语表与知识索引同源可查，基线清空；此后新增孤儿一律硬报。'
                     '只允许在人工确认“该词只留在术语表、不归属任何知识点”后扩写',
            '_generated': '2026-11-07',
            'terms': {},
        }
        for e in orphans:
            item = {
                'type': e.get('type', ''),
                'first_def': e.get('first_def', ''),
                'fp': fingerprint(e),
            }
            n = notes.get(e['term']) or {}
            if n:
                item['tier'] = n.get('tier', '')
                item['reason'] = n.get('reason', '')
            data['terms'][e['term']] = item
        with open(BASE_PATH, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
        print('  已写入基线：%s（%d 条）' % (BASE_PATH, len(orphans)))
        return 0

    base = baseline_terms()
    if base is None:
        print('  [错误] 缺少基线 %s；先人工确认，再运行 --update-baseline' % BASE_PATH)
        return 1

    hard_new, hard_changed, soft = [], [], []
    for e in orphans:
        name = e['term']
        b = base.get(name)
        if not b:
            hard_new.append(e)
        elif b.get('fp') != fingerprint(e):
            hard_changed.append(e)
        else:
            soft.append(e)

    # 已经不再孤儿、但仍留在基线里的词，只提示，供人工清理基线
    gone = sorted(set(base) - {e['term'] for e in orphans})

    for e in hard_new:
        print('  [错误] 新增孤儿术语：%s | %s | first_def %s' % (
            e['term'], e.get('type', ''), e.get('first_def', '') or '（空）'))
    for e in hard_changed:
        print('  [错误] 改动后的孤儿术语：%s | %s | first_def %s' % (
            e['term'], e.get('type', ''), e.get('first_def', '') or '（空）'))

    groups = {GROUP_CONCEPT: [], GROUP_NAME: []}
    for e in soft:
        groups[group_of(e)].append(e)
    print('  [提示] 存量孤儿 %d 条（%s %d / %s %d）' % (
        len(soft), GROUP_CONCEPT, len(groups[GROUP_CONCEPT]),
        GROUP_NAME, len(groups[GROUP_NAME])))
    for g in (GROUP_CONCEPT, GROUP_NAME):
        flag = '（优先人工确认）' if g == GROUP_CONCEPT else ''
        print('  [提示] %s组%s：%d 条' % (g, flag, len(groups[g])))
        for e in sorted(groups[g], key=lambda x: (x.get('first_def', ''), x['term'])):
            b = base.get(e['term']) or {}
            tier = b.get('tier', '')
            note = ('[%s] ' % tier) if tier else ''
            print('    - %s%s | %s | first_def %s' % (
                note, e['term'], e.get('type', ''), e.get('first_def', '') or '（空）'))

    if gone:
        shown = gone[:10]
        more = len(gone) - len(shown)
        tail = '……另 %d 条（逐条见基线文件）' % more if more > 0 else ''
        print('  [提示] 已不再孤儿、可从基线移除 %d 条：%s%s' % (
            len(gone), '、'.join(shown), tail))

    if hard_new or hard_changed:
        print('  结果：阻断（新增 %d / 改动 %d）' % (len(hard_new), len(hard_changed)))
        return 1
    print('  结果：通过（存量孤儿仅提示）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
