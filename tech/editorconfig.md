# EditorConfig

## What it is

EditorConfig is a tiny file format, `.editorconfig`, that tells text editors how to format files: tabs or spaces, line endings, character encoding. Most editors (VS Code, IntelliJ, Vim, Sublime) read it, either built in or through a plug-in. It means every contributor's editor behaves the same way without personal settings.

## Why this repository uses it

Mixed indentation and stray trailing spaces create noisy diffs. A Makefile **requires** tabs while Python and YAML must use spaces. One file states each rule.

## Where it appears

| File | Role |
| --- | --- |
| `.editorconfig` | The only file. Applies to every file in the repository. |

## How it works underneath

When you open a file, the editor looks for `.editorconfig` in the file's folder and each parent folder. `root = true` stops the search going higher. Sections in square brackets are glob patterns for file names; the settings under them apply to matching files. Later sections override earlier ones.

## Walkthrough of this repository's use

```text
root = true

[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
trim_trailing_whitespace = true
indent_style = space
indent_size = 4

[*.{yml,yaml,json,toml}]
indent_size = 2

[Makefile]
indent_style = tab

[*.md]
trim_trailing_whitespace = false
```

- `root = true`: stop searching upward.
- `[*]` applies to all files:
  - `charset = utf-8`: save as UTF-8 text.
  - `end_of_line = lf`: lines end with a line-feed (the Unix way), not Windows' carriage-return plus line-feed.
  - `insert_final_newline = true`: every file ends with a newline.
  - `trim_trailing_whitespace = true`: remove spaces at the end of lines.
  - `indent_style = space`, `indent_size = 4`: indent with four spaces (Python).
- `[*.{yml,yaml,json,toml}]`: the braces list alternatives; these files use `indent_size = 2`.
- `[Makefile]`: `indent_style = tab`, because Make requires a tab at the start of recipe lines.
- `[*.md]`: `trim_trailing_whitespace = false`, because in Markdown two trailing spaces mean a line break.

Ruff's formatter and `.editorconfig` agree on 4 spaces; ruff enforces it, EditorConfig helps while typing.

## Try it yourself

1. Open `Makefile` in an editor with EditorConfig support and press Tab: a tab character is inserted.
2. `grep -c $'\t' Makefile` counts lines containing a tab.
3. `grep -rn ' $' --include='*.py' src | head` shows no trailing-space lines (the output is empty).
4. `file Makefile` reports the type; `od -c Makefile | head -3` lets you see `\n` line ends and no `\r`.
5. Change `indent_size` for Python to 2, edit a Python file and see the editor use 2 spaces; revert.

## Mistakes people make

- Having editors that ignore the file (install the plug-in).
- Spaces in Makefiles.
- Trimming trailing spaces in Markdown where they matter.

## Check yourself

1. What does `root = true` do?
   *Answer: stops the editor looking in parent folders.*
2. Why a separate section for `Makefile`?
   *Answer: Make requires tabs.*
3. Why not trim whitespace in Markdown?
   *Answer: two trailing spaces create a line break.*
4. What are `lf` line endings?
   *Answer: Unix-style single line-feed characters.*
5. Does EditorConfig enforce rules?
   *Answer: no, it configures editors; tools such as ruff enforce.*

## Further reading

- EditorConfig specification: <https://spec.editorconfig.org/>
