# -*- coding: utf-8 -*-
"""把两份清单合成页面用的知识索引（派生数据）。

用法：
    python tools/build_index.py            # 校验并写出 知识点索引.json
    python tools/build_index.py --check    # 只校验，不写文件
    python tools/build_index.py --embed    # 校验、写文件，并注入 index.html 的内嵌数据块

数据源：
    知识点覆盖清单.json   知识点骨架（terms / theme 由本批手工登记）
    知识依赖清单.json     术语定义与 first_def
    index.html            阶段标题、Part 标题、锚点真实存在性

产物 知识点索引.json 是派生数据，不要手改；改两份清单后重跑本脚本。
"""
import argparse
import json
import os
import re
import sys
from datetime import date

from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KP_PATH = os.path.join(ROOT, '知识点覆盖清单.json')
DE_PATH = os.path.join(ROOT, '知识依赖清单.json')
HTML_PATH = os.path.join(ROOT, 'index.html')
OUT_PATH = os.path.join(ROOT, '知识点索引.json')

THEMES = ['语言基础', '内存与对象', '抽象与复用', '标准库', '工程化与调试']
TAGS = set(THEMES) | {'避坑'}

# 知识点卡片的分类枚举：分组用于定位“这一段属于哪一类”，
# 知识类型只用四档，保证 60 条口径一致。
GROUPS = ('输入输出', '数据类型与转换', '表达式与语句', '流程控制',
          '函数与参数', '数组与字符串', '指针与内存', '类与对象',
          '继承与多态', '模板与泛型', '异常与资源管理', '标准库与算法',
          '工程化与文件')
KINDS = ('语法', '语义', '工具', '实践')
# 读者可见文案的行话黑名单（汇报词一旦写进卡片就阻断）。
COPY_BLACKLIST = ('留口', '再考', '收束', '收口', '落账', '欠账', '兜底', '口径')
# 概念简述只回答“是什么”，禁止导语式罗列（“这一段会讲……以及……”）。
SUMMARY_BAN = ('这一段', '本节', '本章', '本篇', '这一节', '本课', '本部分',
               '会讲', '会介绍', '将讲', '将介绍', '主要讲', '重点讲',
               '涵盖', '包括以下', '以下内容', '接下来', '下面介绍', '本文',
               '以及', '还有', '另外')
MORE_TERMS = ('deep-p', 'c-')

# 阶段图读者化黑名单：汇报内部行话一旦渗进图里就阻断。
STAGE_MAP_BLACKLIST = ('留口', '再考', '收口', '落账', '欠账', '兜底',
                       '口径', '闭环', '埋点', '正向', '反向')

OPEN_MARK = '<!-- KP_INDEX_DATA:START -->'
CLOSE_MARK = '<!-- KP_INDEX_DATA:END -->'


def load():
    kp = json.load(open(KP_PATH, encoding='utf-8'))
    de = json.load(open(DE_PATH, encoding='utf-8'))
    html = open(HTML_PATH, encoding='utf-8').read()
    soup = BeautifulSoup(html, 'lxml')
    return kp, de, html, soup


def stage_titles(soup):
    out = {}
    for a in soup.select('a.nav-link[data-stage]'):
        t = a.select_one('.nav-title')
        out[str(a.get('data-stage'))] = t.get_text(strip=True) if t else ''
    return out


def part_ids(soup):
    """按 .stage-parts 的直接子卡片收集 Part id。

    没有 h3 id 的 NEXT 过渡卡片（阶段七末尾那张）在 check_deps 里也会被算作
    一个 Part，这里给它一个合成 id p{stage}-next，保证全书 81 个不漏。
    """
    out = {}
    for group in soup.select('.stage-parts'):
        stage = str(group.get('data-part-stage', '?'))
        ids = []
        for card in group.find_all(recursive=False):
            if card.name != 'div':
                continue
            h3 = card.find('h3')
            pid = h3.get('id') if h3 and h3.get('id') else ''
            if pid:
                ids.append(pid)
            elif 'NEXT' in card.get_text(' ', strip=True)[:80]:
                ids.append('p%s-next' % stage)
        out[stage] = ids
    return out


def map_edges(src):
    """抓取图里任意两个节点之间的边，用于判断两张图的结构是否雷同。

    同时认两种写法：
      - 箭头行的左端是裸节点 id：`b1 --> l1["..."]`
      - 箭头行的左端带节点标签：`b1["..."] --> s1["..."]`
    """
    edges = set()
    for line in src.splitlines():
        s = line.strip()
        if not s or s.startswith(('flowchart', 'graph', 'classDef', 'class ', 'direction')):
            continue
        m = re.search(r'(?:-->|-.->)', s)
        if not m:
            continue
        left = re.match(r'([A-Za-z_]\w*)', s[:m.start()].strip())
        right = re.match(r'(?:\|[^|]*\|\s*)?([A-Za-z_]\w*)', s[m.end():].strip())
        if left and right:
            edges.add((left.group(1), right.group(1)))
    return edges


