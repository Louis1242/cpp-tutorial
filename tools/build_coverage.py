# -*- coding: utf-8 -*-
"""知识点覆盖清单：校验 + 生成人读版。

用法：
    python tools/build_coverage.py            # 校验并重新生成 知识点覆盖清单.md
    python tools/build_coverage.py --check    # 只校验，不写文件
    python tools/build_coverage.py --json     # 校验并生成 知识点覆盖统计.json（编者视图数据）
    python tools/build_coverage.py --embed    # 校验、生成统计 JSON，并更新 index.html 内嵌块

数据源：知识点覆盖清单.json（单一事实来源，不手工维护 md）
校验：知识点锚点在 index.html 中真实存在；阶段/Part 与锚点前缀一致；处置值合法。
"""
import argparse
import json
import os
import re
import sys

from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, 'index.html')
DATA = os.path.join(ROOT, '知识点覆盖清单.json')
MD = os.path.join(ROOT, '知识点覆盖清单.md')
STATS_JSON = os.path.join(ROOT, '知识点覆盖统计.json')
COVERAGE_BLOCK = ('<!-- KP-COVERAGE-DATA:START -->', '<!-- KP-COVERAGE-DATA:END -->')

DISPOSITIONS = {'body': '正文必讲', 'fold': '折叠区讲', 'exclude': '明确不讲'}
BASELINES = {'covered': '已覆盖', 'missing': '未覆盖', 'partial': '部分覆盖'}
KP_BASELINE = {
    'closure': '闭环缺口',
    'form': '形态缺口',
    'covered': '已覆盖',
    'exclude': '明确不讲',
}
# 当前状态：closure 只说明“改造前没讲过”，status 才说明“现在落地没有”。
KP_STATUS = {
    'landed': '已落地',
    'pending': '仍待补',
}


def load():
    with open(DATA, encoding='utf-8') as f:
        return json.load(f)


def page_ids():
    soup = BeautifulSoup(open(INDEX, encoding='utf-8').read(), 'lxml')
    return {el.get('id') for el in soup.find_all(id=True)}


def kp_status(kp):
    """当前状态。旧清单没有 status 时：closure 默认仍待补，其余默认已落地。"""
    return kp.get('status') or ('pending' if kp['baseline'] == 'closure' else 'landed')


def anchor_present(kp, ids):
    anchor = kp.get('anchor')
    return bool(anchor) and anchor in ids


def is_pending(kp, ids):
    """真正还没落地：改造前就没讲，且当前仍待补或锚点在页面里找不到。"""
    if kp['baseline'] != 'closure':
        return False
    return kp_status(kp) == 'pending' or not anchor_present(kp, ids)


def validate(data, ids):
    errors = []
    kps = data['knowledge_points']
    seen = set()
    for kp in kps:
        if kp['id'] in seen:
            errors.append('重复的知识点 id：%s' % kp['id'])
        seen.add(kp['id'])
        if kp['baseline'] not in KP_BASELINE:
            errors.append('%s 的 baseline 非法：%s' % (kp['id'], kp['baseline']))
        status = kp.get('status')
        if status is not None and status not in KP_STATUS:
            errors.append('%s 的 status 非法：%s（只允许 landed / pending）' % (kp['id'], status))
        anchor = kp.get('anchor')
        if not anchor:
            if kp_status(kp) != 'pending':
                errors.append('%s 缺少锚点，且当前状态不是待补' % kp['id'])
        else:
            if anchor not in ids:
                errors.append('%s 的锚点不存在：#%s' % (kp['id'], anchor))
            prefix = 'p%d-%s' % (kp['stage'], kp['part'].split('-', 1)[-1] if '-' in kp['part'] else kp['part'])
            # part 形如 01-04；锚点应为 p1-04（作用域等概念锚点除外）
            if not anchor.startswith(prefix) and anchor not in ('c-scope', 'c-localvar', 'c-block'):
                errors.append('%s 的锚点 %s 与阶段/Part %s-%s 不匹配' % (kp['id'], anchor, kp['stage'], kp['part']))
        if not kp['forms']:
            errors.append('%s 没有登记任何形态' % kp['id'])
        for form in kp['forms']:
            if form['disposition'] not in DISPOSITIONS:
                errors.append('%s 的形态“%s”处置非法：%s' % (kp['id'], form['form'], form['disposition']))
            if form['baseline'] not in BASELINES:
                errors.append('%s 的形态“%s”baseline 非法：%s' % (kp['id'], form['form'], form['baseline']))
            if not form.get('scene'):
                errors.append('%s 的形态“%s”缺少使用场景' % (kp['id'], form['form']))
            if not form.get('note'):
                errors.append('%s 的形态“%s”缺少处置说明' % (kp['id'], form['form']))
    return errors


