# -*- coding: utf-8 -*-
"""发布前注释检查：只阻断硬性项，条数与密度仅作参考提示。

用法：
    python tools/check_comments.py            # 检查，硬性项违规时退出码 1
    python tools/check_comments.py --stats    # 只打印基线统计
    python tools/check_comments.py --verbose  # 打印每段明细

分五类：完整示例 / 片段 / 错误示例 / 练习初始代码 / 参考答案。

阻断项（硬性）：
    - 16 行以上代码块缺少顶部说明块
    - 参考答案缺少“思路说明”注释块
    - 无信息注释（// 输出、// 结束 等）
    - 非中文注释
    - 注释里出现前文未讲的术语
      （位置按文档序：代码块自身 Tag 位置 vs first_def 锚点元素位置，
        不拿 Part 标题当代码位置；折叠区代码块同样按自身位置判定）
    - 错误示例缺少“错在哪 / 为什么错”的说明

提示项（不计违规、不影响退出码）：
    - 按代码行数估算的参考条数（≤15 行 3 条；16～40 行 6 条；
      41～80 行 10 条；>80 行 15 条；参考答案 +2 条），低于该值时打印 [提示]。
    条数没有上限，也不判定行尾占比；按内容需要写即可（制作标准 §5.4.1）。
"""
import argparse
import json
import os
import re
import sys

from bs4 import BeautifulSoup, Tag

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_deps import ROOT, INDEX, MANIFEST, load_parts

TYPE_LABEL = {
    'complete': '完整示例',
    'snippet': '片段',
    'error': '错误示例',
    'practice': '练习初始',
    'answer': '参考答案',
}
ERROR_HINTS = ('错误', '错：', '错在', '反例', '不要运行', '无法编译', '二义性', '后果',
               'problem', '没有 virtual', '忘记', '少了', '写错', '未定义', '泄漏', '越界')
NONSENSE = {'输出', '输入', '结束', '返回', '说明', '示例', '代码', '运行', '提示',
            '测试', '主函数', '定义', '结果', '变量', '赋值', '判断', '循环', '开始',
            '定义变量', '输出结果', '打印', '调用', '创建对象', '头文件', '准备工作',
            '输出。', '输入。', '结束。', '结果。', '定义。', '判断。', '循环。'}
CJK = re.compile(r'[\u4e00-\u9fa5]')