def block_ids(src):
    return set(re.findall(r'^\s*(b\d+)\[', src, re.M))


def kp_part_id(p):
    raw = p.get('part', '')
    if '-' not in raw:
        return ''
    stage, num = raw.split('-', 1)
    return 'p%d-%s' % (int(stage), num)


def stage_registries(points):
    """按阶段汇总“已登记”的知识点，并记下它们归属的 Part。

    2026-11-07 收窄：阶段图只校验知识点覆盖。汇编、目标文件、取地址、结合性
    这类从句里的细粒度概念，只在知识索引里保证可查，不再计入阶段图覆盖账，
    否则图会退化成目录清单。概念的锚点/主题/别名另由 build_concepts 独立校验。
    """
    reg = {}

    def slot(stage):
        return reg.setdefault(stage, {'kp': set(), 'part': {}})

    for p in points:
        r = slot(int(p.get('stage')))
        r['kp'].add(p['id'])
        r['part'].setdefault(p['id'], kp_part_id(p))
    return reg


def validate_stage_maps(soup, points, errors, warnings):
    """校验阶段图口径（2026-11-07 收窄为知识点覆盖）。

    开场图 = 知识全览：叶子 li[data-covers] 必须 100% 覆盖本阶段登记的知识点；
    结尾图 = 学完自查：必须复盘开场图的全部知识板块。
    反向校验：凡登记了知识点的 Part，至少要有一个知识点被覆盖。
    data-covers 里可以保留术语 id 作注释，校验只认 kp- 前缀。
    两张图边重合度 ≥60% 判为雷同。
    """
    parts = part_ids(soup)
    reg = stage_registries(points)
    by_stage = {}
    covered = set()
    map_count = 0

    for m in soup.select('[data-stage-map]'):
        stage = str(m.get('data-stage-map', ''))
        kind = m.get('data-map-kind', '')
        label = '阶段%s %s图' % (
            stage, {'open': '开场', 'close': '结尾'}.get(kind, kind or '未标注'))
        if stage not in parts:
            errors.append('%s：阶段不存在' % label)
            continue
        if kind not in ('open', 'close'):
            errors.append('%s：data-map-kind 非法（%r）' % (label, kind))
            continue
        if kind in by_stage.get(stage, {}):
            errors.append('%s：同阶段重复出现同角色图' % label)
            continue

        source = m.select_one('.mermaid-source')
        src = source.get_text() if source else ''
        if not src.strip():
            errors.append('%s 缺少 mermaid-source' % label)

        # 黑名单 lint：整张图（标题、说明、fallback）与 mermaid 源码（含边标签）都扫。
        visible = ' '.join(m.get_text(' ').split())
        for word in STAGE_MAP_BLACKLIST:
            if word in visible or word in src:
                errors.append('%s 命中内部黑名单词「%s」：图上文字必须是人话' % (label, word))

        covers = set()
        for li in m.select('.stage-map-leaves li[data-leaf]'):
            lid = li.get('data-leaf', '')
            if not re.search(r'\b%s\b' % re.escape(lid), src):
                errors.append('%s 叶子 %s 在图中不存在' % (label, lid))
            for c in (li.get('data-covers') or '').split(','):
                c = c.strip()
                if c:
                    covers.add(c)

        by_stage.setdefault(stage, {})[kind] = {
            'src': src, 'covers': covers, 'blocks': block_ids(src)}
        map_count += 1

    rows = []
    for stage in sorted(by_stage, key=int):
        kinds = by_stage[stage]
        r = reg.get(int(stage), {'kp': set(), 'part': {}})
        registered = r['kp']
        row = {'stage': int(stage), 'parts': len(parts[stage]), 'items': len(registered)}
        if 'open' not in kinds or 'close' not in kinds:
            errors.append('阶段%s 开场图与结尾图未成对' % stage)
            rows.append(dict(row, open_items=0, blocks_open=0, blocks_close=0))
            continue

        o, c = kinds['open'], kinds['close']
        unknown = sorted(x for x in o['covers'] if x.startswith('kp-') and x not in registered)
        if unknown:
            errors.append('阶段%s 开场图 data-covers 含未登记的知识点：%s' % (stage, '、'.join(unknown)))
        missing = sorted(registered - o['covers'])
        if missing:
            errors.append('阶段%s 开场图漏覆盖 %d 项：%s' % (stage, len(missing), '、'.join(missing)))
        covered |= (o['covers'] & registered)

        hit = {r['part'].get(x) for x in (o['covers'] & registered)}
        orphans, no_reg = [], []
        for pid in parts[stage]:
            items = [x for x in registered if r['part'].get(x) == pid]
            if not items:
                no_reg.append(pid)
            elif pid not in hit:
                orphans.append(pid)
        if orphans:
            errors.append('阶段%s 这些 Part 整段没有对应知识点被覆盖：%s' % (
                stage, '、'.join(orphans)))
        if no_reg:
            warnings.append('阶段%s 的 %s 本身没有登记知识点，反向校验跳过' % (
                stage, '、'.join(no_reg)))

        if o['blocks'] != c['blocks']:
            errors.append('阶段%s 结尾图板块与开场图不一致（开场 %s / 结尾 %s）' % (
                stage, ','.join(sorted(o['blocks'])) or '空',
                ','.join(sorted(c['blocks'])) or '空'))

        oe, ce = map_edges(o['src']), map_edges(c['src'])
        if oe and ce and len(oe & ce) / min(len(oe), len(ce)) >= 0.6:
            errors.append('阶段%s 开场图与结尾图结构重合度过高（%d/%d 条边相同）' % (
                stage, len(oe & ce), min(len(oe), len(ce))))

        rows.append(dict(row, open_items=len(registered), blocks_open=len(o['blocks']),
                         blocks_close=len(c['blocks'])))

    for stage in sorted(parts, key=int):
        if stage not in by_stage:
            warnings.append('阶段%s 尚未配置阶段图（%d 个 Part）' % (stage, len(parts[stage])))

    return parts, by_stage, covered, map_count, rows