def stats(data, ids):
    kps = data['knowledge_points']
    forms = [f for kp in kps for f in kp['forms']]
    closure = [kp for kp in kps if kp['baseline'] == 'closure']
    s = {
        'kp_total': len(kps),
        'form_total': len(forms),
        'kp_closure': len(closure),
        'kp_closure_pending': sum(1 for kp in closure if is_pending(kp, ids)),
        'kp_closure_landed': sum(1 for kp in closure if not is_pending(kp, ids)),
        'kp_form': sum(1 for kp in kps if kp['baseline'] == 'form'),
        'kp_covered': sum(1 for kp in kps if kp['baseline'] == 'covered'),
        'form_missing': sum(1 for f in forms if f['baseline'] == 'missing'),
        'form_partial': sum(1 for f in forms if f['baseline'] == 'partial'),
        'form_covered': sum(1 for f in forms if f['baseline'] == 'covered'),
        'body': sum(1 for f in forms if f['disposition'] == 'body'),
        'fold': sum(1 for f in forms if f['disposition'] == 'fold'),
        'exclude': sum(1 for f in forms if f['disposition'] == 'exclude'),
    }
    return s


def coverage_payload(data, ids):
    """编者视图用：所有数字在这里算好，index.html 只展示。"""
    kps = data['knowledge_points']
    s = stats(data, ids)
    themes = []
    buckets = {}
    for kp in kps:
        theme = (kp.get('theme') or {}).get('primary') or '未分类'
        if theme not in buckets:
            buckets[theme] = {
                'name': theme,
                'points': 0, 'forms': 0, 'body': 0, 'fold': 0, 'exclude': 0,
                'covered': 0, 'missing': 0, 'partial': 0, 'closure_landed': 0, 'closure_pending': 0,
                'points_detail': [],
            }
            themes.append(buckets[theme])
        b = buckets[theme]
        forms = kp['forms']
        row = {
            'id': kp['id'], 'name': kp['name'], 'stage': kp['stage'], 'part': kp['part'],
            'anchor': kp.get('anchor') or '',
            'forms': len(forms),
            'body': sum(1 for f in forms if f['disposition'] == 'body'),
            'fold': sum(1 for f in forms if f['disposition'] == 'fold'),
            'exclude': sum(1 for f in forms if f['disposition'] == 'exclude'),
            'covered': sum(1 for f in forms if f['baseline'] == 'covered'),
            'missing': sum(1 for f in forms if f['baseline'] == 'missing'),
            'partial': sum(1 for f in forms if f['baseline'] == 'partial'),
            'closure': kp['baseline'] == 'closure',
            'pending': bool(is_pending(kp, ids)),
        }
        # 热力色块的深浅比例也在脚本里算好，index.html 只读不改。
        row['ratio'] = round(row['covered'] / row['forms'], 3) if row['forms'] else 0
        b['points_detail'].append(row)
        b['points'] += 1
        b['forms'] += row['forms']
        b['body'] += row['body']
        b['fold'] += row['fold']
        b['exclude'] += row['exclude']
        b['covered'] += row['covered']
        b['missing'] += row['missing']
        b['partial'] += row['partial']
        if row['closure']:
            b['closure_pending' if row['pending'] else 'closure_landed'] += 1
    return {
        '_derived': True,
        '_generated_by': 'tools/build_coverage.py --json',
        '_note': '禁止手改；数字由脚本从 知识点覆盖清单.json 计算，index.html 只负责展示。',
        'summary': s,
        'themes': themes,
    }


