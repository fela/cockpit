"use strict";

// A shell command laid out for reading on a blocked call's card (SPEC PERM-22).
//
// `CockpitShell.lines(command)` splits the command into its parts, one per line: at `;` and line breaks, which it
// drops, and after `&&`, `||`, `|` and `&`, which end their line, so the text still runs when copied. A `{ }` or `( )`
// group opens a line of its own and indents what is inside it by two spaces; a line continued with `\` keeps its break
// and indents the next line. Quoted text, `$( )`, backticks and a heredoc's body stay as written.
//
// Each line is `{ indent, tokens }`, each token `{ kind, text }`: kind is "command" (the word a part runs), "keyword"
// (if, then, for, do, done...), "option" (-x, --flag), "string" (quoted text), "variable" ($NAME, ${...}, $( ),
// backticks, NAME= before a command), "operator" (the separators, redirections and group marks), "comment", "heredoc"
// (a heredoc's body, with its end line) or "text". Joining every line's indent and tokens with line breaks gives back
// the command, save the dropped `;` and the spaces around the breaks.
//
// It reads no more than a person skimming would need: it does not check that the command is valid, and a part it cannot
// read (an unclosed quote) runs to the end of the command as written.

(() => {
  const KEYWORDS = new Set(["if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac", "in", "select", "function", "!"]);
  // After these the next word names a variable or a pattern, not a command.
  const NAMING = new Set(["for", "select", "case"]);
  const ENDS_LINE = new Set(["&&", "||", "|", "|&", "&"]);
  const WORD_END = /[\s;&|<>()'"`$]/;

  // Where a quote or $( ) that starts at `start` ends (one past its close), or the command's length.
  function closing(s, start) {
    const open = s[start];
    if (open === "'") {
      const end = s.indexOf("'", start + 1);
      return end < 0 ? s.length : end + 1;
    }
    if (open === '"' || open === "`") {
      for (let i = start + 1; i < s.length; i++) {
        if (s[i] === "\\") i++;
        else if (s[i] === open) return i + 1;
        // Inside "...", a $( ) or backticks may hold quotes of their own: echo "$(echo "a;b")".
        else if (open === '"' && s[i] === "$" && s[i + 1] === "(") i = closing(s, i + 1) - 1;
        else if (open === '"' && s[i] === "`") i = closing(s, i) - 1;
      }
      return s.length;
    }
    // $( or <( or >(, with `start` at its (: to the matching ), past quotes inside. A heredoc inside it whose body
    // holds a ) ends it early; the rest then shows as written, only laid out wrongly.
    let depth = 1;
    for (let i = start + 1; i < s.length; i++) {
      const c = s[i];
      if (c === "\\") i++;
      else if (c === "'" || c === '"' || c === "`") i = closing(s, i) - 1;
      else if (c === "(") depth++;
      else if (c === ")" && --depth === 0) return i + 1;
    }
    return s.length;
  }

  // The command as tokens, with "space", "break" (a line break or `;`), "join" (`&&`, `|`...), "open" and "close"
  // (a group's marks) and "continue" (`\` at a line's end) for the layout.
  function tokens(s) {
    const out = [];
    const heredocs = [];
    let i = 0;
    let wordStart = true;
    // Open `( )` and `{ }` groups, innermost last: a `)` or `}` closes one only when one is open.
    const groups = [];
    const push = (kind, text) => { out.push({ kind, text }); };
    // Whether a command may start here, so a `{` or `(` opens a group: at the start, after a separator, a group's
    // opening, `then`, `do`, `else`, or a function's `name()`, also across a `\` line end.
    const commandStart = () => {
      for (let k = out.length - 1; k >= 0; k--) {
        const { kind, text } = out[k];
        if (kind === "space" || kind === "continue") continue;
        if (kind === "break" || kind === "join" || kind === "open") return true;
        if (kind === "word") return ["then", "do", "else", "!"].includes(text);
        return kind === "operator" && text === ")" && out[k - 1] && out[k - 1].text === "(";
      }
      return true;
    };
    const afterFor = () => {
      // A `\` line end between `for` and `((` is space too, also when the word holds it (`for\` then a newline).
      const last = out.filter((token) => token.kind !== "space" && token.kind !== "continue").pop();
      return Boolean(last) && last.kind === "word" && last.text.replace(/\\\n/g, "") === "for";
    };
    while (i < s.length) {
      const c = s[i];
      const rest = s.slice(i);
      let m;
      if (c === "\n") {
        push("break", "\n");
        i++;
        // A heredoc's body follows the line that opened it, as written, to its end line.
        while (heredocs.length) {
          const { tag, strip } = heredocs.shift();
          let end = i;
          let found = false;
          while (end < s.length) {
            const lineEnd = s.indexOf("\n", end) < 0 ? s.length : s.indexOf("\n", end);
            const line = s.slice(end, lineEnd);
            end = Math.min(lineEnd + 1, s.length);
            if ((strip ? line.replace(/^\t+/, "") : line) === tag) { found = true; break; }
          }
          push("heredoc", s.slice(i, end).replace(/\n$/, ""));
          i = end;
          if (found && i < s.length) push("break", "\n");
        }
        wordStart = true;
      } else if (c === " " || c === "\t") {
        m = /^[ \t]+/.exec(rest);
        push("space", m[0]);
        i += m[0].length;
        wordStart = true;
      } else if (c === "\\" && s[i + 1] === "\n") {
        push("continue", "\\");
        i += 2;
        wordStart = true;
      } else if (c === "#" && wordStart) {
        const end = s.indexOf("\n", i) < 0 ? s.length : s.indexOf("\n", i);
        push("comment", s.slice(i, end));
        i = end;
      } else if ((m = /^(&&|\|\||\|&|\||&(?![>&]))/.exec(rest))) {
        // On the line that opens a heredoc, a break would put the rest of the line into the body.
        push(heredocs.length ? "operator" : "join", m[0]);
        i += m[0].length;
        wordStart = true;
      } else if (c === ";") {
        m = /^(;;&|;&|;+)/.exec(rest);
        // `;;`, `;&` and `;;&` end a case branch: kept, as they read as part of the syntax.
        if (m[0].length > 1 || heredocs.length) push("operator", m[0]);
        if (!heredocs.length) push("break", ";");
        i += m[0].length;
        wordStart = true;
      } else if ((m = /^<<(-?)[ \t]*(\\?)(['"]?)([^\s'"`;&|<>()]+)\3/.exec(rest))) {
        heredocs.push({ tag: m[4], strip: m[1] === "-" });
        push("operator", m[0]);
        i += m[0].length;
        wordStart = false;
      } else if ((m = /^(\d*(>>|>&|<&|&>>|&>|<<<|>\||<>|>|<)(\d+-?|-)?)/.exec(rest)) && !/^[<>]\(/.test(rest)) {
        push("operator", m[0]);
        i += m[0].length;
        wordStart = true;
      } else if ((wordStart && (rest.startsWith("((") || /^\[\[\s/.test(rest)) && commandStart())
          || (rest.startsWith("((") && afterFor())) {
        // Arithmetic (( )), also a `for (( ; ; ))` header (written `for((` too), and a test [[ ]] are one part: their
        // parentheses, `;` and | are not the shell's.
        const end = rest.startsWith("((") ? closing(s, i + 1) : s.indexOf("]]", i) < 0 ? s.length : s.indexOf("]]", i) + 2;
        const close = rest.startsWith("((") && s[end] === ")" ? end + 1 : end;
        push("test", s.slice(i, close));
        i = close;
        wordStart = false;
      } else if (c === "{" && wordStart && (i + 1 >= s.length || /\s/.test(s[i + 1])) && commandStart()) {
        groups.push("}");
        push("open", c);
        i++;
        wordStart = true;
      } else if (c === "}" && wordStart && groups[groups.length - 1] === "}" && (i + 1 >= s.length || /[\s;)|&]/.test(s[i + 1]))
          && /^(break|close|heredoc)$/.test((out.filter((token) => token.kind !== "space").pop() || {}).kind || "break")) {
        groups.pop();
        push("close", c);
        i++;
        wordStart = true;
      } else if (c === "(" && wordStart && commandStart()) {
        groups.push(")");
        push("open", c);
        i++;
        wordStart = true;
      } else if (c === ")" && groups[groups.length - 1] === ")") {
        groups.pop();
        push("close", c);
        i++;
        wordStart = true;
      } else if (c === "(" || c === ")") {
        // A function's `name()`, or a case pattern's `x)`: part of the line.
        push("operator", c);
        i++;
        wordStart = c === ")";
      } else if (c === "'" || c === '"') {
        const end = closing(s, i);
        push("string", s.slice(i, end));
        i = end;
        wordStart = false;
      } else if (c === "`" || /^[$<>]\(/.test(rest)) {
        const end = closing(s, c === "`" ? i : i + 1);
        push("variable", s.slice(i, end));
        i = end;
        wordStart = false;
      } else if (c === "$") {
        m = /^\$(\{[^}]*\}?|[A-Za-z_]\w*|[0-9?#@*$!-])?/.exec(rest);
        push("variable", m[0]);
        i += m[0].length;
        wordStart = false;
      } else {
        // A backslash keeps the character after it in the word, such as find's `\;`.
        let end = c === "\\" ? i + 2 : i + 1;
        while (end < s.length && !WORD_END.test(s[end])) end += s[end] === "\\" ? 2 : 1;
        push("word", s.slice(i, end));
        i = end;
        wordStart = false;
      }
    }
    return out;
  }

  function lines(command) {
    const out = [];
    let depth = 0;
    let line = null;
    // Whether the next word is the command of a part, and whether the last word token was a word.
    let commandNext = true;
    const start = (extra) => {
      line = { indent: "  ".repeat(Math.max(depth, 0) + (extra || 0)), tokens: [] };
      out.push(line);
    };
    const end = () => {
      if (line) {
        while (line.tokens.length && line.tokens[line.tokens.length - 1].kind === "text" && !line.tokens[line.tokens.length - 1].text.trim()) {
          line.tokens.pop();
        }
      }
      if (line && !line.tokens.length) out.pop();
      line = null;
    };
    const add = (kind, text) => {
      if (!line) start();
      const last = line.tokens[line.tokens.length - 1];
      // Neighbouring words of one kind, such as a word and the `=` value after it, stay one token.
      if (last && last.kind === kind && kind === "text") last.text += text;
      else line.tokens.push({ kind, text });
    };
    let afterWord = false;
    for (const token of tokens(command)) {
      switch (token.kind) {
        case "space":
          if (line && line.tokens.length) add("text", " ");
          afterWord = false;
          break;
        case "break":
          end();
          commandNext = true;
          afterWord = false;
          break;
        case "continue":
          if (!line || !line.tokens.length || !line.tokens[line.tokens.length - 1].text.endsWith(" ")) add("text", " ");
          add("operator", "\\");
          end();
          start(1);
          afterWord = false;
          break;
        case "join":
          if (line && line.tokens.length) {
            const last = line.tokens[line.tokens.length - 1];
            if (last.kind !== "text" || !last.text.endsWith(" ")) add("text", " ");
          }
          add("operator", token.text);
          end();
          commandNext = true;
          afterWord = false;
          break;
        case "open":
          add("operator", token.text);
          end();
          depth++;
          commandNext = true;
          afterWord = false;
          break;
        case "close":
          end();
          depth--;
          add("operator", token.text);
          commandNext = false;
          afterWord = false;
          break;
        case "heredoc":
          end();
          out.push({ indent: "", tokens: [{ kind: "heredoc", text: token.text }] });
          afterWord = false;
          break;
        case "word": {
          // A word glued to the one before it (a=b"c") keeps the first one's part.
          if (afterWord) { add("text", token.text); break; }
          let kind = "text";
          if (commandNext && /^[A-Za-z_]\w*=/.test(token.text)) kind = "variable";
          else if (commandNext && KEYWORDS.has(token.text)) {
            kind = "keyword";
            commandNext = !NAMING.has(token.text) && token.text !== "in";
          } else if (commandNext) {
            kind = "command";
            commandNext = false;
          } else if (KEYWORDS.has(token.text) && (token.text === "in" || token.text === "do")) {
            kind = "keyword";
            commandNext = token.text === "do";
          } else if (/^--?[A-Za-z0-9]/.test(token.text)) kind = "option";
          add(kind, token.text);
          afterWord = true;
          break;
        }
        case "test":
          add("text", token.text);
          commandNext = false;
          afterWord = true;
          break;
        default:
          add(token.kind, token.text);
          afterWord = token.kind === "string" || token.kind === "variable";
      }
    }
    end();
    return out;
  }

  const api = { lines };
  if (typeof module !== "undefined") module.exports = api;
  else window.CockpitShell = api;
})();
