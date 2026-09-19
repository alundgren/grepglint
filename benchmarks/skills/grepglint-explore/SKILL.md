---
name: grepglint-explore
description: Explore how a codebase implements a feature or behavior using Grepglint and targeted source reads. Use for implementation questions when the relevant files are not yet known or broad text searches return too many matches.
---

# Explore implementation with Grepglint

Prefer starting with the available `grepglint_search` tool for exploratory
implementation questions when you do not already know the relevant file.
Turn the question into a short group of related words or identifiers, for
example `migration dependency graph`. Pass it as the tool's `query` string;
regex, FTS operators and additional arguments are not supported.

Read promising result ranges to verify them. Results are lexical suggestions,
not exhaustive references or guaranteed answers. Use direct reads for known
files and `rg` for exact strings, regex, or all occurrences. Skip Grepglint when
those tools already provide a focused route; its use is optional.

The first search builds a bounded local index and may take several seconds.
If indexing fails, continue with `rg` and file reads. Repeating the same query
will not fix a capacity failure. Repository files remain unchanged and search
uses no network.
