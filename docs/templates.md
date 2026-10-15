# CV and cover letter templates

jaa builds CVs and cover letters by filling a LaTeX template with LaTeX generated from your
confirmed profile (ADR 0012, ADR 0029). This page is the **template contract**: what a template
must provide so jaa can fill it. `jaa templates check PATH` checks a template against it.

## Shipped templates

| Id | Kind | Look |
| --- | --- | --- |
| `moderncv-classic` | CV (default) | name header, dates in a left column |
| `moderncv-banking` | CV | centred header, full-width sections |
| `moderncv-casual` | CV | large right-aligned name, contact details in the footer |
| `moderncv-letter` | cover letter (default) | moderncv letter |

`jaa templates show ID --preview` builds a preview PDF from synthetic sample data (never your
profile) into `previews/` in the data directory.

The template files are MIT-licensed (this repository). They use the moderncv class (LPPL 1.3c),
which comes with the TeX distribution or Tectonic's bundle and is not shipped with jaa.

## Adding your own

```bash
jaa templates check my-template.zip          # report problems without adding
jaa templates add my-template.zip --id mine  # stored as user-mine; again with changes → @2
jaa templates add --candidate 3 --id old-cv  # from `jaa import cv cv.tex --as-template`
jaa templates default user-mine              # used when no --template is given
jaa cv build --template user-mine@1          # one build with a specific version
jaa templates delete user-mine               # every version; built PDFs are kept
```

A template is a single `.tex` file, a folder or a `.zip` archive. In a folder or archive the
main file is the `.tex` file with `\documentclass` (name it with `--main` if there are several),
and every path is relative to its folder. Allowed files: `.tex .cls .sty .cfg .def .clo .fd`
(UTF-8 text) and `.png .jpg .jpeg .pdf .otf .ttf`; at most 100 files and 5 MB. Hidden files,
`__MACOSX`, files outside the main file's folder and other types are skipped and listed. Files
named `document.*` or `photo.*` at the top level clash with jaa's build and are refused.

Added templates are stored in the database (and in exports and backups). Each version is
immutable, and every PDF's manifest records the template's id, version and SHA-256 over all its
files.

## Slot lines

The main file has these lines, each exactly once and alone on its line. jaa replaces each one
with generated LaTeX; every other line is copied unchanged.

| Slot | Where | Becomes |
| --- | --- | --- |
| `%%jaa:babel%%` | preamble | `\usepackage[english]{babel}` or `[polish]` |
| `%%jaa:header%%` | preamble | `\name`, `\title`, `\address`, `\mobile`, `\email`, `\extrainfo`, `\photo`, `\quote` |
| `%%jaa:body%%` | document | the CV sections; in a cover letter, the paragraphs |
| `%%jaa:consent%%` | document, CV only | your consent clause, or nothing |
| `%%jaa:letter%%` | preamble, cover letter only | `\recipient`, `\date`, `\opening`, `\closing` |

Preamble slots come before `\begin{document}`, the others after it.

## Macros

The generated LaTeX calls moderncv's macros. With `\documentclass{moderncv}` they all exist.
Any other class must define each of these itself (with `\newcommand`, `\def`,
`\NewDocumentCommand`, ... in the main file or a local `.sty`/`.cls`):

- CV: `\name{first}{last}`, `\address{line}{}{}`, `\mobile`, `\email`, `\extrainfo`,
  `\photo[size][frame]{file}`, `\quote`, `\makecvtitle`,
  `\cventry{dates}{title}{organization}{location}{}{description}`, `\cvitem{label}{text}`,
  `\cvlistitem{text}`, `\cvlanguage{name}{level}{comment}`; plus `\section` and `\title` from
  LaTeX.
- Cover letter: `\name`, `\address`, `\mobile`, `\email`, `\extrainfo`,
  `\recipient{name}{address}`, `\opening`, `\closing`, `\makelettertitle`,
  `\makeletterclosing`; plus `\date`.

Header lines are left out when the profile has no value, and `\photo` appears only when you pass
`--photo`. All text from your profile is escaped, so it can never act as a LaTeX command.

## File access

LaTeX engines can read any file you can read, and Tectonic has no sandbox. So an added template
must keep to its own folder:

- Refused: absolute, `~` or `..` paths (in `\input`, `\includegraphics`, `\openin`, font paths,
  ...), `\directlua` and other Lua code, `\write18`, `\ShellEscape`, `\openout` and piped
  `\input|`.
- Warned about: `\csname`, `\catcode` and `\scantokens`, which can build a file name the check
  cannot see. Add such templates only from sources you trust.
- At build time, a build in which Tectonic reports reading an absolute path is refused and no
  PDF is saved. XeLaTeX and LuaLaTeX run with kpathsea's paranoid file access instead.

Comments are ignored by these checks.

## Recommendations (warnings, not errors)

- Turn hyphenation off (`\hyphenpenalty=10000`): applicant tracking systems miss split words.
- Keep text as text: no information only in icons or images. jaa checks the PDF's extractable
  text after every build and lists what it cannot find.
- Use fonts with Polish diacritics if you build Polish CVs.
