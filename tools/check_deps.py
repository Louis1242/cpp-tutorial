# -*- coding: utf-8 -*-
"""发布前知识依赖检查：概念/名称/语法写法先行讲解 + 表格/代码未定义标识符。

用法：
    python tools/check_deps.py              # 只检查，违规时退出码 1
    python tools/check_deps.py --sync-glossary   # 已停用：仅提示改用 build_index.py
    python tools/check_deps.py --verbose    # 额外打印豁免与统计

清单：知识依赖清单.json（单一事实来源，页面概念列表读取由它生成的派生索引）
"""
import argparse
import json
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

# 未登记新词兜底扫描：预处理指令固定集合，另加“加粗短词 / 小节标题前缀”两种候选形态。
PREPROCESSOR_DIRECTIVES = {
    '#include', '#define', '#ifndef', '#ifdef', '#endif', '#pragma',
    '#undef', '#elif', '#else', '#error',
}
# 明显是描述性短语、不算术语的常见字眼
TERM_STOP_WORDS = {
    '注意', '提示', '原因', '结果', '写法', '做法', '问题', '情况', '时候',
    '第一次', '第二次', '下一次', '这一次', '上面', '下面', '前面', '后面',
    '这里', '那里', '本节', '本课', '本教程', '小结', '总结', '练习', '示例',
}
# 标题前缀里这些词说明它是“段落名”而不是术语
TERM_TITLE_STOP = re.compile(
    '示例|例子|写法|指南|小结|练习|对照|反例|形态|实操|完整|总结|清单|步骤|'
    '目录|为什么|是什么|怎么|怎样|如何|注意|提示|动手|示范|选型|第.步')
TERM_BAD_CHARS = set('的是了在把被让给和与或才就都也很更最只还又再能会要没有不'
                     '个这那个一上下里外前后：:，,。.；;！!？?“”"\'（）()、')

