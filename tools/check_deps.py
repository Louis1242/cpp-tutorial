# -*- coding: utf-8 -*-
"""发布前知识依赖检查：概念/名称/语法写法先行讲解 + 表格/代码未定义标识符。

用法：
    python tools/check_deps.py              # 只检查，违规时退出码 1
    python tools/check_deps.py --sync-glossary   # 检查并刷新页面术语表
    python tools/check_deps.py --verbose    # 额外打印豁免与统计

清单：知识依赖清单.json（单一事实来源，页面术语表由它生成）
"""
import argparse
import html as html_mod
import json
import locale
import os
import re
import sys

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, 'index.html')
MANIFEST = os.path.join(ROOT, '知识依赖清单.json')

STD_TYPES = {
    'std::string', 'std::ostream', 'std::istream', 'std::vector', 'std::size_t',
    'size_t', 'string', 'C++', 'Wandbox', 'GCC', 'HTML', 'JS',
}
KEYWORDS = {
    'int', 'double', 'float', 'char', 'bool', 'long', 'short', 'unsigned', 'void',
    'auto', 'const', 'return', 'if', 'else', 'for', 'while', 'do', 'switch', 'case',
    'break', 'continue', 'class', 'struct', 'public', 'private', 'protected',
    'virtual', 'override', 'new', 'delete', 'using', 'namespace', 'include',
    'main', 'std', 'cout', 'cin', 'endl', 'nullptr', 'true', 'false', 'this',
}


class Part:
    def __init__(self, stage, index, title, card, anchor):
        self.stage = stage
        self.index = index
        self.title = title
        self.card = card
        self.anchor = anchor
        self.chunks = []  # (idx, channel, text, nearest_anchor)


def load_parts(soup):
    parts = []
    for group in soup.select('.stage-parts'):
        stage = group.get('data-part-stage', '?')
        idx = 0
        for card in group.find_all(recursive=False):
            if card.name != 'div':
                continue
            h3 = card.find('h3')
            title = h3.get_text(' ', strip=True) if h3 else '(无标题)'
            anchor = h3.get('id') if h3 and h3.get('id') else ''
            parts.append(Part(stage, idx, title, card, anchor))
            idx += 1
    return parts


def build_chunks(parts):
    """按文档顺序给正文/代码/表格分块编号，并记录每个 id 的位置。"""
    anchor_order = {}
    order = 0
    for part in parts:
        header = None
        h3 = part.card.find('h3')
        if h3 is not None and h3.parent is not None:
            header = h3.parent
        last_id = part.anchor
        for node in part.card.descendants:
            if isinstance(node, Comment):
                continue
            if isinstance(node, Tag):
                if node.get('id') and node.get('id') not in anchor_order:
                    anchor_order[node.get('id')] = order
                    if node.name != 'h3':
                        last_id = node.get('id')
                continue
            if not isinstance(node, NavigableString):
                continue
            parent = node.parent
            if parent is None:
                continue
            if header is not None and header in parent.parents:
                continue
            if parent.name in ('script', 'style') or parent.find_parent('script') or parent.find_parent('style'):
                continue
            text = str(node).strip()
            if not text:
                continue
            if parent.find_parent('code') or parent.find_parent('pre'):
                channel = 'code'
            elif parent.find_parent('table'):
                channel = 'table'
            else:
                channel = 'body'
            part.chunks.append((order, channel, text, last_id))
            order += 1
    return order, anchor_order


def anchor_index(anchor, anchor_order, total):
    if anchor in anchor_order:
        return anchor_order[anchor]
    return None


def location(part, anchor):
    return '阶段%s / %02d %s / #%s' % (part.stage, part.index, part.title, anchor or '-')