def hint_count(kind, n_lines):
    """按行数估算的参考条数（仅用于 [提示]，不是硬性下限）。"""
    if n_lines <= 15:
        need = 3
    elif n_lines <= 40:
        need = max(6, -(-n_lines // 5))
    elif n_lines <= 80:
        need = max(10, -(-n_lines // 4))
    else:
        need = max(15, int(-(-n_lines * 2 // 7)))
    if kind == 'answer':
        need += 2
    return need


def has_top_block(text):
    """开头（前 6 行内）是否有连续 2～4 行注释组成的说明块。"""
    lines = text.splitlines()
    run = 0
    for i, line in enumerate(lines[:8]):
        if re.match(r'\s*//', line):
            run += 1
            if run >= 2:
                return True
        elif line.strip() and not line.lstrip().startswith('#'):
            # 遇到第一行实际代码就停止（#include 之前的注释仍算顶部）
            if i > 0:
                return False
        else:
            run = 0
    return run >= 2


class Block:
    def __init__(self, name, kind, text, stage, part, anchor, tag, pos=None):
        self.name = name
        self.kind = kind
        self.text = text
        self.stage = stage
        self.part = part
        self.anchor = anchor
        self.tag = tag
        self.pos = pos  # 代码块自己在文档里的位置（Tag 顺序号），用于术语先后比较
        self.comments = []  # (line_no, comment_text)

    @property
    def where(self):
        return '阶段%s / %s %s' % (self.stage, self.part, '#%s' % self.anchor if self.anchor else '')

    @property
    def code_lines(self):
        # 代码行：非空且不是整行注释；行尾注释仍算代码行
        return [l for l in self.text.splitlines() if l.strip() and not l.strip().startswith('//')]


def part_of(el, parts):
    for p in parts:
        if p.card is not None and (el is p.card or p.card in el.parents):
            return p
    return None


def document_positions(soup):
    """按文档顺序给每个 Tag 编一个递增位置号，并记下 id 属性首次出现的位置。

    代码块用它自己的 Tag 位置，术语讲解用 first_def 锚点元素的位置；两者直接
    按文档序比较。不再拿 Part 标题当代码位置——那会把“同一 Part 里先讲后写码”
    误判成先用后讲，也会让折叠区代码块被迫特殊处理。
    """
    tag_pos, id_pos = {}, {}
    for i, node in enumerate(soup.descendants):
        if not isinstance(node, Tag):
            continue
        tag_pos[id(node)] = i
        aid = node.get('id')
        if aid and aid not in id_pos:
            id_pos[aid] = i
    return tag_pos, id_pos


def classify(name, text, in_answer):
    if in_answer:
        return 'answer'
    if any(h in name for h in ERROR_HINTS):
        return 'error'
    if re.search(r'int\s+main\s*\(', text) and '#include' in text:
        return 'complete'
    return 'snippet'


def collect(soup, parts, tag_pos):
    blocks = []
    # 练习初始代码（textarea）
    for ta in soup.find_all('textarea', class_='practice-editor'):
        box = ta.find_parent(class_='practice-box')
        name = ''
        if box:
            h5 = box.find('h5')
            name = h5.get_text(' ', strip=True) if h5 else ''
        name = name or '练习初始代码'
        p = part_of(ta, parts)
        blocks.append(Block(name, 'practice', ta.get_text(), p.stage if p else '?',
                            '%02d' % p.index if p else '?', p.anchor if p else '', ta,
                            tag_pos.get(id(ta))))
    # 代码块
    for code in soup.find_all('code'):
        if not code.get('class'):
            continue
        if not code.find_parent('pre') or not code.find_parent(class_='code-wrap'):
            continue
        wrap = code.find_parent(class_='code-wrap')
        name = ''
        for sp in wrap.find_all('span'):
            if 'font-mono' in (sp.get('class') or []):
                name = sp.get_text(strip=True)
                break
        text = code.get_text()
        in_answer = bool(code.find_parent('details', id=re.compile('^answer-')))
        cat = classify(name, text, in_answer)
        p = part_of(code, parts)
        # 折叠区里的代码块也用自己在文档里的位置判定（见 document_positions）；
        # 这里取折叠区 id 只用于报错时显示位置，不参与先后比较。
        # 注意折叠区的外壳是 div（里层才是 details），所以按 id 认，不按标签认。
        fold = code.find_parent(
            lambda t: t.get('id') and re.match(r'^deep-p\d+-\d+$', t.get('id')))
        block_anchor = fold.get('id') if fold else (p.anchor if p else '')
        # 没写文件名的片段：用代码里的“错”注释或文件名提示判断类型
        if not name:
            name = TYPE_LABEL[cat]
            if re.search(r'//\s*(错|注意：|不要)', text):
                cat = 'error'
                name = '片段（含错误标记）'
        blocks.append(Block(name, cat, text, p.stage if p else '?',
                            '%02d' % p.index if p else '?', block_anchor, code,
                            tag_pos.get(id(code))))
    return blocks


def parse_comments(text, allow_hash=False):
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        m = re.match(r'//\s*(.*)$', s)
        if m:
            out.append((i, m.group(1).strip()))
        else:
            m2 = re.search(r'//\s*(.*)$', s)
            if m2 and not s.startswith('"'):
                out.append((i, m2.group(1).strip()))
        if allow_hash:
            m3 = re.match(r'#\s*(.*)$', s)
            if m3:
                out.append((i, m3.group(1).strip()))
    return out


def check_block(b, entries, id_pos):
    problems = []
    comments = b.comments
    code_lines = b.code_lines
    if len(code_lines) >= 16 and not has_top_block(b.text):
        problems.append({'type': '缺少顶部说明块', 'block': b,
                         'msg': '%d 行代码开头没有连续 2～4 行注释说明“这段演示什么、输入输出是什么、先看哪几行”' % len(code_lines),
                         'fix': '在 #include 之前或 main 开头补顶部说明块'})
    if b.kind == 'answer' and not any('思路' in c for _, c in comments):
        problems.append({'type': '缺少思路说明', 'block': b,
                         'msg': '参考答案没有“思路说明”注释块',
                         'fix': '在顶部补一段 // 思路：先……再……最后……'})
    for ln, c in comments:
        body = c.strip(' 。.！!：:；;、')
        if body in NONSENSE:
            problems.append({'type': '无信息注释', 'block': b,
                             'msg': '第 %d 行注释“%s”没有信息量' % (ln, c),
                             'fix': '删除或改成说明意图的注释'})
        elif not CJK.search(c):
            problems.append({'type': '非中文注释', 'block': b,
                             'msg': '第 %d 行注释“%s”不含中文' % (ln, c),
                             'fix': '改成中文注释（标识符可保留英文）'})
    if b.kind == 'error':
        has_reason = any(any(h in c for h in ('错', '应', '不要', '问题', '少了', '越界', '泄漏'))
                         for _, c in comments)
        if not has_reason:
            problems.append({'type': '错因未标注', 'block': b,
                             'msg': '错误示例缺少“错在哪/为什么错”的注释',
                             'fix': '在出错行旁加 // 错：… 说明原因'})
    # 注释里的概念必须已在前文讲：代码块位置 vs 术语首次讲解位置的文档序比较。
    pos = b.pos if b.pos is not None else 0
    for ln, c in comments:
        for e in entries:
            if e['type'] != 'concept':
                continue
            if not re.search(e['match'], c):
                continue
            d = id_pos.get(e.get('first_def', ''))
            if d is not None and d > pos:
                problems.append({'type': '注释术语未讲', 'block': b,
                                 'msg': '第 %d 行注释用到“%s”，讲解在 #%s（更靠后）' % (ln, e['term'], e['first_def']),
                                 'fix': '改写注释用已讲过的说法，或先在正文补讲'})
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--verbose', action='store_true')
    ap.add_argument('--stats', action='store_true')
    args = ap.parse_args()

    html = open(INDEX, encoding='utf-8').read()
    soup = BeautifulSoup(html, 'lxml')
    parts = load_parts(soup)
    tag_pos, id_pos = document_positions(soup)
    entries = json.load(open(MANIFEST, encoding='utf-8'))['entries']
    blocks = collect(soup, parts, tag_pos)

    stats = {k: {'n': 0, 'with': 0, 'comments': 0, 'lines': 0, 'short': []} for k in TYPE_LABEL}
    for b in blocks:
        b.comments = parse_comments(b.text, allow_hash=('g++' in b.text))
        s = stats[b.kind]
        s['n'] += 1
        s['comments'] += len(b.comments)
        s['lines'] += len(b.code_lines)
        if len(b.comments) > 0:
            s['with'] += 1
        if len(b.comments) < hint_count(b.kind, len(b.code_lines)):
            s['short'].append(b.name)

    print('代码块总数：%d（代码块 %d + 练习初始 %d）' % (
        len(blocks), len([b for b in blocks if b.kind != 'practice']),
        len([b for b in blocks if b.kind == 'practice'])))
    for k in ('complete', 'snippet', 'error', 'practice', 'answer'):
        s = stats[k]
        ratio = 100.0 * s['comments'] / max(1, s['lines'])
        print('  %-6s %2d 段，含注释 %2d 段，注释 %3d 条，占代码行 %.1f%%' % (
            TYPE_LABEL[k], s['n'], s['with'], s['comments'], ratio))

    problems = []
    hints = []
    for b in blocks:
        problems.extend(check_block(b, entries, id_pos))
        need = hint_count(b.kind, len(b.code_lines))
        if len(b.comments) < need:
            hints.append((b, need))

    if not args.stats:
        for p in problems:
            print('[%s] %s @ %s %s' % (p['type'], p['block'].name, p['block'].where,
                                       '(%s)' % TYPE_LABEL[p['block'].kind]))
            print('    %s' % p['msg'])
            print('    建议：%s' % p['fix'])
        for b, need in hints:
            print('[提示] %s @ %s（%s）：注释 %d 条，参考值 %d 条；低于参考值不影响发布' % (
                b.name, b.where, TYPE_LABEL[b.kind], len(b.comments), need))
        print('---')
        print('注释违规 %d 项（硬性项）；[提示] %d 项（不计违规）' % (len(problems), len(hints)))

    if args.verbose or args.stats:
        for b in blocks:
            need = hint_count(b.kind, len(b.code_lines))
            print('· %-30s %-4s 注释 %2d / 参考 %2d / 代码 %3d 行 %s' % (
                b.name[:30], TYPE_LABEL[b.kind], len(b.comments), need, len(b.code_lines), b.where))

    sys.exit(1 if problems and not args.stats else 0)


if __name__ == '__main__':
    main()