# 标题前缀白名单：这些是正文标题里的自然词组，不是读者会单独查的术语。
# 能登记成术语的优先登记；只有“同义重复 / 纯动作描述”才进白名单，并且必须写明理由。
TITLE_PREFIX_WHITELIST = {
    '数组传进函数': '标题用口语描述“把数组传给函数”这个动作；同一概念的术语「数组参数」'
                    '已在 kp-37 登记并被正文认领，再登记会造成同义重复',
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
    # std::array 在阶段二 #c-std-array 就已就地解释尖括号写法（登记在知识依赖清单），
    # 允许它带“运行时变量长度”的反例出现；模板机制本身仍留到阶段六 Part 02。
    'std::array',
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


# ---------- 回指与首现校验（警告级，默认不阻断） ----------
CN_STAGE = {'一': '1', '二': '2', '三': '3', '四': '4',
            '五': '5', '六': '6', '七': '7', '八': '8'}

# 人工核过的合法回指：按句子片段匹配，命中即跳过。宁可漏报，不制造大红。
BACKREF_OK = [
    '阶段五的析构函数',
    '阶段五的拷贝构造',
    '阶段五的动态绑定',
]

BACKREF_CLAIM_RE = re.compile(
    r'阶段([一二三四五六七八\d]+)'
    r'(?:\s*(?:Part|part)\s*(\d{1,2}))?'
    r'([^。；\n]{0,40}?)'
    r'(?:已经?|都|也)?(?:讲过|学过|介绍过|说明过|提到过|提过)')
GENERIC_BACKREF_RE = re.compile(
    r'(?:前面|上文|前文|之前|上面|前面几节|前面章节)(?:已经?|都|也)?'
    r'(?:讲过|学过|介绍过|说明过|提到过|提过)')
MENTION_RE = re.compile(
    r'以后会讲|后面会讲|后面再讲|以后再讲|后面学|之后会讲|将在阶段|留到阶段|'
    r'阶段[一二三四五六七八\d]+[^。；\n]{0,12}(?:会讲|再讲|细讲|展开|介绍|讲)|'
    r'后续阶段|下一阶段|后面细讲')


def build_anchor_meta(parts):
    """每个 id 属于哪个阶段 / Part，用于核对回指声称的位置。"""
    meta = {}
    for part in parts:
        if part.anchor:
            meta.setdefault(part.anchor, (part.stage, part.index, part.anchor))
        for tag in part.card.find_all(id=True):
            meta.setdefault(tag.get('id'), (part.stage, part.index, part.anchor))
    return meta


def part_prose(part):
    """把一个 Part 的正文（含行内代码与表格）拼成一段，跳过代码块。"""
    texts = []
    for node in part.card.descendants:
        if not isinstance(node, NavigableString):
            continue
        parent = node.parent
        if parent is None:
            continue
        if parent.find_parent('pre') or parent.find_parent('script') or parent.find_parent('style'):
            continue
        text = str(node).strip()
        if text:
            texts.append(text)
    return ' '.join(texts)


def pick_term(text, entries):
    """在文本里找清单条目，取命中的最长一项，减少“变量”“函数”这类短词误配。"""
    best = None
    for entry in entries:
        try:
            m = re.search(entry['match'], text)
        except re.error:
            continue
        if not m:
            continue
        if best is None or len(m.group(0)) > len(best[0]):
            best = (m.group(0), entry)
    return best[1] if best else None


def pick_term_first(text, entries):
    """取文本里最早出现的清单条目：动词后面的第一个词通常就是被提名的对象。"""
    best = None
    for entry in entries:
        try:
            m = re.search(entry['match'], text)
        except re.error:
            continue
        if not m:
            continue
        if best is None or m.start() < best[0]:
            best = (m.start(), entry)
    return best[1] if best else None


def pick_term_near(text, center, entries, radius=70):
    """在指名位置附近挑最近的清单条目，避免整段里“数组”这类高频词盖住真正被提名的词。"""
    lo, hi = max(0, center - radius), center + radius
    best = None
    for entry in entries:
        try:
            pat = re.compile(entry['match'])
        except re.error:
            continue
        for m in pat.finditer(text):
            if m.start() < lo or m.end() > hi:
                continue
            dist = abs((m.start() + m.end()) / 2.0 - center)
            if best is None or dist < best[0]:
                best = (dist, entry)
    return best[1] if best else None


def check_backrefs(entries, parts, anchor_meta):
    """核对“阶段X讲过 / 前面学过”是否指对位置。只报有清单条目可核对的，宁漏不误。"""
    warnings = []
    for part in parts:
        for sentence in re.split(r'[。；\n]', part_prose(part)):
            if any(ok in sentence for ok in BACKREF_OK):
                continue
            m = BACKREF_CLAIM_RE.search(sentence)
            if m:
                claimed = CN_STAGE.get(m.group(1), m.group(1))
                claimed_part = m.group(2)
                entry = pick_term(m.group(3), entries)
                if not entry:
                    continue
                target = entry.get('full_def') or entry.get('first_def', '')
                meta = anchor_meta.get(target)
                if not meta:
                    continue
                actual_stage, actual_part, _ = meta
                if claimed != actual_stage or (claimed_part and int(claimed_part) != actual_part):
                    warnings.append({
                        'term': entry['term'],
                        'where': location(part, part.anchor),
                        'claimed': '阶段%s%s' % (claimed, ' Part %s' % claimed_part if claimed_part else ''),
                        'def': '#' + target + '（阶段%s / %02d）' % (actual_stage, actual_part),
                        'snippet': sentence.strip()[:70],
                    })
                continue
            g = GENERIC_BACKREF_RE.search(sentence)
            if not g:
                continue
            entry = pick_term(sentence[g.end():g.end() + 30], entries)
            if not entry:
                continue
            target = entry.get('full_def') or entry.get('first_def', '')
            meta = anchor_meta.get(target)
            if not meta:
                continue
            actual = (int(meta[0]), meta[1])
            if actual > (int(part.stage), part.index):
                warnings.append({
                    'term': entry['term'],
                    'where': location(part, part.anchor),
                    'claimed': '前面/上文',
                    'def': '#' + target + '（阶段%s / %02d）' % (actual[0], actual[1]),
                    'snippet': sentence.strip()[:70],
                })
    return warnings


def check_first_occurrence(entries, parts, anchor_order, anchor_meta):
    """首现早于 first_def 的候选清单：区分合法提名（mention）与假前提（false-premise）。"""
    candidates = []
    for entry in entries:
        first_def = entry.get('first_def', '')
        if first_def not in anchor_order:
            continue
        def_pos = anchor_order[first_def]
        pattern = re.compile(entry['match'])
        occ = None
        for part in parts:
            for idx, channel, text, near in part.chunks:
                if idx >= def_pos:
                    break
                if pattern.search(text):
                    occ = (idx, channel, text, near, part)
                    break
            if occ:
                break
        if not occ:
            continue
        _, channel, text, near, part = occ
        # 用同一 Part 的正文窗口判断有没有“以后讲 / 阶段X会讲”的提名说明
        prose = part_prose(part)
        m = pattern.search(prose)
        if m:
            context = prose[max(0, m.start() - 60):m.end() + 60]
        else:
            context = text
        covered = any(
            str(ex.get('stage')) == str(part.stage) and int(ex.get('part', -1)) == part.index
            for ex in entry.get('exempt', [])
        )
        judgement = 'mention' if (MENTION_RE.search(context) or covered) else 'false-premise'
        candidates.append({
            'term': entry['term'],
            'judgement': judgement,
            'stage': part.stage,
            'where': location(part, near),
            'channel': channel,
            'def': '#' + first_def,
            'snippet': text[:60].replace('\n', ' '),
        })
    return candidates


DEAD_MENTION_OK = [
    # 人工确认“虽然目的地没直接出现该词，但确实讲到了”的提名，整句写进来兜底。
    # 阶段五 01 讲的是对象销毁，但没出现“生命周期”四个字；这两条前置清单确实是回查那里。
    '阶段五的析构函数、拷贝构造与继承（回查 对象生命周期',
    '阶段五的析构函数、拷贝构造与对象生命周期（回查 对象生命周期',
]

# 只认明确指向未来的措辞；“讲过”不算，避免把回指当提名。
DEAD_MENTION_RE = re.compile(
    r'以后会讲|后面会讲|后面再讲|以后再讲|后面学|之后会讲|将在阶段|留到阶段|'
    r'阶段[一二三四五六七八\d]+[^。；\n]{0,12}(?:会讲|再讲|细讲|展开|介绍|讲(?!过))|'
    r'后续阶段|下一阶段|后面细讲')

# 带锚点的“回查/回看/回顾”也是一种指向：声称链接那处讲过某个词。
BACKREF_LINK_RE = re.compile(r'回查|回看|回顾|参见|详见')

# 纯文字指认：“阶段二 Part 07”“阶段二 05 讲”。
PART_REF_RE = re.compile(
    r'阶段([一二三四五六七八\d]+)(?:的)?\s*(?:Part|part)\s*(\d{1,2})'
    r'|阶段([一二三四五六七八\d]+)(?:的)?\s*(\d{1,2})\s*(?:节|讲|部分)')


def _text_with_link_spans(block):
    """按文档顺序拼出块内文字，并记录每个 <a href> 出现前的字符位置。"""
    pieces, spans, pos = [], [], 0
    for node in block.descendants:
        if isinstance(node, Tag) and node.name == 'a' and node.get('href'):
            spans.append((pos, node))
        elif isinstance(node, NavigableString):
            if node.parent is None or node.parent.find_parent('pre'):
                continue
            t = str(node).strip()
            if not t:
                continue
            pieces.append(t)
            pos += len(t) + 1
    return ' '.join(pieces), spans


def check_dead_mentions(entries, parts, anchor_meta, anchor_order, soup):
    """提名目标校验：正文里“阶段X会讲 / 后面学 / 留到阶段 / 回查 #锚点”这类指向，
    若目的地 Part 里根本没出现被提名的词，就报 dead-mention。
    两种指认都认：句中的锚点链接、以及“阶段二 Part 07”这样的纯文字用法。
    只在能识别出清单条目时报告，宁漏不误；警告级、默认不阻断。"""
    warnings = []
    part_by_key = {(str(p.stage), p.index): p for p in parts}
    part_of_card = {id(p.card): p for p in parts}
    seen = set()

    def context(block):
        """找到块所属的 Part；阶段前置说明不在 Part 里，用段落级标签兜底。"""
        part = None
        stage = '?'
        cur = block
        while cur is not None:
            if id(cur) in part_of_card:
                part = part_of_card[id(cur)]
            sid = cur.get('id') if getattr(cur, 'attrs', None) else None
            if cur.name == 'section' and sid and sid.startswith('stage-'):
                stage = sid[len('stage-'):]
            cur = cur.parent
        if part is not None:
            return part, location(part, part.anchor), part.anchor
        return None, '阶段%s / 前置说明' % stage, 'stage-%s-intro' % stage

    def report(entry, where_label, where_key, target, target_label, snippet):
        # 以“完整讲解”位置为准：目的地只是顺带提一句（如避坑区一行“改用 vector”）不算讲过。
        full = entry.get('full_def') or entry.get('first_def', '')
        full_meta = anchor_meta.get(full)
        if full_meta is not None:
            if (str(full_meta[0]), full_meta[1]) == (str(target.stage), target.index):
                return
        elif re.search(entry['match'], target.card.get_text(' ', strip=True)):
            return
        key = (entry['term'], where_key, target.anchor)
        if key in seen:
            return
        if any(ok in snippet for ok in DEAD_MENTION_OK):
            return
        seen.add(key)
        warnings.append({
            'term': entry['term'],
            'where': where_label,
            'target': '%s（阶段%s / %02d）' % (target_label, target.stage, target.index),
            'snippet': snippet,
        })

    if soup is not None:
        blocks = soup.find_all(['p', 'li', 'td'])
    else:
        blocks = [b for p in parts for b in p.card.find_all(['p', 'li', 'td'])]

    for block in blocks:
        if block.find_parent('pre'):
            continue
        _, where_label, where_key = context(block)
        text, spans = _text_with_link_spans(block)

        # 一、带锚点链接的指认
        for start, a in spans:
            href = a.get('href', '')
            if not href.startswith('#'):
                continue
            tid = href[1:]
            meta = anchor_meta.get(tid)
            if not meta:
                continue
            target = part_by_key.get((str(meta[0]), meta[1]))
            if target is None:
                continue
            # 只取链接所在的分句：向前到最近的句号/分号，向后带出链接文字
            seg = re.split(r'[。；\n]', text[:start])[-1]
            head = seg[-120:]
            window = head + text[start:start + 70]
            is_forward = DEAD_MENTION_RE.search(window)
            is_backref = (BACKREF_LINK_RE.search(window)
                          and re.search(r'阶段[一二三四五六七八\d]', window))
            if not (is_forward or is_backref):
                continue
            # 链接文字往往就是被指认的词，优先用它；否则再看链接前后的最近条目
            entry = pick_term(a.get_text(' ', strip=True), entries)
            if entry is None:
                entry = pick_term_near(window, len(head), entries)
            if entry is None:
                continue
            report(entry, where_label, where_key, target, '#' + tid, window.strip()[:90])

        # 二、纯文字“阶段X Part NN”，没有链接也要核对目的地
        for m in PART_REF_RE.finditer(text):
            stage = CN_STAGE.get(m.group(1) or m.group(3), m.group(1) or m.group(3))
            number = m.group(2) or m.group(4)
            target = part_by_key.get((str(stage), int(number)))
            if target is None:
                continue
            # 只认紧跟在 Part 指认后面的前向动词（“提过”“讲完”不算）
            tail = text[m.end():m.end() + 12]
            if not re.match(r'\s*(?:的)?\s*(?:会讲|再讲|细讲|展开|介绍)', tail):
                continue
            # 被提名的 Y 通常跟在动词后面；若动词后直接收括号，说明 Y 在指认之前
            after = text[m.end():m.end() + 50]
            rest = re.sub(r'^\s*(?:的)?\s*(?:会讲|再讲|细讲|展开|介绍)', '', after).lstrip()
            entry = None if rest[:1] in '）)，。；：、' else pick_term_first(after, entries)
            if entry is None:
                left = re.split(r'[。；\n]', text[:m.start()])[-1][-50:]
                entry = pick_term_near(left, len(left), entries, radius=50)
            if entry is None:
                continue
            snippet = re.split(r'[。；\n]', text[:m.start()])[-1] + text[m.start():m.end()] + after
            report(entry, where_label, where_key, target,
                   '阶段%s Part %s 的文字指认' % (stage, number), snippet.strip()[:90])
    return warnings


# ---------- 解释滞后校验（警告级，默认不阻断） ----------
# 同一个错误出现过两回：术语第一次用在代码里，解释却攒到后一个 Part。
# 口径：解释性文字（名字后紧跟“：/是/表示/读作”等提示）必须落在首次出现的
# 同一 Part 或紧邻的下一个 Part；跨度更大就提示，让人工确认是补解释还是改回指。
LAG_CUE = re.compile(r'[：:]|是|表示|读作|指的是|意思是|一句话')
LAG_WINDOW = 24
LAG_MAX_PART_GAP = 1

# 人工核过的合法滞后：写成 (term, 首次所在阶段, 首次所在 Part 序号)。
LAG_OK = [
    # 例：('某个词', '3', 2)
]


def term_label(term):
    """从清单条目名里取出可读名字：优先第一段连续中文，取不到就退回空格前片段。"""
    m = re.search(r'[\u4e00-\u9fff]{2,}', term)
    if m:
        return m.group(0)
    return term.split(' ')[0]


def _part_gap(part_a, part_b):
    """两个 Part 的跨度：同阶段按序号差，跨阶段放大，避免把“邻 Part”判成合规。"""
    if str(part_a.stage) != str(part_b.stage):
        return abs(int(part_a.stage) - int(part_b.stage)) * 10
    return part_b.index - part_a.index


def check_explanation_lag(entries, parts):
    """首次出现在代码里、解释性文字却落在更后面的 Part，给警告。
    只在能从条目名里取到可读名字时检查，宁漏不误。"""
    warnings = []
    for entry in entries:
        if entry.get('type') != 'syntax':
            continue
        try:
            pattern = re.compile(entry['match'])
        except re.error:
            continue
        label = term_label(entry['term'])
        if len(label) < 2:
            continue
        first = None
        for part in parts:
            for idx, channel, text, near in part.chunks:
                m = pattern.search(text)
                if m:
                    first = (part, idx, channel, text, near, m.start())
                    break
            if first:
                break
        if first is None or first[2] != 'code':
            continue
        part0 = first[0]
        explain = None
        for part in parts:
            for idx, channel, text, near in part.chunks:
                if idx < first[1]:
                    continue
                # 同一块里也要允许：解释常常就写在首次出现的那一行注释里
                pos = text.find(label)
                if pos < 0:
                    continue
                tail = text[pos + len(label): pos + len(label) + LAG_WINDOW]
                if LAG_CUE.search(tail):
                    explain = (part, idx, channel, text, near, pos)
                    break
            if explain:
                break
        if explain is None:
            continue
        if (entry['term'], str(part0.stage), part0.index) in LAG_OK:
            continue
        if _part_gap(part0, explain[0]) <= LAG_MAX_PART_GAP:
            continue
        warnings.append({
            'term': entry['term'],
            'where': location(part0, first[4]),
            'explain': location(explain[0], explain[4]),
            'snippet': explain[3][max(0, explain[5] - 20): explain[5] + 50].replace('\n', ' '),
        })
    return warnings


# ---------- 悬空承诺校验（警告级，默认不阻断） ----------
# “留到阶段七的补充阅读 / 后面会讲 / 后续章节 / 暂不展开”这类推迟说法，
# 必须同时给出能落地的去向，否则读者按这句话去找会扑空。
DANGLING_RE = re.compile(
    r'留到(?:阶段|后面|后续|第)|留在(?:阶段|后面|后续)|放到(?:阶段|后面)|'
    r'后续(?:会|再|才|将)(?:讲|细讲|展开|介绍|学|补充)|'
    r'(?:后面|以后|之后|下一阶段)(?:会|再|才|将)(?:讲|细讲|展开|介绍|学|补充)|'
    r'后续章节|后续阶段|暂不展开|这里不深究|不再展开')
DANGLING_PART_RE = re.compile(
    r'阶段([一二三四五六七八\d]+)(?:的)?\s*(?:Part|part|部分)?\s*(\d{1,2})(?!\d)')
DANGLING_EXIT_RE = re.compile(r'想深入|可看|参见|查阅|再查|自行|参考')


def check_dangling_promises(parts, soup):
    """扫“留到后面再讲”类承诺，判断有没有真实锚点或明确的免责去向。

    一句里出现推迟说法时，只有满足下面任一条才算有去向：
    1) 句内有 href="#..."，且该 id 真实存在；
    2) 句内有「阶段X Part NN / 阶段X NN」，且该 Part 真实存在；
    3) 句内有查阅类动词（想深入可看 / 查阅 / 再查 / 参考），即明确的免责去向。
    否则报警告（警告级，不阻断退出码）。
    """
    all_ids = {tag.get('id') for tag in soup.find_all(id=True)}
    part_by_key = {(str(p.stage), p.index): p for p in parts}
    part_of_card = {id(p.card): p for p in parts}
    warnings = []
    seen = set()
    for block in soup.find_all(['p', 'li', 'td', 'div']):
        if block.find_parent('pre'):
            continue
        # div 只取“叶子块”（阶段导学这类只有文字和内联标签的容器），
        # 避免把整张卡片的所有文字拼成一段，重复报同一句。
        if block.name == 'div' and block.find(['p', 'li', 'td', 'div']):
            continue
        part = None
        stage = '?'
        cur = block
        while cur is not None:
            if id(cur) in part_of_card:
                part = part_of_card[id(cur)]
            sid = cur.get('id') if getattr(cur, 'attrs', None) else None
            if cur.name == 'section' and sid and sid.startswith('stage-'):
                stage = sid[len('stage-'):]
            cur = cur.parent
        where = location(part, part.anchor) if part else '阶段%s / 前置说明' % stage
        text, spans = _text_with_link_spans(block)
        if not DANGLING_RE.search(text):
            continue
        for sm in re.finditer(r'[^。；\n]+', text):
            seg = sm.group(0)
            m = DANGLING_RE.search(seg)
            if not m:
                continue
            ok = False
            for pos, a in spans:
                href = a.get('href', '')
                if sm.start() <= pos < sm.end() and href.startswith('#') and href[1:] in all_ids:
                    ok = True
                    break
            if not ok:
                pm = DANGLING_PART_RE.search(seg)
                if pm:
                    st = CN_STAGE.get(pm.group(1), pm.group(1))
                    if (str(st), int(pm.group(2))) in part_by_key:
                        ok = True
            if not ok and DANGLING_EXIT_RE.search(seg):
                ok = True
            if ok:
                continue
            key = (where, seg.strip()[:50])
            if key in seen:
                continue
            seen.add(key)
            warnings.append({
                'where': where,
                'promise': m.group(0),
                'snippet': seg.strip()[:90],
            })
    return warnings


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


def _directive_registered(word, entries):
    """预处理指令是否登记过：#pragma 可由 term “pragma once” 覆盖。"""
    token = word.lower().lstrip('#')
    for entry in entries:
        hay = (entry.get('term', '') + ' ' + entry.get('match', '')).lower()
        if token in hay:
            return True
    return False


def _term_registered(term, entries):
    """术语是否登记过：候选词出现在任一条目的 term / match 里就算已登记。

    用包含而不是精确相等，是为了让“static”能命中“static 成员”、
    “unique_ptr”能命中“std::unique_ptr”，避免把已讲过的词当成漏登。
    """
    token = term.strip().lower()
    if not token:
        return True
    for entry in entries:
        if token in entry.get('term', '').strip().lower():
            return True
        if token in entry.get('match', '').strip().lower():
            return True
    return False


def scan_unregistered_terms(entries, parts, page_text):
    """兜底扫描：页面里明显是术语、却没登记进清单的新词。

    只做警告，不改退出码。候选形态两类：
    1) 预处理指令（#include / #define / #ifndef ...）；
    2) 小节标题冒号前的前缀（“链接性：extern 声明...”这一类，排除“示例/写法”等段落名）。

    这两类都不依赖人工记得“这次新增了哪个词”。
    """
    warnings = []
    seen = set()

    def add(term, kind, part, anchor, snippet):
        key = (term, kind, part.stage, part.index, anchor)
        if key in seen:
            return
        seen.add(key)
        warnings.append({
            'term': term, 'kind': kind,
            'where': location(part, anchor),
            'snippet': snippet[:70].replace('\n', ' '),
        })

    for part in parts:
        for idx, channel, text, near in part.chunks:
            for m in re.finditer(r'#[A-Za-z_]+', text):
                word = m.group(0)
                if word not in PREPROCESSOR_DIRECTIVES:
                    continue
                if _directive_registered(word, entries):
                    continue
                add(word, '预处理指令', part, near, text)

        for el in part.card.find_all(['h3', 'h4']):
            text = el.get_text(' ', strip=True)
            if '：' not in text:
                continue
            prefix = text.split('：', 1)[0].strip()
            if not (2 <= len(prefix) <= 10):
                continue
            if prefix in TERM_STOP_WORDS or TERM_TITLE_STOP.search(prefix):
                continue
            if any(ch in TERM_BAD_CHARS for ch in prefix):
                continue
            if _term_registered(prefix, entries):
                continue
            if prefix in TITLE_PREFIX_WHITELIST:
                continue
            if page_text.count(prefix) < 2:
                continue
            add(prefix, '标题前缀', part, el.get('id') or part.anchor, prefix)

    return warnings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--index', default=INDEX,
                    help='要检查的页面文件；默认 index.html，可指向旧快照做对比')
    ap.add_argument('--manifest', default=MANIFEST,
                    help='要使用的知识依赖清单；默认 知识依赖清单.json，可指向旧清单做对比')
    ap.add_argument('--sync-glossary', action='store_true',
                    help='已停用：概念列表改由派生索引渲染，保留参数仅提示')
    ap.add_argument('--sync-md', action='store_true')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    page = open(args.index, encoding='utf-8').read()
    manifest = json.load(open(args.manifest, encoding='utf-8'))
    entries = manifest['entries']

    soup = BeautifulSoup(page, 'lxml')
    parts = load_parts(soup)
    total, anchor_order = build_chunks(parts)

    anchor_problems = check_manifest_anchors(entries, anchor_order)
    term_problems, exemptions = check_terms(entries, parts, anchor_order, total, args.verbose)
    table_problems = check_tables(parts, args.verbose)
    code_problems = check_code(parts, args.verbose)
    template_problems = check_template_syntax(entries, parts, anchor_order, total)
    anchor_meta = build_anchor_meta(parts)
    backref_warnings = check_backrefs(entries, parts, anchor_meta)
    first_candidates = check_first_occurrence(entries, parts, anchor_order, anchor_meta)
    dead_warnings = check_dead_mentions(entries, parts, anchor_meta, anchor_order, soup)
    lag_warnings = check_explanation_lag(entries, parts)
    dangling_warnings = check_dangling_promises(parts, soup)
    page_text = soup.get_text(' ', strip=True)
    unregistered = scan_unregistered_terms(entries, parts, page_text)
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

    # 回指校验：警告级，默认不阻断退出码
    print('---')
    print('回指提示 %d 项（警告级，不阻断）' % len(backref_warnings))
    for w in backref_warnings:
        print('[提示] 回指指错：%s' % w['term'])
        print('    出现：%s' % w['where'])
        print('    声称：%s；实际讲解：%s' % (w['claimed'], w['def']))
        print('    片段：%s' % w['snippet'])
        print('    建议：核对这句回指；确属合法回指就把整句加入 BACKREF_OK')

    # 首现位置校验：候选清单，区分合法提名与假前提
    mention_n = sum(1 for c in first_candidates if c['judgement'] == 'mention')
    false_n = len(first_candidates) - mention_n
    print('---')
    print('首现候选 %d 条（mention %d / false-premise %d，不阻断）' % (
        len(first_candidates), mention_n, false_n))
    by_stage = {}
    for c in first_candidates:
        if c['judgement'] == 'false-premise':
            by_stage[c['stage']] = by_stage.get(c['stage'], 0) + 1
    if by_stage:
        print('false-premise 分布：%s' % '、'.join(
            '阶段%s %d 条' % (k, by_stage[k]) for k in sorted(by_stage, key=lambda x: int(x))))
    for c in first_candidates:
        if c['judgement'] == 'mention' and not args.verbose:
            continue
        print('[候选-%s] %s' % (c['judgement'], c['term']))
        print('    实际首现：%s（%s）' % (c['where'], c['channel']))
        print('    登记位置：%s' % c['def'])
        print('    片段：%s' % c['snippet'])

    # 提名目标校验：警告级，默认不阻断退出码
    print('---')
    print('提名目标提示 %d 项（dead-mention，警告级，不阻断）' % len(dead_warnings))
    for w in dead_warnings:
        print('[提示] dead-mention：%s' % w['term'])
        print('    出现：%s' % w['where'])
        print('    指向：%s，但该 Part 里找不到这个词' % w['target'])
        print('    片段：%s' % w['snippet'])
        print('    建议：改指真正讲解该词的位置；确属合法提名就写进 DEAD_MENTION_OK')

    # 解释滞后校验：警告级，默认不阻断退出码
    print('---')
    print('解释滞后提示 %d 项（警告级，不阻断）' % len(lag_warnings))
    for w in lag_warnings:
        print('[提示] 解释滞后：%s' % w['term'])
        print('    首次出现：%s' % w['where'])
        print('    解释位置：%s' % w['explain'])
        print('    片段：%s' % w['snippet'])
        print('    建议：把解释移到首次出现的那一段（或紧邻的一段）；确属合法就写进 LAG_OK')

    # 悬空承诺校验：警告级，默认不阻断退出码
    print('---')
    print('悬空承诺提示 %d 项（警告级，不阻断）' % len(dangling_warnings))
    for w in dangling_warnings:
        print('[提示] 悬空承诺：%s' % w['promise'])
        print('    位置：%s' % w['where'])
        print('    片段：%s' % w['snippet'])
        print('    建议：补可点击锚点或明确的「阶段X Part NN」，或改写成“本书不讲，因为……，想深入可看 X”')

    # 未登记新词兜底扫描：警告级，默认不阻断退出码
    print('---')
    print('未登记新词提示 %d 项（警告级，不阻断）' % len(unregistered))
    for w in unregistered:
        print('[提示] 未登记新词：%s（%s）' % (w['term'], w['kind']))
        print('    位置：%s' % w['where'])
        print('    片段：%s' % w['snippet'])
        print('    建议：确认它是不是术语；是的话登记进 知识依赖清单.json，first_def 不得晚于首次使用')

    if args.sync_glossary:
        print('--sync-glossary 已停用：概念列表改由页面读取派生索引.json 运行时渲染，')
        print('请运行 python tools/build_index.py 重新生成派生数据。')

    if args.sync_md:
        usage_locations(entries, parts)
        md_path = os.path.join(ROOT, '知识依赖清单.md')
        open(md_path, 'w', encoding='utf-8', newline='\n').write(gen_markdown(entries))
        print('清单 Markdown 视图已刷新：%s' % md_path)

    sys.exit(1 if all_problems else 0)


if __name__ == '__main__':
    main()
