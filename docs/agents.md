# Leaf and AI agents

Leaf can work with AI agents (Claude Desktop, Claude Code or any other MCP client) in two ways:

1. **`leaf mcp`**: an MCP server on stdio. Agents read PDFs, find text and write *new* files (edits applied,
   OCR, redactions, text export). It never overwrites a file and never signs anything.
2. **The live agent link**: when Leaf is running and you turned the link on, agents can see the documents you
   have open and **suggest** edits. Suggestions show up in Leaf's *Agent suggestions* panel; nothing changes until
   you click Accept, and Leaf never saves for an agent.

## Register the MCP server

The command is `leaf mcp` (installed Leaf: the `leaf` executable, packaged builds: `Leaf.exe mcp`; source
checkout: `uv run leaf mcp`). *Settings › Agents › Copy MCP config* copies a ready-made snippet with the full
path of this installation.

Claude Desktop (`claude_desktop_config.json`, Settings › Developer › Edit config):

```json
{
  "mcpServers": {
    "leaf": {
      "command": "C:\\Program Files\\Leaf\\Leaf.exe",
      "args": ["mcp"]
    }
  }
}
```

From a source checkout:

```json
{
  "mcpServers": {
    "leaf": {
      "command": "uv",
      "args": ["--directory", "D:\\path\\to\\leaf", "run", "leaf", "mcp"]
    }
  }
}
```

Claude Code:

```sh
claude mcp add leaf -- leaf mcp
# or from a checkout
claude mcp add leaf -- uv --directory /path/to/leaf run leaf mcp
```

or a project `.mcp.json` with the same `mcpServers` block as above.

### Restricting which files agents may touch

Every path must be absolute and local: URLs, UNC paths (`\\server\share`) and network drives are refused, and
inputs must be `.pdf` files. Start the server with `--root DIR` (repeatable) or set `LEAF_MCP_ROOTS` (folders
separated by `;` on Windows, `:` elsewhere) to allow only files inside those folders:

```json
"args": ["mcp", "--root", "C:\\Users\\me\\Documents\\Contracts"]
```

Files an edit set reads (pages inserted from a file, replacement images) follow the same rules.

## Tools

| Tool | What it does |
|---|---|
| `pdf_info` | page count and sizes, metadata, encryption, form field / annotation / outline / signature counts |
| `pdf_text` | text per page in reading order; `boxes: true` adds every word with `[x0, y0, x1, y1, word]`; `pages` like `"1-3, 7"` |
| `pdf_fields` | form fields: page, name, type, value, choices, rect, read-only |
| `pdf_find` | `query` (plain text), `regex`, or `preset` (`email`, `phone`, `card`, `iban`, `date`); hits with page, context and rects |
| `pdf_propose_edits` | sends an edit set to the running Leaf (it opens the file if needed; you review it), else writes `<name>-proposal.leaf-edits.json` next to the PDF |
| `pdf_proposal_status` | `pending`, `accepted`, `partially accepted`, `rejected`, `withdrawn` or `failed` |
| `pdf_apply_edits_to_copy` | applies an edit set headlessly and writes a NEW PDF (`<name>-edited.pdf`, or `output`) |
| `pdf_ocr_to_copy` | recognizes scanned pages (Tesseract `eng` built in; other languages if installed in Leaf) into a NEW searchable PDF |
| `pdf_redact_to_copy` | finds hits like `pdf_find`; **only with `confirm: true`** writes a NEW PDF with them truly removed (text, pixels, vector art, annotations, fields) |
| `pdf_export_text` | writes the text (or Markdown with `markdown: true`) to a NEW `.txt`/`.md` file |
| `leaf_documents` | the documents open in the running Leaf (needs the link) |
| `leaf_read` | an open document as you see it in Leaf, unsaved edits included: text (+ boxes), fields, annotations, outline, pending edits |

Coordinates are PDF points in the page's display space: origin at the top left of the page as shown, the page's
own rotation applied. Edit sets use the same space; their format is described in `docs/edit-set.md` (written by
`leaf.editset`). A small example:

```json
{
  "leaf_edits": 1,
  "comment": "Fill in the applicant",
  "forms": {"name": "Jane Q. Public", "agree": true},
  "annotations": [{"type": "note", "page": 1, "at": [72, 72], "contents": "Please check the date"}]
}
```

## The live agent link

Off by default. Turn it on in **Settings › Agents** ("Let AI agents connect to Leaf"). Leaf then writes a random
token to a file only you can read:

| OS | Token file |
|---|---|
| Windows | `%APPDATA%\Leaf\Leaf\agent-token` |
| macOS | `~/Library/Application Support/Leaf/Leaf/agent-token` |
| Linux | `~/.local/share/Leaf/Leaf/agent-token` (mode 0600) |

Every request must carry the token. *Regenerate token* revokes the old one and disconnects connected agents;
turning the link off deletes the file. The settings page also lists recent agent activity (connections,
proposals and your decisions).

When an agent proposes edits, the *Agent suggestions* panel (View menu) opens with the suggestion and its
changes, each with a checkbox:

- **Accept** applies the ticked changes of the selected suggestion as one undo step, "Agent: &lt;title&gt;".
- **Reject** discards it. **Accept all** applies every listed suggestion in full.
- **Auto-accept further suggestions (this session)** applies new suggestions for that document right away (still
  one undo step each) until Leaf closes.
- Double-click a change to go to its page.

Agents can't save, sign, close documents or open dialogs: those requests don't exist (`save`/`sign` are
answered with a refusal). You save when you're happy with the result.

### Protocol (for other clients)

The link listens on Leaf's per-user local server: on Windows the named pipe `\\.\pipe\leaf-<hash>`, elsewhere
the Unix socket `$XDG_RUNTIME_DIR/leaf-<hash>.sock` (the token file names it under `server`). Messages are JSON
objects, one per line; each request gets one response line `{"ok": true|false, ...}`.

| Request | Response |
|---|---|
| `{"op": "hello", "token": T, "client": "My agent"}` | `{"session", "pid"}` |
| `{"op": "list", "token": T}` | `{"documents": [{"doc", "path", "name", "pages", "modified", "active"}]}` |
| `{"op": "read", "token": T, "doc": 1, "pages": "1-2", "boxes": true, "include": ["text", "fields", "annotations", "outline", "edits"]}` | `{"document": {...}}` |
| `{"op": "propose", "token": T, "path": "/abs/file.pdf", "open": true, "title": "...", "edits": {...}}` | `{"proposal", "status": "pending", "changes"}` |
| `{"op": "status", "token": T, "proposal": 3}` | `{"status", "accepted", "total", "warnings"}` |
| `{"op": "withdraw", "token": T, "proposal": 3}` | `{"status"}` |

A wrong or old token gets `{"ok": false, "auth": false}`: read the file again.

## Single instance

The same local server makes Leaf single-instance: `leaf file.pdf` while Leaf is running hands the file to the
running Leaf (each file opens in its window if it's already open, else in a new window, which comes to the
front) and exits at once. `leaf` with no files brings the running Leaf to the front; `leaf --new-window` opens an
empty window in it (the Linux desktop file's "New Window" action). `--new-instance` (or
`LEAF_NO_SINGLE_INSTANCE=1`) starts a separate Leaf anyway; `--self-test` and the command-line subcommands
always run on their own.
