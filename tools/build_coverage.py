# -*- coding: utf-8 -*-
"""知识点覆盖清单：校验 + 生成人读版。

用法：
    python tools/build_coverage.py            # 校验并重新生成 知识点覆盖清单.md
    python tools/build_coverage.py --check    # 只校验，不写文件

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

DISPOSITIONS = {'body': '正文必讲', 'fold': '折叠区讲', 'exclude': '明确不讲'}
BASELINES = {'covered': '已覆盖', 'missing': '未覆盖', 'partial': '部分覆盖'}
KP_BASELINE = {
    'closure': '闭环缺口',
    'form': '形态缺口',
    'covered': '已覆盖',
    'exclude': '明确不讲',
}


def load():
    with open(DATA, encoding='utf-8') as f:
        return json.load(f)


def page_ids():
    soup = BeautifulSoup(open(INDEX, encoding='utf-8').read(), 'lxml')
    return {el.get('id') for el in soup.find_all(id=True)}


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
        anchor = kp['anchor']
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


def stats(data):
    kps = data['knowledge_points']
    forms = [f for kp in kps for f in kp['forms']]
    s = {
        'kp_total': len(kps),
        'form_total': len(forms),
        'kp_closure': sum(1 for kp in kps if kp['baseline'] == 'closure'),
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


def render(data):
    s = stats(data)
    kps = data['knowledge_points']
    out = []
    out.append('# C++ 教程知识点覆盖清单（人读版）')
    out.append('')
    out.append('> 本文件由 `tools/build_coverage.py` 从 `知识点覆盖清单.json` 生成，不要手工编辑。')
    out.append('> 处置三档：正文必讲（日常高频）/ 折叠区讲（进阶形态与边界）/ 明确不讲（注明去向）。')
    out.append('')
    out.append('## 一、总览')
    out.append('')
    out.append('| 指标 | 数量 |')
    out.append('|---|---|')
    out.append('| 知识点 | %d |' % s['kp_total'])
    out.append('| 形态条目 | %d |' % s['form_total'])
    out.append('| 闭环缺口（知识点） | %d |' % s['kp_closure'])
    out.append('| 形态缺口（知识点） | %d |' % s['kp_form'])
    out.append('| 改造前已覆盖（知识点） | %d |' % s['kp_covered'])
    out.append('| 形态：改造前未覆盖 | %d |' % s['form_missing'])
    out.append('| 形态：正文必讲 | %d |' % s['body'])
    out.append('| 形态：折叠区讲 | %d |' % s['fold'])
    out.append('| 形态：明确不讲 | %d |' % s['exclude'])
    out.append('')
    out.append('## 二、缺口汇总')
    out.append('')
    out.append('### 2.1 闭环缺口（连“为什么、什么时候用、写错会怎样”都没讲）')
    out.append('')
    out.append('| 知识点 | 位置 | 缺口 |')
    out.append('|---|---|---|')
    for kp in kps:
        if kp['baseline'] == 'closure':
            out.append('| %s | 阶段%s · %s · #%s | %s |' % (kp['name'], kp['stage'], kp['part'], kp['anchor'], kp['gap']))
    out.append('')
    out.append('### 2.2 形态缺口（主线讲了，但常见变体没讲）')
    out.append('')
    out.append('| 知识点 | 位置 | 缺了哪些形态 |')
    out.append('|---|---|---|')
    for kp in kps:
        if kp['baseline'] == 'form':
            miss = '、'.join(f['form'] for f in kp['forms'] if f['disposition'] == 'fold')
            out.append('| %s | 阶段%s · %s · #%s | %s |' % (kp['name'], kp['stage'], kp['part'], kp['anchor'], miss or kp['gap']))
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
        out.append('- 位置：阶段%s / %s / `#%s`' % (kp['stage'], kp['part'], kp['anchor']))
        out.append('- 读者会在什么场景遇到：%s' % kp['scene'])
        out.append('- 改造前判定：**%s**' % KP_BASELINE[kp['baseline']])
        out.append('- 缺口说明：%s' % kp['gap'])
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
    args = ap.parse_args()
    data = load()
    ids = page_ids()
    errors = validate(data, ids)
    s = stats(data)
    print('知识点覆盖清单：%d 个知识点 / %d 条形态' % (s['kp_total'], s['form_total']))
    print('  闭环缺口 %d / 形态缺口 %d / 改造前已覆盖 %d' % (s['kp_closure'], s['kp_form'], s['kp_covered']))
    print('  处置：正文 %d / 折叠区 %d / 明确不讲 %d' % (s['body'], s['fold'], s['exclude']))
    if errors:
        print('校验失败 %d 项：' % len(errors))
        for e in errors:
            print('  - ' + e)
        sys.exit(1)
    print('校验通过：锚点全部真实存在，处置值合法')
    if not args.check:
        with open(MD, 'w', encoding='utf-8') as f:
            f.write(render(data))
        print('已生成 %s' % os.path.relpath(MD, ROOT))


if __name__ == '__main__':
    main()