def check_terms(entries, parts, anchor_order, total, verbose):
    problems = []
    exemptions = []
    for entry in entries:
        term = entry['term']
        pattern = re.compile(entry['match'])
        first_def = entry.get('first_def', '')
        if anchor_index(first_def, anchor_order, total) is None:
            problems.append({
                'kind': '锚点缺失', 'term': term, 'where': '-',
                'def': '#' + first_def, 'fix': '在 index.html 里补上 first_def 锚点，或修正清单',
            })
            continue
        def_pos = anchor_order[first_def]
        for channel in ('body', 'table', 'code'):
            first = None
            for part in parts:
                for idx, ch, text, near in part.chunks:
                    if ch != channel:
                        continue
                    if pattern.search(text):
                        first = (idx, part, text, near)
                        break
                if first:
                    break
            if first is None:
                continue
            eidx, part, etext, enear = first
            if eidx >= def_pos:
                continue
            reason = None
            for ex in entry.get('exempt', []):
                if str(ex.get('stage')) == str(part.stage) and int(ex.get('part', -1)) == part.index:
                    if ex.get('channel') in ('any', channel):
                        reason = ex.get('reason', '')
                        break
            item = {
                'kind': '先用后讲', 'term': term, 'channel': channel,
                'where': location(part, enear), 'def': '#' + first_def,
                'snippet': etext[:60].replace('\n', ' '),
                'fix': entry.get('fix', '在首次出现前补一句解释，或把讲解前移'),
            }
            if reason is None:
                problems.append(item)
            else:
                item['reason'] = reason
                exemptions.append(item)
    return problems, exemptions


