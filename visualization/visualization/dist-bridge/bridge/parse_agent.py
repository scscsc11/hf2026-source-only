"""Bridge 内嵌的参赛者算法入口类发现脚本。

被 algorithm-discovery.ts 的 parsePyFile() spawn 调用:
    python parse_agent.py <py_file> <base_class_names>

base_class_names 是逗号分隔的基类名清单(如 'SearchTrackAgent,Agent')。
脚本用 ast 静态分析,不 import 不执行参赛者代码。
stdout 输出一行 JSON, stderr 仅诊断信息。
"""
import ast
import json
import sys


def base_names(node: ast.ClassDef) -> list[str]:
    """提取 ClassDef.bases 里的基类名字符串。

    覆盖:
      class X(SearchTrackAgent)      -> ['SearchTrackAgent']
      class X(agent.SearchTrackAgent)-> ['SearchTrackAgent']
      class X(A, B)                  -> ['A', 'B']
    """
    names = []
    for b in node.bases:
        if isinstance(b, ast.Name):
            names.append(b.id)
        elif isinstance(b, ast.Attribute):
            names.append(b.attr)
    return names


def _iter_classes(node: ast.AST):
    """按源码顺序深度优先遍历所有 ClassDef 节点。

    用 ast.iter_child_nodes 递归而非 ast.walk —— 后者遍历顺序未定义
    (Python 文档明确 "in no specified order"), 会让"取第一个匹配类"的
    结果不确定。深度优先保证了源码定义顺序。
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            yield child
        yield from _iter_classes(child)


def find_entry_class(tree: ast.Module, targets: list[str]) -> ast.ClassDef | None:
    """返回第一个基类名命中 targets 的 ClassDef(按源码顺序深度优先)。"""
    for cls in _iter_classes(tree):
        for bn in base_names(cls):
            if bn in targets:
                return cls
    return None


def main() -> int:
    if len(sys.argv) < 3:
        print(json.dumps({"found": False, "error": "usage: parse_agent.py <file> <base_classes>"}))
        return 2
    py_file = sys.argv[1]
    targets = [t.strip() for t in sys.argv[2].split(",") if t.strip()]
    try:
        with open(py_file, "r", encoding="utf-8-sig") as f:
            src = f.read()
        tree = ast.parse(src, filename=py_file)
    except (SyntaxError, OSError) as e:
        print(json.dumps({"found": False, "error": f"parse_error: {e}"}))
        return 0  # 解析失败不阻塞,返回 found:false

    cls = find_entry_class(tree, targets)
    if cls is None:
        print(json.dumps({"found": False, "error": "no_matching_class"}))
        return 0

    doc = ast.get_docstring(cls) or ""
    short = doc.split("\n", 1)[0].strip() if doc else ""
    print(json.dumps({
        "found": True,
        "entryClass": cls.name,
        "shortName": short,
        "docstring": doc,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
