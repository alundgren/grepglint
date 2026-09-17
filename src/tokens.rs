use anyhow::{Result, ensure};
use std::collections::BTreeSet;

pub fn tokens(text: &str) -> Vec<String> {
    let mut result = Vec::new();
    for word in text.split(|c: char| !(c.is_alphanumeric() || "_$-/.".contains(c))) {
        let word = word.trim_matches(|c: char| !c.is_alphanumeric());
        if word.is_empty() {
            continue;
        }
        let original = word.to_lowercase();
        result.push(original.clone());
        let chars: Vec<char> = word.chars().collect();
        let mut part = String::new();
        let mut parts = BTreeSet::new();
        for (i, &c) in chars.iter().enumerate() {
            let boundary = i > 0
                && c.is_uppercase()
                && (chars[i - 1].is_lowercase()
                    || chars[i - 1].is_numeric()
                    || (chars[i - 1].is_uppercase()
                        && chars.get(i + 1).is_some_and(|n| n.is_lowercase())));
            if (boundary || !c.is_alphanumeric()) && !part.is_empty() {
                parts.insert(std::mem::take(&mut part).to_lowercase());
            }
            if c.is_alphanumeric() {
                part.push(c);
            }
        }
        if !part.is_empty() {
            parts.insert(part.to_lowercase());
        }
        result.extend(parts.into_iter().filter(|p| p != &original));
    }
    result
}

pub fn searchable(text: &str) -> String {
    tokens(text).join(" ")
}

pub fn query(text: &str) -> Result<String> {
    ensure!(text.len() <= 2000, "Queries are limited to 2,000 bytes.");
    let terms: BTreeSet<_> = tokens(text).into_iter().collect();
    ensure!(
        !terms.is_empty(),
        "Enter words or identifiers to search for."
    );
    Ok(terms
        .into_iter()
        .take(32)
        .map(|term| format!("\"{term}\""))
        .collect::<Vec<_>>()
        .join(" OR "))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn identifiers_keep_originals_and_components() {
        let terms =
            tokens("validateRefreshToken HTTPServer snake_case kebab-case src/auth/token.ts");
        for expected in [
            "validaterefreshtoken",
            "validate",
            "refresh",
            "token",
            "httpserver",
            "http",
            "server",
            "snake_case",
            "snake",
            "case",
            "kebab-case",
            "src/auth/token.ts",
            "auth",
        ] {
            assert!(terms.iter().any(|term| term == expected), "{expected}");
        }
        assert_eq!(tokens("refresh refresh"), ["refresh", "refresh"]);
    }
    #[test]
    fn punctuation_is_not_fts_syntax() {
        assert!(query("\"'()*").is_err());
        assert_eq!(
            query("token OR refresh").unwrap(),
            "\"or\" OR \"refresh\" OR \"token\""
        );
    }
}