def strip_code(text):
    text = re.sub(r'//[^\n]*', '', text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    text = re.sub(r'"(\\.|[^"\\])*"', '""', text)
    text = re.sub(r"'(\\.|[^'\\])*'", "''", text)
    return text


def check_tables(parts, verbose):
    problems = []
    declared = set()
    for part in parts:
        # 先收集本 Part 之前的类定义
        for pre in part.card.find_all('pre'):
            code = pre.get_text()
            for m in re.finditer(r'\b(?:class|struct)\s+([A-Za-z_]\w*)', code):
                declared.add(m.group(1))
        for cell in part.card.find_all(['td', 'th']):
            text = cell.get_text(' ', strip=True)
            if not text:
                continue
            near = part.anchor
            for anc in cell.find_parents(id=True):
                near = anc.get('id')
                break
            for m in re.finditer(r'\b([A-Z][A-Za-z0-9_]*)\s+([a-zA-Z_]\w*)\s*(?:=|\(|;)', text):
                typ = m.group(1)
                if typ in STD_TYPES or typ in KEYWORDS or typ in declared:
                    continue
                problems.append({
                    'kind': '表格未定义类型', 'term': typ,
                    'where': location(part, near), 'def': '（表格之前未声明）',
                    'snippet': text[:70],
                    'fix': '在表格之前介绍 %s，或把表格里的示例改成已讲类型' % typ,
                })
            # 单元格自己声明了 a / b（如 auto a = 42、char a[8]、void f(int b)），不算未定义
            declares_ab = re.search(
                r'\b(?:int|double|float|char|bool|long|short|unsigned|auto|std::string)\s+[ab]\b', text)
            if (not declares_ab and re.search(r'[=+<>]', text)
                    and re.search(r'(?<![A-Za-z0-9_])[ab](?![A-Za-z0-9_])', text)):
                problems.append({
                    'kind': '表格未定义变量', 'term': 'a / b',
                    'where': location(part, near), 'def': '（表格之前未声明）',
                    'snippet': text[:70],
                    'fix': '在表格前说明 a、b 分别指哪两个变量，或换成具体名字',
                })
    return problems


DECL_RE = re.compile(
    r'(?:^|[\s;({])(?:const\s+|static\s+)?'
    r'(int|double|float|char|bool|long|short|unsigned|auto|std::string|void|'
    r'[A-Z][A-Za-z0-9_]*)\s*\*?\s*&?\s*([A-Za-z_]\w*)\s*(?:=|;|\()')

TYPE_ALT = r'int|double|float|char|bool|long|short|unsigned|auto|std::string|void|[A-Z][A-Za-z0-9_]*'


def check_code(parts, verbose):
    """代码块内的声明顺序检查。

    类成员（类体内声明，成员函数可先用后见）与函数形参（签名处即声明）
    单独处理，避免把合法的 C++ 写法误报。
    """
    problems = []
    for part in parts:
        for pre in part.card.find_all('pre'):
            clean = strip_code(pre.get_text())
            lines = clean.split('\n')

            class_ranges = []
            cur = None
            depth = 0
            for i, line in enumerate(lines):
                if cur is None and re.search(r'\b(?:class|struct)\s+\w+', line) and '{' in line:
                    cur = i
                    depth = 0
                if cur is not None:
                    depth += line.count('{') - line.count('}')
                    if depth <= 0 and i > cur:
                        class_ranges.append((cur, i))
                        cur = None

            def in_class(i):
                return any(a <= i <= b for a, b in class_ranges)

            declared = {}
            # 类成员：类一开始就视为已声明
            for a, b in class_ranges:
                for i in range(a, b + 1):
                    for m in re.finditer(
                            r'\b(?:%s)\s*\*?\s*&?\s*([A-Za-z_]\w*)\s*(?:;|=|\[)' % TYPE_ALT, lines[i]):
                        name = m.group(1)
                        if name not in KEYWORDS:
                            declared.setdefault(name, a)
            # 形参：函数签名那一行即已声明
            for i, line in enumerate(lines):
                sig = re.search(r'\b([A-Za-z_]\w*)\s*\(([^;{}]*)\)\s*(?:const\s*)?(?:[:{]|;)', line)
                if not sig:
                    continue
                for pm in re.finditer(r'\b(?:%s)\s*&?\s*([A-Za-z_]\w*)' % TYPE_ALT, sig.group(2)):
                    name = pm.group(1)
                    if name not in KEYWORDS:
                        declared.setdefault(name, i)
            # 普通声明（类体外）
            for i, line in enumerate(lines):
                if in_class(i):
                    continue
                for m in DECL_RE.finditer(line):
                    name = m.group(2)
                    if name not in KEYWORDS:
                        declared.setdefault(name, i)
                for m in re.finditer(r'\b(?:class|struct)\s+([A-Za-z_]\w*)', line):
                    declared.setdefault(m.group(1), i)

            for name, decl_line in declared.items():
                if name in KEYWORDS or len(name) < 2:
                    continue
                for ln in range(decl_line):
                    if re.match(r'\s*#', lines[ln]):
                        continue
                    if re.search(r'\b%s\b' % re.escape(name), lines[ln]):
                        problems.append({
                            'kind': '代码先用后声明', 'term': name,
                            'where': location(part, part.anchor),
                            'def': '代码块第 %d 行才声明' % (decl_line + 1),
                            'snippet': lines[ln].strip()[:70],
                            'fix': '把 %s 的声明移到首次使用之前，或调整示例顺序' % name,
                        })
                        break
    return problems


TEMPLATE_SKIP = {
    'include', 'template', 'using', 'namespace', 'return', 'if', 'while', 'for',
    'switch', 'case', 'else', 'operator', 'typedef', 'sizeof', 'decltype',
    'static_cast', 'reinterpret_cast', 'const_cast', 'dynamic_cast', 'noexcept',
    'alignof', 'throw', 'catch',
}
TEMPLATE_RE = re.compile(
    r'\b([A-Za-z_][\w:]*)\s*<\s*([A-Za-z_][\w:]*(?:\s*,\s*[A-Za-z_][\w:]*)?)\s*>')


def template_hits(text):
    """提取 text 里的“标识符<类型>”写法，跳过 include / template 声明等。"""
    for m in TEMPLATE_RE.finditer(text):
        name = m.group(1)
        if name in TEMPLATE_SKIP:
            continue
        # 单个小写标识符更像比较运算里的变量（i < n），保守跳过
        if len(name) == 1 and name.islower():
            continue
        yield name, m.group(2), m.group(0)


def check_template_syntax(entries, parts, anchor_order, total):
    """模板实参写法首次出现必须晚于模板语法讲解（detect=template 的条目）。"""
    targets = [e for e in entries if e.get('detect') == 'template']
    defs = []
    for e in targets:
        pos = anchor_index(e.get('first_def', ''), anchor_order, total)
        if pos is not None:
            defs.append((pos, e))
    if not defs:
        return []
    def_pos, entry = min(defs, key=lambda x: x[0])
    first_def = entry.get('first_def')
    problems = []
    seen = set()
    for part in parts:
        for idx, channel, text, near in part.chunks:
            if idx >= def_pos:
                break
            hit = next(template_hits(text), None)
            if not hit:
                continue
            key = (part.stage, part.index, channel)
            if key in seen:
                break
            seen.add(key)
            problems.append({
                'kind': '模板写法先用后讲', 'term': '%s<%s>' % (hit[0], hit[1]),
                'where': location(part, near), 'def': '#' + first_def,
                'snippet': text[:60].replace('\n', ' '),
                'fix': '这个 Part 还没讲模板，先改成不依赖 <类型> 的写法；模板示例留到 #%s' % first_def,
            })
            break
    return problems


def check_manifest_anchors(entries, anchor_order):
    """清单里的讲解锚点必须真实存在，否则术语表会跳到空处。"""
    problems = []
    for entry in entries:
        for key in ('first_def', 'full_def'):
            anchor = entry.get(key)
            if anchor and anchor not in anchor_order:
                problems.append({
                    'kind': '锚点缺失', 'term': entry['term'],
                    'where': '-', 'def': '#' + anchor,
                    'snippet': '（清单 %s 字段）' % key,
                    'fix': '在 index.html 里补上 id="%s"，或修正清单里的 %s' % (anchor, key),
                })
    return problems


def usage_locations(entries, parts):
    """统计每个条目在正文/表格/代码里出现过的 Part（供 Markdown 视图使用）。"""
    for entry in entries:
        pattern = re.compile(entry['match'])
        seen = []
        for part in parts:
            hit = any(pattern.search(text) for _, _, text, _ in part.chunks)
            if hit:
                seen.append('阶段%s / %02d' % (part.stage, part.index))
        entry['_usage'] = '、'.join(seen) if seen else '（页面未出现）'


def gen_markdown(entries):
    lines = [
        '# 知识依赖清单（人读视图）',
        '',
        '> 本文件由 `python tools/check_deps.py --sync-md` 从 `知识依赖清单.json` 自动生成，请勿手改。',
        '> 单一事实来源是 `知识依赖清单.json`；本表只做阅读与评审之用。',
        '',
        '| 条目 | 类型 | 首次讲解位置 | 完整讲解 | 页内使用位置 | 备注 |',
        '| --- | --- | --- | --- | --- | --- |',
    ]
    for entry in entries:
        typ = {'concept': '概念', 'name': '名称', 'syntax': '语法'}.get(entry['type'], entry['type'])
        note = entry.get('note', '')
        ex = entry.get('exempt') or []
        if ex:
            note = ('%s 豁免：%s' % (note + '；' if note else '', ex[0].get('reason', ''))).strip('；')
        lines.append('| %s | %s | #%s | #%s | %s | %s |' % (
            entry['term'], typ, entry.get('first_def', '-'),
            entry.get('full_def', entry.get('first_def', '-')),
            entry.get('_usage', ''), note))
    lines.append('')
    concepts = sum(1 for e in entries if e['type'] == 'concept')
    names = sum(1 for e in entries if e['type'] == 'name')
    syntaxes = sum(1 for e in entries if e['type'] == 'syntax')
    lines.append('共 %d 条：概念 %d 条，名称 %d 条，语法 %d 条。' % (len(entries), concepts, names, syntaxes))
    lines.append('')
    return '\n'.join(lines)


def term_sort_key(term):
    """按拼音排序（pypinyin 可选；没装时退回系统中文排序，保证脚本仍可跑）。"""
    try:
        from pypinyin import lazy_pinyin
        return (''.join(lazy_pinyin(term)).lower(), term)
    except ImportError:
        try:
            locale.setlocale(locale.LC_COLLATE, 'Chinese_China.936')
        except Exception:
            pass
        return (locale.strxfrm(term), term)


def build_glossary(entries):
    items = []
    for entry in entries:
        if entry.get('type') == 'syntax':
            continue
        brief = entry.get('brief', '').strip()
        anchor = entry.get('full_def') or entry.get('first_def')
        if not brief or not anchor:
            continue
        items.append((entry['term'], brief, anchor, entry['type']))
    items.sort(key=lambda x: term_sort_key(x[0]))
    out = []
    for term, brief, anchor, typ in items:
        label = '概念' if typ == 'concept' else '名称'
        out.append(
            '        <a class="glossary-item" href="#%s" data-glossary>'
            '<span class="glossary-term">%s</span>'
            '<span class="glossary-brief">%s</span>'
            '<span class="glossary-kind">%s</span></a>' % (
                html_mod.escape(anchor), html_mod.escape(term),
                html_mod.escape(brief), label)
        )
    return '\n'.join(out), len(items)


def sync_glossary(page, entries):
    generated, count = build_glossary(entries)
    start = '<!-- GLOSSARY:LIST:START -->'
    end = '<!-- GLOSSARY:LIST:END -->'
    if start not in page or end not in page:
        print('! 页面缺少术语表标记，跳过生成')
        return page, 0
    head, rest = page.split(start, 1)
    _, tail = rest.split(end, 1)
    body = '\n' + generated + '\n        '
    return head + start + body + end + tail, count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sync-glossary', action='store_true')
    ap.add_argument('--sync-md', action='store_true')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    page = open(INDEX, encoding='utf-8').read()
    manifest = json.load(open(MANIFEST, encoding='utf-8'))
    entries = manifest['entries']

    soup = BeautifulSoup(page, 'lxml')
    parts = load_parts(soup)
    total, anchor_order = build_chunks(parts)

    anchor_problems = check_manifest_anchors(entries, anchor_order)
    term_problems, exemptions = check_terms(entries, parts, anchor_order, total, args.verbose)
    table_problems = check_tables(parts, args.verbose)
    code_problems = check_code(parts, args.verbose)
    template_problems = check_template_syntax(entries, parts, anchor_order, total)
    all_problems = (anchor_problems + term_problems + table_problems
                    + code_problems + template_problems)

    print('知识依赖检查：%d 个条目，%d 个 Part' % (len(entries), len(parts)))
    for p in all_problems:
        print('[%s] %s' % (p['kind'], p['term']))
        print('    首次使用：%s' % p['where'])
        print('    讲解位置：%s' % p['def'])
        if p.get('snippet'):
            print('    片段：%s' % p['snippet'])
        print('    建议：%s' % p['fix'])
    if args.verbose:
        for e in exemptions:
            print('[豁免] %s @ %s（%s）理由：%s' % (e['term'], e['where'], e['channel'], e['reason']))
    print('---')
    print('违规 %d 项（锚点缺失 %d / 先用后讲 %d / 表格 %d / 代码 %d / 模板 %d），豁免 %d 项' % (
        len(all_problems), len(anchor_problems), len(term_problems),
        len(table_problems), len(code_problems), len(template_problems), len(exemptions)))

    if args.sync_glossary:
        page2, count = sync_glossary(page, entries)
        if count:
            open(INDEX, 'w', encoding='utf-8', newline='\n').write(page2)
            print('术语表已刷新：%d 条' % count)

    if args.sync_md:
        usage_locations(entries, parts)
        md_path = os.path.join(ROOT, '知识依赖清单.md')
        open(md_path, 'w', encoding='utf-8', newline='\n').write(gen_markdown(entries))
        print('清单 Markdown 视图已刷新：%s' % md_path)

    sys.exit(1 if all_problems else 0)


if __name__ == '__main__':
    main()
