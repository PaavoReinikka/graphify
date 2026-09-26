import tree_sitter_bicep as tsb
from tree_sitter import Language, Parser

lang = Language(tsb.language())
parser = Parser(lang)
src = open("investigation/sample.bicep", "rb").read()
tree = parser.parse(src)


def show(node, depth=0, maxdepth=4):
    if depth > maxdepth:
        return
    text = src[node.start_byte:node.end_byte].decode("utf-8", "replace")
    snippet = text.replace("\n", " ")[:50]
    field = ""
    print(f"{'  ' * depth}{node.type}"
          f"{' [' + repr(snippet) + ']' if node.is_named and node.child_count == 0 else ''}"
          f"  <L{node.start_point[0]+1}>")
    for i in range(node.child_count):
        child = node.child(i)
        fname = node.field_name_for_child(i)
        if fname:
            print(f"{'  ' * (depth+1)}.{fname}:")
        show(child, depth + 1, maxdepth)


show(tree.root_node)
