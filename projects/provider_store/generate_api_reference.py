"""Generate a deterministic, dependency-free Python API reference from source ASTs."""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


def _doc(node: ast.AST) -> str:
    value = ast.get_docstring(node, clean=True)
    return " ".join(value.split()) if value else ""


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    args = ast.unparse(node.args)
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix}def {node.name}({args}){returns}"


def render_api_reference(package_root: Path) -> str:
    package_root = package_root.resolve()
    if not package_root.is_dir():
        raise ValueError("provider-store package root must be a directory")
    modules: list[tuple[str, ast.Module, str]] = []
    for path in sorted(package_root.rglob("*.py")):
        if path.is_symlink() or any(part.startswith(".") or part == "__pycache__" for part in path.parts):
            continue
        if path.name == "__init__.py":
            module_name = ".".join(path.parent.relative_to(package_root.parent).parts) or package_root.name
        else:
            module_name = ".".join(path.relative_to(package_root.parent).with_suffix("").parts)
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise ValueError(f"cannot parse provider-store module: {path.name}") from exc
        modules.append((module_name, tree, _doc(tree)))

    lines = [
        "# Provider Store Python API Reference",
        "",
        "Generated from the provider-store Python source using AST parsing. Private symbols are omitted. "
        "This index documents declarations and source docstrings; it does not claim live-provider verification.",
        "",
    ]
    for module_name, tree, module_doc in modules:
        public_nodes = [
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and not node.name.startswith("_")
        ]
        if not public_nodes:
            continue
        lines.extend([f"## {module_name}", ""])
        if module_doc:
            lines.extend([module_doc, ""])
        for node in public_nodes:
            if isinstance(node, ast.ClassDef):
                bases = f"({', '.join(ast.unparse(base) for base in node.bases)})" if node.bases else ""
                lines.append(f"### class {node.name}{bases}")
                doc = _doc(node)
                if doc:
                    lines.extend(["", doc])
                for member in node.body:
                    if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) or member.name.startswith("_"):
                        continue
                    lines.extend(["", f"- `{_signature(member)}`"])
                    member_doc = _doc(member)
                    if member_doc:
                        lines.append(f"  - {member_doc}")
                lines.append("")
            else:
                lines.append(f"### {_signature(node)}")
                doc = _doc(node)
                if doc:
                    lines.extend(["", doc])
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--check", action="store_true", help="fail if the committed reference is stale")
    args = parser.parse_args(argv)
    output = args.output or (args.package_root / "PROVIDER_STORE_PYTHON_API.md")
    try:
        rendered = render_api_reference(args.package_root)
        if args.check:
            try:
                existing = output.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                print("API reference is missing or unreadable", file=sys.stderr)
                return 1
            if existing != rendered:
                print("API reference is stale; regenerate it with generate_api_reference.py", file=sys.stderr)
                return 1
            print(f"API reference is current: {output}")
            return 0
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(f"Generated API reference: {output}")
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