def write_stats(payload):
    with open(STATS_JSON, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return os.path.relpath(STATS_JSON, ROOT)


def embed_stats(payload):
    """把统计 JSON 写进 index.html 的 KP-COVERAGE-DATA 标记块。"""
    start, end = COVERAGE_BLOCK
    block = '%s\n<script type="application/json" id="kp-coverage-data">%s</script>\n%s' % (
        start, json.dumps(payload, ensure_ascii=False, separators=(',', ':')), end)
    with open(INDEX, encoding='utf-8') as f:
        text = f.read()
    pattern = re.compile(re.escape(start) + r'[\s\S]*?' + re.escape(end))
    text, count = pattern.subn(lambda _: block, text)
    if not count:
        raise SystemExit('index.html 中缺少 %s / %s 标记，未写入内嵌数据' % COVERAGE_BLOCK)
    with open(INDEX, 'w', encoding='utf-8') as f:
        f.write(text)
    return count


def gap_kind(kp):
    """gap 写的是历史缺口，还是“已经补了什么”的完成说明。"""
    return '历史缺口说明' if kp['gap'].startswith('改造前') else '完成说明（非缺口）'


def render(data, ids):
    s = stats(data, ids)
    kps = data['knowledge_points']
    closure = [kp for kp in kps if kp['baseline'] == 'closure']
    closure_landed = [kp for kp in closure if not is_pending(kp, ids)]
    closure_pending = [kp for kp in closure if is_pending(kp, ids)]

    def loc(kp):
        anchor = kp.get('anchor') or '（暂无锚点）'
        return '阶段%s · %s · #%s' % (kp['stage'], kp['part'], anchor)

    out = []
    out.append('# C++ 教程知识点覆盖清单（人读版）')
    out.append('')
    out.append('> 本文件由 `tools/build_coverage.py` 从 `知识点覆盖清单.json` 生成，不要手工编辑。')
    out.append('> 处置三档：正文必讲（日常高频）/ 折叠区讲（进阶形态与边界）/ 明确不讲（注明去向）。')
    out.append('> 口径：`baseline` 是改造前的历史状态；`status` 是当前是否已落地（landed / pending），并会用 `index.html` 里的真实锚点复核。')
    out.append('')
    out.append('## 一、总览')
    out.append('')
    out.append('| 指标 | 数量 |')
    out.append('|---|---|')
    out.append('| 知识点 | %d |' % s['kp_total'])
    out.append('| 形态条目 | %d |' % s['form_total'])
    out.append('| 历史缺口（知识点，改造前没讲过） | %d |' % s['kp_closure'])
    out.append('| 其中：已落地 | %d |' % s['kp_closure_landed'])
    out.append('| 其中：仍待补 | %d |' % s['kp_closure_pending'])
    out.append('| 形态缺口（按知识点计） | %d |' % s['kp_form'])
    out.append('| 改造前已覆盖（知识点） | %d |' % s['kp_covered'])
    out.append('| 形态未覆盖（按形态计） | %d |' % s['form_missing'])
    out.append('| 形态：正文必讲 | %d |' % s['body'])
    out.append('| 形态：折叠区讲 | %d |' % s['fold'])
    out.append('| 形态：明确不讲 | %d |' % s['exclude'])
    out.append('')
    out.append('## 二、缺口汇总')
    out.append('')
    out.append('### 2.1 历史缺口（`baseline == closure`：改造前连“为什么、什么时候用、写错会怎样”都没讲）')
    out.append('')
    out.append('> 共 %d 条。`baseline` 只记录改造前的状态；下面按当前是否落地分成两组。' % s['kp_closure'])
    out.append('')
    out.append('#### 2.1.1 已落地（%d 条）' % len(closure_landed))
    out.append('')
    out.append('| 知识点 | 位置 | 说明类型 | 说明 |')
    out.append('|---|---|---|---|')
    for kp in closure_landed:
        out.append('| %s | %s | %s | %s |' % (kp['name'], loc(kp), gap_kind(kp), kp['gap']))
    out.append('')
    out.append('#### 2.1.2 仍待补（%d 条）' % len(closure_pending))
    out.append('')
    out.append('| 知识点 | 位置 | 说明类型 | 缺口说明 |')
    out.append('|---|---|---|---|')
    for kp in closure_pending:
        out.append('| %s | %s | %s | %s |' % (kp['name'], loc(kp), gap_kind(kp), kp['gap']))
    out.append('')
    out.append('### 2.2 形态缺口（主线讲了，但常见变体没讲）')
    out.append('')
    out.append('| 知识点 | 位置 | 缺了哪些形态 |')
    out.append('|---|---|---|')
    for kp in kps:
        if kp['baseline'] == 'form':
            miss = '、'.join(f['form'] for f in kp['forms'] if f['disposition'] == 'fold')
            out.append('| %s | %s | %s |' % (kp['name'], loc(kp), miss or kp['gap']))
    out.append('')
    out.append('### 2.3 明确不讲（保留并标注去向）')
    out.append('')
    out.append('| 知识点 | 形态 | 去向 |')
    out.append('|---|---|---|')
    for kp in kps:
        for f in kp['forms']:
            if f['disposition'] == 'exclude':
                out.append('| %s | %s | %s |' % (kp['name'], f['form'], f['note']))
    out.append('')
    out.append('## 三、逐知识点明细')
    out.append('')
    for kp in kps:
        out.append('### %s · %s' % (kp['id'], kp['name']))
        out.append('')
        out.append('- 位置：%s' % loc(kp))
        out.append('- 读者会在什么场景遇到：%s' % kp['scene'])
        out.append('- 改造前判定：**%s**' % KP_BASELINE[kp['baseline']])
        if kp['baseline'] == 'closure':
            effective = 'pending' if is_pending(kp, ids) else 'landed'
            out.append('- 当前状态：**%s**' % KP_STATUS[effective])
        out.append('- %s：%s' % (gap_kind(kp), kp['gap']))
        out.append('')
        out.append('| 常见形态 | 处置 | 改造前 | 使用场景 | 处置说明 |')
        out.append('|---|---|---|---|---|')
        for f in kp['forms']:
            out.append('| %s | %s | %s | %s | %s |' % (
                f['form'], DISPOSITIONS[f['disposition']], BASELINES[f['baseline']], f['scene'], f['note']))
        out.append('')
    return '\n'.join(out) + '\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', help='只校验，不写 md')
    ap.add_argument('--sync-md', action='store_true', help='校验并生成 知识点覆盖清单.md（默认行为，保留该参数以兼容调用）')
    ap.add_argument('--json', action='store_true', help='生成 知识点覆盖统计.json（编者视图数据）')
    ap.add_argument('--embed', action='store_true', help='生成统计 JSON 并更新 index.html 内嵌块')
    args = ap.parse_args()
    data = load()
    ids = page_ids()
    errors = validate(data, ids)
    s = stats(data, ids)
    print('知识点覆盖清单：%d 个知识点 / %d 条形态' % (s['kp_total'], s['form_total']))
    print('历史缺口：%d 条（其中已落地 %d 条、仍待补 %d 条）' % (
        s['kp_closure'], s['kp_closure_landed'], s['kp_closure_pending']))
    print('形态未覆盖：%d 条' % s['form_missing'])
    print('  形态缺口（按知识点计）%d / 改造前已覆盖 %d' % (s['kp_form'], s['kp_covered']))
    print('  处置：正文 %d / 折叠区 %d / 明确不讲 %d' % (s['body'], s['fold'], s['exclude']))
    if errors:
        print('校验失败 %d 项：' % len(errors))
        for e in errors:
            print('  - ' + e)
        sys.exit(1)
    print('校验通过：锚点全部真实存在，处置值合法')
    if args.json or args.embed:
        payload = coverage_payload(data, ids)
        print('已生成 %s' % write_stats(payload))
    if args.embed:
        embed_stats(payload)
        print('已更新 index.html 的 KP-COVERAGE-DATA 内嵌块')
    if not args.check:
        with open(MD, 'w', encoding='utf-8') as f:
            f.write(render(data, ids))
        print('已生成 %s' % os.path.relpath(MD, ROOT))


if __name__ == '__main__':
    main()
