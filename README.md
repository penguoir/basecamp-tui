# basecamp-tui

Bash through your Basecamp inbox from the terminal, with vim keys.

One Python file, standard library only. It talks to Basecamp through the official
[`basecamp` CLI](https://github.com/basecamp/basecamp-cli), so it uses whatever account
that CLI is logged into. No tokens to set up.

- Read every notification's whole thread (comments, chat lines, check-in answers). It
  jumps to the comment that pinged you.
- Mark read, complete todos, move cards to Done or Not now, with undo.
- Reply in vim, with real `@mentions`.
- Hand anything to a [Claude Code](https://claude.com/claude-code) session with one key.

## Install

Needs Python 3.11+, the [`basecamp` CLI](https://github.com/basecamp/basecamp-cli) (logged in)
and, optionally, `claude` for the Claude key.

```sh
curl -fsSL https://raw.githubusercontent.com/penguoir/basecamp-tui/main/basecamp-tui \
  -o ~/.local/bin/basecamp-tui && chmod +x ~/.local/bin/basecamp-tui
basecamp-tui
```

## Keys

Press `?` inside for this list.

| | Key | Does |
|---|---|---|
| **Move** | `j` `k` / arrows | next / previous |
| | `gg` `G`, `{count}j` | top, bottom, jump by count |
| | `C-d` `C-u` `C-f` `C-b` | half / full page |
| | `H` `M` `L` | top / middle / bottom of the screen |
| **Open** | `l` / Enter | open the thread (side by side when the terminal is 140+ columns wide) |
| | `h` / Esc | back to the list |
| | `J` `K` | next / previous notification, also from inside a thread |
| **Triage** | `e` or `dd` | mark read and move on (takes a count, or a `V` range) |
| | `V` | visual line mode |
| | `D` | done: complete the todo, or move the card to its board's Done column |
| | `X` | archive: archive the todo, or move the card to Not now (asks y/n) |
| | `u` | undo the last mark-read, done or archive |
| | `r` | reply in vim: opens in insert mode, `:wq` sends, `:q!` drops it. On a check-in reminder it posts your answer. |
| | `c` / `C` | type a message and press Enter: starts a background Claude Code session with it and the item's URL. You stay in the inbox; pick the session up in `claude agents`. |
| | `o` / `y` | open in browser / copy the link (OSC 52, works over SSH) |
| **View** | Tab | unread only / everything |
| | `/` `n` `N` | search, next, previous |
| | `R`, `:q` / `q` | refresh, quit |

### @mentions

In a reply, `@jane`, `@ja` or `@Jane.Smith` becomes a real Basecamp mention. If a name matches
nobody or more than one person, nothing is sent: vim reopens with your text and a note
listing the options. In vim, `C-n` completes names.

## Configuration

| Variable | Default | |
|---|---|---|
| `BASECAMP_TUI_SESSION_DIR` | current directory | where Claude sessions start. `claude --bg` refuses untrusted directories and `$HOME`, so point this at a trusted workspace. |
| `BASECAMP_TUI_CLAUDE_FLAGS` | none | extra flags for the background sessions, e.g. `--dangerously-skip-permissions` (the session can then act in Basecamp and on your machine without asking) |
| `BASECAMP_BIN` | `basecamp` | path to the Basecamp CLI |
| `CLAUDE_BIN` | `claude` | path to Claude Code |
| `VISUAL` / `EDITOR` | `vim` | editor for replies (vi, vim and nvim start in insert mode) |

## License

MIT