def build_concepts(entries, points, all_ids, errors):
    """派生概念索引：manifest 的一条 entry 对应一条概念。

    硬护栏（2026-11-07）：每条概念必须落到真实存在的锚点（full_def，回退
    first_def）、必须有主主题（由 points 里文档序最靠前的知识点推导）、别名
    不得与概念名或其他别名冲突。概念与知识点是多对多：关联登记在 entry.points，
    不改知识点清单，也不把概念硬塞成知识点。
    """
    kp_by_id = {p['id']: p for p in points}
    kp_rank = {p['id']: i for i, p in enumerate(points)}
    # 知识点侧 terms[] 与清单侧 points 是同一关系的两个登记面：取并集，避免重复登记。
    kp_of_term = {}
    for p in points:
        for t in p.get('terms') or []:
            kp_of_term.setdefault(t, []).append(p['id'])
    names = {e['term'] for e in entries}
    alias_owner = {}
    theme_counts = {t: 0 for t in THEMES}
    out = []
    anchor_bad = 0

    if len(names) != len(entries):
        errors.append('概念名重复：manifest 里存在同名 term')

    for e in entries:
        term = e['term']
        raw_points = e.get('points') or []
        if not isinstance(raw_points, list):
            errors.append('概念「%s」points 必须是数组' % term)
            raw_points = []
        raw_points = list(dict.fromkeys(list(raw_points) + kp_of_term.get(term, [])))
        for pid in raw_points:
            if pid not in kp_by_id:
                errors.append('概念「%s」points 指向不存在的知识点：%s' % (term, pid))
        valid = [pid for pid in raw_points if pid in kp_rank]
        theme = None
        if valid:
            best = min(valid, key=lambda pid: kp_rank[pid])
            theme = (kp_by_id[best].get('theme') or {}).get('primary')
        if theme not in THEMES:
            errors.append('概念「%s」主主题缺失或非法：%r' % (term, theme))
        else:
            theme_counts[theme] += 1

        anchor = e.get('full_def') or e.get('first_def') or ''
        if anchor not in all_ids:
            anchor_bad += 1
            errors.append('概念「%s」锚点不存在：#%s' % (term, anchor))

        aliases = list(e.get('aliases') or [])
        for a in aliases:
            if a in names:
                errors.append('别名「%s」与已登记概念重名（归属于 %s）' % (a, term))
            if a in alias_owner and alias_owner[a] != term:
                errors.append('别名「%s」重复归属：%s / %s' % (a, alias_owner[a], term))
            alias_owner[a] = term

        out.append({
            'name': term,
            'type': e.get('type', 'concept'),
            'anchor': anchor,
            'brief': e.get('brief', ''),
            'theme': theme,
            'points': list(raw_points),
            'aliases': aliases,
        })

    return out, theme_counts, anchor_bad


