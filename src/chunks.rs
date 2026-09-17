use std::{
    path::Path,
    time::{Duration, Instant},
};
use tree_sitter::{Node, ParseOptions, Parser};

pub const MAX_FILE_BYTES: usize = 512 * 1024;
const MAX_LINES: usize = 60;
const OVERLAP: usize = 6;

pub fn parser_for(path: &str) -> Option<&'static str> {
    if path.split('/').any(|p| {
        [
            ".git",
            "node_modules",
            "vendor",
            "dist",
            "build",
            "coverage",
            "target",
            ".next",
            ".cache",
        ]
        .contains(&p)
    }) {
        return None;
    }
    let file = Path::new(path).file_name()?.to_str()?;
    if [
        "pnpm-lock.yaml",
        "package-lock.json",
        "yarn.lock",
        "bun.lock",
        "Cargo.lock",
    ]
    .contains(&file)
    {
        return None;
    }
    let ext = Path::new(path)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("");
    match ext {
        "ts" | "mts" | "cts" | "js" | "mjs" | "cjs" => Some("typescript-v1"),
        "tsx" | "jsx" => Some("tsx-v1"),
        "py" | "go" | "rs" | "c" | "h" | "cpp" | "cc" | "hpp" | "java" | "cs" | "rb" | "php"
        | "swift" | "kt" | "sh" | "bash" | "sql" | "lua" | "vue" | "svelte" | "html" | "css"
        | "scss" | "json" | "jsonc" | "md" | "mdx" | "yaml" | "yml" | "toml" | "ini" | "xml"
        | "graphql" | "proto" | "tf" | "txt" => Some("lines-v1"),
        _ if ["Dockerfile", "Makefile", "CMakeLists.txt"].contains(&file) => Some("lines-v1"),
        _ => None,
    }
}

#[derive(Debug)]
pub struct Chunk {
    pub start: usize,
    pub end: usize,
    pub symbol: Option<String>,
    pub content: String,
}

fn add(chunks: &mut Vec<Chunk>, lines: &[&str], start: usize, end: usize, symbol: Option<String>) {
    let mut first = start;
    while first <= end && chunks.len() < 256 {
        let last = end.min(first + MAX_LINES - 1);
        let content = lines[first - 1..last].join("\n");
        if !content.trim().is_empty() {
            chunks.push(Chunk {
                start: first,
                end: last,
                symbol: symbol.clone(),
                content,
            });
        }
        if last == end {
            break;
        }
        first = last + 1 - OVERLAP;
    }
}

fn symbol(node: Node<'_>, bytes: &[u8]) -> Option<String> {
    let named = node.child_by_field_name("name").or_else(|| {
        let mut cursor = node.walk();
        node.named_children(&mut cursor)
            .find(|n| n.kind() == "variable_declarator")
            .and_then(|n| n.child_by_field_name("name"))
    });
    named
        .and_then(|n| n.utf8_text(bytes).ok())
        .map(str::to_owned)
}

pub fn extract(bytes: &[u8], format: &str) -> Option<Vec<Chunk>> {
    if bytes.len() > MAX_FILE_BYTES || bytes.contains(&0) {
        return None;
    }
    let text = std::str::from_utf8(bytes).ok()?;
    let lines: Vec<_> = text.lines().collect();
    if lines.len() > 12_000 || lines.iter().any(|line| line.len() > 4000) {
        return None;
    }
    let mut regions = Vec::new();
    if format != "lines-v1" {
        let mut parser = Parser::new();
        let language = if format == "tsx-v1" {
            tree_sitter_typescript::LANGUAGE_TSX
        } else {
            tree_sitter_typescript::LANGUAGE_TYPESCRIPT
        };
        parser.set_language(&language.into()).ok()?;
        let deadline = Instant::now() + Duration::from_millis(100);
        let mut callback = |_: &tree_sitter::ParseState| Instant::now() >= deadline;
        let options = ParseOptions::new().progress_callback(&mut callback);
        let tree =
            parser.parse_with_options(&mut |offset, _| &bytes[offset..], None, Some(options));
        if let Some(tree) = tree {
            let mut cursor = tree.root_node().walk();
            for mut node in tree.root_node().named_children(&mut cursor) {
                if node.kind() == "export_statement" {
                    node = node.child_by_field_name("declaration").unwrap_or(node);
                }
                let start = node.start_position().row + 1;
                let end = (node.end_position().row + usize::from(node.end_position().column > 0))
                    .max(start);
                let name = symbol(node, bytes);
                if node.kind() == "class_declaration" || node.kind() == "abstract_class_declaration"
                {
                    if let Some(body) = node.child_by_field_name("body") {
                        regions.push((start, body.start_position().row + 1, name.clone()));
                        let mut members = body.walk();
                        for member in body.named_children(&mut members) {
                            let member_name = symbol(member, bytes)
                                .map(|m| format!("{}.{m}", name.as_deref().unwrap_or("default")));
                            let first = member.start_position().row + 1;
                            let last = (member.end_position().row
                                + usize::from(member.end_position().column > 0))
                            .max(first);
                            regions.push((first, last, member_name.or_else(|| name.clone())));
                        }
                    }
                } else if name.is_some() {
                    regions.push((start, end, name));
                }
            }
        }
    }
    let mut chunks = Vec::new();
    let mut covered = vec![false; lines.len()];
    for (start, end, name) in regions {
        let end = end.min(lines.len());
        if start > end {
            continue;
        }
        covered[start - 1..end].fill(true);
        add(&mut chunks, &lines, start, end, name);
    }
    let mut first = 1;
    while first <= lines.len() {
        if covered[first - 1] {
            first += 1;
            continue;
        }
        let mut last = first;
        while last < lines.len() && !covered[last] {
            last += 1;
        }
        add(&mut chunks, &lines, first, last, None);
        first = last + 1;
    }
    if chunks.len() >= 256 {
        return None;
    }
    chunks.sort_by_key(|c| c.start);
    Some(chunks)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn extracts_methods_and_fallback_without_whole_class_duplication() {
        let source = b"import x from 'x';\nexport class TokenService {\n  validateRefreshToken() {\n    return true;\n  }\n}\n";
        let chunks = extract(source, "typescript-v1").unwrap();
        assert!(chunks.iter().any(|c| c.symbol.as_deref()
            == Some("TokenService.validateRefreshToken")
            && c.start == 3
            && c.end == 5));
        assert!(
            chunks
                .iter()
                .any(|c| c.symbol.is_none() && c.content.contains("import"))
        );
        assert!(!chunks.iter().any(|c| c.start == 2 && c.end == 6));
    }
    #[test]
    fn large_binary_and_minified_files_are_bounded() {
        assert!(extract(b"abc\0def", "lines-v1").is_none());
        assert!(extract(&vec![b'a'; MAX_FILE_BYTES + 1], "lines-v1").is_none());
        assert!(extract(&vec![b'a'; 4001], "lines-v1").is_none());
        let source = "x\n".repeat(130);
        let chunks = extract(source.as_bytes(), "lines-v1").unwrap();
        assert_eq!((chunks[1].start, chunks[0].end), (55, 60));
        assert_eq!(chunks.last().unwrap().end, 130);
    }
}
