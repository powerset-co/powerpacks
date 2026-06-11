export function parseJsonFragment(text: string): Record<string, any> | null {
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (char !== "{" && char !== "[") continue;
    try {
      const parsed = JSON.parse(text.slice(i));
      return typeof parsed === "object" && parsed ? parsed as Record<string, any> : { payload: parsed };
    } catch {
      continue;
    }
  }
  return null;
}

export function parseLastJsonFragment(text: string): Record<string, any> | null {
  let start = -1;
  let inString = false;
  let escaped = false;
  const stack: string[] = [];
  let last: Record<string, any> | null = null;

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (char === "\\") {
        escaped = true;
      } else if (char === "\"") {
        inString = false;
      }
      continue;
    }
    if (char === "\"") {
      inString = true;
      continue;
    }
    if (char === "{" || char === "[") {
      if (stack.length === 0) start = i;
      stack.push(char === "{" ? "}" : "]");
      continue;
    }
    if ((char === "}" || char === "]") && stack.length > 0 && stack[stack.length - 1] === char) {
      stack.pop();
      if (stack.length === 0 && start >= 0) {
        try {
          const parsed = JSON.parse(text.slice(start, i + 1));
          if (typeof parsed === "object" && parsed) last = parsed as Record<string, any>;
        } catch {
          // Logs can contain braces that are not JSON payloads.
        }
        start = -1;
      }
    }
  }

  return last || parseJsonFragment(text);
}