def build():
    kp, de, html, soup = load()
    points = kp['knowledge_points']
    entries = de['entries']
    by_term = {e['term']: e for e in entries}
    all_ids = {t.get('id') for t in soup.find_all(id=True)}
    titles = stage_titles(soup)
    errors = []
    warnings = []

    if len(points) != 60:
        errors.append('知识点数量应为 60，实际 %d' % len(points))
    total_forms = sum(len(p.get('forms', [])) for p in points)
    if total_forms != 281:
        errors.append('形态总数应为 281，实际 %d' % total_forms)

    out_points = []
    link_count = 0
    used_terms = set()
    fallback = 0
    for p in points:
        pid = p.get('id', '?')
        th = (p.get('theme') or {})
        primary = th.get('primary')
        tags = th.get('tags') or []
        if primary not in THEMES:
            errors.append('%s 主主题非法：%r' % (pid, primary))
        if len(tags) > 2:
            errors.append('%s 副标签超过 2 个：%r' % (pid, tags))
        for g in tags:
            if g not in TAGS:
                errors.append('%s 副标签非法：%r' % (pid, g))
        if g := [g for g in tags if g == primary]:
            errors.append('%s 副标签重复主主题：%r' % (pid, g))

        summary = (p.get('summary') or '').strip()
        group = (p.get('group') or '').strip()
        kind = (p.get('kind') or '').strip()
        if group and group not in GROUPS:
            errors.append('%s 分组不在枚举内：%r' % (pid, group))
        if kind and kind not in KINDS:
            errors.append('%s 知识类型不在枚举内：%r' % (pid, kind))
        for w in COPY_BLACKLIST:
            if w in summary:
                errors.append('%s 概念简述含行话「%s」' % (pid, w))
        if summary:
            # 「本文件」是正文里正常的说法，先剔除再查「本文」等导语词，避免误报。
            probe = summary.replace('本文件', '')
            for w in SUMMARY_BAN:
                if w in probe:
                    errors.append('%s 概念简述是导语/罗列口吻，命中「%s」' % (pid, w))
            if len(summary) < 25 or len(summary) > 60:
                errors.append('%s 概念简述 %d 字，应在 25～60 字' % (pid, len(summary)))

        terms = p.get('terms') or []
        for t in terms:
            if t not in by_term:
                errors.append('%s 引用了不存在的术语：%s' % (pid, t))
        link_count += len(terms)
        used_terms.update(terms)
        if not terms:
            warnings.append('%s「%s」没有登记术语' % (pid, p.get('name')))

        anchor = p.get('anchor', '')
        if anchor and anchor not in all_ids:
            errors.append('%s 锚点不存在：#%s' % (pid, anchor))

        jump = anchor
        if terms and terms[0] in by_term:
            fd = by_term[terms[0]].get('first_def', '')
            if fd and fd in all_ids:
                jump = fd
            else:
                fallback += 1
        elif anchor:
            fallback += 1
        if jump and jump not in all_ids:
            errors.append('%s 跳转目标不存在：#%s' % (pid, jump))

        part = p.get('part', '')
        h3 = soup.find(id='p%s' % part)
        out_points.append({
            'id': pid,
            'name': p.get('name', ''),
            'stage': p.get('stage'),
            'stageTitle': titles.get(str(p.get('stage')), ''),
            'part': part,
            'partTitle': h3.get_text(strip=True) if h3 else '',
            'anchor': anchor,
            'theme': primary,
            'tags': tags,
            'terms': terms,
            'jump': jump,
            'scene': p.get('scene', ''),
            'summary': summary,
            'group': group,
            'kind': kind,
            'forms': [{'f': f.get('form', ''), 'd': f.get('disposition', '')}
                      for f in p.get('forms', [])],
        })

    theme_counts = {t: 0 for t in THEMES}
    for p in out_points:
        theme_counts[p['theme']] += 1

    stages = []
    for s in sorted({p['stage'] for p in out_points}):
        stages.append({
            'stage': s,
            'title': titles.get(str(s), ''),
            'count': sum(1 for p in out_points if p['stage'] == s),
        })

    concepts, concept_themes, concept_anchor_bad = build_concepts(
        entries, points, all_ids, errors)
    concept_aliases = sum(len(c['aliases']) for c in concepts)

    referenced = set(used_terms)
    for e in entries:
        if e.get('points'):
            referenced.add(e['term'])
    unlinked = sorted(set(by_term) - referenced)
    stats = {
        'points': len(out_points),
        'forms': total_forms,
        'terms': len(by_term),
        'term_links': link_count,
        'terms_referenced': len(referenced),
        'terms_unreferenced': len(unlinked),
        'concepts': len(concepts),
        'concepts_with_theme': sum(1 for c in concepts if c['theme']),
        'concept_anchors_bad': concept_anchor_bad,
        'concept_aliases': concept_aliases,
        'concept_themes': concept_themes,
        'jump_fallback': fallback,
        'themes': theme_counts,
        'stages': len(stages),
    }
    data = {
        '_derived': True,
        '_generated_by': 'tools/build_index.py',
        '_source': ['知识点覆盖清单.json', '知识依赖清单.json', 'index.html'],
        '_note': '派生数据，勿手改；改两份清单后重跑 tools/build_index.py',
        '_generated': date.today().isoformat(),
        'themes': [{'name': t, 'count': theme_counts[t]} for t in THEMES],
        'conceptThemes': [{'name': t, 'count': concept_themes[t]} for t in THEMES],
        'stages': stages,
        'points': out_points,
        'concepts': concepts,
        'stats': stats,
    }

    parts_by_stage, stage_maps, covered, map_count, map_rows = validate_stage_maps(
        soup, points, errors, warnings)
    data['stats']['stage_maps'] = {
        'maps': map_count,
        'stages': len(stage_maps),
        'parts': sum(len(v) for v in parts_by_stage.values()),
        'items': sum(r['items'] for r in map_rows),
        'covered': sum(r['open_items'] for r in map_rows),
        'rows': map_rows,
    }
    return data, errors, warnings, total_forms


def embed(data, html):
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    block = '%s\n  <script type="application/json" id="kp-index-data">%s</script>\n  %s' % (
        OPEN_MARK, payload, CLOSE_MARK)
    if OPEN_MARK in html and CLOSE_MARK in html:
        return re.sub(re.escape(OPEN_MARK) + r'.*?' + re.escape(CLOSE_MARK),
                      lambda _: block, html, flags=re.S)
    needle = "<script>\n    (function () {"
    if needle not in html:
        raise SystemExit('找不到注入点：<script>\\n    (function () {')
    return html.replace(needle, block + '\n\n  ' + needle, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--embed', action='store_true')
    args = ap.parse_args()

    data, errors, warnings, total_forms = build()
    s = data['stats']
    print('知识点索引校验')
    print('  知识点 %d；形态 %d' % (s['points'], s['forms']))
    print('  主题：%s' % ' / '.join('%s %d' % (t['name'], t['count']) for t in data['themes']))
    print('  术语链接 %d 条，覆盖概念 %d / %d；未入库 %d' % (
        s['term_links'], s['terms_referenced'], s['terms'], s['terms_unreferenced']))
    reach = s['concepts'] - s['concept_anchors_bad']
    print('  概念可查 %d / %d，锚点失效 %d；主主题 %d / %d；别名 %d 组' % (
        reach, s['concepts'], s['concept_anchors_bad'],
        s['concepts_with_theme'], s['concepts'], s['concept_aliases']))
    print('  概念主题：%s' % ' / '.join('%s %d' % (t['name'], t['count'])
                                     for t in data.get('conceptThemes', [])))
    print('  跳转目标：术语首现 %d，回退 Part 标题 %d' % (
        s['points'] - s['jump_fallback'], s['jump_fallback']))
    sm = s.get('stage_maps') or {}
    print('  阶段图：%d 张 / 已配阶段 %d；知识点覆盖 %d / %d' % (
        sm.get('maps', 0), sm.get('stages', 0),
        sm.get('covered', 0), sm.get('items', 0)))
    for row in sm.get('rows', []):
        print('    阶段%d：Part %d；开场覆盖 %d/%d 项；板块 开场 %d → 结尾 %d' % (
            row['stage'], row['parts'], row['open_items'], row['items'],
            row['blocks_open'], row['blocks_close']))
    for w in warnings:
        print('  [提示] %s' % w)
    if errors:
        print('  违规 %d 项：' % len(errors))
        for e in errors:
            print('    - %s' % e)
        return 1
    print('  校验通过：术语存在、概念锚点/主主题/别名合格、阶段图知识点全覆盖')

    if args.check:
        return 0
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print('  已写出 %s（派生数据）' % os.path.basename(OUT_PATH))

    if args.embed:
        html = open(HTML_PATH, encoding='utf-8').read()
        new = embed(data, html)
        if new == html:
            print('  内嵌数据块无变化')
        else:
            open(HTML_PATH, 'w', encoding='utf-8').write(new)
            print('  已注入 index.html（KB 变化见 git/文件大小）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
